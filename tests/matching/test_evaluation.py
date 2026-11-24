"""Unit & integration tests for TV5 split, MRR@5, prediction, and evaluation."""

from __future__ import annotations

import json
import random

import pytest

from arxiv_pipeline.matching.contracts import (
    ContractError,
    FeatureBatch,
    LabelRecord,
    PairFeature,
    PartitionManifest,
    PartitionRow,
    PublicationBundle,
    RankedPrediction,
    ScoreRecord,
    publication_from_rows,
    validate_predictions,
    validate_split_quotas,
)
from arxiv_pipeline.matching.evaluation import (
    evaluate,
    evaluate_scores,
    run as evaluation_run,
    save_evaluation_report,
)
from arxiv_pipeline.matching.metrics import (
    UNMATCHED_POLICY,
    compute_ranking_metrics,
    mrr_at_5,
    rank_candidates_from_scores,
    reciprocal_rank_at_k,
    score_mrr_at_5,
)
from arxiv_pipeline.matching.prediction import (
    build_predictions,
    build_predictions_from_bundles,
    load_predictions_json,
    run as prediction_run,
    save_predictions_json,
)
from arxiv_pipeline.matching.split import (
    assert_no_split_leakage,
    create_split,
    load_partition_manifest,
    save_partition_manifest,
)


def _make_bib(pid: str, ckey: str, *, title: str = "Paper title", year: int | None = 2022, doi: str | None = None):
    return {
        "arxiv_id": pid,
        "citation_key": ckey,
        "entry_type": "article",
        "title": title,
        "authors": ["Alice Smith", "Bob Jones"],
        "year": year,
        "doi": doi,
        "journal": None,
        "url": None,
    }


def _make_ref(pid: str, rid: str, *, title: str = "Ref title", date: str | None = "2022-01-01"):
    return {
        "arxiv_id": pid,
        "ref_arxiv_id": rid,
        "ref_title": title,
        "ref_authors": ["Alice Smith"],
        "ref_submission_date": date,
        "ref_s2_id": f"s2-{rid}",
    }


def _make_pool_dataset(
    *,
    n_manual: int = 25,
    n_auto: int = 12,
    entries_per_pub: int = 20,
    n_refs: int = 7,
    seed: int = 7,
):
    """Build a deterministic 5-member-quota synthetic dataset with bundles, labels & mock scores."""
    rng = random.Random(seed)
    bundles: list[PublicationBundle] = []
    labels: list[LabelRecord] = []
    model_scores: list[ScoreRecord] = []
    baseline_scores: list[ScoreRecord] = []

    ref_seq = 1
    for idx in range(n_manual + n_auto):
        is_manual = idx < n_manual
        pid = f"2401.{idx + 1:05d}" if is_manual else f"2402.{idx - n_manual + 1:05d}"
        local_n_refs = 0 if (not is_manual and idx == n_manual + n_auto - 1) else n_refs
        refs = []
        ref_ids = []
        for _ in range(local_n_refs):
            rid = f"2001.{ref_seq:05d}"
            ref_seq += 1
            ref_ids.append(rid)
            refs.append(_make_ref(pid, rid, title=f"Candidate Paper {rid}"))

        bibs = []
        for q in range(entries_per_pub):
            ckey = f"Ref{q:02d}"
            title = "" if (q == 0 and idx % 4 == 0) else f"Query Title {pid} {q}"
            bibs.append(_make_bib(pid, ckey, title=title, year=None if q == 1 else 2022))

            if local_n_refs == 0 or q % 5 == 0:
                gold, decision = None, "unmatched"
            else:
                gold, decision = ref_ids[q % local_n_refs], "matched"

            if is_manual:
                labels.append(
                    LabelRecord(
                        arxiv_id=pid,
                        citation_key=ckey,
                        groundtruth_arxiv_id=gold,
                        decision=decision,
                        label_source="manual",
                        annotator=f"tv{(idx % 5) + 1}",
                        reviewer=f"tv{((idx + 1) % 5) + 1}",
                        review_status="accepted",
                    )
                )
            else:
                labels.append(
                    LabelRecord(
                        arxiv_id=pid,
                        citation_key=ckey,
                        groundtruth_arxiv_id=gold,
                        decision=decision,
                        label_source="heuristic",
                        confidence=0.92,
                        heuristic_version="mock-h-v1",
                        review_status="accepted",
                    )
                )

            for rid in ref_ids:
                is_gold = rid == gold
                m_score = (0.85 if is_gold else 0.30) + rng.uniform(-0.15, 0.15)
                b_score = (0.55 if is_gold else 0.40) + rng.uniform(-0.20, 0.20)
                model_scores.append(ScoreRecord(pid, ckey, rid, round(m_score, 6)))
                baseline_scores.append(ScoreRecord(pid, ckey, rid, round(b_score, 6)))

        bundles.append(publication_from_rows(pid, bibs, refs, readiness="ready"))

    return bundles, labels, model_scores, baseline_scores


# --------------------------------------------------------------------------- split & leakage tests
def test_create_split_five_member_quotas_and_disjointness(tmp_path):
    bundles, labels, _, _ = _make_pool_dataset(n_manual=25, n_auto=12, entries_per_pub=20)
    pop_ids = [b.arxiv_id for b in bundles] + [f"2403.{i:05d}" for i in range(1, 109)]
    assert len(pop_ids) == 145

    manifest = create_split(
        labels,
        {"publication_ids": pop_ids, "population_snapshot_id": "pop-145"},
        seed=42,
        group_size=5,
        bundles=bundles,
    )
    assert manifest.counts == {
        "train": {"manual": 15, "auto": 2},
        "valid": {"manual": 5, "auto": 5},
        "test": {"manual": 5, "auto": 5},
    }
    assert validate_split_quotas(manifest, population_count=145).valid
    guard = assert_no_split_leakage(manifest, labels=labels, population_count=145)
    assert guard["disjoint_verified"] is True
    assert guard["gold_coverage"]["valid"]["gold_publications"] > 0
    assert guard["gold_coverage"]["test"]["gold_publications"] > 0

    # Order-independent & deterministic
    manifest_rev = create_split(
        list(reversed(labels)),
        {"publication_ids": list(reversed(pop_ids)), "population_snapshot_id": "pop-145"},
        seed=42,
        group_size=5,
    )
    assert manifest_rev.split_id == manifest.split_id
    assert manifest_rev.rows == manifest.rows

    # Short contract signature create_split(label_manifest, seed)
    assert create_split(labels, 42).counts == manifest.counts

    # Save / load round-trip
    saved = save_partition_manifest(manifest, tmp_path / "partitions.json")
    loaded = load_partition_manifest(saved)
    assert loaded == manifest


def test_create_split_rejects_pool_overlap_and_insufficient_quotas():
    _, labels, _, _ = _make_pool_dataset(n_manual=25, n_auto=12, entries_per_pub=2)

    with pytest.raises(ContractError, match="Insufficient auto publications"):
        create_split(labels, {"population_count": 200}, seed=1, group_size=5)

    small_labels = [l for l in labels if l.arxiv_id != "2401.00001"]
    with pytest.raises(ContractError, match="Insufficient manual publications"):
        create_split(small_labels, seed=1, group_size=5)

    mixed = list(labels) + [
        LabelRecord(
            arxiv_id="2401.00001",
            citation_key="ExtraMixedKey",
            groundtruth_arxiv_id=None,
            decision="unmatched",
            label_source="heuristic",
            confidence=0.8,
            heuristic_version="v1",
            review_status="accepted",
        )
    ]
    with pytest.raises(ContractError, match="pool overlap"):
        create_split(mixed, seed=1, group_size=5)


def test_split_stratification_preserves_gold_coverage_when_feasible():
    labels: list[LabelRecord] = []
    for prefix, src in (("2401", "manual"), ("2402", "heuristic")):
        for i in range(1, 26 if src == "manual" else 13):
            pid = f"{prefix}.{i:05d}"
            has_gold = i <= 4
            for q in range(2):
                matched = has_gold and q == 0
                labels.append(
                    LabelRecord(
                        arxiv_id=pid,
                        citation_key=f"k{q}",
                        groundtruth_arxiv_id="2001.00001" if matched else None,
                        decision="matched" if matched else "unmatched",
                        label_source=src,
                        confidence=0.9 if src == "heuristic" else None,
                        heuristic_version="v1" if src == "heuristic" else None,
                        annotator="tv5" if src == "manual" else None,
                        reviewer="tv1" if src == "manual" else None,
                        review_status="accepted",
                    )
                )

    manifest = create_split(labels, seed=99, group_size=5, stratify=True)
    summary = assert_no_split_leakage(manifest, labels=labels)
    assert summary["gold_coverage"]["valid"]["gold_publications"] >= 2
    assert summary["gold_coverage"]["test"]["gold_publications"] >= 2


def test_leakage_guard_catches_feature_and_prediction_leakage():
    _, labels, _, _ = _make_pool_dataset(n_manual=25, n_auto=12, entries_per_pub=2)
    manifest = create_split(labels, seed=3, group_size=5)
    test_pub = next(r.arxiv_id for r in manifest.rows if r.partition == "test")

    leaked_batch = FeatureBatch(
        [PairFeature(test_pub, "Ref00", "2001.00001", {"sim": 0.9})],
        "schema-1",
    )
    with pytest.raises(ContractError, match="Feature leakage"):
        assert_no_split_leakage(manifest, feature_batches={"train": leaked_batch})

    bad_pred = RankedPrediction(test_pub, "train", "Ref00", None)
    with pytest.raises(ContractError, match="partition mismatch"):
        assert_no_split_leakage(manifest, predictions=[bad_pred])


# --------------------------------------------------------------------------- MRR@5 hand-solved toy tests
def test_mrr_at_5_hand_computed_toy_examples():
    cands = [f"2001.0000{i}" for i in range(1, 7)]
    assert reciprocal_rank_at_k("2001.00001", cands) == 1.0
    assert reciprocal_rank_at_k("2001.00002", cands) == 0.5
    assert reciprocal_rank_at_k("2001.00003", cands) == pytest.approx(1 / 3)
    assert reciprocal_rank_at_k("2001.00004", cands) == 0.25
    assert reciprocal_rank_at_k("2001.00005", cands) == 0.2
    assert reciprocal_rank_at_k("2001.00006", cands) == 0.0
    assert reciprocal_rank_at_k("2001.00099", cands) == 0.0
    assert reciprocal_rank_at_k(None, cands) == 0.0
    assert reciprocal_rank_at_k(None, []) == 0.0

    # Official contract example: (1/2 + 0) / 2 = 0.25
    gt = {("2310.12345", "q1"): "2001.00002", ("2310.12345", "q2"): None}
    ranked = {
        ("2310.12345", "q1"): ["2001.00001", "2001.00002", "2001.00003"],
        ("2310.12345", "q2"): ["2001.00001", "2001.00002"],
    }
    assert mrr_at_5(gt, ranked) == pytest.approx(0.25)

    # 5-query hand solution: (1 + 1/3 + 1/5 + 0 + 0) / 5 = 23/75; matched-only = 23/60
    gt5 = {
        ("2310.00001", "q1"): "2001.00001v2",
        ("2310.00001", "q2"): "2001.00003",
        ("2310.00001", "q3"): "2001.00005",
        ("2310.00001", "q4"): "2001.00006",
        ("hep-th/9901001", "q5"): None,
    }
    ranked5 = {
        ("2310.00001", "q1"): ["2001.00001", "2001.00002", None, None, None],
        ("2310.00001", "q2"): ["2001.00001", "2001.00002", "2001.00003"],
        ("2310.00001", "q3"): ["2001.00001", "2001.00002", "2001.00003", "2001.00004", "2001.00005"],
        ("2310.00001", "q4"): ["2001.00001", "2001.00002", "2001.00003", "2001.00004", "2001.00005"],
        ("hep-th/9901001", "q5"): [],
    }
    detail = compute_ranking_metrics(gt5, ranked5)
    assert detail["mrr_at_5"] == pytest.approx(23 / 75)
    assert detail["mrr_at_5_matched_only"] == pytest.approx(23 / 60)
    assert detail["recall_at_1"] == pytest.approx(1 / 4)
    assert detail["recall_at_3"] == pytest.approx(2 / 4)
    assert detail["recall_at_5"] == pytest.approx(3 / 4)
    assert detail["unmatched_policy"] == UNMATCHED_POLICY
    assert detail["counts"]["empty_candidate_queries"] == 1


def test_mrr_at_5_rejects_empty_duplicates_holes_and_missing():
    with pytest.raises(ContractError, match="at least one labeled query"):
        mrr_at_5({}, {})

    with pytest.raises(ContractError, match="Duplicate ranked candidate"):
        mrr_at_5(
            {("2310.00001", "q1"): "2001.00001"},
            {("2310.00001", "q1"): ["2001.00001", "2001.00001"]},
        )

    with pytest.raises(ContractError, match="Non-trailing NULL"):
        mrr_at_5(
            {("2310.00001", "q1"): "2001.00001"},
            {("2310.00001", "q1"): [None, "2001.00001"]},
        )

    with pytest.raises(ContractError, match="Missing ranked candidates"):
        mrr_at_5(
            {("2310.00001", "q1"): "2001.00001", ("2310.00001", "q2"): None},
            {("2310.00001", "q1"): ["2001.00001"]},
        )


def test_ranking_tied_candidates_and_tv4_score_adapter():
    q = ("2310.12345", "RefTie")
    raw_scores = [
        ScoreRecord(*q, "2001.00003", 0.5),
        ScoreRecord(*q, "2001.00001v2", 0.5),
        ScoreRecord(*q, "hep-th/9901001", 0.5),
        ScoreRecord(*q, "2001.00002", 0.5),
    ]
    ranked = rank_candidates_from_scores(raw_scores, query=q)
    assert ranked == ["2001.00001", "2001.00002", "2001.00003", "hep-th/9901001"]
    assert rank_candidates_from_scores(list(reversed(raw_scores)), query=q) == ranked

    targets = {(*q, cid): int(cid == "2001.00002") for cid in ranked}
    assert score_mrr_at_5(raw_scores, targets) == pytest.approx(0.5)
    assert score_mrr_at_5(raw_scores, targets, queries={("2310.99999", "EmptyQ")}) == pytest.approx(0.25)

    with pytest.raises(ContractError, match="Missing scores"):
        score_mrr_at_5(raw_scores[:-1], targets)


# --------------------------------------------------------------------------- prediction & evaluation tests
def test_build_predictions_schema_ties_padding_and_empty_references(tmp_path):
    b_a = publication_from_rows(
        "2310.00001",
        [_make_bib("2310.00001", "k1"), _make_bib("2310.00001", "k_uncertain")],
        [_make_ref("2310.00001", f"2001.0000{i}") for i in range(1, 7)],
        readiness="ready",
    )
    b_b = publication_from_rows(
        "2310.00002",
        [_make_bib("2310.00002", "k_unmatched")],
        [_make_ref("2310.00002", "2002.00002"), _make_ref("2310.00002", "2002.00001")],
        readiness="ready",
    )
    b_c = publication_from_rows(
        "hep-th/9901001",
        [_make_bib("hep-th/9901001", "k_empty")],
        [],
        readiness="ready",
    )
    bundles = [b_a, b_b, b_c]
    manifest = PartitionManifest(
        seed=0,
        group_size=1,
        population_snapshot_id="pop-toy",
        label_snapshot_id="lbl-toy",
        manual_publication_ids=["2310.00001", "hep-th/9901001"],
        auto_publication_ids=["2310.00002"],
        rows=[
            PartitionRow("2310.00001", "test", "manual"),
            PartitionRow("2310.00002", "valid", "auto"),
            PartitionRow("hep-th/9901001", "train", "manual"),
        ],
    )
    labels = [
        LabelRecord("2310.00001", "k1", "2001.00004", "matched", "manual", annotator="tv5", reviewer="tv1", review_status="accepted"),
        LabelRecord("2310.00001", "k_uncertain", None, "uncertain", "manual", annotator="tv5"),
        LabelRecord("2310.00002", "k_unmatched", None, "unmatched", "heuristic", confidence=0.9, heuristic_version="v1", review_status="accepted"),
        LabelRecord("hep-th/9901001", "k_empty", None, "unmatched", "manual", annotator="tv5", reviewer="tv1", review_status="accepted"),
    ]
    scores_a = [
        ScoreRecord("2310.00001", ckey, rid, sc)
        for ckey in ("k1", "k_uncertain")
        for rid, sc in [
            ("2001.00001", 0.1),
            ("2001.00002", 0.8),
            ("2001.00003", 0.6),
            ("2001.00004", 0.8),
            ("2001.00005", 0.7),
            ("2001.00006", 0.9),
        ]
    ]
    scores_b = [
        ScoreRecord("2310.00002", "k_unmatched", "2002.00002", 0.5),
        ScoreRecord("2310.00002", "k_unmatched", "2002.00001", 0.5),
    ]
    scores = scores_a + scores_b

    preds = build_predictions_from_bundles(bundles, scores, labels, manifest)
    assert len(preds) == 4
    assert validate_predictions(bundles, preds, labels, manifest).valid

    by_key = {p.key: p for p in preds}
    p_k1 = by_key[("2310.00001", "k1")]
    assert p_k1.partition == "test"
    assert p_k1.groundtruth_arxiv_id == "2001.00004"
    assert [p_k1.cand_1, p_k1.cand_2, p_k1.cand_3, p_k1.cand_4, p_k1.cand_5] == [
        "2001.00006",
        "2001.00002",
        "2001.00004",
        "2001.00005",
        "2001.00003",
    ]
    assert by_key[("2310.00001", "k_uncertain")].groundtruth_arxiv_id is None

    p_b = by_key[("2310.00002", "k_unmatched")]
    assert [p_b.cand_1, p_b.cand_2, p_b.cand_3, p_b.cand_4, p_b.cand_5] == [
        "2002.00001",
        "2002.00002",
        None,
        None,
        None,
    ]
    p_c = by_key[("hep-th/9901001", "k_empty")]
    assert [p_c.cand_1, p_c.cand_2, p_c.cand_3, p_c.cand_4, p_c.cand_5] == [None] * 5

    all_queries = [b for bundle in bundles for b in bundle.bibtex]
    all_cands = [r for bundle in bundles for r in bundle.references]
    assert build_predictions(all_queries, all_cands, list(reversed(scores)), labels, manifest) == preds

    saved = save_predictions_json(preds, tmp_path / "preds.json")
    assert load_predictions_json(saved) == preds

    with pytest.raises(ContractError, match="Missing scores"):
        build_predictions_from_bundles(bundles, scores[:-1], labels, manifest)
    with pytest.raises(ContractError, match="Duplicate score"):
        build_predictions_from_bundles(bundles, scores + [scores[0]], labels, manifest)

    res = prediction_run(
        {"arxiv_id": "2310.00001"},
        tmp_path,
        upstream={"bundles": [b_a], "scores": scores_a, "labels": labels, "partition": "test", "pool": "manual"},
    )
    assert res.status == "success"
    assert res.schema_version == "3.0"


def test_evaluate_scores_tv4_vs_tv2_baseline_end_to_end(tmp_path):
    bundles, labels, model_scores, baseline_scores = _make_pool_dataset(
        n_manual=25, n_auto=12, entries_per_pub=10, n_refs=7, seed=11
    )
    manifest = create_split(labels, seed=42, group_size=5)

    result = evaluate_scores(
        bundles,
        model_scores,
        labels,
        manifest,
        partition="test",
        model_name="tv4_hgb_mock",
        baseline_scores=baseline_scores,
        baseline_name="tv2_baseline_mock",
        provenance={"synthetic": True},
    )
    assert result["partition"] == "test"
    assert result["unmatched_policy"] == UNMATCHED_POLICY
    assert result["model"]["metrics"]["mrr_at_5"] > result["baseline"]["metrics"]["mrr_at_5"]
    assert result["comparison"]["delta_mrr_at_5"] > 0
    assert set(result["model"]["slices"]["by_pool"]) == {"manual", "auto"}
    assert "by_metadata" in result["model"]["slices"]
    assert "top1_confusion" in result["model"]["confusion_and_errors"]
    assert len(result["model"]["prediction_preview"]) > 0

    out_file = save_evaluation_report(result, tmp_path / "evaluation_test.json")
    loaded = json.loads(out_file.read_text(encoding="utf-8"))
    assert loaded["split_id"] == manifest.split_id
    assert loaded["model"]["provenance"]["synthetic"] is True

    bad_pred = RankedPrediction(
        result["predictions"][0].arxiv_id,
        "train",
        result["predictions"][0].citation_key,
        result["predictions"][0].groundtruth_arxiv_id,
    )
    with pytest.raises(ContractError, match="Partition mismatch"):
        evaluate([bad_pred], labels, manifest, partition="test")

    stage_res = evaluation_run(
        {"arxiv_id": "run"},
        tmp_path,
        upstream={
            "bundles": bundles,
            "scores": model_scores,
            "baseline_scores": baseline_scores,
            "labels": labels,
            "partition_manifest": manifest,
            "partition": "test",
            "provenance": {"synthetic": True},
        },
    )
    assert stage_res.status == "success"
    assert stage_res.schema_version == "3.0"
    assert (tmp_path / stage_res.artifacts["evaluation"]).is_file()
