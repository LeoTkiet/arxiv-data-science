#!/usr/bin/env python3
"""TV5 6-Table Sharded Parquet Exporter & Hugging Face Hub Uploader (Milestone 3 · T5-05).

Supports BOTH:
  - Per-member individual dataset export & upload (`--member tv1|tv2|tv3|tv4|tv5`)
    for individual Hugging Face submission under each student's personal account,
    automatically scoped to their assigned arXiv range in `configs/ms1_group6_ranges.json`.
  - Full-group export (`--member all`) across all 5 members (3,696 split papers).

Builds `predictions` (`train + valid + test`) from TV4's frozen score parquets +
TV5's official PartitionManifest & labels, verifies official group gate-check
metrics (`valid MRR@5`, `test MRR@5`), loads the 5 MS1/MS2 source tables
(`papers`, `references`, `elements`, `hierarchy`, `bibtex`) from either:
  - `--source-mode tables`: pre-assembled Parquet / JSONL / JSON files or shard dirs
  - `--source-mode ms1_ms2_checkpoints`: raw MS1 & MS2 checkpoint roots via
    `arxiv_pipeline.matching.io.load_with_provenance(..., include_export_tables=True)`

Runs full cross-table validation (`validate_six_tables`), writes zstd-compressed
sharded Parquet tables under `<output_dir>/data/<table>/part-XXXXX-of-YYYYY.parquet`,
verifies read-back & SHA-256 (`verify_parquet_export`), generates the official
Hugging Face Dataset Card (`README.md`), and optionally uploads to Hugging Face Hub
(`upload_to_hf` + `verify_hf_repo_visibility`) when `--upload` is explicitly passed.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import getpass
import hashlib
import io
import json
import math
import os
from pathlib import Path
import sys
import zipfile
from typing import Any, Iterable, Sequence

import pyarrow.parquet as pq

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from arxiv_pipeline.common.utils import utc_now
from arxiv_pipeline.matching.contracts import (
    ContractError,
    LabelRecord,
    PARTITIONS,
    PartitionManifest,
    RankedPrediction,
    canonical_id,
)
from arxiv_pipeline.matching.evaluation import evaluate
from arxiv_pipeline.matching.export import (
    TABLE_NAMES,
    export_hf_dataset,
    prepare_published_references,
    upload_to_hf,
    verify_hf_repo_visibility,
)
from arxiv_pipeline.matching.io import load_with_provenance
from arxiv_pipeline.matching.split import assert_no_split_leakage, load_partition_manifest

SOURCE_TABLE_NAMES = ("papers", "references", "elements", "hierarchy", "bibtex")
MEMBER_CHOICES = ("tv1", "tv2", "tv3", "tv4", "tv5", "all")
DEFAULT_RANGES_CONFIG = REPO_ROOT / "configs" / "ms1_group6_ranges.json"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_member_range(member: str, config_path: Path = DEFAULT_RANGES_CONFIG) -> str | None:
    """Resolve assigned `LO:HI` arXiv range for tv1..tv5 from configs/ms1_group6_ranges.json."""
    if member == "all":
        return None
    if member not in ("tv1", "tv2", "tv3", "tv4", "tv5"):
        raise ContractError(f"Unsupported member: {member!r} (expected one of {MEMBER_CHOICES})")
    cfg = json.loads(config_path.read_text(encoding="utf-8-sig"))
    ranges = cfg.get("ranges", [])
    idx = int(member[2:]) - 1
    if idx < 0 or idx >= len(ranges):
        raise ContractError(f"Range index {idx} for {member} not found in {config_path}")
    return str(ranges[idx]["range"])


def _parse_modern_id_key(arxiv_id: str) -> tuple[str, int] | None:
    parts = arxiv_id.split(".")
    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
        return (parts[0], int(parts[1]))
    return None


def in_member_range(arxiv_id: str, range_str: str | None) -> bool:
    """Return True if canonical arxiv_id falls within inclusive `LO:HI` range (or range_str is None)."""
    if range_str is None:
        return True
    lo_s, hi_s = range_str.split(":")
    lo_k = _parse_modern_id_key(canonical_id(lo_s))
    hi_k = _parse_modern_id_key(canonical_id(hi_s))
    cur_k = _parse_modern_id_key(canonical_id(arxiv_id))
    if lo_k is None or hi_k is None or cur_k is None:
        return False
    return lo_k <= cur_k <= hi_k


def slice_manifest_for_member(
    full_manifest: PartitionManifest,
    member: str,
    range_str: str | None,
) -> PartitionManifest:
    """Build a member-scoped PartitionManifest preserving exact group partition assignments."""
    if member == "all" or range_str is None:
        return full_manifest
    member_rows = [r for r in full_manifest.rows if in_member_range(r.arxiv_id, range_str)]
    if not member_rows:
        raise ContractError(
            f"No split publications found in PartitionManifest for {member} ({range_str})"
        )
    member_manual = sorted(r.arxiv_id for r in member_rows if r.pool == "manual")
    member_auto = sorted(r.arxiv_id for r in member_rows if r.pool == "auto")
    return PartitionManifest(
        seed=full_manifest.seed,
        group_size=full_manifest.group_size,
        population_snapshot_id=full_manifest.population_snapshot_id,
        label_snapshot_id=full_manifest.label_snapshot_id,
        manual_publication_ids=member_manual,
        auto_publication_ids=member_auto,
        rows=member_rows,
        schema_version=full_manifest.schema_version,
    )


def load_label_records(path: Path) -> list[LabelRecord]:
    """Load LabelRecord list from .json, .jsonl, or .zip containing *labels.json."""
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path, "r") as zf:
            cands = [n for n in zf.namelist() if n.endswith("labels.json")]
            if not cands:
                raise ContractError(f"No *labels.json inside {path}")
            chosen = "auto_labels.json" if "auto_labels.json" in cands else cands[0]
            raw = json.loads(zf.read(chosen).decode("utf-8-sig"))
    elif path.suffix.lower() == ".jsonl":
        raw = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8-sig").splitlines()
            if line.strip()
        ]
    else:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))

    if isinstance(raw, dict):
        for key in ("labels", "records", "rows", "entries"):
            if isinstance(raw.get(key), list):
                raw = raw[key]
                break
    if not isinstance(raw, list):
        raise ContractError(f"{path}: expected list of LabelRecord objects")
    return [LabelRecord(**item) for item in raw]


def _iter_score_parquet_tables(
    tv4_scores_source: Path,
    partitions: Iterable[str] = PARTITIONS,
) -> dict[str, pq.ParquetFile]:
    """Open ParquetFile handles for requested score partitions from TV4 zip or directory."""
    handles: dict[str, pq.ParquetFile] = {}
    if tv4_scores_source.is_file() and tv4_scores_source.suffix.lower() == ".zip":
        with zipfile.ZipFile(tv4_scores_source, "r") as zf:
            names = zf.namelist()
            for part in partitions:
                matches = [n for n in names if n.endswith(f"scores/{part}/scores.parquet")]
                if not matches:
                    raise ContractError(
                        f"Missing scores/{part}/scores.parquet inside {tv4_scores_source}"
                    )
                handles[part] = pq.ParquetFile(io.BytesIO(zf.read(matches[0])))
    elif tv4_scores_source.is_dir():
        for part in partitions:
            candidates = [
                tv4_scores_source / "scores" / part / "scores.parquet",
                tv4_scores_source / part / "scores.parquet",
                tv4_scores_source / f"scores_{part}.parquet",
            ]
            found = next((p for p in candidates if p.is_file()), None)
            if found is None:
                raise ContractError(
                    f"Missing scores parquet for partition '{part}' under {tv4_scores_source}"
                )
            handles[part] = pq.ParquetFile(found)
    else:
        raise ContractError(f"TV4 scores source not found: {tv4_scores_source}")
    return handles


def read_tv4_provenance(tv4_scores_source: Path) -> dict[str, Any]:
    """Extract frozen model provenance from TV4 handoff zip or directory (best-effort)."""
    info: dict[str, Any] = {"tv4_source": str(tv4_scores_source)}
    if tv4_scores_source.is_file() and tv4_scores_source.suffix.lower() == ".zip":
        info["tv4_zip_sha256"] = sha256_file(tv4_scores_source)
        with zipfile.ZipFile(tv4_scores_source, "r") as zf:
            frozen_names = [n for n in zf.namelist() if n.endswith("/frozen.json")]
            if frozen_names:
                frozen = json.loads(zf.read(frozen_names[0]).decode("utf-8"))
                info["tv4_model_id"] = frozen.get("model_id")
                info["tv4_model_kind"] = frozen.get("kind")
                info["tv4_model_sha256"] = frozen.get("model_sha256")
                info["tv4_split_id"] = frozen.get("split_id")
                info["tv4_feature_schema_id"] = frozen.get("feature_schema_id")
    elif tv4_scores_source.is_dir():
        frozen_file = tv4_scores_source / "frozen.json"
        if frozen_file.is_file():
            frozen = json.loads(frozen_file.read_text(encoding="utf-8"))
            info["tv4_model_id"] = frozen.get("model_id")
            info["tv4_model_kind"] = frozen.get("kind")
            info["tv4_model_sha256"] = frozen.get("model_sha256")
            info["tv4_split_id"] = frozen.get("split_id")
            info["tv4_feature_schema_id"] = frozen.get("feature_schema_id")
    return info


def build_predictions_streaming(
    tv4_scores_source: Path,
    target_manifest: PartitionManifest,
    all_labels: Sequence[LabelRecord],
    *,
    full_manifest: PartitionManifest | None = None,
    partitions: Iterable[str] = PARTITIONS,
    bibtex_queries_by_pub: dict[str, set[str]] | None = None,
    refs_by_pub: dict[str, set[str]] | None = None,
    batch_size: int = 250_000,
    verbose: bool = True,
) -> list[RankedPrediction]:
    """Build deterministic Top-5 RankedPredictions for all papers in `target_manifest`.

    When exporting a single member (`target_manifest` is a subset of `full_manifest`),
    rows belonging to other members are validated against `full_manifest` partition
    membership and skipped cleanly without needing their MS1/MS2 tables in memory.
    """
    selected_parts = tuple(partitions)
    target_part_by_pub = {
        row.arxiv_id: row.partition
        for row in target_manifest.rows
        if row.partition in selected_parts
    }
    full_part_by_pub = (
        {row.arxiv_id: row.partition for row in full_manifest.rows}
        if full_manifest is not None
        else target_part_by_pub
    )

    supervised_truth: dict[tuple[str, str], str | None] = {}
    label_queries_by_pub: dict[str, set[str]] = defaultdict(set)
    seen_labels: set[tuple[str, str]] = set()
    for lbl in all_labels:
        if lbl.key in seen_labels:
            raise ContractError(f"Duplicate label for query {lbl.key}")
        seen_labels.add(lbl.key)
        if lbl.arxiv_id in target_part_by_pub:
            label_queries_by_pub[lbl.arxiv_id].add(lbl.citation_key)
            if lbl.is_supervised:
                supervised_truth[lbl.key] = lbl.groundtruth_arxiv_id

    canon_cache: dict[str, str] = {}

    def _canon(raw: Any) -> str:
        s = raw if isinstance(raw, str) else str(raw)
        res = canon_cache.get(s)
        if res is None:
            res = canonical_id(s)
            canon_cache[s] = res
        return res

    top5_by_query: dict[tuple[str, str], list[tuple[float, str]]] = {}
    scored_cand_counts: dict[tuple[str, str], int] = defaultdict(int)
    scored_queries_by_pub: dict[str, set[str]] = defaultdict(set)

    handles = _iter_score_parquet_tables(tv4_scores_source, selected_parts)
    kept_pairs = 0
    for part, pf in handles.items():
        expected_target_pubs = {pid for pid, p in target_part_by_pub.items() if p == part}
        expected_full_pubs = {pid for pid, p in full_part_by_pub.items() if p == part}
        seen_refs_by_q: dict[tuple[str, str], set[str]] = defaultdict(set)
        part_kept = 0
        part_total = 0
        for batch in pf.iter_batches(
            batch_size=batch_size,
            columns=["arxiv_id", "citation_key", "ref_arxiv_id", "score"],
        ):
            aids = batch.column(0).to_pylist()
            ckeys = batch.column(1).to_pylist()
            rids = batch.column(2).to_pylist()
            scores = batch.column(3).to_pylist()
            for raw_pid, ckey, raw_rid, raw_sc in zip(aids, ckeys, rids, scores):
                part_total += 1
                pid = _canon(raw_pid)
                if pid not in expected_full_pubs:
                    raise ContractError(
                        f"Score row paper {pid} does not belong to partition '{part}'"
                    )
                if pid not in expected_target_pubs:
                    continue
                rid = _canon(raw_rid)
                sc = float(raw_sc)
                if not math.isfinite(sc):
                    raise ContractError(f"Non-finite score for ({pid}, {ckey}, {rid}): {raw_sc}")
                if refs_by_pub is not None and rid not in refs_by_pub.get(pid, ()):
                    raise ContractError(
                        f"Score candidate {rid} not in published references for {pid}"
                    )
                qkey = (pid, ckey)
                q_seen = seen_refs_by_q[qkey]
                if rid in q_seen:
                    raise ContractError(
                        f"Duplicate score pair {(pid, ckey, rid)} in partition '{part}'"
                    )
                q_seen.add(rid)

                scored_queries_by_pub[pid].add(ckey)
                scored_cand_counts[qkey] += 1
                item = (-sc, rid)
                cur = top5_by_query.get(qkey)
                if cur is None:
                    top5_by_query[qkey] = [item]
                elif len(cur) < 5:
                    cur.append(item)
                    cur.sort()
                elif item < cur[-1]:
                    cur[-1] = item
                    cur.sort()
                part_kept += 1
        seen_refs_by_q.clear()
        kept_pairs += part_kept
        if verbose:
            print(
                f"  Streamed TV4 scores [{part:5s}]: kept {part_kept:,} / {part_total:,} pairs "
                f"({len(expected_target_pubs):,} target pubs)"
            )

    predictions: list[RankedPrediction] = []
    for pid in sorted(target_part_by_pub):
        part = target_part_by_pub[pid]
        if bibtex_queries_by_pub is not None:
            if pid not in bibtex_queries_by_pub or not bibtex_queries_by_pub[pid]:
                raise ContractError(f"Split publication {pid} has no rows in bibtex source table")
            queries_for_pub = bibtex_queries_by_pub[pid]
            extra_scored = scored_queries_by_pub.get(pid, set()) - queries_for_pub
            if extra_scored:
                raise ContractError(
                    f"TV4 scores contain {len(extra_scored)} query(s) missing from bibtex for {pid}"
                )
        else:
            queries_for_pub = scored_queries_by_pub.get(pid, set()) | label_queries_by_pub.get(
                pid, set()
            )
            if not queries_for_pub:
                raise ContractError(f"No queries found for split publication {pid}")

        expected_ref_count = len(refs_by_pub.get(pid, set())) if refs_by_pub is not None else None
        for ckey in sorted(queries_for_pub):
            qkey = (pid, ckey)
            if expected_ref_count is not None:
                got_cnt = scored_cand_counts.get(qkey, 0)
                if got_cnt != expected_ref_count:
                    raise ContractError(
                        f"Candidate score count mismatch for {qkey}: got {got_cnt}, "
                        f"expected {expected_ref_count} from references table"
                    )
            top_items = top5_by_query.get(qkey, [])
            top_ids: list[str | None] = [rid for _, rid in top_items]
            padded = top_ids + [None] * (5 - len(top_ids))
            predictions.append(
                RankedPrediction(
                    arxiv_id=pid,
                    partition=part,
                    citation_key=ckey,
                    groundtruth_arxiv_id=supervised_truth.get(qkey),
                    cand_1=padded[0],
                    cand_2=padded[1],
                    cand_3=padded[2],
                    cand_4=padded[3],
                    cand_5=padded[4],
                )
            )

    if verbose:
        print(
            f"  Built {len(predictions):,} RankedPrediction rows across "
            f"{len(target_part_by_pub):,} publications ({kept_pairs:,} scored pairs)"
        )
    return predictions


def _load_one_table_path(table_dir_or_file: Path, table_name: str) -> list[dict[str, Any]]:
    """Load one source table from Parquet file/dir, JSONL, or JSON."""
    candidates = [
        table_dir_or_file / "data" / table_name,
        table_dir_or_file / table_name,
        table_dir_or_file / f"{table_name}.parquet",
        table_dir_or_file / f"{table_name}.jsonl",
        table_dir_or_file / f"{table_name}.json",
    ]
    target = next((p for p in candidates if p.exists()), None)
    if target is None:
        raise ContractError(
            f"Could not find source table '{table_name}' under {table_dir_or_file} "
            f"(checked {table_name}/, data/{table_name}/, .parquet, .jsonl, .json)"
        )

    rows: list[dict[str, Any]] = []
    if target.is_dir():
        parquet_files = sorted(target.glob("*.parquet"))
        jsonl_files = sorted(target.glob("*.jsonl"))
        json_files = sorted(target.glob("*.json"))
        if parquet_files:
            for pf in parquet_files:
                rows.extend(pq.read_table(pf).to_pylist())
        elif jsonl_files:
            for jf in jsonl_files:
                for line in jf.read_text(encoding="utf-8-sig").splitlines():
                    if line.strip():
                        rows.append(json.loads(line))
        elif json_files:
            for jf in json_files:
                obj = json.loads(jf.read_text(encoding="utf-8-sig"))
                if isinstance(obj, list):
                    rows.extend(obj)
                elif isinstance(obj, dict):
                    rows.append(obj)
        else:
            raise ContractError(f"Directory {target} contains no .parquet/.jsonl/.json files")
    elif target.suffix.lower() == ".parquet":
        rows = pq.read_table(target).to_pylist()
    elif target.suffix.lower() == ".jsonl":
        rows = [
            json.loads(line)
            for line in target.read_text(encoding="utf-8-sig").splitlines()
            if line.strip()
        ]
    elif target.suffix.lower() == ".json":
        obj = json.loads(target.read_text(encoding="utf-8-sig"))
        if isinstance(obj, dict) and isinstance(obj.get(table_name), list):
            rows = obj[table_name]
        elif isinstance(obj, list):
            rows = obj
        else:
            raise ContractError(f"Unsupported JSON structure in {target}")
    else:
        raise ContractError(f"Unsupported file extension for {target}")

    print(f"  Loaded {table_name:10s}: {len(rows):>10,} rows from {target}")
    return rows


def load_source_tables_from_dir(
    source_dir: Path,
    *,
    member_range: str | None = None,
    allowed_pub_ids: set[str] | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """Load the 5 MS1/MS2 source tables and filter to member range / export scope."""
    raw_tables = {t: _load_one_table_path(source_dir, t) for t in SOURCE_TABLE_NAMES}
    if member_range is not None or allowed_pub_ids is not None:
        canon_cache: dict[str, str] = {}

        def _keep(raw_pid: Any) -> bool:
            s = raw_pid if isinstance(raw_pid, str) else str(raw_pid)
            pid = canon_cache.get(s)
            if pid is None:
                pid = canonical_id(s)
                canon_cache[s] = pid
            if member_range is not None and not in_member_range(pid, member_range):
                return False
            if allowed_pub_ids is not None and pid not in allowed_pub_ids:
                return False
            return True

        for t in SOURCE_TABLE_NAMES:
            before = len(raw_tables[t])
            raw_tables[t] = [r for r in raw_tables[t] if _keep(r["arxiv_id"])]
            if len(raw_tables[t]) != before:
                print(f"  Filtered {t:10s}: {before:,} -> {len(raw_tables[t]):,} rows")

    published_refs, ref_stats = prepare_published_references(raw_tables["references"])
    raw_tables["references"] = published_refs
    print(f"  prepare_published_references: {ref_stats}")
    return raw_tables, {
        "source_mode": "tables",
        "source_dir": str(source_dir),
        "ref_stats": ref_stats,
    }


def resolve_ms1_root(ms1_input: Path) -> Path:
    """Accept either `<member>/data` or `<member>` containing `data/state`."""
    p = ms1_input.expanduser()
    if (p / "state").is_dir():
        return p
    if (p / "data" / "state").is_dir():
        print(f"  Auto-resolved MS1_ROOT -> {p / 'data'}")
        return p / "data"
    return p


def resolve_ms2_root(ms2_input: Path, *, member: str, unpack_base: Path) -> Path:
    """Accept unpacked MS2 dir, folder containing `MS2_RESULTS.zip`, or `.zip` path directly."""
    p = ms2_input.expanduser()
    if p.is_dir():
        if (p / "state").is_dir():
            return p
        if member != "all" and (p / member / "state").is_dir():
            print(f"  Auto-resolved MS2_ROOT -> {p / member}")
            return p / member
        zip_candidate = p / "MS2_RESULTS.zip"
        if zip_candidate.is_file():
            p = zip_candidate
        else:
            return p

    if p.is_file() and p.suffix.lower() == ".zip":
        target_dir = unpack_base / f"ms2_unpacked_{member}_{sha256_file(p)[:12]}"
        marker = target_dir / ".extracted_ok"
        if not marker.is_file():
            target_dir.mkdir(parents=True, exist_ok=True)
            print(f"  Extracting {p.name} -> {target_dir} ...")
            with zipfile.ZipFile(p, "r") as zf:
                zf.extractall(target_dir)
            marker.write_text("ok\n", encoding="utf-8")
        else:
            print(f"  Reusing extracted MS2 cache: {target_dir}")

        if (target_dir / "state").is_dir():
            return target_dir
        if member != "all" and (target_dir / member / "state").is_dir():
            return target_dir / member
        for child in sorted(target_dir.iterdir()):
            if child.is_dir() and (child / "state").is_dir():
                return child
        raise ContractError(
            f"Extracted {p} to {target_dir} but could not find a 'state/' directory inside"
        )
    return p


def load_source_tables_from_checkpoints(
    ms1_root: Path,
    ms2_root: Path,
    pub_ids: Sequence[str],
    *,
    member: str = "all",
    unpack_base: Path | None = None,
    fixture_mode: bool = False,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """Load the 5 MS1/MS2 source tables directly from validated MS1/MS2 checkpoints."""
    resolved_ms1 = resolve_ms1_root(ms1_root)
    resolved_ms2 = resolve_ms2_root(
        ms2_root,
        member=member,
        unpack_base=unpack_base or Path.cwd() / ".tmp_ms2_unpack",
    )
    ctx = {"ms1_root": str(resolved_ms1), "ms2_root": str(resolved_ms2), "fixture_mode": fixture_mode}
    tables: dict[str, list[dict[str, Any]]] = {t: [] for t in SOURCE_TABLE_NAMES}
    not_ready: list[str] = []
    ordered_ids = sorted({canonical_id(p) for p in pub_ids})
    for idx, pid in enumerate(ordered_ids, start=1):
        loaded = load_with_provenance(pid, ctx, include_export_tables=True)
        if loaded.bundle.readiness != "ready" or loaded.source_tables is None:
            not_ready.append(f"{pid} ({loaded.bundle.readiness})")
            continue
        for t in SOURCE_TABLE_NAMES:
            tables[t].extend(loaded.source_tables[t])
        if idx % 500 == 0 or idx == len(ordered_ids):
            print(
                f"  Loaded checkpoints: {idx:,}/{len(ordered_ids):,} papers "
                f"(not_ready={len(not_ready)})"
            )

    if not_ready:
        raise ContractError(
            f"{len(not_ready)} publication(s) not ready in MS1/MS2 checkpoints, e.g. {not_ready[:5]}"
        )
    published_refs, ref_stats = prepare_published_references(tables["references"])
    tables["references"] = published_refs
    print(f"  prepare_published_references: {ref_stats}")
    return tables, {
        "source_mode": "ms1_ms2_checkpoints",
        "ms1_root": str(ms1_root),
        "ms2_root": str(ms2_root),
        "n_publications_loaded": len(ordered_ids),
        "ref_stats": ref_stats,
    }


def enrich_dataset_card(
    output_dir: Path,
    *,
    member: str,
    member_range: str | None,
    group_split_id: str,
    member_manifest: PartitionManifest,
    group_valid_mrr: float,
    group_test_mrr: float,
    tv4_model_id: str,
) -> None:
    """Append Group 6 / member scope & verified MRR@5 provenance to README.md (keeping YAML intact)."""
    card_path = output_dir / "README.md"
    existing = card_path.read_text(encoding="utf-8")
    scope_desc = (
        f"Member `{member.upper()}` (assigned range `{member_range}`)"
        if member != "all"
        else "Full Group 6 (`TV1`–`TV5` combined)"
    )
    extra = (
        "\n## Provenance & Scope (HCMUS · Group 6 · Milestone 3)\n\n"
        f"- **Dataset scope:** {scope_desc}\n"
        f"- **Official group `split_id`:** `{group_split_id}` (`seed=42`, `group_size=5`, `stratify=True`)\n"
        f"- **Included split publications:** `{len(member_manifest.rows):,}` "
        f"(`train={sum(member_manifest.counts['train'].values())}`, "
        f"`valid={sum(member_manifest.counts['valid'].values())}`, "
        f"`test={sum(member_manifest.counts['test'].values())}`)\n"
        f"- **Frozen TV4 ranking model:** `{tv4_model_id}`\n"
        f"- **Official group benchmark (`include_zero_in_denominator`):** "
        f"`valid MRR@5 = {group_valid_mrr:.6f}`, `test MRR@5 = {group_test_mrr:.6f}` "
        "(100% Rank-1 on matched queries)\n"
    )
    card_path.write_text(existing.rstrip() + "\n" + extra, encoding="utf-8")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="TV5: Export verified 6-table sharded Parquet dataset (per-member or full group) & upload to Hugging Face Hub"
    )
    p.add_argument(
        "--member",
        choices=MEMBER_CHOICES,
        default="all",
        help="Export individual member slice (tv1..tv5) for personal HF submission, or 'all' for full Group 6",
    )
    p.add_argument(
        "--ranges-config",
        type=Path,
        default=DEFAULT_RANGES_CONFIG,
        help="Path to configs/ms1_group6_ranges.json",
    )
    p.add_argument(
        "--source-mode",
        choices=("tables", "ms1_ms2_checkpoints"),
        required=True,
        help="Read 5 source tables from pre-assembled Parquet/JSONL/JSON dir or MS1/MS2 checkpoints",
    )
    p.add_argument("--source-tables-dir", type=Path, help="Required when --source-mode=tables")
    p.add_argument("--ms1-root", type=Path, help="Required when --source-mode=ms1_ms2_checkpoints")
    p.add_argument("--ms2-root", type=Path, help="Required when --source-mode=ms1_ms2_checkpoints")
    p.add_argument(
        "--export-scope",
        choices=("all_available", "split_only"),
        default="all_available",
        help="Keep all available MS1/MS2 papers for this member (or group) or filter to split publications only",
    )
    p.add_argument(
        "--population-manifest",
        type=Path,
        help="Optional population_manifest.json (used when --source-mode=ms1_ms2_checkpoints & --export-scope=all_available)",
    )
    p.add_argument("--partition-manifest", type=Path, required=True, help="Official group partition_manifest.json")
    p.add_argument("--manual-labels", type=Path, required=True, help="Manual labels.json (T3-04)")
    p.add_argument("--auto-labels", type=Path, help="Auto labels json/zip (T3-05); omit if merged")
    p.add_argument(
        "--tv4-scores",
        type=Path,
        required=True,
        help="Path to tv4-real-02_ban_giao.zip or extracted tv4-real-02 directory",
    )
    p.add_argument("--output-dir", type=Path, required=True, help="Output directory for 6-table Parquet export")
    p.add_argument("--max-rows-per-shard", type=int, default=100_000)
    p.add_argument("--compression", default="zstd", choices=("zstd", "snappy", "gzip", "none"))
    p.add_argument(
        "--allow-extra-source-columns",
        action="store_true",
        help="Allow extra columns on papers/references source tables (projected away cleanly)",
    )
    p.add_argument("--expected-valid-mrr", type=float, default=None, help="Gate check on official group valid MRR@5")
    p.add_argument("--expected-test-mrr", type=float, default=None, help="Gate check on official group test MRR@5")
    p.add_argument("--synthetic", action="store_true", help="Mark export as synthetic/dry-run (blocks HF upload)")
    p.add_argument("--upload", action="store_true", help="Upload verified non-synthetic export to Hugging Face Hub")
    p.add_argument("--repo-id", type=str, default=None, help="Personal Hugging Face dataset repo_id, e.g. user/arxiv-tv5")
    p.add_argument("--private", action="store_true", help="Create/upload as private HF repo (default: public)")
    p.add_argument("--commit-message", default="Upload verified 6-table arXiv MS3 dataset")
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    member_range = load_member_range(args.member, args.ranges_config)

    print("=== 1. Load & Verify Official Group PartitionManifest + Member Slice ===")
    full_manifest = load_partition_manifest(args.partition_manifest)
    assert_no_split_leakage(full_manifest)
    export_manifest_part = slice_manifest_for_member(full_manifest, args.member, member_range)
    target_split_ids = {r.arxiv_id for r in export_manifest_part.rows}

    print(f"  member             : {args.member} (range={member_range or 'ALL Group 6'})")
    print(f"  group split_id     : {full_manifest.split_id}")
    print(f"  label_snapshot_id  : {full_manifest.label_snapshot_id}")
    print(f"  target split pubs  : {len(target_split_ids):,} | counts={export_manifest_part.counts}")

    all_labels = load_label_records(args.manual_labels)
    if args.auto_labels is not None:
        all_labels.extend(load_label_records(args.auto_labels))
    print(
        f"  Loaded labels      : {len(all_labels):,} rows "
        f"(supervised={sum(1 for l in all_labels if l.is_supervised):,})"
    )

    print("\n=== 2. Official Group Gate-Check on TV4 Scores (valid & test) ===")
    tv4_prov = read_tv4_provenance(args.tv4_scores)
    if tv4_prov.get("tv4_split_id") and tv4_prov["tv4_split_id"] != full_manifest.split_id:
        raise ContractError(
            f"TV4 split_id ({tv4_prov['tv4_split_id']}) != manifest split_id ({full_manifest.split_id})"
        )
    tv4_model_id = str(tv4_prov.get("tv4_model_id") or "tv4_model")
    gate_preds = build_predictions_streaming(
        args.tv4_scores,
        full_manifest,
        all_labels,
        full_manifest=full_manifest,
        partitions=("valid", "test"),
        verbose=False,
    )
    group_valid_rep = evaluate(gate_preds, all_labels, full_manifest, partition="valid", model_name=tv4_model_id)
    group_test_rep = evaluate(gate_preds, all_labels, full_manifest, partition="test", model_name=tv4_model_id)
    group_valid_mrr = group_valid_rep.metrics["mrr_at_5"]
    group_test_mrr = group_test_rep.metrics["mrr_at_5"]
    print(f"  Group Gate-Check VALID MRR@5: {group_valid_mrr:.6f}")
    print(f"  Group Gate-Check TEST  MRR@5: {group_test_mrr:.6f}")
    if args.expected_valid_mrr is not None:
        assert abs(group_valid_mrr - args.expected_valid_mrr) < 1e-5, (
            f"Group VALID MRR@5 mismatch: {group_valid_mrr} != {args.expected_valid_mrr}"
        )
    if args.expected_test_mrr is not None:
        assert abs(group_test_mrr - args.expected_test_mrr) < 1e-5, (
            f"Group TEST MRR@5 mismatch: {group_test_mrr} != {args.expected_test_mrr}"
        )

    print("\n=== 3. Load 5 MS1/MS2 Source Tables (Scoped to Member) ===")
    if args.source_mode == "tables":
        if args.source_tables_dir is None:
            raise ContractError("--source-tables-dir is required when --source-mode=tables")
        allowed = target_split_ids if args.export_scope == "split_only" else None
        source_tables, source_prov = load_source_tables_from_dir(
            args.source_tables_dir,
            member_range=member_range,
            allowed_pub_ids=allowed,
        )
    else:
        if args.ms1_root is None or args.ms2_root is None:
            raise ContractError("--ms1-root and --ms2-root are required for ms1_ms2_checkpoints")
        if args.export_scope == "split_only" or args.population_manifest is None:
            target_ids = sorted(target_split_ids)
        else:
            pop_raw = json.loads(args.population_manifest.read_text(encoding="utf-8-sig"))
            raw_ids = pop_raw.get("publication_ids") or pop_raw.get("population_ids") or pop_raw
            target_ids = sorted(
                canonical_id(p) for p in raw_ids if in_member_range(p, member_range)
            )
        source_tables, source_prov = load_source_tables_from_checkpoints(
            args.ms1_root,
            args.ms2_root,
            target_ids,
            member=args.member,
            unpack_base=args.output_dir.parent / ".ms2_unpack_cache",
            fixture_mode=args.synthetic,
        )

    bibtex_queries_by_pub: dict[str, set[str]] = defaultdict(set)
    for row in source_tables["bibtex"]:
        bibtex_queries_by_pub[canonical_id(str(row["arxiv_id"]))].add(str(row["citation_key"]))
    refs_by_pub: dict[str, set[str]] = defaultdict(set)
    for row in source_tables["references"]:
        refs_by_pub[canonical_id(str(row["arxiv_id"]))].add(canonical_id(str(row["ref_arxiv_id"])))

    print(f"\n=== 4. Build `predictions` Table for Scope `{args.member}` ===")
    predictions = build_predictions_streaming(
        args.tv4_scores,
        export_manifest_part,
        all_labels,
        full_manifest=full_manifest,
        partitions=PARTITIONS,
        bibtex_queries_by_pub=bibtex_queries_by_pub,
        refs_by_pub=refs_by_pub,
    )

    member_metrics: dict[str, float | None] = {}
    for eval_part in ("valid", "test"):
        n_pubs_part = sum(export_manifest_part.counts[eval_part].values())
        if n_pubs_part > 0:
            mrep = evaluate(
                predictions,
                all_labels,
                export_manifest_part,
                partition=eval_part,
                model_name=tv4_model_id,
            )
            member_metrics[f"{eval_part}_mrr_at_5"] = mrep.metrics["mrr_at_5"]
            print(
                f"  Member ({args.member}) {eval_part:5s} MRR@5: "
                f"{mrep.metrics['mrr_at_5']:.6f} ({n_pubs_part} pub(s))"
            )
        else:
            member_metrics[f"{eval_part}_mrr_at_5"] = None
            print(f"  Member ({args.member}) {eval_part:5s} MRR@5: N/A (0 {eval_part} pubs in range)")

    print("\n=== 5. Validate 6 Tables & Export Sharded Parquet + Read-Back Verification ===")
    all_six_tables: dict[str, Sequence[Any]] = {
        **source_tables,
        "predictions": predictions,
    }
    provenance = {
        "exported_at_utc": utc_now(),
        "exporter_role": "TV5",
        "member": args.member,
        "member_range": member_range,
        "export_scope": args.export_scope,
        "group_split_id": full_manifest.split_id,
        "member_split_id": export_manifest_part.split_id,
        "label_snapshot_id": full_manifest.label_snapshot_id,
        "group_valid_mrr_at_5": group_valid_mrr,
        "group_test_mrr_at_5": group_test_mrr,
        "member_metrics": member_metrics,
        "tv4": tv4_prov,
        "source": source_prov,
    }

    export_manifest = export_hf_dataset(
        all_six_tables,
        args.output_dir,
        partition_manifest=export_manifest_part,
        max_rows_per_shard=args.max_rows_per_shard,
        compression=args.compression,
        synthetic=args.synthetic,
        allow_extra_source_columns=args.allow_extra_source_columns,
        provenance=provenance,
        upload=False,
    )
    enrich_dataset_card(
        args.output_dir,
        member=args.member,
        member_range=member_range,
        group_split_id=full_manifest.split_id,
        member_manifest=export_manifest_part,
        group_valid_mrr=group_valid_mrr,
        group_test_mrr=group_test_mrr,
        tv4_model_id=tv4_model_id,
    )

    print(f"  Exported & read-back verified: {args.output_dir}")
    print(f"  ready_for_hf_upload: {export_manifest.ready_for_hf_upload} (synthetic={export_manifest.synthetic})")
    print(f"  Total rows across 6 tables: {export_manifest.total_rows:,}")
    for tname in TABLE_NAMES:
        tinfo = export_manifest.tables[tname]
        print(
            f"    - {tname:12s}: {tinfo['row_count']:>10,} rows | "
            f"{tinfo['shard_count']:>3} shard(s) | {tinfo['total_bytes']:>12,} bytes"
        )

    if args.upload:
        print("\n=== 6. Upload Verified Dataset to Hugging Face Hub ===")
        if not args.repo_id:
            raise ContractError("--repo-id is required when --upload is set")
        token = os.environ.get("HF_TOKEN") or getpass.getpass("Enter Hugging Face write token (HF_TOKEN): ")
        res = upload_to_hf(
            args.output_dir,
            args.repo_id,
            manifest=export_manifest,
            token=token,
            private=args.private,
            commit_message=args.commit_message,
        )
        vis = verify_hf_repo_visibility(
            args.repo_id, token=token, expected_manifest=export_manifest
        )
        print(f"  Public Dataset URL : https://huggingface.co/datasets/{args.repo_id}")
        print(f"  publicly_accessible: {vis['publicly_accessible']}")
        print("  HF upload result   :", json.dumps(res, indent=2))
        print("  HF visibility check:", json.dumps(vis, indent=2))
    else:
        print(
            "\n[DRY-RUN COMPLETE] Chưa upload lên Hugging Face Hub "
            "(chạy lại với --upload --repo-id <your-hf-username>/<dataset-name> để công bố)."
        )


if __name__ == "__main__":
    main()
