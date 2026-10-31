import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from contextlib import ExitStack, redirect_stdout, redirect_stderr
from io import StringIO
import json
import tempfile
from unittest import TestCase
from unittest.mock import patch
from urllib.error import URLError

from arxiv_pipeline.common.schemas import StageResult
from arxiv_pipeline.common.utils import utc_now
from arxiv_pipeline.main import main, expand_range
from arxiv_pipeline.pipeline.crawl import run, MODULES
from helpers import fixture_client, feed


class CLITests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)

    def call(self, args, client):
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            return main(["--output", str(self.output), *args], client=client)

    def test_cli_input_file_range_duplicate_and_summary(self):
        source = self.output / "ids.txt"
        source.write_text("# inputs\n2301.00001\n\n", encoding="utf-8-sig")
        client, opener = fixture_client()
        code = self.call(["--input", str(source), "--ids", "2301.00001", "--range", "2301.00001:2301.00002"], client)
        self.assertEqual(code, 1)  # second ID does not exist
        result = json.loads((self.output / "summary.json").read_text())
        self.assertEqual(result["inputs"], 2)
        self.assertEqual(result["versions"], 2)
        self.assertEqual(result["discovery_statuses"], {"success": 1, "not_found": 1})
        self.assertTrue((self.output / "logs/pipeline.log").exists())

    def test_resume_and_force(self):
        client, opener = fixture_client()
        self.assertEqual(self.call(["--ids", "2301.00001"], client), 0)
        self.assertEqual(self.call(["--ids", "2301.00001", "--resume"], client), 0)
        self.assertEqual(len(opener.calls), 2)
        self.assertEqual(self.call(["--ids", "2301.00001", "--force"], client), 0)
        self.assertEqual(len(opener.calls), 4)

    def test_resume_retries_failed_input(self):
        client, opener = fixture_client({"2301.00001": b"broken"})
        self.assertEqual(self.call(["--ids", "2301.00001"], client), 1)
        opener.mapping = {"2301.00001": feed(version=1)}
        self.assertEqual(self.call(["--ids", "2301.00001", "--resume"], client), 0)
        self.assertEqual(len(opener.calls), 2)

    def test_corrupt_checkpoint_requeried(self):
        client, opener = fixture_client()
        self.call(["--ids", "2301.00001"], client)
        checkpoint = next((self.output / "state/discovery").glob("*.json"))
        checkpoint.write_text("{}", encoding="utf-8")
        self.call(["--ids", "2301.00001", "--resume"], client)
        self.assertEqual(len(opener.calls), 4)

    def test_interrupted_run_keeps_completed_input(self):
        client, opener = fixture_client({"2301.00001": feed(version=1), "2301.00002": KeyboardInterrupt()})
        code = self.call(["--ids", "2301.00001", "2301.00002"], client)
        self.assertEqual(code, 130)
        self.assertEqual(len(list((self.output / "state/discovery").glob("*.json"))), 1)
        opener.mapping["2301.00002"] = feed("2301.00002", 1)
        self.assertEqual(self.call(["--ids", "2301.00001", "2301.00002", "--resume"], client), 0)
        self.assertEqual(opener.calls.count("2301.00001"), 1)

    def test_pending_modules_make_full_pipeline_incomplete(self):
        client, _ = fixture_client()
        from arxiv_pipeline.crawling.s2.s2_source import Outcome
        from arxiv_pipeline.crawling.s2.store import store_outcome

        def references(record, output, **kwargs):
            return store_outcome(Outcome(record['arxiv_id'], 'ok_empty'), output)

        with patch("arxiv_pipeline.crawling.downloader.run", side_effect=NotImplementedError("pending")), \
                patch("arxiv_pipeline.crawling.references.run", side_effect=references):
            self.assertEqual(self.call(["--ids", "2301.00001", "--stage", "crawl"], client), 1)
        stages = json.loads((self.output / "manifests/stages.json").read_text())
        self.assertEqual([s["status"] for s in stages],
                         ["not_implemented", "skipped"] * 2 + ["success", "no_data", "data_error"])

    def test_invalid_config(self):
        client, _ = fixture_client()
        for args in ([], ["--ids", "2301.00001", "--request-delay", "0"],
                     ["--ids", "2301.00001", "--resume", "--force"],
                     ["--range", "2301.00001:2302.00001"]):
            with self.subTest(args=args), self.assertRaises(SystemExit) as caught:
                self.call(args, client)
            self.assertEqual(caught.exception.code, 2)

    def test_range_expansion(self):
        self.assertEqual(expand_range("0704.0001:0704.0003"), ["0704.0001", "0704.0002", "0704.0003"])


class IntegrationTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)
        self.calls = []

    def adapter(self, stage, *, status="success", fail=False, escape=False):
        def execute(record, output, *, version=None, upstream=None):
            self.calls.append(stage)
            if fail:
                raise RuntimeError("synthetic adapter failure")
            number = version["version"] if version else None
            artifacts = {}
            if status == "success":
                if stage == "download":
                    relative, key = version["source_path"], "source"
                elif stage == "extract":
                    self.assertTrue((output / upstream["artifacts"]["source"]).exists())
                    relative, key = version["corpus_path"] + "/main.tex", "corpus"
                else:
                    relative, key = f"{stage}/{record['arxiv_id']}.json", stage
                path = output / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("test artifact", encoding="utf-8")
                artifacts[key] = "../escape" if escape else relative
            return StageResult(stage, record["arxiv_id"], number, status, utc_now(), artifacts=artifacts)
        return execute

    def patched(self, **overrides):
        stack = ExitStack()
        for stage, module in MODULES.items():
            stack.enter_context(patch(module + ".run", overrides.get(stage, self.adapter(stage))))
        return stack

    def test_integrated_adapters_and_resume(self):
        client, _ = fixture_client()
        with self.patched():
            self.assertTrue(run(["2301.00001"], self.output, client=client, full=True)["complete"])
            self.assertEqual(self.calls, ["download", "extract"] * 2 + ["metadata", "references", "validate"])
            self.calls.clear()
            self.assertTrue(run(["2301.00001"], self.output, client=client, full=True, resume=True)["complete"])
            self.assertEqual(self.calls, [])
            (self.output / "corpus/2301.00001/v1/main.tex").unlink()
            run(["2301.00001"], self.output, client=client, full=True, resume=True)
            self.assertEqual(self.calls, ["extract"])

    def test_pdf_only_preserved_and_extraction_skipped(self):
        client, _ = fixture_client()
        with self.patched(download=self.adapter("download", status="no_source")):
            summary = run(["2301.00001"], self.output, client=client, full=True)
        self.assertTrue(summary["complete"])
        self.assertEqual(summary["stage_statuses"]["no_source"], 2)
        self.assertNotIn("extract", self.calls)

    def test_failed_adapter_does_not_stop_other_stages(self):
        client, _ = fixture_client()
        with self.patched(download=self.adapter("download", fail=True)):
            summary = run(["2301.00001"], self.output, client=client, full=True)
        self.assertFalse(summary["complete"])
        self.assertIn("metadata", self.calls)
        self.assertNotIn("extract", self.calls)

    def test_escaping_artifact_rejected(self):
        client, _ = fixture_client()
        with self.patched(download=self.adapter("download", escape=True)):
            summary = run(["2301.00001"], self.output, client=client, full=True)
        self.assertFalse(summary["complete"])
        self.assertEqual(summary["stage_statuses"]["error"], 2)
