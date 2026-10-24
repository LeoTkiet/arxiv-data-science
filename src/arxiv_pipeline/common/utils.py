"""Identifiers, stable layout and atomic JSON persistence."""

import json
import os
from pathlib import Path
import re
import tempfile
from datetime import datetime, timezone
from urllib.parse import urlsplit

_ID = re.compile(r"^(?P<base>(?:\d{4}\.\d{4,5}|[a-z][a-z0-9.-]*/\d{7}))(?:v(?P<v>[1-9]\d*))?$")


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def normalize_id(value: str) -> tuple[str, int | None]:
    value = value.strip()
    if value.startswith(("https://", "http://")):
        url = urlsplit(value)
        if url.hostname not in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}:
            raise ValueError("Expected an arxiv.org URL")
        value = url.path
        if not value.startswith(("/abs/", "/pdf/")):
            raise ValueError("Expected /abs/ or /pdf/ URL")
        value = value[5:]
        if value.endswith(".pdf"):
            value = value[:-4]
    if value.lower().startswith("arxiv:"):
        value = value[6:].strip()
    match = _ID.fullmatch(value)
    if not match:
        raise ValueError("Invalid arXiv identifier syntax")
    base = match["base"]
    date, serial = (base.split(".") if "/" not in base else (base.split("/")[1][:4], base.split("/")[1][4:]))
    year, month = int(date[:2]), int(date[2:])
    if not 1 <= month <= 12 or int(serial) == 0:
        raise ValueError("Invalid arXiv month or sequence number")
    if "/" not in base:
        if not (7 <= year <= 99) or (year == 7 and month < 4):
            raise ValueError("Modern arXiv IDs start in April 2007")
        expected_length = 4 if year < 15 else 5
        if len(serial) != expected_length:
            raise ValueError("Incorrect arXiv sequence length for year")
    elif not (year >= 91 or year <= 7) or (year == 7 and month > 3):
        raise ValueError("Legacy arXiv IDs end in March 2007")
    return base, int(match["v"]) if match["v"] else None


def paper_key(arxiv_id: str) -> str:
    base, _ = normalize_id(arxiv_id)
    return base.replace("/", "_")


def version_paths(arxiv_id: str, version: int) -> tuple[str, str]:
    key = paper_key(arxiv_id)
    return f"raw/{key}/v{version}/source", f"corpus/{key}/v{version}"


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
