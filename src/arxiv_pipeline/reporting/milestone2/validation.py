"""Milestone 2 QA Validator (TV5).

Enforces:
1. Schema integrity across elements, hierarchy, bibtex tables, references.bib, occurrences, and parsing_manifest.
2. Exact snapshot match (both context.input_snapshot_id and manifest.input_snapshot_id must be non-empty and equal).
3. No fallback default paths when artifacts are not explicitly declared by upstream deduplication.
4. Manifest version identity & status verification ([v["version"] ...] == expected_versions, status in {"success", "no_source"}).
5. Full parsing and 1-to-1 verification of references.bib against bibtex.json.
6. Occurrence tree validity (unique ID, valid root, position continuity 0..k-1, cycle-free DAG).
7. Exact multiset equivalence between occurrences edges and hierarchy rows (supporting deduplicated repeated elements).
8. Business rules: no 'References'/'Bibliography' headings, unique (kind, content) across non-document elements, and citation synchronization.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path, PureWindowsPath
from typing import Any

from arxiv_pipeline.common.schemas import (
    PARSING_SCHEMA_VERSION,
    ErrorInfo,
    StageResult,
)
from arxiv_pipeline.common.utils import atomic_json, paper_key, utc_now
from arxiv_pipeline.parsing.bibliography import parse_bib_text
from arxiv_pipeline.parsing.deduplication import normalize_doi, normalize_title

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

    dedup_artifacts = dedup_stage.get("artifacts")
    if not isinstance(dedup_artifacts, dict):
        return result("data_error", ErrorInfo("missing_artifacts_map", "Deduplicate stage did not provide artifacts map"))

    # 2. Strict artifact declaration check: NEVER fall back to unauthenticated default paths
    REQUIRED_DEDUP_ARTIFACTS = ("elements", "hierarchy", "bibtex", "bibtex_file", "parsing_manifest")
    for req in REQUIRED_DEDUP_ARTIFACTS:
        if req not in dedup_artifacts or not dedup_artifacts[req]:
            issue("manifest", "missing_artifact_declaration", f"Deduplicate stage did not declare artifact '{req}'")

    if errors:
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
        return result("data_error", ErrorInfo("unauthenticated_artifacts", "; ".join(e["message"] for e in errors)))

    def read_declared_json(name: str) -> Any:
        rel = dedup_artifacts[name]
        try:
            p = safe_path(output, rel)
            if not p.exists():
                issue("manifest", "missing_artifact_file", f"Declared artifact file for '{name}' does not exist: {rel}")
                return None
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            issue("manifest", "unreadable_artifact", f"Failed reading declared artifact '{name}' ({rel}): {exc}")
            return None

    manifest = read_declared_json("parsing_manifest")
    elements = read_declared_json("elements")
    hierarchy = read_declared_json("hierarchy")
    bibtex = read_declared_json("bibtex")

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

    # 3. Snapshot verification: both context and manifest must provide non-empty snapshot_id and match
    manifest_snapshot = manifest.get("input_snapshot_id")
    if not isinstance(input_snapshot_id, str) or not input_snapshot_id.strip():
        issue("manifest", "missing_snapshot_id", "Upstream context missing or empty input_snapshot_id")
    elif not isinstance(manifest_snapshot, str) or not manifest_snapshot.strip():
        issue("manifest", "missing_snapshot_id", "Manifest missing or empty input_snapshot_id")
    elif manifest_snapshot != input_snapshot_id:
        issue("manifest", "snapshot_mismatch",
              f"Manifest input_snapshot_id '{manifest_snapshot}' does not match current snapshot '{input_snapshot_id}'")

    # 4. Parsing manifest verification
    expected_versions = [v["version"] for v in record.get("versions", [])]
    if manifest.get("schema_version") != PARSING_SCHEMA_VERSION:
        issue("manifest", "invalid_schema_version", f"Manifest schema_version {manifest.get('schema_version')} != {PARSING_SCHEMA_VERSION}")
    if manifest.get("arxiv_id") != paper_id:
        issue("manifest", "mismatched_arxiv_id", f"Manifest arxiv_id {manifest.get('arxiv_id')} != {paper_id}")
    if manifest.get("complete") is not True:
        issue("manifest", "incomplete_manifest", "Manifest 'complete' flag is not True")
    if manifest.get("expected_versions") != expected_versions:
        issue("manifest", "mismatched_versions", f"Manifest expected_versions {manifest.get('expected_versions')} != {expected_versions}")

    # Check manifest.tables declarations
    manifest_tables = manifest.get("tables", {})
    REQUIRED_TABLES = ("elements", "hierarchy", "bibtex", "bibtex_file")
    if not isinstance(manifest_tables, dict):
        issue("manifest", "invalid_manifest_tables", "Manifest tables mapping is missing or not a dict")
    else:
        for t_name in REQUIRED_TABLES:
            if manifest_tables.get(t_name) != dedup_artifacts.get(t_name):
                issue("manifest", "manifest_table_path_mismatch",
                      f"Manifest table path for '{t_name}' ({manifest_tables.get(t_name)}) != declared artifact ({dedup_artifacts.get(t_name)})")

    # Version list and status verification
    manifest_versions = manifest.get("versions", [])
    if not isinstance(manifest_versions, list):
        issue("manifest", "invalid_versions_list", "Manifest versions must be a list")
        manifest_versions = []
    else:
        actual_versions = []
        for v_idx, v_info in enumerate(manifest_versions):
            if not isinstance(v_info, dict):
                issue("manifest", "malformed_version_info", f"Manifest version entry at index {v_idx} is not an object")
                continue
            v_num = v_info.get("version")
            v_status = v_info.get("status")
            actual_versions.append(v_num)
            if v_status not in {"success", "no_source"}:
                issue("manifest", "invalid_version_status", f"Version {v_num} status '{v_status}' not in {{'success', 'no_source'}}")
        if actual_versions != expected_versions:
            issue("manifest", "invalid_versions_list", f"Manifest versions {actual_versions} do not match expected {expected_versions}")

    # Parse and deeply verify references.bib against bibtex.json
    bibtex_file_rel = dedup_artifacts["bibtex_file"]
    try:
        bib_path = safe_path(output, bibtex_file_rel)
        if not bib_path.exists():
            issue("schema", "missing_bibtex_file", f"Declared references.bib file does not exist: {bibtex_file_rel}")
        else:
            bib_text = bib_path.read_text(encoding="utf-8")
            parsed_bib_entries, bib_warnings = parse_bib_text(bib_text)
            for bw in bib_warnings:
                code = bw.get("code")
                if code in ("bib_entries_unparsed", "bib_entry_without_key"):
                    issue("schema", code, bw.get("message", "BibTeX parse error in references.bib"))
                else:
                    warnings.append(dict(bw, category="bib_warning", paper_id=paper_id))

            if len(parsed_bib_entries) != len(bibtex):
                issue("schema", "bibtex_file_entry_count_mismatch",
                      f"references.bib parsed {len(parsed_bib_entries)} entries != bibtex.json {len(bibtex)} entries")
            else:
                parsed_by_key = {}
                for p_entry in parsed_bib_entries:
                    k = p_entry.get("citation_key")
                    if k in parsed_by_key:
                        issue("schema", "duplicate_citation_key_in_bib_file", f"Duplicate citation key in references.bib: {k}")
                    parsed_by_key[k] = p_entry

                for brow in bibtex:
                    bkey = brow.get("citation_key")
                    if bkey not in parsed_by_key:
                        issue("schema", "bibtex_file_missing_key", f"citation_key '{bkey}' from bibtex.json not found in references.bib")
                        continue
                    p_entry = parsed_by_key[bkey]
                    if brow.get("entry_type", "").lower() != p_entry.get("entry_type", "").lower():
                        issue("schema", "bibtex_field_mismatch",
                              f"Key '{bkey}' entry_type mismatch: bibtex.json '{brow.get('entry_type')}' != references.bib '{p_entry.get('entry_type')}'")
                    if brow.get("year") != p_entry.get("year"):
                        issue("schema", "bibtex_field_mismatch",
                              f"Key '{bkey}' year mismatch: bibtex.json {brow.get('year')} != references.bib {p_entry.get('year')}")
                    if [a.strip().lower() for a in brow.get("authors", [])] != [a.strip().lower() for a in p_entry.get("authors", [])]:
                        issue("schema", "bibtex_field_mismatch",
                              f"Key '{bkey}' authors mismatch: bibtex.json {brow.get('authors')} != references.bib {p_entry.get('authors')}")
                    if normalize_title(brow.get("title", "")) != normalize_title(p_entry.get("title", "")):
                        issue("schema", "bibtex_field_mismatch",
                              f"Key '{bkey}' title mismatch: bibtex.json '{brow.get('title')}' != references.bib '{p_entry.get('title')}'")
                    for opt_field in ("doi", "journal", "url"):
                        v_json = (brow.get(opt_field) or "").strip()
                        v_bib = (p_entry.get(opt_field) or "").strip()
                        if opt_field == "doi":
                            if normalize_doi(v_json) != normalize_doi(v_bib):
                                issue("schema", "bibtex_field_mismatch", f"Key '{bkey}' doi mismatch: '{v_json}' != '{v_bib}'")
                        else:
                            if v_json.lower() != v_bib.lower():
                                issue("schema", "bibtex_field_mismatch", f"Key '{bkey}' {opt_field} mismatch: '{v_json}' != '{v_bib}'")
    except Exception as exc:
        issue("schema", "invalid_bibtex_file", str(exc))

    # 5. Schema Integrity for elements table
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
            if kind == "heading":
                norm_h = content.strip().lower()
                if norm_h in FORBIDDEN_HEADINGS:
                    issue("business", "forbidden_heading", f"Heading element {eid} has forbidden title '{content}'")

            if kind != "document":
                pair = (kind, content)
                if pair in content_kind_seen:
                    issue("business", "duplicate_kind_content", f"Duplicate non-document (kind, content) for kind '{kind}': {content[:50]}...")
                content_kind_seen.add(pair)

    # 6. Schema Integrity for hierarchy table
    if not isinstance(hierarchy, list):
        issue("schema", "invalid_hierarchy_table", "hierarchy must be a JSON list")
        hierarchy = []

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

        if type(h_ver) is int:
            hierarchy_by_version.setdefault(h_ver, []).append(row)

    # 7. Deep Occurrences Tree Validation & Exact Multiset Correspondence with Hierarchy
    prefix = paper_id.replace(".", "-").replace("/", "_") + "#"

    for v_info in manifest_versions if isinstance(manifest_versions, list) else []:
        v_num = v_info.get("version")
        v_status = v_info.get("status")
        occ_rel = (v_info.get("artifacts") or {}).get("occurrences")
        v_hier_rows = hierarchy_by_version.get(v_num, [])

        if not occ_rel:
            issue("manifest", "missing_occurrences_artifact", f"Version {v_num} has no occurrences artifact defined")
            continue

        try:
            occ_path = safe_path(output, occ_rel)
            if not occ_path.exists():
                issue("manifest", "occurrences_file_missing", f"Version {v_num} occurrences file missing: {occ_rel}")
                continue
            occ_data = json.loads(occ_path.read_text(encoding="utf-8"))
        except Exception as exc:
            issue("manifest", "unreadable_occurrences", f"Version {v_num} occurrences error: {exc}")
            continue

        if occ_data.get("schema_version") != PARSING_SCHEMA_VERSION:
            issue("manifest", "invalid_occurrences_schema", f"Version {v_num} occurrences schema {occ_data.get('schema_version')} != {PARSING_SCHEMA_VERSION}")
        if occ_data.get("arxiv_id") != paper_id:
            issue("manifest", "invalid_occurrences_arxiv_id", f"Version {v_num} occurrences arxiv_id != {paper_id}")
        if occ_data.get("version") != v_num:
            issue("manifest", "invalid_occurrences_version", f"Version {v_num} occurrences version field != {v_num}")

        occs = occ_data.get("occurrences", [])

        if v_status == "no_source":
            if occs:
                issue("manifest", "no_source_has_occurrences", f"Version {v_num} is no_source but has occurrences")
            if v_hier_rows:
                issue("tree", "no_source_has_hierarchy", f"Version {v_num} is no_source but has hierarchy rows")
            continue

        if not occs:
            issue("manifest", "empty_occurrences_for_success_version", f"Version {v_num} status is success but occurrences list is empty")
            continue

        occ_by_id: dict[str, dict[str, Any]] = {}
        children_by_parent: dict[str | None, list[dict[str, Any]]] = {}
        root_occs: list[dict[str, Any]] = []

        for occ in occs:
            oid = occ.get("occurrence_id")
            p_oid = occ.get("parent_occurrence_id")
            pos = occ.get("position")
            elem_id = occ.get("element_id")

            if not isinstance(oid, str) or not oid.strip():
                issue("manifest", "empty_occurrence_id", f"Version {v_num} contains empty occurrence_id")
                continue
            if oid in occ_by_id:
                issue("manifest", "duplicate_occurrence_id", f"Version {v_num} has duplicate occurrence_id: {oid}")
            occ_by_id[oid] = occ

            if elem_id not in element_ids:
                issue("reference", "occurrence_missing_element", f"Occurrence {oid} references unknown element {elem_id}")

            if p_oid is None:
                root_occs.append(occ)
            else:
                children_by_parent.setdefault(p_oid, []).append(occ)

        expected_root_elem = f"{prefix}root{v_num}"
        if len(root_occs) != 1:
            issue("tree", "multiple_root_occurrences", f"Version {v_num} must have exactly 1 root occurrence (parent=None), found {len(root_occs)}")
        else:
            root_occ = root_occs[0]
            if root_occ.get("position") != 0:
                issue("tree", "root_occurrence_position_nonzero", f"Version {v_num} root occurrence position is {root_occ.get('position')} != 0")
            if root_occ.get("element_id") != expected_root_elem:
                issue("tree", "root_occurrence_element_mismatch",
                      f"Version {v_num} root occurrence element {root_occ.get('element_id')} != expected {expected_root_elem}")

        # Cycle detection and ancestor reachability in occurrence tree
        for oid, occ in occ_by_id.items():
            curr = occ
            visited = [oid]
            while curr.get("parent_occurrence_id") is not None:
                p_oid = curr["parent_occurrence_id"]
                if p_oid not in occ_by_id:
                    issue("tree", "occurrence_missing_parent", f"Occurrence {curr['occurrence_id']} parent {p_oid} not found in version {v_num}")
                    break
                if p_oid in visited:
                    cycle = " -> ".join(visited[visited.index(p_oid):] + [p_oid])
                    issue("tree", "occurrence_cycle", f"Version {v_num} occurrence tree has cycle: {cycle}")
                    break
                visited.append(p_oid)
                curr = occ_by_id[p_oid]

        # Position continuity 0..k-1 per parent
        for p_oid, children in children_by_parent.items():
            positions = [c.get("position") for c in children]
            if sorted(positions) != list(range(len(children))):
                issue("tree", "non_continuous_occurrence_positions",
                      f"Version {v_num} parent {p_oid} children positions {positions} != expected 0..{len(children)-1}")

        # Exact Multiset Correspondence: occurrences edges vs hierarchy rows
        occ_edges = []
        for occ in occs:
            p_oid = occ.get("parent_occurrence_id")
            if p_oid is not None and p_oid in occ_by_id:
                child_elem = occ.get("element_id")
                parent_elem = occ_by_id[p_oid].get("element_id")
                if child_elem and parent_elem:
                    occ_edges.append((v_num, child_elem, parent_elem))

        hier_edges = [(r["version"], r["element_id"], r["parent_id"]) for r in v_hier_rows]

        occ_counter = Counter(occ_edges)
        hier_counter = Counter(hier_edges)

        if occ_counter != hier_counter:
            missing_in_hier = occ_counter - hier_counter
            surplus_in_hier = hier_counter - occ_counter
            issue("tree", "hierarchy_occurrence_mismatch",
                  f"Version {v_num} hierarchy rows do not match occurrence edges. "
                  f"Missing in hierarchy: {dict(missing_in_hier)}; Surplus in hierarchy: {dict(surplus_in_hier)}")

    # 8. Schema & Integrity for bibtex table
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

    missing_cites = all_cited_keys - bibtex_keys
    if missing_cites:
        manifest_warnings = manifest.get("warnings", [])
        unknown_warning_keys = set()
        for w in manifest_warnings:
            if w.get("code") == "citation_key_unknown" and w.get("message"):
                unknown_warning_keys.update(k.strip() for k in w["message"].split(","))

        unaccounted = missing_cites - unknown_warning_keys
        if unaccounted:
            issue("business", "unresolved_citations",
                  f"Citations {sorted(unaccounted)} in text not found in bibtex or declared unknown in warnings",
                  warning=True)

    # 10. Deduplication Counts Verification
    manifest_counts = manifest.get("counts", {})
    if manifest_counts.get("elements_after") != len(elements):
        issue("manifest", "mismatched_element_counts",
              f"Manifest elements_after {manifest_counts.get('elements_after')} != len(elements) {len(elements)}")
    if manifest_counts.get("bibtex_after") != len(bibtex):
        issue("manifest", "mismatched_bibtex_counts",
              f"Manifest bibtex_after {manifest_counts.get('bibtex_after')} != len(bibtex) {len(bibtex)}")

    # Forward any upstream warnings
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
