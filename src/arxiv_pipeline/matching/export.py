"""6-table cross-consistency validator, sharded Parquet exporter & HF upload (Milestone 3 · TV5).

Validates and exports the 6 official Hugging Face dataset tables per the HCMUS
Milestone 1, 2, and 3 specifications:

1. papers      : arxiv_id (str, non-null), title (str), authors (list<str>),
                 submission_date (str, ISO), revised_dates (list<str>), venue (str, nullable)
2. references  : arxiv_id (str, non-null), ref_arxiv_id (str, non-null — arXiv-keyed only),
                 ref_title (str, nullable), ref_authors (list<str>),
                 ref_submission_date (str, nullable), ref_s2_id (str, nullable)
3. elements    : element_id (str, non-null), arxiv_id (str, non-null),
                 kind (dictionary<int8, str>, non-null), content (str)
4. hierarchy   : arxiv_id (str, non-null), version (int64, non-null),
                 element_id (str, non-null), parent_id (str, non-null)
5. bibtex      : arxiv_id (str, non-null), citation_key (str, non-null),
                 entry_type (dictionary<int8, str>, non-null), title (str),
                 authors (list<str>), year (int64, nullable),
                 doi (str, nullable), journal (str, nullable), url (str, nullable)
6. predictions : arxiv_id (str, non-null), partition (dictionary<int8, str>, non-null),
                 citation_key (str, non-null), groundtruth_arxiv_id (str, nullable),
                 cand_1..cand_5 (str, nullable)

Guarantees:
- Cross-table FK, PK/unique-pair, dtype, nullability, and partition-leakage checks.
- Sharded Parquet writing with explicit Arrow schemas and atomic file replacement.
- Mandatory read-back verification (Arrow schema, dictionary encoding, row counts, SHA-256).
- Hugging Face dataset card (`README.md`) with `data/<table>/*.parquet` YAML configs
  and zero custom loading scripts.
- Hard guard refusing to upload synthetic/mock or unverified datasets to Hugging Face Hub.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field, is_dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping, Sequence

import pyarrow as pa
import pyarrow.parquet as pq

from arxiv_pipeline.common.schemas import MATCHING_SCHEMA_VERSION
from arxiv_pipeline.common.utils import atomic_json, utc_now
from arxiv_pipeline.matching.contracts import (
    ContractError,
    Issue,
    PARTITIONS,
    PartitionManifest,
    ValidationReport,
    canonical_id,
)

TABLE_NAMES = ("papers", "references", "elements", "hierarchy", "bibtex", "predictions")
VALID_ELEMENT_KINDS = {
    "document",
    "heading",
    "itemize",
    "item",
    "sentence",
    "block_formula",
    "figure",
}

DICT_STRING_TYPE = pa.dictionary(pa.int8(), pa.string())
DICTIONARY_COLUMNS: dict[str, set[str]] = {
    "elements": {"kind"},
    "bibtex": {"entry_type"},
    "predictions": {"partition"},
}

ARROW_SCHEMAS: dict[str, pa.Schema] = {
    "papers": pa.schema(
        [
            pa.field("arxiv_id", pa.string(), nullable=False),
            pa.field("title", pa.string(), nullable=False),
            pa.field("authors", pa.list_(pa.string()), nullable=False),
            pa.field("submission_date", pa.string(), nullable=False),
            pa.field("revised_dates", pa.list_(pa.string()), nullable=False),
            pa.field("venue", pa.string(), nullable=True),
        ]
    ),
    "references": pa.schema(
        [
            pa.field("arxiv_id", pa.string(), nullable=False),
            pa.field("ref_arxiv_id", pa.string(), nullable=False),
            pa.field("ref_title", pa.string(), nullable=True),
            pa.field("ref_authors", pa.list_(pa.string()), nullable=False),
            pa.field("ref_submission_date", pa.string(), nullable=True),
            pa.field("ref_s2_id", pa.string(), nullable=True),
        ]
    ),
    "elements": pa.schema(
        [
            pa.field("element_id", pa.string(), nullable=False),
            pa.field("arxiv_id", pa.string(), nullable=False),
            pa.field("kind", DICT_STRING_TYPE, nullable=False),
            pa.field("content", pa.string(), nullable=False),
        ]
    ),
    "hierarchy": pa.schema(
        [
            pa.field("arxiv_id", pa.string(), nullable=False),
            pa.field("version", pa.int64(), nullable=False),
            pa.field("element_id", pa.string(), nullable=False),
            pa.field("parent_id", pa.string(), nullable=False),
        ]
    ),
    "bibtex": pa.schema(
        [
            pa.field("arxiv_id", pa.string(), nullable=False),
            pa.field("citation_key", pa.string(), nullable=False),
            pa.field("entry_type", DICT_STRING_TYPE, nullable=False),
            pa.field("title", pa.string(), nullable=False),
            pa.field("authors", pa.list_(pa.string()), nullable=False),
            pa.field("year", pa.int64(), nullable=True),
            pa.field("doi", pa.string(), nullable=True),
            pa.field("journal", pa.string(), nullable=True),
            pa.field("url", pa.string(), nullable=True),
        ]
    ),
    "predictions": pa.schema(
        [
            pa.field("arxiv_id", pa.string(), nullable=False),
            pa.field("partition", DICT_STRING_TYPE, nullable=False),
            pa.field("citation_key", pa.string(), nullable=False),
            pa.field("groundtruth_arxiv_id", pa.string(), nullable=True),
            pa.field("cand_1", pa.string(), nullable=True),
            pa.field("cand_2", pa.string(), nullable=True),
            pa.field("cand_3", pa.string(), nullable=True),
            pa.field("cand_4", pa.string(), nullable=True),
            pa.field("cand_5", pa.string(), nullable=True),
        ]
    ),
}


@dataclass(frozen=True)
class ExportManifest:
    """Published Parquet dataset manifest & verification evidence."""

    output_dir: str
    synthetic: bool
    readback_verified: bool
    ready_for_hf_upload: bool
    tables: dict[str, dict[str, Any]]
    total_rows: int
    partition_counts: dict[str, dict[str, int]]
    validation: dict[str, Any]
    provenance: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)
    schema_version: str = MATCHING_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _to_row_dict(item: Any) -> dict[str, Any]:
    if isinstance(item, Mapping):
        return dict(item)
    if is_dataclass(item):
        return asdict(item)
    raise ContractError(f"Unsupported table row type: {type(item).__name__}")


def _is_int64(val: Any) -> bool:
    return type(val) is int and -(2**63) <= val < 2**63


def _is_str_list(val: Any) -> bool:
    return isinstance(val, list) and all(isinstance(x, str) for x in val)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def prepare_published_references(
    raw_references: Sequence[Mapping[str, Any] | Any],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Filter raw MS1 references down to the arXiv-keyed published `references` table.

    Per Milestone 1 & 3 specifications, all crawled references stay in local MS1
    artifacts, while the published Hugging Face `references` table retains only
    references carrying a non-null arXiv ID. Identical canonical duplicates are
    collapsed; conflicting metadata raises ContractError.
    """
    cols = [f.name for f in ARROW_SCHEMAS["references"]]
    kept: dict[tuple[str, str], dict[str, Any]] = {}
    excluded_non_arxiv = 0
    collapsed_duplicates = 0

    for raw in raw_references:
        row = _to_row_dict(raw)
        if row.get("ref_arxiv_id") is None:
            excluded_non_arxiv += 1
            continue
        pid = canonical_id(row["arxiv_id"])
        rid = canonical_id(row["ref_arxiv_id"])
        clean = {
            "arxiv_id": pid,
            "ref_arxiv_id": rid,
            "ref_title": row.get("ref_title"),
            "ref_authors": list(row.get("ref_authors") or []),
            "ref_submission_date": row.get("ref_submission_date"),
            "ref_s2_id": row.get("ref_s2_id"),
        }
        key = (pid, rid)
        if key in kept:
            if kept[key] != clean:
                raise ContractError(f"Conflicting duplicate reference metadata for {key}")
            collapsed_duplicates += 1
            continue
        kept[key] = {c: clean[c] for c in cols}

    ordered = [kept[k] for k in sorted(kept)]
    stats = {
        "input_rows": len(raw_references),
        "published_rows": len(ordered),
        "excluded_non_arxiv": excluded_non_arxiv,
        "collapsed_duplicates": collapsed_duplicates,
    }
    return ordered, stats


def _project_and_check_columns(
    table_name: str,
    rows: Sequence[Any],
    *,
    allow_extra_columns: bool = False,
) -> tuple[list[dict[str, Any]], list[Issue]]:
    expected = [f.name for f in ARROW_SCHEMAS[table_name]]
    expected_set = set(expected)
    normalized: list[dict[str, Any]] = []
    errors: list[Issue] = []

    for idx, raw in enumerate(rows):
        row = _to_row_dict(raw)
        missing = sorted(expected_set - set(row))
        extra = sorted(set(row) - expected_set)
        pid = row.get("arxiv_id")
        issue_pid = "0704.00001"
        if isinstance(pid, str) and pid.strip():
            try:
                issue_pid = canonical_id(pid)
            except ContractError:
                pass

        if missing:
            errors.append(
                Issue(
                    issue_pid,
                    "export",
                    f"{table_name}_missing_columns",
                    "error",
                    f"Row {idx} in {table_name} missing columns: {missing}",
                )
            )
            continue
        if extra and not allow_extra_columns:
            errors.append(
                Issue(
                    issue_pid,
                    "export",
                    f"{table_name}_unexpected_columns",
                    "error",
                    f"Row {idx} in {table_name} has unexpected columns: {extra}",
                )
            )
            continue
        normalized.append({col: row[col] for col in expected})

    return normalized, errors


def validate_six_tables(
    tables: Mapping[str, Sequence[Any]],
    *,
    partition_manifest: PartitionManifest | None = None,
    allow_extra_source_columns: bool = False,
) -> tuple[ValidationReport, dict[str, list[dict[str, Any]]]]:
    """Validate schema, dtypes, PK/unique pairs, FKs, and partitions across all 6 tables."""
    missing_tables = [t for t in TABLE_NAMES if t not in tables]
    if missing_tables:
        raise ContractError(f"Missing required dataset table(s): {missing_tables}")

    errors: list[Issue] = []
    warnings: list[Issue] = []
    clean: dict[str, list[dict[str, Any]]] = {}

    for tname in TABLE_NAMES:
        allow_extra = allow_extra_source_columns and tname in {"papers", "references"}
        proj, col_errs = _project_and_check_columns(
            tname, tables[tname], allow_extra_columns=allow_extra
        )
        errors.extend(col_errs)
        clean[tname] = proj

    # 1. papers
    paper_ids: set[str] = set()
    for idx, row in enumerate(clean["papers"]):
        try:
            pid = canonical_id(row["arxiv_id"])
            row["arxiv_id"] = pid
        except ContractError as exc:
            errors.append(Issue("0704.00001", "export", "papers_invalid_arxiv_id", "error", f"row {idx}: {exc}"))
            continue
        if pid in paper_ids:
            errors.append(Issue(pid, "export", "papers_duplicate_pk", "error", f"Duplicate paper {pid}"))
        paper_ids.add(pid)
        if not isinstance(row["title"], str) or not row["title"].strip():
            errors.append(Issue(pid, "export", "papers_invalid_title", "error", "title must be nonempty str"))
        if not _is_str_list(row["authors"]):
            errors.append(Issue(pid, "export", "papers_invalid_authors", "error", "authors must be list[str]"))
        if not isinstance(row["submission_date"], str) or not row["submission_date"].strip():
            errors.append(Issue(pid, "export", "papers_invalid_submission_date", "error", "submission_date required"))
        if not _is_str_list(row["revised_dates"]):
            errors.append(Issue(pid, "export", "papers_invalid_revised_dates", "error", "revised_dates must be list[str]"))
        if row["venue"] is not None and not isinstance(row["venue"], str):
            errors.append(Issue(pid, "export", "papers_invalid_venue", "error", "venue must be str|None"))

    # 2. references (published table: arXiv-keyed references only -> ref_arxiv_id non-null)
    ref_pairs: set[tuple[str, str]] = set()
    refs_by_paper: dict[str, set[str]] = defaultdict(set)
    for idx, row in enumerate(clean["references"]):
        try:
            pid = canonical_id(row["arxiv_id"])
            row["arxiv_id"] = pid
        except ContractError as exc:
            errors.append(Issue("0704.00001", "export", "references_invalid_arxiv_id", "error", f"row {idx}: {exc}"))
            continue
        if pid not in paper_ids:
            errors.append(Issue(pid, "export", "references_fk_paper", "error", f"Reference cites unknown paper {pid}"))
        if row["ref_arxiv_id"] is None:
            errors.append(
                Issue(
                    pid,
                    "export",
                    "references_null_ref_arxiv_id",
                    "error",
                    "Published references table must keep arXiv-keyed references only (ref_arxiv_id non-null)",
                )
            )
            continue
        try:
            rid = canonical_id(row["ref_arxiv_id"])
            row["ref_arxiv_id"] = rid
        except ContractError as exc:
            errors.append(Issue(pid, "export", "references_invalid_ref_id", "error", f"row {idx}: {exc}"))
            continue
        rpair = (pid, rid)
        if rpair in ref_pairs:
            errors.append(
                Issue(pid, "export", "references_duplicate_pair", "error", f"Duplicate reference pair {rpair}", ref_arxiv_id=rid)
            )
        ref_pairs.add(rpair)
        refs_by_paper[pid].add(rid)
        if row["ref_title"] is not None and not isinstance(row["ref_title"], str):
            errors.append(Issue(pid, "export", "references_invalid_title", "error", "ref_title must be str|None"))
        if not _is_str_list(row["ref_authors"]):
            errors.append(Issue(pid, "export", "references_invalid_authors", "error", "ref_authors must be list[str]"))
        if row["ref_submission_date"] is not None and not isinstance(row["ref_submission_date"], str):
            errors.append(Issue(pid, "export", "references_invalid_date", "error", "ref_submission_date must be str|None"))
        if row["ref_s2_id"] is not None and not isinstance(row["ref_s2_id"], str):
            errors.append(Issue(pid, "export", "references_invalid_s2_id", "error", "ref_s2_id must be str|None"))

    # 3. elements
    element_owner: dict[str, str] = {}
    for idx, row in enumerate(clean["elements"]):
        try:
            pid = canonical_id(row["arxiv_id"])
            row["arxiv_id"] = pid
        except ContractError as exc:
            errors.append(Issue("0704.00001", "export", "elements_invalid_arxiv_id", "error", f"row {idx}: {exc}"))
            continue
        if pid not in paper_ids:
            errors.append(Issue(pid, "export", "elements_fk_paper", "error", f"Element belongs to unknown paper {pid}"))
        eid = row["element_id"]
        if not isinstance(eid, str) or not eid.strip():
            errors.append(Issue(pid, "export", "elements_invalid_id", "error", "element_id must be nonempty str"))
        elif eid in element_owner:
            errors.append(Issue(pid, "export", "elements_duplicate_pk", "error", f"Duplicate element_id {eid}"))
        else:
            element_owner[eid] = pid
        if not isinstance(row["kind"], str) or row["kind"] not in VALID_ELEMENT_KINDS:
            errors.append(Issue(pid, "export", "elements_invalid_kind", "error", f"Invalid kind: {row['kind']!r}"))
        if not isinstance(row["content"], str):
            errors.append(Issue(pid, "export", "elements_invalid_content", "error", "content must be str"))

    # 4. hierarchy
    seen_hier: set[tuple[str, int, str]] = set()
    for idx, row in enumerate(clean["hierarchy"]):
        try:
            pid = canonical_id(row["arxiv_id"])
            row["arxiv_id"] = pid
        except ContractError as exc:
            errors.append(Issue("0704.00001", "export", "hierarchy_invalid_arxiv_id", "error", f"row {idx}: {exc}"))
            continue
        if pid not in paper_ids:
            errors.append(Issue(pid, "export", "hierarchy_fk_paper", "error", f"Hierarchy belongs to unknown paper {pid}"))
        ver = row["version"]
        if not _is_int64(ver) or ver < 1:
            errors.append(Issue(pid, "export", "hierarchy_invalid_version", "error", f"Invalid version {ver!r}"))
            continue
        eid, parent_id = row["element_id"], row["parent_id"]
        if not isinstance(eid, str) or eid not in element_owner:
            errors.append(Issue(pid, "export", "hierarchy_fk_element", "error", f"Unknown element_id {eid!r}"))
        elif element_owner[eid] != pid:
            errors.append(Issue(pid, "export", "hierarchy_element_paper_mismatch", "error", f"element_id {eid} belongs to {element_owner[eid]}, not {pid}"))
        if not isinstance(parent_id, str) or parent_id not in element_owner:
            errors.append(Issue(pid, "export", "hierarchy_fk_parent", "error", f"Unknown parent_id {parent_id!r}"))
        elif element_owner[parent_id] != pid:
            errors.append(Issue(pid, "export", "hierarchy_parent_paper_mismatch", "error", f"parent_id {parent_id} belongs to {element_owner[parent_id]}, not {pid}"))
        if isinstance(eid, str) and eid == parent_id:
            errors.append(Issue(pid, "export", "hierarchy_self_loop", "error", f"Self-parenting element {eid}"))
        hkey = (pid, ver, str(eid))
        if hkey in seen_hier:
            errors.append(Issue(pid, "export", "hierarchy_duplicate_key", "error", f"Duplicate hierarchy key {hkey}"))
        seen_hier.add(hkey)

    # 5. bibtex
    bibtex_keys: set[tuple[str, str]] = set()
    bibtex_by_paper: dict[str, set[str]] = defaultdict(set)
    for idx, row in enumerate(clean["bibtex"]):
        try:
            pid = canonical_id(row["arxiv_id"])
            row["arxiv_id"] = pid
        except ContractError as exc:
            errors.append(Issue("0704.00001", "export", "bibtex_invalid_arxiv_id", "error", f"row {idx}: {exc}"))
            continue
        if pid not in paper_ids:
            errors.append(Issue(pid, "export", "bibtex_fk_paper", "error", f"BibTeX belongs to unknown paper {pid}"))
        ckey = row["citation_key"]
        if not isinstance(ckey, str) or not ckey.strip():
            errors.append(Issue(pid, "export", "bibtex_invalid_citation_key", "error", "citation_key must be nonempty str"))
            continue
        qkey = (pid, ckey)
        if qkey in bibtex_keys:
            errors.append(Issue(pid, "export", "bibtex_duplicate_key", "error", f"Duplicate QueryKey {qkey}", citation_key=ckey))
        bibtex_keys.add(qkey)
        bibtex_by_paper[pid].add(ckey)
        if not isinstance(row["entry_type"], str) or not row["entry_type"].strip():
            errors.append(Issue(pid, "export", "bibtex_invalid_entry_type", "error", "entry_type required", citation_key=ckey))
        if not isinstance(row["title"], str):
            errors.append(Issue(pid, "export", "bibtex_invalid_title", "error", "title must be str (empty if missing)", citation_key=ckey))
        if not _is_str_list(row["authors"]):
            errors.append(Issue(pid, "export", "bibtex_invalid_authors", "error", "authors must be list[str]", citation_key=ckey))
        if row["year"] is not None and not _is_int64(row["year"]):
            errors.append(Issue(pid, "export", "bibtex_invalid_year", "error", "year must be int64|None (not bool)", citation_key=ckey))
        for opt in ("doi", "journal", "url"):
            if row[opt] is not None and not isinstance(row[opt], str):
                errors.append(Issue(pid, "export", f"bibtex_invalid_{opt}", "error", f"{opt} must be str|None", citation_key=ckey))

    # 6. predictions
    pred_keys: set[tuple[str, str]] = set()
    pred_partitions_by_pub: dict[str, set[str]] = defaultdict(set)
    manifest_part = (
        {r.arxiv_id: r.partition for r in partition_manifest.rows}
        if partition_manifest is not None
        else None
    )

    for idx, row in enumerate(clean["predictions"]):
        try:
            pid = canonical_id(row["arxiv_id"])
            row["arxiv_id"] = pid
        except ContractError as exc:
            errors.append(Issue("0704.00001", "export", "predictions_invalid_arxiv_id", "error", f"row {idx}: {exc}"))
            continue
        if pid not in paper_ids:
            errors.append(Issue(pid, "export", "predictions_fk_paper", "error", f"Prediction paper {pid} not in papers"))
        part = row["partition"]
        if not isinstance(part, str) or part not in PARTITIONS:
            errors.append(Issue(pid, "export", "predictions_invalid_partition", "error", f"Invalid partition {part!r}"))
        else:
            pred_partitions_by_pub[pid].add(part)
            if manifest_part is not None:
                if pid not in manifest_part:
                    errors.append(Issue(pid, "export", "predictions_outside_manifest", "error", f"Paper {pid} not in PartitionManifest"))
                elif manifest_part[pid] != part:
                    errors.append(Issue(pid, "export", "predictions_partition_mismatch", "error", f"Partition {part} != manifest {manifest_part[pid]}"))

        ckey = row["citation_key"]
        if not isinstance(ckey, str) or not ckey.strip():
            errors.append(Issue(pid, "export", "predictions_invalid_citation_key", "error", "citation_key required"))
            continue
        qkey = (pid, ckey)
        if qkey in pred_keys:
            errors.append(Issue(pid, "export", "predictions_duplicate_key", "error", f"Duplicate prediction {qkey}", citation_key=ckey))
        pred_keys.add(qkey)
        if qkey not in bibtex_keys:
            errors.append(Issue(pid, "export", "predictions_fk_bibtex", "error", f"Prediction query {qkey} missing from bibtex", citation_key=ckey))

        gt = row["groundtruth_arxiv_id"]
        if gt is not None:
            try:
                gt_canon = canonical_id(gt)
                row["groundtruth_arxiv_id"] = gt_canon
                if (pid, gt_canon) not in ref_pairs:
                    errors.append(
                        Issue(pid, "export", "predictions_fk_groundtruth", "error", f"Groundtruth {gt_canon} not in references for {pid}", citation_key=ckey, ref_arxiv_id=gt_canon)
                    )
            except ContractError as exc:
                errors.append(Issue(pid, "export", "predictions_invalid_groundtruth", "error", str(exc), citation_key=ckey))

        cands_raw = [row[f"cand_{i}"] for i in range(1, 6)]
        seen_null = False
        seen_cands: set[str] = set()
        for pos, val in enumerate(cands_raw, start=1):
            if val is None:
                seen_null = True
                continue
            if seen_null:
                errors.append(Issue(pid, "export", "predictions_non_trailing_null", "error", f"Non-trailing null before cand_{pos}", citation_key=ckey))
            try:
                cid = canonical_id(val)
                row[f"cand_{pos}"] = cid
            except ContractError as exc:
                errors.append(Issue(pid, "export", "predictions_invalid_candidate", "error", f"cand_{pos}: {exc}", citation_key=ckey))
                continue
            if cid in seen_cands:
                errors.append(Issue(pid, "export", "predictions_duplicate_candidate", "error", f"Duplicate candidate {cid}", citation_key=ckey, ref_arxiv_id=cid))
            seen_cands.add(cid)
            if (pid, cid) not in ref_pairs:
                errors.append(Issue(pid, "export", "predictions_fk_candidate", "error", f"Candidate {cid} not in references for {pid}", citation_key=ckey, ref_arxiv_id=cid))

        expected_cand_count = min(5, len(refs_by_paper.get(pid, set())))
        if len(seen_cands) != expected_cand_count:
            errors.append(
                Issue(
                    pid,
                    "export",
                    "predictions_candidate_count_mismatch",
                    "error",
                    f"Expected {expected_cand_count} candidates, got {len(seen_cands)}",
                    citation_key=ckey,
                )
            )

    for pid, parts in sorted(pred_partitions_by_pub.items()):
        if len(parts) > 1:
            errors.append(
                Issue(pid, "export", "predictions_partition_leakage", "error", f"Paper {pid} spans multiple partitions: {sorted(parts)}")
            )
        missing_bib = sorted(bibtex_by_paper.get(pid, set()) - {q[1] for q in pred_keys if q[0] == pid})
        if missing_bib:
            errors.append(
                Issue(pid, "export", "predictions_missing_bibtex_queries", "error", f"Missing {len(missing_bib)} bibtex queries in predictions, e.g. {missing_bib[0]}")
            )

    if manifest_part is not None:
        for pid in sorted(manifest_part):
            expected_qs = {(pid, ck) for ck in bibtex_by_paper.get(pid, set())}
            missing_qs = sorted(expected_qs - pred_keys)
            if missing_qs:
                errors.append(
                    Issue(pid, "export", "predictions_missing_split_queries", "error", f"Split paper {pid} missing {len(missing_qs)} predictions")
                )

    counts = {tname: len(clean[tname]) for tname in TABLE_NAMES}
    return ValidationReport(errors, warnings, counts), clean


def _build_arrow_table(table_name: str, rows: list[dict[str, Any]]) -> pa.Table:
    schema = ARROW_SCHEMAS[table_name]
    dict_cols = DICTIONARY_COLUMNS.get(table_name, set())
    columns = {}
    for field_spec in schema:
        col_values = [r[field_spec.name] for r in rows]
        if field_spec.name in dict_cols:
            arr = pa.array(col_values, type=pa.string()).dictionary_encode().cast(DICT_STRING_TYPE)
            columns[field_spec.name] = arr
        else:
            columns[field_spec.name] = pa.array(col_values, type=field_spec.type)
    return pa.table(columns, schema=schema)


def _atomic_write_parquet(arrow_table: pa.Table, path: Path, *, compression: str = "zstd") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    os.close(fd)
    try:
        pq.write_table(arrow_table, tmp_name, compression=compression)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _write_dataset_card(output_dir: Path, manifest: ExportManifest) -> Path:
    """Write a Hugging Face dataset card README.md with official YAML configs."""
    configs_yaml = "\n".join(
        f"  - config_name: {tname}\n"
        f"    data_files:\n"
        f"      - split: train\n"
        f"        path: data/{tname}/*.parquet"
        for tname in TABLE_NAMES
    )
    status_banner = (
        "> **WARNING: SYNTHETIC / MOCK EXPORT — DO NOT PUBLISH TO HUGGING FACE HUB.**\n"
        if manifest.synthetic
        else "> Verified 6-table sharded Parquet export ready for streaming and Hub publication.\n"
    )
    rows_md = "\n".join(
        f"| `{t}` | {manifest.tables[t]['row_count']} | {manifest.tables[t]['shard_count']} | {manifest.tables[t]['total_bytes']} |"
        for t in TABLE_NAMES
    )
    content = (
        "---\n"
        "configs:\n"
        f"{configs_yaml}\n"
        "---\n\n"
        "# arXiv Version-Aware Corpus & Reference Matching Dataset\n\n"
        f"{status_banner}\n"
        f"- **Schema version:** `{manifest.schema_version}`\n"
        f"- **Total rows across 6 tables:** `{manifest.total_rows}`\n"
        f"- **Read-back verified:** `{manifest.readback_verified}`\n\n"
        "## Table Summary\n\n"
        "| Table | Rows | Shards | Bytes |\n"
        "| --- | ---: | ---: | ---: |\n"
        f"{rows_md}\n"
    )
    card_path = output_dir / "README.md"
    fd, tmp = tempfile.mkstemp(prefix="README.md.", dir=output_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp, card_path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return card_path


def verify_parquet_export(
    export_dir: str | Path,
    manifest: ExportManifest | Mapping[str, Any],
) -> dict[str, Any]:
    """Read back every exported Parquet shard and verify schema, row totals, and SHA-256."""
    root = Path(export_dir)
    data = manifest.to_dict() if isinstance(manifest, ExportManifest) else dict(manifest)
    checked_rows = 0
    per_table_rows: dict[str, int] = {}

    for tname in TABLE_NAMES:
        tinfo = data["tables"][tname]
        expected_schema = ARROW_SCHEMAS[tname]
        dict_cols = DICTIONARY_COLUMNS.get(tname, set())
        table_rows = 0
        for shard in tinfo["shards"]:
            shard_path = root / shard["path"]
            if not shard_path.is_file():
                raise ContractError(f"Missing exported shard: {shard['path']}")
            actual_hash = _sha256_file(shard_path)
            if actual_hash != shard["sha256"]:
                raise ContractError(f"Checksum mismatch for shard {shard['path']}")
            tbl = pq.read_table(shard_path)
            if tbl.column_names != expected_schema.names:
                raise ContractError(
                    f"Read-back column mismatch in {shard['path']}: "
                    f"{tbl.column_names} != {expected_schema.names}"
                )
            for field_spec in expected_schema:
                actual_field = tbl.schema.field(field_spec.name)
                if field_spec.name in dict_cols:
                    if not pa.types.is_dictionary(actual_field.type):
                        raise ContractError(
                            f"{tname}.{field_spec.name} must be dictionary-encoded, got {actual_field.type}"
                        )
                elif actual_field.type != field_spec.type:
                    raise ContractError(
                        f"Read-back dtype mismatch in {shard['path']}.{field_spec.name}: "
                        f"{actual_field.type} != {field_spec.type}"
                    )
            if tbl.num_rows != shard["rows"]:
                raise ContractError(
                    f"Shard row count mismatch in {shard['path']}: {tbl.num_rows} != {shard['rows']}"
                )
            table_rows += tbl.num_rows
        if table_rows != tinfo["row_count"]:
            raise ContractError(
                f"Total row count mismatch for {tname}: {table_rows} != {tinfo['row_count']}"
            )
        per_table_rows[tname] = table_rows
        checked_rows += table_rows

    if checked_rows != data["total_rows"]:
        raise ContractError(
            f"Grand total row count mismatch: {checked_rows} != {data['total_rows']}"
        )

    return {"verified": True, "total_rows": checked_rows, "per_table_rows": per_table_rows}


def export_hf_dataset(
    tables: Mapping[str, Sequence[Any]],
    paths: str | Path | Mapping[str, str | Path],
    *,
    partition_manifest: PartitionManifest | None = None,
    max_rows_per_shard: int = 100_000,
    compression: str = "zstd",
    synthetic: bool = False,
    allow_extra_source_columns: bool = False,
    provenance: Mapping[str, Any] | None = None,
    upload: bool = False,
    repo_id: str | None = None,
    token: str | None = None,
    private: bool = False,
    api: Any = None,
) -> ExportManifest:
    """Validate 6 tables, write sharded Parquet files, verify read-back, and optionally upload to HF.

    Contract: `export_hf_dataset(tables, paths) -> ExportManifest`.
    Refuses to export invalid tables and refuses to upload synthetic/mock data to HF.
    """
    if type(max_rows_per_shard) is not int or max_rows_per_shard < 1:
        raise ContractError("max_rows_per_shard must be a positive int")

    if isinstance(paths, Mapping):
        if "output_dir" not in paths:
            raise ContractError("paths mapping must contain 'output_dir'")
        output_dir = Path(paths["output_dir"])
        manifest_path = Path(paths.get("manifest_path", output_dir / "export_manifest.json"))
    else:
        output_dir = Path(paths)
        manifest_path = output_dir / "export_manifest.json"

    report, clean_tables = validate_six_tables(
        tables,
        partition_manifest=partition_manifest,
        allow_extra_source_columns=allow_extra_source_columns,
    )
    if not report.valid:
        codes = sorted({e.code for e in report.errors})
        raise ContractError(
            f"6-table consistency check failed with {len(report.errors)} error(s): {codes}"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    table_manifests: dict[str, dict[str, Any]] = {}
    total_rows = 0

    for tname in TABLE_NAMES:
        rows = clean_tables[tname]
        n_rows = len(rows)
        total_rows += n_rows
        n_shards = max(1, math.ceil(n_rows / max_rows_per_shard))
        table_dir = output_dir / "data" / tname
        table_dir.mkdir(parents=True, exist_ok=True)
        for old_file in table_dir.glob("part-*.parquet"):
            old_file.unlink()

        shard_entries: list[dict[str, Any]] = []
        total_bytes = 0
        for shard_idx in range(n_shards):
            start = shard_idx * max_rows_per_shard
            chunk = rows[start : start + max_rows_per_shard]
            arrow_tbl = _build_arrow_table(tname, chunk)
            rel_path = f"data/{tname}/part-{shard_idx:05d}-of-{n_shards:05d}.parquet"
            full_path = output_dir / rel_path
            _atomic_write_parquet(arrow_tbl, full_path, compression=compression)
            size_bytes = full_path.stat().st_size
            total_bytes += size_bytes
            shard_entries.append(
                {
                    "path": rel_path,
                    "rows": len(chunk),
                    "bytes": size_bytes,
                    "sha256": _sha256_file(full_path),
                }
            )

        table_manifests[tname] = {
            "row_count": n_rows,
            "shard_count": n_shards,
            "total_bytes": total_bytes,
            "columns": [
                {"name": f.name, "type": str(f.type), "nullable": f.nullable}
                for f in ARROW_SCHEMAS[tname]
            ],
            "shards": shard_entries,
        }

    part_counts: dict[str, dict[str, int]] = {}
    for p in PARTITIONS:
        p_rows = [r for r in clean_tables["predictions"] if r["partition"] == p]
        part_counts[p] = {
            "rows": len(p_rows),
            "publications": len({r["arxiv_id"] for r in p_rows}),
        }

    draft_manifest = ExportManifest(
        output_dir=output_dir.as_posix(),
        synthetic=bool(synthetic),
        readback_verified=False,
        ready_for_hf_upload=False,
        tables=table_manifests,
        total_rows=total_rows,
        partition_counts=part_counts,
        validation=report.to_dict(),
        provenance=dict(provenance or {}),
    )
    verify_parquet_export(output_dir, draft_manifest)

    final_manifest = ExportManifest(
        output_dir=output_dir.as_posix(),
        synthetic=bool(synthetic),
        readback_verified=True,
        ready_for_hf_upload=(not bool(synthetic) and report.valid),
        tables=table_manifests,
        total_rows=total_rows,
        partition_counts=part_counts,
        validation=report.to_dict(),
        provenance=dict(provenance or {}),
        created_at=draft_manifest.created_at,
    )
    _write_dataset_card(output_dir, final_manifest)
    atomic_json(manifest_path, final_manifest.to_dict())

    if upload:
        if not repo_id:
            raise ContractError("repo_id is required when upload=True")
        upload_to_hf(
            output_dir,
            repo_id,
            manifest=final_manifest,
            token=token,
            private=private,
            api=api,
        )

    return final_manifest


def export_dataset(
    source_tables: Mapping[str, Sequence[Any]],
    predictions: Sequence[Any],
    output_dir: str | Path,
    *,
    partition_manifest: PartitionManifest | None = None,
    max_rows_per_shard: int = 100_000,
    synthetic: bool = False,
    allow_extra_source_columns: bool = False,
    provenance: Mapping[str, Any] | None = None,
) -> ExportManifest:
    """Contract alias matching docs/MS3_CONTRACTS.md section 6."""
    combined = dict(source_tables)
    combined["predictions"] = list(predictions)
    return export_hf_dataset(
        combined,
        output_dir,
        partition_manifest=partition_manifest,
        max_rows_per_shard=max_rows_per_shard,
        synthetic=synthetic,
        allow_extra_source_columns=allow_extra_source_columns,
        provenance=provenance,
    )


def _get_hf_api(api: Any = None, token: str | None = None):
    if api is not None:
        return api
    from huggingface_hub import HfApi

    return HfApi(token=token)


def upload_to_hf(
    export_dir: str | Path,
    repo_id: str,
    *,
    manifest: ExportManifest | Mapping[str, Any] | None = None,
    token: str | None = None,
    private: bool = False,
    commit_message: str = "Upload verified 6-table arXiv dataset",
    api: Any = None,
) -> dict[str, Any]:
    """Upload a verified, non-synthetic sharded Parquet dataset to Hugging Face Hub."""
    root = Path(export_dir)
    if manifest is None:
        manifest_file = root / "export_manifest.json"
        if not manifest_file.is_file():
            raise ContractError("Cannot upload without export_manifest.json")
        manifest_data = json.loads(manifest_file.read_text(encoding="utf-8"))
    elif isinstance(manifest, ExportManifest):
        manifest_data = manifest.to_dict()
    else:
        manifest_data = dict(manifest)

    if manifest_data.get("synthetic", True):
        raise ContractError("Refusing to upload synthetic/mock dataset to Hugging Face Hub")
    if not manifest_data.get("readback_verified") or not manifest_data.get("ready_for_hf_upload"):
        raise ContractError(
            "Refusing to upload dataset that has not passed validation and Parquet read-back"
        )

    verify_parquet_export(root, manifest_data)

    hf_api = _get_hf_api(api=api, token=token)
    hf_api.create_repo(repo_id=repo_id, repo_type="dataset", private=private, exist_ok=True)
    hf_api.upload_folder(
        folder_path=str(root),
        repo_id=repo_id,
        repo_type="dataset",
        commit_message=commit_message,
    )
    visibility = verify_hf_repo_visibility(repo_id, token=token, api=hf_api)
    return {
        "repo_id": repo_id,
        "uploaded": True,
        "visibility": visibility,
    }


def verify_hf_repo_visibility(
    repo_id: str,
    *,
    token: str | None = None,
    api: Any = None,
) -> dict[str, Any]:
    """Check Hugging Face dataset repository visibility and presence of all 6 Parquet tables."""
    hf_api = _get_hf_api(api=api, token=token)
    info = hf_api.dataset_info(repo_id=repo_id)
    files = list(hf_api.list_repo_files(repo_id=repo_id, repo_type="dataset"))

    is_private = bool(getattr(info, "private", False))
    gated = getattr(info, "gated", False)
    is_gated = bool(gated) and str(gated).lower() != "false"
    parquet_by_table = {
        tname: sorted(f for f in files if f.startswith(f"data/{tname}/") and f.endswith(".parquet"))
        for tname in TABLE_NAMES
    }
    missing_tables = [tname for tname, paths in parquet_by_table.items() if not paths]

    return {
        "repo_id": repo_id,
        "exists": True,
        "private": is_private,
        "gated": is_gated,
        "publicly_accessible": (not is_private) and (not is_gated),
        "all_six_tables_present": not missing_tables,
        "missing_tables": missing_tables,
        "parquet_files": parquet_by_table,
    }
