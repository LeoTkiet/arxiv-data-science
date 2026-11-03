"""Resolver contract and content tests; all corpus/checkpoint inputs are synthetic."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from arxiv_pipeline.parsing import resolver


class ResolverTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / 'ms1'
        self.output = self.base / 'ms2'
        self.corpus = self.source / 'corpus/2310.12345/v1'
        self.corpus.mkdir(parents=True)
        self.record = {'arxiv_id': '2310.12345', 'status': 'success'}
        self.version = dict(version=1, versioned_id='2310.12345v1',
                            source_path='raw/2310.12345/v1/source',
                            corpus_path='corpus/2310.12345/v1')
        self.write(self.version['source_path'], 'synthetic archive bytes')
        self.upstream = dict(context=dict(input_root=str(self.source), input_snapshot_id='fixture-snapshot'),
                             stages=dict(download=self.stage('download', 'success',
                                         {'source': self.version['source_path']}),
                                         extract=self.stage('extract', 'success',
                                         {'corpus': self.version['corpus_path']})))

    def stage(self, name, status, artifacts=None, error=None):
        return dict(schema_version='1.0', stage=name, paper_id='2310.12345', version=1,
                    status=status, artifacts=artifacts or {}, error=error)

    def write(self, relative, text):
        target = self.source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')
        return target

    def tex(self, relative, text):
        return self.write(self.version['corpus_path'] + '/' + relative, text)

    def main(self, body='Hello world.', preamble=''):
        return self.tex('main.tex', '\\documentclass{article}\n' + preamble +
                        '\n\\begin{document}\n' + body + '\n\\end{document}')

    def run_resolver(self):
        return resolver.run(self.record, self.output, version=self.version, upstream=self.upstream)

    def gathered(self):
        result = self.run_resolver()
        self.assertEqual(result.status, 'success', result.error)
        self.assertEqual((result.stage, result.paper_id, result.version, result.schema_version),
                         ('resolve', '2310.12345', 1, '2.0'))
        return json.loads((self.output / result.artifacts['gathered']).read_text(encoding='utf-8'))

    def test_single_file_contract_and_input_unchanged(self):
        self.main('\\section{Intro} Hello $x$ \\cite{a}.', '\\newcommand{\\foo}{bar}')
        before = {p: p.read_bytes() for p in self.source.rglob('*') if p.is_file()}
        doc = self.gathered()
        self.assertEqual(doc['schema_version'], '2.0')
        self.assertIn('\\newcommand', doc['preamble_tex'])
        self.assertIn('\\cite{a}', doc['merged_body_tex'])
        self.assertNotIn('documentclass', doc['merged_body_tex'])
        self.assertEqual(doc['main_tex_path'], 'corpus/2310.12345/v1/main.tex')
        self.assertEqual(doc['input_snapshot_id'], 'fixture-snapshot')
        self.assertEqual(before, {p: p.read_bytes() for p in self.source.rglob('*') if p.is_file()})
        self.assertNotIn(str(self.source), json.dumps(doc))

    def test_nested_relative_includes_order_repetition_and_ignored(self):
        self.main('Start \\input{sections/intro} Middle \\include{last} \\input{sections/intro} End')
        self.tex('sections/intro.tex', 'Intro \\input{nested/detail}')
        self.tex('sections/nested/detail.tex', 'Detail')
        self.tex('last.tex', 'Last')
        self.tex('draft.tex', 'Trash')
        doc = self.gathered()
        body = ' '.join(doc['merged_body_tex'].split())
        self.assertEqual(body, 'Start Intro Detail Middle Last Intro Detail End')
        self.assertEqual(len(doc['included_files']), 3)
        self.assertEqual(doc['ignored_files'], ['corpus/2310.12345/v1/draft.tex'])

    def test_unbraced_input_and_root_fallback(self):
        self.main('\\input sections/a.tex')
        self.tex('sections/a.tex', 'A \\input shared')
        self.tex('shared.tex', 'Shared')
        self.assertIn('A Shared', self.gathered()['merged_body_tex'])

    def test_comments_escape_parity_and_no_extra_word_boundary(self):
        self.main('A% ignored \\input{missing}\nB \\% visible % hidden\nC \\\\% hidden too\nD')
        body = self.gathered()['merged_body_tex']
        self.assertIn('AB', body)
        self.assertIn('\\% visible', body)
        self.assertNotIn('hidden', body)
        self.assertNotIn('missing', body)

    def test_verbatim_and_macro_definitions_do_not_expand_fake_includes(self):
        self.main('\\verb|% \\input{missing}|\n\\begin{verbatim}\n% \\input{missing}\n'
                  '\\end{verbatim}\nActual', '\\newcommand{\\fake}{\\input{missing}}')
        doc = self.gathered()
        self.assertIn('% \\input{missing}', doc['merged_body_tex'])
        self.assertIn('\\input{missing}', doc['preamble_tex'])

    def test_comments_cannot_create_fake_root(self):
        self.main('Real')
        self.tex('paper.tex', '% \\documentclass{article}\nUnused')
        self.assertTrue(self.gathered()['main_tex_path'].endswith('main.tex'))

    def test_root_selection_is_deterministic_and_warns(self):
        self.main('Main')
        self.tex('paper.tex', '\\documentclass{article}\\begin{document}Other\\end{document}')
        doc = self.gathered()
        self.assertTrue(doc['main_tex_path'].endswith('main.tex'))
        self.assertIn('ambiguous_root', [w['code'] for w in doc['warnings']])

    def test_document_boundaries_in_included_file_and_stop_after_end(self):
        self.tex('main.tex', '\\documentclass{article}\\input{body}\\input{missing}')
        self.tex('body.tex', '\\begin{document}Included body\\end{document}')
        self.assertEqual(self.gathered()['merged_body_tex'], 'Included body')

    def test_include_in_preamble_and_endinput(self):
        self.main('Body', '\\input{macros}')
        self.tex('macros.tex', '\\newcommand{\\foo}{value}\\endinput \\input{missing}')
        self.assertIn('\\newcommand', self.gathered()['preamble_tex'])

    def test_includeonly_skips_excluded_include_but_not_input(self):
        self.main('\\include{a}\\include{b}\\input{c}', '\\includeonly{a}')
        for name in ('a', 'b', 'c'):
            self.tex(name + '.tex', name.upper())
        doc = self.gathered()
        self.assertEqual(doc['merged_body_tex'].strip(), 'AC')
        self.assertIn('corpus/2310.12345/v1/b.tex', doc['ignored_files'])

    def test_bibliography_removed_and_appendix_kept(self):
        self.main('Body \\cite{one}.\\begin{thebibliography}{99}\n'
                  '\\bibitem[Label]{one} First.\n\\bibitem{two} Second.\n'
                  '\\end{thebibliography}\n\\section*{Appendix} Proof.')
        doc = self.gathered()
        self.assertEqual(len(doc['raw_bibitems']), 2)
        self.assertTrue(doc['raw_bibitems'][0].startswith('\\bibitem[Label]{one}'))
        self.assertNotIn('bibitem', doc['merged_body_tex'])
        self.assertIn('Appendix', doc['merged_body_tex'])
        self.assertIn('\\cite{one}', doc['merged_body_tex'])

    def test_references_heading_with_bibitems_stops_before_acknowledgements(self):
        self.main('Body\\section*{References}\\bibitem{x} Entry.'
                  '\\section*{Acknowledgements} Thanks.\\section*{Appendix} Proof.')
        doc = self.gathered()
        self.assertEqual(len(doc['raw_bibitems']), 1)
        self.assertNotIn('References', doc['merged_body_tex'])
        self.assertIn('Acknowledgements', doc['merged_body_tex'])
        self.assertIn('Appendix', doc['merged_body_tex'])

    def test_bib_inventory_reference_subset_and_missing_warning(self):
        self.main('Body\\bibliographystyle{plain}\\bibliography{refs,missing}\\printbibliography',
                  '\\addbibresource{refs.bib}')
        self.tex('refs.bib', '@misc{x, title={X}}')
        self.tex('unused.bib', '@misc{y, title={Y}}')
        doc = self.gathered()
        self.assertEqual(len(doc['bib_file_paths']), 2)
        self.assertEqual(doc['referenced_bib_file_paths'], ['corpus/2310.12345/v1/refs.bib'])
        self.assertNotIn('bibliography', doc['merged_body_tex'])
        self.assertIn('missing_bibliography', [w['code'] for w in doc['warnings']])

    def test_source_spans_are_exact_after_comment_include_and_reference_removal(self):
        self.main('Before%comment\n\\input{a}\\begin{thebibliography}{1}\\bibitem{x} X'
                  '\\end{thebibliography}After\\input{a}')
        self.tex('a.tex', 'Included')
        doc = self.gathered()
        cursor = 0
        for span in doc['source_spans']:
            self.assertEqual(span['start'], cursor)
            # Offsets refer to decoded original source, including Windows CRLF.
            original = (self.source / span['source_path']).read_bytes().decode('utf-8')
            self.assertEqual(doc['merged_body_tex'][span['start']:span['end']],
                             original[span['source_start']:span['source_end']])
            cursor = span['end']
        self.assertEqual(cursor, len(doc['merged_body_tex']))
        self.assertEqual(doc['merged_body_tex'].count('Included'), 2)

    def test_no_source_requires_pdf_sidecar_and_keeps_artifact(self):
        meta = 'raw/2310.12345/v1/download_metadata.json'
        self.write(meta, json.dumps(dict(arxiv_id='2310.12345', version=1, status='no_source',
                                        source_format='pdf', http_status=200)))
        self.upstream['stages']['download'] = self.stage('download', 'no_source', {'download_metadata': meta})
        self.upstream['stages']['extract'] = self.stage('extract', 'skipped')
        result = self.run_resolver()
        self.assertEqual(result.status, 'no_source', result.error)
        doc = json.loads((self.output / result.artifacts['gathered']).read_text(encoding='utf-8'))
        self.assertTrue(doc['is_empty_source'])
        self.assertEqual(doc['merged_body_tex'], '')
        self.assertIsNone(doc['main_tex_path'])

    def test_bib_only_preserves_inventory(self):
        self.tex('refs.bib', '@misc{x, title={X}}')
        result = self.run_resolver()
        self.assertEqual(result.status, 'no_source')
        doc = json.loads((self.output / result.artifacts['gathered']).read_text(encoding='utf-8'))
        self.assertEqual(doc['bib_file_paths'], ['corpus/2310.12345/v1/refs.bib'])

    def test_extract_confirmed_empty_archive(self):
        self.upstream['stages']['extract'] = self.stage('extract', 'no_source', error=dict(
            kind='no_tex_source', message='archive contains no valid .tex or .bib files'))
        self.assertEqual(self.run_resolver().status, 'no_source')

    def test_missing_checkpoints_are_skipped_not_empty(self):
        self.upstream['stages'] = {}
        result = self.run_resolver()
        self.assertEqual(result.status, 'skipped')
        self.assertEqual(result.artifacts, {})

    def test_upstream_errors_block_resolve(self):
        for name, status in [('download', 'api_error'), ('extract', 'data_error')]:
            with self.subTest(name=name):
                saved = self.upstream['stages'][name]
                self.upstream['stages'][name] = self.stage(name, status)
                self.assertEqual(self.run_resolver().status, 'skipped')
                self.upstream['stages'][name] = saved

    def test_inconsistent_or_unverified_inputs_are_data_errors(self):
        cases = ['missing_source', 'empty_source', 'missing_corpus', 'empty_corpus',
                 'mismatched_checkpoint', 'wrong_schema', 'html_no_source']
        for case in cases:
            with self.subTest(case=case):
                self.setUp_case(case)
                result = self.run_resolver()
                self.assertEqual(result.status, 'data_error', (case, result.error))
                self.assertFalse(result.artifacts)

    def setUp_case(self, case):
        # Each subcase uses an independent synthetic root.
        case_base = self.base / case
        self.source = case_base / 'ms1'
        self.output = case_base / 'ms2'
        self.corpus = self.source / self.version['corpus_path']
        self.corpus.mkdir(parents=True)
        self.main()
        self.write(self.version['source_path'], 'archive bytes')
        self.upstream = dict(context=dict(input_root=str(self.source), input_snapshot_id='fixture'),
                             stages=dict(download=self.stage('download', 'success', {'source': self.version['source_path']}),
                                         extract=self.stage('extract', 'success', {'corpus': self.version['corpus_path']})))
        if case == 'missing_source':
            (self.source / self.version['source_path']).unlink()
        elif case == 'empty_source':
            self.write(self.version['source_path'], '')
        elif case == 'missing_corpus':
            (self.corpus / 'main.tex').unlink()
            self.corpus.rmdir()
        elif case == 'empty_corpus':
            (self.corpus / 'main.tex').unlink()
        elif case == 'mismatched_checkpoint':
            self.upstream['stages']['extract']['paper_id'] = '2310.99999'
        elif case == 'wrong_schema':
            self.upstream['stages']['download']['schema_version'] = '99'
        else:
            meta = 'raw/2310.12345/v1/download_metadata.json'
            self.write(meta, json.dumps(dict(arxiv_id='2310.12345', version=1, status='no_source', source_format='html')))
            self.upstream['stages']['download'] = self.stage('download', 'no_source', {'download_metadata': meta})
            self.upstream['stages']['extract'] = self.stage('extract', 'skipped')

    def test_invalid_tex_does_not_return_partial_success(self):
        cases = [r'\input{missing}', r'\input{a}', r'\input{\dynamic}',
                 r'\begin{thebibliography}{9}\bibitem{x} No end',
                 r'\iffalse\input{a}\fi']
        for body in cases:
            with self.subTest(body=body):
                self.main(body)
                self.tex('a.tex', r'\input{a}')
                self.assertEqual(self.run_resolver().status, 'data_error')
                self.assertFalse((self.output / 'parsed/2310.12345/v1/gathered.json').exists())

    def test_no_root_or_document_delimiters_is_error(self):
        for tex in ['Just prose.', r'\documentclass{article} No document']:
            with self.subTest(tex=tex):
                self.tex('main.tex', tex)
                self.assertEqual(self.run_resolver().status, 'data_error')

    def test_include_cannot_escape_version_corpus(self):
        self.main(r'\input{../secret}')
        self.write('corpus/2310.12345/secret.tex', 'Secret')
        result = self.run_resolver()
        self.assertEqual(result.status, 'data_error')

    def test_absolute_include_and_corpus_path_rejected(self):
        self.main('\\input{' + (self.corpus / 'outside.tex').as_posix() + '}')
        self.assertEqual(self.run_resolver().status, 'data_error')
        self.version['corpus_path'] = '../outside'
        self.assertEqual(self.run_resolver().status, 'data_error')

    def test_output_must_not_overlap_input(self):
        self.main()
        self.output = self.source / 'outputs'
        self.assertEqual(self.run_resolver().status, 'data_error')

    def test_symlink_outside_corpus_is_rejected(self):
        self.main(r'\input{escape}')
        outside = self.base / 'outside.tex'
        outside.write_text('Secret', encoding='utf-8')
        try:
            (self.corpus / 'escape.tex').symlink_to(outside)
        except OSError:
            self.skipTest('Symlink privilege unavailable on this Windows host')
        self.assertEqual(self.run_resolver().status, 'data_error')

    def test_write_failure_returns_error_without_success_artifact(self):
        self.main()
        with patch.object(resolver, 'atomic_json', side_effect=OSError('disk full'), create=True):
            result = self.run_resolver()
        self.assertEqual(result.status, 'error')
        self.assertEqual(result.artifacts, {})

    def test_parent_relative_include_within_corpus_is_valid(self):
        self.main(r'\input{sections/a}')
        self.tex('sections/a.tex', r'A \input{../shared}')
        self.tex('shared.tex', 'Shared')
        self.assertIn('A Shared', self.gathered()['merged_body_tex'])

    def test_executed_macro_that_controls_compilation_is_rejected(self):
        self.main(r'\loadpart', r'\newcommand{\loadpart}{\input{a}}')
        self.tex('a.tex', 'Actual included text')
        result = self.run_resolver()
        self.assertEqual(result.status, 'data_error')
        self.assertEqual(result.error.kind, 'dynamic_include')

    def test_section_wrapper_does_not_leak_bibliography_end_into_bibitem(self):
        self.main(r'Body\section*{References}\begin{thebibliography}{9}'
                  r'\bibitem{x} Entry.\end{thebibliography}'
                  r'\section*{Appendix} Proof.')
        doc = self.gathered()
        self.assertEqual(doc['raw_bibitems'], [r'\bibitem{x} Entry.'])
        self.assertIn('Proof.', doc['merged_body_tex'])

    def test_depth_and_expansion_budgets_fail_instead_of_partial_output(self):
        self.main(r'\input{a}\input{a}')
        self.tex('a.tex', 'Words')
        with patch.object(resolver, 'MAX_INCLUDE_DEPTH', 1, create=True):
            self.assertEqual(self.run_resolver().status, 'data_error')
        with patch.object(resolver, 'MAX_EXPANDED_CHARS', 8, create=True):
            self.assertEqual(self.run_resolver().status, 'data_error')

    def test_versioned_id_must_match_paper_and_version(self):
        self.main()
        self.version['versioned_id'] = '2310.12345v2'
        self.assertEqual(self.run_resolver().status, 'data_error')

    def test_comment_between_begin_command_and_argument(self):
        self.tex('main.tex', '\\documentclass{article}\\begin% delimiter comment\n'
                 '{document}Body\\end{document}')
        self.assertEqual(self.gathered()['merged_body_tex'], 'Body')

    def test_malformed_unused_tail_after_end_document_is_not_parsed(self):
        main = self.main('Body')
        with main.open('a', encoding='utf-8') as stream:
            stream.write(r'\begin{broken')
        self.assertEqual(self.gathered()['merged_body_tex'].strip(), 'Body')

    def test_unsupported_compilation_directives_fail_explicitly(self):
        self.main(r'\import{sections/}{intro.tex}')
        result = self.run_resolver()
        self.assertEqual(result.status, 'data_error')
        self.assertEqual(result.error.kind, 'unsupported_tex')

    def test_include_inside_heading_argument_is_expanded(self):
        self.main(r'\section{\input{title}} Body')
        self.tex('title.tex', 'Actual title')
        doc = self.gathered()
        self.assertIn(r'\section{Actual title}', doc['merged_body_tex'])
        self.assertEqual(len(doc['included_files']), 1)

    def test_empty_checkpoint_cannot_override_real_corpus(self):
        self.main('Real body')
        self.upstream['stages']['extract'] = self.stage('extract', 'no_source', error=dict(
            kind='no_tex_source', message='archive contains no valid .tex or .bib files'))
        self.assertEqual(self.run_resolver().status, 'data_error')

    def test_comment_before_verbatim_argument_preserves_literal_percent(self):
        self.main('\\begin% delimiter comment\n{verbatim}\n100% literal text\n'
                  '\\end{verbatim}\nBody')
        self.assertIn('100% literal text', self.gathered()['merged_body_tex'])

    def test_environment_with_indirect_include_is_rejected(self):
        self.main(r'\begin{load}Inside\end{load}',
                  r'\newcommand{\loader}{\input{a}}\newenvironment{load}{\loader}{}')
        self.tex('a.tex', 'Included content')
        self.assertEqual(self.run_resolver().status, 'data_error')

    def test_appendix_command_after_references_is_preserved(self):
        self.main(r'Body\section*{References}\begin{thebibliography}{9}'
                  r'\bibitem{x} Entry.\end{thebibliography}'
                  r'\appendix\section{Proof} Appendix content.')
        self.assertIn(r'\appendix\section{Proof}', self.gathered()['merged_body_tex'])

    def test_legacy_paper_key_and_version_isolation(self):
        self.record['arxiv_id'] = 'hep-th/9901001'
        self.version.update(version=2, versioned_id='hep-th/9901001v2',
                            source_path='raw/hep-th_9901001/v2/source',
                            corpus_path='corpus/hep-th_9901001/v2')
        self.write(self.version['source_path'], 'archive')
        self.main('Legacy')
        for stage in self.upstream['stages'].values():
            stage.update(paper_id='hep-th/9901001', version=2)
        self.upstream['stages']['download']['artifacts']['source'] = self.version['source_path']
        self.upstream['stages']['extract']['artifacts']['corpus'] = self.version['corpus_path']
        doc = self.gathered_legacy()
        self.assertEqual(doc['version'], 2)

    def gathered_legacy(self):
        result = self.run_resolver()
        self.assertEqual(result.status, 'success', result.error)
        self.assertEqual(result.artifacts['gathered'], 'parsed/hep-th_9901001/v2/gathered.json')
        return json.loads((self.output / result.artifacts['gathered']).read_text(encoding='utf-8'))
