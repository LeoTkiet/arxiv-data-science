"""Parquet dataset exporter facade delegating to matching/export (Milestone 3 · TV5)."""

from pathlib import Path

from arxiv_pipeline.common.schemas import MATCHING_SCHEMA_VERSION, ErrorInfo, StageResult
from arxiv_pipeline.common.utils import utc_now
from arxiv_pipeline.matching.contracts import ContractError
from arxiv_pipeline.matching.export import (
    ARROW_SCHEMAS,
    TABLE_NAMES,
    ExportManifest,
    export_dataset,
    export_hf_dataset,
    prepare_published_references,
    upload_to_hf,
    validate_six_tables,
    verify_hf_repo_visibility,
    verify_parquet_export,
)

__all__ = [
    "ARROW_SCHEMAS",
    "TABLE_NAMES",
    "ExportManifest",
    "export_dataset",
    "export_hf_dataset",
    "prepare_published_references",
    "upload_to_hf",
    "validate_six_tables",
    "verify_hf_repo_visibility",
    "verify_parquet_export",
    "run",
]


def run(record, output_dir, *, version=None, upstream=None):
    """Pipeline adapter entrypoint for exporting the 6-table sharded Parquet dataset."""
    raw_id = record.get("arxiv_id") if isinstance(record, dict) else "dataset"
    if not isinstance(upstream, dict) or "tables" not in upstream:
        return StageResult(
            "export_parquet",
            str(raw_id),
            None,
            "skipped",
            utc_now(),
            schema_version=MATCHING_SCHEMA_VERSION,
            error=ErrorInfo("upstream_unavailable", "All 6 dataset tables are required for Parquet export"),
        )
    out_root = Path(output_dir) / "hf_dataset"
    try:
        manifest = export_hf_dataset(
            upstream["tables"],
            out_root,
            partition_manifest=upstream.get("partition_manifest"),
            max_rows_per_shard=upstream.get("max_rows_per_shard", 100_000),
            synthetic=bool(upstream.get("synthetic", False)),
            provenance=upstream.get("provenance"),
        )
        return StageResult(
            "export_parquet",
            str(raw_id),
            None,
            "success",
            utc_now(),
            schema_version=MATCHING_SCHEMA_VERSION,
            artifacts={"export_manifest": "hf_dataset/export_manifest.json", "dataset_card": "hf_dataset/README.md"},
            size_bytes=sum(t["total_bytes"] for t in manifest.tables.values()),
        )
    except (ContractError, KeyError, TypeError, ValueError) as exc:
        return StageResult(
            "export_parquet",
            str(raw_id),
            None,
            "data_error",
            utc_now(),
            schema_version=MATCHING_SCHEMA_VERSION,
            error=ErrorInfo("export_error", str(exc)),
        )
