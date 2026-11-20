"""Verified stage imports, never a whitelist of successful papers."""

import json
import shutil
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from arxiv_pipeline.pipeline import parse
from tests.parsing.test_ms2_benchmark import PAPER, make_parsed_output


def fixture(tmp_path, count=1):
    legacy = make_parsed_output(tmp_path, count=count)
    source = tmp_path / "ms1"
    target = tmp_path / "current"
    target.mkdir()
    records, _, _ = parse._discover(source, [PAPER])
    return source, legacy, target, records


def test_same_consumer_input_reuses_hierarchy_and_normalize_without_calling_producers(tmp_path):
    from arxiv_pipeline.pipeline.stage_migration import prepare_batch

    source, legacy, target, records = fixture(tmp_path)
    before = {p: p.read_bytes() for p in legacy.rglob("*") if p.is_file()}
    # A change to unused input changes the version snapshot but not its body/tree.
    (source / f"corpus/{PAPER}/v1/unused.tex").write_text("Unused source.")
    report = prepare_batch(source, target, legacy, records)
    assert {(r["stage"], r["version"]) for r in report["decisions"] if r["action"] == "reused"} >= {
        ("hierarchy", 1),
        ("normalize", 1),
    }
    with (
        patch(
            "arxiv_pipeline.parsing.hierarchy.build_hierarchy_tree",
            side_effect=AssertionError("hierarchy recomputed"),
        ),
        patch(
            "arxiv_pipeline.parsing.normalization.transform_tree_to_normalized",
            side_effect=AssertionError("normalize recomputed"),
        ),
    ):
        result = parse.run([PAPER], target, input_root=source, resume=True)
    assert result["complete"]
    assert (target / f"parsed/{PAPER}/v1/normalized_tree.json").read_bytes() == (
        legacy / f"parsed/{PAPER}/v1/normalized_tree.json"
    ).read_bytes()
    assert {p: p.read_bytes() for p in legacy.rglob("*") if p.is_file()} == before


def test_changed_body_does_not_reuse_old_tree(tmp_path):
    from arxiv_pipeline.pipeline.stage_migration import prepare_batch

    source, legacy, target, records = fixture(tmp_path)
    tex = source / f"corpus/{PAPER}/v1/main.tex"
    tex.write_text(r"\documentclass{article}\begin{document}New content.\end{document}")
    report = prepare_batch(source, target, legacy, records)
    assert not any(
        r["action"] == "reused" and r["stage"] in {"hierarchy", "normalize"}
        for r in report["decisions"]
    )
    assert parse.run([PAPER], target, input_root=source, resume=True)["complete"]
    rows = json.loads((target / f"parsed/{PAPER}/v1/normalized_tree.json").read_text())["nodes"]
    assert any("New content" in r["cleaned_content"] for r in rows)


def test_corrupt_or_unproven_artifact_is_recomputed(tmp_path):
    from arxiv_pipeline.pipeline.stage_migration import prepare_batch

    source, legacy, target, records = fixture(tmp_path)
    (legacy / f"parsed/{PAPER}/v1/raw_tree.json").write_text("{}")
    report = prepare_batch(source, target, legacy, records)
    assert not any(
        r["action"] == "reused" and r["stage"] in {"hierarchy", "normalize"}
        for r in report["decisions"]
    )
    assert parse.run([PAPER], target, input_root=source, resume=True)["complete"]


def test_missing_fingerprint_does_not_count_success_as_evidence(tmp_path):
    from arxiv_pipeline.pipeline.stage_migration import prepare_batch

    source, legacy, target, records = fixture(tmp_path)
    (legacy / f"state/stages/hierarchy/{PAPER}/1.dependencies.json").unlink()
    report = prepare_batch(source, target, legacy, records)
    assert not any(
        r["action"] == "reused" and r["stage"] == "hierarchy" for r in report["decisions"]
    )


def test_current_valid_results_take_priority_over_legacy_and_remain_cached(tmp_path):
    from arxiv_pipeline.pipeline.stage_migration import prepare_batch

    source, legacy, target, records = fixture(tmp_path)
    shutil.copytree(legacy, target, dirs_exist_ok=True)
    before = {p: p.read_bytes() for p in target.glob(f"state/stages/*/{PAPER}/*.json")}
    report = prepare_batch(source, target, legacy, records)
    assert not any(r["action"] == "reused" for r in report["decisions"])
    assert all(p.read_bytes() == data for p, data in before.items())
    events = []
    assert parse.run([PAPER], target, input_root=source, resume=True, observer=events.append)[
        "complete"
    ]
    assert all(e["cached"] for e in events)


def test_unrecognized_producer_fingerprint_is_rejected_even_with_good_rows(tmp_path):
    from arxiv_pipeline.pipeline.stage_migration import prepare_batch

    source, legacy, target, records = fixture(tmp_path)
    path = legacy / f"state/stages/hierarchy/{PAPER}/1.dependencies.json"
    meta = json.loads(path.read_text())
    meta["fingerprint"] = "0" * 64
    path.write_text(json.dumps(meta))
    report = prepare_batch(source, target, legacy, records)
    assert not any(
        r["action"] == "reused" and r["stage"] == "hierarchy" for r in report["decisions"]
    )


def test_preparation_metrics_do_not_count_newly_computed_resolver_as_cached(tmp_path):
    from arxiv_pipeline.pipeline.stage_migration import prepare_batch, merge_events

    source, legacy, target, records = fixture(tmp_path)
    report = prepare_batch(source, target, legacy, records)
    events = []
    parse.run([PAPER], target, input_root=source, resume=True, observer=events.append)
    combined = merge_events(report["events"], events)
    assert len(combined) == 6
    assert not next(e for e in combined if e["stage"] == "resolve")["cached"]
    assert next(e for e in combined if e["stage"] == "normalize")["migrated"]


def test_shared_input_or_output_roots_are_rejected_before_writing(tmp_path):
    import pytest
    from arxiv_pipeline.pipeline.stage_migration import prepare_batch

    source, legacy, target, records = fixture(tmp_path)
    with pytest.raises(ValueError):
        prepare_batch(source, target, source, records)
    with pytest.raises(ValueError):
        prepare_batch(source, target, target, records)
    assert list(target.iterdir()) == []


def test_optimized_runner_imports_verified_old_stages_but_keeps_new_results_on_resume(tmp_path):
    from arxiv_pipeline.pipeline import optimized_staging as fast

    source, legacy, target, records = fixture(tmp_path)
    job = dict(
        member="tv1",
        input=str(source),
        ids=[PAPER],
        total_assigned=1,
        assignment_range=f"{PAPER}:{PAPER}",
        legacy_output=str(legacy),
    )
    result = fast.run(
        job, target, local_root=tmp_path / "work", code_sha="current-parser", workers=2
    )
    assert result["migrated_stages"] == 2
    proof = target / "migration-reports"
    assert list(proof.glob("*.json"))
    again = fast.run(
        job, target, local_root=tmp_path / "work2", code_sha="current-parser", workers=2
    )
    assert again["skipped_completed_papers"] == 1 and again["total_stages"] == 0


def batch_only(legacy):
    from arxiv_pipeline.pipeline import optimized_staging as fast
    from arxiv_pipeline.common.utils import atomic_json

    files = [p.relative_to(legacy).as_posix() for p in legacy.rglob("*") if p.is_file()]
    archive = legacy / "local-batches/previous.zip"
    archive.parent.mkdir()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for relative in files:
            bundle.write(legacy / relative, relative)
    atomic_json(
        archive.with_suffix(".json"),
        dict(
            format="ms2-local-batch-1",
            archive="local-batches/previous.zip",
            sha256=fast._hash(archive),
            paper_files={PAPER: files},
        ),
    )
    for relative in files:
        (legacy / relative).unlink()
    return archive


def run_optimized(source, legacy, target, workspace):
    from arxiv_pipeline.pipeline import optimized_staging as fast

    job = dict(
        member="tv1",
        input=str(source),
        ids=[PAPER],
        total_assigned=1,
        assignment_range=f"{PAPER}:{PAPER}",
        legacy_output=str(legacy),
    )
    return fast.run(job, target, local_root=workspace, code_sha="current-parser", workers=2)


def test_archive_only_legacy_results_are_authenticated_and_reused(tmp_path):
    source, legacy, target, _ = fixture(tmp_path)
    batch_only(legacy)
    result = run_optimized(source, legacy, target, tmp_path / "workspace")
    assert result["complete"] and result["migrated_stages"] == 2
    with zipfile.ZipFile(target / "MS2_RESULTS.zip") as bundle:
        meta = json.loads(bundle.read(f"tv1/state/stages/normalize/{PAPER}/1.dependencies.json"))
        assert meta["migration"]["strategy"] == "equal-consumer-input"


def test_corrupt_legacy_zip_falls_back_to_current_producers(tmp_path):
    source, legacy, target, _ = fixture(tmp_path)
    batch_only(legacy).write_bytes(b"corrupt old upload")
    result = run_optimized(source, legacy, target, tmp_path / "workspace")
    assert result["complete"] and result["migrated_stages"] == 0
    report = json.loads(next((target / "migration-reports").glob("*.json")).read_text())
    assert any("checksum" in i["reason"] for i in report["staging_issues"])


def test_changed_version_recomputes_only_its_tree_stages(tmp_path):
    from arxiv_pipeline.pipeline.stage_migration import prepare_batch

    source, legacy, target, records = fixture(tmp_path, count=2)
    tex = source / f"corpus/{PAPER}/v1/main.tex"
    tex.write_text(r"\documentclass{article}\begin{document}Changed.\end{document}")
    report = prepare_batch(source, target, legacy, records)
    reused = {(d["stage"], d["version"]) for d in report["decisions"] if d["action"] == "reused"}
    assert reused == {("hierarchy", 2), ("normalize", 2)}
    assert parse.run([PAPER], target, input_root=source, resume=True)["complete"]


def test_enabling_migration_preserves_already_completed_current_output(tmp_path):
    from arxiv_pipeline.pipeline import optimized_staging as fast, completion_index

    source, legacy, target, _ = fixture(tmp_path)
    job = dict(
        member="tv1",
        input=str(source),
        ids=[PAPER],
        total_assigned=1,
        assignment_range=f"{PAPER}:{PAPER}",
    )
    identity = completion_index.context(source, job)
    fast.run(job, target, local_root=tmp_path / "work", code_sha="old-notebook")
    job["legacy_output"] = str(legacy)
    assert completion_index.context(source, job) == identity
    with (
        patch.object(fast, "_stage_inputs", side_effect=AssertionError("source recopied")),
        patch.object(
            fast, "_stage_legacy", side_effect=AssertionError("legacy scanned for complete ID")
        ),
    ):
        result = fast.run(job, target, local_root=tmp_path / "work2", code_sha="new-notebook")
    assert result["skipped_completed_papers"] == 1 and result["migrated_stages"] == 0


def test_optimized_runner_rejects_overlapping_legacy_before_any_output_write(tmp_path):
    import pytest

    source, legacy, target, _ = fixture(tmp_path)
    for bad in (source, target, tmp_path / "workspace"):
        bad.mkdir(exist_ok=True)
        with pytest.raises(ValueError, match="disjoint"):
            run_optimized(source, bad, target, tmp_path / "workspace")
    assert list(target.iterdir()) == []


def test_wrong_version_in_legacy_artifact_is_not_reused(tmp_path):
    from arxiv_pipeline.pipeline.stage_migration import prepare_batch

    source, legacy, target, records = fixture(tmp_path)
    tree = legacy / f"parsed/{PAPER}/v1/raw_tree.json"
    payload = json.loads(tree.read_text())
    payload["version"] = 2
    tree.write_text(json.dumps(payload))
    report = prepare_batch(source, target, legacy, records)
    assert not any(d["action"] == "reused" for d in report["decisions"])


def test_partial_processing_receipt_includes_honest_migration_metrics(tmp_path):
    from arxiv_pipeline.pipeline import optimized_staging as fast

    source, legacy, target, _ = fixture(tmp_path)
    job = dict(
        member="tv1",
        input=str(source),
        ids=[PAPER],
        total_assigned=1,
        assignment_range=f"{PAPER}:{PAPER}",
        legacy_output=str(legacy),
    )
    result = fast.run(job, target, local_root=tmp_path / "work", code_sha="test", finalize=False)
    assert result["migrated_stages"] == 2 and result["cached_stages"] == 2
    assert result["total_stages"] == 6 and not result["full_assignment_accepted"]


def test_malformed_optional_legacy_receipt_does_not_block_parsing(tmp_path):
    source, legacy, target, _ = fixture(tmp_path)
    archive = batch_only(legacy)
    receipt = json.loads(archive.with_suffix(".json").read_text())
    receipt.pop("archive")
    archive.with_suffix(".json").write_text(json.dumps(receipt))
    result = run_optimized(source, legacy, target, tmp_path / "workspace")
    assert result["complete"] and result["migrated_stages"] == 0


def test_notebook_selects_legacy_as_separate_read_only_input(tmp_path):
    repo = Path(__file__).parents[2]
    notebook = json.loads((repo / "colab/MS2_Integration_CPU.ipynb").read_text(encoding="utf-8"))
    scope = next(
        "".join(c["source"]) for c in notebook["cells"] if "MEMBER_ROWS =" in "".join(c["source"])
    )
    legacy = tmp_path / "old/tv5"
    legacy.mkdir(parents=True)
    (legacy / "parsed").mkdir()
    (tmp_path / "data").mkdir()
    context = dict(
        Path=Path,
        REPO=repo,
        MEMBER="tv5",
        RUN_MODE="SMOKE",
        MS1_INPUT=str(tmp_path / "data"),
        OUTPUT_ROOT=str(tmp_path / "results"),
        RUN_TAG="ms2-repaired-20261119",
        SMOKE_IDS="2410.03026",
        VERIFY_RESUME=False,
        CODE_SHA="local-preview",
        LEGACY_MS2_INPUT=str(legacy),
    )
    exec(compile(scope, "notebook-scope", "exec"), context)
    assert context["JOBS"][0]["legacy_output"] == str(legacy)
    assert context["OUTPUT_BASE"] / "tv5" != legacy


def test_notebook_does_not_silently_ignore_migration_on_an_old_pinned_runtime(tmp_path):
    import pytest
    from arxiv_pipeline.pipeline import optimized_staging as fast

    repo = Path(__file__).parents[2]
    notebook = json.loads((repo / "colab/MS2_Integration_CPU.ipynb").read_text(encoding="utf-8"))
    cell = next(
        "".join(c["source"]) for c in notebook["cells"] if "results = []" in "".join(c["source"])
    )
    with patch.object(fast, "stage_migration", None):
        with pytest.raises(RuntimeError, match="runtime"):
            exec(
                compile(cell, "notebook-run", "exec"),
                dict(JOBS=[dict(legacy_output=str(tmp_path))]),
            )


def test_recomputed_stage_does_not_count_as_migrated_after_preparation(tmp_path):
    from arxiv_pipeline.pipeline.stage_migration import merge_events

    imported = dict(
        stage="normalize", paper_id=PAPER, version=1, cached=True, migrated=True, elapsed_seconds=1
    )
    recomputed = dict(imported, cached=False, migrated=False, elapsed_seconds=2)
    (event,) = merge_events([imported], [recomputed])
    assert not event["cached"] and not event.get("migrated")


def test_selected_legacy_zip_members_reject_duplicates_and_links(tmp_path):
    from arxiv_pipeline.pipeline import optimized_staging as fast
    from arxiv_pipeline.common.utils import atomic_json

    source, legacy, target, _ = fixture(tmp_path)
    archive = batch_only(legacy)
    with zipfile.ZipFile(archive, "a") as bundle:
        info = zipfile.ZipInfo(f"parsed/{PAPER}/v1/raw_tree.json")
        info.create_system = 3
        info.external_attr = 0o120777 << 16
        with pytest.warns(UserWarning, match="Duplicate name"):
            bundle.writestr(info, "../../unrelated")
        bundle.writestr("../escape.txt", "Never extract this")
    receipt = json.loads(archive.with_suffix(".json").read_text())
    receipt["sha256"] = fast._hash(archive)
    atomic_json(archive.with_suffix(".json"), receipt)
    result = run_optimized(source, legacy, target, tmp_path / "workspace")
    assert result["complete"] and result["migrated_stages"] == 0
    assert not list((tmp_path / "workspace").rglob("escape.txt"))


def test_legacy_copy_creates_directories_before_starting_windows_workers(tmp_path):
    from arxiv_pipeline.pipeline import optimized_staging as fast

    source, legacy, target, records = fixture(tmp_path)
    executor = fast.ThreadPoolExecutor

    def start_pool(*args, **kwargs):
        assert (target / f"parsed/{PAPER}/v1").is_dir()
        for stage in ("resolve", "hierarchy", "normalize"):
            assert (target / f"state/stages/{stage}/{PAPER}").is_dir()
        return executor(*args, **kwargs)

    with patch.object(fast, "ThreadPoolExecutor", side_effect=start_pool):
        fast._stage_legacy(legacy, target, records, 8)


@pytest.mark.parametrize("kind", ["empty_parent", "wrong_member"])
def test_invalid_legacy_source_stops_before_writing_current_output(tmp_path, kind):
    from arxiv_pipeline.pipeline import optimized_staging as fast
    from arxiv_pipeline.common.utils import atomic_json

    source, legacy, target, _ = fixture(tmp_path)
    if kind == "empty_parent":
        legacy = tmp_path / "wrong-parent"
        legacy.mkdir()
    else:
        atomic_json(legacy / "acceptance-config.json", dict(job=dict(member="tv5")))
    with pytest.raises(ValueError, match="[Ll]egacy"):
        run_optimized(source, legacy, target, tmp_path / "work")
    assert list(target.iterdir()) == []


def test_partial_current_tree_caches_do_not_copy_any_legacy_evidence(tmp_path):
    from arxiv_pipeline.pipeline import optimized_staging as fast
    from arxiv_pipeline.common.schemas import StageResult
    from arxiv_pipeline.common.utils import utc_now

    source, legacy, target, _ = fixture(tmp_path)

    def failed_qa(record, output_dir, *, version=None, upstream=None):
        return StageResult("validate_parse", PAPER, None, "error", utc_now(), schema_version="2.0")

    result = parse.run([PAPER], target, input_root=source, adapters={"validate_parse": failed_qa})
    assert not result["complete"]
    with patch.object(
        fast, "_stage_legacy", side_effect=AssertionError("unnecessary old Drive read")
    ):
        result = run_optimized(source, legacy, target, tmp_path / "work")
    assert result["complete"] and result["migrated_stages"] == 0


def test_default_smoke_selection_reads_discovery_locally_and_reuses_compact_cache(tmp_path):
    from arxiv_pipeline.pipeline import optimized_staging as fast
    from tests.parsing import test_ms2_benchmark as factory

    repo = Path(__file__).parents[2]
    notebook = json.loads((repo / "colab/MS2_Integration_CPU.ipynb").read_text(encoding="utf-8"))
    scope = next(
        "".join(c["source"]) for c in notebook["cells"] if "MEMBER_ROWS =" in "".join(c["source"])
    )
    with patch.object(factory, "PAPER", "2508.12872"):
        factory.make_parsed_output(tmp_path, count=1)
    source = (tmp_path / "ms1").resolve()
    context = dict(
        Path=Path,
        REPO=repo,
        MEMBER="tv1",
        RUN_MODE="SMOKE",
        MS1_INPUT=str(source),
        OUTPUT_ROOT=str(tmp_path / "outputs"),
        RUN_TAG="review",
        SMOKE_IDS="",
        VERIFY_RESUME=False,
        CODE_SHA="local-test",
        LOCAL_ROOT=tmp_path / "work",
    )
    read = parse._read

    def local_json_only(path, **kwargs):
        assert not Path(path).is_relative_to(source), "discovery JSON was parsed directly on Drive"
        return read(path, **kwargs)

    with patch.object(parse, "_read", side_effect=local_json_only):
        exec(compile(scope, "notebook-scope", "exec"), context)
    assert context["JOBS"][0]["ids"] == ["2508.12872"]
    copy = fast._copy_files

    def no_source_recopy(origin, *args, **kwargs):
        assert Path(origin).resolve() != source, "warm smoke recopied Drive discovery files"
        return copy(origin, *args, **kwargs)

    with patch.object(fast, "_copy_files", side_effect=no_source_recopy):
        exec(compile(scope, "notebook-scope", "exec"), context)


def test_only_version_with_invalid_tree_reads_legacy_and_is_repaired(tmp_path):
    from arxiv_pipeline.pipeline import optimized_staging as fast

    source, legacy, target, _ = fixture(tmp_path, count=2)
    shutil.copytree(legacy, target, dirs_exist_ok=True)
    (target / f"parsed/{PAPER}/v2/normalized_tree.json").write_text("{}")
    copied = []
    copy = fast.shutil.copyfile

    def capture(original, destination, *args, **kwargs):
        if Path(original).is_relative_to(legacy):
            copied.append(Path(original).relative_to(legacy).as_posix())
        return copy(original, destination, *args, **kwargs)

    with patch.object(fast.shutil, "copyfile", side_effect=capture):
        result = run_optimized(source, legacy, target, tmp_path / "work")
    assert result["complete"] and result["migrated_stages"] == 1
    assert len(copied) == 9
    assert not any(
        "/v1/" in name or name.endswith("/1.json") or name.endswith("/1.dependencies.json")
        for name in copied
    )


def test_legacy_metadata_with_non_object_job_is_rejected_before_writing(tmp_path):
    from arxiv_pipeline.common.utils import atomic_json

    source, legacy, target, _ = fixture(tmp_path)
    atomic_json(legacy / "acceptance-config.json", dict(job=["tv1"]))
    with pytest.raises(ValueError, match="[Ll]egacy"):
        run_optimized(source, legacy, target, tmp_path / "work")
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("missing", ["select_smoke_ids", "validate_legacy"])
def test_notebook_rejects_stale_runtime_before_discovery_or_output_writes(tmp_path, missing):
    from arxiv_pipeline.pipeline import optimized_staging as fast

    repo = Path(__file__).parents[2]
    notebook = json.loads((repo / "colab/MS2_Integration_CPU.ipynb").read_text(encoding="utf-8"))
    scope = next(
        "".join(c["source"]) for c in notebook["cells"] if "MEMBER_ROWS =" in "".join(c["source"])
    )
    source = tmp_path / "data"
    source.mkdir()
    context = dict(
        Path=Path,
        REPO=repo,
        MEMBER="tv5",
        RUN_MODE="SMOKE",
        MS1_INPUT=str(source),
        OUTPUT_ROOT=str(tmp_path / "output"),
        RUN_TAG="local-review",
        SMOKE_IDS="2410.03026",
        VERIFY_RESUME=False,
        CODE_SHA="stale-pinned-runtime",
    )
    with patch.object(fast, missing, None):
        with pytest.raises(RuntimeError, match="runtime"):
            exec(compile(scope, "notebook-scope", "exec"), context)
    assert not (tmp_path / "output").exists()
