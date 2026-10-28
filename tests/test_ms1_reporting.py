"""Hand-calculated statistics and real child-process benchmark tests (no network)."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from arxiv_pipeline.common.utils import atomic_json


class StatisticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        records = [dict(arxiv_id='2301.00001', input_id='2301.00001', status='success',
                        versions=[{'version': 1}, {'version': 2}]),
                   dict(arxiv_id='2301.00002', input_id='2301.00002', status='success',
                        versions=[{'version': 1}])]
        # Same publication expressed twice must not inflate denominators.
        records.append(dict(records[0], input_id='arXiv:2301.00001'))
        atomic_json(self.root / 'manifests/discovery.json', records)
        self.stage('download', '2301.00001', 1, 'success')
        self.stage('download', '2301.00001', 2, 'success')
        self.stage('extract', '2301.00001', 1, 'success')
        self.stage('extract', '2301.00001', 2, 'success')
        self.stage('download', '2301.00002', 1, 'no_source')
        self.stage('extract', '2301.00002', 1, 'skipped')
        for paper in ('2301.00001', '2301.00002'):
            self.stage('metadata', paper, None, 'success')
        self.stage('references', '2301.00001', None, 'success')
        self.stage('references', '2301.00002', None, 'no_data')
        for n in (1, 2):
            atomic_json(self.root / f'raw/2301.00001/v{n}/download_metadata.json', {'raw_size_bytes': 100})
            atomic_json(self.root / f'corpus/2301.00001/v{n}/extract_metadata.json',
                        {'raw_size_bytes': 1000, 'corpus_size_bytes': 200})
        refs = [dict(ref_arxiv_id='2001.00001', ref_title='A', ref_authors=['X'],
                     ref_submission_date='2020', ref_s2_id='s2a', missing_fields=[]),
                dict(ref_arxiv_id=None, ref_title='B', ref_authors=['Y'],
                     ref_submission_date=None, ref_s2_id='s2b', missing_fields=['ref_arxiv_id', 'ref_submission_date'])]
        atomic_json(self.root / 'references/2301.00001.json', refs)
        atomic_json(self.root / 'references/2301.00002.json', [])

    def stage(self, stage, paper, version, status):
        atomic_json(self.root / f'state/stages/{stage}/{paper}/{version or "paper"}.json',
                    dict(stage=stage, paper_id=paper, version=version, status=status, error=None))

    def summarize(self):
        from arxiv_pipeline.reporting.milestone1.statistics import summarize
        return summarize(self.root)

    def test_unique_papers_version_rates_and_paired_uncompressed_sizes(self):
        report = self.summarize()
        self.assertEqual(report['counts']['papers'], 2)
        self.assertEqual(report['counts']['expected_versions'], 3)
        self.assertAlmostEqual(report['rates']['download_success']['value'], 2 / 3)
        self.assertEqual(report['rates']['source_complete_papers']['value'], 0.5)
        self.assertEqual(report['sizes']['mean_uncompressed_before_per_paper_bytes'], 2000)
        self.assertEqual(report['sizes']['mean_filtered_per_paper_bytes'], 400)
        self.assertEqual(report['sizes']['mean_download_package_per_paper_bytes'], 200)
        self.assertEqual(report['references']['mean_per_retrieved_paper'], 1)
        self.assertEqual(report['references']['complete_metadata_rate']['value'], 0.5)

    def test_missing_checkpoint_stays_in_denominator(self):
        (self.root / 'state/stages/download/2301.00001/2.json').unlink()
        report = self.summarize()
        self.assertAlmostEqual(report['rates']['download_success']['value'], 1 / 3)
        self.assertAlmostEqual(report['rates']['version_record_coverage']['value'], 2 / 3)

    def test_empty_data_uses_null_not_fake_zero_rates(self):
        atomic_json(self.root / 'manifests/discovery.json', [])
        report = self.summarize()
        self.assertIsNone(report['rates']['download_success']['value'])
        self.assertIsNone(report['references']['mean_per_retrieved_paper'])

    def test_corrupt_sidecar_and_checkpoint_identity_are_diagnostics(self):
        (self.root / 'raw/2301.00001/v1/download_metadata.json').write_text('{bad')
        self.stage('download', '2301.00001', 2, 'success')
        path = self.root / 'state/stages/download/2301.00001/2.json'
        doc = json.loads(path.read_text())
        doc['paper_id'] = '2301.00099'
        atomic_json(path, doc)
        report = self.summarize()
        self.assertTrue(report['diagnostics'])
        self.assertAlmostEqual(report['rates']['download_success']['value'], 1 / 3)

    def test_success_with_empty_references_is_not_nonempty_success(self):
        atomic_json(self.root / 'references/2301.00001.json', [])
        report = self.summarize()
        self.assertEqual(report['rates']['reference_nonempty_success']['value'], 0)


class ExportTests(unittest.TestCase):
    def test_export_keeps_evidence_and_removes_machine_paths(self):
        from arxiv_pipeline.reporting.milestone1.export import export_run
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = root / 'run'
            run.mkdir()
            atomic_json(run / 'benchmark.json', dict(label='synthetic_demo', command=['secret-local-path'],
                        output_dir='private-local-path', environment={'executable': 'local-python', 'python': '3.12'}))
            atomic_json(run / 'statistics.json', {'label': 'synthetic_demo'})
            (run / 'samples.jsonl').write_text('{"rss_bytes":1}\n')
            (run / 'timings.jsonl').write_text('{"kind":"paper"}\n')
            target = export_run(run, root / 'book', 'demo')
            exported = json.loads((target / 'benchmark.json').read_text())
            self.assertNotIn('command', exported)
            self.assertNotIn('output_dir', exported)
            self.assertNotIn('executable', exported['environment'])
            self.assertTrue((target / 'provenance.json').exists())
            self.assertTrue((target / 'samples.jsonl').exists())
            with self.assertRaises(ValueError):
                export_run(run, root / 'book', '../escape')
            with self.assertRaises(ValueError):
                export_run(run, root / 'book', 'demo')


class BenchmarkTests(unittest.TestCase):
    def test_measure_real_process_failure_disk_and_samples(self):
        from arxiv_pipeline.reporting.milestone1.benchmark import measure
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data, result = root / 'data', root / 'results'
            data.mkdir()
            script = ('import pathlib,time,sys; '
                      f'pathlib.Path({str(data / "payload")!r}).write_bytes(b"x"*4096); '
                      'time.sleep(0.25); sys.exit(3)')
            report = measure([sys.executable, '-c', script], data, result, interval=0.02, label='synthetic_demo')
            self.assertEqual(report['exit_code'], 3)
            self.assertGreaterEqual(report['wall_seconds'], 0.2)
            self.assertEqual(report['disk']['final_bytes'], 4096)
            self.assertGreater(report['ram']['peak_rss_bytes'], 0)
            self.assertGreater(report['ram']['time_weighted_mean_rss_bytes'], 0)
            self.assertTrue((result / 'samples.jsonl').exists())
            self.assertTrue((result / 'benchmark.json').exists())

    def test_sampler_counts_live_descendants(self):
        from arxiv_pipeline.reporting.milestone1.benchmark import measure
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            child = 'import time; x=bytearray(8000000); time.sleep(0.5)'
            script = f'import subprocess,sys; p=subprocess.Popen([sys.executable,"-c",{child!r}]); p.wait()'
            report = measure([sys.executable, '-c', script], root / 'data', root / 'results',
                             interval=0.02, label='synthetic_demo')
            self.assertEqual(report['exit_code'], 0)
            self.assertGreaterEqual(report['max_observed_processes'], 2)

    def test_sampler_waits_for_observed_child_after_parent_exits(self):
        from arxiv_pipeline.reporting.milestone1.benchmark import measure
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            child = 'import time; x=bytearray(4000000); time.sleep(0.6)'
            script = f'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",{child!r}]); time.sleep(0.2)'
            report = measure([sys.executable, '-c', script], root / 'data', root / 'results', interval=0.02)
            self.assertGreaterEqual(report['wall_seconds'], 0.5)

    def test_refuses_existing_results_and_results_inside_measured_directory(self):
        from arxiv_pipeline.reporting.milestone1.benchmark import measure
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for result in (root, root / 'data/results'):
                with self.subTest(result=result), self.assertRaises(ValueError):
                    measure([sys.executable, '-c', 'pass'], root / 'data', result)

    def test_missing_executable_persists_failure(self):
        from arxiv_pipeline.reporting.milestone1.benchmark import measure
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = measure(['a-nonexistent-tv5-command'], root / 'data', root / 'results')
            self.assertNotEqual(report['exit_code'], 0)
            self.assertIn('launch_error', report)


class TimingTests(unittest.TestCase):
    def test_discovery_and_paper_timings_mark_resume(self):
        from helpers import fixture_client
        from arxiv_pipeline.pipeline.crawl import run
        with tempfile.TemporaryDirectory() as temporary:
            client, _ = fixture_client()
            events = []
            run(['2301.00001'], Path(temporary), client=client, observer=events.append)
            self.assertEqual([e['kind'] for e in events], ['discovery', 'paper'])
            self.assertFalse(events[0]['cached'])
            self.assertGreaterEqual(events[1]['elapsed_seconds'], events[0]['elapsed_seconds'])
            events.clear()
            run(['2301.00001'], Path(temporary), client=client, resume=True, observer=events.append)
            self.assertTrue(events[0]['cached'])
