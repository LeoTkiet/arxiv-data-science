"""Manual & QA Labeling workflow for Reference Matching (Milestone 3 · TV3).

Provides:
- Label validation (`validate_label_manifest`) wrapping `contracts.validate_labels`
- Side-by-side CSV/JSONL annotation sheet export & import
- Testable interactive CLI (`run_interactive_cli`) for manual annotation
- Quota & provenance audit (`audit_label_manifest`)
- Pipeline adapter `run(...)` for Milestone 3 integration
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Callable, Iterable

from arxiv_pipeline.common.schemas import MATCHING_SCHEMA_VERSION, ErrorInfo, StageResult
from arxiv_pipeline.common.utils import atomic_json, paper_key, utc_now
from arxiv_pipeline.matching.contracts import (
    ContractError,
    Issue,
    LabelRecord,
    PublicationBundle,
    ValidationReport,
    canonical_id,
    publication_from_rows,
    validate_labels,
)


def _format_authors(authors: list[str], limit: int = 3) -> str:
    if not authors:
        return ""
    if len(authors) <= limit:
        return "; ".join(authors)
    return "; ".join(authors[:limit]) + f" (+{len(authors) - limit} more)"


def export_annotation_sheet(
    bundles: Iterable[PublicationBundle],
    output_path: str | Path,
) -> int:
    """Export side-by-side BibTeX queries and candidates to a UTF-8 CSV annotation sheet."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "arxiv_id",
        "citation_key",
        "bib_title",
        "bib_authors",
        "bib_year",
        "bib_doi",
        "bib_url",
        "candidates_preview",
        "decision",
        "groundtruth_arxiv_id",
        "annotator",
        "reviewer",
        "review_status",
        "notes",
    ]
    row_count = 0
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for bundle in bundles:
            cand_lines = [
                f"[{idx}] {ref.ref_arxiv_id} | {ref.ref_title} | "
                f"{_format_authors(ref.ref_authors)} | {ref.ref_submission_date or ''}"
                for idx, ref in enumerate(bundle.references)
            ]
            cand_preview = "\n".join(cand_lines) if cand_lines else "(no candidates)"
            for bib in bundle.bibtex:
                writer.writerow({
                    "arxiv_id": bundle.arxiv_id,
                    "citation_key": bib.citation_key,
                    "bib_title": bib.title,
                    "bib_authors": _format_authors(bib.authors),
                    "bib_year": "" if bib.year is None else str(bib.year),
                    "bib_doi": bib.doi or "",
                    "bib_url": bib.url or "",
                    "candidates_preview": cand_preview,
                    "decision": "",
                    "groundtruth_arxiv_id": "",
                    "annotator": "",
                    "reviewer": "",
                    "review_status": "pending",
                    "notes": "",
                })
                row_count += 1
    return row_count


def load_annotation_sheet(
    input_path: str | Path,
    *,
    default_annotator: str | None = None,
) -> list[LabelRecord]:
    """Load annotated rows from a CSV sheet into validated LabelRecord objects.

    Rows with empty `decision` are skipped (remain unlabeled). Duplicate keys raise ContractError.
    """
    path = Path(input_path)
    records: list[LabelRecord] = []
    seen_keys: set[tuple[str, str]] = set()

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row_num, row in enumerate(reader, start=2):
            decision = (row.get("decision") or "").strip().lower()
            if not decision:
                continue

            arxiv_id = canonical_id((row.get("arxiv_id") or "").strip())
            citation_key = (row.get("citation_key") or "").strip()
            key = (arxiv_id, citation_key)
            if key in seen_keys:
                raise ContractError(f"Duplicate annotation row at line {row_num}: {key}")
            seen_keys.add(key)

            raw_gt = (row.get("groundtruth_arxiv_id") or "").strip()
            gt_id = canonical_id(raw_gt) if raw_gt else None
            annotator = (row.get("annotator") or "").strip() or default_annotator
            reviewer = (row.get("reviewer") or "").strip() or None
            review_status = (row.get("review_status") or "").strip().lower() or "pending"
            notes = (row.get("notes") or "").strip() or None

            records.append(
                LabelRecord(
                    arxiv_id=arxiv_id,
                    citation_key=citation_key,
                    groundtruth_arxiv_id=gt_id,
                    decision=decision,
                    label_source="manual",
                    confidence=None,
                    annotator=annotator,
                    reviewer=reviewer,
                    review_status=review_status,
                    heuristic_version=None,
                    notes=notes,
                )
            )
    return records


def save_labels_json(labels: Iterable[LabelRecord], output_path: str | Path) -> None:
    """Persist LabelRecord list atomically as JSON."""
    payload = [label.to_dict() for label in labels]
    atomic_json(Path(output_path), payload)


def load_labels_json(input_path: str | Path) -> list[LabelRecord]:
    """Load LabelRecord list from JSON file with schema validation."""
    raw_list = json.loads(Path(input_path).read_text(encoding="utf-8"))
    if not isinstance(raw_list, list):
        raise ContractError("Labels JSON must be a list of LabelRecord objects")
    records: list[LabelRecord] = []
    for item in raw_list:
        if not isinstance(item, dict):
            raise ContractError("Each label entry must be a JSON object")
        records.append(LabelRecord(**item))
    return records


def validate_label_manifest(
    bundles: list[PublicationBundle],
    labels: list[LabelRecord],
    *,
    require_annotator_for_manual: bool = True,
) -> ValidationReport:
    """Validate label FK/uniqueness via contracts.validate_labels plus TV3 provenance rules."""
    base_report = validate_labels(bundles, labels)
    errors = list(base_report.errors)
    warnings = list(base_report.warnings)

    for label in labels:
        if label.label_source == "manual" and require_annotator_for_manual and not label.annotator:
            errors.append(
                Issue(
                    label.arxiv_id,
                    "labels",
                    "missing_annotator",
                    "error",
                    "Manual label is missing annotator identifier",
                    citation_key=label.citation_key,
                )
            )
        if label.review_status == "accepted" and label.label_source == "manual" and not label.reviewer:
            warnings.append(
                Issue(
                    label.arxiv_id,
                    "labels",
                    "missing_reviewer",
                    "warning",
                    "Accepted manual label has no cross-reviewer recorded",
                    citation_key=label.citation_key,
                )
            )
        if label.decision == "uncertain" and not label.notes:
            warnings.append(
                Issue(
                    label.arxiv_id,
                    "labels",
                    "uncertain_without_notes",
                    "warning",
                    "Uncertain label should include explanatory notes for review",
                    citation_key=label.citation_key,
                )
            )

    return ValidationReport(errors, warnings, dict(base_report.counts))


def audit_label_manifest(
    bundles: list[PublicationBundle],
    labels: list[LabelRecord],
    *,
    min_manual_pubs: int = 25,
    min_entries_per_pub: int = 20,
    min_entries_per_annotator: int = 100,
    min_pubs_per_annotator: int = 5,
) -> dict[str, object]:
    """Audit label coverage, manual/auto split readiness, and annotator quotas."""
    report = validate_label_manifest(bundles, labels)
    by_pub_total: dict[str, int] = {b.arxiv_id: len(b.bibtex) for b in bundles}
    by_pub_labeled: dict[str, int] = {b.arxiv_id: 0 for b in bundles}
    by_pub_supervised: dict[str, int] = {b.arxiv_id: 0 for b in bundles}
    by_pub_supervised_manual: dict[str, int] = {b.arxiv_id: 0 for b in bundles}
    by_annotator_entries: dict[str, int] = {}
    by_annotator_supervised_entries: dict[str, int] = {}
    by_annotator_pubs: dict[str, set[str]] = {}
    by_annotator_supervised_pubs: dict[str, set[str]] = {}
    by_source: dict[str, int] = {"manual": 0, "heuristic": 0}
    by_decision: dict[str, int] = {"matched": 0, "unmatched": 0, "uncertain": 0}
    by_review: dict[str, int] = {"pending": 0, "accepted": 0, "rejected": 0}

    manual_pubs: set[str] = set()
    heuristic_pubs: set[str] = set()
    manual_supervised_total = 0

    for label in labels:
        if label.arxiv_id in by_pub_labeled:
            by_pub_labeled[label.arxiv_id] += 1
            if label.is_supervised:
                by_pub_supervised[label.arxiv_id] += 1
        by_source[label.label_source] = by_source.get(label.label_source, 0) + 1
        by_decision[label.decision] = by_decision.get(label.decision, 0) + 1
        by_review[label.review_status] = by_review.get(label.review_status, 0) + 1

        if label.label_source == "manual":
            manual_pubs.add(label.arxiv_id)
            ann = label.annotator or "<unknown>"
            by_annotator_entries[ann] = by_annotator_entries.get(ann, 0) + 1
            by_annotator_pubs.setdefault(ann, set()).add(label.arxiv_id)
            if label.is_supervised:
                manual_supervised_total += 1
                by_pub_supervised_manual[label.arxiv_id] = (
                    by_pub_supervised_manual.get(label.arxiv_id, 0) + 1
                )
                by_annotator_supervised_entries[ann] = (
                    by_annotator_supervised_entries.get(ann, 0) + 1
                )
                by_annotator_supervised_pubs.setdefault(ann, set()).add(label.arxiv_id)
        else:
            heuristic_pubs.add(label.arxiv_id)

    qualifying_manual_pubs = [
        pid
        for pid in manual_pubs
        if by_pub_supervised_manual.get(pid, 0) >= min_entries_per_pub
    ]
    pool_overlap = sorted(manual_pubs & heuristic_pubs)

    annotator_summary = {
        ann: {
            "publications": len(by_annotator_pubs[ann]),
            "entries": count,
            "supervised_publications": len(by_annotator_supervised_pubs.get(ann, set())),
            "supervised_entries": by_annotator_supervised_entries.get(ann, 0),
            "meets_quota": (
                len(by_annotator_supervised_pubs.get(ann, set())) >= min_pubs_per_annotator
                and by_annotator_supervised_entries.get(ann, 0) >= min_entries_per_annotator
            ),
        }
        for ann, count in sorted(by_annotator_entries.items())
    }

    return {
        "valid": report.valid and not pool_overlap,
        "validation_counts": report.counts,
        "error_count": len(report.errors),
        "warning_count": len(report.warnings),
        "by_source": by_source,
        "by_decision": by_decision,
        "by_review_status": by_review,
        "manual_publication_count": len(manual_pubs),
        "manual_entered_entries": by_source.get("manual", 0),
        "manual_supervised_entries": manual_supervised_total,
        "qualifying_manual_publications": len(qualifying_manual_pubs),
        "manual_quota_met": (
            len(qualifying_manual_pubs) >= min_manual_pubs
            and manual_supervised_total >= min_manual_pubs * min_entries_per_pub
        ),
        "heuristic_publication_count": len(heuristic_pubs),
        "pool_overlap_publications": pool_overlap,
        "annotators": annotator_summary,
        "per_publication_coverage": {
            pid: {
                "total_bibtex": by_pub_total[pid],
                "labeled": by_pub_labeled[pid],
                "supervised": by_pub_supervised[pid],
            }
            for pid in sorted(by_pub_total)
        },
    }


def run_interactive_cli(
    bundles: Iterable[PublicationBundle],
    *,
    annotator: str,
    existing_labels: Iterable[LabelRecord] | None = None,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> list[LabelRecord]:
    """Interactive terminal CLI for manual annotation, preserving existing labels."""
    if not annotator or not annotator.strip():
        raise ContractError("annotator identifier is required")

    by_key: dict[tuple[str, str], LabelRecord] = {}
    if existing_labels:
        for lbl in existing_labels:
            by_key[lbl.key] = lbl

    stop = False
    for bundle in bundles:
        if stop:
            break
        for bib in bundle.bibtex:
            key = (bundle.arxiv_id, bib.citation_key)
            if key in by_key:
                continue

            output_fn("=" * 60)
            output_fn(f"Paper: {bundle.arxiv_id} | Key: {bib.citation_key}")
            output_fn(f"Title: {bib.title}")
            output_fn(f"Authors: {_format_authors(bib.authors, limit=5)} | Year: {bib.year}")
            output_fn(f"DOI: {bib.doi or '-'} | URL: {bib.url or '-'}")
            output_fn("-" * 60)
            if not bundle.references:
                output_fn("  (No reference candidates in this publication)")
            else:
                for idx, ref in enumerate(bundle.references):
                    output_fn(
                        f"  [{idx}] {ref.ref_arxiv_id} | {ref.ref_title} | "
                        f"{_format_authors(ref.ref_authors)} | {ref.ref_submission_date or '-'}"
                    )

            while True:
                choice = input_fn(
                    "Select [0..N] candidate, [u]nmatched, [?]uncertain, [s]kip, [q]uit: "
                ).strip()
                low = choice.lower()
                if low == "q":
                    stop = True
                    break
                if low == "s":
                    break
                if low == "u":
                    note = input_fn("Notes (optional): ").strip() or None
                    by_key[key] = LabelRecord(
                        arxiv_id=bundle.arxiv_id,
                        citation_key=bib.citation_key,
                        groundtruth_arxiv_id=None,
                        decision="unmatched",
                        label_source="manual",
                        annotator=annotator.strip(),
                        review_status="pending",
                        notes=note,
                    )
                    break
                if low in {"?", "uncertain"}:
                    note = input_fn("Reason for uncertain (recommended): ").strip() or None
                    by_key[key] = LabelRecord(
                        arxiv_id=bundle.arxiv_id,
                        citation_key=bib.citation_key,
                        groundtruth_arxiv_id=None,
                        decision="uncertain",
                        label_source="manual",
                        annotator=annotator.strip(),
                        review_status="pending",
                        notes=note,
                    )
                    break
                if choice.isdigit():
                    idx = int(choice)
                    if 0 <= idx < len(bundle.references):
                        ref = bundle.references[idx]
                        by_key[key] = LabelRecord(
                            arxiv_id=bundle.arxiv_id,
                            citation_key=bib.citation_key,
                            groundtruth_arxiv_id=canonical_id(ref.ref_arxiv_id),
                            decision="matched",
                            label_source="manual",
                            annotator=annotator.strip(),
                            review_status="pending",
                        )
                        break
                output_fn("Invalid choice. Please try again.")
            if stop:
                break

    return list(by_key.values())


def run(record, output_dir, *, version=None, upstream=None):
    """Pipeline adapter entrypoint for matching/labeling (Milestone 3 · TV3)."""
    out_root = Path(output_dir)
    raw_id = record.get("arxiv_id") if isinstance(record, dict) else None
    if not raw_id:
        return StageResult(
            "labeling",
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
            "labeling",
            str(raw_id),
            None,
            "data_error",
            utc_now(),
            schema_version=MATCHING_SCHEMA_VERSION,
            error=ErrorInfo("invalid_input", str(exc)),
        )

    key = paper_key(paper)
    if not isinstance(upstream, dict) or "bundles" not in upstream:
        return StageResult(
            "labeling",
            paper,
            None,
            "skipped",
            utc_now(),
            schema_version=MATCHING_SCHEMA_VERSION,
            error=ErrorInfo("upstream_unavailable", "PublicationBundle upstream input is required"),
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

        if not bundles:
            return StageResult(
                "labeling",
                paper,
                None,
                "data_error",
                utc_now(),
                schema_version=MATCHING_SCHEMA_VERSION,
                error=ErrorInfo("identity_mismatch", f"No PublicationBundle provided for {paper}"),
            )

        mismatched_bundles = [b.arxiv_id for b in bundles if b.arxiv_id != paper]
        if mismatched_bundles:
            return StageResult(
                "labeling",
                paper,
                None,
                "data_error",
                utc_now(),
                schema_version=MATCHING_SCHEMA_VERSION,
                error=ErrorInfo(
                    "identity_mismatch",
                    f"Bundle arxiv_id mismatch: expected {paper}, got {mismatched_bundles}",
                ),
            )

        raw_labels = upstream.get("labels", [])
        labels = [
            lbl if isinstance(lbl, LabelRecord) else LabelRecord(**lbl)
            for lbl in raw_labels
        ]
        mismatched_labels = [lbl.arxiv_id for lbl in labels if lbl.arxiv_id != paper]
        if mismatched_labels:
            return StageResult(
                "labeling",
                paper,
                None,
                "data_error",
                utc_now(),
                schema_version=MATCHING_SCHEMA_VERSION,
                error=ErrorInfo(
                    "identity_mismatch",
                    f"Label arxiv_id mismatch: expected {paper}, got {mismatched_labels}",
                ),
            )

        if not labels and upstream.get("auto_label_if_empty"):
            from arxiv_pipeline.matching.heuristics import auto_label
            labels = auto_label(bundles, upstream.get("policy"))

        report = validate_label_manifest(bundles, labels)
        audit = audit_label_manifest(bundles, labels)

        labels_rel = f"matching/{key}/labels.json"
        audit_rel = f"matching/{key}/label_audit.json"
        save_labels_json(labels, out_root / labels_rel)
        atomic_json(out_root / audit_rel, audit)

        status = "success" if report.valid else "data_error"
        error = None if report.valid else ErrorInfo(
            "label_validation_failed",
            f"Label validation reported {len(report.errors)} error(s)",
        )
        return StageResult(
            "labeling",
            paper,
            None,
            status,
            utc_now(),
            schema_version=MATCHING_SCHEMA_VERSION,
            artifacts={"labels": labels_rel, "label_audit": audit_rel},
            error=error,
        )
    except (ContractError, KeyError, TypeError, ValueError) as exc:
        return StageResult(
            "labeling",
            paper,
            None,
            "data_error",
            utc_now(),
            schema_version=MATCHING_SCHEMA_VERSION,
            error=ErrorInfo("labeling_error", str(exc)),
        )
