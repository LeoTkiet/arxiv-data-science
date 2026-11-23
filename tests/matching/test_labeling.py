"""Unit tests for Reference Matching manual labeling & QA (Milestone 3 · TV3)."""

from __future__ import annotations

import csv
import json

import pytest

from arxiv_pipeline.matching.contracts import (
    ContractError,
    LabelRecord,
    publication_from_rows,
)
from arxiv_pipeline.matching.labeling import (
    audit_label_manifest,
    export_annotation_sheet,
    load_annotation_sheet,
    load_labels_json,
    run,
    run_interactive_cli,
    save_labels_json,
    validate_label_manifest,
)
from tests.fixtures.matching.examples import labels as fixture_labels
from tests.fixtures.matching.examples import publications as fixture_publications


def _bundles():
    return [
        publication_from_rows(
            p["arxiv_id"], p["bibtex"], p["references"], readiness=p["readiness"]
        )
        for p in fixture_publications()
    ]


def test_export_and_load_annotation_sheet_roundtrip(tmp_path):
    bundles = _bundles()
    csv_path = tmp_path / "sheet.csv"
    count = export_annotation_sheet(bundles, csv_path)
    assert count == 4

    # Simulate annotator filling rows 1 and 2, leaving rows 3 and 4 unlabeled
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert "[0] 2001.00001" in rows[0]["candidates_preview"]
    rows[0]["decision"] = "matched"
    rows[0]["groundtruth_arxiv_id"] = "2001.00001v1"
    rows[0]["annotator"] = "tv3"
    rows[0]["reviewer"] = "tv5"
    rows[0]["review_status"] = "accepted"

    rows[1]["decision"] = "uncertain"
    rows[1]["annotator"] = "tv3"
    rows[1]["notes"] = "two identical titles"

    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    loaded = load_annotation_sheet(csv_path)
    assert len(loaded) == 2
    assert loaded[0].groundtruth_arxiv_id == "2001.00001"
    assert loaded[0].is_supervised is True
    assert loaded[1].decision == "uncertain"
    assert loaded[1].is_supervised is False

    report = validate_label_manifest(bundles, loaded)
    assert report.valid


def test_load_annotation_sheet_rejects_duplicate_rows(tmp_path):
    csv_path = tmp_path / "dup.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["arxiv_id", "citation_key", "decision", "groundtruth_arxiv_id", "annotator"],
        )
        writer.writeheader()
        writer.writerow({"arxiv_id": "2310.12345", "citation_key": "RefA", "decision": "unmatched", "groundtruth_arxiv_id": "", "annotator": "tv3"})
        writer.writerow({"arxiv_id": "2310.12345v2", "citation_key": "RefA", "decision": "unmatched", "groundtruth_arxiv_id": "", "annotator": "tv3"})

    with pytest.raises(ContractError, match="Duplicate annotation row"):
        load_annotation_sheet(csv_path)


def test_validate_label_manifest_checks_provenance_and_warnings():
    bundles = _bundles()
    bad_manual = LabelRecord(
        arxiv_id="2310.12345",
        citation_key="RefA",
        groundtruth_arxiv_id="2001.00001",
        decision="matched",
        label_source="manual",
        annotator=None,
        review_status="accepted",
    )
    report = validate_label_manifest(bundles, [bad_manual])
    assert not report.valid
    assert any(err.code == "missing_annotator" for err in report.errors)
    assert any(warn.code == "missing_reviewer" for warn in report.warnings)


def test_audit_label_manifest_and_pool_overlap():
    bundles = _bundles()
    labels = [LabelRecord(**item) for item in fixture_labels()]
    audit = audit_label_manifest(
        bundles,
        labels,
        min_manual_pubs=1,
        min_entries_per_pub=1,
        min_entries_per_annotator=1,
        min_pubs_per_annotator=1,
    )
    assert audit["valid"] is True
    assert audit["manual_publication_count"] == 2
    assert audit["heuristic_publication_count"] == 1
    assert audit["pool_overlap_publications"] == []
    assert audit["annotators"]["synthetic-tv1"]["meets_quota"] is True

    # Pool overlap between manual and heuristic on same publication must fail audit
    overlap = LabelRecord(
        arxiv_id="2310.12345",
        citation_key="RefA",
        groundtruth_arxiv_id="2001.00001",
        decision="matched",
        label_source="heuristic",
        confidence=0.99,
        heuristic_version="v1.0.0",
    )
    bad_audit = audit_label_manifest(bundles, [overlap, labels[0]])
    assert bad_audit["valid"] is False
    assert bad_audit["pool_overlap_publications"] == ["2310.12345"]


def test_run_interactive_cli_records_decisions_and_preserves_existing():
    bundles = _bundles()
    existing = [
        LabelRecord(
            arxiv_id="2310.12345",
            citation_key="RefA",
            groundtruth_arxiv_id="2001.00001",
            decision="matched",
            label_source="manual",
            annotator="tv1",
            review_status="accepted",
        )
    ]
    answers = iter(["?", "ambiguous title", "u", "no matching ref", "q"])
    output_lines: list[str] = []

    updated = run_interactive_cli(
        bundles,
        annotator="tv3",
        existing_labels=existing,
        input_fn=lambda _prompt: next(answers),
        output_fn=output_lines.append,
    )
    assert len(updated) == 3
    by_id = {lbl.arxiv_id: lbl for lbl in updated}
    # Existing label preserved untouched
    assert by_id["2310.12345"].annotator == "tv1"
    # New labels recorded for tv3
    assert by_id["2310.12346"].decision == "uncertain"
    assert by_id["2310.12346"].notes == "ambiguous title"
    assert by_id["2310.12347"].decision == "unmatched"
    assert by_id["2310.12347"].annotator == "tv3"


def test_save_and_load_labels_json_roundtrip(tmp_path):
    labels = [LabelRecord(**item) for item in fixture_labels()]
    path = tmp_path / "labels.json"
    save_labels_json(labels, path)
    reloaded = load_labels_json(path)
    assert reloaded == labels


def test_run_adapter_success_auto_and_errors(tmp_path):
    record = {"arxiv_id": "2310.12345"}

    # Missing upstream -> skipped with MATCHING_SCHEMA_VERSION (3.0)
    res_skip = run(record, tmp_path, upstream=None)
    assert res_skip.status == "skipped"
    assert res_skip.schema_version == "3.0"

    # Missing arxiv_id -> data_error with schema 3.0
    res_no_id = run({}, tmp_path, upstream=None)
    assert res_no_id.status == "data_error"
    assert res_no_id.schema_version == "3.0"

    # Valid upstream for the requested paper with auto_label_if_empty=True -> success
    target_bundle = [b for b in _bundles() if b.arxiv_id == "2310.12345"]
    res_ok = run(
        record,
        tmp_path,
        upstream={"bundles": target_bundle, "auto_label_if_empty": True},
    )
    assert res_ok.status == "success"
    assert res_ok.schema_version == "3.0"
    assert "labels" in res_ok.artifacts
    assert "label_audit" in res_ok.artifacts

    labels_file = tmp_path / res_ok.artifacts["labels"]
    audit_file = tmp_path / res_ok.artifacts["label_audit"]
    assert labels_file.exists()
    assert audit_file.exists()
    audit_data = json.loads(audit_file.read_text(encoding="utf-8"))
    assert audit_data["valid"] is True

    # Invalid manual label (target outside publication) -> data_error
    bad_label = LabelRecord(
        arxiv_id="2310.12345",
        citation_key="RefA",
        groundtruth_arxiv_id="2001.00002",
        decision="matched",
        label_source="manual",
        annotator="tv3",
    )
    res_err = run(record, tmp_path, upstream={"bundles": target_bundle, "labels": [bad_label]})
    assert res_err.status == "data_error"
    assert res_err.schema_version == "3.0"
    assert res_err.error is not None
    assert res_err.error.kind == "label_validation_failed"


def test_run_adapter_rejects_mismatched_paper_identity(tmp_path):
    bundles = _bundles()
    wrong_bundle = [b for b in bundles if b.arxiv_id == "2310.12346"]
    out_dir = tmp_path / "mismatch_out"

    # Passing bundle 2310.12346 when requesting 2310.12345 must fail without writing artifacts
    res = run({"arxiv_id": "2310.12345"}, out_dir, upstream={"bundles": wrong_bundle})
    assert res.status == "data_error"
    assert res.schema_version == "3.0"
    assert res.error is not None
    assert res.error.kind == "identity_mismatch"
    assert not (out_dir / "matching/2310.12345/labels.json").exists()

    # Empty bundles list -> identity_mismatch
    res_empty = run({"arxiv_id": "2310.12345"}, out_dir, upstream={"bundles": []})
    assert res_empty.status == "data_error"
    assert res_empty.error is not None
    assert res_empty.error.kind == "identity_mismatch"

    # Correct bundle but label belongs to another paper -> identity_mismatch
    target_bundle = [b for b in bundles if b.arxiv_id == "2310.12345"]
    wrong_label = LabelRecord(
        arxiv_id="2310.12346",
        citation_key="RefA",
        groundtruth_arxiv_id=None,
        decision="unmatched",
        label_source="manual",
        annotator="tv3",
    )
    res_lbl = run(
        {"arxiv_id": "2310.12345"},
        out_dir,
        upstream={"bundles": target_bundle, "labels": [wrong_label]},
    )
    assert res_lbl.status == "data_error"
    assert res_lbl.error is not None
    assert res_lbl.error.kind == "identity_mismatch"


def test_audit_label_manifest_requires_supervised_labels_for_quota():
    # Build 25 publications x 20 entries = 500 manual entries, all uncertain or pending
    bundles = []
    pending_or_uncertain_labels = []
    accepted_labels = []

    for pub_idx in range(1, 26):
        pid = f"2401.{pub_idx:05d}"
        annotator = f"tv{((pub_idx - 1) % 5) + 1}"
        bibs = [
            dict(
                arxiv_id=pid,
                citation_key=f"Ref{j}",
                entry_type="article",
                title=f"Title {j}",
                authors=["Author A"],
                year=2023,
                doi=None,
                journal=None,
                url=None,
            )
            for j in range(20)
        ]
        bundles.append(publication_from_rows(pid, bibs, [], readiness="ready"))
        for j in range(20):
            # Half uncertain (even if accepted), half pending -> 0 supervised
            if j % 2 == 0:
                pending_or_uncertain_labels.append(
                    LabelRecord(
                        arxiv_id=pid,
                        citation_key=f"Ref{j}",
                        groundtruth_arxiv_id=None,
                        decision="uncertain",
                        label_source="manual",
                        annotator=annotator,
                        review_status="accepted",
                        notes="ambiguous",
                    )
                )
            else:
                pending_or_uncertain_labels.append(
                    LabelRecord(
                        arxiv_id=pid,
                        citation_key=f"Ref{j}",
                        groundtruth_arxiv_id=None,
                        decision="unmatched",
                        label_source="manual",
                        annotator=annotator,
                        review_status="pending",
                    )
                )
            accepted_labels.append(
                LabelRecord(
                    arxiv_id=pid,
                    citation_key=f"Ref{j}",
                    groundtruth_arxiv_id=None,
                    decision="unmatched",
                    label_source="manual",
                    annotator=annotator,
                    reviewer="tv5",
                    review_status="accepted",
                )
            )

    audit_zero = audit_label_manifest(bundles, pending_or_uncertain_labels)
    assert audit_zero["manual_entered_entries"] == 500
    assert audit_zero["manual_supervised_entries"] == 0
    assert audit_zero["qualifying_manual_publications"] == 0
    assert audit_zero["manual_quota_met"] is False
    for ann_stats in audit_zero["annotators"].values():
        assert ann_stats["entries"] == 100
        assert ann_stats["supervised_entries"] == 0
        assert ann_stats["meets_quota"] is False

    audit_pass = audit_label_manifest(bundles, accepted_labels)
    assert audit_pass["manual_supervised_entries"] == 500
    assert audit_pass["qualifying_manual_publications"] == 25
    assert audit_pass["manual_quota_met"] is True
    for ann_stats in audit_pass["annotators"].values():
        assert ann_stats["supervised_entries"] == 100
        assert ann_stats["meets_quota"] is True
