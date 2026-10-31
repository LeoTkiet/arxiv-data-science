"""Offline validation of real on-disk artifacts and pipeline checkpoints."""
import json
from pathlib import Path
import sys
import tempfile
from unittest import TestCase
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from helpers import fixture_client
from arxiv_pipeline.crawling.discovery import discover
from arxiv_pipeline.crawling.metadata import run as metadata_run
from arxiv_pipeline.crawling.extractor import run as extract_run
from arxiv_pipeline.common.schemas import StageResult
from arxiv_pipeline.common.utils import atomic_json, utc_now
from arxiv_pipeline.export.validation import run


class ValidationTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)
        client, _ = fixture_client()
        self.record = discover('2301.00001', client).to_dict()
        for v in self.record['versions']:
            source = self.output / v['source_path']
            source.parent.mkdir(parents=True)
            source.write_text('\\documentclass{article}\n% fixture\n', encoding='utf-8')
            download = StageResult('download', '2301.00001', v['version'], 'success',
                                   utc_now(), artifacts={'source': v['source_path']})
            self.checkpoint(download)
            self.checkpoint(extract_run(self.record, self.output, version=v, upstream=download.to_dict()))
        self.checkpoint(metadata_run(self.record, self.output))
        self.ref = {'arxiv_id': '2301.00001', 'ref_arxiv_id': None,
                    'ref_title': 'Referenced paper', 'ref_authors': ['Author'],
                    'ref_submission_date': '2020', 'ref_s2_id': 's2-example',
                    'missing_fields': ['ref_arxiv_id']}
        atomic_json(self.output / 'references/2301.00001.json', [self.ref])
        self.checkpoint(StageResult('references', '2301.00001', None, 'success', utc_now(),
                                   artifacts={'references': 'references/2301.00001.json'}))

    def checkpoint(self, result):
        atomic_json(self.output / f'state/stages/{result.stage}/2301.00001/{result.version or "paper"}.json',
                    result.to_dict())

    def validate(self):
        result = run(self.record, self.output)
        report = json.loads((self.output / result.artifacts['validation']).read_text(encoding='utf-8'))
        return result, report

    def test_valid_multiversion_corpus_accepts_sidecars_and_missing_reference_id(self):
        before = (self.output / 'corpus/2301.00001/v1/main.tex').read_bytes()
        result, report = self.validate()
        self.assertEqual(result.status, 'success')
        self.assertTrue(report['valid'])
        self.assertEqual(report['counts']['versions'], 2)
        self.assertEqual(report['counts']['references'], 1)
        self.assertEqual(report['counts']['source_versions'], 2)
        self.assertEqual(before, (self.output / 'corpus/2301.00001/v1/main.tex').read_bytes())
        self.assertGreater(result.size_bytes, 0)

    def test_missing_source_and_missing_checkpoint_are_failures(self):
        (self.output / 'raw/2301.00001/v1/source').unlink()
        (self.output / 'state/stages/extract/2301.00001/2.json').unlink()
        result, report = self.validate()
        self.assertEqual(result.status, 'data_error')
        self.assertFalse(report['valid'])
        self.assertGreaterEqual(len(report['errors']), 2)

    def test_missing_version_in_discovery_is_rejected(self):
        self.record['versions'].pop()
        self.assertEqual(self.validate()[0].status, 'data_error')

    def test_metadata_wrong_schema_or_malformed_json_is_rejected(self):
        path = self.output / 'metadata/2301.00001.json'
        for content in ('{"authors": "wrong"}', '{broken', '[]'):
            with self.subTest(content=content):
                path.write_text(content, encoding='utf-8')
                self.assertEqual(self.validate()[0].status, 'data_error')

    def test_reference_wrong_owner_type_missing_marker_and_duplicates(self):
        cases = [dict(self.ref, arxiv_id='2301.00002'),
                 dict(self.ref, ref_authors='Author'),
                 dict(self.ref, missing_fields=[])]
        for row in cases:
            with self.subTest(row=row):
                atomic_json(self.output / 'references/2301.00001.json', [row])
                self.assertEqual(self.validate()[0].status, 'data_error')
        atomic_json(self.output / 'references/2301.00001.json', [self.ref, self.ref])
        self.assertEqual(self.validate()[0].status, 'data_error')

    def test_pdf_only_is_separate_valid_outcome_not_source_success(self):
        self.checkpoint(StageResult('download', '2301.00001', 1, 'no_source', utc_now()))
        self.checkpoint(StageResult('extract', '2301.00001', 1, 'skipped', utc_now()))
        result, report = self.validate()
        self.assertEqual(result.status, 'success')
        self.assertEqual(report['counts']['source_versions'], 1)
        self.assertEqual(report['counts']['no_source_versions'], 1)

    def test_upstream_failure_is_not_hidden_by_existing_files(self):
        self.checkpoint(StageResult('download', '2301.00001', 1, 'api_error', utc_now()))
        self.assertEqual(self.validate()[0].status, 'data_error')

    def test_escaping_path_and_wrong_checkpoint_identity_rejected(self):
        self.checkpoint(StageResult('download', '2301.00001', 1, 'success', utc_now(),
                                   artifacts={'source': '../outside'}))
        self.checkpoint(StageResult('metadata', '2301.00002', None, 'success', utc_now()))
        self.assertEqual(self.validate()[0].status, 'data_error')

    def test_binary_empty_and_unexpected_corpus_files_rejected(self):
        path = self.output / 'corpus/2301.00001/v1/main.tex'
        for content in (b'', b'\x00binary', b'%PDF-1.7'):
            with self.subTest(content=content):
                path.write_bytes(content)
                self.assertEqual(self.validate()[0].status, 'data_error')
        path.write_text('valid source', encoding='utf-8')
        path.with_suffix('.png').write_bytes(b'image')
        self.assertEqual(self.validate()[0].status, 'data_error')

    def test_duplicate_source_content_is_reported_without_deleting_files(self):
        source = self.output / 'corpus/2301.00001/v1/main.tex'
        duplicate = source.with_name('copy.tex')
        duplicate.write_bytes(source.read_bytes())
        result, report = self.validate()
        self.assertEqual(result.status, 'success')
        self.assertTrue(report['duplicate_files'])
        self.assertTrue(duplicate.exists())

    def test_empty_reference_outcomes_are_valid_but_success_empty_is_not(self):
        atomic_json(self.output / 'references/2301.00001.json', [])
        for status in ('no_data', 'not_indexed', 'success'):
            with self.subTest(status=status):
                self.checkpoint(StageResult('references', '2301.00001', None, status, utc_now(),
                                           artifacts={'references': 'references/2301.00001.json'}))
                result, report = self.validate()
                self.assertEqual(result.status, 'data_error' if status == 'success' else 'success')
                self.assertEqual(report['counts']['references'], 0)

    def test_pipeline_runs_real_extractor_metadata_and_validator_before_manifests(self):
        from arxiv_pipeline.pipeline.crawl import run as crawl
        from arxiv_pipeline.crawling.s2.s2_source import Outcome
        from arxiv_pipeline.crawling.s2.store import store_outcome

        def download(record, output, *, version, **kwargs):
            path = output / version['source_path']
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('\\documentclass{article}\n% source', encoding='utf-8')
            return StageResult('download', record['arxiv_id'], version['version'], 'success', utc_now(),
                               artifacts={'source': version['source_path']})

        def references(record, output, **kwargs):
            return store_outcome(Outcome(record['arxiv_id'], 'ok_empty'), output)

        fresh = self.output / 'fresh-run'
        client, _ = fixture_client()
        with patch('arxiv_pipeline.crawling.downloader.run', side_effect=download), \
                patch('arxiv_pipeline.crawling.references.run', side_effect=references):
            summary = crawl(['2301.00001'], fresh, client=client, full=True)
        self.assertTrue(summary['complete'])
        report = json.loads((fresh / 'validation/2301.00001.json').read_text())
        self.assertTrue(report['valid'])
        self.assertEqual(report['counts']['source_versions'], 2)

    def test_bad_checksum_is_rejected(self):
        relative = 'raw/2301.00001/v1/download_metadata.json'
        atomic_json(self.output / relative, {'sha256': '0' * 64})
        self.checkpoint(StageResult('download', '2301.00001', 1, 'success', utc_now(),
                                   artifacts={'source': 'raw/2301.00001/v1/source',
                                              'download_metadata': relative}))
        self.assertEqual(self.validate()[0].status, 'data_error')

    def test_extra_version_directory_is_rejected(self):
        (self.output / 'corpus/2301.00001/v3').mkdir()
        self.assertEqual(self.validate()[0].status, 'data_error')
