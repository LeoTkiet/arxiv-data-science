"""Stage `metadata` (TV4): arXiv metadata of one paper, written to metadata/<paper_key>.json.

Discovery has already queried arXiv for every version, so this stage makes no network call: it
validates the Discovery record, normalizes it into the shared schema and stores it. Nothing is
inferred: a value arXiv did not provide is null/[] and its name is listed in `missing_fields`.

Paper-level fields come from the latest version (`submission_date` from v1, `revised_dates` from
the `updated` date of v2..vn); every entry of `versions` carries that version's own metadata.
"""
from datetime import datetime
from pathlib import Path

from arxiv_pipeline.common.schemas import SCHEMA_VERSION, ErrorInfo, StageResult
from arxiv_pipeline.common.utils import atomic_json, paper_key, utc_now

STAGE = "metadata"
OPTIONAL_FIELDS = ("venue", "doi", "abstract", "categories")   # may legitimately be absent


def _iso(value, name):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        raise ValueError(f"{name} is not an ISO timestamp: {value!r}") from None
    if parsed.tzinfo is None:
        raise ValueError(f"{name} has no timezone: {value!r}")
    return value


def _version_entry(v: dict) -> dict:
    """One version's own metadata. Checkpoints written before per-version fields existed lack
    title/authors/venue: they become null/[] and are listed in the entry's `missing_fields`."""
    entry = {
        "version": v["version"],
        "versioned_id": v["versioned_id"],
        "title": v.get("title") or None,
        "authors": list(v.get("authors") or []),
        "venue": v.get("venue") or None,
        "submission_date": _iso(v.get("submission_date"), "version submission_date"),
        "updated_date": _iso(v.get("updated_date"), "version updated_date"),
    }
    entry["missing_fields"] = [name for name in ("title", "authors", "venue") if entry[name] in (None, [])]
    return entry


def build_metadata(record: dict) -> dict:
    """Validated metadata document for a successful Discovery record; ValueError if inconsistent."""
    if record.get("status") != "success":
        raise ValueError(f"discovery status is {record.get('status')!r}, not 'success'")
    meta, versions = record.get("metadata"), record.get("versions")
    if not isinstance(meta, dict) or not isinstance(versions, list):
        raise ValueError("record has no metadata or versions")
    arxiv_id, count = record["arxiv_id"], meta.get("latest_version")
    if meta.get("arxiv_id") != arxiv_id:
        raise ValueError("metadata.arxiv_id does not match record.arxiv_id")
    title, authors = meta.get("title"), meta.get("authors")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("title is empty")
    if not isinstance(authors, list) or not authors or not all(isinstance(a, str) and a.strip() for a in authors):
        raise ValueError("authors must be a non-empty list of names")
    if type(count) is not int or [v.get("version") for v in versions] != list(range(1, count + 1)):
        raise ValueError("versions are not 1..latest_version")
    if any(v.get("versioned_id") != f"{arxiv_id}v{v['version']}" for v in versions):
        raise ValueError("versioned_id does not match arxiv_id and version")
    revised = meta.get("revised_dates")
    if not isinstance(revised, list) or len(revised) != count - 1:
        raise ValueError("revised_dates must hold one date per version after v1")
    entries = [_version_entry(v) for v in versions]
    latest = entries[-1]
    if latest["title"] is not None and (latest["title"], latest["authors"]) != (title, authors):
        raise ValueError("paper-level title/authors differ from the latest version")
    doc = {
        "schema_version": SCHEMA_VERSION,
        "arxiv_id": arxiv_id,
        "title": title,
        "authors": authors,
        "submission_date": _iso(meta.get("submission_date"), "submission_date"),
        "revised_dates": [_iso(d, "revised_dates") for d in revised],
        "venue": meta.get("venue") or None,           # arXiv journal_ref only, never made up
        "latest_version": count,
        "abstract": meta.get("abstract") or None,
        "categories": list(meta.get("categories") or []),
        "doi": meta.get("doi") or None,
        "source": meta.get("source"),
        "retrieved_at": meta.get("retrieved_at"),
        "versions": entries,
    }
    doc["missing_fields"] = [name for name in OPTIONAL_FIELDS if doc[name] in (None, [])]
    return doc


def run(record, output_dir, *, version=None, upstream=None):
    """Adapter for the orchestrator (docs/CONTRACTS.md): paper-level stage, no network."""
    output, arxiv_id = Path(output_dir), record["arxiv_id"]
    try:
        doc = build_metadata(record)
    except (ValueError, KeyError, TypeError) as exc:
        return StageResult(STAGE, arxiv_id, None, "data_error", utc_now(),
                           error=ErrorInfo("data_error", str(exc)))
    relative = f"metadata/{paper_key(arxiv_id)}.json"
    atomic_json(output / relative, doc)
    return StageResult(STAGE, arxiv_id, None, "success", utc_now(), artifacts={"metadata": relative},
                       size_bytes=(output / relative).stat().st_size)
