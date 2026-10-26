import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import json
import tempfile
from unittest import SkipTest, TestCase
from unittest.mock import patch

try:
    import requests  # noqa: F401  (declared dependency, see pyproject.toml)
except ImportError:
    raise SkipTest("requests is not installed: run python -m pip install -r requirements.txt")

from arxiv_pipeline.common.utils import atomic_json
from arxiv_pipeline.crawling.s2 import batch
from arxiv_pipeline.crawling.s2.s2_source import (Outcome, S2BadRequest, S2RetryError, S2Source,
                                                  clean_refs, normalize_ref)
from arxiv_pipeline.crawling.s2.store import progress_path, reference_row, store_outcome
from arxiv_pipeline.pipeline.crawl import _run_stage

REF = {"s2_id": "p1", "title": "T", "authors": ["A"], "submission_date": "2020-01-01",
       "arxiv_id": None, "year": 2020, "venue": None, "external_ids": {}}
RECORD = {"arxiv_id": "2301.00001"}


class FakeSource:
    """Stands in for S2Source: ok / ok_empty / not_found / error by last digit."""
    n_requests = 0

    def __init__(self):
        self.fetched = []

    def fetch(self, ids):
        for i in ids:
            self.fetched.append(i)
            kind = {"1": "ok", "2": "ok_empty", "3": "not_found"}.get(i[-1], "error")
            yield Outcome(i, kind, s2_paper_id="x" if kind in ("ok", "ok_empty") else None,
                          references=[REF] if kind == "ok" else [], error="boom" if kind == "error" else None)


class StoreTests(TestCase):
    def test_layout_and_contract_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = store_outcome(Outcome("2301.00001", "ok", references=[REF]), tmp)
            self.assertEqual((result.status, result.version), ("success", None))
            rows = json.loads((Path(tmp) / "references/2301.00001.json").read_text())
            self.assertEqual(rows[0]["ref_s2_id"], "p1")
            self.assertEqual(rows[0]["missing_fields"], ["ref_arxiv_id"])
            self.assertTrue((Path(tmp) / "references/2301.00001.provenance.json").exists())

    def test_progress_log_one_line_per_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_outcome(Outcome("2301.00004", "error", error="boom", seconds=1.23456), tmp)
            store_outcome(Outcome("2301.00004", "ok", references=[REF], paginated=True), tmp)
            lines = [json.loads(l) for l in progress_path(Path(tmp)).read_text().splitlines()]
            self.assertEqual([l["status"] for l in lines], ["error", "ok"])
            self.assertEqual(lines[0], {"arxiv_id": "2301.00004", "status": "error", "n_refs": 0,
                                        "paginated": False, "seconds": 1.235})
            self.assertEqual((lines[1]["n_refs"], lines[1]["paginated"]), (1, True))

    def test_old_style_id_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_outcome(Outcome("hep-th/9901001", "ok_empty"), tmp)
            self.assertTrue((Path(tmp) / "references/hep-th_9901001.json").exists())



class ArxivIdTests(TestCase):
    """ref_arxiv_id is exactly what Semantic Scholar reports; nothing is inferred."""

    def arxiv_id(self, ext):
        return normalize_ref({"paperId": "q", "externalIds": ext})["arxiv_id"]

    def test_taken_from_s2_field(self):
        self.assertEqual(self.arxiv_id({"ArXiv": "2301.00001"}), "2301.00001")
        self.assertEqual(self.arxiv_id({"ArXiv": "hep-th/9901001"}), "hep-th/9901001")   # legacy kept as-is

    def test_arxiv_doi_is_not_turned_into_an_id(self):
        for ext in ({"DOI": "10.48550/arXiv.2306.09896"}, {"DOI": "10.48550/arXiv.2306.09896v2"},
                    {"DOI": "10.1109/CVPR.2016.90"}, {"DOI": None}, {}, None):
            with self.subTest(ext=ext):
                self.assertIsNone(self.arxiv_id(ext))

    def test_missing_arxiv_id_is_marked(self):
        row = reference_row("2301.00001", normalize_ref({"paperId": "q", "title": "T",
                            "externalIds": {"DOI": "10.48550/arXiv.2306.09896"}}))
        self.assertIsNone(row["ref_arxiv_id"])
        self.assertIn("ref_arxiv_id", row["missing_fields"])


class BatchResumeTests(TestCase):
    def test_orchestrator_reuses_batch_results(self):
        """Batch pre-fill -> orchestrator resume must not call the adapter again."""
        with tempfile.TemporaryDirectory() as tmp, patch.object(batch.config, "RETRY_PASSES", 0):
            out = Path(tmp)
            ids = ["2301.00001", "2301.00002", "2301.00003"]
            summary = batch.crawl(ids, out, FakeSource())
            self.assertEqual((summary["ok"], summary["ok_empty"], summary["not_found"]), (1, 1, 1))
            for i in ids:
                with patch("importlib.import_module", side_effect=AssertionError("adapter called")):
                    result = _run_stage("references", {"arxiv_id": i}, out, None, None, True)
                self.assertIn(result["status"], {"success", "no_data", "not_indexed"})

    def test_rerun_only_retries_errors(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(batch.config, "RETRY_PASSES", 0):
            out, ids = Path(tmp), ["2301.00001", "2301.00004"]
            batch.crawl(ids, out, FakeSource())
            again = FakeSource()
            summary = batch.crawl(ids, out, again)
            self.assertEqual(again.fetched, ["2301.00004"])
            self.assertEqual((summary["skipped"], summary["error"]), (1, 1))

    def test_error_is_not_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_outcome(Outcome("2301.00004", "error", error="boom"), tmp)
            self.assertFalse(batch.is_done(Path(tmp), "2301.00004"))


def raw_ref(pid=None, title=None, arxiv=None, **extra):
    """A reference object as Semantic Scholar returns it."""
    return {"paperId": pid, "title": title, "externalIds": {"ArXiv": arxiv} if arxiv else None, **extra}


class ParsingTests(TestCase):
    def test_all_fields(self):
        ref = normalize_ref({"paperId": "p1", "title": "Attention Is All You Need",
                             "authors": [{"name": "A. Vaswani"}, {"name": None}, {}, {"name": "N. Shazeer"}],
                             "year": 2017, "publicationDate": "2017-06-12", "venue": "NeurIPS",
                             "externalIds": {"ArXiv": "1706.03762", "DOI": "10.5555/x"}})
        self.assertEqual(ref, {"s2_id": "p1", "title": "Attention Is All You Need",
                               "authors": ["A. Vaswani", "N. Shazeer"],      # nameless authors dropped
                               "submission_date": "2017-06-12", "year": 2017, "venue": "NeurIPS",
                               "arxiv_id": "1706.03762",
                               "external_ids": {"ArXiv": "1706.03762", "DOI": "10.5555/x"}})

    def test_date_falls_back_to_year_only(self):
        self.assertEqual(normalize_ref({"year": 2019})["submission_date"], "2019")
        self.assertIsNone(normalize_ref({})["submission_date"])

    def test_missing_values_are_null_or_empty(self):
        ref = normalize_ref({"paperId": None, "authors": None, "venue": "", "externalIds": None})
        self.assertEqual((ref["s2_id"], ref["title"], ref["authors"], ref["venue"], ref["arxiv_id"]),
                         (None, None, [], None, None))


class DuplicateTests(TestCase):
    def test_same_s2_id_kept_once(self):
        refs = clean_refs([raw_ref("p1", "A"), raw_ref("p2", "B"), raw_ref("p1", "A again")])
        self.assertEqual([(r["s2_id"], r["title"]) for r in refs], [("p1", "A"), ("p2", "B")])

    def test_same_arxiv_id_without_s2_id_kept_once(self):
        refs = clean_refs([raw_ref(None, "A", "2301.00001"), raw_ref(None, "A'", "2301.00001")])
        self.assertEqual(len(refs), 1)

    def test_title_only_entries_are_not_merged(self):
        """Without any ID two entries cannot be proven identical, so both are kept."""
        self.assertEqual(len(clean_refs([raw_ref(None, "Same title"), raw_ref(None, "Same title")])), 2)

    def test_empty_entries_dropped(self):
        refs = clean_refs([{}, raw_ref(), raw_ref(None, None, None, authors=[{"name": "X"}]), raw_ref("p1")])
        self.assertEqual([r["s2_id"] for r in refs], ["p1"])


class EmptyAndMissingTests(TestCase):
    def read(self, tmp, name):
        return json.loads((Path(tmp) / "references" / name).read_text())

    def test_found_without_references_is_no_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = store_outcome(Outcome("2301.00002", "ok_empty", s2_paper_id="x"), tmp)
            self.assertEqual(result.status, "no_data")
            self.assertEqual(self.read(tmp, "2301.00002.json"), [])
            prov = self.read(tmp, "2301.00002.provenance.json")
            self.assertEqual((prov["source"], prov["status"], prov["reference_count"]),
                             ("semantic_scholar", "no_data", 0))
            self.assertIn("retrieved_at", prov)

    def test_unknown_to_s2_is_not_indexed(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = store_outcome(Outcome("2301.00003", "not_found"), tmp)
            self.assertEqual(result.status, "not_indexed")
            self.assertIsNone(self.read(tmp, "2301.00003.provenance.json")["s2_paper_id"])

    def test_api_failure_writes_no_data_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = store_outcome(Outcome("2301.00004", "error", error="429 after retries"), tmp)
            self.assertEqual((result.status, result.artifacts), ("api_error", {}))
            self.assertTrue(result.error.retryable)
            self.assertFalse((Path(tmp) / "references/2301.00004.json").exists())

    def test_row_with_everything_missing(self):
        row = reference_row("2301.00001", normalize_ref({}))
        self.assertEqual(row["missing_fields"],
                         ["ref_arxiv_id", "ref_title", "ref_authors", "ref_submission_date", "ref_s2_id"])
        self.assertEqual(row["ref_authors"], [])


class FetchTests(TestCase):
    """S2Source.fetch with the HTTP layer replaced: no network, no throttling."""

    def fetch(self, responses, ids):
        def fake(method, path, **kwargs):
            answer = responses.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        with tempfile.TemporaryDirectory() as tmp:
            source = S2Source(Path(tmp) / "last_call")
            with patch.object(source, "_request", side_effect=fake):
                return list(source.fetch(ids))

    def test_statuses_from_batch_response(self):
        batch_answer = [{"paperId": "a", "referenceCount": 1, "references": [raw_ref("p1", "T")]},
                        {"paperId": "b", "referenceCount": 0, "references": []},
                        None]
        out = self.fetch([batch_answer], ["2301.00001", "2301.00002", "2301.00003"])
        self.assertEqual([o.status for o in out], ["ok", "ok_empty", "not_found"])

    def test_duplicates_removed_from_response(self):
        answer = [{"paperId": "a", "referenceCount": 2, "references": [raw_ref("p1", "T"), raw_ref("p1", "T")]}]
        out = self.fetch([answer, {"data": [], "next": None}], ["2301.00001"])
        self.assertEqual(len(out[0].references), 1)

    def test_truncated_list_uses_paginated_endpoint(self):
        answer = [{"paperId": "a", "referenceCount": 3, "references": [raw_ref("p1", "T1")]}]
        page = {"data": [{"citedPaper": raw_ref(f"p{i}", f"T{i}")} for i in (1, 2, 3)], "next": None}
        out = self.fetch([answer, page], ["2301.00001"])
        self.assertTrue(out[0].paginated)
        self.assertEqual([r["s2_id"] for r in out[0].references], ["p1", "p2", "p3"])

    def test_bad_request_splits_batch_down_to_the_bad_id(self):
        ok = {"paperId": "a", "referenceCount": 0, "references": []}
        out = self.fetch([S2BadRequest("too big"), [ok], S2BadRequest("bad id")], ["2301.00001", "2301.00002"])
        self.assertEqual([(o.arxiv_id, o.status) for o in out], [("2301.00001", "ok_empty"), ("2301.00002", "error")])

    def test_exhausted_retries_mark_whole_batch_error(self):
        out = self.fetch([S2RetryError("429")], ["2301.00001", "2301.00002"])
        self.assertEqual([o.status for o in out], ["error", "error"])
        self.assertTrue(all(o.error for o in out))
