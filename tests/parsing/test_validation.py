"""Unit tests for TV5 Milestone 2 QA Validator (offline, synthetic data only)."""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from arxiv_pipeline.common.schemas import PARSING_SCHEMA_VERSION, VersionRecord
from arxiv_pipeline.common.utils import atomic_json, paper_key
from arxiv_pipeline.reporting.milestone2.validation import (
    run,
    safe_path,
)

PAPER = "2310.12345"
KEY = paper_key(PAPER)
PREFIX = "2310-12345#"


def make_record(versions=(1, 2)):
    return {
        "arxiv_id": PAPER,
        "input_id": PAPER,
        "versions": [
            {
                "version": v,
                "versioned_id": f"{PAPER}v{v}",
                "source_path": f"corpus/{PAPER}/v{v}",
            }
            for v in versions
        ],
    }


def make_valid_dataset(tmp_path: Path):
    """Create a fully compliant multi-version dataset on disk."""
    tables_dir = tmp_path / f"tables/{KEY}"
    tables_dir.mkdir(parents=True, exist_ok=True)

    elements = [
        {"element_id": f"{PREFIX}root1", "arxiv_id": PAPER, "kind": "document", "content": "Root V1"},
        {"element_id": f"{PREFIX}root2", "arxiv_id": PAPER, "kind": "document", "content": "Root V2"},
        {"element_id": f"{PREFIX}h1", "arxiv_id": PAPER, "kind": "heading", "content": "Introduction"},
        {"element_id": f"{PREFIX}s1", "arxiv_id": PAPER, "kind": "sentence", "content": "We cite \\cite{vaswani2017}."},
        {"element_id": f"{PREFIX}eq1", "arxiv_id": PAPER, "kind": "block_formula", "content": "E=mc^2"},
    ]

    hierarchy = [
        {"arxiv_id": PAPER, "version": 1, "element_id": f"{PREFIX}h1", "parent_id": f"{PREFIX}root1"},
        {"arxiv_id": PAPER, "version": 1, "element_id": f"{PREFIX}s1", "parent_id": f"{PREFIX}h1"},
        {"arxiv_id": PAPER, "version": 1, "element_id": f"{PREFIX}eq1", "parent_id": f"{PREFIX}h1"},
        {"arxiv_id": PAPER, "version": 2, "element_id": f"{PREFIX}h1", "parent_id": f"{PREFIX}root2"},
        {"arxiv_id": PAPER, "version": 2, "element_id": f"{PREFIX}s1", "parent_id": f"{PREFIX}h1"},
    ]

    bibtex = [
        {
            "arxiv_id": PAPER,
            "citation_key": "vaswani2017",
            "entry_type": "article",
            "title": "Attention is all you need",
            "authors": ["A. Vaswani"],
            "year": 2017,
            "doi": "10.5555/3295222.3295349",
        }
    ]

    references_bib = "@article{vaswani2017,\n  title={Attention is all you need},\n  year={2017}\n}\n"

    # Write occurrences
    for v in (1, 2):
        v_dir = tmp_path / f"parsed/{KEY}/v{v}"
        v_dir.mkdir(parents=True, exist_ok=True)
        occs = [
            {"occurrence_id": f"o_root_{v}", "parent_occurrence_id": None, "position": 0, "element_id": f"{PREFIX}root{v}"},
            {"occurrence_id": f"o_h1_{v}", "parent_occurrence_id": f"o_root_{v}", "position": 0, "element_id": f"{PREFIX}h1"},
            {"occurrence_id": f"o_s1_{v}", "parent_occurrence_id": f"o_h1_{v}", "position": 0, "element_id": f"{PREFIX}s1"},
        ]
        if v == 1:
            occs.append({"occurrence_id": "o_eq1_1", "parent_occurrence_id": "o_h1_1", "position": 1, "element_id": f"{PREFIX}eq1"})
        atomic_json(v_dir / "occurrences.json", {"schema_version": PARSING_SCHEMA_VERSION, "version": v, "occurrences": occs})

    manifest = {
        "schema_version": PARSING_SCHEMA_VERSION,
        "arxiv_id": PAPER,
        "complete": True,
        "expected_versions": [1, 2],
        "versions": [
            {
                "version": 1,
                "status": "success",
                "artifacts": {"occurrences": f"parsed/{KEY}/v1/occurrences.json"},
            },
            {
                "version": 2,
                "status": "success",
                "artifacts": {"occurrences": f"parsed/{KEY}/v2/occurrences.json"},
            },
        ],
        "tables": {
            "elements": f"tables/{KEY}/elements.json",
            "hierarchy": f"tables/{KEY}/hierarchy.json",
            "bibtex": f"tables/{KEY}/bibtex.json",
            "bibtex_file": f"tables/{KEY}/references.bib",
        },
        "counts": {
            "elements_before": 6,
            "elements_after": len(elements),
            "bibtex_before": 2,
            "bibtex_after": len(bibtex),
        },
        "warnings": [],
    }

    atomic_json(tables_dir / "elements.json", elements)
    atomic_json(tables_dir / "hierarchy.json", hierarchy)
    atomic_json(tables_dir / "bibtex.json", bibtex)
    (tables_dir / "references.bib").write_text(references_bib, encoding="utf-8")
    atomic_json(tables_dir / "parsing_manifest.json", manifest)

    upstream = {
        "context": {"input_snapshot_id": "snap-123"},
        "stages": {
            "deduplicate": {
                "status": "success",
                "artifacts": {
                    "elements": f"tables/{KEY}/elements.json",
                    "hierarchy": f"tables/{KEY}/hierarchy.json",
                    "bibtex": f"tables/{KEY}/bibtex.json",
                    "bibtex_file": f"tables/{KEY}/references.bib",
                    "parsing_manifest": f"tables/{KEY}/parsing_manifest.json",
                },
            }
        },
    }
    return upstream


def test_valid_dataset_passes_validation(tmp_path):
    upstream = make_valid_dataset(tmp_path)
    res = run(make_record(), tmp_path, upstream=upstream)

    assert res.status == "success"
    assert res.schema_version == PARSING_SCHEMA_VERSION
    assert res.paper_id == PAPER
    assert res.stage == "validate_parse"

    report_path = tmp_path / res.artifacts["validation"]
    assert report_path.exists()
    doc = json.loads(report_path.read_text(encoding="utf-8"))
    assert doc["valid"] is True
    assert doc["arxiv_id"] == PAPER
    assert doc["schema_version"] == PARSING_SCHEMA_VERSION
    assert all(doc["checks"].values())
    assert doc["errors"] == []


def test_no_source_dataset_passes_with_no_source_status(tmp_path):
    tables_dir = tmp_path / f"tables/{KEY}"
    tables_dir.mkdir(parents=True, exist_ok=True)

    atomic_json(tables_dir / "elements.json", [])
    atomic_json(tables_dir / "hierarchy.json", [])
    atomic_json(tables_dir / "bibtex.json", [])
    (tables_dir / "references.bib").write_text("", encoding="utf-8")

    v_dir = tmp_path / f"parsed/{KEY}/v1"
    v_dir.mkdir(parents=True, exist_ok=True)
    atomic_json(v_dir / "occurrences.json", {"schema_version": PARSING_SCHEMA_VERSION, "version": 1, "occurrences": []})

    manifest = {
        "schema_version": PARSING_SCHEMA_VERSION,
        "arxiv_id": PAPER,
        "complete": True,
        "expected_versions": [1],
        "versions": [{"version": 1, "status": "no_source", "artifacts": {"occurrences": f"parsed/{KEY}/v1/occurrences.json"}}],
        "tables": {
            "elements": f"tables/{KEY}/elements.json",
            "hierarchy": f"tables/{KEY}/hierarchy.json",
            "bibtex": f"tables/{KEY}/bibtex.json",
            "bibtex_file": f"tables/{KEY}/references.bib",
        },
        "counts": {"elements_before": 0, "elements_after": 0, "bibtex_before": 0, "bibtex_after": 0},
        "warnings": [],
    }
    atomic_json(tables_dir / "parsing_manifest.json", manifest)

    upstream = {
        "context": {"input_snapshot_id": "snap-empty"},
        "stages": {
            "deduplicate": {
                "status": "no_source",
                "artifacts": {
                    "elements": f"tables/{KEY}/elements.json",
                    "hierarchy": f"tables/{KEY}/hierarchy.json",
                    "bibtex": f"tables/{KEY}/bibtex.json",
                    "bibtex_file": f"tables/{KEY}/references.bib",
                    "parsing_manifest": f"tables/{KEY}/parsing_manifest.json",
                },
            }
        },
    }

    res = run(make_record(versions=(1,)), tmp_path, upstream=upstream)
    assert res.status == "no_source"
    report = json.loads((tmp_path / res.artifacts["validation"]).read_text(encoding="utf-8"))
    assert report["valid"] is True
    assert report["status"] == "no_source"


def test_invalid_kind_fails_schema_integrity(tmp_path):
    upstream = make_valid_dataset(tmp_path)
    elems_file = tmp_path / f"tables/{KEY}/elements.json"
    data = json.loads(elems_file.read_text(encoding="utf-8"))
    data.append({"element_id": f"{PREFIX}x1", "arxiv_id": PAPER, "kind": "unsupported_kind", "content": "bad"})
    atomic_json(elems_file, data)

    res = run(make_record(), tmp_path, upstream=upstream)
    assert res.status == "data_error"
    report = json.loads((tmp_path / res.artifacts["validation"]).read_text(encoding="utf-8"))
    assert report["valid"] is False
    assert any(e["code"] == "invalid_element_kind" for e in report["errors"])


def test_forbidden_references_heading_fails_business_rule(tmp_path):
    upstream = make_valid_dataset(tmp_path)
    elems_file = tmp_path / f"tables/{KEY}/elements.json"
    data = json.loads(elems_file.read_text(encoding="utf-8"))
    data.append({"element_id": f"{PREFIX}href", "arxiv_id": PAPER, "kind": "heading", "content": "  References  "})
    atomic_json(elems_file, data)

    res = run(make_record(), tmp_path, upstream=upstream)
    assert res.status == "data_error"
    report = json.loads((tmp_path / res.artifacts["validation"]).read_text(encoding="utf-8"))
    assert report["valid"] is False
    assert any(e["code"] == "forbidden_heading" for e in report["errors"])


def test_duplicate_kind_content_fails_business_rule(tmp_path):
    upstream = make_valid_dataset(tmp_path)
    elems_file = tmp_path / f"tables/{KEY}/elements.json"
    data = json.loads(elems_file.read_text(encoding="utf-8"))
    # Add another sentence with exact same content but different ID
    data.append({"element_id": f"{PREFIX}s2_dup", "arxiv_id": PAPER, "kind": "sentence", "content": data[3]["content"]})
    atomic_json(elems_file, data)

    res = run(make_record(), tmp_path, upstream=upstream)
    assert res.status == "data_error"
    report = json.loads((tmp_path / res.artifacts["validation"]).read_text(encoding="utf-8"))
    assert report["valid"] is False
    assert any(e["code"] == "duplicate_kind_content" for e in report["errors"])


def test_foreign_key_missing_fails_referential_integrity(tmp_path):
    upstream = make_valid_dataset(tmp_path)
    hier_file = tmp_path / f"tables/{KEY}/hierarchy.json"
    data = json.loads(hier_file.read_text(encoding="utf-8"))
    data.append({"arxiv_id": PAPER, "version": 1, "element_id": f"{PREFIX}ghost_node", "parent_id": f"{PREFIX}root1"})
    atomic_json(hier_file, data)

    res = run(make_record(), tmp_path, upstream=upstream)
    assert res.status == "data_error"
    report = json.loads((tmp_path / res.artifacts["validation"]).read_text(encoding="utf-8"))
    assert report["valid"] is False
    assert any(e["code"] == "foreign_key_missing_child" for e in report["errors"])


def test_self_loop_and_cycles_fail_tree_validity(tmp_path):
    upstream = make_valid_dataset(tmp_path)
    hier_file = tmp_path / f"tables/{KEY}/hierarchy.json"
    data = json.loads(hier_file.read_text(encoding="utf-8"))
    # Introduce cycle: eq1 -> s1 -> eq1
    data.append({"arxiv_id": PAPER, "version": 1, "element_id": f"{PREFIX}h1", "parent_id": f"{PREFIX}s1"})
    atomic_json(hier_file, data)

    res = run(make_record(), tmp_path, upstream=upstream)
    assert res.status == "data_error"
    report = json.loads((tmp_path / res.artifacts["validation"]).read_text(encoding="utf-8"))
    assert report["valid"] is False
    assert any(e["category"] == "tree" for e in report["errors"])


def test_hierarchy_extra_columns_fail_schema(tmp_path):
    upstream = make_valid_dataset(tmp_path)
    hier_file = tmp_path / f"tables/{KEY}/hierarchy.json"
    data = json.loads(hier_file.read_text(encoding="utf-8"))
    data[0]["extra_position"] = 0
    atomic_json(hier_file, data)

    res = run(make_record(), tmp_path, upstream=upstream)
    assert res.status == "data_error"
    report = json.loads((tmp_path / res.artifacts["validation"]).read_text(encoding="utf-8"))
    assert report["valid"] is False
    assert any(e["code"] == "invalid_hierarchy_columns" for e in report["errors"])


def test_safe_path_security_checks(tmp_path):
    with pytest.raises(ValueError, match="Absolute or parent"):
        safe_path(tmp_path, "../outside.json")
    with pytest.raises(ValueError, match="Absolute or parent"):
        safe_path(tmp_path, "C:/Windows/system.ini")

