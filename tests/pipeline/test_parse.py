"""Offline parsing orchestration: real MS1-shaped inputs and mock stage adapters."""

from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from arxiv_pipeline.common.schemas import StageResult
from arxiv_pipeline.common.utils import atomic_json, paper_key, utc_now
from arxiv_pipeline.main import main
from arxiv_pipeline.pipeline import parse


class ParseTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source, self.output = self.base / 'ms1', self.base / 'ms2'
        self.paper = '2310.12345'
        self.record = self.write_paper(self.paper)
        self.calls = []
        self.adapters = {s: self.adapter(s) for s in
                         ('resolve', 'hierarchy', 'normalize', 'bibliography', 'deduplicate', 'validate_parse')}

    def write_paper(self, paper, count=2):
        key = paper_key(paper)
        versions = []
        for number in range(1, count + 1):
            source = f'raw/{key}/v{number}/source'
            corpus = f'corpus/{key}/v{number}'
            versions.append(dict(version=number, versioned_id=f'{paper}v{number}',
                                 source_path=source, corpus_path=corpus, source_url='https://example.invalid/source',
                                 submission_date='2026-01-01T00:00:00Z', updated_date='2026-01-01T00:00:00Z'))
            path = self.source / source
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('synthetic archive', encoding='utf-8')
            path = self.source / corpus / 'main.tex'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(r'\documentclass{article}\begin{document}\section{Intro}Hello.\end{document}', encoding='utf-8')
            for stage, artifacts in [('download', {'source': source}), ('extract', {'corpus': corpus})]:
                cp = StageResult(stage, paper, number, 'success', utc_now(), artifacts).to_dict()
                atomic_json(self.source / f'state/stages/{stage}/{key}/{number}.json', cp)
        record = dict(schema_version='1.0', input_id=paper, arxiv_id=paper, requested_version=None,
                      status='success', metadata={'latest_version': count}, versions=versions, error=None)
        atomic_json(self.source / f'state/discovery/{hashlib.sha256(paper.encode()).hexdigest()}.json', record)
        return record

    def adapter(self, stage, *, fail=None, escape=False, revision='1'):
        def execute(record, output_dir, *, version=None, upstream=None):
            number = version['version'] if version else None
            self.calls.append((stage, record['arxiv_id'], number))
            if fail:
                raise fail
            self.assertIn('input_root', upstream['context'])
            self.assertTrue(upstream['context']['input_snapshot_id'])
            paper = record['arxiv_id']
            key = paper_key(paper)
            status = 'success'
            payload = dict(schema_version='2.0', arxiv_id=paper, version=number)
            if stage == 'resolve':
                self.assertEqual(set(upstream['stages']), {'download', 'extract'})
                if upstream['stages']['download']['status'] == 'no_source':
                    status = 'no_source'
                artifact, name = f'parsed/{key}/v{number}/gathered.json', 'gathered'
                payload.update(is_empty_source=status == 'no_source',
                               merged_body_tex='' if status == 'no_source' else r'\section{Intro}Hello.',
                               main_tex_path=None if status == 'no_source' else version['corpus_path'] + '/main.tex',
                               input_snapshot_id=upstream['context']['input_snapshot_id'], warnings=[])
            elif stage in {'hierarchy', 'normalize', 'bibliography'}:
                dependency = {'hierarchy': 'resolve', 'normalize': 'hierarchy', 'bibliography': 'resolve'}[stage]
                self.assertEqual(set(upstream['stages']), {dependency})
                status = upstream['stages'][dependency]['status']
                name, filename = {'hierarchy': ('raw_tree', 'raw_tree.json'),
                                  'normalize': ('normalized_tree', 'normalized_tree.json'),
                                  'bibliography': ('raw_bibtex', 'raw_bibtex.json')}[stage]
                artifact = f'parsed/{key}/v{number}/{filename}'
                payload.update(entries=[] if stage == 'bibliography' else None, nodes=[], warnings=[])
            elif stage == 'deduplicate':
                self.assertIsNone(version)
                versions = upstream['versions']
                self.assertEqual([v['version'] for v in versions], [v['version'] for v in record['versions']])
                self.assertTrue(all(v['normalize']['status'] in {'success', 'no_source'} and
                                    v['bibliography']['status'] in {'success', 'no_source'} for v in versions))
                if all(v['normalize']['status'] == 'no_source' for v in versions):
                    status = 'no_source'
                artifacts = {}
                for table in ('elements', 'hierarchy', 'bibtex'):
                    artifacts[table] = f'tables/{key}/{table}.json'
                    atomic_json(output_dir / artifacts[table], [])
                artifacts['parsing_manifest'] = f'tables/{key}/parsing_manifest.json'
                atomic_json(output_dir / artifacts['parsing_manifest'], dict(schema_version='2.0', arxiv_id=paper,
                    input_snapshot_id=upstream['context']['input_snapshot_id'],
                    expected_versions=[v['version'] for v in record['versions']], complete=True,
                    versions=[dict(version=v['version'], status=v['normalize']['status'], reason=None, artifacts={}) for v in versions],
                    tables={t: artifacts[t] for t in ('elements', 'hierarchy', 'bibtex')},
                    counts=dict(elements_before=0, elements_after=0, bibtex_before=0, bibtex_after=0)))
                return StageResult(stage, paper, None, status, utc_now(), artifacts, schema_version='2.0')
            else:
                self.assertEqual(set(upstream['stages']), {'deduplicate'})
                artifact, name = f'validation/parsing/{key}.json', 'validation'
                payload.update(valid=True)
            atomic_json(output_dir / artifact, payload)
            return StageResult(stage, paper, number, status, utc_now(),
                               {name: '../escape.json' if escape else artifact}, schema_version='2.0')
        execute.__parsing_revision__ = revision
        return execute

    def run_pipeline(self, ids=None, **kwargs):
        return parse.run([self.paper] if ids is None else ids, self.output,
                         input_root=self.source, adapters=self.adapters, **kwargs)

    def counted(self):
        return Counter((stage, number) for stage, _, number in self.calls)

    def test_graph_manifest_and_ms1_input_is_read_only(self):
        before = {p: p.read_bytes() for p in self.source.rglob('*') if p.is_file()}
        summary = self.run_pipeline()
        self.assertTrue(summary['complete'], summary)
        self.assertEqual([(s, v) for s, _, v in self.calls],
                         [(s, v) for v in (1, 2) for s in ('resolve', 'hierarchy', 'normalize', 'bibliography')]
                         + [('deduplicate', None), ('validate_parse', None)])
        self.assertEqual(before, {p: p.read_bytes() for p in self.source.rglob('*') if p.is_file()})
        self.assertTrue((self.output / 'parsing_summary.json').is_file())
        self.assertTrue((self.output / 'manifests/stages.json').is_file())

    def test_resume_reuses_unchanged_checkpoints(self):
        self.run_pipeline()
        self.calls.clear()
        self.assertTrue(self.run_pipeline(resume=True)['complete'])
        self.assertEqual(self.calls, [])

    def test_force_reruns_selected_paper_only(self):
        other = '2310.12346'
        self.write_paper(other)
        self.run_pipeline(ids=[self.paper, other])
        before = (self.output / f'tables/{other}/parsing_manifest.json').read_bytes()
        self.calls.clear()
        self.assertTrue(self.run_pipeline(force=True)['complete'])
        self.assertEqual(len(self.calls), 10)
        self.assertTrue(all(p == self.paper for _, p, _ in self.calls))
        self.assertEqual(before, (self.output / f'tables/{other}/parsing_manifest.json').read_bytes())

    def test_changed_source_only_reruns_affected_version_and_paper(self):
        self.run_pipeline()
        path = self.source / 'corpus/2310.12345/v2/main.tex'
        path.write_text(path.read_text(encoding='utf-8') + 'changed', encoding='utf-8')
        self.calls.clear()
        self.assertTrue(self.run_pipeline(resume=True)['complete'])
        self.assertEqual(self.counted(), Counter({(s, 2): 1 for s in
                         ('resolve', 'hierarchy', 'normalize', 'bibliography')} |
                         {('deduplicate', None): 1, ('validate_parse', None): 1}))

    def test_new_version_invalidates_paper_export(self):
        self.run_pipeline()
        # Preserve existing inputs/checkpoints while adding v3 and updating discovery.
        saved = {p: p.read_bytes() for p in self.source.rglob('*') if p.is_file() and 'discovery' not in p.parts}
        self.write_paper(self.paper, count=3)
        for p, data in saved.items():
            p.write_bytes(data)
        self.calls.clear()
        self.assertTrue(self.run_pipeline(resume=True)['complete'])
        self.assertEqual([v for s, _, v in self.calls if s == 'resolve'], [3])
        self.assertIn(('deduplicate', None), self.counted())

    def test_frozen_input_reads_each_raw_archive_once_and_still_checks_resume(self):
        raw = self.source / 'raw/2310.12345/v1/source'
        original_hash = parse._file_hash
        with patch.object(parse, '_file_hash', wraps=original_hash) as file_hash:
            result = self.run_pipeline(frozen_input=True)
        self.assertTrue(result['complete'])
        self.assertEqual(sum(call.args == (raw.resolve(),) for call in file_hash.call_args_list), 1)
        raw.write_text('new archive bytes', encoding='utf-8')
        self.calls.clear()
        self.assertTrue(self.run_pipeline(resume=True, frozen_input=True)['complete'])
        self.assertEqual([v for s, _, v in self.calls if s == 'resolve'], [1])

    def test_resume_reuses_checkpoints_from_pr20(self):
        original_hash = parse._file_hash

        def legacy_hash(path):
            if path == Path(parse.__file__):
                return parse._COMPATIBLE_PIPELINE_REVISIONS[0]
            return original_hash(path)

        with patch.object(parse, '_file_hash', side_effect=legacy_hash):
            self.assertTrue(self.run_pipeline()['complete'])
        self.calls.clear()
        self.assertTrue(self.run_pipeline(resume=True, frozen_input=True)['complete'])
        self.assertEqual(self.calls, [])

    def test_missing_artifact_recomputes_stage_and_descendants(self):
        self.run_pipeline()
        (self.output / 'parsed/2310.12345/v1/normalized_tree.json').unlink()
        self.calls.clear()
        self.assertTrue(self.run_pipeline(resume=True)['complete'])
        self.assertEqual(self.counted(), Counter({('normalize', 1): 1, ('deduplicate', None): 1,
                                                  ('validate_parse', None): 1}))

    def test_tampered_table_is_not_reused(self):
        self.run_pipeline()
        (self.output / 'tables/2310.12345/elements.json').write_text('[{"changed":true}]', encoding='utf-8')
        self.calls.clear()
        self.assertTrue(self.run_pipeline(resume=True)['complete'])
        self.assertEqual(self.counted(), Counter({('deduplicate', None): 1, ('validate_parse', None): 1}))

    def test_failed_stage_does_not_block_independent_bibliography_or_other_version(self):
        self.adapters['normalize'] = self.adapter('normalize', fail=RuntimeError('synthetic failure'))
        summary = self.run_pipeline()
        self.assertFalse(summary['complete'])
        self.assertEqual(self.counted()[('bibliography', 1)], 1)
        self.assertEqual(self.counted()[('bibliography', 2)], 1)
        self.assertNotIn(('deduplicate', None), self.counted())
        self.assertEqual(summary['stage_statuses']['error'], 2)

    def test_unimplemented_adapter_is_reported_not_faked(self):
        self.adapters['normalize'] = self.adapter('normalize', fail=NotImplementedError('not delivered'))
        summary = self.run_pipeline()
        self.assertFalse(summary['complete'])
        self.assertEqual(summary['stage_statuses']['not_implemented'], 2)

    def test_partial_selection_invalidates_old_paper_manifest(self):
        self.run_pipeline()
        self.calls.clear()
        summary = self.run_pipeline(resume=True, selected_versions=[1])
        self.assertFalse(summary['complete'])
        self.assertNotIn(('deduplicate', None), self.counted())
        manifest = json.loads((self.output / 'tables/2310.12345/parsing_manifest.json').read_text(encoding='utf-8'))
        self.assertFalse(manifest['complete'])
        self.assertTrue(self.run_pipeline(resume=True)['complete'])

    def test_missing_ms1_checkpoint_does_not_become_no_source(self):
        (self.source / 'state/stages/extract/2310.12345/1.json').unlink()
        summary = self.run_pipeline()
        self.assertFalse(summary['complete'])
        self.assertNotIn(('resolve', 1), self.counted())

    def test_unsafe_adapter_artifact_is_rejected(self):
        self.adapters['resolve'] = self.adapter('resolve', escape=True)
        summary = self.run_pipeline()
        self.assertFalse(summary['complete'])
        self.assertEqual(summary['stage_statuses']['error'], 2)

    def test_input_change_during_execution_blocks_publication(self):
        original = self.adapters['resolve']
        def changing(*args, **kwargs):
            result = original(*args, **kwargs)
            path = self.source / kwargs['version']['source_path']
            path.write_text('source changed during parse', encoding='utf-8')
            return result
        self.adapters['resolve'] = changing
        self.assertFalse(self.run_pipeline()['complete'])
        self.assertNotIn(('deduplicate', None), self.counted())

    def test_module_revision_invalidates_stage_and_descendants(self):
        self.run_pipeline()
        self.adapters['normalize'].__parsing_revision__ = '2'
        self.calls.clear()
        self.assertTrue(self.run_pipeline(resume=True)['complete'])
        self.assertEqual(self.counted()[('normalize', 1)], 1)
        self.assertEqual(self.counted()[('normalize', 2)], 1)
        self.assertNotIn(('resolve', 1), self.counted())

    def test_real_resolver_and_hierarchy_match_orchestrator_envelope(self):
        from arxiv_pipeline.parsing.resolver import run as resolve
        from arxiv_pipeline.parsing.hierarchy import run as hierarchy
        self.adapters.update(resolve=resolve, hierarchy=hierarchy)
        self.assertTrue(self.run_pipeline()['complete'])
        tree = json.loads((self.output / 'parsed/2310.12345/v1/raw_tree.json').read_text(encoding='utf-8'))
        self.assertEqual(len(tree['nodes']), 3)

    def test_pdf_only_versions_are_preserved_with_empty_artifacts(self):
        for version in self.record['versions']:
            number = version['version']
            (self.source / version['corpus_path'] / 'main.tex').unlink()
            meta = f'raw/2310.12345/v{number}/download_metadata.json'
            atomic_json(self.source / meta, dict(arxiv_id=self.paper, version=number,
                                                status='no_source', source_format='pdf', http_status=200))
            atomic_json(self.source / f'state/stages/download/2310.12345/{number}.json',
                        StageResult('download', self.paper, number, 'no_source', utc_now(), {'download_metadata': meta}).to_dict())
            atomic_json(self.source / f'state/stages/extract/2310.12345/{number}.json',
                        StageResult('extract', self.paper, number, 'skipped', utc_now()).to_dict())
        from arxiv_pipeline.parsing.resolver import run as resolve
        from arxiv_pipeline.parsing.hierarchy import run as hierarchy
        self.adapters.update(resolve=resolve, hierarchy=hierarchy)
        summary = self.run_pipeline()
        self.assertTrue(summary['complete'], summary)
        self.assertEqual(summary['paper_results'][0]['status'], 'no_source')
        self.assertEqual(summary['paper_results'][0]['expected_versions'], [1, 2])

    def test_cli_parse_uses_snapshot_without_constructing_network_client(self):
        real_run = parse.run
        def mocked(ids, output, **kwargs):
            return real_run(ids, output, adapters=self.adapters, **kwargs)
        with patch('arxiv_pipeline.pipeline.parse.run', side_effect=mocked), \
                patch('arxiv_pipeline.main.ArxivClient', side_effect=AssertionError('network client constructed')), \
                redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            code = main(['--stage', 'parse', '--ms1-input', str(self.source), '--output', str(self.output)])
        self.assertEqual(code, 0)

    def test_cli_rejects_overlap_before_writing_logs(self):
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as raised:
            main(['--stage', 'parse', '--ms1-input', str(self.source), '--output', str(self.source)])
        self.assertEqual(raised.exception.code, 2)
        self.assertFalse((self.source / 'logs').exists())

    def test_input_change_during_export_blocks_final_success(self):
        original = self.adapters['deduplicate']
        def changing(*args, **kwargs):
            result = original(*args, **kwargs)
            (self.source / 'corpus/2310.12345/v1/main.tex').write_text('changed during export', encoding='utf-8')
            return result
        self.adapters['deduplicate'] = changing
        summary = self.run_pipeline()
        self.assertFalse(summary['complete'])
        self.assertEqual(summary['paper_results'][0]['artifacts'], {})
        manifest = json.loads((self.output / 'tables/2310.12345/parsing_manifest.json').read_text(encoding='utf-8'))
        self.assertFalse(manifest['complete'])

    def test_malformed_discovery_metadata_is_reported_without_crashing(self):
        cp = next((self.source / 'state/discovery').glob('*.json'))
        record = self.record.copy()
        record['metadata'] = 'not an object'
        atomic_json(cp, record)
        self.assertFalse(self.run_pipeline()['complete'])
        self.assertEqual(self.calls, [])

    def test_mismatched_result_version_type_cannot_be_reused(self):
        original = self.adapters['resolve']
        def wrong(*args, **kwargs):
            result = original(*args, **kwargs)
            result.version = True
            return result
        self.adapters['resolve'] = wrong
        summary = self.run_pipeline()
        self.assertFalse(summary['complete'])
        self.assertEqual(summary['stage_statuses']['error'], 2)

    def test_incomplete_export_manifest_is_rejected(self):
        original = self.adapters['deduplicate']
        def wrong(*args, **kwargs):
            result = original(*args, **kwargs)
            path = self.output / result.artifacts['parsing_manifest']
            manifest = json.loads(path.read_text(encoding='utf-8'))
            manifest['expected_versions'] = [1]
            atomic_json(path, manifest)
            return result
        self.adapters['deduplicate'] = wrong
        self.assertFalse(self.run_pipeline()['complete'])
        self.assertNotIn(('validate_parse', None), self.counted())

    def test_discovery_aliases_do_not_parse_same_paper_twice(self):
        record = self.record.copy()
        record['input_id'] = 'https://arxiv.org/abs/' + self.paper
        atomic_json(self.source / 'state/discovery/alias.json', record)
        self.assertTrue(self.run_pipeline()['complete'])
        self.assertEqual(len(self.calls), 10)

    def test_no_production_modules_are_fabricated_as_success(self):
        # Make one production import unavailable deliberately, independently of
        # which team modules have already been delivered on this branch.
        adapters = {s: adapter for s, adapter in self.adapters.items() if s != 'normalize'}
        with patch.dict(parse.MODULES, {'normalize': 'arxiv_pipeline.__missing_test_normalization__'}):
            summary = parse.run([self.paper], self.output, input_root=self.source, adapters=adapters)
        self.assertFalse(summary['complete'])
        self.assertEqual(summary['stage_statuses']['not_implemented'], 2)
        self.assertNotIn(('deduplicate', None), self.counted())
        self.assertNotIn(('validate_parse', None), self.counted())

    def test_rejects_bool_export_manifest_complete(self):
        original = self.adapters['deduplicate']
        def wrong(*args, **kwargs):
            result = original(*args, **kwargs)
            path = self.output / result.artifacts['parsing_manifest']
            manifest = json.loads(path.read_text(encoding='utf-8'))
            manifest['complete'] = 1
            atomic_json(path, manifest)
            return result
        self.adapters['deduplicate'] = wrong
        self.assertFalse(self.run_pipeline()['complete'])

    def test_resume_and_force_are_mutually_exclusive(self):
        with self.assertRaises(ValueError):
            self.run_pipeline(resume=True, force=True)

    def test_malformed_discovery_id_is_reported_not_raised(self):
        cp = next((self.source / 'state/discovery').glob('*.json'))
        record = self.record.copy()
        record.update(arxiv_id=123, input_id=123)
        atomic_json(cp, record)
        summary = self.run_pipeline(ids=[])
        self.assertFalse(summary['complete'])
        self.assertTrue(summary['input_errors'])

    def test_corrupt_discovery_invalidates_previous_complete_export(self):
        self.run_pipeline()
        cp = next((self.source / 'state/discovery').glob('*.json'))
        cp.write_text('{broken JSON', encoding='utf-8')
        self.assertFalse(self.run_pipeline(ids=[], resume=True)['complete'])
        manifest = json.loads((self.output / 'tables/2310.12345/parsing_manifest.json').read_text(encoding='utf-8'))
        self.assertFalse(manifest['complete'])
        state = json.loads((self.output / 'state/parsing/2310.12345.json').read_text(encoding='utf-8'))
        self.assertFalse(state['complete'])

    def test_output_log_junction_cannot_write_into_ms1(self):
        import os
        self.output.mkdir()
        link = self.output / 'logs'
        if os.name == 'nt':
            import _winapi
            _winapi.CreateJunction(str(self.source), str(link))
        else:
            link.symlink_to(self.source, target_is_directory=True)
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as raised:
            main(['--stage', 'parse', '--ms1-input', str(self.source), '--output', str(self.output)])
        self.assertEqual(raised.exception.code, 2)
        self.assertFalse((self.source / 'pipeline.log').exists())


    def test_real_tv4_bibtex_export_is_hashed_and_resumed_as_text(self):
        from arxiv_pipeline.parsing import bibliography, deduplication, hierarchy, resolver
        self.adapters.update(resolve=resolver.run, hierarchy=hierarchy.run,
                             bibliography=bibliography.run, deduplicate=deduplication.run)
        for version in self.record['versions']:
            path = self.source / version['corpus_path'] / 'main.tex'
            path.write_text(
                r'\documentclass{article}\begin{document}Hello \cite{x}.'
                r'\begin{thebibliography}{9}\bibitem{x} Author. Title. 2020.'
                r'\end{thebibliography}\end{document}', encoding='utf-8')
        events = []
        self.assertTrue(self.run_pipeline(observer=events.append)['complete'])
        bib = self.output / 'tables/2310.12345/references.bib'
        self.assertIn('@', bib.read_text(encoding='utf-8'))
        dependencies = json.loads((self.output / 'state/stages/deduplicate/2310.12345/paper.dependencies.json')
                                  .read_text(encoding='utf-8'))
        self.assertEqual(dependencies['artifacts']['tables/2310.12345/references.bib'],
                         hashlib.sha256(bib.read_bytes()).hexdigest())
        events.clear()
        self.assertTrue(self.run_pipeline(resume=True, observer=events.append)['complete'])
        self.assertTrue(next(e for e in events if e['stage'] == 'deduplicate')['cached'])
        bib.write_text('% modified export', encoding='utf-8')
        events.clear()
        self.assertTrue(self.run_pipeline(resume=True, observer=events.append)['complete'])
        self.assertFalse(next(e for e in events if e['stage'] == 'deduplicate')['cached'])
        self.assertIn('@', bib.read_text(encoding='utf-8'))

    def test_real_tv4_empty_bibtex_export_is_valid(self):
        from arxiv_pipeline.parsing import deduplication
        self.adapters['deduplicate'] = deduplication.run
        self.assertTrue(self.run_pipeline()['complete'])
        self.assertEqual((self.output / 'tables/2310.12345/references.bib').read_text(encoding='utf-8'), '')
        self.assertTrue(self.run_pipeline(resume=True)['complete'])

    def test_json_artifacts_still_require_valid_json(self):
        original = self.adapters['deduplicate']
        def malformed(*args, **kwargs):
            result = original(*args, **kwargs)
            (self.output / result.artifacts['elements']).write_text('{broken', encoding='utf-8')
            return result
        self.adapters['deduplicate'] = malformed
        summary = self.run_pipeline()
        self.assertFalse(summary['complete'])
        self.assertEqual(summary['stage_statuses']['error'], 1)
        self.assertNotIn(('validate_parse', None), self.counted())
