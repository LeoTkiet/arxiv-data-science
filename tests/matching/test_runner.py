"""Offline end-to-end tests of matching.runner on synthetic publications.

The feature handoff is reproduced by computing the real pair features of
synthetic bundles and writing them in the shipped parquet layout.
"""
import hashlib
import json
import random
import zipfile

import pytest

pytest.importorskip("sklearn")
pytest.importorskip("pyarrow")

from arxiv_pipeline.common.utils import atomic_json
from arxiv_pipeline.matching import runner
from arxiv_pipeline.matching.contracts import (
    ContractError, LabelRecord, PartitionManifest, PartitionRow, publication_from_rows,
)

WORDS = ("graph neural network attention transformer diffusion sparse robust kernel "
         "bayesian quantum language vision federated causal contrastive policy").split()


def _title(rng):
    return " ".join(rng.sample(WORDS, 6)).title()


def _synthetic(seed=0, n_manual=6, n_auto=24):
    """Bundles whose matched entries share title/authors with their reference."""
    rng = random.Random(seed)
    bundles, labels, ref_id = {}, [], 0
    pubs = [f"2410.{3100 + i:05d}" for i in range(n_manual + n_auto)]
    for p, pub in enumerate(pubs):
        refs, bib = [], []
        for r in range(rng.randint(5, 9)):
            ref_id += 1
            refs.append(dict(arxiv_id=pub, ref_arxiv_id=f"2001.{ref_id:05d}", ref_title=_title(rng),
                             ref_authors=[f"Author {rng.randint(1, 60)}"],
                             ref_submission_date=f"20{rng.randint(10, 20)}-01-01", ref_s2_id=None))
        for q in range(rng.randint(4, 7)):
            matched = rng.random() < 0.6
            gold = rng.choice(refs) if matched else None
            bib.append(dict(arxiv_id=pub, citation_key=f"k{q}", entry_type="article",
                            title=gold["ref_title"] if gold else _title(rng),
                            authors=gold["ref_authors"] if gold else [f"Author {rng.randint(61, 99)}"],
                            year=int(gold["ref_submission_date"][:4]) if gold else 2015,
                            doi=None, journal=None, url=None))
            manual = p < n_manual
            labels.append(LabelRecord(
                pub, f"k{q}", gold["ref_arxiv_id"] if gold else None,
                "matched" if gold else "unmatched", "manual" if manual else "heuristic",
                confidence=None if manual else 0.9, annotator="annotator" if manual else None,
                heuristic_version=None if manual else "synthetic-1", review_status="accepted"))
        bundles[pub] = publication_from_rows(pub, bib, refs, readiness="ready")
    manual_ids, auto_ids = pubs[:n_manual], pubs[n_manual:]
    part = {}
    for pool in (manual_ids, auto_ids):
        for i, pid in enumerate(pool):
            part[pid] = "test" if i < 2 else "valid" if i < 4 else "train"
    manifest = PartitionManifest(42, 5, "pop-synthetic", "labels-synthetic", manual_ids, auto_ids,
                                 [PartitionRow(pid, part[pid], "manual" if pid in manual_ids else "auto")
                                  for pid in pubs])
    return bundles, labels, manifest


def _handoff(tmp_path, labels, manifest, *, extra=None):
    test = {r.arxiv_id for r in manifest.rows if r.partition == "test"}
    files = {
        "partition_manifest.json": json.dumps(manifest.to_dict()).encode(),
        "labels_train_valid.json": json.dumps([l.to_dict() for l in labels
                                               if l.arxiv_id not in test]).encode(),
        "split_report.json": b"{}",
    }
    meta = dict(split_id=manifest.split_id, run_id="synthetic", test_labels_excluded=True,
                files={k: hashlib.sha256(v).hexdigest() for k, v in files.items()})
    path = tmp_path / "split_handoff.zip"
    with zipfile.ZipFile(path, "w") as z:
        for k, v in {**files, **(extra or {})}.items():
            z.writestr(k, v)
        z.writestr("handoff_manifest.json", json.dumps(meta))
    return path


def write_feature_handoff(root, bundles, manifest, *, drop=(), extra_cols=None, coverage=True):
    """Shipped layout: features/features_{train,valid,test_unlabeled}.parquet + schema + coverage report."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    from arxiv_pipeline.matching.features import build_feature_batch, get_feature_schema
    schema = get_feature_schema()
    names = {"train": "features_train.parquet", "valid": "features_valid.parquet",
             "test": "features_test_unlabeled.parquet"}
    folder = root / "features"
    folder.mkdir(parents=True, exist_ok=True)
    for part, name in names.items():
        cols = {c: [] for c in (*runner.KEY_COLUMNS, *[f.name for f in schema.features])}
        for row in manifest.rows:
            bundle = bundles[row.arxiv_id]
            if row.partition != part or row.arxiv_id in drop or not bundle.references:
                continue
            for pair in build_feature_batch(bundle, schema).rows:
                for c, v in zip(runner.KEY_COLUMNS, pair.key):
                    cols[c].append(v)
                for f in schema.features:
                    cols[f.name].append(pair.values[f.name])
        cols.update(extra_cols or {})
        pq.write_table(pa.table(cols), folder / name)
    atomic_json(folder / "feature_schema.json", schema.to_dict())
    if coverage:
        atomic_json(root / "missing_bundles_report.json",
                    dict(split_id=manifest.split_id, status="COMPLETE", missing_publications=0))
    return root


@pytest.fixture()
def setup(tmp_path):
    bundles, labels, manifest = _synthetic()
    handoff = _handoff(tmp_path, labels, manifest)
    config = runner.RunConfig(handoff_zip=handoff, features_dir=write_feature_handoff(tmp_path / "shipped", bundles, manifest),
                           output_root=tmp_path / "runs", run_id="r1", kinds=("logreg", "hgb"),
                           train_eval_queries=50,
                           grids={"logreg": [{"C": 1.0}], "hgb": [{"max_iter": 30}]})
    return config, bundles, labels, manifest


def _run(config, **changes):
    return runner.Run(runner.RunConfig(**{**config.__dict__, **changes}))


def _prepared(config, **changes):
    run = _run(config, **changes)
    runner.stage_inputs(run)
    runner.stage_features(run)
    return run


def test_cli_end_to_end_with_freeze(setup, tmp_path):
    config, bundles, labels, manifest = setup
    out = tmp_path / "cli"
    assert runner.main(["--handoff", str(config.handoff_zip), "--features", str(config.features_dir),
                     "--output", str(out), "--run-id", "cli", "--kinds", "logreg,hgb", "--freeze"]) == 0
    root = out / "cli"
    selection = json.loads((root / "metrics/model_selection.json").read_text())
    assert [r["kind"] for r in selection] == ["baseline", "logreg", "hgb"]
    assert all(0 <= r["valid_mrr_at_5"] <= 1 for r in selection)
    analysis = json.loads((root / "analysis/error_analysis.json").read_text())
    assert analysis["ablation"][0]["dropped"] is None
    for rel in ("frozen.json", "scores/train/scores.parquet", "scores/valid/scores.parquet",
                "scores/test/scores.parquet", "run_manifest.json", "metrics/model_selection.md"):
        assert (root / rel).is_file(), rel
    assert json.loads((root / "run_manifest.json").read_text())["test_labels_read"] is False
    frozen = json.loads((root / "frozen.json").read_text())
    access = (root / "models" / frozen["model_id"] / "test_access.jsonl").read_text().splitlines()
    assert len(access) == 1  # test scored exactly once, after freezing


def test_freeze_happens_before_test_scoring(setup):
    config, bundles, labels, manifest = setup
    run = _prepared(config)
    runner.stage_train(run)
    from arxiv_pipeline.matching.model import score_test
    with pytest.raises(ContractError, match="not frozen"):
        score_test(run.path("models", run.done("train")["selected"]),
                   runner.load_partition_matrix(run, "test"), split_id=manifest.split_id)


def test_resume_skips_finished_stages_and_refuses_other_settings(setup):
    config, *_ = setup
    run = _prepared(config)
    first = runner.stage_train(run)
    assert runner.stage_train(_run(config)) == first  # same --run-id, e.g. later with --freeze
    with pytest.raises(ContractError, match="seed"):
        _run(config, seed=7)


def test_test_label_files_are_refused(setup, tmp_path):
    config, bundles, labels, manifest = setup
    for name in runner.FORBIDDEN_LABEL_FILES:
        with pytest.raises(ContractError, match="Test labels must never be read"):
            runner._read_json(tmp_path / "shared" / name)
    sneaky = _handoff(tmp_path, labels, manifest, extra={"labels_test_tv5_only.json": b"[]"})
    with pytest.raises(ContractError, match="Test labels must never be read"):
        runner.stage_inputs(_run(config, handoff_zip=sneaky, run_id="r2"))


def test_label_rows_for_test_publications_abort(setup, tmp_path):
    config, bundles, labels, manifest = setup
    test_pub = next(r.arxiv_id for r in manifest.rows if r.partition == "test")
    files = {"partition_manifest.json": json.dumps(manifest.to_dict()).encode(),
             "labels_train_valid.json": json.dumps([l.to_dict() for l in labels]).encode(),
             "split_report.json": b"{}"}
    path = tmp_path / "leaky.zip"
    with zipfile.ZipFile(path, "w") as z:
        for k, v in files.items():
            z.writestr(k, v)
        z.writestr("handoff_manifest.json", json.dumps(dict(
            split_id=manifest.split_id, test_labels_excluded=True,
            files={k: hashlib.sha256(v).hexdigest() for k, v in files.items()})))
    with pytest.raises(ContractError, match=test_pub):
        runner.stage_inputs(_run(config, handoff_zip=path, run_id="r3"))


def test_feature_import_checks(setup, tmp_path):
    config, bundles, labels, manifest = setup
    valid_pub = next(r.arxiv_id for r in manifest.rows if r.partition == "valid")
    train_pub = next(r.arxiv_id for r in manifest.rows if r.partition == "train")

    def features(folder, rid):
        return runner.stage_features(_prepared_inputs(config, folder, rid))

    def _prepared_inputs(config, folder, rid):
        run = _run(config, features_dir=folder, run_id=rid)
        runner.stage_inputs(run)
        return run

    no_report = write_feature_handoff(tmp_path / "a", bundles, manifest, drop={valid_pub}, coverage=False)
    with pytest.raises(ContractError, match=valid_pub):
        features(no_report, "a")
    missing_train = write_feature_handoff(tmp_path / "b", bundles, manifest, drop={train_pub}, coverage=False)
    assert features(missing_train, "b")["skipped_train_ids"] == [train_pub]
    labelled = write_feature_handoff(tmp_path / "c", bundles, manifest)
    import pyarrow.parquet as pq
    t = pq.read_table(labelled / "features/features_valid.parquet")
    pq.write_table(t.append_column("label", [[0] * t.num_rows]), labelled / "features/features_valid.parquet")
    with pytest.raises(ContractError, match="label columns"):
        features(labelled, "c")
    schema = json.loads((tmp_path / "shipped/features/feature_schema.json").read_text())
    schema["features"] = schema["features"][::-1]
    other = write_feature_handoff(tmp_path / "d", bundles, manifest)
    (other / "features/feature_schema.json").write_text(json.dumps(schema))
    with pytest.raises(ContractError, match="differs from code schema"):
        features(other, "d")


def test_zero_candidate_eval_paper_is_accepted_when_coverage_confirms(setup, tmp_path):
    """A valid paper with 0 references has no pair rows; its queries still count in MRR."""
    config, bundles, labels, manifest = setup
    valid_pub = next(r.arxiv_id for r in manifest.rows if r.partition == "valid")
    folder = write_feature_handoff(tmp_path / "z", bundles, manifest, drop={valid_pub})
    run = _run(config, features_dir=folder, run_id="z")
    runner.stage_inputs(run)
    assert runner.stage_features(run)["zero_candidate_ids"] == {"valid": [valid_pub]}
    valid = {r.arxiv_id for r in manifest.rows if r.partition == "valid"}
    expected = sum(l.arxiv_id in valid and l.is_supervised for l in labels)
    assert runner.stage_train(run)["valid_queries"] == expected  # 0-candidate queries stay in the denominator


def test_grouped_cv_by_hard_case_slice(setup):
    config, bundles, labels, manifest = setup
    run = _prepared(config)
    runner.stage_train(run)
    runner.stage_cv(run, folds=2, eval_queries=50)
    rows = json.loads(run.path("analysis", "cv_hard_cases.json").read_text())
    assert {r["model"] for r in rows} == {"baseline", "logreg", "hgb"}
    assert {r["fold"] for r in rows} == {0, 1}
    train_pubs = {r.arxiv_id for r in manifest.rows if r.partition == "train"}
    assert all(r["queries"] <= sum(l.arxiv_id in train_pubs for l in labels) for r in rows)
    assert all(0 <= r["mrr_at_5"] <= 1 for r in rows)
    assert "| all / all |" in run.path("analysis", "cv_hard_cases.md").read_text()


def test_query_slices_use_features_only():
    import numpy as np
    from arxiv_pipeline.matching.features import get_feature_schema
    from arxiv_pipeline.matching.model import PairMatrix
    schema = get_feature_schema()
    cols = [f.name for f in schema.features]
    X = np.zeros((2, len(cols)))
    X[1, cols.index("title_char_ngram_cosine")] = 0.4
    keys = [("2301.00001", "a", "2001.00001"), ("2301.00001", "b", "2001.00001")]
    s = runner.query_slices(PairMatrix(keys, X, cols, schema.schema_id))
    assert s[("2301.00001", "a")] == ["no_title_signal", "no_author_signal"]
    assert s[("2301.00001", "b")] == ["no_author_signal"]


def test_ablation_variants_on_train_folds(setup):
    config, bundles, labels, manifest = setup
    run = _prepared(config)
    runner.stage_train(run)
    receipt = runner.stage_ablation(run, folds=2, eval_queries=50)
    rows = json.loads(run.path("analysis", "ablation.json").read_text())
    ran = {(r["fold"], r["model"]) for r in rows}
    assert all((f, v) in ran or {"fold": f, "variant": v} in
               [{k: x[k] for k in ("fold", "variant")} for x in receipt["skipped"]]
               for f in (0, 1) for v in runner.ABLATIONS)  # every variant ran or is reported as skipped
    assert {r["model"] for r in rows} >= set(runner.ABLATIONS) - {"manual_only"}
    assert receipt["kind"] == runner.stage_train(run)["selected_kind"]  # same family as the selected model
    train_pubs = {r.arxiv_id for r in manifest.rows if r.partition == "train"}
    assert all(r["queries"] <= sum(l.arxiv_id in train_pubs for l in labels) for r in rows)
    assert not run.path("frozen.json").exists()  # analysis never freezes or scores test


def test_rank_metrics_match_official_evaluator(setup):
    from arxiv_pipeline.matching.model import evaluator, predict_scores
    from arxiv_pipeline.matching.train import labeled_queries
    config, bundles, labels, manifest = setup
    run = _prepared(config)
    runner.stage_train(run)
    from arxiv_pipeline.matching.model import load_artifact, predict_score_array
    art = load_artifact(run.path("models", run.done("train")["selected"]))
    valid, valid_t = runner._labeled_view(runner.load_partition_matrix(run, "valid"), labels,
                                          allow_sources=("manual", "heuristic"))
    queries = labeled_queries(labels, [r.arxiv_id for r in manifest.rows if r.partition == "valid"])
    sup = {l.key: l for l in labels if l.is_supervised}
    gold = {q: sup[q].groundtruth_arxiv_id for q in queries}
    _, scores = predict_score_array(art, valid)
    out = runner.query_outcomes(valid.keys, scores, runner._group_rows(valid.keys), gold)
    ours = runner.rank_metrics(out, set(queries), gold)
    official = evaluator()(predict_scores(art, valid), valid_t, queries=queries)
    assert abs(ours["mrr_at_5"] - official) < 1e-12


def test_insights_stage_outputs(setup):
    config, bundles, labels, manifest = setup
    run = _prepared(config)
    runner.stage_train(run)
    receipt = runner.stage_insights(run, folds=2)
    for name in ("no_answer_detection.json", "suspicious_labels.json", "learning_curve.json",
                 "model_coefficients_and_margins.json", "insights.md"):
        assert run.path("analysis", name).is_file(), name
    curve = json.loads(run.path("analysis", "learning_curve.json").read_text())
    assert [r["fraction"] for r in curve] == list(runner.LEARNING_FRACTIONS)
    det = json.loads(run.path("analysis", "no_answer_detection.json").read_text())
    assert 0 <= det["model"]["roc_auc"] <= 1 and "model_on_valid" in det
    assert receipt["suspicious"] >= 0 and not run.path("frozen.json").exists()
