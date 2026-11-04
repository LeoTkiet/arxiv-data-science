"""Milestone 2 QA Validator (TV5).

Enforces schema integrity, referential integrity, tree validity (DAG/single root per version),
and business rules across elements, hierarchy, bibtex tables, occurrences, and parsing manifest.
"""
from __future__ import annotations

import json
import re
from pathlib import Path, PureWindowsPath
from typing import Any

from arxiv_pipeline.common.schemas import (
    PARSING_SCHEMA_VERSION,
    ErrorInfo,
    StageResult,
)
from arxiv_pipeline.common.utils import atomic_json, paper_key, utc_now

VALID_KINDS = {
    "document",
    "heading",
    "itemize",
    "item",
    "sentence",
    "block_formula",
    "figure",
}

FORBIDDEN_HEADINGS = {"references", "bibliography"}

_CITE_RE = re.compile(
    r"\\(?:cite|citep|citet|citealp|citealt|citeauthor|citeyear|citeyearpar|citenum|nocite|"
    r"parencite|textcite|autocite|footcite|smartcite)\*?\s*(?:\[[^\]]*\]\s*){0,2}\{([^}]+)\}"
)


def safe_path(output_dir: Path, relative: str) -> Path:
    """Resolve and validate that a relative path does not escape output_dir and is not a symlink."""
    if not isinstance(relative, str) or not relative:
        raise ValueError("Artifact path must be a non-empty string")
    candidate = Path(relative)
    if candidate.is_absolute() or PureWindowsPath(relative).drive or ".." in relative.replace("\\", "/").split("/"):
        raise ValueError("Absolute or parent artifact paths are forbidden")
    path = (output_dir / candidate).resolve()
    if not path.is_relative_to(output_dir.resolve()):
        raise ValueError("Artifact path escapes output root")
    if any(p.is_symlink() for p in (path, *path.parents) if p != output_dir and output_dir in p.parents):
        raise ValueError("Symlink artifacts are forbidden")
    return path


def run(
    record: dict[str, Any],
    output_dir: Path | str,
    *,
    version: dict[str, Any] | None = None,
    upstream: dict[str, Any] | None = None,
) -> StageResult:
    """Validate one discovered paper's MS2 tables and emit validation/parsing/<paper_key>.json."""
    output = Path(output_dir).resolve()
    paper_id = record["arxiv_id"]
    key = paper_key(paper_id)

    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    def issue(category: str, code: str, message: str, *, warning: bool = False, details: Any = None):
        target = warnings if warning else errors
        target.append({
            "category": category,
            "code": code,
            "message": message,
            "paper_id": paper_id,
            "details": details,
        })

    def result(status: str, error: ErrorInfo | None = None) -> StageResult:
        rel_report = f"validation/parsing/{key}.json"
        return StageResult(
            stage="validate_parse",
            paper_id=paper_id,
            version=None,
            status=status,
            timestamp=utc_now(),
            artifacts={"validation": rel_report},
            error=error,
            schema_version=PARSING_SCHEMA_VERSION,
        )

    # 1. Upstream validation
    dedup_stage = (upstream or {}).get("stages", {}).get("deduplicate")
    context = (upstream or {}).get("context", {})
    input_snapshot_id = context.get("input_snapshot_id")

    if not isinstance(dedup_stage, dict):
        return result("skipped", ErrorInfo("upstream_missing", "Upstream deduplicate stage not provided"))

    dedup_status = dedup_stage.get("status")
    if dedup_status not in {"success", "no_source"}:
        return result("skipped", ErrorInfo("upstream_not_ready", f"Deduplicate stage status is {dedup_status}"))

    dedup_artifacts = dedup_stage.get("artifacts") or {}

    # 2. Read artifacts safely
    def read_json_artifact(name: str, fallback_relative: str) -> Any:
        rel = dedup_artifacts.get(name) or fallback_relative
        try:
            p = safe_path(output, rel)
            if not p.exists():
                issue("manifest", "missing_artifact", f"Required artifact {name} does not exist: {rel}")
                return None
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            issue("manifest", "unreadable_artifact", f"Failed reading {name} ({rel}): {exc}")
            return None

    manifest = read_json_artifact("parsing_manifest", f"tables/{key}/parsing_manifest.json")
    elements = read_json_artifact("elements", f"tables/{key}/elements.json")
    hierarchy = read_json_artifact("hierarchy", f"tables/{key}/hierarchy.json")
    bibtex = read_json_artifact("bibtex", f"tables/{key}/bibtex.json")

    # If critical files failed to read, exit with data_error
    if manifest is None or elements is None or hierarchy is None or bibtex is None:
        report_data = {
            "schema_version": PARSING_SCHEMA_VERSION,
            "arxiv_id": paper_id,
            "valid": False,
            "input_snapshot_id": input_snapshot_id,
            "status": "data_error",
            "counts": {},
            "checks": {"schema_integrity": False},
            "errors": errors,
            "warnings": warnings,
        }
        rel_report = f"validation/parsing/{key}.json"
        atomic_json(safe_path(output, rel_report), report_data)
        return result("data_error", ErrorInfo("read_failure", "; ".join(e["message"] for e in errors)))

    # 3. Validate parsing_manifest
    expected_versions = [v["version"] for v in record.get("versions", [])]
    if manifest.get("schema_version") != PARSING_SCHEMA_VERSION:
        issue("manifest", "invalid_schema_version", f"Manifest schema_version {manifest.get('schema_version')} != {PARSING_SCHEMA_VERSION}")
    if manifest.get("arxiv_id") != paper_id:
        issue("manifest", "mismatched_arxiv_id", f"Manifest arxiv_id {manifest.get('arxiv_id')} != {paper_id}")
    if manifest.get("complete") is not True:
        issue("manifest", "incomplete_manifest", "Manifest 'complete' flag is not True")
    if manifest.get("expected_versions") != expected_versions:
        issue("manifest", "mismatched_versions", f"Manifest expected_versions {manifest.get('expected_versions')} != {expected_versions}")

    manifest_versions = manifest.get("versions", [])
    if not isinstance(manifest_versions, list) or len(manifest_versions) != len(expected_versions):
        issue("manifest", "invalid_versions_list", "Manifest versions list does not match expected count")

    # Check references.bib if present or expected
    bibtex_file_rel = dedup_artifacts.get("bibtex_file") or f"tables/{key}/references.bib"
    try:
        bib_path = safe_path(output, bibtex_file_rel)
        if len(bibtex) > 0 and (not bib_path.exists() or bib_path.stat().st_size == 0):
            issue("schema", "empty_bibtex_file", "BibTeX table has entries but references.bib is missing or empty")
    except Exception as exc:
        issue("schema", "invalid_bibtex_file_path", str(exc))

    # 4. Check occurrences per version
    for v_info in manifest_versions if isinstance(manifest_versions, list) else []:
        v_num = v_info.get("version")
        v_status = v_info.get("status")
        occ_rel = (v_info.get("artifacts") or {}).get("occurrences")
        if not occ_rel:
            issue("manifest", "missing_occurrences_artifact", f"Version {v_num} has no occurrences artifact defined")
            continue
        try:
            occ_path = safe_path(output, occ_rel)
            if not occ_path.exists():
                issue("manifest", "occurrences_file_missing", f"Version {v_num} occurrences file missing: {occ_rel}")
                continue
            occ_data = json.loads(occ_path.read_text(encoding="utf-8"))
            occs = occ_data.get("occurrences", [])
            if v_status == "no_source" and len(occs) > 0:
                issue("manifest", "no_source_has_occurrences", f"Version {v_num} is no_source but has occurrences")
            elif v_status == "success":
                # Check occurrence ID uniqueness
                occ_ids = set()
                occ_by_parent: dict[str | None, list[dict[str, Any]]] = {}
                for occ in occs:
                    oid = occ.get("occurrence_id")
                    if not oid or oid in occ_ids:
                        issue("manifest", "duplicate_occurrence_id", f"Version {v_num} has duplicate/empty occurrence_id {oid}")
                    occ_ids.add(oid)
                    p_id = occ.get("parent_occurrence_id")
                    occ_by_parent.setdefault(p_id, []).append(occ)

                # Check position continuity 0..k-1 per parent
                for p_id, children in occ_by_parent.items():
                    positions = [c.get("position") for c in children]
                    if sorted(positions) != list(range(len(children))):
                        issue("manifest", "non_continuous_positions", f"Version {v_num} under parent {p_id} positions not continuous 0..{len(children)-1}")
        except Exception as exc:
            issue("manifest", "unreadable_occurrences", f"Version {v_num} occurrences error: {exc}")

    # 5. Schema Integrity Check for elements
    if not isinstance(elements, list):
        issue("schema", "invalid_elements_table", "elements must be a JSON list")
        elements = []

    element_ids: set[str] = set()
    element_by_id: dict[str, dict[str, Any]] = {}
    content_kind_seen: set[tuple[str, str]] = set()

    for idx, row in enumerate(elements):
        if not isinstance(row, dict):
            issue("schema", "malformed_element_row", f"Element row {idx} is not an object")
            continue
        eid = row.get("element_id")
        row_arxiv_id = row.get("arxiv_id")
        kind = row.get("kind")
        content = row.get("content")

        if not isinstance(eid, str) or not eid.strip():
            issue("schema", "empty_element_id", f"Row {idx} element_id is empty or non-string")
        elif eid in element_ids:
            issue("schema", "duplicate_element_id", f"Duplicate element_id: {eid}")
        else:
            element_ids.add(eid)
            element_by_id[eid] = row

        if row_arxiv_id != paper_id:
            issue("schema", "mismatched_element_arxiv_id", f"Element {eid} arxiv_id {row_arxiv_id} != {paper_id}")

        if kind not in VALID_KINDS:
            issue("schema", "invalid_element_kind", f"Element {eid} kind '{kind}' not in {VALID_KINDS}")

        if not isinstance(content, str):
            issue("schema", "non_string_content", f"Element {eid} content is not a string")
        else:
            # Business rule: heading cannot be 'References' or 'Bibliography'
            if kind == "heading":
                norm_h = content.strip().lower()
                if norm_h in FORBIDDEN_HEADINGS:
                    issue("business", "forbidden_heading", f"Heading element {eid} has forbidden title '{content}'")

            # Business rule: (kind, content) uniqueness across paper for non-document rows
            if kind != "document":
                pair = (kind, content)
                if pair in content_kind_seen:
                    issue("business", "duplicate_kind_content", f"Duplicate non-document (kind, content) for kind '{kind}': {content[:50]}...")
                content_kind_seen.add(pair)

    # 6. Schema & Referential Integrity Check for hierarchy
    if not isinstance(hierarchy, list):
        issue("schema", "invalid_hierarchy_table", "hierarchy must be a JSON list")
        hierarchy = []

    # Exactly 4 columns per row
    EXPECTED_HIERARCHY_KEYS = {"arxiv_id", "version", "element_id", "parent_id"}
    hierarchy_by_version: dict[int, list[dict[str, Any]]] = {}

    for idx, row in enumerate(hierarchy):
        if not isinstance(row, dict):
            issue("schema", "malformed_hierarchy_row", f"Hierarchy row {idx} is not an object")
            continue
        if set(row.keys()) != EXPECTED_HIERARCHY_KEYS:
            issue("schema", "invalid_hierarchy_columns", f"Hierarchy row {idx} keys {set(row.keys())} != {EXPECTED_HIERARCHY_KEYS}")

        h_arxiv_id = row.get("arxiv_id")
        h_ver = row.get("version")
        child_id = row.get("element_id")
        parent_id = row.get("parent_id")

        if h_arxiv_id != paper_id:
            issue("schema", "mismatched_hierarchy_arxiv_id", f"Hierarchy row {idx} arxiv_id {h_arxiv_id} != {paper_id}")

        if type(h_ver) is not int or h_ver < 1:
            issue("schema", "invalid_hierarchy_version", f"Hierarchy row {idx} version {h_ver} is not an int >= 1")
        elif h_ver not in expected_versions:
            issue("schema", "unexpected_hierarchy_version", f"Hierarchy version {h_ver} not in discovery versions")

        if not isinstance(child_id, str) or not child_id.strip():
            issue("schema", "empty_hierarchy_element_id", f"Hierarchy row {idx} element_id is empty")
        elif child_id not in element_ids:
            issue("reference", "foreign_key_missing_child", f"Hierarchy element_id '{child_id}' not found in elements")

        if not isinstance(parent_id, str) or not parent_id.strip():
            issue("schema", "empty_hierarchy_parent_id", f"Hierarchy row {idx} parent_id is empty")
        elif parent_id not in element_ids:
            issue("reference", "foreign_key_missing_parent", f"Hierarchy parent_id '{parent_id}' not found in elements")

        if child_id == parent_id:
            issue("tree", "self_loop", f"Hierarchy row {idx} contains self-loop: {child_id} == {parent_id}")

        if type(h_ver) is int:
            hierarchy_by_version.setdefault(h_ver, []).append(row)

    # 7. Tree Validity per version
    for v_info in manifest_versions if isinstance(manifest_versions, list) else []:
        v_num = v_info.get("version")
        v_status = v_info.get("status")
        v_rows = hierarchy_by_version.get(v_num, [])

        if v_status == "no_source":
            if v_rows:
                issue("tree", "no_source_has_hierarchy", f"Version {v_num} is no_source but has hierarchy rows")
            continue

        # For versions with source:
        # Every child can only have ONE parent in the same version
        parent_map: dict[str, str] = {}
        for r in v_rows:
            c = r["element_id"]
            p = r["parent_id"]
            if c in parent_map:
                issue("tree", "multiple_parents", f"Version {v_num} node {c} has multiple parents: {parent_map[c]} and {p}")
            parent_map[c] = p

        # Cycle detection
        for start_node in parent_map:
            visited_path = []
            curr = start_node
            while curr in parent_map:
                if curr in visited_path:
                    cycle = " -> ".join(visited_path[visited_path.index(curr):] + [curr])
                    issue("tree", "cycle_detected", f"Version {v_num} contains cycle: {cycle}")
                    break
                visited_path.append(curr)
                curr = parent_map[curr]

        # Root document check:
        # The version must have a root document node `#root{v_num}`
        expected_root_suffix = f"root{v_num}"
        root_nodes = [
            eid for eid, row in element_by_id.items()
            if row.get("kind") == "document" and eid.endswith(expected_root_suffix)
        ]
        if not root_nodes:
            # Fallback: check if any document kind exists for this version
            root_nodes = [
                eid for eid, row in element_by_id.items()
                if row.get("kind") == "document" and f"root{v_num}" in eid
            ]

        if len(root_nodes) != 1:
            issue("tree", "missing_root_document", f"Version {v_num} must have exactly 1 root document node, found {len(root_nodes)}: {root_nodes}")
        else:
            root_id = root_nodes[0]
            # Root must not be a child in hierarchy
            if root_id in parent_map:
                issue("tree", "root_has_parent", f"Root node {root_id} for version {v_num} cannot have a parent in hierarchy")

            # All nodes in hierarchy must reach root
            for start_node in parent_map:
                curr = start_node
                hops = 0
                while curr in parent_map and hops < len(parent_map) + 2:
                    curr = parent_map[curr]
                    hops += 1
                if curr != root_id and hops <= len(parent_map):
                    issue("tree", "disconnected_from_root", f"Version {v_num} node {start_node} terminates at {curr} instead of root {root_id}")

    # 8. Schema & Integrity Check for bibtex
    if not isinstance(bibtex, list):
        issue("schema", "invalid_bibtex_table", "bibtex must be a JSON list")
        bibtex = []

    bibtex_keys: set[str] = set()
    REQUIRED_BIB_FIELDS = ("arxiv_id", "citation_key", "entry_type", "title", "authors", "year")

    for idx, row in enumerate(bibtex):
        if not isinstance(row, dict):
            issue("schema", "malformed_bibtex_row", f"BibTeX row {idx} is not an object")
            continue

        for req in REQUIRED_BIB_FIELDS:
            if req not in row:
                issue("schema", "missing_bibtex_field", f"BibTeX row {idx} missing required field '{req}'")

        b_arxiv_id = row.get("arxiv_id")
        ckey = row.get("citation_key")
        etype = row.get("entry_type")
        authors = row.get("authors")
        year = row.get("year")

        if b_arxiv_id != paper_id:
            issue("schema", "mismatched_bibtex_arxiv_id", f"BibTeX row {idx} arxiv_id {b_arxiv_id} != {paper_id}")

        if not isinstance(ckey, str) or not ckey.strip():
            issue("schema", "empty_citation_key", f"BibTeX row {idx} citation_key is empty")
        elif ckey in bibtex_keys:
            issue("schema", "duplicate_citation_key", f"Duplicate citation_key in bibtex table: {ckey}")
        else:
            bibtex_keys.add(ckey)

        if not isinstance(etype, str) or not etype.strip():
            issue("schema", "empty_entry_type", f"BibTeX row {idx} entry_type is empty")

        if not isinstance(authors, list) or not all(isinstance(a, str) for a in authors):
            issue("schema", "invalid_authors_type", f"BibTeX key {ckey} authors must be a list of strings")

        if year is not None and type(year) is not int:
            issue("schema", "invalid_year_type", f"BibTeX key {ckey} year must be int or None, got {type(year)}")

    # 9. Citation Key Synchronization Check
    all_cited_keys: set[str] = set()
    for row in elements:
        if row.get("kind") == "sentence":
            content = row.get("content", "")
            for match in _CITE_RE.finditer(content):
                for k in match.group(1).split(","):
                    k_clean = k.strip()
                    if k_clean:
                        all_cited_keys.add(k_clean)

    # Check cited keys against bibtex
    missing_cites = all_cited_keys - bibtex_keys
    if missing_cites:
        # Check if they are recorded in manifest warnings as citation_key_unknown
        manifest_warnings = manifest.get("warnings", [])
        unknown_warning_keys = set()
        for w in manifest_warnings:
            if w.get("code") == "citation_key_unknown" and w.get("message"):
                unknown_warning_keys.update(k.strip() for k in w["message"].split(","))

        unaccounted = missing_cites - unknown_warning_keys
        if unaccounted:
            issue("business", "unresolved_citations", f"Citations {sorted(unaccounted)} in text not found in bibtex or declared unknown", warning=True)

    # 10. Deduplication Counts Verification
    manifest_counts = manifest.get("counts", {})
    if manifest_counts.get("elements_after") != len(elements):
        issue("manifest", "mismatched_element_counts", f"Manifest elements_after {manifest_counts.get('elements_after')} != len(elements) {len(elements)}")
    if manifest_counts.get("bibtex_after") != len(bibtex):
        issue("manifest", "mismatched_bibtex_counts", f"Manifest bibtex_after {manifest_counts.get('bibtex_after')} != len(bibtex) {len(bibtex)}")

    # Forward any manifest warnings to report
    for w in manifest.get("warnings", []):
        warnings.append({
            "category": "upstream_warning",
            "code": w.get("code", "warning"),
            "message": w.get("message", ""),
            "paper_id": paper_id,
            "details": w,
        })

    # 11. Build and write atomic validation report
    valid = len(errors) == 0
    all_empty = all(v.get("status") == "no_source" for v in manifest_versions)
    final_status = "no_source" if (valid and all_empty) else ("success" if valid else "data_error")

    report_data = {
        "schema_version": PARSING_SCHEMA_VERSION,
        "arxiv_id": paper_id,
        "valid": valid,
        "input_snapshot_id": input_snapshot_id,
        "status": final_status,
        "counts": {
            "versions": len(expected_versions),
            "elements": len(elements),
            "hierarchy_edges": len(hierarchy),
            "bibtex_entries": len(bibtex),
            "elements_before_dedup": manifest_counts.get("elements_before", len(elements)),
            "elements_after_dedup": len(elements),
            "bibtex_before_dedup": manifest_counts.get("bibtex_before", len(bibtex)),
            "bibtex_after_dedup": len(bibtex),
        },
        "checks": {
            "schema_integrity": not any(e["category"] == "schema" for e in errors),
            "referential_integrity": not any(e["category"] == "reference" for e in errors),
            "tree_validity": not any(e["category"] == "tree" for e in errors),
            "business_rules": not any(e["category"] == "business" for e in errors),
            "manifest_and_occurrences": not any(e["category"] == "manifest" for e in errors),
        },
        "errors": errors,
        "warnings": warnings,
    }

    rel_report = f"validation/parsing/{key}.json"
    atomic_json(safe_path(output, rel_report), report_data)

    err_info = ErrorInfo("validation_failed", "; ".join(e["message"] for e in errors[:5])) if errors else None
    return result(final_status, err_info)

