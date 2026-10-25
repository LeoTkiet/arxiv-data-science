"""Unit tests for TV2 Source Downloader (tests/crawling/test_downloader.py).

All tests run completely offline using unittest.mock to ensure instantaneous execution.
"""

import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
import requests

from arxiv_pipeline.crawling.downloader import ArxivDownloader, detect_format, run
from arxiv_pipeline.common.schemas import StageResult


class MockResponse:
    """Simulates a requests.Response object for streaming content."""

    def __init__(self, chunks: list[bytes], status_code: int = 200, headers: dict | None = None):
        self.chunks = chunks
        self.status_code = status_code
        self.headers = headers or {}
        self.closed = False

    def iter_content(self, chunk_size: int = 64 * 1024):
        for chunk in self.chunks:
            yield chunk

    def raise_for_status(self):
        if self.status_code >= 400:
            exc = requests.exceptions.HTTPError(f"HTTP {self.status_code}")
            exc.response = self
            raise exc

    def close(self):
        self.closed = True


@pytest.fixture
def downloader():
    """Returns a downloader with 0 rate limit for instant test execution."""
    dl = ArxivDownloader(rate_limit=0.0, max_retries=2)
    return dl


def test_detect_format():
    """Verify magic bytes classification."""
    assert detect_format(b"%PDF-1.5", "application/pdf") == "pdf"
    assert detect_format(b"\x1f\x8b\x08", "application/x-eprint-tar") == "tar.gz"
    assert detect_format(b"\x1f\x8b\x08", "application/x-eprint") == "gz"
    assert detect_format(b"<!DOCTYPE html>", "text/html") == "html"
    assert detect_format(b"\\documentclass{article}", "text/plain") == "tex"


def test_download_tar_gz_success(tmp_path: Path, downloader: ArxivDownloader):
    """Test successful download, SHA-256 calculation and atomic rename."""
    content = b"\x1f\x8b\x08mock_tar_gz_archive_payload_data"
    expected_sha = hashlib.sha256(content).hexdigest()

    mock_resp = MockResponse([content], status_code=200, headers={"Content-Type": "application/x-eprint-tar"})
    target = tmp_path / "raw/2301.00001/v1/source"
    meta = tmp_path / "raw/2301.00001/v1/download_metadata.json"

    with patch.object(downloader.session, "get", return_value=mock_resp):
        res = downloader.download_version("2301.00001", 1, target, meta)

    assert res["status"] == "success"
    assert res["source_format"] == "tar.gz"
    assert res["raw_size_bytes"] == len(content)
    assert res["sha256"] == expected_sha
    assert target.exists()
    assert target.read_bytes() == content
    assert meta.exists()


def test_pdf_only_detection_and_early_close(tmp_path: Path, downloader: ArxivDownloader):
    """Verify that PDF streams are closed early without saving garbage to disk."""
    pdf_header = b"%PDF-1.4 header bytes..."
    mock_resp = MockResponse([pdf_header], status_code=200, headers={"Content-Type": "application/pdf", "Content-Length": "1048576"})
    target = tmp_path / "raw/2301.00002/v1/source"
    meta = tmp_path / "raw/2301.00002/v1/download_metadata.json"

    with patch.object(downloader.session, "get", return_value=mock_resp):
        res = downloader.download_version("2301.00002", 1, target, meta)

    assert res["status"] == "no_source"
    assert res["source_format"] == "pdf"
    assert mock_resp.closed is True
    # Crucial: source file should NOT be saved for PDF-only
    assert not target.exists()
    assert meta.exists()


def test_multi_version_no_overwrite(tmp_path: Path, downloader: ArxivDownloader):
    """Ensure v1 and v2 are kept in isolated version directories."""
    v1_content = b"\x1f\x8bv1_content"
    v2_content = b"\x1f\x8bv2_content"

    target_v1 = tmp_path / "raw/2301.00003/v1/source"
    meta_v1 = tmp_path / "raw/2301.00003/v1/download_metadata.json"
    target_v2 = tmp_path / "raw/2301.00003/v2/source"
    meta_v2 = tmp_path / "raw/2301.00003/v2/download_metadata.json"

    with patch.object(downloader.session, "get", side_effect=[
        MockResponse([v1_content], status_code=200, headers={"Content-Type": "application/x-eprint-tar"}),
        MockResponse([v2_content], status_code=200, headers={"Content-Type": "application/x-eprint-tar"}),
    ]):
        res_v1 = downloader.download_version("2301.00003", 1, target_v1, meta_v1)
        res_v2 = downloader.download_version("2301.00003", 2, target_v2, meta_v2)

    assert res_v1["status"] == "success"
    assert res_v2["status"] == "success"
    assert target_v1.read_bytes() == v1_content
    assert target_v2.read_bytes() == v2_content


def test_http_429_retry_success(tmp_path: Path, downloader: ArxivDownloader):
    """Verify exponential backoff retry on HTTP 429."""
    success_content = b"\x1f\x8bsuccess_after_retry"
    resp_429 = MockResponse([], status_code=429, headers={"Retry-After": "0"})
    resp_200 = MockResponse([success_content], status_code=200, headers={"Content-Type": "application/x-eprint-tar"})

    target = tmp_path / "raw/2301.00004/v1/source"
    meta = tmp_path / "raw/2301.00004/v1/download_metadata.json"

    with patch.object(downloader.session, "get", side_effect=[resp_429, resp_200]):
        with patch("time.sleep"):  # Mock sleep so tests run instantly
            res = downloader.download_version("2301.00004", 1, target, meta)

    assert res["status"] == "success"
    assert res["attempts"] == 2
    assert target.exists()


def test_network_error_cleans_up_part_file(tmp_path: Path, downloader: ArxivDownloader):
    """Verify that interrupted downloads delete temporary .part files."""
    target = tmp_path / "raw/2301.00005/v1/source"
    meta = tmp_path / "raw/2301.00005/v1/download_metadata.json"
    part_file = target.with_suffix(".part")

    def make_bad_response(*args, **kwargs):
        def interrupted_chunks():
            yield b"\x1f\x8bchunk1"
            raise requests.exceptions.ConnectionError("Connection abruptly terminated")
        return MockResponse(interrupted_chunks(), status_code=200)

    with patch.object(downloader.session, "get", side_effect=make_bad_response):
        with patch("time.sleep"):
            res = downloader.download_version("2301.00005", 1, target, meta)

    assert res["status"] == "api_error"
    assert not target.exists()
    assert not part_file.exists()  # .part must be cleaned up in finally block



def test_http_404_not_found(tmp_path: Path, downloader: ArxivDownloader):
    """HTTP 404 should terminate immediately without retries."""
    resp_404 = MockResponse([], status_code=404)
    target = tmp_path / "raw/2301.99999/v9/source"
    meta = tmp_path / "raw/2301.99999/v9/download_metadata.json"

    with patch.object(downloader.session, "get", return_value=resp_404) as mock_get:
        res = downloader.download_version("2301.99999", 9, target, meta)

    assert res["status"] == "not_found"
    assert mock_get.call_count == 1  # No useless retry on 404
    assert not target.exists()


def test_resume_skips_download(tmp_path: Path, downloader: ArxivDownloader):
    """Existing valid target should skip network request entirely."""
    target = tmp_path / "raw/2301.00006/v1/source"
    meta = tmp_path / "raw/2301.00006/v1/download_metadata.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"existing_source")
    meta.write_text(json.dumps({"status": "success", "raw_size_bytes": 15}), encoding="utf-8")

    with patch.object(downloader.session, "get") as mock_get:
        res = downloader.download_version("2301.00006", 1, target, meta)

    assert res["status"] == "success"
    assert mock_get.call_count == 0  # Zero network calls


def test_adapter_run_interface(tmp_path: Path):
    """Test the run() adapter function returning StageResult conforming to CONTRACTS.md."""
    record = {"arxiv_id": "2301.00007"}
    version = {
        "version": 1,
        "source_path": "raw/2301.00007/v1/source",
        "corpus_path": "corpus/2301.00007/v1",
    }
    content = b"\x1f\x8bcontent"
    mock_resp = MockResponse([content], status_code=200, headers={"Content-Type": "application/x-eprint-tar"})

    with patch("arxiv_pipeline.crawling.downloader.get_downloader") as mock_get_dl:
        mock_dl = ArxivDownloader(rate_limit=0.0)
        mock_get_dl.return_value = mock_dl
        with patch.object(mock_dl.session, "get", return_value=mock_resp):
            stage_result = run(record, tmp_path, version=version)

    assert isinstance(stage_result, StageResult)
    assert stage_result.stage == "download"
    assert stage_result.paper_id == "2301.00007"
    assert stage_result.version == 1
    assert stage_result.status == "success"
    assert "source" in stage_result.artifacts
    assert (tmp_path / stage_result.artifacts["source"]).exists()
