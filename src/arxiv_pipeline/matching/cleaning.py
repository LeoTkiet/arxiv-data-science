"""TV2 cleaning and normalization module for Milestone 3 Reference Matching.

Provides text cleaning, LaTeX unescaping, author parsing, identifier extraction,
and metadata normalization for BibTeX entries and crawled reference records.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from arxiv_pipeline.common.utils import normalize_id


# Regex for arXiv ID detection in free text (eprint, url, journal, note)
_ARXIV_NEW_RE = re.compile(r"\b(\d{4}\.\d{4,5}(?:v\d+)?)\b", re.IGNORECASE)
_ARXIV_OLD_RE = re.compile(r"\b([a-z\-]+(?:\.[a-z]{2})?/\d{7}(?:v\d+)?)\b", re.IGNORECASE)

# Mapping of LaTeX accent macros to Unicode combining characters
_ACCENT_COMBINING_MAP = {
    "`": "\u0300",  # Grave: \`{e}
    "'": "\u0301",  # Acute: \'{e}
    "^": "\u0302",  # Circumflex: \^{e}
    "~": "\u0303",  # Tilde: \~{n}
    "=": "\u0304",  # Macron: \={a}
    "u": "\u0306",  # Breve: \u{a}
    ".": "\u0307",  # Dot above: \.{z}
    '"': "\u0308",  # Diaeresis/Umlaut: \"{u}
    "r": "\u030a",  # Ring above: \r{a}
    "H": "\u030b",  # Double acute: \H{o}
    "v": "\u030c",  # Caron: \v{c}
    "c": "\u0327",  # Cedilla: \c{c}
    "k": "\u0328",  # Ogonek: \k{a}
}

# LaTeX special character substitutions
_LATEX_SPECIALS = [
    (re.compile(r"\\&"), "&"),
    (re.compile(r"\\%"), "%"),
    (re.compile(r"\\_"), "_"),
    (re.compile(r"\\#"), "#"),
    (re.compile(r"\\\$"), "$"),
    (re.compile(r"\\~"), "~"),
    (re.compile(r"---"), "-"),
    (re.compile(r"--"), "-"),
    (re.compile(r"``|''"), '"'),
    (re.compile(r"`|'"), "'"),
    (re.compile(r"(?<!\\)~"), " "),
]


def _replace_latex_accents(text: str) -> str:
    """Convert LaTeX accent macros to corresponding Unicode characters."""
    def repl_braced(m: re.Match) -> str:
        acc = m.group(1)
        char = m.group(2)
        comb = _ACCENT_COMBINING_MAP.get(acc)
        if comb:
            return unicodedata.normalize("NFKC", char + comb)
        return char

    # Pattern 1 for \`{e}, \"{o}, \c{c}, \~{n}, etc. (with braces)
    text = re.sub(
        r"\\([`'^~=.u\"rHvck])\{([a-zA-Z])\}",
        repl_braced,
        text,
    )
    # Pattern 2 without braces for symbol accents ONLY: \'e, \"o, \~n, etc.
    # Letter accents (u, r, H, v, c, k) are excluded here so macros like \url are not corrupted.
    text = re.sub(
        r"\\([`'^~=.\"])([a-zA-Z])",
        repl_braced,
        text,
    )
    # Pattern 3 for letter accents with whitespace: \u a, \v c, etc.
    text = re.sub(
        r"\\([urHvck])\s+([a-zA-Z])",
        repl_braced,
        text,
    )
    return text


def _unwrap_braces(text: str) -> str:
    """Iteratively remove LaTeX styling macros and balanced outer braces."""
    macro_pattern = re.compile(
        r"\\(?:emph|textit|textbf|textsc|texttt|textsf|text|mathrm|mathbf|mathit|url)\s*\{"
    )
    result = text
    for _ in range(5):
        m = macro_pattern.search(result)
        if not m:
            break
        start_brace = m.end() - 1
        depth = 0
        end_brace = -1
        for idx in range(start_brace, len(result)):
            if result[idx] == "{":
                depth += 1
            elif result[idx] == "}":
                depth -= 1
                if depth == 0:
                    end_brace = idx
                    break
        if end_brace != -1:
            inner = result[start_brace + 1 : end_brace]
            result = result[: m.start()] + inner + result[end_brace + 1 :]
        else:
            break
    return result


def clean_latex_text(text: str | None) -> str:
    """Clean LaTeX macros, accents, inline formulas, and normalize Unicode to NFKC."""
    if not text:
        return ""
    if not isinstance(text, str):
        text = str(text)

    s = text

    # Strip inline math expressions like $x^2 + y$ or \( ... \)
    s = re.sub(r"\$[^$]*\$", " ", s)
    s = re.sub(r"\\\([^\\]*\\\)", " ", s)

    # Convert accent macros to proper Unicode characters first, before special char replacement
    s = _replace_latex_accents(s)

    # Substitute LaTeX escaped specials
    for pattern, repl in _LATEX_SPECIALS:
        s = pattern.sub(repl, s)

    # Unwrap style macros
    s = _unwrap_braces(s)

    # Remove remaining protective BibTeX braces {...} while keeping content
    for _ in range(3):
        s = re.sub(r"\{([^{}]*)\}", r"\1", s)
    s = s.replace("{", "").replace("}", "")

    # Unicode NFKC normalization
    s = unicodedata.normalize("NFKC", s)

    # Collapse multiple whitespace
    s = re.sub(r"\s+", " ", s).strip()
    return s


def normalize_title(title: str | None) -> str:
    """Normalize a title string for deterministic text matching.

    Applies LaTeX cleanup, lowercases, removes non-alphanumeric punctuation,
    and collapses whitespace.
    """
    if not title:
        return ""
    cleaned = clean_latex_text(title).casefold()
    # Retain alphanumeric characters, hyphens, and whitespace
    alphanumeric_only = re.sub(r"[^\w\s\-]", " ", cleaned)
    # Replace hyphens with spaces for token alignment
    normalized = re.sub(r"[-_]", " ", alphanumeric_only)
    return re.sub(r"\s+", " ", normalized).strip()


def canonical_doi(doi: str | None) -> str | None:
    """Normalize DOI to canonical format (lowercase, strip URL/doi prefixes)."""
    if not doi:
        return None
    d = str(doi).strip().lower()
    for prefix in (
        "https://doi.org/",
        "http://doi.org/",
        "https://dx.doi.org/",
        "http://dx.doi.org/",
        "dx.doi.org/",
        "doi.org/",
        "doi:",
    ):
        if d.startswith(prefix):
            d = d[len(prefix) :].strip()
    d = d.strip("/")
    # A valid DOI typically begins with 10.
    if d.startswith("10.") and "/" in d:
        return d
    return d if d else None


def extract_arxiv_id(text: str | None) -> str | None:
    """Extract and canonicalize an arXiv ID from free text, eprint, or URL."""
    if not text:
        return None
    s = str(text).strip()
    # Try new format first (e.g. 2310.12345 or 2310.12345v1)
    m = _ARXIV_NEW_RE.search(s)
    if m:
        raw_id = m.group(1).lower()
        try:
            return normalize_id(raw_id)[0]
        except ValueError:
            pass
    # Try old format (e.g. hep-th/9901001 or math.GT/0309136)
    m = _ARXIV_OLD_RE.search(s)
    if m:
        raw_id = m.group(1).lower()
        try:
            return normalize_id(raw_id)[0]
        except ValueError:
            pass
    return None


def parse_year(year: Any) -> int | None:
    """Safely parse a year value into an integer in [1900, 2030]."""
    if year is None:
        return None
    if isinstance(year, int):
        return year if 1900 <= year <= 2030 else None
    if isinstance(year, (str, float)):
        m = re.search(r"\b(19\d{2}|20\d{2})\b", str(year))
        if m:
            val = int(m.group(1))
            return val if 1900 <= val <= 2030 else None
    return None


def parse_authors(authors: list[str] | str | None) -> list[dict[str, str]]:
    """Parse author list into structured components: last_name, first_name, initials.

    Returns a list of dicts: [{'last_name': ..., 'first_name': ..., 'initials': ...}]
    """
    if not authors:
        return []

    # Flatten author strings that might contain multiple authors separated by 'and' or ';'
    raw_list: list[str] = []
    if isinstance(authors, str):
        items = [authors]
    else:
        items = list(authors)

    for it in items:
        if isinstance(it, str):
            splits = re.split(r"\s+and\s+|;\s*", it, flags=re.IGNORECASE)
            raw_list.extend(splits)

    parsed = []
    for item in raw_list:
        if not item or not isinstance(item, str):
            continue
        cleaned = clean_latex_text(item).strip()
        if not cleaned:
            continue
        # Strip 'et al.' markers from author name instead of dropping the author
        cleaned = re.sub(r"\bet\s+al\.?(?:\s|$)", " ", cleaned, flags=re.IGNORECASE).strip()
        if not cleaned:
            continue

        if "," in cleaned:
            parts = cleaned.split(",", 1)
            last = parts[0].strip()
            first = parts[1].strip() if len(parts) > 1 else ""
        else:
            tokens = cleaned.split()
            if len(tokens) == 1:
                last = tokens[0]
                first = ""
            else:
                last = tokens[-1]
                first = " ".join(tokens[:-1])

        last_norm = re.sub(r"[^\w\s]", "", last).casefold().strip()
        first_norm = re.sub(r"[^\w\s]", "", first).casefold().strip()
        initials = "".join(tok[0] for tok in first_norm.split() if tok)

        if last_norm:
            parsed.append({
                "last_name": last_norm,
                "first_name": first_norm,
                "initials": initials,
            })
    return parsed


def normalize_bibtex(row: Any) -> dict[str, Any]:
    """Clean and normalize a BibtexInput record, keeping audit metadata."""
    title_raw = getattr(row, "title", None) or ""
    authors_raw = getattr(row, "authors", None) or []
    year_raw = getattr(row, "year", None)
    doi_raw = getattr(row, "doi", None)
    url_raw = getattr(row, "url", None)
    journal_raw = getattr(row, "journal", None)

    extracted_id = (
        extract_arxiv_id(url_raw)
        or extract_arxiv_id(journal_raw)
        or extract_arxiv_id(getattr(row, "citation_key", None))
    )

    return {
        "arxiv_id": getattr(row, "arxiv_id", ""),
        "citation_key": getattr(row, "citation_key", ""),
        "clean_title": normalize_title(title_raw),
        "raw_title": title_raw,
        "clean_authors": parse_authors(authors_raw),
        "clean_year": parse_year(year_raw),
        "clean_doi": canonical_doi(doi_raw),
        "extracted_arxiv_id": extracted_id,
    }


def normalize_reference(row: Any) -> dict[str, Any]:
    """Clean and normalize a ReferenceInput record, keeping audit metadata."""
    ref_title_raw = getattr(row, "ref_title", None) or ""
    ref_authors_raw = getattr(row, "ref_authors", None) or []
    sub_date = getattr(row, "ref_submission_date", None)
    ref_year = parse_year(sub_date) if sub_date else None

    return {
        "arxiv_id": getattr(row, "arxiv_id", ""),
        "ref_arxiv_id": getattr(row, "ref_arxiv_id", ""),
        "clean_title": normalize_title(ref_title_raw),
        "raw_title": ref_title_raw,
        "clean_authors": parse_authors(ref_authors_raw),
        "clean_year": ref_year,
    }
