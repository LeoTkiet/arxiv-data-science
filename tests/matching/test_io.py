"""Snapshot boundary tests: readiness is evidence, never just file existence."""
import hashlib
import json

import pytest

from arxiv_pipeline.common.utils import atomic_json
from tests.fixtures.matching.examples import publications


def ready_snapshot(root, items=None):
    """A small explicit synthetic upstream handoff with real checkpoint shapes."""
    ms1, ms2 = root / 'ms1', root / 'ms2'
    atomic_json(root / 'fixture_manifest.json', {'synthetic': True})
    for item in publications() if items is None else items:
        paper = item['arxiv_id']
        from arxiv_pipeline.common.utils import paper_key
        key = paper_key(paper)
        refs = f'references/{key}.json'
        atomic_json(ms1 / refs, item['references'])
        _checkpoint(ms1, 'references', paper, {'references': refs},
                    status='success' if item['references'] else 'no_data', schema='1.0')
        metadata = f'metadata/{key}.json'
        atomic_json(ms1 / metadata, dict(schema_version='1.0', arxiv_id=paper,
            title=f'Synthetic publication {paper}', authors=['Synthetic Author'],
            submission_date='2023-10-01T00:00:00Z', revised_dates=[], venue=None))
        _checkpoint(ms1, 'metadata', paper, {'metadata': metadata}, schema='1.0')
        report = f'validation/{key}.json'
        atomic_json(ms1 / report, dict(schema_version='1.0', paper_id=paper,
                                     valid=True, errors=[]))
        _checkpoint(ms1, 'validate', paper, {'validation': report}, schema='1.0')
        bib = f'tables/{key}/bibtex.json'
        atomic_json(ms2 / bib, [dict(row, arxiv_id=paper) for row in item['bibtex']])
        elements, hierarchy = f'tables/{key}/elements.json', f'tables/{key}/hierarchy.json'
        atomic_json(ms2 / elements, [dict(arxiv_id=paper, element_id=f'{key}:root',
            kind='document', content='Synthetic document'), dict(arxiv_id=paper,
            element_id=f'{key}:s1', kind='sentence', content='Synthetic sentence.')])
        atomic_json(ms2 / hierarchy, [dict(arxiv_id=paper, version=1,
            element_id=f'{key}:s1', parent_id=f'{key}:root')])
        tables = dict(bibtex=bib, elements=elements, hierarchy=hierarchy)
        manifest = f'tables/{key}/parsing_manifest.json'
        atomic_json(ms2 / manifest, dict(schema_version='2.0', arxiv_id=paper,
            complete=True, input_snapshot_id='synthetic-ms1', expected_versions=[1],
            versions=[dict(version=1, status='success')], tables=tables))
        _checkpoint(ms2, 'deduplicate', paper, dict(tables, parsing_manifest=manifest))
        report = f'validation/parsing/{key}.json'
        atomic_json(ms2 / report, dict(schema_version='2.0', arxiv_id=paper,
            valid=True, errors=[], input_snapshot_id='synthetic-ms1'))
        _checkpoint(ms2, 'validate_parse', paper, {'validation': report})
        atomic_json(ms2 / f'state/parsing/{key}.json', dict(arxiv_id=paper, complete=True,
            status='success', expected_versions=[1], selected_versions=[1]))
    return dict(ms1_root=ms1, ms2_root=ms2, fixture_mode=True)


def _checkpoint(root, stage, paper, artifacts, status='success', schema='2.0'):
    from arxiv_pipeline.common.utils import paper_key
    rel = f'state/stages/{stage}/{paper_key(paper)}/paper.json'
    atomic_json(root / rel, dict(stage=stage, paper_id=paper, version=None,
        schema_version=schema, status=status, artifacts=artifacts, synthetic=True))
    if schema == '2.0':
        atomic_json(root / rel.replace('.json', '.dependencies.json'), dict(artifacts={
            p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in artifacts.values()}))


def test_loader_requires_an_implemented_boundary(tmp_path):
    from arxiv_pipeline.matching import io
    assert callable(getattr(io, 'load_publication', None)), 'T1-02 loader is missing'


def test_full_pipeline_inputs_include_five_tables_and_frozen_provenance(tmp_path):
    from arxiv_pipeline.matching.io import load_with_provenance
    ctx = ready_snapshot(tmp_path)
    before = {str(p): p.read_bytes() for p in tmp_path.rglob('*.json')}
    loaded = load_with_provenance('2310.12345', ctx, include_export_tables=True)
    assert loaded.bundle.readiness == 'ready'
    assert set(loaded.source_tables) == {'papers', 'references', 'elements', 'hierarchy', 'bibtex'}
    assert loaded.source_tables['papers'][0]['arxiv_id'] == '2310.12345'
    assert loaded.source_tables['elements'] and loaded.source_tables['hierarchy']
    assert 'ms1/metadata/2310.12345.json' in loaded.provenance['files']
    assert 'ms1/state/stages/metadata/2310.12345/paper.json' in loaded.provenance['files']
    assert 'ms2/tables/2310.12345/elements.json' in loaded.provenance['files']
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob('*.json')}


@pytest.mark.parametrize('bad', ['missing_metadata', 'foreign_metadata', 'changed_table_path'])
def test_full_pipeline_input_tables_cannot_bypass_readiness(tmp_path, bad):
    from arxiv_pipeline.matching.io import load_with_provenance
    ctx = ready_snapshot(tmp_path)
    if bad == 'missing_metadata':
        (ctx['ms1_root'] / 'state/stages/metadata/2310.12345/paper.json').unlink()
    elif bad == 'foreign_metadata':
        path = ctx['ms1_root'] / 'metadata/2310.12345.json'
        doc = json.loads(path.read_text()); doc['arxiv_id'] = '2310.12346'
        atomic_json(path, doc)
    else:
        root = ctx['ms2_root']
        path = root / 'tables/2310.12345/parsing_manifest.json'
        doc = json.loads(path.read_text()); doc['tables']['elements'] = 'tables/2310.12346/elements.json'
        atomic_json(path, doc)
        _checkpoint(root, 'deduplicate', '2310.12345', dict(
            bibtex='tables/2310.12345/bibtex.json', elements='tables/2310.12345/elements.json',
            hierarchy='tables/2310.12345/hierarchy.json', parsing_manifest='tables/2310.12345/parsing_manifest.json'))
    loaded = load_with_provenance('2310.12345', ctx, include_export_tables=True)
    assert loaded.bundle.readiness == ('not_ready' if bad == 'missing_metadata' else 'invalid')
    assert loaded.source_tables is None


def _set_versions(ctx, expected, statuses=None):
    root = ctx['ms2_root']
    paper = '2310.12345'
    path = root / f'tables/{paper}/parsing_manifest.json'
    data = json.loads(path.read_text())
    data['expected_versions'] = expected
    data['versions'] = [dict(version=v, status=s) for v, s in
                        zip(expected, statuses or ['success'] * len(expected))]
    atomic_json(path, data)
    _checkpoint(root, 'deduplicate', paper, dict(bibtex=f'tables/{paper}/bibtex.json',
                parsing_manifest=f'tables/{paper}/parsing_manifest.json'))
    atomic_json(root / f'state/parsing/{paper}.json', dict(arxiv_id=paper, complete=True,
                expected_versions=expected, selected_versions=expected))


@pytest.mark.parametrize('expected', [[2], [1, 3], [], [1, 1], [True], [0], [2, 1]])
def test_sparse_or_malformed_versions_cannot_claim_ms2_complete(tmp_path, expected):
    from arxiv_pipeline.matching.io import load_publication
    ctx = ready_snapshot(tmp_path)
    _set_versions(ctx, expected)
    bundle = load_publication('2310.12345', ctx)
    assert bundle.readiness == 'invalid'
    assert any('MS1 discovery' in i.message for i in bundle.issues)
    assert load_publication('2310.12346', ctx).readiness == 'ready'


def test_contiguous_versions_with_no_source_are_ready(tmp_path):
    from arxiv_pipeline.matching.io import load_publication
    ctx = ready_snapshot(tmp_path)
    _set_versions(ctx, [1, 2, 3], ['success', 'no_source', 'success'])
    bundle = load_publication('2310.12345', ctx)
    assert bundle.readiness == 'ready'
    assert bundle.bibtex and bundle.references


def test_ready_and_empty_candidates_preserved_without_source_writes(tmp_path):
    from arxiv_pipeline.matching.io import load_with_provenance
    ctx = ready_snapshot(tmp_path)
    before = {str(p): p.read_bytes() for p in tmp_path.rglob('*.json')}
    loaded = load_with_provenance('2310.12345v2', ctx)
    assert loaded.bundle.readiness == 'ready'
    assert loaded.bundle.arxiv_id == '2310.12345'
    assert loaded.provenance['files'] and loaded.provenance['input_fingerprint']
    empty = load_with_provenance('hep-th/9901001', ctx)
    assert empty.bundle.readiness == 'ready' and empty.bundle.references == []
    assert empty.bundle.bibtex
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob('*.json')}


@pytest.mark.parametrize('change,expected', [
    ('missing_checkpoint', 'not_ready'), ('incomplete', 'not_ready'),
    ('bad_qa', 'not_ready'), ('foreign_manifest', 'invalid'),
    ('tamper_bibtex', 'invalid'), ('unsafe_path', 'invalid'),
    ('malformed_json', 'invalid'), ('snapshot_mismatch', 'invalid'),
])
def test_readiness_and_corruption_are_not_empty_success(tmp_path, change, expected):
    from arxiv_pipeline.matching.io import load_publication
    ctx = ready_snapshot(tmp_path)
    root = ctx['ms2_root']
    path = root / 'state/parsing/2310.12345.json'
    if change == 'missing_checkpoint':
        (ctx['ms1_root'] / 'state/stages/references/2310.12345/paper.json').unlink()
    elif change == 'incomplete':
        atomic_json(path, dict(arxiv_id='2310.12345', complete=False))
    elif change == 'bad_qa':
        _checkpoint(root, 'validate_parse', '2310.12345', {}, status='data_error')
    elif change == 'foreign_manifest':
        p = root / 'tables/2310.12345/parsing_manifest.json'
        data = json.loads(p.read_text()); data['arxiv_id'] = '2310.12346'; atomic_json(p, data)
    elif change == 'tamper_bibtex':
        (root / 'tables/2310.12345/bibtex.json').write_text('[]')
    elif change == 'unsafe_path':
        atomic_json(tmp_path / 'outside.json', [])
        _checkpoint(root, 'deduplicate', '2310.12345', {'bibtex': '../outside.json'})
    elif change == 'malformed_json':
        path.write_text('{')
    elif change == 'snapshot_mismatch':
        p = root / 'validation/parsing/2310.12345.json'
        data = json.loads(p.read_text()); data['input_snapshot_id'] = 'other'; atomic_json(p, data)
    bundle = load_publication('2310.12345', ctx)
    assert bundle.readiness == expected
    assert any(i.severity == 'error' for i in bundle.issues)


def test_fixture_requires_explicit_mode(tmp_path):
    from arxiv_pipeline.matching.io import load_publication
    ctx = ready_snapshot(tmp_path); ctx['fixture_mode'] = False
    assert load_publication('2310.12345', ctx).readiness == 'invalid'


def test_successful_references_still_require_declared_artifact(tmp_path):
    from arxiv_pipeline.matching.io import load_publication
    ctx = ready_snapshot(tmp_path)
    _checkpoint(ctx['ms1_root'], 'references', '2310.12345', {}, status='success', schema='1.0')
    bundle = load_publication('2310.12345', ctx)
    assert bundle.readiness == 'invalid'
    assert any('Missing declared references artifact' in issue.message for issue in bundle.issues)


def test_no_data_without_references_artifact_preserves_queries_and_provenance(tmp_path):
    from arxiv_pipeline.matching.io import load_with_provenance
    ctx = ready_snapshot(tmp_path)
    _checkpoint(ctx['ms1_root'], 'references', '2310.12345', {}, status='no_data', schema='1.0')
    before = {str(p): p.read_bytes() for p in tmp_path.rglob('*.json')}
    loaded = load_with_provenance('2310.12345', ctx)
    assert loaded.bundle.readiness == 'ready'
    assert loaded.bundle.references == []
    assert loaded.bundle.bibtex
    # A leftover undeclared references file must not become candidate evidence.
    files = loaded.provenance['files']
    assert 'ms1/state/stages/references/2310.12345/paper.json' in files
    assert 'ms1/references/2310.12345.json' not in files
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob('*.json')}


@pytest.mark.parametrize('status', ['api_error', 'error', 'skipped'])
def test_failed_references_without_artifact_are_not_empty_success(tmp_path, status):
    from arxiv_pipeline.matching.io import load_publication
    ctx = ready_snapshot(tmp_path)
    _checkpoint(ctx['ms1_root'], 'references', '2310.12345', {}, status=status, schema='1.0')
    assert load_publication('2310.12345', ctx).readiness == 'not_ready'


@pytest.mark.parametrize('bad_qa', [False, True])
def test_no_data_fallback_keeps_ms1_and_ms2_validation_gates(tmp_path, bad_qa):
    from arxiv_pipeline.matching.io import load_publication
    ctx = ready_snapshot(tmp_path)
    _checkpoint(ctx['ms1_root'], 'references', '2310.12345', {}, status='no_data', schema='1.0')
    if bad_qa:
        p = ctx['ms1_root'] / 'validation/2310.12345.json'
        data = json.loads(p.read_text()); data.update(valid=False, errors=['incomplete MS1'])
        atomic_json(p, data)
    else:
        atomic_json(ctx['ms2_root'] / 'state/parsing/2310.12345.json', dict(arxiv_id='2310.12345', complete=False))
    assert load_publication('2310.12345', ctx).readiness == 'not_ready'


@pytest.mark.parametrize('missing', [False, True])
def test_no_data_with_declared_references_still_reads_that_artifact(tmp_path, missing):
    from arxiv_pipeline.matching.io import load_publication
    ctx = ready_snapshot(tmp_path)
    _checkpoint(ctx['ms1_root'], 'references', '2310.12345',
                {'references': 'references/2310.12345.json'}, status='no_data', schema='1.0')
    p = ctx['ms1_root'] / 'references/2310.12345.json'
    if missing:
        p.unlink()
    else:
        p.write_text('{bad JSON')
    assert load_publication('2310.12345', ctx).readiness == ('not_ready' if missing else 'invalid')


def test_loader_hashes_bibtex_text_without_decoding_it_as_json(tmp_path):
    from arxiv_pipeline.matching.io import load_publication
    ctx = ready_snapshot(tmp_path)
    root = ctx['ms2_root']
    (root / 'tables/2310.12345/references.bib').write_text('@misc{x, title={Example}}')
    _checkpoint(root, 'deduplicate', '2310.12345', dict(
        bibtex='tables/2310.12345/bibtex.json',
        parsing_manifest='tables/2310.12345/parsing_manifest.json',
        bibtex_file='tables/2310.12345/references.bib'))
    assert load_publication('2310.12345', ctx).readiness == 'ready'
