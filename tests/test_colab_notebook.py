"""Offline regression checks executing the actual embedded notebook cells."""
import ast
import contextlib
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

from arxiv_pipeline.reporting.milestone1.audit import audit

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(os.environ.get('TV5_SOURCE', ROOT)).resolve()
NOTEBOOK = json.loads((ROOT / 'colab/MS1_Benchmark_CPU.ipynb').read_text(encoding='utf-8'))


def cell(index, overrides=None):
    tree = ast.parse(''.join(NOTEBOOK['cells'][index]['source']))
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            key = node.targets[0].id
            if key in (overrides or {}):
                node.value = ast.parse(overrides[key], mode='eval').body
    return compile(ast.fix_missing_locations(tree), f'notebook-cell-{index}', 'exec')


class NotebookTests(unittest.TestCase):
    def setUp(self):
        temp_root = SOURCE / 'artifacts/colab-notebook-tests'
        temp_root.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=temp_root)
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        assert self.base.is_relative_to(temp_root.resolve())
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)

    def configure(self, role='TV2'):
        import shutil
        ns = dict(Path=Path, SOURCE=SOURCE, SOURCE_READY=True, os=os, sys=sys,
                  shutil=shutil, datetime=datetime, json=json)
        exec(cell(10, {'MEMBER_ROLE': repr(role), 'base_root': f'Path({str(self.base)!r})'}), ns)
        ns['RUN_BENCHMARK'] = True
        return ns

    def snapshot(self, ns):
        results = self.base / 'run'
        results.mkdir()
        ns['RESULTS'] = results
        output = ns['OUTPUT']
        payloads = {'manifests/discovery.json': [], 'manifests/stages.json': [], 'summary.json': {}}
        hashes = {}
        for name, data in payloads.items():
            path = output / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data), encoding='utf-8')
            hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        receipt = {'run_id': 'current', 'sha256': hashes}
        (output / 'manifests/run.json').write_text(json.dumps(receipt), encoding='utf-8')
        report = {'run_id': 'current', 'snapshot': receipt, 'statistics_status': 'current_run',
                  'label': 'live_unverified', 'environment': {}}
        (results / 'benchmark.json').write_text(json.dumps(report), encoding='utf-8')
        (results / 'statistics.json').write_text('original statistics', encoding='utf-8')
        return results

    def executor(self, ns, mutate=False, fail_audit=False):
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            module = command[command.index('-m') + 1]
            def flag(name):
                return Path(command[command.index(name) + 1])
            if module.endswith('.statistics'):
                self.assertTrue(kwargs['check'])
                rates = {'missing': {'value': None, 'numerator': 0, 'denominator': 0},
                         'zero': {'value': 0, 'numerator': 0, 'denominator': 1}}
                flag('--save').write_text(json.dumps({'rates': rates}), encoding='utf-8')
                if mutate:
                    (ns['OUTPUT'] / 'summary.json').write_text('changed', encoding='utf-8')
                return SimpleNamespace(returncode=0)
            self.assertTrue(module.endswith('.audit'))
            if fail_audit:
                return SimpleNamespace(returncode=1)
            definition = json.loads(flag('--assignment').read_text(encoding='utf-8'))
            result = audit(flag('--output'), definition)
            flag('--save').write_text(json.dumps(result), encoding='utf-8')
            return SimpleNamespace(returncode=0 if result['audit_complete'] else 1)
        ns['subprocess'] = SimpleNamespace(run=run)
        return calls

    def test_each_scope_uses_its_actual_ids(self):
        for role in ['TEST_SMOKE_1_BAI', 'TV1', 'TV2', 'TV3', 'TV4', 'TV5', 'ALL_GROUP_25K']:
            with self.subTest(role=role):
                ns = self.configure(role)
                self.assertEqual(ns['paper_count'], 1 if role.startswith('TEST') else 25000 if role.startswith('ALL') else 5000)
                self.assertIsNone(ns['RESULTS'])
                self.assertFalse(ns['RESULTS_ROOT'].exists())

    def test_rerun_preserves_previous_evidence_and_checkpoint(self):
        ns = self.configure()
        checkpoint = ns['OUTPUT'] / 'checkpoint.txt'
        checkpoint.write_text('keep checkpoint')
        paths = []
        def launch(command, **kwargs):
            path = Path(command[command.index('--results') + 1])
            self.assertFalse(path.exists())
            path.mkdir(parents=True)
            (path / 'benchmark.json').write_text('{}')
            (path / 'evidence.txt').write_text('keep benchmark')
            paths.append(path)
            return SimpleNamespace(returncode=0)
        ns['subprocess'] = SimpleNamespace(run=launch)
        exec(cell(12), ns)
        exec(cell(12), ns)
        self.assertNotEqual(paths[0], paths[1])
        self.assertEqual((paths[0] / 'evidence.txt').read_text(), 'keep benchmark')
        self.assertEqual(checkpoint.read_text(), 'keep checkpoint')

    def test_setup_failure_blocks_launch(self):
        ns = self.configure()
        ns['SOURCE_READY'] = False
        with self.assertRaises(AssertionError):
            exec(cell(12), ns)

    def test_member_audit_real_schema_and_null_rate(self):
        ns = self.configure()
        results = self.snapshot(ns)
        self.executor(ns)
        exec(cell(14), ns)
        data = json.loads((results / 'assignment-audit.json').read_text())
        self.assertEqual(data['assigned_input_count'], 5000)
        self.assertEqual(len(data['unseen_ids']), 5000)
        self.assertFalse(data['audit_complete'])
        self.assertFalse((results / 'group-assignment-audit.json').exists())
        self.assertIn("'unseen': 5000", self.output.getvalue())
        self.assertIn('0.00%', self.output.getvalue())
        self.assertIn('mẫu số', self.output.getvalue())
        checked = json.loads((results / 'statistics.json').read_text())
        self.assertEqual(checked['run_id'], 'current')
        self.assertEqual(checked['label'], 'live_unverified')

    def test_group_audit_separate_from_member(self):
        ns = self.configure()
        results = self.snapshot(ns)
        group = self.base / 'consolidated'
        group.mkdir()
        ns['GROUP_OUTPUT'] = group
        self.executor(ns)
        exec(cell(14), ns)
        member = json.loads((results / 'assignment-audit.json').read_text())
        whole = json.loads((results / 'group-assignment-audit.json').read_text())
        self.assertEqual(member['assigned_input_count'], 5000)
        self.assertEqual(whole['assigned_input_count'], 25000)
        self.assertEqual(len(whole['unseen_ids']), 25000)

    def test_all_group_scope(self):
        ns = self.configure('ALL_GROUP_25K')
        self.snapshot(ns)
        self.executor(ns)
        exec(cell(14), ns)
        self.assertEqual(ns['audit_data']['assigned_input_count'], 25000)

    def test_wrong_run_id_cannot_replace_statistics(self):
        ns = self.configure()
        results = self.snapshot(ns)
        report = json.loads((results / 'benchmark.json').read_text())
        report['run_id'] = 'old'
        (results / 'benchmark.json').write_text(json.dumps(report))
        calls = self.executor(ns)
        with self.assertRaises(ValueError):
            exec(cell(14), ns)
        self.assertFalse(calls)
        self.assertEqual((results / 'statistics.json').read_text(), 'original statistics')

    def test_changed_hash_before_revalidate_is_rejected(self):
        ns = self.configure()
        results = self.snapshot(ns)
        (ns['OUTPUT'] / 'summary.json').write_text('tampered')
        calls = self.executor(ns)
        with self.assertRaises(ValueError):
            exec(cell(14), ns)
        self.assertFalse(calls)
        self.assertEqual((results / 'statistics.json').read_text(), 'original statistics')

    def test_change_during_revalidate_cannot_replace_statistics(self):
        ns = self.configure()
        results = self.snapshot(ns)
        calls = self.executor(ns, mutate=True)
        with self.assertRaises(ValueError):
            exec(cell(14), ns)
        self.assertEqual(len(calls), 1)
        self.assertEqual((results / 'statistics.json').read_text(), 'original statistics')

    def test_failed_audit_cannot_reuse_previous_report(self):
        ns = self.configure()
        results = self.snapshot(ns)
        previous = results / 'assignment-audit.json'
        previous.write_text('previous evidence')
        self.executor(ns, fail_audit=True)
        with self.assertRaises(RuntimeError):
            exec(cell(14), ns)
        self.assertEqual(previous.read_text(), 'previous evidence')

    def test_setup_pins_commit_and_stops_on_test_failure(self):
        ns = dict(Path=Path, os=os, sys=sys)
        calls = []
        class Stub:
            def check_output(self, args, **kwargs):
                return '' if 'status' in args else ns['SOURCE_REF']
            def run(self, args, **kwargs):
                calls.append(args)
                self_check = kwargs.get('check')
                if not self_check:
                    raise AssertionError('Setup commands must check failure')
                if 'pytest' in args:
                    raise subprocess.CalledProcessError(1, args)
                return SimpleNamespace(returncode=0)
        # Replace only subprocess import for the setup harness; no git/network/pip executes.
        tree = ast.parse(''.join(NOTEBOOK['cells'][6]['source']))
        for node in tree.body:
            if isinstance(node, ast.Import):
                node.names = [name for name in node.names if name.name != 'subprocess']
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and node.targets[0].id == 'SOURCE':
                node.value = ast.parse(f'Path({str(SOURCE)!r})', mode='eval').body
        ns['subprocess'] = Stub()
        with self.assertRaises(subprocess.CalledProcessError):
            exec(compile(ast.fix_missing_locations(tree), 'setup', 'exec'), ns)
        self.assertFalse(ns['SOURCE_READY'])
        self.assertIn(['git', '-C', str(SOURCE), 'fetch', 'origin', ns['SOURCE_REF']], calls)
        self.assertFalse(any('pull' in args for args in calls))


if __name__ == '__main__':
    unittest.main()
