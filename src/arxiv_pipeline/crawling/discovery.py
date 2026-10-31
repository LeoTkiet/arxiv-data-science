"""Sequential arXiv discovery with version metadata and classified failures."""

import logging
import socket
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

from arxiv_pipeline.common.config import APIConfig
from arxiv_pipeline.common.schemas import DiscoveryRecord, ErrorInfo, VersionRecord
from arxiv_pipeline.common.utils import normalize_id, utc_now, version_paths

ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"
LOG = logging.getLogger("arxiv_pipeline")


class APIError(Exception):
    def __init__(self, message, *, retryable=False, http_status=None):
        super().__init__(message)
        self.retryable = retryable
        self.http_status = http_status


class DataError(Exception):
    pass


def _retry_after(value):
    try:
        return max(0.0, float(value))
    except (ValueError, TypeError):
        try:
            return max(0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0.0


class ArxivClient:
    def __init__(self, config=None, *, opener=urlopen, sleep=time.sleep, clock=time.monotonic):
        self.config = config or APIConfig()
        self.opener, self.sleep, self.clock = opener, sleep, clock
        self.last_request = None
        self.attempts = 0

    def fetch(self, identifier: str) -> bytes:
        query = urlencode({"id_list": identifier, "max_results": 1})
        request = Request(f"{self.config.endpoint}?{query}", headers={"User-Agent": self.config.user_agent})
        for retry in range(self.config.retries + 1):
            if self.last_request is not None:
                self.sleep(max(0, self.config.delay - (self.clock() - self.last_request)))
            self.last_request = self.clock()
            self.attempts += 1
            try:
                with self.opener(request, timeout=self.config.timeout) as response:
                    return response.read()
            except HTTPError as exc:
                retryable = exc.code in {408, 429} or 500 <= exc.code <= 599
                error = APIError(f"HTTP {exc.code}: {exc.reason}", retryable=retryable, http_status=exc.code)
                wait = _retry_after(exc.headers.get("Retry-After")) if exc.headers else 0
                exc.close()
            except (URLError, TimeoutError, socket.timeout, ConnectionError, OSError, HTTPException) as exc:
                error = APIError(str(exc), retryable=True)
                wait = 0
            if not error.retryable or retry == self.config.retries:
                raise error
            pause = max(wait, self.config.delay * 2**retry)
            LOG.warning("Retry %s for %s in %.1fs: %s", retry + 1, identifier, pause, error)
            self.sleep(pause)
        raise AssertionError("Unreachable")

    def fetch_batch(self, identifiers: list[str]) -> bytes:
        id_list = ",".join(identifiers)
        query = urlencode({"id_list": id_list, "max_results": len(identifiers)})
        request = Request(f"{self.config.endpoint}?{query}", headers={"User-Agent": self.config.user_agent})
        for retry in range(self.config.retries + 1):
            if self.last_request is not None:
                self.sleep(max(0, self.config.delay - (self.clock() - self.last_request)))
            self.last_request = self.clock()
            self.attempts += 1
            try:
                with self.opener(request, timeout=self.config.timeout) as response:
                    return response.read()
            except HTTPError as exc:
                retryable = exc.code in {408, 429} or 500 <= exc.code <= 599
                error = APIError(f"HTTP {exc.code}: {exc.reason}", retryable=retryable, http_status=exc.code)
                wait = _retry_after(exc.headers.get("Retry-After")) if exc.headers else 0
                exc.close()
            except (URLError, TimeoutError, socket.timeout, ConnectionError, OSError, HTTPException) as exc:
                error = APIError(str(exc), retryable=True)
                wait = 0
            if not error.retryable or retry == self.config.retries:
                raise error
            pause = max(wait, self.config.delay * 2**retry)
            LOG.warning("Retry %s for batch in %.1fs: %s", retry + 1, pause, error)
            self.sleep(pause)
        raise AssertionError("Unreachable")


def parse_entry(payload: bytes, expected_base: str, expected_version: int | None = None):
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise DataError(f"Malformed Atom XML: {exc}") from exc
    if root.tag != ATOM + "feed":
        raise DataError("Response is not an Atom feed")
    entries = root.findall(ATOM + "entry")
    if not entries:
        return None
    if len(entries) != 1:
        raise DataError("Expected exactly one entry for one identifier")
    entry = entries[0]
    identifier = entry.findtext(ATOM + "id", "")
    if "/api/errors" in identifier:
        raise APIError(entry.findtext(ATOM + "summary", "arXiv returned an API error"))
    try:
        base, version = normalize_id(identifier)
    except ValueError as exc:
        raise DataError(f"Invalid identifier in response: {identifier}") from exc
    if base != expected_base or version is None or (expected_version is not None and version != expected_version):
        raise DataError(f"Response identifier does not match request: {identifier}")
    title = " ".join(entry.findtext(ATOM + "title", "").split())
    authors = [" ".join(a.findtext(ATOM + "name", "").split()) for a in entry.findall(ATOM + "author")]
    published, updated = entry.findtext(ATOM + "published"), entry.findtext(ATOM + "updated")
    if not title or not authors or not all(authors) or not published or not updated:
        raise DataError("Missing title/authors/published/updated metadata")
    try:
        for date in (published, updated):
            parsed = datetime.fromisoformat(date.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("Timestamp has no timezone")
    except ValueError as exc:
        raise DataError(f"Invalid metadata date: {exc}") from exc
    return {"arxiv_id": base, "version": version, "title": title, "authors": authors,
            "submission_date": published, "updated_date": updated,
            "venue": entry.findtext(ARXIV + "journal_ref"),
            "abstract": " ".join(entry.findtext(ATOM + "summary", "").split()),
            "categories": [c.get("term") for c in entry.findall(ATOM + "category") if c.get("term")],
            "doi": entry.findtext(ARXIV + "doi")}


def parse_feed_entries(payload: bytes) -> dict[str, dict]:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise DataError(f"Malformed Atom XML: {exc}") from exc
    if root.tag != ATOM + "feed":
        raise DataError("Response is not an Atom feed")
    results = {}
    for entry in root.findall(ATOM + "entry"):
        identifier = entry.findtext(ATOM + "id", "")
        if "/api/errors" in identifier:
            continue
        try:
            base, version = normalize_id(identifier)
        except ValueError:
            continue
        title = " ".join(entry.findtext(ATOM + "title", "").split())
        authors = [" ".join(a.findtext(ATOM + "name", "").split()) for a in entry.findall(ATOM + "author")]
        published, updated = entry.findtext(ATOM + "published"), entry.findtext(ATOM + "updated")
        if not title or not authors or not all(authors) or not published or not updated:
            continue
        try:
            for date in (published, updated):
                parsed = datetime.fromisoformat(date.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError("Timestamp has no timezone")
        except ValueError:
            continue
        results[base] = {
            "arxiv_id": base, "version": version, "title": title, "authors": authors,
            "submission_date": published, "updated_date": updated,
            "venue": entry.findtext(ARXIV + "journal_ref"),
            "abstract": " ".join(entry.findtext(ATOM + "summary", "").split()),
            "categories": [c.get("term") for c in entry.findall(ATOM + "category") if c.get("term")],
            "doi": entry.findtext(ARXIV + "doi"),
        }
    return results


def discover(input_id: str, client: ArxivClient | None = None) -> DiscoveryRecord:
    record = DiscoveryRecord(input_id, None, None, "invalid_id", utc_now())
    try:
        base, requested = normalize_id(input_id)
    except ValueError as exc:
        record.error = ErrorInfo("invalid_id", str(exc))
        return record
    record.arxiv_id, record.requested_version = base, requested
    client = client or ArxivClient()
    before = client.attempts
    try:
        latest = parse_entry(client.fetch(base), base)
        if latest is None:
            record.status = "not_found"
            record.error = ErrorInfo("not_found", "No entry in successful Atom response")
            return record
        count = latest["version"]
        if requested is not None and requested > count:
            record.status = "not_found"
            record.error = ErrorInfo("version_not_found", f"Requested v{requested}; latest is v{count}")
            return record
        record.metadata = {k: v for k, v in latest.items() if k not in {"version", "updated_date"}}
        record.metadata.update({"latest_version": count, "revised_dates": [], "source": "arxiv_api",
                                "retrieved_at": record.timestamp})
        for version in range(1, count + 1):
            metadata = latest if version == count else parse_entry(client.fetch(f"{base}v{version}"), base, version)
            if metadata is None:
                raise DataError(f"Missing v{version} although latest is v{count}")
            source, corpus = version_paths(base, version)
            record.versions.append(VersionRecord(version, f"{base}v{version}",
                f"https://arxiv.org/src/{base}v{version}", source, corpus,
                metadata["submission_date"], metadata["updated_date"],
                metadata["title"], metadata["authors"], metadata["venue"]))
            if version == 1:
                record.metadata["submission_date"] = metadata["submission_date"]
            else:
                record.metadata["revised_dates"].append(metadata["updated_date"])
        record.status = "success"
    except APIError as exc:
        record.status = "api_error"
        record.error = ErrorInfo("api_error", str(exc), exc.retryable, exc.http_status)
    except DataError as exc:
        record.status = "data_error"
        record.error = ErrorInfo("data_error", str(exc))
    finally:
        record.attempts = client.attempts - before
    return record


def discover_many(identifiers, client=None):
    """Yield one record per input; individual failures do not stop the batch."""
    client = client or ArxivClient()
    for identifier in identifiers:
        yield discover(identifier, client)
