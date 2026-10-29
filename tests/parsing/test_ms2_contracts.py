"""Executable contract examples; no network or real corpus required."""

from dataclasses import asdict, fields
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from arxiv_pipeline.common.schemas import (
    BibtexRow, ElementRow, HierarchyRow, PARSING_SCHEMA_VERSION, StageResult,
)
from parsing.ms2_contract_examples import examples, write_corpus_fixture


class MS2ContractTests(TestCase):
    def test_output_columns_match_official_spec(self):
        self.assertEqual([f.name for f in fields(ElementRow)],
                         ['element_id', 'arxiv_id', 'kind', 'content'])
        self.assertEqual([f.name for f in fields(HierarchyRow)],
                         ['arxiv_id', 'version', 'element_id', 'parent_id'])
        self.assertEqual([f.name for f in fields(BibtexRow)],
                         ['arxiv_id', 'citation_key', 'entry_type', 'title',
                          'authors', 'year', 'doi', 'journal', 'url'])

    def test_official_two_version_example(self):
        docs = examples()
        elements, edges = docs['elements'], docs['hierarchy']
        self.assertEqual(len(elements), 5)
        self.assertEqual(len(edges), 4)
        ids = {row['element_id'] for row in elements}
        self.assertTrue(all(e['parent_id'] in ids and e['element_id'] in ids for e in edges))
        shared = [e for e in edges if e['element_id'] == '2310-12345#s1']
        self.assertEqual([e['version'] for e in shared], [1, 2])
        self.assertNotEqual(shared[0]['parent_id'], shared[1]['parent_id'])
        self.assertEqual(json.loads(json.dumps(docs, allow_nan=False)), docs)

    def test_occurrences_preserve_repetition_and_order(self):
        rows = examples()['repeated_occurrences']['occurrences']
        repeated = rows[2:]
        self.assertEqual(repeated[0]['element_id'], repeated[1]['element_id'])
        self.assertEqual(repeated[0]['parent_occurrence_id'], repeated[1]['parent_occurrence_id'])
        self.assertNotEqual(repeated[0]['occurrence_id'], repeated[1]['occurrence_id'])
        self.assertEqual([r['position'] for r in repeated], [0, 1])

    def test_empty_version_has_no_parsed_nodes(self):
        docs = examples()
        self.assertTrue(docs['empty_gathered']['is_empty_source'])
        self.assertEqual(docs['empty_tree']['nodes'], [])
        self.assertEqual(docs['empty_bibtex']['entries'], [])
        self.assertEqual(docs['empty_occurrences']['occurrences'], [])

    def test_ms1_default_unchanged_and_ms2_explicit(self):
        old = StageResult('extract', '2310.12345', 1, 'success', '2026-10-29T00:00:00+00:00')
        self.assertEqual(old.to_dict()['schema_version'], '1.0')
        new = StageResult('resolve', old.paper_id, 1, 'success', old.timestamp,
                          schema_version=PARSING_SCHEMA_VERSION)
        self.assertEqual(new.to_dict()['schema_version'], '2.0')
        self.assertIsNone(asdict(BibtexRow(old.paper_id, 'ref', 'misc', '', [], None))['year'])

    def test_bib_only_keeps_references_without_parsed_elements(self):
        docs = examples()
        self.assertTrue(docs['bib_only_gathered']['is_empty_source'])
        self.assertTrue(docs['bib_only_gathered']['bib_file_paths'])
        self.assertEqual(docs['bib_only_tables']['elements'], [])
        self.assertEqual(docs['bib_only_tables']['hierarchy'], [])
        self.assertEqual(len(docs['bib_only_tables']['bibtex']), 1)

    def test_missing_provenance_and_empty_have_explanations(self):
        docs = examples()
        self.assertIn('source_map_unavailable', [w['code'] for w in docs['gathered']['warnings']])
        self.assertIn('no_tex_source', [w['code'] for w in docs['empty_gathered']['warnings']])

    def test_fixture_separates_input_output_and_keeps_versions(self):
        with TemporaryDirectory() as folder:
            base = Path(folder)
            source = base / 'ms1'
            output = base / 'ms2'
            record = write_corpus_fixture(source)
            before = {str(p.relative_to(source)): p.read_bytes() for p in source.rglob('*') if p.is_file()}
            self.assertEqual([v['version'] for v in record['versions']], [1, 2, 3])
            for v in record['versions'][:2]:
                self.assertTrue((source / v['corpus_path'] / 'main.tex').exists())
            self.assertFalse((source / record['versions'][2]['corpus_path']).exists())
            output.mkdir()
            (output / 'example.json').write_text(json.dumps(examples()), encoding='utf-8')
            after = {str(p.relative_to(source)): p.read_bytes() for p in source.rglob('*') if p.is_file()}
            self.assertEqual(before, after)
