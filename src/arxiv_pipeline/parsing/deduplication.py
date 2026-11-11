"""Cross-version deduplication for Milestone 2 (TV4).

Level 1 (references): entries that name the same work (same key, same DOI or same normalized
title) are merged into one row with a single chosen key (the earliest one); fields are unionized,
never picked from a single entry; every `\\cite{old}` is renamed to the chosen key.
Level 2 (full text): after the rename, elements with the same kind and exactly the same cleaned
content share one `element_id`; every version keeps its own parent edges.
Complies with docs/MS2_CONTRACTS.md: stage `deduplicate`, paper level, output under `tables/<paper_key>/`.
"""
import json
import os
import re
import tempfile
from dataclasses import asdict
from pathlib import Path

from arxiv_pipeline.common.schemas import (
    BibtexRow, ElementRow, ErrorInfo, HierarchyRow, PARSING_SCHEMA_VERSION, StageResult,
)
from arxiv_pipeline.common.utils import atomic_json, paper_key, utc_now
from arxiv_pipeline.parsing.bibliography import safe_join

STAGE = "deduplicate"
MIN_TITLE_LENGTH = 15                      # shorter normalized titles are too generic to prove identity
FIELDS = ("entry_type", "title", "authors", "year", "doi", "journal", "url")
ID_PREFIX = {"heading": "h", "sentence": "s", "block_formula": "eq", "figure": "fig", "itemize": "list", "item": "item"}
_CITE = re.compile(
    r"(\\(?:cite|citep|citet|citealp|citealt|citeauthor|citeyear|citeyearpar|citenum|nocite|"
    r"parencite|textcite|autocite|footcite|smartcite)\*?\s*(?:\[[^\]]*\]\s*){0,2}\{)([^}]*)(\})")


# ---------------------------------------------------------------- level 1: references
def normalize_title(title: str) -> str:
    title = re.sub(r"\\[a-zA-Z]+", " ", title or "")
    return re.sub(r"\s+", " ", re.sub(r"[^0-9a-z]+", " ", title.lower())).strip()


def normalize_doi(doi) -> str:
    return re.sub(r"^https?://(?:dx\.)?doi\.org/", "", (doi or "").strip(), flags=re.I).lower()


def _empty(value) -> bool:
    return value in (None, "", [])


def _same(name, a, b) -> bool:
    """Equal up to formatting: case/punctuation in titles, DOI prefix and case, author case."""
    if name == "title":
        return normalize_title(a) == normalize_title(b)
    if name == "doi":
        return normalize_doi(a) == normalize_doi(b)
    if name == "authors":
        return [x.lower() for x in a] == [x.lower() for x in b]
    return a == b


def unionize_bibtex(entries_by_version):
    """Merge duplicate references of one paper.

    `entries_by_version`: list of (version, [BibtexRow dict, ...]) in ascending version order.
    Returns (rows, key_mapping, conflicts): merged row dicts, {dropped_key: chosen_key} and the
    field values that lost against an earlier entry.
    """
    flat = [(v, e) for v, entries in entries_by_version for e in entries]
    parent = list(range(len(flat)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    seen = {}
    for index, (_, entry) in enumerate(flat):
        title = normalize_title(entry.get("title"))
        signals = [("key", entry["citation_key"]), ("doi", normalize_doi(entry.get("doi")))]
        if len(title) >= MIN_TITLE_LENGTH:
            signals.append(("title", title))
        for signal in signals:
            if not signal[1]:
                continue
            if signal in seen:
                parent[find(index)] = find(seen[signal])
            else:
                seen[signal] = index
    groups = {}
    for index in range(len(flat)):
        groups.setdefault(find(index), []).append(index)

    rows, key_mapping, conflicts = [], {}, []
    for members in sorted(groups.values(), key=min):
        members.sort()
        first = flat[members[0]][1]
        merged = {name: first.get(name) for name in FIELDS}
        chosen = first["citation_key"]
        for index in members[1:]:
            other = flat[index][1]
            for name in FIELDS:
                value = other.get(name)
                if _empty(value):
                    continue
                if _empty(merged[name]) or (name == "entry_type" and merged[name] == "misc" and value != "misc"):
                    merged[name] = value
                elif name != "entry_type" and not _same(name, value, merged[name]):
                    conflicts.append(dict(citation_key=chosen, field=name, kept=merged[name], dropped=value,
                                          source_key=other["citation_key"]))
            if other["citation_key"] != chosen:
                key_mapping[other["citation_key"]] = chosen
        rows.append(dict(arxiv_id=first["arxiv_id"], citation_key=chosen, **merged))
    return rows, key_mapping, conflicts


def rename_citations(text: str, key_mapping: dict, known_keys=None):
    """Rewrite the keys inside every cite command. Returns (new_text, unknown_keys)."""
    unknown = set()

    def keys(match):
        out = []
        for token in re.split(r"(\s*,\s*)", match.group(2)):
            if token.strip() in ("", ",") or re.fullmatch(r"\s*,\s*", token):
                out.append(token)
                continue
            key = token.strip()
            if known_keys is not None and key not in known_keys and key not in key_mapping:
                unknown.add(key)
            out.append(token.replace(key, key_mapping.get(key, key)))
        # a rename can make two keys of one command identical: keep the first
        seen, final = set(), []
        for token in out:
            if token.strip() in ("", ",") or re.fullmatch(r"\s*,\s*", token):
                final.append(token)
            elif token.strip() not in seen:
                seen.add(token.strip())
                final.append(token)
            elif final and re.fullmatch(r"\s*,\s*", final[-1]):
                final.pop()
        return match.group(1) + "".join(final) + match.group(3)

    return _CITE.sub(keys, text), unknown


def render_bibtex(rows) -> str:
    """Standard BibTeX text of the merged table."""
    def value(text):
        return str(text).replace("{", "").replace("}", "")

    chunks = []
    for row in rows:
        lines = []
        for field, text in (("title", row["title"]), ("author", " and ".join(row["authors"])),
                            ("year", row["year"]), ("doi", row["doi"]), ("journal", row["journal"]), ("url", row["url"])):
            if not _empty(text):
                lines.append(f"  {field} = {{{value(text)}}}")
        if not lines:
            # Note-only source references can have no fields in the table schema.
            # An explicit empty title preserves the key and remains parseable.
            lines.append("  title = {}")
        chunks.append(f"@{row['entry_type']}{{{row['citation_key']},\n" + ",\n".join(lines) + "\n}\n")
    return "\n".join(chunks)


# ---------------------------------------------------------------- level 2: full text
def _preorder(version: int, nodes):
    """Validate one normalized tree and return its nodes parents-first, children by position."""
    if not nodes:
        return []
    by_id = {}
    for node in nodes:
        if node["temp_id"] in by_id:
            raise ValueError(f"v{version}: duplicate temp_id {node['temp_id']!r}")
        by_id[node["temp_id"]] = node
    roots = [n for n in nodes if n["parent_temp_id"] is None]
    if len(roots) != 1 or roots[0]["kind"] != "document":
        raise ValueError(f"v{version}: a tree needs exactly one document root")
    children = {}
    for node in nodes:
        parent = node["parent_temp_id"]
        if parent is not None:
            if parent not in by_id:
                raise ValueError(f"v{version}: parent {parent!r} does not exist")
            children.setdefault(parent, []).append(node)
    order, stack = [], [roots[0]]
    while stack:
        node = stack.pop()
        order.append(node)
        stack.extend(sorted(children.get(node["temp_id"], []), key=lambda n: n["position"], reverse=True))
    if len(order) != len(nodes):
        raise ValueError(f"v{version}: tree has a cycle or unreachable nodes")
    return order


def deduplicate_elements_and_build_hierarchy(arxiv_id: str, trees_by_version, key_mapping=None, known_keys=None):
    """Share one element per (kind, content) across versions.

    `trees_by_version`: list of (version, nodes) ascending; nodes carry `temp_id`,
    `parent_temp_id`, `position`, `kind`, `cleaned_content`.
    Returns (elements, hierarchy, occurrences_by_version, unknown_citation_keys).
    """
    prefix = arxiv_id.replace(".", "-").replace("/", "_") + "#"
    key_mapping = key_mapping or {}
    unknown, elements, hierarchy, occurrences = set(), [], [], {}
    store, counters = {}, {}
    for version, nodes in trees_by_version:
        element_of, occurrence_of, rows = {}, {}, []
        for node in _preorder(version, nodes):
            kind = node["kind"]
            content, missing = rename_citations(node["cleaned_content"], key_mapping, known_keys)
            unknown |= missing
            if kind == "document":
                element_id = f"{prefix}root{version}"
                elements.append(asdict(ElementRow(element_id, arxiv_id, kind, content)))
            else:
                if kind not in ID_PREFIX:
                    raise ValueError(f"v{version}: unsupported element kind {kind!r}")
                element_id = store.get((kind, content))
                if element_id is None:
                    counters[kind] = counters.get(kind, 0) + 1
                    element_id = f"{prefix}{ID_PREFIX[kind]}{counters[kind]}"
                    store[(kind, content)] = element_id
                    elements.append(asdict(ElementRow(element_id, arxiv_id, kind, content)))
            parent = node["parent_temp_id"]
            occurrence_id = f"o{len(rows) + 1}"
            element_of[node["temp_id"]], occurrence_of[node["temp_id"]] = element_id, occurrence_id
            rows.append(dict(occurrence_id=occurrence_id,
                             parent_occurrence_id=occurrence_of[parent] if parent is not None else None,
                             position=node["position"], element_id=element_id))
            if parent is not None:
                hierarchy.append(asdict(HierarchyRow(arxiv_id, version, element_id, element_of[parent])))
        occurrences[version] = rows
    return elements, hierarchy, occurrences, unknown


# ---------------------------------------------------------------- adapter
def _atomic_text(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _load(output: Path, result: dict, stage: str, artifact: str, paper_id: str, version: int):
    """Read one upstream artifact after checking its identity."""
    if not isinstance(result, dict) or (result.get("stage"), result.get("paper_id"), result.get("version"),
                                        result.get("schema_version")) != (stage, paper_id, version, PARSING_SCHEMA_VERSION):
        raise ValueError(f"v{version}: {stage} result does not match the requested paper/version")
    artifacts = result.get("artifacts")
    if not isinstance(artifacts, dict) or artifact not in artifacts:
        raise ValueError(f"v{version}: {stage} result has no {artifact} artifact")
    payload = json.loads(safe_join(output, artifacts[artifact]).read_text(encoding="utf-8"))
    if (payload.get("schema_version"), payload.get("arxiv_id"), payload.get("version")) != (PARSING_SCHEMA_VERSION, paper_id, version):
        raise ValueError(f"v{version}: {artifact} header does not match")
    return payload


def run(record: dict, output_dir: Path, *, version: dict | None = None,
        upstream: dict | None = None) -> StageResult:
    """Deduplicate references and elements of every version of one paper and write the three tables."""
    paper_id = record.get("arxiv_id", "") if isinstance(record, dict) else ""

    def result(status, artifacts=None, error=None, size=0):
        return StageResult(STAGE, paper_id, None, status, utc_now(), artifacts or {}, error,
                           attempts=1, size_bytes=size, schema_version=PARSING_SCHEMA_VERSION)

    try:
        key = paper_key(paper_id)
        output = Path(output_dir).resolve()
        if not isinstance(upstream, dict) or not isinstance(upstream.get("context"), dict):
            raise ValueError("Deduplication requires the MS2 context envelope")
        snapshot = upstream["context"].get("input_snapshot_id")
        if not isinstance(snapshot, str) or not snapshot:
            raise ValueError("Missing input snapshot identifier")
        listed = upstream.get("versions")
        if not isinstance(listed, list) or not all(isinstance(v, dict) and type(v.get("version")) is int for v in listed):
            raise ValueError("Deduplication requires upstream.versions: a list of per-version results")
        numbers = [v["version"] for v in listed]
        if numbers != sorted(set(numbers)):
            raise ValueError("upstream.versions must be unique and ascending")
        expected = [v["version"] for v in record.get("versions", [])]
        if not expected or expected != list(range(1, len(expected) + 1)):
            raise ValueError("Discovery record must list versions 1..N")
        missing = [n for n in expected if n not in numbers]
        blocked = [f"v{v['version']}" for v in listed
                   if (v.get("normalize") or {}).get("status") not in {"success", "no_source"}
                   or (v.get("bibliography") or {}).get("status") not in {"success", "no_source"}]
        if missing or blocked or numbers != expected:
            detail = ", ".join([f"missing v{n}" for n in missing] + [f"{b} not ready" for b in blocked]) or "unexpected versions"
            return result("skipped", error=ErrorInfo("upstream_unavailable", detail))

        trees, bib_entries, version_info, warnings = [], [], [], []
        for item in listed:
            number = item["version"]
            tree = _load(output, item["normalize"], "normalize", "normalized_tree", paper_id, number)
            raw = _load(output, item["bibliography"], "bibliography", "raw_bibtex", paper_id, number)
            if item["normalize"]["status"] == "no_source" and tree["nodes"]:
                raise ValueError(f"v{number}: a no_source version must have an empty tree")
            trees.append((number, tree["nodes"]))
            bib_entries.append((number, raw["entries"]))
            warnings.extend(dict(w, version=number) for w in raw.get("warnings", []))
            empty = item["normalize"]["status"] == "no_source"
            reason = None
            if empty:
                reason = "no TeX source; BibTeX inventory kept" if raw["entries"] else "no TeX source"
            version_info.append(dict(version=number, status=item["normalize"]["status"], reason=reason))

        rows, key_mapping, conflicts = unionize_bibtex(bib_entries)
        known = {e["citation_key"] for _, entries in bib_entries for e in entries}
        elements, hierarchy, occurrences, unknown = deduplicate_elements_and_build_hierarchy(
            paper_id, trees, key_mapping, known)
        for conflict in conflicts:
            warnings.append(dict(code="bib_field_conflict", message=json.dumps(conflict, ensure_ascii=False), source_path=None))
        if unknown:
            warnings.append(dict(code="citation_key_unknown", message=", ".join(sorted(unknown)), source_path=None))
        if any(w.get("code") == "bib_source_missing" for w in warnings):
            warnings.append(dict(code="bib_source_missing_versions",
                                 message=", ".join(f"v{w['version']}" for w in warnings if w.get("code") == "bib_source_missing"),
                                 source_path=None))

        header = dict(schema_version=PARSING_SCHEMA_VERSION, arxiv_id=paper_id)
        tables = {"elements": f"tables/{key}/elements.json", "hierarchy": f"tables/{key}/hierarchy.json",
                  "bibtex": f"tables/{key}/bibtex.json", "bibtex_file": f"tables/{key}/references.bib"}
        manifest_path = f"tables/{key}/parsing_manifest.json"
        atomic_json(safe_join(output, tables["elements"]), elements)
        atomic_json(safe_join(output, tables["hierarchy"]), hierarchy)
        atomic_json(safe_join(output, tables["bibtex"]), rows)
        _atomic_text(safe_join(output, tables["bibtex_file"]), render_bibtex(rows))
        for info in version_info:
            relative = f"parsed/{key}/v{info['version']}/occurrences.json"
            atomic_json(safe_join(output, relative),
                        dict(header, version=info["version"], occurrences=occurrences[info["version"]]))
            info["artifacts"] = {"occurrences": relative}
        counts = dict(elements_before=sum(len(nodes) for _, nodes in trees), elements_after=len(elements),
                      bibtex_before=sum(len(entries) for _, entries in bib_entries), bibtex_after=len(rows))
        manifest = dict(header, input_snapshot_id=snapshot, expected_versions=expected, complete=True,
                        versions=version_info, tables=tables, counts=counts, warnings=warnings)
        atomic_json(safe_join(output, manifest_path), manifest)
        artifacts = {"elements": tables["elements"], "hierarchy": tables["hierarchy"], "bibtex": tables["bibtex"],
                     "bibtex_file": tables["bibtex_file"], "parsing_manifest": manifest_path}
        size = sum(safe_join(output, p).stat().st_size for p in artifacts.values())
        status = "no_source" if all(v["status"] == "no_source" for v in version_info) else "success"
        return result(status, artifacts, size=size)
    except (ValueError, TypeError, KeyError) as exc:
        return result("data_error", error=ErrorInfo("invalid_deduplication_input", str(exc)))
    except OSError as exc:
        return result("error", error=ErrorInfo("io_error", str(exc)))
