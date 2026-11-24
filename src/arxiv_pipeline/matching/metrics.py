"""Official MRR@5 ranking metrics & tie/duplicate/missing checks (Milestone 3 · TV5).

Implements the official Milestone 3 evaluation metric and docs/MS3_CONTRACTS.md §7:
- Query unit: QueryKey = (arxiv_id, citation_key).
- Ranking order: score descending; ties broken by canonical ref_arxiv_id ascending.
- Reciprocal rank at 5: 1/r if groundtruth appears at 1-based rank r <= 5, else 0.
- Official unmatched convention: every accepted supervised query (matched OR
  unmatched, including queries with 0 candidates) counts in the denominator |Q|;
  no-hit and unmatched queries score 0.0. Empty labeled query sets raise
  ContractError (never MRR=0).
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

from arxiv_pipeline.matching.contracts import (
    ContractError,
    LabelRecord,
    PairKey,
    QueryKey,
    RankedPrediction,
    ScoreRecord,
    canonical_id,
)

UNMATCHED_POLICY = "include_zero_in_denominator"
UNMATCHED_POLICY_DESCRIPTION = (
    "Official MS3 convention: every accepted supervised query (decision in "
    "{matched, unmatched}, including queries with 0 candidates) is included in the "
    "MRR@5 denominator |Q|. Matched queries score 1/r for rank r <= 5 and 0.0 on "
    "miss (r > 5); unmatched queries score 0.0. Uncertain, pending, rejected, and "
    "unlabeled queries are excluded from MRR@5."
)


def _canon_query_key(key: QueryKey) -> QueryKey:
    if not isinstance(key, tuple) or len(key) != 2:
        raise ContractError(f"QueryKey must be a 2-tuple (arxiv_id, citation_key), got {key!r}")
    arxiv_id, citation_key = key
    if not isinstance(citation_key, str) or not citation_key.strip():
        raise ContractError("citation_key must be a nonempty string")
    return canonical_id(arxiv_id), citation_key


def _validate_ranked_list(
    candidates: Sequence[str | None],
    *,
    query: QueryKey,
    max_len: int | None = None,
) -> list[str]:
    """Validate a ranked candidate sequence: canonical IDs, trailing nulls only, no duplicates."""
    if isinstance(candidates, (str, bytes)):
        raise ContractError(f"Ranked candidates for {query} must be a sequence, not str/bytes")

    seen_null = False
    present: list[str] = []
    seen_ids: set[str] = set()

    for idx, cand in enumerate(candidates):
        if cand is None:
            seen_null = True
            continue
        if seen_null:
            raise ContractError(
                f"Non-trailing NULL candidate at position {idx + 1} for query {query}"
            )
        cid = canonical_id(cand)
        if cid in seen_ids:
            raise ContractError(f"Duplicate ranked candidate {cid!r} for query {query}")
        seen_ids.add(cid)
        present.append(cid)

    if max_len is not None and len(present) > max_len:
        return present[:max_len]
    return present


def rank_candidates_from_scores(
    scores: Sequence[ScoreRecord],
    *,
    expected_candidates: set[str] | None = None,
    query: QueryKey | None = None,
    top_k: int | None = None,
) -> list[str]:
    """Sort candidate ScoreRecords by (-score, canonical ref_arxiv_id).

    Rejects duplicate pairs, non-finite scores, missing expected candidates,
    or unexpected candidates outside the publication when `expected_candidates`
    is provided.
    """
    seen: set[str] = set()
    normalized: list[tuple[float, str]] = []

    for rec in scores:
        if not isinstance(rec, ScoreRecord):
            raise ContractError(f"Expected ScoreRecord, got {type(rec).__name__}")
        if query is not None and (rec.arxiv_id, rec.citation_key) != query:
            raise ContractError(
                f"Score query {(rec.arxiv_id, rec.citation_key)} does not match {query}"
            )
        if type(rec.score) not in (int, float) or not math.isfinite(rec.score):
            raise ContractError(f"Non-finite score for {rec.key}")
        ref_id = canonical_id(rec.ref_arxiv_id)
        if ref_id in seen:
            raise ContractError(
                f"Duplicate score for candidate {ref_id!r} on query {(rec.arxiv_id, rec.citation_key)}"
            )
        seen.add(ref_id)
        normalized.append((float(rec.score), ref_id))

    if expected_candidates is not None:
        canon_expected = {canonical_id(c) for c in expected_candidates}
        missing = sorted(canon_expected - seen)
        extra = sorted(seen - canon_expected)
        if missing:
            raise ContractError(
                f"Missing scores for {len(missing)} candidate(s) on {query}: {missing[:5]}"
            )
        if extra:
            raise ContractError(
                f"Unexpected candidate score(s) outside publication on {query}: {extra[:5]}"
            )

    normalized.sort(key=lambda item: (-item[0], item[1]))
    ranked = [ref_id for _, ref_id in normalized]
    return ranked[:top_k] if top_k is not None else ranked


def _normalize_ground_truth(
    ground_truth: Mapping[QueryKey, str | None] | Sequence[LabelRecord],
) -> tuple[dict[QueryKey, str | None], dict[QueryKey, str]]:
    """Return ({query: gt_or_None}, {query: decision}) for supervised queries."""
    truth: dict[QueryKey, str | None] = {}
    decisions: dict[QueryKey, str] = {}

    if isinstance(ground_truth, Mapping):
        for raw_key, raw_gt in ground_truth.items():
            qkey = _canon_query_key(raw_key)
            if qkey in truth:
                raise ContractError(f"Duplicate canonical query key in ground_truth: {qkey}")
            gt = None if raw_gt is None else canonical_id(raw_gt)
            truth[qkey] = gt
            decisions[qkey] = "matched" if gt is not None else "unmatched"
        return truth, decisions

    if isinstance(ground_truth, Sequence) and not isinstance(ground_truth, (str, bytes)):
        seen_all: set[QueryKey] = set()
        for item in ground_truth:
            if not isinstance(item, LabelRecord):
                raise ContractError(f"Expected LabelRecord, got {type(item).__name__}")
            if item.key in seen_all:
                raise ContractError(f"Duplicate LabelRecord for query {item.key}")
            seen_all.add(item.key)
            if item.is_supervised:
                truth[item.key] = item.groundtruth_arxiv_id
                decisions[item.key] = item.decision
        return truth, decisions

    raise ContractError("ground_truth must be a Mapping[QueryKey, str|None] or Sequence[LabelRecord]")


def _normalize_ranked_candidates(
    ranked_candidates: Mapping[QueryKey, Sequence[str | None]] | Sequence[RankedPrediction],
) -> dict[QueryKey, list[str]]:
    """Normalize ranked candidate collections to {QueryKey: list[canonical_ref_id]}."""
    out: dict[QueryKey, list[str]] = {}

    if isinstance(ranked_candidates, Mapping):
        for raw_key, seq in ranked_candidates.items():
            qkey = _canon_query_key(raw_key)
            if qkey in out:
                raise ContractError(f"Duplicate canonical query key in ranked_candidates: {qkey}")
            out[qkey] = _validate_ranked_list(seq, query=qkey)
        return out

    if isinstance(ranked_candidates, Sequence) and not isinstance(ranked_candidates, (str, bytes)):
        for row in ranked_candidates:
            if not isinstance(row, RankedPrediction):
                raise ContractError(f"Expected RankedPrediction, got {type(row).__name__}")
            if row.key in out:
                raise ContractError(f"Duplicate RankedPrediction for query {row.key}")
            raw_cands = [getattr(row, f"cand_{i}") for i in range(1, 6)]
            out[row.key] = _validate_ranked_list(raw_cands, query=row.key)
        return out

    raise ContractError(
        "ranked_candidates must be a Mapping[QueryKey, Sequence[str|None]] or Sequence[RankedPrediction]"
    )


def reciprocal_rank_at_k(
    ground_truth_id: str | None,
    ranked_candidate_ids: Sequence[str | None],
    *,
    k: int = 5,
) -> float:
    """Compute Reciprocal Rank @ k for a single query.

    Returns 1/r if canonical ground_truth_id appears at 1-based rank r <= k, else 0.0.
    Unmatched queries (ground_truth_id is None) return 0.0.
    """
    if type(k) is not int or k < 1:
        raise ContractError("k must be a positive int")
    present = _validate_ranked_list(ranked_candidate_ids, query=("<single>", "<single>"))
    if ground_truth_id is None:
        return 0.0
    target = canonical_id(ground_truth_id)
    top_k = present[:k]
    if target in top_k:
        return 1.0 / (top_k.index(target) + 1)
    return 0.0


def compute_ranking_metrics(
    ground_truth: Mapping[QueryKey, str | None] | Sequence[LabelRecord],
    ranked_candidates: Mapping[QueryKey, Sequence[str | None]] | Sequence[RankedPrediction],
    *,
    k: int = 5,
    strict: bool = True,
) -> dict[str, Any]:
    """Compute official MRR@5 plus Recall@1/@3/@5, matched-only MRR@5, and per-query ranks."""
    if type(k) is not int or k < 1:
        raise ContractError("k must be a positive int")

    truth, decisions = _normalize_ground_truth(ground_truth)
    ranked_map = _normalize_ranked_candidates(ranked_candidates)

    if not truth:
        raise ContractError("MRR@5 requires at least one labeled query")

    missing_queries = sorted(q for q in truth if q not in ranked_map)
    if missing_queries and strict:
        raise ContractError(
            f"Missing ranked candidates for {len(missing_queries)} labeled query(ies), "
            f"e.g. {missing_queries[0]}"
        )

    rr_sum = 0.0
    matched_rr_sum = 0.0
    matched_count = 0
    unmatched_count = 0
    empty_cand_count = 0
    hits_at_1 = 0
    hits_at_3 = 0
    hits_at_5 = 0
    rank_dist: dict[str, int] = {
        "rank_1": 0,
        "rank_2": 0,
        "rank_3": 0,
        "rank_4": 0,
        "rank_5": 0,
        "miss": 0,
        "unmatched": 0,
    }
    per_query: list[dict[str, Any]] = []

    for qkey in sorted(truth):
        gt = truth[qkey]
        decision = decisions[qkey]
        cands = ranked_map.get(qkey, [])
        top_k = cands[:k]
        if not cands:
            empty_cand_count += 1

        rank: int | None = None
        rr = 0.0
        if gt is None:
            unmatched_count += 1
            rank_dist["unmatched"] += 1
        else:
            matched_count += 1
            if gt in top_k:
                rank = top_k.index(gt) + 1
                rr = 1.0 / rank
                if rank <= 5:
                    rank_dist[f"rank_{rank}"] += 1
                if rank == 1:
                    hits_at_1 += 1
                if rank <= 3:
                    hits_at_3 += 1
                if rank <= 5:
                    hits_at_5 += 1
            else:
                rank_dist["miss"] += 1
            matched_rr_sum += rr

        rr_sum += rr
        per_query.append(
            {
                "arxiv_id": qkey[0],
                "citation_key": qkey[1],
                "decision": decision,
                "groundtruth_arxiv_id": gt,
                "rank": rank,
                "reciprocal_rank": rr,
                "n_candidates": len(cands),
                "top_5": top_k[:5],
            }
        )

    total = len(truth)
    mrr_official = rr_sum / total
    mrr_matched_only = (matched_rr_sum / matched_count) if matched_count else None

    return {
        "k": k,
        "mrr_at_5": mrr_official,
        "mrr_at_5_matched_only": mrr_matched_only,
        "recall_at_1": (hits_at_1 / matched_count) if matched_count else None,
        "recall_at_3": (hits_at_3 / matched_count) if matched_count else None,
        "recall_at_5": (hits_at_5 / matched_count) if matched_count else None,
        "hit_rate_at_1_all": hits_at_1 / total,
        "hit_rate_at_5_all": hits_at_5 / total,
        "unmatched_policy": UNMATCHED_POLICY,
        "unmatched_policy_description": UNMATCHED_POLICY_DESCRIPTION,
        "counts": {
            "total_labeled": total,
            "matched": matched_count,
            "unmatched": unmatched_count,
            "hits_at_1": hits_at_1,
            "hits_at_3": hits_at_3,
            "hits_at_5": hits_at_5,
            "misses_matched": matched_count - hits_at_5,
            "empty_candidate_queries": empty_cand_count,
        },
        "rank_distribution": rank_dist,
        "per_query": per_query,
    }


def mrr_at_5(
    ground_truth: Mapping[QueryKey, str | None] | Sequence[LabelRecord],
    ranked_candidates: Mapping[QueryKey, Sequence[str | None]] | Sequence[RankedPrediction],
    *,
    strict: bool = True,
) -> float:
    """Official MRR@5 contract: mrr_at_5(ground_truth, ranked_candidates) -> float."""
    return float(
        compute_ranking_metrics(ground_truth, ranked_candidates, k=5, strict=strict)["mrr_at_5"]
    )


def score_mrr_at_5(
    scores: Sequence[ScoreRecord],
    targets: Mapping[PairKey, int],
    *,
    queries: Sequence[QueryKey] | set[QueryKey] | None = None,
) -> float:
    """TV5 evaluator adapter compatible with TV4's `fit_model(..., evaluator=score_mrr_at_5)`."""
    by_query_scores: dict[QueryKey, list[ScoreRecord]] = {}
    for rec in scores:
        if not isinstance(rec, ScoreRecord):
            raise ContractError(f"Expected ScoreRecord, got {type(rec).__name__}")
        by_query_scores.setdefault((rec.arxiv_id, rec.citation_key), []).append(rec)

    expected_by_query: dict[QueryKey, set[str]] = {}
    gold_by_query: dict[QueryKey, str | None] = {}
    for pair_key, y in targets.items():
        if y not in (0, 1) or type(y) is bool:
            raise ContractError(f"Target must be 0 or 1 (not bool) for {pair_key}")
        qkey = _canon_query_key((pair_key[0], pair_key[1]))
        ref_id = canonical_id(pair_key[2])
        expected_by_query.setdefault(qkey, set()).add(ref_id)
        gold_by_query.setdefault(qkey, None)
        if y == 1:
            if gold_by_query[qkey] is not None and gold_by_query[qkey] != ref_id:
                raise ContractError(f"Multiple positive targets for query {qkey}")
            gold_by_query[qkey] = ref_id

    for raw_q in queries or ():
        qkey = _canon_query_key(raw_q)
        gold_by_query.setdefault(qkey, None)
        expected_by_query.setdefault(qkey, set())

    if not gold_by_query:
        raise ContractError("MRR@5 requires at least one labeled query")

    ranked_map: dict[QueryKey, list[str]] = {}
    for qkey in sorted(gold_by_query):
        q_scores = by_query_scores.get(qkey, [])
        expected = expected_by_query[qkey]
        if expected:
            ranked_map[qkey] = rank_candidates_from_scores(
                q_scores, expected_candidates=expected, query=qkey, top_k=5
            )
        elif q_scores:
            ranked_map[qkey] = rank_candidates_from_scores(q_scores, query=qkey, top_k=5)
        else:
            ranked_map[qkey] = []

    return mrr_at_5(gold_by_query, ranked_map, strict=True)
