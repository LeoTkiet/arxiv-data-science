"""Bibliography extraction for Milestone 2 (TV4).

Turns the reference sources of one version into BibTeX rows (`BibtexRow`):
- `\\bibitem` blocks written by hand in the .tex files are converted with regular expressions;
- `.bib` files are parsed with bibtexparser.

Nothing is guessed: a field is filled only when it can be extracted with confidence, the rest
stays empty ("" / [] / None) and the original text is kept in a provenance sidecar.
Complies with docs/MS2_CONTRACTS.md: stage `bibliography`, input `upstream.stages.resolve`,
output `parsed/<paper_key>/vN/raw_bibtex.json`.
"""
import json
import re
from dataclasses import asdict
from pathlib import Path

from arxiv_pipeline.common.schemas import BibtexRow, ErrorInfo, PARSING_SCHEMA_VERSION, StageResult
from arxiv_pipeline.common.utils import atomic_json, paper_key, utc_now

STAGE = "bibliography"
DEFAULT_BIBITEM_TYPE = "misc"         

_BIBITEM = re.compile(r"\\bibitem\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}", re.DOTALL)
_DOI_BARE = re.compile(r"(?:\bdoi\s*[:=]?\s*|\\doi\s*\{\s*|https?://(?:dx\.)?doi\.org/)(10\.\d{4,9}/[^\s,}\\]+)", re.I)
_URL = re.compile(r"\\url\s*\{([^}]+)\}|\\href\s*\{([^}]+)\}|(https?://[^\s,}\\]+)")
_ARXIV_ID = re.compile(r"(?:arxiv\s*[:/]?\s*)?\b(?:\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?\b", re.I)
_YEAR = re.compile(r"(?<![\d.])(1[89]\d{2}|20\d{2})(?![\d])")
_QUOTED = re.compile(r"``(.+?)''|\"(.+?)\"|\\textquotedblleft\s*(.+?)\s*\\textquotedblright", re.DOTALL)
_EMPH = re.compile(r"\\(?:emph|textit|textsl)\s*\{((?:[^{}]|\{[^{}]*\})*)\}|\{\\(?:em|it|sl)\s+([^{}]*)\}")
_INITIALS = re.compile(r"^(?:[A-Z]\.?-?\s*)+$")
_BIB_HEADER = re.compile(r'@([A-Za-z]+)\s*([{(])')
_BIB_DELIMITER = re.compile(r'[\\{}()"@]')
_BIB_KEY = re.compile(r'\s*([^,\s{}()]+)\s*,')
_CITATION = re.compile(
    r'\\(?:cite|citep|citet|citealp|citealt|citeauthor|citeyear|citeyearpar|citenum|nocite|'
    r'parencite|textcite|autocite|footcite|smartcite)\*?\s*(?:\[[^\]]*\]\s*){0,2}\{([^{}]*)\}')


def _bib_blocks(text, *, selected=None):
    """Index top-level BibTeX blocks in one pass, without parsing unused entries."""
    pos = 0
    while match := _BIB_HEADER.search(text, pos):
        start, body = match.start(), match.end()
        kind, opening = match.group(1).lower(), match.group(2)
        key_match = _BIB_KEY.match(text, body) if kind not in {'string','comment','preamble'} else None
        key = key_match.group(1) if key_match else None
        malformed = False
        brace = 1 if opening == '{' else 0
        base = brace
        paren, quoted = (1 if opening == '(' else 0), False
        pos = body
        while delimiter := _BIB_DELIMITER.search(text, pos):
            char, pos = delimiter.group(), delimiter.end()
            if (char == '@' and brace == base and not quoted and
                    _BIB_HEADER.match(text, delimiter.start()) and
                    not text[text.rfind('\n', start, delimiter.start()) + 1:delimiter.start()].strip()):
                if selected is None or key is None or key in selected:
                    raise ValueError(f'Unclosed BibTeX {kind} block at offset {start}')
                # Resynchronize only between balanced fields at a new header.
                # Do not invent a closing brace or parse the malformed entry.
                pos, malformed = delimiter.start(), True
                break
            elif char == '\\':
                pos += 1
            elif char == '"' and brace == base:
                quoted = not quoted
            elif char == '{':
                brace += 1
            elif char == '}':
                if not (quoted and brace == base): brace -= 1
                if opening == '{' and brace == 0 and not quoted: break
            elif opening == '(' and brace == 0 and not quoted:
                if char == '(': paren += 1
                elif char == ')':
                    paren -= 1
                    if paren == 0: break
        else:
            if selected is None or key is None or key in selected or brace != base or quoted:
                raise ValueError(f'Unclosed BibTeX {kind} block at offset {start}')
            pos, malformed = len(text), True
        yield ('malformed' if malformed else kind), key, start, pos


def _cited_keys(gathered):
    """Conservative selection: literals/definitions are not executed citations.

    No recognizable citation or nocite{*} retains the legacy full inventory.
    Dynamic keys also retain all entries rather than silently dropping data.
    """
    from arxiv_pipeline.parsing.resolver import _tokens
    supported = {'cite','citep','citet','citealp','citealt','citeauthor','citeyear','citeyearpar',
                 'citenum','nocite','parencite','textcite','autocite','footcite','smartcite'}
    sources = (gathered.get('preamble_tex',''), gathered.get('merged_body_tex',''))
    lexed = [list(_tokens(text)) for text in sources]
    definitions = {}
    invoked = set()
    for tokens in lexed:
        for token in tokens:
            if token.name == 'definition':
                commands = re.findall(r'\\([A-Za-z@]+)', token.value)
                if len(commands) > 1:
                    definitions.setdefault(commands[1], set()).update(commands[2:])
                if commands and commands[0].endswith('environment') and any('cite' in c.lower() for c in commands):
                    return None
            else:
                invoked.add(token.name)
                if token.name == 'let' or ('cite' in token.name.lower() and token.name not in supported):
                    return None
    # A called macro may generate citations using arguments or another macro.
    # Retain the full library whenever that execution cannot be selected literally.
    pending = list(invoked & definitions.keys())
    visited = set()
    while pending:
        name = pending.pop()
        if name in visited: continue
        visited.add(name)
        commands = definitions.get(name, set())
        if any('cite' in command.lower() for command in commands): return None
        pending.extend(commands & definitions.keys())
    keys, recognized = set(), False
    for text,tokens in zip(sources,lexed):
        starts = {t.start for t in tokens if t.name in supported}
        matched = set()
        for match in _CITATION.finditer(text):
            if match.start() not in starts: continue
            matched.add(match.start())
            recognized = True
            for key in match.group(1).split(','):
                key = key.strip()
                if key == '*' or '\\' in key or '#' in key: return None
                if key: keys.add(key)
        if matched != starts: return None
    return keys if recognized else None


def safe_join(root, relative: str) -> Path:
    """Resolve a serialized path under `root`; absolute paths, `..` and symlink escapes are rejected."""
    if not isinstance(relative, str) or not relative or "\\" in relative or Path(relative).is_absolute():
        raise ValueError(f"Unsafe artifact path: {relative!r}")
    base = Path(root).resolve()
    path = (base / relative).resolve()
    if not path.is_relative_to(base) or ".." in Path(relative).parts:
        raise ValueError(f"Artifact path escapes its root: {relative!r}")
    return path


def clean_text(text: str, strip: str = " \t\n.,;") -> str:
    """Strip LaTeX markup that carries no meaning for a bibliographic field."""
    text = re.sub(r"(?<!\\)%.*", "", text)
    text = _EMPH.sub(lambda m: m.group(1) or m.group(2) or "", text)
    text = re.sub(r"\\(?:newblock|bgroup|egroup)\b", " ", text)
    text = text.replace("\\&", "&").replace("\\%", "%").replace("~", " ").replace("\\ ", " ")
    text = re.sub(r"\\(?:textbf|textrm|textsc|mbox|text)\s*(?=\{)", "", text)
    text = re.sub(r"\\[\"'`^~=.]\s*\{?(\w)\}?", r"\1", text)      # accents: \'e -> e
    text = re.sub(r"[{}]", "", text)
    return re.sub(r"\s+", " ", text).strip(strip)


def _split_authors(text: str) -> list[str]:
    """Split a list of author names; 'Last, F.' pairs are kept together."""
    text = clean_text(text, strip=" \t\n,;")
    text = re.sub(r"\bet\.? al\.?$", "", text).strip(" ,")
    parts = [p.strip() for p in re.split(r"\s+and\s+|\s*&\s*|;", text) if p.strip()]
    names = []
    for part in parts:
        pieces = [p.strip() for p in re.sub(r"^and\s+", "", part).split(",") if p.strip()]
        merged = []
        for piece in pieces:
            if merged and _INITIALS.match(piece) and not _INITIALS.match(merged[-1]):
                merged[-1] = f"{merged[-1]}, {piece}"          # "Doe", "J." -> "Doe, J."
            else:
                merged.append(piece)
        names.extend(merged)
    # a closing sentence period is not part of a name, an initial's period is
    names = [n[:-1] if n.endswith(".") and len(n.split()[-1]) > 2 else n for n in names]
    return [n for n in names if n]


def _journal(text: str):
    emph = _EMPH.search(text)
    value = clean_text(emph.group(1) or emph.group(2)) if emph else clean_text(text)
    value = re.sub(r",?\s*(?:vol\.?|volume|pp\.?|no\.?)\s.*$", "", value, flags=re.I)
    value = _YEAR.sub("", value).strip(" ,.;()")
    return value or None


def parse_bibitem(raw: str):
    """Convert one `\\bibitem` block into (fields dict, provenance); fields is None without a key."""
    match = _BIBITEM.search(raw or "")
    provenance = {"raw_text": raw, "fields_extracted": []}
    if not match or not match.group(1).strip():
        return None, provenance
    key, body = match.group(1).strip(), raw[match.end():]
    provenance["citation_key"] = key
    doi = None
    m = _DOI_BARE.search(body)
    if m:
        doi = m.group(1).rstrip(".;)")
        body = body[:m.start()] + " " + body[m.end():]
    url = None
    for m in _URL.finditer(body):
        candidate = (m.group(1) or m.group(2) or m.group(3)).rstrip(".;)")
        if "doi.org" not in candidate:
            url = candidate
            break
    body_no_links = _URL.sub(" ", body)
    years = _YEAR.findall(clean_text(_ARXIV_ID.sub(" ", body_no_links)))
    year = int(years[-1]) if years else None

    title, authors, journal = "", [], None
    blocks = [b for b in re.split(r"\\newblock\b", body_no_links) if b.strip()]
    quoted = _QUOTED.search(body_no_links)
    if len(blocks) >= 2:                                   # plain/ieeetr style: authors \newblock title \newblock venue
        authors = _split_authors(blocks[0])
        title = clean_text(blocks[1])
        if len(blocks) >= 3:
            journal = _journal(blocks[2])
    elif quoted:                                           # authors, ``Title,'' venue, year
        title = clean_text(next(g for g in quoted.groups() if g))
        authors = _split_authors(body_no_links[:quoted.start()])
        journal = _journal(body_no_links[quoted.end():])
    fields = {"citation_key": key, "entry_type": DEFAULT_BIBITEM_TYPE, "title": title,
              "authors": authors, "year": year, "doi": doi, "journal": journal, "url": url}
    provenance["fields_extracted"] = [k for k in ("title", "authors", "year", "doi", "journal", "url")
                                      if fields[k] not in ("", [], None)]
    return fields, provenance


def _bib_name(name: str) -> str:
    name = re.sub(r"\s+", " ", name.replace("{", "").replace("}", "")).strip()
    if name.count(",") == 1:                               # "Last, First" -> "First Last"
        last, first = (p.strip() for p in name.split(","))
        return f"{first} {last}".strip()
    return name


def parse_bib_text(text: str, *, strings=None, inherit_crossrefs=False):
    """Parse the content of a .bib file into (list of fields dicts, warnings)."""
    import bibtexparser                                     # imported late: only this stage needs it
    from bibtexparser.bparser import BibTexParser

    parser = BibTexParser(common_strings=True, ignore_nonstandard_types=False, homogenize_fields=False,
                          add_missing_from_crossref=inherit_crossrefs)
    if strings:
        parser.bib_database.strings.update(strings)
    db = bibtexparser.loads(text, parser=parser)
    rows, warnings = [], []
    for entry in db.entries:
        key = (entry.get("ID") or "").strip()
        if not key:
            warnings.append(dict(code="bib_entry_without_key", message="Skipped an entry without a key.", source_path=None))
            continue
        year_match = re.search(r"\d{4}", entry.get("year", ""))
        authors = [_bib_name(a) for a in re.split(r"\s+and\s+", entry.get("author", "")) if a.strip()]
        doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", entry.get("doi", "").strip(), flags=re.I) or None
        rows.append({
            "citation_key": key,
            "entry_type": entry.get("ENTRYTYPE", "misc").lower(),
            "title": clean_text(entry.get("title", "")),
            "authors": authors,
            "year": int(year_match.group()) if year_match else None,
            "doi": doi,
            "journal": clean_text(entry.get("journal") or entry.get("booktitle") or "") or None,
            "url": entry.get("url", "").strip() or None,
        })
    declared = len(re.findall(r"^\s*@(?!comment\b|string\b|preamble\b)\w+\s*[{(]", text, flags=re.I | re.M))
    if declared > len(db.entries):
        warnings.append(dict(code="bib_entries_unparsed",
                             message=f"{declared - len(db.entries)} of {declared} entries could not be parsed.",
                             source_path=None))
    return rows, warnings


def _row(arxiv_id: str, fields: dict) -> dict:
    fields = dict(fields, authors=[_bib_name(name) for name in fields['authors']])
    return asdict(BibtexRow(arxiv_id=arxiv_id, **fields))


def run(record: dict, output_dir: Path, *, version: dict | None = None,
        upstream: dict | None = None) -> StageResult:
    """Read `stages.resolve`, extract every reference of this version, write raw_bibtex.json."""
    paper_id = record.get("arxiv_id", "") if isinstance(record, dict) else ""
    number = version.get("version") if isinstance(version, dict) else None

    def result(status, artifacts=None, error=None, size=0):
        return StageResult(STAGE, paper_id, number, status, utc_now(), artifacts or {}, error,
                           attempts=1, size_bytes=size, schema_version=PARSING_SCHEMA_VERSION)

    try:
        key = paper_key(paper_id)
        if type(number) is not int or number < 1:
            raise ValueError("Bibliography requires a positive integer version")
        output = Path(output_dir).resolve()
        if not isinstance(upstream, dict) or not isinstance(upstream.get("context"), dict):
            raise ValueError("Bibliography requires the MS2 context/stages envelope")
        context = upstream["context"]
        input_root, snapshot = context.get("input_root"), context.get("input_snapshot_id")
        if not isinstance(input_root, str) or not input_root:
            raise ValueError("Missing MS1 input root in runtime context")
        if not isinstance(snapshot, str) or not snapshot:
            raise ValueError("Missing input snapshot identifier")
        stages = upstream.get("stages")
        if not isinstance(stages, dict):
            raise ValueError("Missing stages envelope")
        resolved = stages.get("resolve")
        if resolved is None:
            return result("skipped", error=ErrorInfo("upstream_unavailable", "resolve checkpoint is missing"))
        if not isinstance(resolved, dict) or (resolved.get("stage"), resolved.get("paper_id"), resolved.get("version"),
                                              resolved.get("schema_version")) != ("resolve", paper_id, number, PARSING_SCHEMA_VERSION):
            raise ValueError("Resolver stage identity/schema does not match requested paper/version")
        status = resolved.get("status")
        if status not in {"success", "no_source"}:
            return result("skipped", error=ErrorInfo("upstream_unavailable", f"resolve status: {status}"))
        artifacts = resolved.get("artifacts")
        if not isinstance(artifacts, dict) or "gathered" not in artifacts:
            raise ValueError("Resolver result has no gathered artifact")
        gathered_path = safe_join(output, artifacts["gathered"])
        if not gathered_path.is_file():
            raise ValueError("Resolver gathered artifact does not exist")
        gathered = json.loads(gathered_path.read_text(encoding="utf-8"))
        if not isinstance(gathered, dict) or (gathered.get("schema_version"), gathered.get("arxiv_id"),
                                              gathered.get("version")) != (PARSING_SCHEMA_VERSION, paper_id, number):
            raise ValueError("Gathered artifact identity/schema mismatch")
        if gathered.get("input_snapshot_id") != snapshot:
            raise ValueError("Gathered snapshot does not match the runtime context")
        raw_items, bib_all = gathered.get("raw_bibitems", []), gathered.get("bib_file_paths", [])
        supplemental_items = gathered.get('archive_bibitems', [])
        bib_used = gathered.get("referenced_bib_file_paths", [])
        for name, value in (("raw_bibitems", raw_items), ('archive_bibitems', supplemental_items), ("bib_file_paths", bib_all),
                            ("referenced_bib_file_paths", bib_used)):
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ValueError(f"Gathered {name} must be a list of strings")
        # Cited .bib files win; a version without any bibliography command still keeps its inventory.
        bib_paths = bib_used or bib_all

        entries, items, warnings = [], [], []
        # Parse definitions without interpolating entries first: a cited file
        # may use symbols defined by a sibling bibliography of this version.
        import bibtexparser
        from bibtexparser.bparser import BibTexParser
        context_paths = [relative for relative in bib_all if safe_join(input_root, relative).is_file()]
        libraries, malformed_keys = {}, set()
        selected = _cited_keys(gathered)
        string_blocks, entry_index = [], {}
        for relative in context_paths:
            text = safe_join(input_root, relative).read_text(encoding='utf-8', errors='replace')
            blocks = list(_bib_blocks(text, selected=selected))
            libraries[relative] = (text, blocks)
            for kind, citation_key, start, end in blocks:
                if kind == 'malformed':
                    malformed_keys.add(citation_key)
                    warnings.append(dict(code='bib_malformed_uncited_entry', source_path=relative,
                        message=f'Unclosed uncited entry {citation_key!r} at offsets {start}:{end}; not parsed.'))
                elif kind == 'string': string_blocks.append(text[start:end])
                elif citation_key is not None:
                    entry_index.setdefault(citation_key, []).append((relative, start, end))
        context_text = '\n'.join(string_blocks)
        context_db = bibtexparser.loads(context_text, parser=BibTexParser(interpolate_strings=False))
        # Keep expressions lazy: an unused alias may legitimately reference an
        # unavailable journal macro. A used undefined symbol still raises.
        strings = context_db.strings
        for raw in raw_items:
            fields, provenance = parse_bibitem(raw)
            if fields is None:
                warnings.append(dict(code="bibitem_unparsed", message="\\bibitem without a usable key was skipped.",
                                     source_path=None))
                items.append(dict(provenance, source="bibitem", parsed=False))
                continue
            entries.append(_row(paper_id, fields))
            items.append(dict(provenance, source="bibitem", parsed=True))
        for relative in bib_paths:
            path = safe_join(input_root, relative)
            if not path.is_file():
                warnings.append(dict(code="bib_file_missing", message="Listed .bib file does not exist.",
                                     source_path=relative))
                continue
            text, blocks = libraries[relative]
            own = [(citation_key,start,end) for kind,citation_key,start,end in blocks
                   if kind not in {'string','comment','preamble','malformed'}
                   and (selected is None or citation_key in selected)]
            parse_parts = [text[start:end] for _,start,end in own]
            included = {citation_key for citation_key,_,_ in own}
            # Crossref parents are dependencies, not automatically printed rows.
            pending = list(parse_parts)
            while pending:
                part = pending.pop()
                for parent in re.findall(r'\b(?:crossref|xref)\s*=\s*[{\"]([^}\"]+)[}\"]', part, re.I):
                    if parent in malformed_keys:
                        raise ValueError(f'Crossref dependency {parent!r} is malformed')
                    if parent in included: continue
                    included.add(parent)
                    for origin,start,end in entry_index.get(parent, []):
                        parent_text = libraries[origin][0][start:end]
                        parse_parts.append(parent_text); pending.append(parent_text)
            rows, bib_warnings = parse_bib_text('\n'.join(parse_parts), strings=strings, inherit_crossrefs=True)
            if selected is not None:
                omitted = sum(1 for kind,k,_,_ in blocks if kind not in {'string','comment','preamble'} and k not in selected)
                if omitted:
                    warnings.append(dict(code='bib_uncited_entries_omitted',source_path=relative,
                                         message=f'{omitted} library entries are not cited by this version.'))
            own_keys = {k for k,_,_ in own}
            rows = [row for row in rows if row['citation_key'] in own_keys]
            warnings.extend(dict(w, source_path=relative) for w in bib_warnings)
            for fields in rows:
                entries.append(_row(paper_id, fields))
                items.append(dict(citation_key=fields["citation_key"], source="bib_file", source_path=relative,
                                  parsed=True))
        known = {row['citation_key'] for row in entries}
        for raw in supplemental_items:
            fields, provenance = parse_bibitem(raw)
            if fields is None: continue
            citation_key = fields['citation_key']
            if citation_key in known or (selected is not None and citation_key not in selected): continue
            entries.append(_row(paper_id, fields))
            known.add(citation_key)
            items.append(dict(provenance, source='archive_bibitem', parsed=True))
            warnings.append(dict(code='bib_archive_supplement', source_path=None,
                                 message=f'Entry {citation_key!r} missing from library; recovered from same-jobname BBL.'))
        if selected is not None:
            for unresolved_key in sorted(selected - known):
                warnings.append(dict(code='bib_citation_unresolved', source_path=None,
                                     message=f'Cited key {unresolved_key!r} has no extractable entry in the supplied sources.'))
        out_status = "success" if entries else "no_source"
        if not entries and status == "success":
            warnings.append(dict(code="bib_source_missing", message="TeX source has no extractable reference list.",
                                 source_path=None))
        header = dict(schema_version=PARSING_SCHEMA_VERSION, arxiv_id=paper_id, version=number)
        relative = f"parsed/{key}/v{number}/raw_bibtex.json"
        side = f"parsed/{key}/v{number}/raw_bibtex.provenance.json"
        atomic_json(safe_join(output, relative), dict(header, entries=entries, warnings=warnings))
        atomic_json(safe_join(output, side), dict(header, items=items,
                    string_context=dict(source_paths=context_paths, symbols=sorted(strings)),
                    selection=dict(mode='full_inventory' if selected is None else 'cited_keys',
                                   citation_keys=sorted(selected) if selected is not None else None)))
        size = safe_join(output, relative).stat().st_size + safe_join(output, side).stat().st_size
        return result(out_status, {"raw_bibtex": relative, "provenance": side}, size=size)
    except (ValueError, TypeError, KeyError) as exc:
        return result("data_error", error=ErrorInfo("invalid_bibliography_input", str(exc)))
    except OSError as exc:
        return result("error", error=ErrorInfo("io_error", str(exc)))
