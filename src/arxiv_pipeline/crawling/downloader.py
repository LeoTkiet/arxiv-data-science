"""Source Downloader module for Milestone 1 (TV2).

Complies with docs/CONTRACTS.md and the project specification:
- Strictly queries https://export.arxiv.org/src/{arxiv_id}v{version}
- Rate limiting >= 3.0s delay with single persistent session
- Magic bytes detection: early abort for PDF-only (status='no_source')
- Atomic streaming write via .part file with SHA-256 calculation
- Exponential backoff retry on 429/503 and network errors
- Emits sidecar download_metadata.json and returns StageResult
"""

import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import random
import time
from typing import Any
import requests

from arxiv_pipeline.common.schemas import StageResult, ErrorInfo
from arxiv_pipeline.common.utils import atomic_json, normalize_id, paper_key, utc_now

LOG = logging.getLogger("arxiv_pipeline")

ARXIV_SRC_BASE_URL = "https://export.arxiv.org/src"
DEFAULT_USER_AGENT = "arxiv-data-science/0.1 (academic corpus crawler; mailto:group-email@student.hcmus.edu.vn)"
CHUNK_SIZE = 64 * 1024  # 64 KB


def detect_format(first_chunk: bytes, content_type: str = "") -> str:
    """Detect actual source payload format using magic bytes and Content-Type."""
    ct = content_type.lower()
    if first_chunk.startswith(b"%PDF-") or "application/pdf" in ct:
        return "pdf"
    if first_chunk.startswith(b"\x1f\x8b"):
        return "tar.gz" if "tar" in ct or b"ustar" in first_chunk[:1024] else "gz"
    if first_chunk.startswith((b"<!DOCTYPE html", b"<html", b"<HTML")) or "text/html" in ct:
        return "html"
    if b"\\documentclass" in first_chunk or b"\\input" in first_chunk:
        return "tex"
    return "unknown"


class ArxivDownloader:
    """Handles rate-limited, fault-tolerant downloading of raw arXiv sources."""

    def __init__(
        self,
        rate_limit: float = 3.0,
        timeout: tuple[float, float] = (10.0, 60.0),
        max_retries: int = 4,
        user_agent: str = DEFAULT_USER_AGENT,
        session: requests.Session | None = None,
    ):
        self.rate_limit = max(3.0, rate_limit)
        self.timeout = timeout
        self.max_retries = max_retries
        self.user_agent = user_agent
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": self.user_agent})
        self._last_request_time: float = 0.0

    def _wait_rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_time
        if elapsed < self.rate_limit:
            time.sleep(self.rate_limit - elapsed)
        self._last_request_time = time.monotonic()

    def download_version(
        self,
        arxiv_id: str,
        version: int,
        target_path: Path,
        metadata_path: Path,
        force: bool = False,
    ) -> dict[str, Any]:
        """Downloads a single version of an arXiv paper with retry, streaming and atomic write."""
        versioned_id = f"{arxiv_id}v{version}"
        url = f"{ARXIV_SRC_BASE_URL}/{versioned_id}"

        # Resume check
        if not force and target_path.exists() and metadata_path.exists():
            try:
                meta = json.loads(metadata_path.read_text(encoding="utf-8"))
                if meta.get("status") in {"success", "no_source"}:
                    LOG.info("Resume download: %s already downloaded", versioned_id)
                    return meta
            except (OSError, ValueError):
                pass

        target_path.parent.mkdir(parents=True, exist_ok=True)
        part_path = target_path.with_suffix(".part")

        attempt = 0
        last_error = None
        start_time = time.time()

        while attempt <= self.max_retries:
            attempt += 1
            self._wait_rate_limit()
            response = None
            try:
                LOG.info("Downloading %s (attempt %d/%d)", url, attempt, self.max_retries + 1)
                response = self.session.get(url, stream=True, timeout=self.timeout)

                # HTTP 404: Not found (no retry)
                if response.status_code == 404:
                    result = {
                        "arxiv_id": arxiv_id,
                        "version": version,
                        "versioned_id": versioned_id,
                        "url": url,
                        "status": "not_found",
                        "http_status": 404,
                        "attempts": attempt,
                        "error": "Source archive not found on export.arxiv.org",
                        "timestamp": utc_now(),
                    }
                    atomic_json(metadata_path, result)
                    return result

                # HTTP 429 / 503 / Server errors: Exponential Backoff Retry
                if response.status_code in {429, 500, 502, 503, 504}:
                    retry_after = response.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after and retry_after.isdigit() else (
                        (2 ** attempt) + random.uniform(0.5, 1.5)
                    )
                    LOG.warning("HTTP %d for %s. Retrying in %.2fs", response.status_code, url, delay)
                    time.sleep(delay)
                    continue

                response.raise_for_status()

                # Read first chunk to inspect magic bytes
                stream_iter = response.iter_content(chunk_size=CHUNK_SIZE)
                try:
                    first_chunk = next(stream_iter)
                except StopIteration:
                    first_chunk = b""

                content_type = response.headers.get("Content-Type", "")
                fmt = detect_format(first_chunk, content_type)

                # Early abort for PDF-only submissions (Rule 4: no TeX source available)
                if fmt == "pdf":
                    response.close()
                    content_length = int(response.headers.get("Content-Length", 0))
                    LOG.info("Detected PDF-only submission for %s. Closing stream early.", versioned_id)
                    result = {
                        "arxiv_id": arxiv_id,
                        "version": version,
                        "versioned_id": versioned_id,
                        "url": url,
                        "status": "no_source",
                        "source_format": "pdf",
                        "http_status": response.status_code,
                        "content_type": content_type,
                        "raw_size_bytes": content_length,
                        "attempts": attempt,
                        "download_duration_sec": round(time.time() - start_time, 2),
                        "timestamp": utc_now(),
                    }
                    atomic_json(metadata_path, result)
                    return result

                # HTML response often means error or unavailable notice
                if fmt == "html":
                    response.close()
                    result = {
                        "arxiv_id": arxiv_id,
                        "version": version,
                        "versioned_id": versioned_id,
                        "url": url,
                        "status": "no_source",
                        "source_format": "html",
                        "http_status": response.status_code,
                        "content_type": content_type,
                        "raw_size_bytes": len(first_chunk),
                        "attempts": attempt,
                        "download_duration_sec": round(time.time() - start_time, 2),
                        "timestamp": utc_now(),
                    }
                    atomic_json(metadata_path, result)
                    return result

                # Valid archive / TeX source -> Stream download with atomic rename
                hasher = hashlib.sha256()
                total_bytes = 0

                try:
                    with open(part_path, "wb") as f:
                        if first_chunk:
                            f.write(first_chunk)
                            hasher.update(first_chunk)
                            total_bytes += len(first_chunk)
                        for chunk in stream_iter:
                            if chunk:
                                f.write(chunk)
                                hasher.update(chunk)
                                total_bytes += len(chunk)
                        if total_bytes == 0:
                            raise requests.exceptions.RequestException("Empty response: 0 bytes received")

                    os.replace(part_path, target_path)
                finally:
                    if part_path.exists():
                        try:
                            part_path.unlink()
                        except OSError:
                            pass

                result = {
                    "arxiv_id": arxiv_id,
                    "version": version,
                    "versioned_id": versioned_id,
                    "url": url,
                    "status": "success",
                    "source_format": fmt,
                    "http_status": response.status_code,
                    "content_type": content_type,
                    "sha256": hasher.hexdigest(),
                    "raw_size_bytes": total_bytes,
                    "attempts": attempt,
                    "download_duration_sec": round(time.time() - start_time, 2),
                    "timestamp": utc_now(),
                }
                atomic_json(metadata_path, result)
                return result

            except (requests.exceptions.RequestException, OSError) as exc:
                last_error = exc
                LOG.warning("Attempt %d failed for %s: %s", attempt, url, exc)
                delay = (2 ** attempt) + random.uniform(0.5, 1.5)
                time.sleep(delay)
            finally:
                if response:
                    response.close()

        # Retries exhausted
        error_msg = str(last_error) if last_error else "Max retries exceeded"
        result = {
            "arxiv_id": arxiv_id,
            "version": version,
            "versioned_id": versioned_id,
            "url": url,
            "status": "api_error",
            "attempts": attempt,
            "error": error_msg,
            "timestamp": utc_now(),
        }
        atomic_json(metadata_path, result)
        return result


# Singleton downloader instance for pipeline execution
_DOWNLOADER: ArxivDownloader | None = None


def get_downloader() -> ArxivDownloader:
    global _DOWNLOADER
    if _DOWNLOADER is None:
        _DOWNLOADER = ArxivDownloader()
    return _DOWNLOADER

    
def run(record: dict, output_dir: Path, *, version: dict | None = None, upstream: dict | None = None) -> StageResult:
    """Adapter entrypoint adhering to docs/CONTRACTS.md for stage 'download'."""
    output_dir = Path(output_dir)
    arxiv_id = record["arxiv_id"]
    if not version:
        raise ValueError("Download stage requires a version record")

    v_num = version["version"]
    key = paper_key(arxiv_id)

    # Relative paths defined in contracts
    source_rel = version.get("source_path", f"raw/{key}/v{v_num}/source")
    meta_rel = f"raw/{key}/v{v_num}/download_metadata.json"

    target_path = output_dir / source_rel
    metadata_path = output_dir / meta_rel

    downloader = get_downloader()
    download_info = downloader.download_version(
        arxiv_id=arxiv_id,
        version=v_num,
        target_path=target_path,
        metadata_path=metadata_path,
    )

    status = download_info.get("status")
    attempts = download_info.get("attempts", 1)
    raw_size = download_info.get("raw_size_bytes", 0)

    if status == "success":
        artifacts = {
            "source": source_rel,
            "download_metadata": meta_rel,
        }
        return StageResult(
            stage="download",
            paper_id=arxiv_id,
            version=v_num,
            status="success",
            timestamp=utc_now(),
            artifacts=artifacts,
            attempts=attempts,
            size_bytes=raw_size,
        )

    if status == "no_source":
        # Keep empty record with download_metadata artifact so validator/orchestrator can verify
        artifacts = {"download_metadata": meta_rel}
        return StageResult(
            stage="download",
            paper_id=arxiv_id,
            version=v_num,
            status="no_source",
            timestamp=utc_now(),
            artifacts=artifacts,
            attempts=attempts,
            size_bytes=0,
        )

    # Errors (not_found, api_error, etc.)
    err_msg = download_info.get("error", f"Download failed with status: {status}")
    http_code = download_info.get("http_status")
    return StageResult(
        stage="download",
        paper_id=arxiv_id,
        version=v_num,
        status="api_error" if status in {"api_error", "not_found"} else "error",
        timestamp=utc_now(),
        artifacts={"download_metadata": meta_rel} if metadata_path.exists() else {},
        error=ErrorInfo(kind=status or "error", message=err_msg, retryable=(status == "api_error"), http_status=http_code),
        attempts=attempts,
        size_bytes=0,
    )


def main(argv: list[str] | None = None) -> int:
    """CLI runner allowing TV2 to independently test source downloads."""
    parser = argparse.ArgumentParser(description="Download raw arXiv sources for given versions")
    parser.add_argument("--ids", nargs="+", required=True, help="List of versioned IDs, e.g. 2301.00001v1")
    parser.add_argument("--output", type=Path, default=Path("./downloads"), help="Output root directory")
    parser.add_argument("--force", action="store_true", help="Force re-download even if already present")
    args = parser.parse_args(argv)

    downloader = ArxivDownloader()
    print(f"Downloading {len(args.ids)} versions to {args.output}...")

    for vid in args.ids:
        try:
            base_id, v_num = normalize_id(vid)
            if v_num is None:
                v_num = 1
            key = paper_key(base_id)
            target = args.output / f"raw/{key}/v{v_num}/source"
            meta = args.output / f"raw/{key}/v{v_num}/download_metadata.json"
            res = downloader.download_version(base_id, v_num, target, meta, force=args.force)
            print(f"[{res['status'].upper()}] {vid} -> format: {res.get('source_format')}, bytes: {res.get('raw_size_bytes', 0)}")
        except Exception as e:
            print(f"[ERROR] {vid}: {e}")

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
