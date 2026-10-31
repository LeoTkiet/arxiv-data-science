"""Synthetic MS2 handoff examples, not parser implementations or real data.

Run tests/parsing/test_ms2_contracts.py to verify these examples. Generated JSON
and TeX live only in a caller-provided temporary directory, never in the repo.
"""

from dataclasses import asdict
from pathlib import Path

from arxiv_pipeline.common.schemas import (
    BibtexRow, DiscoveryRecord, ElementRow, HierarchyRow, PARSING_SCHEMA_VERSION,
    VersionRecord,
)

PAPER = '2310.12345'
PREFIX = '2310-12345#'
STAMP = '2026-10-29T00:00:00+00:00'


def examples():
    """Return fresh JSON-compatible objects for each call."""
    common = dict(schema_version=PARSING_SCHEMA_VERSION, arxiv_id=PAPER, version=1)
    root = dict(temp_id='r1', parent_temp_id=None, position=0,
                kind='document', raw_content='Version 1 root')
    heading = dict(temp_id='h1', parent_temp_id='r1', position=0,
                   kind='heading', raw_content='Introduction')
    prose = dict(temp_id='p1', parent_temp_id='h1', position=0,
                 kind='prose_block', raw_content='We study the citation matching problem.')
    gathered = dict(common, is_empty_source=False,
                    main_tex_path='corpus/2310.12345/v1/main.tex',
                    included_files=['corpus/2310.12345/v1/sections/intro.tex'],
                    ignored_files=['corpus/2310.12345/v1/draft.tex'],
                    merged_body_tex='\\section{Introduction}\nWe study the citation matching problem.',
                    preamble_tex='\\documentclass{article}',
                    raw_bibitems=['\\bibitem{example2026} A. Example. Synthetic title. 2026.'],
                    bib_file_paths=['corpus/2310.12345/v1/refs.bib'],
                    referenced_bib_file_paths=['corpus/2310.12345/v1/refs.bib'],
                    warnings=[dict(code='source_map_unavailable',
                                   message='Synthetic example omits source offsets.',
                                   source_path=None)], source_spans=[],
                    input_snapshot_id='synthetic-snapshot-1')
    raw_tree = dict(common, nodes=[root, heading, prose], warnings=[])
    normalized_tree = dict(common, nodes=[
        dict(temp_id='r1', parent_temp_id=None, position=0, kind='document',
             cleaned_content='Version 1 root'),
        dict(temp_id='h1', parent_temp_id='r1', position=0, kind='heading',
             cleaned_content='Introduction'),
        dict(temp_id='s1', parent_temp_id='h1', position=0, kind='sentence',
             cleaned_content='We study the citation matching problem'),
    ], warnings=[])
    bib = asdict(BibtexRow(PAPER, 'example2026', 'article', 'Synthetic title',
                          ['A. Example'], 2026))
    elements = [asdict(ElementRow(PREFIX + suffix, PAPER, kind, content))
                for suffix, kind, content in [
                    ('root1', 'document', 'Version 1 root'),
                    ('root2', 'document', 'Version 2 root'),
                    ('h1', 'heading', 'Introduction'),
                    ('h2', 'heading', 'Background and model'),
                    ('s1', 'sentence', 'We study the citation matching problem'),
                ]]
    hierarchy = [asdict(HierarchyRow(PAPER, v, PREFIX + child, PREFIX + parent))
                 for v, child, parent in [(1, 'h1', 'root1'), (1, 's1', 'h1'),
                                          (2, 'h2', 'root2'), (2, 's1', 'h2')]]
    occurrences = [dict(occurrence_id=oid, parent_occurrence_id=parent,
                        position=pos, element_id=PREFIX + element)
                   for oid, parent, pos, element in [
                       ('o1', None, 0, 'root1'), ('o2', 'o1', 0, 'h1'),
                       ('o3', 'o2', 0, 's1'), ('o4', 'o2', 1, 's1')]]
    empty = dict(gathered, version=3, is_empty_source=True, main_tex_path=None,
                 included_files=[], ignored_files=[], merged_body_tex='', preamble_tex='',
                 raw_bibitems=[], bib_file_paths=[], referenced_bib_file_paths=[],
                 warnings=[dict(code='no_tex_source', message='Confirmed PDF-only fixture.',
                                source_path=None)])
    bib_only = dict(empty, version=4, bib_file_paths=['corpus/2310.12345/v4/refs.bib'],
                    warnings=[dict(code='no_tex_source', message='Only BibTeX remains.',
                                   source_path=None)])
    return dict(gathered=gathered, raw_tree=raw_tree, normalized_tree=normalized_tree,
                raw_bibtex=dict(common, entries=[bib], warnings=[]),
                elements=elements, hierarchy=hierarchy, bibtex=[bib],
                repeated_occurrences=dict(common, occurrences=occurrences),
                empty_gathered=empty,
                bib_only_gathered=bib_only,
                bib_only_tables=dict(elements=[], hierarchy=[], bibtex=[bib]),
                empty_tree=dict(common, version=3, nodes=[], warnings=[]),
                empty_bibtex=dict(common, version=3, entries=[], warnings=[]),
                empty_occurrences=dict(common, version=3, occurrences=[]))


def write_corpus_fixture(input_root: Path):
    """Build two TeX versions plus one explicitly empty version's discovery record.

    The caller supplies an empty temporary root. Checkpoints are not fabricated:
    mock adapters/tests must supply download/extract results explicitly.
    """
    versions = []
    for number in (1, 2, 3):
        rel = f'corpus/{PAPER}/v{number}'
        versions.append(VersionRecord(number, f'{PAPER}v{number}',
                                      f'https://example.invalid/{PAPER}v{number}',
                                      f'raw/{PAPER}/v{number}/source', rel, STAMP, STAMP))
        if number == 3:
            continue
        corpus = input_root / rel
        (corpus / 'sections').mkdir(parents=True)
        (corpus / 'main.tex').write_text(
            '\\documentclass{article}\n\\begin{document}\n'
            '\\input{sections/intro}\n% \\input{draft}\n'
            '\\bibliography{refs}\n\\end{document}\n', encoding='utf-8')
        title = 'Introduction' if number == 1 else 'Background and model'
        (corpus / 'sections/intro.tex').write_text(
            '\\section{' + title + '}\nWe study the citation matching problem.', encoding='utf-8')
        (corpus / 'draft.tex').write_text('Unused draft.', encoding='utf-8')
        (corpus / 'refs.bib').write_text(
            '@article{example2026, title={Synthetic title}, author={A. Example}, year={2026}}',
            encoding='utf-8')
    return DiscoveryRecord(PAPER, PAPER, None, 'success', STAMP,
                           metadata={'latest_version': 3}, versions=versions).to_dict()
