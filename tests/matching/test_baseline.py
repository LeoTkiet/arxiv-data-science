"""Unit tests for TV2 deterministic heuristic baseline module."""

from arxiv_pipeline.matching.baseline import (
    baseline_score,
    predict_baseline_scores,
    rank_top5_predictions,
    score_pair_heuristics,
)
from arxiv_pipeline.matching.contracts import (
    LabelRecord,
    PartitionManifest,
    PartitionRow,
    PublicationBundle,
    publication_from_rows,
    validate_predictions,
)
from arxiv_pipeline.matching.features import build_feature_batch
from fixtures.matching.examples import labels as example_labels, publications


def test_predict_baseline_scores():
    pubs = publications()
    bundles = [
        publication_from_rows(p["arxiv_id"], p["bibtex"], p["references"], readiness="ready")
        for p in pubs
    ]
    batch = build_feature_batch(bundles)
    scores = predict_baseline_scores(batch)
    assert len(scores) == len(batch.rows)

    # Test baseline_score alias
    scores_alias = baseline_score(batch)
    assert [s.score for s in scores_alias] == [s.score for s in scores]

    # Exact case should have higher score than unmatched case
    score_map = {(s.arxiv_id, s.ref_arxiv_id): s.score for s in scores}
    assert score_map[("2310.12345", "2001.00001")] > 0.8
    assert score_map[("2310.12347", "2001.00004")] < 0.3


def test_rank_top5_predictions_and_contract_validation():
    pubs = publications()
    bundles = [
        publication_from_rows(p["arxiv_id"], p["bibtex"], p["references"], readiness="ready")
        for p in pubs
    ]
    batch = build_feature_batch(bundles)
    scores = predict_baseline_scores(batch)

    label_records = [LabelRecord(**l) for l in example_labels()]
    prows = [
        PartitionRow("2310.12345", "valid", "manual"),
        PartitionRow("2310.12346", "valid", "auto"),
        PartitionRow("2310.12347", "test", "manual"),
        PartitionRow("hep-th/9901001", "train", "auto"),
    ]
    manifest = PartitionManifest(
        seed=42,
        group_size=1,
        population_snapshot_id="pop-test",
        label_snapshot_id="lbl-test",
        manual_publication_ids=["2310.12345", "2310.12347"],
        auto_publication_ids=["2310.12346", "hep-th/9901001"],
        rows=prows,
    )

    predictions = rank_top5_predictions(bundles, scores, manifest, label_records)
    assert len(predictions) == 4

    # Validate against official contract validator
    report = validate_predictions(bundles, predictions, label_records, manifest)
    assert report.valid, f"Validation errors: {report.errors}"
