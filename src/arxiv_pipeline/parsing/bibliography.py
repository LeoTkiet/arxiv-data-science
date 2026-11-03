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


def parse_bib_text(text: str):
    """Parse the content of a .bib file into (list of fields dicts, warnings)."""
    import bibtexparser                                     # imported late: only this stage needs it
    from bibtexparser.bparser import BibTexParser

    parser = BibTexParser(common_strings=True, ignore_nonstandard_types=False, homogenize_fields=False)
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
        bib_used = gathered.get("referenced_bib_file_paths", [])
        for name, value in (("raw_bibitems", raw_items), ("bib_file_paths", bib_all),
                            ("referenced_bib_file_paths", bib_used)):
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ValueError(f"Gathered {name} must be a list of strings")
        # Cited .bib files win; a version without any bibliography command still keeps its inventory.
        bib_paths = bib_used or bib_all

        entries, items, warnings = [], [], []
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
            rows, bib_warnings = parse_bib_text(path.read_text(encoding="utf-8", errors="replace"))
            warnings.extend(dict(w, source_path=relative) for w in bib_warnings)
            for fields in rows:
                entries.append(_row(paper_id, fields))
                items.append(dict(citation_key=fields["citation_key"], source="bib_file", source_path=relative,
                                  parsed=True))
        out_status = "success" if entries else "no_source"
        if not entries and status == "success":
            warnings.append(dict(code="bib_source_missing", message="TeX source has no extractable reference list.",
                                 source_path=None))
        header = dict(schema_version=PARSING_SCHEMA_VERSION, arxiv_id=paper_id, version=number)
        relative = f"parsed/{key}/v{number}/raw_bibtex.json"
        side = f"parsed/{key}/v{number}/raw_bibtex.provenance.json"
        atomic_json(safe_join(output, relative), dict(header, entries=entries, warnings=warnings))
        atomic_json(safe_join(output, side), dict(header, items=items))
        size = safe_join(output, relative).stat().st_size + safe_join(output, side).stat().st_size
        return result(out_status, {"raw_bibtex": relative, "provenance": side}, size=size)
    except (ValueError, TypeError, KeyError) as exc:
        return result("data_error", error=ErrorInfo("invalid_bibliography_input", str(exc)))
    except OSError as exc:
        return result("error", error=ErrorInfo("io_error", str(exc)))
