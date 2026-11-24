"""Top-5 candidate ranking & prediction builder (Milestone 3 · TV5).

Implements docs/MS3_CONTRACTS.md sections 6 and 7:
- `build_predictions(queries, candidates, scores, labels, partition_manifest) -> list[RankedPrediction]`
- Ranks candidate scores descending with stable canonical `ref_arxiv_id` ascending tie-break.
- Outputs `cand_1..cand_5` with trailing `None` when fewer than 5 candidates exist (including 0).
- Sets `groundtruth_arxiv_id` from accepted supervised labels only; `None` for unmatched,
  uncertain, pending, rejected, or unlabeled queries.
- Assigns `partition` strictly from `PartitionManifest`; fails on missing/duplicate/non-finite
  scores or out-of-publication candidates.
"""

from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from arxiv_pipeline.common.schemas import MATCHING_SCHEMA_VERSION, BibtexRow, ErrorInfo, StageResult
from arxiv_pipeline.common.utils import atomic_json, paper_key, utc_now
from arxiv_pipeline.matching.contracts import (
    BibtexInput,
    ContractError,
    LabelRecord,
    PARTITIONS,
    PartitionManifest,
    PartitionRow,
    PublicationBundle,
    QueryKey,
    RankedPrediction,
    ReferenceInput,
    ReferenceKey,
    ScoreRecord,
    canonical_id,
    publication_from_rows,
    validate_predictions,
)
from arxiv_pipeline.matching.metrics import rank_candidates_from_scores


def _extract_queries_and_candidates(
    queries: Sequence[BibtexInput | QueryKey | PublicationBundle],
    candidates: Sequence[ReferenceInput | ReferenceKey] | None,
) -> tuple[dict[str, set[str]], dict[str, set[str]], list[PublicationBundle] | None]:
    """Normalize queries and candidates into ({arxiv_id: {citation_key}}, {arxiv_id: {ref_id}})."""
    if queries and all(isinstance(q, PublicationBundle) for q in queries):
        bundles = list(queries)  # type: ignore[arg-type]
        if candidates is not None:
            raise ContractError("Pass candidates=None when queries is a sequence of PublicationBundle")
        seen_pubs: set[str] = set()
        query_map: dict[str, set[str]] = {}
        cand_map: dict[str, set[str]] = {}
        for b in bundles:
            if b.arxiv_id in seen_pubs:
                raise ContractError(f"Duplicate publication in bundles: {b.arxiv_id}")
            if b.readiness != "ready":
                raise ContractError(
                    f"Publication {b.arxiv_id} is not ready (readiness={b.readiness})"
                )
            seen_pubs.add(b.arxiv_id)
            query_map[b.arxiv_id] = {row.citation_key for row in b.bibtex}
            cand_map[b.arxiv_id] = {ref.ref_arxiv_id for ref in b.references}
        return query_map, cand_map, bundles

    query_map = defaultdict(set)
    seen_queries: set[QueryKey] = set()
    for item in queries:
        if isinstance(item, BibtexRow):
            pid = canonical_id(item.arxiv_id)
            ckey = item.citation_key
        elif isinstance(item, tuple) and len(item) == 2:
            pid = canonical_id(item[0])
            ckey = item[1]
        elif isinstance(item, Mapping):
            pid = canonical_id(item["arxiv_id"])
            ckey = item["citation_key"]
        else:
            raise ContractError(f"Unsupported query entry type: {type(item).__name__}")
        if not isinstance(ckey, str) or not ckey.strip():
            raise ContractError("citation_key must be a nonempty string")
        qkey = (pid, ckey)
        if qkey in seen_queries:
            raise ContractError(f"Duplicate query {qkey}")
        seen_queries.add(qkey)
        query_map[pid].add(ckey)

    cand_map = {pid: set() for pid in query_map}
    seen_refs: set[ReferenceKey] = set()
    if candidates is not None:
        for item in candidates:
            if isinstance(item, ReferenceInput):
                pid, ref_id = item.arxiv_id, item.ref_arxiv_id
            elif isinstance(item, tuple) and len(item) == 2:
                pid, ref_id = canonical_id(item[0]), canonical_id(item[1])
            elif isinstance(item, Mapping):
                pid = canonical_id(item["arxiv_id"])
                if item.get("ref_arxiv_id") is None:
                    continue
                ref_id = canonical_id(item["ref_arxiv_id"])
            else:
                raise ContractError(f"Unsupported candidate entry type: {type(item).__name__}")
            rkey = (pid, ref_id)
            if rkey in seen_refs:
                raise ContractError(f"Duplicate reference candidate {rkey}")
            seen_refs.add(rkey)
            cand_map.setdefault(pid, set()).add(ref_id)

    return dict(query_map), cand_map, None


def build_predictions(
    queries: Sequence[BibtexInput | QueryKey | PublicationBundle],
    candidates: Sequence[ReferenceInput | ReferenceKey] | None,
    scores: Sequence[ScoreRecord],
    labels: Sequence[LabelRecord],
    partition_manifest: PartitionManifest,
    *,
    partitions: Iterable[str] | None = None,
) -> list[RankedPrediction]:
    """Rank candidate scores into schema-valid Top-5 RankedPrediction rows.

    Order-independent: output is sorted by canonical (arxiv_id, citation_key).
    Only publications belonging to `partition_manifest` (matching `partitions`)
    produce rows, and every BibTeX query of those publications survives—including
    publications with zero arXiv references (`cand_1..cand_5 = None`).
    """
    if not isinstance(partition_manifest, PartitionManifest):
        raise ContractError("partition_manifest must be a PartitionManifest")

    target_partitions = set(PARTITIONS) if partitions is None else set(partitions)
    invalid_parts = target_partitions - set(PARTITIONS)
    if invalid_parts:
        raise ContractError(f"Invalid partition filter: {sorted(invalid_parts)}")

    query_map, cand_map, bundles = _extract_queries_and_candidates(queries, candidates)

    part_by_pub = {
        row.arxiv_id: row.partition
        for row in partition_manifest.rows
        if row.partition in target_partitions
    }
    if not part_by_pub:
        raise ContractError("No publications in partition_manifest match requested partitions")

    missing_pubs = sorted(set(part_by_pub) - set(query_map))
    if missing_pubs:
        raise ContractError(
            f"Split publications missing from queries input: {missing_pubs[:5]}"
        )

    all_target_queries: set[QueryKey] = {
        (pid, ckey) for pid in part_by_pub for ckey in query_map[pid]
    }
    seen_labels: set[QueryKey] = set()
    supervised_truth: dict[QueryKey, str | None] = {}
    for item in labels:
        lbl = item if isinstance(item, LabelRecord) else LabelRecord(**dict(item))
        if lbl.key in seen_labels:
            raise ContractError(f"Duplicate label for query {lbl.key}")
        seen_labels.add(lbl.key)
        if lbl.arxiv_id not in part_by_pub:
            continue
        if lbl.key not in all_target_queries:
            raise ContractError(f"Label references unknown query {lbl.key}")
        if (
            lbl.groundtruth_arxiv_id is not None
            and lbl.groundtruth_arxiv_id not in cand_map.get(lbl.arxiv_id, set())
        ):
            raise ContractError(
                f"Label groundtruth {lbl.groundtruth_arxiv_id} is outside candidates for {lbl.key}"
            )
        if lbl.is_supervised:
            supervised_truth[lbl.key] = lbl.groundtruth_arxiv_id

    scores_by_query: dict[QueryKey, list[ScoreRecord]] = defaultdict(list)
    seen_score_keys: set[tuple[str, str, str]] = set()
    for raw_score in scores:
        rec = raw_score if isinstance(raw_score, ScoreRecord) else ScoreRecord(**dict(raw_score))
        if rec.key in seen_score_keys:
            raise ContractError(f"Duplicate score for pair {rec.key}")
        seen_score_keys.add(rec.key)
        if rec.arxiv_id not in part_by_pub:
            if partitions is not None:
                continue
            raise ContractError(f"Score for publication outside partition manifest: {rec.arxiv_id}")
        qkey = (rec.arxiv_id, rec.citation_key)
        if qkey not in all_target_queries:
            raise ContractError(f"Score references unknown query {qkey}")
        if rec.ref_arxiv_id not in cand_map.get(rec.arxiv_id, set()):
            raise ContractError(
                f"Score candidate {rec.ref_arxiv_id} does not belong to publication {rec.arxiv_id}"
            )
        scores_by_query[qkey].append(rec)

    predictions: list[RankedPrediction] = []
    for pid in sorted(part_by_pub):
        partition = part_by_pub[pid]
        expected_cands = cand_map.get(pid, set())
        for ckey in sorted(query_map[pid]):
            qkey = (pid, ckey)
            if expected_cands:
                top_ids = rank_candidates_from_scores(
                    scores_by_query.get(qkey, []),
                    expected_candidates=expected_cands,
                    query=qkey,
                    top_k=5,
                )
            else:
                if scores_by_query.get(qkey):
                    raise ContractError(
                        f"Unexpected candidate score for zero-reference query {qkey}"
                    )
                top_ids = []

            padded: list[str | None] = list(top_ids) + [None] * (5 - len(top_ids))
            predictions.append(
                RankedPrediction(
                    arxiv_id=pid,
                    partition=partition,
                    citation_key=ckey,
                    groundtruth_arxiv_id=supervised_truth.get(qkey),
                    cand_1=padded[0],
                    cand_2=padded[1],
                    cand_3=padded[2],
                    cand_4=padded[3],
                    cand_5=padded[4],
                )
            )

    if bundles is not None and target_partitions == set(PARTITIONS):
        report = validate_predictions(
            [b for b in bundles if b.arxiv_id in part_by_pub],
            predictions,
            list(labels),
            partition_manifest,
        )
        if not report.valid:
            codes = [e.code for e in report.errors]
            raise ContractError(f"Built predictions failed contract validation: {codes}")

    return predictions


def build_predictions_from_bundles(
    bundles: Sequence[PublicationBundle],
    scores: Sequence[ScoreRecord],
    labels: Sequence[LabelRecord],
    partition_manifest: PartitionManifest,
    *,
    partitions: Iterable[str] | None = None,
) -> list[RankedPrediction]:
    """Convenience wrapper building Top-5 predictions directly from PublicationBundles."""
    return build_predictions(
        bundles,
        None,
        scores,
        labels,
        partition_manifest,
        partitions=partitions,
    )


def save_predictions_json(
    predictions: Sequence[RankedPrediction], output_path: str | Path
) -> Path:
    """Persist RankedPrediction rows atomically as UTF-8 JSON."""
    path = Path(output_path)
    atomic_json(path, [row.to_dict() for row in predictions])
    return path


def load_predictions_json(input_path: str | Path) -> list[RankedPrediction]:
    """Load and validate RankedPrediction rows from JSON."""
    raw = json.loads(Path(input_path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ContractError("Predictions JSON must be a list of objects")
    return [RankedPrediction(**item) for item in raw]


def run(record, output_dir, *, version=None, upstream=None):
    """Pipeline adapter entrypoint for matching/prediction (Milestone 3 · TV5)."""
    out_root = Path(output_dir)
    raw_id = record.get("arxiv_id") if isinstance(record, dict) else None
    if not raw_id:
        return StageResult(
            "prediction",
            "",
            None,
            "data_error",
            utc_now(),
            schema_version=MATCHING_SCHEMA_VERSION,
            error=ErrorInfo("invalid_input", "Missing arxiv_id in record"),
        )
    try:
        paper = canonical_id(raw_id)
    except ContractError as exc:
        return StageResult(
            "prediction",
            str(raw_id),
            None,
            "data_error",
            utc_now(),
            schema_version=MATCHING_SCHEMA_VERSION,
            error=ErrorInfo("invalid_input", str(exc)),
        )

    if not isinstance(upstream, dict) or "bundles" not in upstream or "scores" not in upstream:
        return StageResult(
            "prediction",
            paper,
            None,
            "skipped",
            utc_now(),
            schema_version=MATCHING_SCHEMA_VERSION,
            error=ErrorInfo("upstream_unavailable", "bundles, scores, and partition_manifest are required"),
        )

    try:
        bundles: list[PublicationBundle] = []
        for item in upstream["bundles"]:
            if isinstance(item, PublicationBundle):
                bundles.append(item)
            elif isinstance(item, dict):
                bundles.append(
                    publication_from_rows(
                        item["arxiv_id"],
                        item.get("bibtex", []),
                        item.get("references", []),
                        readiness=item.get("readiness", "ready"),
                    )
                )
            else:
                raise ContractError("Invalid bundle item in upstream")

        target_bundles = [b for b in bundles if b.arxiv_id == paper]
        if len(target_bundles) != 1:
            raise ContractError(f"Expected exactly one PublicationBundle for {paper}")

        scores = [
            s if isinstance(s, ScoreRecord) else ScoreRecord(**s)
            for s in upstream["scores"]
            if canonical_id(s.arxiv_id if isinstance(s, ScoreRecord) else s["arxiv_id"]) == paper
        ]
        labels = [
            l if isinstance(l, LabelRecord) else LabelRecord(**l)
            for l in upstream.get("labels", [])
            if canonical_id(l.arxiv_id if isinstance(l, LabelRecord) else l["arxiv_id"]) == paper
        ]
        manifest = upstream.get("partition_manifest")
        if manifest is None:
            partition = upstream.get("partition", "test")
            pool = upstream.get("pool", "manual")
            manifest = PartitionManifest(
                seed=0,
                group_size=1,
                population_snapshot_id="single-paper",
                label_snapshot_id="single-paper",
                manual_publication_ids=[paper] if pool == "manual" else [],
                auto_publication_ids=[paper] if pool == "auto" else [],
                rows=[PartitionRow(paper, partition, pool)],
            )

        rows = build_predictions_from_bundles(target_bundles, scores, labels, manifest)
        rel = f"matching/{paper_key(paper)}/predictions.json"
        save_predictions_json(rows, out_root / rel)
        return StageResult(
            "prediction",
            paper,
            None,
            "success",
            utc_now(),
            schema_version=MATCHING_SCHEMA_VERSION,
            artifacts={"predictions": rel},
            size_bytes=(out_root / rel).stat().st_size,
        )
    except (ContractError, KeyError, TypeError, ValueError) as exc:
        return StageResult(
            "prediction",
            paper,
            None,
            "data_error",
            utc_now(),
            schema_version=MATCHING_SCHEMA_VERSION,
            error=ErrorInfo("prediction_error", str(exc)),
        )
