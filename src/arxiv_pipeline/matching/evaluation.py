"""Independent evaluation API: TV4 model vs TV2 baseline, slices & errors (Milestone 3 · TV5).

Implements docs/MS3_CONTRACTS.md section 6:
- `evaluate(predictions, labels, partition_manifest, *, partition) -> EvaluationReport`
- `evaluate_scores(bundles, scores, labels, partition_manifest, *, baseline_scores=...) -> dict`

Strict anti-leakage & convention guarantees:
- Evaluates strictly on the requested partition (never mixes train/valid/test).
- Uses only accepted supervised labels (`LabelRecord.is_supervised`) for MRR@5;
  never infers `unmatched` from null `groundtruth_arxiv_id` in `predictions`.
- Documents the official unmatched convention explicitly in every report.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
import statistics
from typing import Any, Mapping, Sequence

from arxiv_pipeline.common.schemas import MATCHING_SCHEMA_VERSION, ErrorInfo, StageResult
from arxiv_pipeline.common.utils import atomic_json, utc_now
from arxiv_pipeline.matching.contracts import (
    ContractError,
    LabelRecord,
    PARTITIONS,
    PartitionManifest,
    PublicationBundle,
    QueryKey,
    RankedPrediction,
    ScoreRecord,
)
from arxiv_pipeline.matching.metrics import (
    UNMATCHED_POLICY,
    UNMATCHED_POLICY_DESCRIPTION,
    compute_ranking_metrics,
)
from arxiv_pipeline.matching.prediction import build_predictions_from_bundles


@dataclass(frozen=True)
class EvaluationReport:
    """Structured JSON-serializable evaluation output for one model on one partition."""

    partition: str
    split_id: str
    model_name: str
    metrics: dict[str, Any]
    slices: dict[str, Any]
    confusion_and_errors: dict[str, Any]
    prediction_preview: list[dict[str, Any]]
    coverage: dict[str, int]
    unmatched_policy: str = UNMATCHED_POLICY
    unmatched_policy_description: str = UNMATCHED_POLICY_DESCRIPTION
    provenance: dict[str, Any] = field(default_factory=dict)
    schema_version: str = MATCHING_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _cand_bucket(n: int) -> str:
    if n == 0:
        return "0"
    if n <= 5:
        return "1-5"
    if n <= 20:
        return "6-20"
    return "21+"


def _norm_simple(text: str | None) -> str:
    if not text:
        return ""
    return " ".join(text.lower().split())


def _title_sim(a: str | None, b: str | None) -> float:
    na, nb = _norm_simple(a), _norm_simple(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def _summarize_slice_items(items: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(items)
    if n == 0:
        return {
            "n_queries": 0,
            "n_matched": 0,
            "n_unmatched": 0,
            "mrr_at_5": None,
            "mrr_at_5_matched_only": None,
            "recall_at_1": None,
            "recall_at_5": None,
        }
    matched = [x for x in items if x["decision"] == "matched"]
    unmatched = n - len(matched)
    mrr_all = sum(x["reciprocal_rank"] for x in items) / n
    mrr_matched = (
        sum(x["reciprocal_rank"] for x in matched) / len(matched) if matched else None
    )
    r1 = sum(1 for x in matched if x["rank"] == 1) / len(matched) if matched else None
    r5 = (
        sum(1 for x in matched if x["rank"] is not None and x["rank"] <= 5) / len(matched)
        if matched
        else None
    )
    return {
        "n_queries": n,
        "n_matched": len(matched),
        "n_unmatched": unmatched,
        "mrr_at_5": mrr_all,
        "mrr_at_5_matched_only": mrr_matched,
        "recall_at_1": r1,
        "recall_at_5": r5,
    }


def _build_slices(
    per_query: list[dict[str, Any]],
    pool_by_pub: Mapping[str, str],
    label_by_key: Mapping[QueryKey, LabelRecord],
    bundle_by_pub: Mapping[str, PublicationBundle],
) -> dict[str, Any]:
    by_pool: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_decision: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_cand_bucket: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_pub: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_metadata: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_entry_type: dict[str, list[dict[str, Any]]] = defaultdict(list)

    bib_lookup: dict[QueryKey, Any] = {}
    ref_count_by_pub: dict[str, int] = {}
    for pid, bundle in bundle_by_pub.items():
        ref_count_by_pub[pid] = len(bundle.references)
        for bib in bundle.bibtex:
            bib_lookup[(pid, bib.citation_key)] = bib

    for item in per_query:
        qkey = (item["arxiv_id"], item["citation_key"])
        pid = item["arxiv_id"]
        by_pool[pool_by_pub.get(pid, "unknown")].append(item)
        lbl = label_by_key.get(qkey)
        if lbl is not None:
            by_source[lbl.label_source].append(item)
        by_decision[item["decision"]].append(item)
        n_cands = ref_count_by_pub.get(pid, item["n_candidates"])
        by_cand_bucket[_cand_bucket(n_cands)].append(item)
        by_pub[pid].append(item)

        bib = bib_lookup.get(qkey)
        if bib is not None:
            by_metadata[f"has_title={bool(bib.title.strip())}"].append(item)
            by_metadata[f"has_year={bib.year is not None}"].append(item)
            by_metadata[f"has_doi={bool(bib.doi)}"].append(item)
            by_metadata[f"has_authors={bool(bib.authors)}"].append(item)
            by_entry_type[bib.entry_type].append(item)

    per_pub_mrrs = {
        pid: sum(x["reciprocal_rank"] for x in items) / len(items)
        for pid, items in sorted(by_pub.items())
    }
    mrr_values = list(per_pub_mrrs.values())
    pub_summary = {
        "n_publications": len(per_pub_mrrs),
        "macro_mean_mrr_at_5": statistics.mean(mrr_values) if mrr_values else None,
        "median_mrr_at_5": statistics.median(mrr_values) if mrr_values else None,
        "min_mrr_at_5": min(mrr_values) if mrr_values else None,
        "max_mrr_at_5": max(mrr_values) if mrr_values else None,
        "per_publication": {
            pid: _summarize_slice_items(items) for pid, items in sorted(by_pub.items())
        },
    }

    slices_out: dict[str, Any] = {
        "by_pool": {k: _summarize_slice_items(v) for k, v in sorted(by_pool.items())},
        "by_label_source": {k: _summarize_slice_items(v) for k, v in sorted(by_source.items())},
        "by_decision": {k: _summarize_slice_items(v) for k, v in sorted(by_decision.items())},
        "by_candidate_count": {
            k: _summarize_slice_items(v) for k, v in sorted(by_cand_bucket.items())
        },
        "by_publication": pub_summary,
    }
    if by_metadata:
        slices_out["by_metadata"] = {
            k: _summarize_slice_items(v) for k, v in sorted(by_metadata.items())
        }
    if by_entry_type:
        slices_out["by_entry_type"] = {
            k: _summarize_slice_items(v) for k, v in sorted(by_entry_type.items())
        }
    return slices_out


def _build_confusion_and_errors(
    per_query: list[dict[str, Any]],
    pred_by_key: Mapping[QueryKey, RankedPrediction],
    pool_by_pub: Mapping[str, str],
    bundle_by_pub: Mapping[str, PublicationBundle],
    scores_by_pair: Mapping[tuple[str, str, str], float],
    *,
    error_limit: int = 50,
) -> dict[str, Any]:
    bib_lookup: dict[QueryKey, Any] = {}
    ref_lookup: dict[tuple[str, str], Any] = {}
    for pid, bundle in bundle_by_pub.items():
        for bib in bundle.bibtex:
            bib_lookup[(pid, bib.citation_key)] = bib
        for ref in bundle.references:
            ref_lookup[(pid, ref.ref_arxiv_id)] = ref

    top1_confusion = {
        "matched_top1_hit": 0,
        "matched_ranked_2_to_5": 0,
        "matched_missed_outside_top_5": 0,
        "unmatched_with_candidates": 0,
        "unmatched_empty_candidates": 0,
    }
    error_taxonomy: Counter[str] = Counter()
    error_examples: list[dict[str, Any]] = []

    for item in per_query:
        qkey = (item["arxiv_id"], item["citation_key"])
        pid = item["arxiv_id"]
        pred = pred_by_key[qkey]
        decision = item["decision"]
        rank = item["rank"]
        gt = item["groundtruth_arxiv_id"]

        if decision == "unmatched":
            if pred.cand_1 is None:
                top1_confusion["unmatched_empty_candidates"] += 1
            else:
                top1_confusion["unmatched_with_candidates"] += 1
            continue

        if rank == 1:
            top1_confusion["matched_top1_hit"] += 1
            continue

        if rank is not None and 2 <= rank <= 5:
            top1_confusion["matched_ranked_2_to_5"] += 1
        else:
            top1_confusion["matched_missed_outside_top_5"] += 1

        bib = bib_lookup.get(qkey)
        top1_ref = ref_lookup.get((pid, pred.cand_1)) if pred.cand_1 else None
        gold_ref = ref_lookup.get((pid, gt)) if gt else None
        top1_score = scores_by_pair.get((pid, qkey[1], pred.cand_1)) if pred.cand_1 else None
        gold_score = scores_by_pair.get((pid, qkey[1], gt)) if gt else None

        if pred.cand_1 is None:
            tag = "empty_candidates"
        elif top1_score is not None and gold_score is not None and top1_score == gold_score:
            tag = "score_tie_lost"
        elif bib is not None and not bib.title.strip():
            tag = "missing_bibtex_title"
        elif gold_ref is not None and not gold_ref.ref_title.strip():
            tag = "missing_gold_ref_title"
        elif (
            bib is not None
            and top1_ref is not None
            and _title_sim(bib.title, top1_ref.ref_title) >= 0.75
        ):
            tag = "near_duplicate_title"
        elif rank is not None and 2 <= rank <= 5:
            tag = "ranked_2_to_5_other"
        else:
            tag = "ranked_outside_top_5"

        error_taxonomy[tag] += 1
        if len(error_examples) < error_limit:
            example: dict[str, Any] = {
                "arxiv_id": pid,
                "citation_key": qkey[1],
                "pool": pool_by_pub.get(pid),
                "groundtruth_arxiv_id": gt,
                "gold_rank": rank,
                "top1_pred": pred.cand_1,
                "top_5": item["top_5"],
                "error_type": tag,
            }
            if top1_score is not None:
                example["top1_score"] = top1_score
            if gold_score is not None:
                example["gold_score"] = gold_score
            if bib is not None:
                example["bib_title"] = bib.title
            if top1_ref is not None:
                example["top1_ref_title"] = top1_ref.ref_title
            if gold_ref is not None:
                example["gold_ref_title"] = gold_ref.ref_title
            error_examples.append(example)

    return {
        "top1_confusion": top1_confusion,
        "n_matched_errors": (
            top1_confusion["matched_ranked_2_to_5"]
            + top1_confusion["matched_missed_outside_top_5"]
        ),
        "n_matched_misses_outside_top_5": top1_confusion["matched_missed_outside_top_5"],
        "error_taxonomy": dict(sorted(error_taxonomy.items())),
        "error_examples": error_examples,
    }


def _build_prediction_preview(
    partition_preds: list[RankedPrediction],
    per_query_by_key: Mapping[QueryKey, dict[str, Any]],
    label_by_key: Mapping[QueryKey, LabelRecord],
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Build a deterministic preview of ranked predictions with evaluation status."""
    preview: list[dict[str, Any]] = []
    for pred in sorted(partition_preds, key=lambda r: (r.arxiv_id, r.citation_key))[:limit]:
        eval_row = per_query_by_key.get(pred.key)
        lbl = label_by_key.get(pred.key)
        if eval_row is not None:
            if eval_row["decision"] == "unmatched":
                outcome = "unmatched"
            elif eval_row["rank"] == 1:
                outcome = "hit_rank_1"
            elif eval_row["rank"] is not None:
                outcome = f"hit_rank_{eval_row['rank']}"
            else:
                outcome = "miss_outside_top_5"
            rr = eval_row["reciprocal_rank"]
        elif lbl is not None:
            outcome = f"excluded_{lbl.decision}_{lbl.review_status}"
            rr = None
        else:
            outcome = "unlabeled"
            rr = None
        preview.append(
            {
                **pred.to_dict(),
                "evaluation_outcome": outcome,
                "reciprocal_rank": rr,
            }
        )
    return preview


def evaluate(
    predictions: Sequence[RankedPrediction],
    labels: Sequence[LabelRecord],
    partition_manifest: PartitionManifest,
    *,
    partition: str = "test",
    bundles: Sequence[PublicationBundle] | None = None,
    scores: Sequence[ScoreRecord] | None = None,
    model_name: str = "model",
    preview_limit: int = 20,
    error_limit: int = 50,
    provenance: Mapping[str, Any] | None = None,
) -> EvaluationReport:
    """Evaluate RankedPredictions on a single split partition (default: test)."""
    if partition not in PARTITIONS:
        raise ContractError(f"partition must be one of {PARTITIONS}, got {partition!r}")
    if not isinstance(partition_manifest, PartitionManifest):
        raise ContractError("partition_manifest must be a PartitionManifest")

    part_by_pub = {row.arxiv_id: row.partition for row in partition_manifest.rows}
    pool_by_pub = {row.arxiv_id: row.pool for row in partition_manifest.rows}
    target_pubs = {pid for pid, p in part_by_pub.items() if p == partition}
    if not target_pubs:
        raise ContractError(f"Partition '{partition}' has no publications in PartitionManifest")

    pred_by_key: dict[QueryKey, RankedPrediction] = {}
    for raw in predictions:
        pred = raw if isinstance(raw, RankedPrediction) else RankedPrediction(**dict(raw))
        if pred.arxiv_id not in part_by_pub:
            raise ContractError(f"Prediction publication {pred.arxiv_id} outside PartitionManifest")
        if pred.partition != part_by_pub[pred.arxiv_id]:
            raise ContractError(
                f"Partition mismatch for {pred.arxiv_id}: prediction has {pred.partition!r}, "
                f"manifest has {part_by_pub[pred.arxiv_id]!r}"
            )
        if pred.arxiv_id not in target_pubs:
            continue
        if pred.key in pred_by_key:
            raise ContractError(f"Duplicate prediction for query {pred.key}")
        pred_by_key[pred.key] = pred

    label_by_key: dict[QueryKey, LabelRecord] = {}
    supervised_labels: list[LabelRecord] = []
    for raw in labels:
        lbl = raw if isinstance(raw, LabelRecord) else LabelRecord(**dict(raw))
        if lbl.key in label_by_key:
            raise ContractError(f"Duplicate label for query {lbl.key}")
        if lbl.arxiv_id not in target_pubs:
            continue
        label_by_key[lbl.key] = lbl
        if lbl.is_supervised:
            supervised_labels.append(lbl)

    if not supervised_labels:
        raise ContractError(f"No accepted supervised labels found for partition '{partition}'")

    partition_preds = list(pred_by_key.values())
    raw_metrics = compute_ranking_metrics(supervised_labels, partition_preds, k=5, strict=True)
    per_query = raw_metrics.pop("per_query")
    per_query_by_key = {(x["arxiv_id"], x["citation_key"]): x for x in per_query}

    bundle_by_pub = {b.arxiv_id: b for b in (bundles or ()) if b.arxiv_id in target_pubs}
    scores_by_pair: dict[tuple[str, str, str], float] = {}
    for s in scores or ():
        if s.arxiv_id in target_pubs:
            scores_by_pair[s.key] = float(s.score)

    slices = _build_slices(per_query, pool_by_pub, label_by_key, bundle_by_pub)
    confusion_and_errors = _build_confusion_and_errors(
        per_query,
        pred_by_key,
        pool_by_pub,
        bundle_by_pub,
        scores_by_pair,
        error_limit=error_limit,
    )
    preview = _build_prediction_preview(
        partition_preds, per_query_by_key, label_by_key, limit=preview_limit
    )

    coverage = {
        "partition_publications": len(target_pubs),
        "predicted_queries": len(partition_preds),
        "total_labels_in_partition": len(label_by_key),
        "supervised_evaluated_queries": len(supervised_labels),
        "unlabeled_or_excluded_queries": len(partition_preds) - len(supervised_labels),
    }

    full_prov = {
        "population_snapshot_id": partition_manifest.population_snapshot_id,
        "label_snapshot_id": partition_manifest.label_snapshot_id,
        "seed": partition_manifest.seed,
        **(dict(provenance) if provenance else {}),
    }

    return EvaluationReport(
        partition=partition,
        split_id=partition_manifest.split_id,
        model_name=model_name,
        metrics=raw_metrics,
        slices=slices,
        confusion_and_errors=confusion_and_errors,
        prediction_preview=preview,
        coverage=coverage,
        provenance=full_prov,
    )


def _compare_reports(
    model_report: EvaluationReport,
    baseline_report: EvaluationReport,
    model_preds: Sequence[RankedPrediction],
    baseline_preds: Sequence[RankedPrediction],
    labels: Sequence[LabelRecord],
    partition_manifest: PartitionManifest,
    partition: str,
) -> dict[str, Any]:
    """Compute side-by-side deltas between TV4 model and TV2 baseline."""
    target_pubs = {r.arxiv_id for r in partition_manifest.rows if r.partition == partition}
    sup = [l for l in labels if l.arxiv_id in target_pubs and l.is_supervised]
    m_detail = compute_ranking_metrics(
        sup, [p for p in model_preds if p.partition == partition]
    )["per_query"]
    b_detail = compute_ranking_metrics(
        sup, [p for p in baseline_preds if p.partition == partition]
    )["per_query"]

    b_by_q = {(x["arxiv_id"], x["citation_key"]): x for x in b_detail}
    improved, regressed, unchanged = 0, 0, 0
    for m_row in m_detail:
        b_row = b_by_q[(m_row["arxiv_id"], m_row["citation_key"])]
        if m_row["reciprocal_rank"] > b_row["reciprocal_rank"]:
            improved += 1
        elif m_row["reciprocal_rank"] < b_row["reciprocal_rank"]:
            regressed += 1
        else:
            unchanged += 1

    def _diff(a: float | None, b: float | None) -> float | None:
        return None if (a is None or b is None) else a - b

    mm, bm = model_report.metrics, baseline_report.metrics
    pool_deltas = {}
    for pool in sorted(
        set(model_report.slices["by_pool"]) | set(baseline_report.slices["by_pool"])
    ):
        ms = model_report.slices["by_pool"].get(pool, {})
        bs = baseline_report.slices["by_pool"].get(pool, {})
        pool_deltas[pool] = {
            "model_mrr_at_5": ms.get("mrr_at_5"),
            "baseline_mrr_at_5": bs.get("mrr_at_5"),
            "delta_mrr_at_5": _diff(ms.get("mrr_at_5"), bs.get("mrr_at_5")),
        }

    return {
        "model_name": model_report.model_name,
        "baseline_name": baseline_report.model_name,
        "delta_mrr_at_5": _diff(mm["mrr_at_5"], bm["mrr_at_5"]),
        "delta_mrr_at_5_matched_only": _diff(
            mm["mrr_at_5_matched_only"], bm["mrr_at_5_matched_only"]
        ),
        "delta_recall_at_1": _diff(mm["recall_at_1"], bm["recall_at_1"]),
        "delta_recall_at_3": _diff(mm["recall_at_3"], bm["recall_at_3"]),
        "delta_recall_at_5": _diff(mm["recall_at_5"], bm["recall_at_5"]),
        "query_level": {
            "improved": improved,
            "regressed": regressed,
            "unchanged": unchanged,
        },
        "by_pool_delta": pool_deltas,
    }


def evaluate_scores(
    bundles: Sequence[PublicationBundle],
    scores: Sequence[ScoreRecord],
    labels: Sequence[LabelRecord],
    partition_manifest: PartitionManifest,
    *,
    partition: str = "test",
    model_name: str = "tv4_model",
    baseline_scores: Sequence[ScoreRecord] | None = None,
    baseline_name: str = "tv2_baseline",
    preview_limit: int = 20,
    error_limit: int = 50,
    provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """High-level TV5 evaluation API accepting TV4 scores and optional TV2 baseline scores."""
    model_preds = build_predictions_from_bundles(
        bundles, scores, labels, partition_manifest, partitions=(partition,)
    )
    model_report = evaluate(
        model_preds,
        labels,
        partition_manifest,
        partition=partition,
        bundles=bundles,
        scores=scores,
        model_name=model_name,
        preview_limit=preview_limit,
        error_limit=error_limit,
        provenance=provenance,
    )

    result: dict[str, Any] = {
        "schema_version": MATCHING_SCHEMA_VERSION,
        "partition": partition,
        "split_id": partition_manifest.split_id,
        "unmatched_policy": UNMATCHED_POLICY,
        "unmatched_policy_description": UNMATCHED_POLICY_DESCRIPTION,
        "model": model_report.to_dict(),
        "predictions": model_preds,
    }

    if baseline_scores is not None:
        base_preds = build_predictions_from_bundles(
            bundles, baseline_scores, labels, partition_manifest, partitions=(partition,)
        )
        base_report = evaluate(
            base_preds,
            labels,
            partition_manifest,
            partition=partition,
            bundles=bundles,
            scores=baseline_scores,
            model_name=baseline_name,
            preview_limit=preview_limit,
            error_limit=error_limit,
            provenance=provenance,
        )
        result["baseline"] = base_report.to_dict()
        result["baseline_predictions"] = base_preds
        result["comparison"] = _compare_reports(
            model_report,
            base_report,
            model_preds,
            base_preds,
            labels,
            partition_manifest,
            partition,
        )

    return result


def save_evaluation_report(
    report: EvaluationReport | Mapping[str, Any], output_path: str | Path
) -> Path:
    """Persist an EvaluationReport or evaluate_scores payload atomically as UTF-8 JSON."""
    path = Path(output_path)
    if isinstance(report, EvaluationReport):
        payload: Any = report.to_dict()
    else:
        payload = {
            k: ([r.to_dict() for r in v] if k in {"predictions", "baseline_predictions"} else v)
            for k, v in report.items()
        }
    atomic_json(path, payload)
    return path


def run(record, output_dir, *, version=None, upstream=None):
    """Pipeline adapter entrypoint for matching/evaluation (Milestone 3 · TV5)."""
    out_root = Path(output_dir)
    raw_id = record.get("arxiv_id") if isinstance(record, dict) else "run"
    if not isinstance(upstream, dict) or "partition_manifest" not in upstream or "labels" not in upstream:
        return StageResult(
            "evaluation",
            str(raw_id),
            None,
            "skipped",
            utc_now(),
            schema_version=MATCHING_SCHEMA_VERSION,
            error=ErrorInfo(
                "upstream_unavailable",
                "partition_manifest, labels, and predictions or (bundles + scores) are required",
            ),
        )

    try:
        manifest: PartitionManifest = upstream["partition_manifest"]
        labels = [
            l if isinstance(l, LabelRecord) else LabelRecord(**l)
            for l in upstream["labels"]
        ]
        partition = upstream.get("partition", "test")
        model_name = upstream.get("model_name", "tv4_model")
        provenance = upstream.get("provenance")

        if "bundles" in upstream and "scores" in upstream:
            payload = evaluate_scores(
                upstream["bundles"],
                upstream["scores"],
                labels,
                manifest,
                partition=partition,
                model_name=model_name,
                baseline_scores=upstream.get("baseline_scores"),
                baseline_name=upstream.get("baseline_name", "tv2_baseline"),
                provenance=provenance,
            )
            rel = f"metrics/evaluation_{partition}.json"
            save_evaluation_report(
                {k: v for k, v in payload.items() if k not in {"predictions", "baseline_predictions"}},
                out_root / rel,
            )
        elif "predictions" in upstream:
            report = evaluate(
                upstream["predictions"],
                labels,
                manifest,
                partition=partition,
                bundles=upstream.get("bundles"),
                scores=upstream.get("scores"),
                model_name=model_name,
                provenance=provenance,
            )
            rel = f"metrics/evaluation_{partition}.json"
            save_evaluation_report(report, out_root / rel)
        else:
            return StageResult(
                "evaluation",
                str(raw_id),
                None,
                "skipped",
                utc_now(),
                schema_version=MATCHING_SCHEMA_VERSION,
                error=ErrorInfo("upstream_unavailable", "Missing predictions or scores"),
            )

        return StageResult(
            "evaluation",
            str(raw_id),
            None,
            "success",
            utc_now(),
            schema_version=MATCHING_SCHEMA_VERSION,
            artifacts={"evaluation": rel},
            size_bytes=(out_root / rel).stat().st_size,
        )
    except (ContractError, KeyError, TypeError, ValueError) as exc:
        return StageResult(
            "evaluation",
            str(raw_id),
            None,
            "data_error",
            utc_now(),
            schema_version=MATCHING_SCHEMA_VERSION,
            error=ErrorInfo("evaluation_error", str(exc)),
        )
