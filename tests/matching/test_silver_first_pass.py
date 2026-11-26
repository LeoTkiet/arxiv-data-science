"""First-pass silver labels preserve abstentions and source provenance."""
import hashlib
import json

import pytest

from arxiv_pipeline.common.utils import atomic_json
from arxiv_pipeline.matching.contracts import ContractError, publication_from_rows
from arxiv_pipeline.matching.silver_first_pass import run_first_pass
from tests.fixtures.matching.examples import publications


def _inputs(root):
    prepared_root = root / "prepared"
    records = []
    for item in publications()[:2]:
        paper = item["arxiv_id"]
        bibtex = [dict(row, arxiv_id=paper) for row in item["bibtex"]]
        bundle = publication_from_rows(paper, bibtex, item["references"], readiness="ready")
        relative = f"bundles/tv1/{paper}.json"
        path = prepared_root / relative
        atomic_json(path, bundle.to_dict())
        records.append(dict(arxiv_id=paper, member="tv1", status="ready",
                            bundle=relative, sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
    prepared = prepared_root / "prepared_manifest.json"
    atomic_json(prepared, dict(task="T3-05", total=2, ready=2, not_ready=0,
                               publications=records))
    population = root / "population_manifest.json"
    atomic_json(population, dict(publication_ids=[r["arxiv_id"] for r in records] + ["2310.12347"],
                                 excluded_manual_ids=["2310.12347"], population_count=3,
                                 population_snapshot_id="synthetic-test"))
    return prepared, population, records


def test_first_pass_reports_full_coverage_and_uncertain_without_handoff(tmp_path):
    prepared, population, records = _inputs(tmp_path)
    output = tmp_path / "first_pass"
    summary = run_first_pass(prepared, population, output, progress_every=1)
    assert summary["prepared_ready"] == 2
    assert summary["total_labels"] == 2
    assert summary["candidate_full_coverage_publications"] == 1
    assert summary["full_coverage_with_references"] == 1
    assert summary["full_coverage_without_references"] == 0
    assert summary["full_coverage_with_match"] == 1
    assert summary["uncertain_entries"] == 1
    assert summary["ready_for_handoff"] is False
    labels = json.loads((output / "draft_labels.json").read_text())
    assert {row["review_status"] for row in labels} == {"accepted", "pending"}
    uncertain = json.loads((output / "uncertain_rows.json").read_text())
    assert uncertain[0]["arxiv_id"] == records[1]["arxiv_id"]
    details = json.loads((output / "uncertain_details.json").read_text())
    assert len(details) == 1
    assert details[0]["citation_key"] == uncertain[0]["citation_key"]
    assert details[0]["candidates"]
    sample = json.loads((output / "spot_check_sample.json").read_text())
    assert len(sample) == summary["spot_check_sample_count"] == 1
    assert sample[0]["decision"] == "matched"
    assert run_first_pass(prepared, population, output)["total_labels"] == 2


def test_first_pass_rejects_changed_prepared_bundle(tmp_path):
    prepared, population, records = _inputs(tmp_path)
    path = prepared.parent / records[0]["bundle"]
    path.write_text(path.read_text() + " ")
    with pytest.raises(ContractError, match="hash mismatch"):
        run_first_pass(prepared, population, tmp_path / "output")
