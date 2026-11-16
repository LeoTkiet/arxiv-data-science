"""TV2 deterministic baseline module for Milestone 3 Reference Matching.

Implements rule-based and heuristic pair scoring and Top-5 ranking.
Provides a stable, leak-free benchmark for evaluation and MRR@5 comparison.
"""

from __future__ import annotations

from typing import Any

from arxiv_pipeline.matching.contracts import (
    ContractError,
    FeatureBatch,
    LabelRecord,
    PairFeature,
    PartitionManifest,
    PublicationBundle,
    RankedPrediction,
    ScoreRecord,
)


def score_pair_heuristics(values: dict[str, Any] | PairFeature) -> float:
    """Compute a deterministic heuristic matching score in [0.0, 1.0].

    Priority:
    1. Exact identifier match (arXiv ID or DOI) -> score = 1.0.
    2. Weighted combination of text, author, and temporal features.
    """
    if isinstance(values, PairFeature):
        vals = values.values
    elif isinstance(values, dict):
        vals = values
    else:
        raise ContractError("values must be a dict or PairFeature")

    # Priority 1: Exact direct matches
    if vals.get("arxiv_id_match") == 1:
        return 1.0
    if vals.get("doi_exact_match") == 1:
        return 0.98

    # Priority 2: Title exact match
    if vals.get("title_exact_match") == 1:
        base_score = 0.90
        # Boost if first author also matches
        if vals.get("first_author_match") == 1:
            base_score += 0.05
        return min(0.95, base_score)

    # Priority 3: Weighted fuzzy similarity
    title_cosine = float(vals.get("title_char_ngram_cosine", 0.0) or 0.0)
    title_jaccard = float(vals.get("title_jaccard_word", 0.0) or 0.0)
    author_jaccard = float(vals.get("author_jaccard_last", 0.0) or 0.0)
    first_author = float(vals.get("first_author_match", 0) or 0)
    year_exact = float(vals.get("year_exact_match", 0) or 0)

    score = (
        0.50 * title_cosine
        + 0.20 * title_jaccard
        + 0.15 * author_jaccard
        + 0.05 * first_author
        + 0.10 * year_exact
    )

    # Penalize non-chronological references
    if vals.get("year_is_chronological") == 0:
        score *= 0.5

    # Penalize severe title length differences
    len_diff = float(vals.get("title_len_diff_ratio", 0.0) or 0.0)
    if len_diff > 0.5:
        score *= (1.0 - 0.5 * len_diff)

    return max(0.0, min(1.0, float(score)))


def predict_baseline_scores(feature_batch: FeatureBatch) -> list[ScoreRecord]:
    """Score all candidate pairs in a FeatureBatch using deterministic heuristics."""
    if not isinstance(feature_batch, FeatureBatch):
        raise ContractError("feature_batch must be an instance of FeatureBatch")

    scores: list[ScoreRecord] = []
    for row in feature_batch.rows:
        sc = score_pair_heuristics(row)
        scores.append(
            ScoreRecord(
                arxiv_id=row.arxiv_id,
                citation_key=row.citation_key,
                ref_arxiv_id=row.ref_arxiv_id,
                score=sc,
            )
        )
    return scores


def rank_top5_predictions(
    bundles: list[PublicationBundle],
    scores: list[ScoreRecord],
    manifest: PartitionManifest,
    labels: list[LabelRecord] | None = None,
) -> list[RankedPrediction]:
    """Rank candidates into Top-5 predictions conforming strictly to contract 3.0.

    Requirements:
    - Exactly one RankedPrediction per (arxiv_id, citation_key) in the split publications.
    - Tie-breaking is deterministic: score DESC, then ref_arxiv_id ASC.
    - Trailing candidate slots are None when fewer than 5 candidates exist.
    - groundtruth_arxiv_id joined only from accepted, non-uncertain labels.
    """
    partitions = {r.arxiv_id: r.partition for r in manifest.rows}
    truth: dict[tuple[str, str], str | None] = {}
    if labels:
        truth = {
            r.key: r.groundtruth_arxiv_id
            for r in labels
            if r.is_supervised
        }

    # Group scores by QueryKey: (arxiv_id, citation_key)
    scores_by_query: dict[tuple[str, str], list[ScoreRecord]] = {}
    for s in scores:
        scores_by_query.setdefault((s.arxiv_id, s.citation_key), []).append(s)

    predictions: list[RankedPrediction] = []
    for bundle in bundles:
        paper = bundle.arxiv_id
        if paper not in partitions:
            continue
        partition = partitions[paper]

        for b in bundle.bibtex:
            q_key = (paper, b.citation_key)
            query_scores = scores_by_query.get(q_key, [])

            # Deterministic sorting: score DESC, ref_arxiv_id ASC
            ranked_candidates = sorted(
                query_scores,
                key=lambda item: (-item.score, item.ref_arxiv_id),
            )

            top5_ids = [item.ref_arxiv_id for item in ranked_candidates[:5]]
            # Pad with None up to 5
            padded = top5_ids + [None] * (5 - len(top5_ids))

            predictions.append(
                RankedPrediction(
                    arxiv_id=paper,
                    partition=partition,
                    citation_key=b.citation_key,
                    groundtruth_arxiv_id=truth.get(q_key),
                    cand_1=padded[0],
                    cand_2=padded[1],
                    cand_3=padded[2],
                    cand_4=padded[3],
                    cand_5=padded[4],
                )
            )

    return predictions
