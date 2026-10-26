import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from io import BytesIO
from urllib.error import HTTPError, URLError
from http.client import IncompleteRead
from unittest import TestCase
from unittest.mock import Mock

from arxiv_pipeline.common.config import APIConfig
from arxiv_pipeline.common.utils import normalize_id, paper_key
from arxiv_pipeline.crawling.discovery import ArxivClient, discover, parse_entry, DataError
from helpers import feed, EMPTY, fixture_client


class IdentifierTests(TestCase):
    def test_forms(self):
        for text in ("2301.00001v2", "arXiv:2301.00001v2", "https://arxiv.org/abs/2301.00001v2",
                     "https://arxiv.org/pdf/2301.00001v2.pdf"):
            self.assertEqual(normalize_id(text), ("2301.00001", 2))
        self.assertEqual(normalize_id("0704.0001"), ("0704.0001", None))
        self.assertEqual(normalize_id("hep-th/9901001v3"), ("hep-th/9901001", 3))
        self.assertEqual(paper_key("hep-th/9901001"), "hep-th_9901001")

    def test_invalid_ids(self):
        for text in ("../secret", "2313.00001", "2301.00000", "2301.1234", "0703.0001",
                     "1501.0001", "hep-th/0801001", "2301.00001v0", "https://evil.com/abs/2301.00001"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                normalize_id(text)


class DiscoveryTests(TestCase):
    def test_every_version_and_dates(self):
        client, opener = fixture_client()
        result = discover("2301.00001", client)
        self.assertEqual(result.status, "success")
        self.assertEqual([v.version for v in result.versions], [1, 2])
        self.assertEqual(opener.calls, ["2301.00001", "2301.00001v1"])
        self.assertEqual(result.attempts, 2)
        self.assertEqual(result.metadata["title"], "Test Paper")
        self.assertEqual(result.metadata["revised_dates"], ["2023-01-02T00:00:00Z"])
        self.assertEqual(result.versions[0].source_path, "raw/2301.00001/v1/source")
        self.assertEqual([(v.title, v.authors, v.venue) for v in result.versions],
                         [("Test Paper", ["Alice Example"], "Test Journal")] * 2)      # kept for every version

    def test_legacy_and_single_version(self):
        client, _ = fixture_client({"hep-th/9901001": feed("hep-th/9901001", 1)})
        record = discover("hep-th/9901001", client)
        self.assertEqual(record.status, "success")
        self.assertEqual(record.versions[0].corpus_path, "corpus/hep-th_9901001/v1")
        self.assertEqual(record.metadata["revised_dates"], [])

    def test_requested_version_still_discovers_all(self):
        client, _ = fixture_client()
        self.assertEqual(len(discover("2301.00001v1", client).versions), 2)

    def test_requested_version_missing(self):
        client, _ = fixture_client()
        record = discover("2301.00001v3", client)
        self.assertEqual(record.status, "not_found")
        self.assertEqual(record.error.kind, "version_not_found")

    def test_invalid_no_network(self):
        client, opener = fixture_client()
        self.assertEqual(discover("bad", client).status, "invalid_id")
        self.assertEqual(opener.calls, [])

    def test_not_found(self):
        client, _ = fixture_client()
        self.assertEqual(discover("2301.99999", client).status, "not_found")

    def test_data_errors(self):
        for payload in (b'bad xml', b'<feed/>', feed("2301.00002"),
                        feed().replace(b'<title> Test   Paper </title>', b''),
                        feed().replace(b'2023-01-01T00:00:00Z', b'bad-date')):
            with self.subTest(payload=payload):
                client, _ = fixture_client({"2301.00001": payload})
                self.assertEqual(discover("2301.00001", client).status, "data_error")

    def test_missing_intermediate_version(self):
        client, _ = fixture_client({"2301.00001": feed(), "2301.00001v1": EMPTY})
        self.assertEqual(discover("2301.00001", client).status, "data_error")

    def test_wrong_intermediate_version(self):
        client, _ = fixture_client({"2301.00001": feed(), "2301.00001v1": feed(version=2)})
        self.assertEqual(discover("2301.00001", client).status, "data_error")

    def test_atom_error_is_api_error(self):
        payload = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/api/errors#bad</id><summary>Bad query</summary></entry></feed>'
        client, _ = fixture_client({"2301.00001": payload})
        self.assertEqual(discover("2301.00001", client).status, "api_error")


class HTTPTests(TestCase):
    def test_retry_after_and_attempts(self):
        opener = Mock(side_effect=[HTTPError("url", 429, "slow down", {"Retry-After": "10"}, None), BytesIO(feed(version=1))])
        sleep = Mock()
        client = ArxivClient(opener=opener, sleep=sleep)
        result = discover("2301.00001", client)
        self.assertEqual(result.status, "success")
        self.assertEqual(result.attempts, 2)
        self.assertIn(((10.0,), {}), sleep.call_args_list)

    def test_exhausted_network_and_incomplete_read(self):
        for error in (URLError("offline"), IncompleteRead(b"partial")):
            client = ArxivClient(APIConfig(retries=1), opener=Mock(side_effect=error), sleep=lambda _: None)
            result = discover("2301.00001", client)
            self.assertEqual(result.status, "api_error")
            self.assertTrue(result.error.retryable)
            self.assertEqual(result.attempts, 2)

    def test_500_retry_and_400_no_retry(self):
        for code, attempts in ((500, 2), (400, 1), (404, 1)):
            client = ArxivClient(APIConfig(retries=1),
                                 opener=Mock(side_effect=HTTPError("url", code, "failure", {}, None)), sleep=lambda _: None)
            record = discover("2301.00001", client)
            self.assertEqual(record.status, "api_error")
            self.assertEqual(record.error.http_status, code)
            self.assertEqual(record.attempts, attempts)

    def test_rate_limit_between_successful_requests(self):
        sleep = Mock()
        client = ArxivClient(opener=Mock(side_effect=[BytesIO(feed()), BytesIO(feed(version=1))]),
                             sleep=sleep, clock=lambda: 100.0)
        self.assertEqual(discover("2301.00001", client).status, "success")
        sleep.assert_called_once_with(3.0)

    def test_bad_config(self):
        for values in ({"delay": 0}, {"timeout": 0}, {"retries": -1}, {"delay": float("nan")}, {"timeout": float("inf")}):
            with self.assertRaises(ValueError):
                APIConfig(**values)
