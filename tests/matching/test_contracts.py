"""Boundary tests: broken keys, silent label changes and leakage must fail."""

from dataclasses import replace
import json

import pytest

from arxiv_pipeline.common.schemas import BibtexRow, StageResult
from arxiv_pipeline.matching import contracts

# Fixtures are source files rather than committed corpus/JSON data.
from fixtures.matching.examples import labels, partition_rows, publications


@pytest.fixture
def c():
    return contracts


def bundle(c, index=0):
    raw = publications()[index]
    return c.publication_from_rows(raw["arxiv_id"], raw["bibtex"], raw["references"],
                                   readiness=raw["readiness"])


def manifest(c, rows=None):
    rows = partition_rows() if rows is None else rows
    return c.PartitionManifest(seed=42, group_size=5, population_snapshot_id="synthetic-pop",
        label_snapshot_id="synthetic-labels",
        manual_publication_ids=[r["arxiv_id"] for r in rows if r["pool"] == "manual"],
        auto_publication_ids=[r["arxiv_id"] for r in rows if r["pool"] == "auto"],
        rows=[c.PartitionRow(**r) for r in rows])


def test_old_schemas_and_stage_envelope_are_compatible(c):
    from arxiv_pipeline.common.schemas import MATCHING_SCHEMA_VERSION
    assert c.BibtexInput is BibtexRow
    assert MATCHING_SCHEMA_VERSION == "3.0"
    old = StageResult("extract", "2310.12345", 1, "success", "synthetic")
    new = StageResult("match_input", "2310.12345", None, "success", "synthetic",
                      schema_version=MATCHING_SCHEMA_VERSION)
    assert old.to_dict()["schema_version"] == "1.0"
    assert new.to_dict()["schema_version"] == "3.0"
    assert bundle(c).bibtex[0].citation_key == "RefA"
    assert [row.ref_arxiv_id for row in bundle(c).references] == ["2001.00001"]


def test_fixtures_round_trip_and_do_not_mutate_upstream(c):
    raw = publications()
    before = json.dumps(raw, ensure_ascii=False, sort_keys=True)
    for item in raw:
        result = c.publication_from_rows(item["arxiv_id"], item["bibtex"], item["references"],
                                         readiness="ready")
        assert json.loads(json.dumps(result.to_dict(), allow_nan=False)) == result.to_dict()
    assert json.dumps(raw, ensure_ascii=False, sort_keys=True) == before
    unmatched = bundle(c, 2)
    assert len(unmatched.references) == 1
    assert unmatched.references[0].ref_title == ""
    assert {i.code for i in unmatched.issues} == {"missing_reference_title", "non_arxiv_reference"}
    empty = bundle(c, 3)
    assert len(empty.bibtex) == 1 and empty.references == []


@pytest.mark.parametrize("change", [
    {"year": True}, {"authors": "Nguyễn An"}, {"title": None},
    {"citation_key": " "}, {"year": 1.5}, {"doi": 123}, {"year": 2**63},
])
def test_malformed_m2_rows_are_rejected(c, change):
    raw = publications()[0]
    raw["bibtex"][0].update(change)
    with pytest.raises(c.ContractError):
        c.publication_from_rows(raw["arxiv_id"], raw["bibtex"], raw["references"], readiness="ready")


def test_reference_duplicates_and_cross_publication_are_not_silently_accepted(c):
    raw = publications()[0]
    duplicate = dict(raw["references"][0])
    result = c.publication_from_rows(raw["arxiv_id"], raw["bibtex"],
                                     raw["references"] + [duplicate], readiness="ready")
    assert len(result.references) == 1
    assert result.issues[0].code == "duplicate_reference"
    duplicate["ref_title"] = "Conflicting metadata"
    with pytest.raises(c.ContractError, match="conflict"):
        c.publication_from_rows(raw["arxiv_id"], raw["bibtex"],
                                raw["references"] + [duplicate], readiness="ready")
    with pytest.raises(c.ContractError, match="publication"):
        c.publication_from_rows("2310.99999", raw["bibtex"], raw["references"], readiness="ready")
    with pytest.raises(c.ContractError, match="duplicate"):
        c.publication_from_rows(raw["arxiv_id"], raw["bibtex"] * 2, [], readiness="ready")


def test_unmatched_uncertain_and_unlabeled_remain_distinct(c):
    records = [c.LabelRecord(**r) for r in labels()]
    assert [r.is_supervised for r in records] == [True, False, True]
    report = c.validate_labels([bundle(c, i) for i in range(4)], records)
    assert report.valid
    assert report.counts == {"queries": 4, "labeled": 3, "supervised": 2, "unlabeled": 1}
    bad = replace(records[0], groundtruth_arxiv_id="2001.99999")
    report = c.validate_labels([bundle(c)], [bad])
    assert not report.valid
    assert report.query_keys == [("2310.12345", "RefA")]
    assert report.errors[0].code == "groundtruth_outside_candidates"
    assert not c.validate_labels([bundle(c)], [records[0], records[0]]).valid


@pytest.mark.parametrize("change", [
    {"decision": "unmatched"}, {"decision": "uncertain"},
    {"review_status": "oops"}, {"label_source": "auto"},
    {"confidence": float("nan")}, {"confidence": True}, {"confidence": 1.01},
    {"label_source": "heuristic", "confidence": None},
    {"label_source": "heuristic", "confidence": .9, "heuristic_version": None},
])
def test_invalid_labels_fail_at_boundary(c, change):
    raw = labels()[0]
    raw.update(change)
    with pytest.raises(c.ContractError):
        c.LabelRecord(**raw)


def test_pair_and_score_keys_are_canonical_and_scores_are_finite(c):
    score = c.ScoreRecord("2310.12345v2", "RefA", "2001.00001v1", .7)
    assert score.key == ("2310.12345", "RefA", "2001.00001")
    for value in (float("inf"), float("nan"), True, "0.2"):
        with pytest.raises(c.ContractError):
            c.ScoreRecord("2310.12345", "RefA", "2001.00001", value)


def test_feature_batch_enforces_registered_columns_missingness_and_no_leakage(c):
    schema = c.FeatureSchema([c.FeatureSpec("title_similarity", "float64", False, 0., 1.,
        "error", "Title agreement", ["title", "ref_title"])])
    row = c.PairFeature("2310.12345", "RefA", "2001.00001", {"title_similarity": .8})
    batch = c.FeatureBatch([row], schema.schema_id)
    assert c.validate_features(batch, schema).valid
    assert row.to_dict() == dict(arxiv_id="2310.12345", citation_key="RefA",
                                ref_arxiv_id="2001.00001", title_similarity=.8)
    for values in ({"title_similarity": 1.5}, {"title_similarity": None},
                   {"title_similarity": True}, {"wrong_column": .2}):
        candidate = c.FeatureBatch([replace(row, values=values)], schema.schema_id)
        assert not c.validate_features(candidate, schema).valid
    assert not c.validate_features(replace(batch, feature_schema_id="wrong"), schema).valid
    for name in ("groundtruth_arxiv_id", "confidence", "arxiv_id", "label_source", "y"):
        with pytest.raises(c.ContractError):
            c.FeatureSpec(name, "float64", False, 0., 1., "error", "Leaked metadata", [name])


def test_prediction_schema_and_rank_holes_duplicates(c):
    row = c.RankedPrediction("2310.12345", "test", "RefA", "2001.00001", "2001.00001")
    assert list(row.to_dict()) == ["arxiv_id", "partition", "citation_key",
        "groundtruth_arxiv_id", "cand_1", "cand_2", "cand_3", "cand_4", "cand_5"]
    assert row.to_dict()["cand_5"] is None
    for changes in ({"cand_2": "2001.00001"}, {"cand_3": "2001.00002"}, {"partition": "validation"}):
        with pytest.raises(c.ContractError):
            replace(row, **changes)


def test_partition_manifest_content_hash_and_group_quota(c):
    m = manifest(c)
    assert m.counts == {"train": {"manual": 15, "auto": 2},
                         "valid": {"manual": 5, "auto": 5}, "test": {"manual": 5, "auto": 5}}
    assert c.validate_split_quotas(m, population_count=145).valid
    assert not c.validate_split_quotas(m, population_count=146).valid
    assert manifest(c, list(reversed(partition_rows()))).split_id == m.split_id
    assert replace(m, label_snapshot_id="changed").split_id != m.split_id
    assert replace(m, rows=[replace(r, partition="train") if r.partition == "valid" else r
                             for r in m.rows]).split_id != m.split_id
    with pytest.raises(c.ContractError, match="duplicate"):
        replace(m, rows=m.rows + [m.rows[0]])
    with pytest.raises(c.ContractError):
        replace(m, auto_publication_ids=m.auto_publication_ids + [m.manual_publication_ids[0]])
    with pytest.raises(c.ContractError):
        replace(m, group_size=True)


def test_compatibility_with_real_ms1_row_serializer(c):
    from arxiv_pipeline.crawling.s2.store import reference_row
    row = reference_row("2310.12345", dict(arxiv_id="2001.00001", title="Synthetic",
                        authors=["Nguyễn An"], submission_date=None, s2_id="synthetic-s2"))
    result = c.publication_from_rows("2310.12345", publications()[0]["bibtex"], [row], readiness="ready")
    assert result.references[0].ref_s2_id == "synthetic-s2"
    assert result.references[0].ref_submission_date is None


def test_missing_input_cannot_be_promoted_to_ready_by_label_validation(c):
    b = replace(bundle(c), readiness="not_ready")
    report = c.validate_labels([b], [c.LabelRecord(**labels()[0])])
    assert not report.valid
    assert report.errors[0].code == "input_not_ready"


def test_feature_schema_order_and_nullable_values_are_enforced(c):
    specs = [c.FeatureSpec("year_gap", "int64", True, 0, None, "keep_null", "Year gap", ["year"]),
             c.FeatureSpec("title_missing", "bool", False, None, None, "error", "Missing title", ["title"])]
    schema = c.FeatureSchema(specs)
    row = c.PairFeature("2310.12345", "RefA", "2001.00001", {"year_gap": None, "title_missing": True})
    assert c.validate_features(c.FeatureBatch([row], schema.schema_id), schema).valid
    assert c.FeatureSchema(list(reversed(specs))).schema_id != schema.schema_id
    assert not c.validate_features(c.FeatureBatch([row, row], schema.schema_id), schema).valid
    assert not c.validate_features(c.FeatureBatch([replace(row, values={"year_gap": .5,
        "title_missing": True})], schema.schema_id), schema).valid


def test_snapshot_fixture_is_separate_reproducible_and_marked_synthetic(c, tmp_path):
    from fixtures.matching.examples import write_compatibility_fixture
    first, second = tmp_path / "first", tmp_path / "second"
    layout = write_compatibility_fixture(first)
    write_compatibility_fixture(second)
    content = lambda root: {p.relative_to(root).as_posix(): p.read_bytes()
                            for p in root.rglob("*") if p.is_file()}
    assert content(first) == content(second)
    assert json.loads((first / "fixture_manifest.json").read_text(encoding="utf-8"))["synthetic"] is True
    m1 = json.loads((layout["ms1"] / "references/2310.12345.json").read_text(encoding="utf-8"))
    m2 = json.loads((layout["ms2"] / "tables/2310.12345/bibtex.json").read_text(encoding="utf-8"))
    loaded = c.publication_from_rows("2310.12345", m2, m1, readiness="ready")
    assert loaded.references[0].ref_arxiv_id == "2001.00001"
    with pytest.raises(FileExistsError):
        write_compatibility_fixture(first)


def test_scores_and_predictions_keep_query_and_reference_foreign_keys(c):
    b = bundle(c)
    score = c.ScoreRecord(b.arxiv_id, "RefA", "2001.00001", .9)
    assert c.validate_scores([b], [score]).valid
    assert not c.validate_scores([b], [score, score]).valid
    assert not c.validate_scores([b], []).valid
    assert not c.validate_scores([b], [replace(score, ref_arxiv_id="2001.99999")]).valid
    assert c.validate_scores([bundle(c, 3)], []).valid
    p = c.RankedPrediction(b.arxiv_id, "test", "RefA", "2001.00001", "2001.00001")
    rows = [c.PartitionRow(b.arxiv_id, "test", "manual")]
    m = c.PartitionManifest(42, 5, "pop", "labels", [b.arxiv_id], [], rows)
    assert c.validate_predictions([b], [p], [c.LabelRecord(**labels()[0])], m).valid
    assert not c.validate_predictions([b], [replace(p, partition="train")], [], m).valid
    assert not c.validate_predictions([b], [replace(p, cand_1="2001.99999")], [], m).valid
    assert not c.validate_predictions([b], [], [], m).valid
    assert not c.validate_predictions([b], [replace(p, groundtruth_arxiv_id=None)],
                                      [c.LabelRecord(**labels()[0])], m).valid


def test_serialized_manifests_and_feature_batches_keep_contract_metadata(c):
    m = manifest(c)
    payload = json.loads(json.dumps(m.to_dict(), allow_nan=False))
    assert payload["schema_version"] == "3.0"
    assert payload["split_id"] == m.split_id
    assert payload["counts"]["train"] == {"manual": 15, "auto": 2}
    assert payload["population_snapshot_id"] == "synthetic-pop"
    row = c.PairFeature("2310.12345", "RefA", "2001.00001", {"title_missing": True})
    data = c.FeatureBatch([row], "synthetic-feature-schema").to_dict()
    assert data["rows"][0]["title_missing"] is True
    assert "values" not in data["rows"][0]
