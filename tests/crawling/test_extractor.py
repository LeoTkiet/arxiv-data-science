import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import gzip
import io
import shutil
import tarfile
import tempfile
from unittest import TestCase

from arxiv_pipeline.crawling.extractor import (
    is_safe_path,
    extract_source,
    run,
    ExtractionSecurityError,
    ExtractionDataError,
)
from arxiv_pipeline.common.schemas import StageResult


class ExtractorTests(TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.output_dir = Path(self.temp_dir.name)

    def _create_tar_gz(self, files: dict[str, bytes]) -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name, content in files.items():
                info = tarfile.TarInfo(name=name)
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
        return buf.getvalue()

    def test_is_safe_path(self):
        base = self.output_dir / "target"
        base.mkdir()
        self.assertTrue(is_safe_path(base, base / "main.tex"))
        self.assertTrue(is_safe_path(base, base / "sub/chapter.tex"))
        self.assertFalse(is_safe_path(base, base / "../escape.txt"))
        self.assertFalse(is_safe_path(base, Path(r"C:\Windows\System32")))

    def test_safe_tar_gz_extract_and_filter(self):
        files = {
            "main.tex": b"\\documentclass{article}\n\\begin{document}Hello\\end{document}",
            "references.bib": b"@article{author2024, title={Sample Paper}}",
            "figure.png": b"\x89PNG\r\n\x1a\n\x00mock_image_binary_data",
            "sub/chapter1.tex": b"\\section{Chapter 1}\nSome content here.",
            "sub/diagram.pdf": b"%PDF-1.4 mock binary pdf diagram",
            "paper.aux": b"\\relax",
        }
        tar_bytes = self._create_tar_gz(files)
        source_path = self.output_dir / "raw/2301.00001/v1/source"
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(tar_bytes)

        corpus_dir = self.output_dir / "corpus/2301.00001/v1"
        status, kept_files, meta = extract_source(source_path, corpus_dir)

        self.assertEqual(status, "success")
        self.assertTrue((corpus_dir / "main.tex").exists())
        self.assertTrue((corpus_dir / "references.bib").exists())
        self.assertTrue((corpus_dir / "sub/chapter1.tex").exists())

        # Non-tex/bib files must be filtered out
        self.assertFalse((corpus_dir / "figure.png").exists())
        self.assertFalse((corpus_dir / "sub/diagram.pdf").exists())
        self.assertFalse((corpus_dir / "paper.aux").exists())

        # Metadata checks
        self.assertEqual(meta["file_count"], 3)
        self.assertIn("main.tex", meta["extracted_files"])
        self.assertIn("references.bib", meta["extracted_files"])

    def test_path_traversal_attack_blocked(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo(name="../escape.txt")
            content = b"malicious content"
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))

        source_path = self.output_dir / "raw/2301.00001/v1/source"
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(buf.getvalue())

        corpus_dir = self.output_dir / "corpus/2301.00001/v1"
        with self.assertRaises(ExtractionSecurityError):
            extract_source(source_path, corpus_dir)

        # Ensure nothing escaped
        self.assertFalse((self.output_dir / "corpus/2301.00001/escape.txt").exists())

    def test_safe_gz_single_tex_extract(self):
        tex_content = b"\\documentclass{article}\n\\begin{document}Single TeX\\end{document}"
        source_path = self.output_dir / "raw/hep-th_9901001/v1/source"
        source_path.parent.mkdir(parents=True, exist_ok=True)

        with gzip.open(source_path, "wb") as gz:
            gz.write(tex_content)

        corpus_dir = self.output_dir / "corpus/hep-th_9901001/v1"
        status, kept_files, meta = extract_source(source_path, corpus_dir)

        self.assertEqual(status, "success")
        self.assertTrue((corpus_dir / "main.tex").exists())
        self.assertEqual((corpus_dir / "main.tex").read_bytes(), tex_content)

    def test_plain_tex_extract(self):
        tex_content = b"\\documentclass{article}\n\\begin{document}Plain TeX\\end{document}"
        source_path = self.output_dir / "raw/2301.00001/v1/source"
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(tex_content)

        corpus_dir = self.output_dir / "corpus/2301.00001/v1"
        status, kept_files, meta = extract_source(source_path, corpus_dir)

        self.assertEqual(status, "success")
        self.assertTrue((corpus_dir / "main.tex").exists())
        self.assertEqual((corpus_dir / "main.tex").read_bytes(), tex_content)

    def test_pdf_only_source_returns_no_source(self):
        pdf_content = b"%PDF-1.4\nmock pdf binary data"
        source_path = self.output_dir / "raw/2301.00001/v1/source"
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(pdf_content)

        corpus_dir = self.output_dir / "corpus/2301.00001/v1"
        status, kept_files, meta = extract_source(source_path, corpus_dir)

        self.assertEqual(status, "no_source")
        self.assertFalse(corpus_dir.exists())

    def test_archive_without_tex_returns_no_source(self):
        files = {
            "diagram.png": b"\x89PNG\r\n\x1a\n",
            "readme.txt": b"Just a readme",
        }
        tar_bytes = self._create_tar_gz(files)
        source_path = self.output_dir / "raw/2301.00001/v1/source"
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(tar_bytes)

        corpus_dir = self.output_dir / "corpus/2301.00001/v1"
        status, kept_files, meta = extract_source(source_path, corpus_dir)

        self.assertEqual(status, "no_source")
        self.assertFalse(corpus_dir.exists())

    def test_corrupt_archive_returns_data_error(self):
        corrupt_bytes = b"\x1f\x8b\x08corrupted_garbage_bytes_here"
        source_path = self.output_dir / "raw/2301.00001/v1/source"
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(corrupt_bytes)

        corpus_dir = self.output_dir / "corpus/2301.00001/v1"
        with self.assertRaises(ExtractionDataError):
            extract_source(source_path, corpus_dir)

    def test_adapter_run_integration(self):
        files = {
            "paper.tex": b"\\documentclass{article}\n\\begin{document}Integrated\\end{document}",
            "ref.bib": b"@article{demo, title={Integrated Test}}",
        }
        tar_bytes = self._create_tar_gz(files)
        source_path = self.output_dir / "raw/2301.00001/v1/source"
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(tar_bytes)

        record = {"arxiv_id": "2301.00001"}
        version = {
            "version": 1,
            "versioned_id": "2301.00001v1",
            "source_path": "raw/2301.00001/v1/source",
            "corpus_path": "corpus/2301.00001/v1",
        }
        upstream = {
            "status": "success",
            "artifacts": {"source": "raw/2301.00001/v1/source"},
        }

        res = run(record, self.output_dir, version=version, upstream=upstream)

        self.assertIsInstance(res, StageResult)
        self.assertEqual(res.status, "success")
        self.assertEqual(res.stage, "extract")
        self.assertEqual(res.paper_id, "2301.00001")
        self.assertEqual(res.version, 1)
        self.assertEqual(res.artifacts, {"corpus": "corpus/2301.00001/v1"})
        self.assertTrue((self.output_dir / "corpus/2301.00001/v1/extract_metadata.json").exists())

    def test_multi_version_isolation(self):
        record = {"arxiv_id": "2301.00001"}
        for v in (1, 2):
            files = {f"main_v{v}.tex": f"\\title{{Version {v}}}".encode("utf-8")}
            tar_bytes = self._create_tar_gz(files)
            source_path = self.output_dir / f"raw/2301.00001/v{v}/source"
            source_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.write_bytes(tar_bytes)

            version = {
                "version": v,
                "versioned_id": f"2301.00001v{v}",
                "source_path": f"raw/2301.00001/v{v}/source",
                "corpus_path": f"corpus/2301.00001/v{v}",
            }
            upstream = {"status": "success", "artifacts": {"source": f"raw/2301.00001/v{v}/source"}}
            res = run(record, self.output_dir, version=version, upstream=upstream)
            self.assertEqual(res.status, "success")

        # Confirm both versions exist and did not overwrite each other
        v1_file = self.output_dir / "corpus/2301.00001/v1/main_v1.tex"
        v2_file = self.output_dir / "corpus/2301.00001/v2/main_v2.tex"
        self.assertTrue(v1_file.exists())
        self.assertTrue(v2_file.exists())

    def test_pipeline_crawl_integration(self):
        from unittest.mock import patch
        from arxiv_pipeline.pipeline.crawl import run as crawl_run
        from arxiv_pipeline.common.utils import utc_now
        from helpers import fixture_client

        client, _ = fixture_client()

        def fake_downloader(record, output, *, version=None, upstream=None):
            src_path = output / version["source_path"]
            src_path.parent.mkdir(parents=True, exist_ok=True)
            files = {f"paper_v{version['version']}.tex": b"\\documentclass{article}\n\\begin{document}Real Pipeline\\end{document}"}
            src_path.write_bytes(self._create_tar_gz(files))
            return StageResult("download", record["arxiv_id"], version["version"], "success", utc_now(),
                               artifacts={"source": version["source_path"]})

        def fake_references(record, output, *, version=None, upstream=None):
            ref_file = output / f"references/{record['arxiv_id']}.json"
            ref_file.parent.mkdir(parents=True, exist_ok=True)
            ref_file.write_text("[]", encoding="utf-8")
            return StageResult("references", record["arxiv_id"], None, "success", utc_now(),
                               artifacts={"references": f"references/{record['arxiv_id']}.json"})

        def fake_validator(record, output, *, version=None, upstream=None):
            val_file = output / f"validation/{record['arxiv_id']}.json"
            val_file.parent.mkdir(parents=True, exist_ok=True)
            val_file.write_text("{}", encoding="utf-8")
            return StageResult("validate", record["arxiv_id"], None, "success", utc_now(),
                               artifacts={"validation": f"validation/{record['arxiv_id']}.json"})

        with patch("arxiv_pipeline.crawling.downloader.run", side_effect=fake_downloader), \
             patch("arxiv_pipeline.crawling.references.run", side_effect=fake_references), \
             patch("arxiv_pipeline.export.validation.run", side_effect=fake_validator):
            summary = crawl_run(["2301.00001"], self.output_dir, client=client, full=True)

        self.assertEqual(summary["stage_statuses"]["success"], 7)  # 2 downloads, 2 extracts, 1 metadata, 1 references, 1 validation
        self.assertTrue((self.output_dir / "corpus/2301.00001/v1/paper_v1.tex").exists())
        self.assertTrue((self.output_dir / "corpus/2301.00001/v2/paper_v2.tex").exists())
        self.assertTrue((self.output_dir / "manifests/stages.json").exists())

