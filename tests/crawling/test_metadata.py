import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import copy
import json
import tempfile
from unittest import TestCase

from arxiv_pipeline.common.schemas import StageResult
from arxiv_pipeline.crawling import metadata
from arxiv_pipeline.crawling.discovery import discover
from helpers import fixture_client


def atom(identifier, version, *, title="A Title", authors=("Alice Example",), journal=None, doi=None,
         summary="An abstract", updated_day=None):
    """Atom entry for one version; `updated_day` defaults to the version number (v2 -> 2023-01-02)."""
    extra = (f"<arxiv:journal_ref>{journal}</arxiv:journal_ref>" if journal else "") + \
            (f"<arxiv:doi>{doi}</arxiv:doi>" if doi else "")
    people = "".join(f"<author><name>{a}</name></author>" for a in authors)
    return f'''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
      <entry><id>http://arxiv.org/abs/{identifier}v{version}</id><title> {title} </title>
      <summary>{summary}</summary>{people}
      <published>2023-01-01T00:00:00Z</published><updated>2023-01-0{updated_day or version}T00:00:00Z</updated>
      <category term="cs.AI"/><category term="cs.LG"/>{extra}</entry></feed>'''.encode()


def two_versions(identifier="2301.00001"):
    """v1 and v2 differ in title, authors and venue, as real revisions can."""
    return {identifier: atom(identifier, 2, title="Final Title", authors=("Alice Example", "Bob Example"),
                             journal="Some Journal 2024"),
            f"{identifier}v1": atom(identifier, 1, title="First Title", authors=("Alice Example",))}


def record_for(identifier="2301.00001", mapping=None):
    client, _ = fixture_client(mapping if mapping is not None else two_versions(identifier))
    return discover(identifier, client).to_dict()


class PaperLevelTests(TestCase):
    def test_papers_table_columns(self):
        doc = metadata.build_metadata(record_for())
        self.assertEqual((doc["arxiv_id"], doc["title"], doc["authors"]),
                         ("2301.00001", "Final Title", ["Alice Example", "Bob Example"]))   # latest version
        self.assertEqual(doc["submission_date"], "2023-01-01T00:00:00Z")                    # from v1
        self.assertEqual(doc["revised_dates"], ["2023-01-02T00:00:00Z"])                    # updated of v2
        self.assertEqual((doc["venue"], doc["latest_version"], doc["source"]), ("Some Journal 2024", 2, "arxiv_api"))
        self.assertIn("retrieved_at", doc)

    def test_single_version_has_no_revised_dates(self):
        doc = metadata.build_metadata(record_for("2301.00002", {"2301.00002": atom("2301.00002", 1)}))
        self.assertEqual((doc["revised_dates"], doc["latest_version"]), ([], 1))

    def test_missing_optional_values_are_null_and_listed(self):
        doc = metadata.build_metadata(record_for("2301.00003", {"2301.00003": atom("2301.00003", 1, summary="")}))
        self.assertEqual((doc["venue"], doc["doi"], doc["abstract"]), (None, None, None))
        self.assertEqual(doc["missing_fields"], ["venue", "doi", "abstract"])
        self.assertEqual(doc["categories"], ["cs.AI", "cs.LG"])                              # present, not missing

    def test_present_optional_values_are_kept_not_listed(self):
        mapping = {"2301.00003": atom("2301.00003", 1, journal="Some Journal", doi="10.1000/1")}
        doc = metadata.build_metadata(record_for("2301.00003", mapping))
        self.assertEqual((doc["venue"], doc["doi"], doc["missing_fields"]), ("Some Journal", "10.1000/1", []))

    def test_whitespace_in_title_is_normalized(self):
        mapping = {"2301.00003": atom("2301.00003", 1, title="  A   Title ")}
        self.assertEqual(metadata.build_metadata(record_for("2301.00003", mapping))["title"], "A Title")


class PerVersionTests(TestCase):
    """The statement asks for each version's own title, authors, dates and venue."""

    def test_each_version_keeps_its_own_metadata(self):
        v1, v2 = metadata.build_metadata(record_for())["versions"]
        self.assertEqual((v1["version"], v1["versioned_id"], v1["title"], v1["authors"], v1["venue"]),
                         (1, "2301.00001v1", "First Title", ["Alice Example"], None))
        self.assertEqual((v2["version"], v2["versioned_id"], v2["title"], v2["authors"], v2["venue"]),
                         (2, "2301.00001v2", "Final Title", ["Alice Example", "Bob Example"], "Some Journal 2024"))
        self.assertEqual((v1["submission_date"], v1["updated_date"]), ("2023-01-01T00:00:00Z", "2023-01-01T00:00:00Z"))
        self.assertEqual((v2["submission_date"], v2["updated_date"]), ("2023-01-01T00:00:00Z", "2023-01-02T00:00:00Z"))

    def test_version_missing_values_are_listed_per_version(self):
        v1, v2 = metadata.build_metadata(record_for())["versions"]
        self.assertEqual((v1["missing_fields"], v2["missing_fields"]), (["venue"], []))

    def test_title_change_does_not_leak_into_other_version(self):
        doc = metadata.build_metadata(record_for())
        self.assertNotEqual(doc["versions"][0]["title"], doc["title"])        # v1 keeps the old title

    def test_checkpoint_without_per_version_fields_is_still_valid(self):
        record = record_for()
        for v in record["versions"]:                                          # shape written before the change
            for key in ("title", "authors", "venue"):
                v.pop(key)
        doc = metadata.build_metadata(record)
        self.assertEqual([v["missing_fields"] for v in doc["versions"]], [["title", "authors", "venue"]] * 2)
        self.assertEqual((doc["versions"][0]["title"], doc["versions"][0]["authors"]), (None, []))
        self.assertEqual(doc["title"], "Final Title")                         # paper level unaffected


class InvalidRecordTests(TestCase):
    def check(self, mutate, message):
        record = copy.deepcopy(record_for())
        mutate(record)
        with self.assertRaisesRegex(ValueError, message):
            metadata.build_metadata(record)
        with tempfile.TemporaryDirectory() as tmp:
            result = metadata.run(record, tmp)
            self.assertEqual((result.status, result.artifacts), ("data_error", {}))
            self.assertFalse((Path(tmp) / "metadata").exists())               # nothing written on failure

    def test_not_a_successful_discovery(self):
        self.check(lambda r: r.update(status="not_found"), "not 'success'")

    def test_empty_title(self):
        self.check(lambda r: r["metadata"].update(title="  "), "title")

    def test_no_authors(self):
        self.check(lambda r: r["metadata"].update(authors=[]), "authors")

    def test_date_without_timezone(self):
        self.check(lambda r: r["metadata"].update(submission_date="2023-01-01T00:00:00"), "timezone")

    def test_garbage_date(self):
        self.check(lambda r: r["metadata"].update(revised_dates=["not a date"]), "ISO")

    def test_garbage_version_date(self):
        self.check(lambda r: r["versions"][0].update(updated_date="yesterday"), "ISO")

    def test_revised_dates_count_mismatch(self):
        self.check(lambda r: r["metadata"].update(revised_dates=[]), "revised_dates")

    def test_version_gap(self):
        self.check(lambda r: r["versions"].pop(0), "1..latest_version")

    def test_other_paper_in_metadata(self):
        self.check(lambda r: r["metadata"].update(arxiv_id="2301.99999"), "arxiv_id")

    def test_paper_title_differs_from_latest_version(self):
        self.check(lambda r: r["metadata"].update(title="Something Else"), "latest version")


class AdapterTests(TestCase):
    def test_writes_artifact_and_stage_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = metadata.run(record_for(), tmp)
            self.assertIsInstance(result, StageResult)
            self.assertEqual((result.stage, result.version, result.status), ("metadata", None, "success"))
            self.assertEqual(result.artifacts, {"metadata": "metadata/2301.00001.json"})
            self.assertGreater(result.size_bytes, 0)
            stored = json.loads((Path(tmp) / "metadata/2301.00001.json").read_text())
            self.assertEqual(stored["title"], "Final Title")
            self.assertEqual(stored["versions"][0]["title"], "First Title")

    def test_legacy_id_uses_underscore_key(self):
        mapping = {"hep-th/9901001": atom("hep-th/9901001", 1)}
        with tempfile.TemporaryDirectory() as tmp:
            result = metadata.run(record_for("hep-th/9901001", mapping), tmp)
            self.assertEqual(result.artifacts, {"metadata": "metadata/hep-th_9901001.json"})

    def test_stub_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(metadata.run(record_for(), tmp).status, "success")   # no NotImplementedError
