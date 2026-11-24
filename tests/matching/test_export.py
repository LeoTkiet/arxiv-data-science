"""Tests for 6-table consistency checks, sharded Parquet read-back & HF upload/visibility."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from arxiv_pipeline.export import parquet as parquet_facade
from arxiv_pipeline.matching.contracts import (
    ContractError,
    PartitionManifest,
    PartitionRow,
)
from arxiv_pipeline.matching.export import (
    ARROW_SCHEMAS,
    DICTIONARY_COLUMNS,
    TABLE_NAMES,
    export_dataset,
    export_hf_dataset,
    prepare_published_references,
    upload_to_hf,
    validate_six_tables,
    verify_hf_repo_visibility,
    verify_parquet_export,
)


def _valid_six_tables():
    """Build a small, fully consistent 6-table fixture conforming to MS1/MS2/MS3 specs."""
    papers = [
        {
            "arxiv_id": "2310.00001",
            "title": "First Paper",
            "authors": ["Alice Author"],
            "submission_date": "2023-10-01T00:00:00Z",
            "revised_dates": ["2023-10-15T00:00:00Z"],
            "venue": "NeurIPS 2023",
        },
        {
            "arxiv_id": "2310.00002",
            "title": "Second Paper",
            "authors": ["Bob Author", "Carol Coauthor"],
            "submission_date": "2023-10-02T00:00:00Z",
            "revised_dates": [],
            "venue": None,
        },
        {
            "arxiv_id": "hep-th/9901001",
            "title": "Legacy Paper With No ArXiv Refs",
            "authors": ["Dan Physicist"],
            "submission_date": "1999-01-01T00:00:00Z",
            "revised_dates": [],
            "venue": None,
        },
    ]
    # Published references table keeps arXiv-keyed references only (ref_arxiv_id non-null)
    references = [
        {
            "arxiv_id": "2310.00001",
            "ref_arxiv_id": "2001.00001",
            "ref_title": "Ref One",
            "ref_authors": ["Eve"],
            "ref_submission_date": "2020-01-01",
            "ref_s2_id": "s2-1",
        },
        {
            "arxiv_id": "2310.00001",
            "ref_arxiv_id": "2001.00002",
            "ref_title": None,
            "ref_authors": [],
            "ref_submission_date": None,
            "ref_s2_id": "s2-2",
        },
        {
            "arxiv_id": "2310.00002",
            "ref_arxiv_id": "2002.00001",
            "ref_title": "Ref Two",
            "ref_authors": ["Grace"],
            "ref_submission_date": "2020-02-01",
            "ref_s2_id": "s2-3",
        },
    ]
    elements = [
        {"element_id": "2310-00001#root1", "arxiv_id": "2310.00001", "kind": "document", "content": "Root v1"},
        {"element_id": "2310-00001#h1", "arxiv_id": "2310.00001", "kind": "heading", "content": "Introduction"},
        {"element_id": "2310-00001#s1", "arxiv_id": "2310.00001", "kind": "sentence", "content": "First sentence."},
        {"element_id": "2310-00002#root1", "arxiv_id": "2310.00002", "kind": "document", "content": "Root v1"},
    ]
    hierarchy = [
        {"arxiv_id": "2310.00001", "version": 1, "element_id": "2310-00001#h1", "parent_id": "2310-00001#root1"},
        {"arxiv_id": "2310.00001", "version": 1, "element_id": "2310-00001#s1", "parent_id": "2310-00001#h1"},
    ]
    bibtex = [
        {
            "arxiv_id": "2310.00001",
            "citation_key": "KeyA",
            "entry_type": "article",
            "title": "Ref One",
            "authors": ["Eve"],
            "year": 2020,
            "doi": None,
            "journal": None,
            "url": None,
        },
        {
            "arxiv_id": "2310.00001",
            "citation_key": "KeyB",
            "entry_type": "book",
            "title": "External Textbook",
            "authors": ["Frank"],
            "year": 2018,
            "doi": None,
            "journal": None,
            "url": None,
        },
        {
            "arxiv_id": "2310.00002",
            "citation_key": "KeyC",
            "entry_type": "inproceedings",
            "title": "Ref Two",
            "authors": ["Grace"],
            "year": None,
            "doi": "10.1000/test",
            "journal": None,
            "url": None,
        },
        {
            "arxiv_id": "hep-th/9901001",
            "citation_key": "KeyEmpty",
            "entry_type": "misc",
            "title": "",
            "authors": [],
            "year": None,
            "doi": None,
            "journal": None,
            "url": None,
        },
    ]
    predictions = [
        {
            "arxiv_id": "2310.00001",
            "partition": "test",
            "citation_key": "KeyA",
            "groundtruth_arxiv_id": "2001.00001",
            "cand_1": "2001.00001",
            "cand_2": "2001.00002",
            "cand_3": None,
            "cand_4": None,
            "cand_5": None,
        },
        {
            "arxiv_id": "2310.00001",
            "partition": "test",
            "citation_key": "KeyB",
            "groundtruth_arxiv_id": None,
            "cand_1": "2001.00002",
            "cand_2": "2001.00001",
            "cand_3": None,
            "cand_4": None,
            "cand_5": None,
        },
        {
            "arxiv_id": "2310.00002",
            "partition": "valid",
            "citation_key": "KeyC",
            "groundtruth_arxiv_id": "2002.00001",
            "cand_1": "2002.00001",
            "cand_2": None,
            "cand_3": None,
            "cand_4": None,
            "cand_5": None,
        },
        {
            "arxiv_id": "hep-th/9901001",
            "partition": "train",
            "citation_key": "KeyEmpty",
            "groundtruth_arxiv_id": None,
            "cand_1": None,
            "cand_2": None,
            "cand_3": None,
            "cand_4": None,
            "cand_5": None,
        },
    ]
    manifest = PartitionManifest(
        seed=42,
        group_size=1,
        population_snapshot_id="pop-test",
        label_snapshot_id="lbl-test",
        manual_publication_ids=["2310.00001", "hep-th/9901001"],
        auto_publication_ids=["2310.00002"],
        rows=[
            PartitionRow("2310.00001", "test", "manual"),
            PartitionRow("2310.00002", "valid", "auto"),
            PartitionRow("hep-th/9901001", "train", "manual"),
        ],
    )
    return {
        "papers": papers,
        "references": references,
        "elements": elements,
        "hierarchy": hierarchy,
        "bibtex": bibtex,
        "predictions": predictions,
    }, manifest


def test_prepare_published_references_filters_non_arxiv_and_deduplicates():
    raw_refs = [
        {"arxiv_id": "2310.00001v2", "ref_arxiv_id": "2001.00001v1", "ref_title": "T", "ref_authors": ["A"], "ref_submission_date": "2020", "ref_s2_id": "s1"},
        {"arxiv_id": "2310.00001", "ref_arxiv_id": "2001.00001", "ref_title": "T", "ref_authors": ["A"], "ref_submission_date": "2020", "ref_s2_id": "s1"},
        {"arxiv_id": "2310.00001", "ref_arxiv_id": None, "ref_title": "Book", "ref_authors": ["B"], "ref_submission_date": "2019", "ref_s2_id": "s2"},
    ]
    published, stats = prepare_published_references(raw_refs)
    assert len(published) == 1
    assert published[0]["arxiv_id"] == "2310.00001"
    assert published[0]["ref_arxiv_id"] == "2001.00001"
    assert stats == {
        "input_rows": 3,
        "published_rows": 1,
        "excluded_non_arxiv": 1,
        "collapsed_duplicates": 1,
    }


def test_validate_six_tables_and_sharded_parquet_readback(tmp_path):
    tables, part_manifest = _valid_six_tables()
    report, _ = validate_six_tables(tables, partition_manifest=part_manifest)
    assert report.valid
    assert report.counts == {
        "papers": 3,
        "references": 3,
        "elements": 4,
        "hierarchy": 2,
        "bibtex": 4,
        "predictions": 4,
    }

    out_dir = tmp_path / "hf_export"
    manifest = export_hf_dataset(
        tables,
        out_dir,
        partition_manifest=part_manifest,
        max_rows_per_shard=2,
        synthetic=True,
        provenance={"split_id": part_manifest.split_id},
    )
    assert manifest.readback_verified is True
    assert manifest.synthetic is True
    assert manifest.ready_for_hf_upload is False
    assert manifest.total_rows == 20
    assert manifest.tables["references"]["shard_count"] == 2
    assert manifest.tables["predictions"]["shard_count"] == 2
    assert manifest.partition_counts == {
        "train": {"rows": 1, "publications": 1},
        "valid": {"rows": 1, "publications": 1},
        "test": {"rows": 2, "publications": 1},
    }

    # Verify all 6 tables read back with exact official Arrow schemas & dictionary columns
    total_reloaded = 0
    for tname in TABLE_NAMES:
        shards = manifest.tables[tname]["shards"]
        t_rows = 0
        for shard in shards:
            tbl = pq.read_table(out_dir / shard["path"])
            assert tbl.column_names == ARROW_SCHEMAS[tname].names
            for dict_col in DICTIONARY_COLUMNS.get(tname, set()):
                assert pa.types.is_dictionary(tbl.schema.field(dict_col).type)
            t_rows += tbl.num_rows
        assert t_rows == len(tables[tname])
        total_reloaded += t_rows
    assert total_reloaded == manifest.total_rows

    card_text = (out_dir / "README.md").read_text(encoding="utf-8")
    assert "path: data/papers/*.parquet" in card_text
    assert "path: data/predictions/*.parquet" in card_text
    assert "DO NOT PUBLISH" in card_text

    loaded_manifest = json.loads((out_dir / "export_manifest.json").read_text(encoding="utf-8"))
    assert loaded_manifest["total_rows"] == 20

    # Tampering with a shard is caught by checksum verification
    first_shard = out_dir / manifest.tables["papers"]["shards"][0]["path"]
    first_shard.write_bytes(first_shard.read_bytes() + b"corrupt")
    with pytest.raises(ContractError, match="Checksum mismatch"):
        verify_parquet_export(out_dir, manifest)


def test_export_dataset_alias_and_facade_adapter(tmp_path):
    tables, part_manifest = _valid_six_tables()
    preds = tables["predictions"]
    source_only = {k: v for k, v in tables.items() if k != "predictions"}
    manifest = export_dataset(
        source_only,
        preds,
        tmp_path / "alias_out",
        partition_manifest=part_manifest,
        synthetic=True,
    )
    assert manifest.readback_verified is True
    assert manifest.total_rows == 20

    stage_res = parquet_facade.run(
        {"arxiv_id": "dataset"},
        tmp_path / "adapter_out",
        upstream={"tables": tables, "partition_manifest": part_manifest, "synthetic": True},
    )
    assert stage_res.status == "success"
    assert stage_res.schema_version == "3.0"
    assert (tmp_path / "adapter_out" / stage_res.artifacts["export_manifest"]).is_file()


@pytest.mark.parametrize(
    "mutator,expected_code",
    [
        (lambda t: t["papers"].append(dict(t["papers"][0])), "papers_duplicate_pk"),
        (lambda t: t["references"][0].update({"arxiv_id": "2310.99999"}), "references_fk_paper"),
        (lambda t: t["references"][0].update({"ref_arxiv_id": None}), "references_null_ref_arxiv_id"),
        (lambda t: t["references"].append(dict(t["references"][0])), "references_duplicate_pair"),
        (lambda t: t["elements"][0].update({"kind": "prose_block"}), "elements_invalid_kind"),
        (lambda t: t["hierarchy"][0].update({"version": True}), "hierarchy_invalid_version"),
        (lambda t: t["hierarchy"][0].update({"parent_id": "2310-00002#root1"}), "hierarchy_parent_paper_mismatch"),
        (lambda t: t["hierarchy"][0].update({"parent_id": t["hierarchy"][0]["element_id"]}), "hierarchy_self_loop"),
        (lambda t: t["bibtex"][0].update({"year": True}), "bibtex_invalid_year"),
        (lambda t: t["predictions"][0].update({"citation_key": "UnknownKey"}), "predictions_fk_bibtex"),
        (lambda t: t["predictions"][0].update({"groundtruth_arxiv_id": "2002.00001"}), "predictions_fk_groundtruth"),
        (lambda t: t["predictions"][0].update({"cand_1": "2002.00001"}), "predictions_fk_candidate"),
        (lambda t: t["predictions"][0].update({"cand_2": None, "cand_3": "2001.00002"}), "predictions_non_trailing_null"),
        (lambda t: t["predictions"][1].update({"partition": "train"}), "predictions_partition_leakage"),
        (lambda t: t["predictions"].pop(1), "predictions_missing_bibtex_queries"),
    ],
)
def test_six_table_consistency_rejects_broken_fks_dtypes_and_leakage(mutator, expected_code):
    tables, part_manifest = _valid_six_tables()
    mutated = deepcopy(tables)
    mutator(mutated)
    report, _ = validate_six_tables(mutated, partition_manifest=part_manifest)
    assert not report.valid
    assert expected_code in {e.code for e in report.errors}


def test_export_rejects_deleted_split_publication(tmp_path):
    """P1 regression: dropping an entire split publication from all 6 tables must fail."""
    tables, part_manifest = _valid_six_tables()
    dropped_pid = "2310.00002"
    truncated = {
        tname: [row for row in rows if row.get("arxiv_id") != dropped_pid]
        for tname, rows in tables.items()
    }
    report, _ = validate_six_tables(truncated, partition_manifest=part_manifest)
    assert not report.valid
    codes = {e.code for e in report.errors}
    assert "papers_missing_split_publication" in codes
    assert "bibtex_missing_split_publication" in codes
    assert "predictions_missing_split_publication" in codes

    with pytest.raises(ContractError, match="6-table consistency check failed"):
        export_hf_dataset(
            truncated,
            tmp_path / "missing_pub_out",
            partition_manifest=part_manifest,
            synthetic=False,
        )

    # Even if papers keeps the row, removing its bibtex & predictions must also fail
    missing_bp = deepcopy(tables)
    missing_bp["bibtex"] = [r for r in missing_bp["bibtex"] if r["arxiv_id"] != dropped_pid]
    missing_bp["predictions"] = [r for r in missing_bp["predictions"] if r["arxiv_id"] != dropped_pid]
    report_bp, _ = validate_six_tables(missing_bp, partition_manifest=part_manifest)
    assert not report_bp.valid
    assert "bibtex_missing_split_publication" in {e.code for e in report_bp.errors}
    assert "predictions_missing_split_publication" in {e.code for e in report_bp.errors}


def test_hf_upload_blocks_synthetic_and_verifies_visibility_for_real(tmp_path):
    from fnmatch import fnmatch

    tables, part_manifest = _valid_six_tables()

    @dataclass
    class FakeDatasetInfo:
        private: bool = False
        gated: bool = False

    class FakeHfApi:
        def __init__(self, *, private=False, gated=False, omit_table=None, ignore_delete_patterns=False):
            self.private = private
            self.gated = gated
            self.omit_table = omit_table
            self.ignore_delete_patterns = ignore_delete_patterns
            self.created = []
            self.uploaded = []
            self.files: set[str] = set()

        def create_repo(self, *, repo_id, repo_type, private, exist_ok):
            self.created.append((repo_id, repo_type, private, exist_ok))

        def upload_folder(self, *, folder_path, repo_id, repo_type, commit_message, delete_patterns=None):
            self.uploaded.append((folder_path, repo_id, repo_type, delete_patterns))
            root = Path(folder_path)
            if delete_patterns and not self.ignore_delete_patterns:
                patterns = [delete_patterns] if isinstance(delete_patterns, str) else list(delete_patterns)
                self.files = {
                    f for f in self.files if not any(fnmatch(f, pat) for pat in patterns)
                }
            for p in root.rglob("*"):
                if not p.is_file():
                    continue
                rel = p.relative_to(root).as_posix()
                if self.omit_table is not None and f"data/{self.omit_table}/" in rel:
                    continue
                self.files.add(rel)

        def dataset_info(self, *, repo_id):
            return FakeDatasetInfo(private=self.private, gated=self.gated)

        def list_repo_files(self, *, repo_id, repo_type):
            return sorted(self.files)

    syn_dir = tmp_path / "syn"
    syn_manifest = export_hf_dataset(tables, syn_dir, partition_manifest=part_manifest, synthetic=True)
    fake_api = FakeHfApi()
    with pytest.raises(ContractError, match="Refusing to upload synthetic"):
        upload_to_hf(syn_dir, "group6/arxiv-matching", manifest=syn_manifest, api=fake_api)
    assert fake_api.created == [] and fake_api.uploaded == []

    # First upload: max_rows_per_shard=1 creates 20 shards across the 6 tables
    real_dir = tmp_path / "real"
    first_manifest = export_hf_dataset(
        tables,
        real_dir,
        partition_manifest=part_manifest,
        max_rows_per_shard=1,
        synthetic=False,
        upload=True,
        repo_id="group6/arxiv-matching",
        api=fake_api,
    )
    assert first_manifest.ready_for_hf_upload is True
    first_parquets = [f for f in fake_api.files if f.endswith(".parquet")]
    assert len(first_parquets) == 20

    # Second upload to SAME repo with max_rows_per_shard=100 -> must replace stale shards with exact 6 shards
    second_manifest = export_hf_dataset(
        tables,
        real_dir,
        partition_manifest=part_manifest,
        max_rows_per_shard=100,
        synthetic=False,
        upload=True,
        repo_id="group6/arxiv-matching",
        api=fake_api,
    )
    assert len(fake_api.uploaded) == 2
    assert fake_api.uploaded[-1][3] == [f"data/{t}/*.parquet" for t in TABLE_NAMES]
    second_parquets = [f for f in fake_api.files if f.endswith(".parquet")]
    assert len(second_parquets) == 6

    vis_public = verify_hf_repo_visibility(
        "group6/arxiv-matching", api=fake_api, expected_manifest=second_manifest
    )
    assert vis_public["publicly_accessible"] is True
    assert vis_public["all_six_tables_present"] is True
    assert vis_public["missing_tables"] == []
    assert vis_public["stale_parquet_files"] == []
    assert vis_public["exact_shard_match"] is True

    # If a remote still retains stale shards, upload_to_hf must raise ContractError
    stale_api = FakeHfApi(ignore_delete_patterns=True)
    stale_api.files = set(first_parquets) | {"README.md", "export_manifest.json"}
    with pytest.raises(ContractError, match="Remote HF Parquet files do not match"):
        upload_to_hf(real_dir, "group6/arxiv-matching", manifest=second_manifest, api=stale_api)

    private_api = FakeHfApi(private=True)
    private_api.files = set(fake_api.files)
    vis_priv = verify_hf_repo_visibility("group6/arxiv-matching", api=private_api)
    assert vis_priv["private"] is True
    assert vis_priv["publicly_accessible"] is False

    partial_api = FakeHfApi(omit_table="predictions")
    partial_api.upload_folder(
        folder_path=str(real_dir),
        repo_id="group6/arxiv-matching",
        repo_type="dataset",
        commit_message="x",
    )
    vis_missing = verify_hf_repo_visibility("group6/arxiv-matching", api=partial_api)
    assert vis_missing["all_six_tables_present"] is False
    assert vis_missing["missing_tables"] == ["predictions"]
