"""Semantic Scholar as a source of References.

Interface: S2Source(state_file).fetch(arxiv_ids) yields one Outcome per id, batch by batch.
Hidden inside: global throttle, 429/5xx backoff, batch splitting on HTTP 400, paginated
fallback for truncated reference lists, normalization and de-duplication.
"""
import logging
import random
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

from . import config

log = logging.getLogger("arxiv_pipeline.s2")


@dataclass
class Outcome:
    """Result of crawling one Paper from Semantic Scholar."""
    arxiv_id: str
    status: str                       # ok | ok_empty | not_found | error
    s2_paper_id: str | None = None
    venue: str | None = None
    references: list = field(default_factory=list)
    paginated: bool = False
    seconds: float = 0.0
    error: str | None = None          # set when status == "error"
    attempts: int = 0                 # HTTP attempts spent on this paper's batch


class S2Error(RuntimeError):
    """Per-request failure that is recorded as a Paper-level "error"."""


class S2RetryError(S2Error):
    """Still failing (429/5xx/timeout) after all retries."""


class S2BadRequest(S2Error):
    """HTTP 400, e.g. batch response too large; the batch may be split."""


def normalize_ref(ref: dict) -> dict:
    ext = ref.get("externalIds") or {}
    date = ref.get("publicationDate") or (str(ref["year"]) if ref.get("year") else None)
    return {
        "s2_id": ref.get("paperId"),
        "title": ref.get("title"),
        "authors": [a.get("name") for a in (ref.get("authors") or []) if a.get("name")],
        "submission_date": date,
        "year": ref.get("year"),
        "venue": ref.get("venue") or None,
        "arxiv_id": ext.get("ArXiv"), 
        "external_ids": ext,
    }


def clean_refs(raw) -> list:
    """Normalize, drop empty entries and de-duplicate (by S2 id, else by arXiv id)."""
    out, seen = [], set()
    for r in raw:
        n = normalize_ref(r)
        if not (n["s2_id"] or n["title"] or n["external_ids"]):
            continue
        key = n["s2_id"] or (n["arxiv_id"] and ("arxiv", n["arxiv_id"]))
        if key:
            if key in seen:
                continue
            seen.add(key)
        out.append(n)
    return out


_FAILED = object()
_FAILED_MESSAGE = "Semantic Scholar request failed after retries, or was rejected (HTTP 400)"


class S2Source:
    def __init__(self, state_file: Path):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = config.USER_AGENT
        if config.S2_API_KEY:
            self.session.headers["x-api-key"] = config.S2_API_KEY
            self.min_interval = config.S2_MIN_INTERVAL
            log.info("S2 API key loaded (...%s): 1 call / %.1fs", config.S2_API_KEY[-4:], self.min_interval)
        else:
            log.warning("S2_API_KEY missing: using the shared pool, throttled to 1 call / %.0fs "
                        "(slow, expect many 429)", config.S2_NOKEY_INTERVAL)
            self.min_interval = config.S2_NOKEY_INTERVAL
        self.state_file = state_file
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.last_call = self._load_last_call()   # wall-clock time of last response
        self.penalty_left = 0
        self.n_requests = 0
        self.n_429 = 0

    # ---- public --------------------------------------------------------------------
    def fetch(self, arxiv_ids):
        """Yield an Outcome for every id, in order, one batch at a time."""
        for start in range(0, len(arxiv_ids), config.S2_BATCH_SIZE):
            chunk = arxiv_ids[start:start + config.S2_BATCH_SIZE]
            t0 = time.time()
            results = self._batch_split(chunk)
            t_batch = (time.time() - t0) / len(chunk)    # batch time shared evenly per paper
            for aid, data in zip(chunk, results):
                t1 = time.time()
                outcome = self._to_outcome(aid, data)
                outcome.seconds = t_batch + time.time() - t1
                yield outcome

    # ---- batch → outcome -----------------------------------------------------------
    def _to_outcome(self, aid, data) -> Outcome:
        if data is _FAILED:
            return Outcome(aid, "error", error=_FAILED_MESSAGE)
        if data is None:
            return Outcome(aid, "not_found")
        raw, paginated = list(data.get("references") or []), False
        if len(raw) < (data.get("referenceCount") or 0):   # truncated -> paginated endpoint
            try:
                raw, paginated = self._references_paged(aid) or raw, True
            except S2Error as e:
                log.error("%s: %s", aid, e)
                return Outcome(aid, "error", error=_FAILED_MESSAGE)
        refs = clean_refs(raw)
        return Outcome(aid, "ok" if refs else "ok_empty", s2_paper_id=data.get("paperId"),
                       venue=data.get("venue") or None, references=refs, paginated=paginated)

    def _batch_split(self, chunk):
        """Batch call; on HTTP 400 halve the batch (down to 1 id -> failed)."""
        try:
            return self._request(
                "POST", "/paper/batch",
                params={"fields": config.REF_FIELDS},
                json={"ids": [f"arXiv:{i}" for i in chunk]},
            )
        except S2BadRequest as e:
            if len(chunk) == 1:
                log.error("%s: %s", chunk[0], e)
                return [_FAILED]
            mid = len(chunk) // 2
            log.warning("batch of %d got 400, splitting", len(chunk))
            return self._batch_split(chunk[:mid]) + self._batch_split(chunk[mid:])
        except S2RetryError as e:
            log.error("batch failed: %s", e)
            return [_FAILED] * len(chunk)

    def _references_paged(self, arxiv_id):
        """Fetch all references of one paper via the paginated endpoint."""
        out, offset = [], 0
        while True:
            data = self._request(
                "GET", f"/paper/arXiv:{arxiv_id}/references",
                params={"fields": config.REF_PAGE_FIELDS, "limit": 1000, "offset": offset},
            )
            if not data:
                break
            out.extend(item.get("citedPaper") or {} for item in data.get("data", []))
            if data.get("next") is None:
                break
            offset = data["next"]
        return out

    # ---- HTTP: throttle + retry ----------------------------------------------------
    def _load_last_call(self) -> float:
        try:
            return float(self.state_file.read_text())
        except (OSError, ValueError):
            return 0.0

    def _wait(self):
        interval = max(self.min_interval, config.S2_PENALTY_INTERVAL) if self.penalty_left > 0 else self.min_interval
        delay = self.last_call + interval - time.time()
        if delay > 0:
            time.sleep(delay)

    def _mark_done(self):
        self.last_call = time.time()
        try:
            self.state_file.write_text(str(self.last_call))
        except OSError:
            pass
        if self.penalty_left > 0:
            self.penalty_left -= 1

    def _request(self, method: str, path: str, **kwargs):
        """Throttled request. Returns parsed JSON, or None on 404."""
        url = f"{config.S2_BASE}{path}"
        for attempt in range(config.S2_MAX_RETRIES):
            self._wait()
            self.n_requests += 1
            try:
                resp = self.session.request(method, url, timeout=60, **kwargs)
            except (requests.Timeout, requests.ConnectionError) as e:
                self._mark_done()
                self._sleep_backoff(attempt, None, repr(e))
                continue
            self._mark_done()
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 404:
                return None
            if resp.status_code == 429:
                self.n_429 += 1
                self.penalty_left = config.S2_PENALTY_CALLS
            if resp.status_code == 429 or resp.status_code >= 500:
                self._sleep_backoff(attempt, resp.headers.get("Retry-After"), str(resp.status_code))
                continue
            if resp.status_code == 400:
                raise S2BadRequest(f"S2 400 on {path}: {resp.text[:300]}")
            raise RuntimeError(f"S2 {resp.status_code} on {path}: {resp.text[:300]}")
        raise S2RetryError(f"S2 request failed after {config.S2_MAX_RETRIES} attempts: {path}")

    @staticmethod
    def _sleep_backoff(attempt, retry_after, why):
        try:
            wait = float(retry_after)
        except (TypeError, ValueError):
            wait = min(2 ** (attempt + 1), config.S2_BACKOFF_CAP)
        wait += random.uniform(0, 1)
        log.warning("S2 retry (%s), sleeping %.1fs", why, wait)
        time.sleep(wait)
