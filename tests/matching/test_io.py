"""Snapshot boundary tests: readiness is evidence, never just file existence."""
import hashlib
import json

import pytest

from arxiv_pipeline.common.utils import atomic_json
from tests.fixtures.matching.examples import publications


def ready_snapshot(root):
    """A small explicit synthetic upstream handoff with real checkpoint shapes."""
    ms1, ms2 = root / 'ms1', root / 'ms2'
    atomic_json(root / 'fixture_manifest.json', {'synthetic': True})
    for item in publications():
        paper = item['arxiv_id']
        from arxiv_pipeline.common.utils import paper_key
        key = paper_key(paper)
        refs = f'references/{key}.json'
        atomic_json(ms1 / refs, item['references'])
        _checkpoint(ms1, 'references', paper, {'references': refs},
                    status='success' if item['references'] else 'no_data', schema='1.0')
        report = f'validation/{key}.json'
        atomic_json(ms1 / report, dict(schema_version='1.0', paper_id=paper,
                                     valid=True, errors=[]))
        _checkpoint(ms1, 'validate', paper, {'validation': report}, schema='1.0')
        bib = f'tables/{key}/bibtex.json'
        atomic_json(ms2 / bib, [dict(row, arxiv_id=paper) for row in item['bibtex']])
        manifest = f'tables/{key}/parsing_manifest.json'
        atomic_json(ms2 / manifest, dict(schema_version='2.0', arxiv_id=paper,
            complete=True, input_snapshot_id='synthetic-ms1', expected_versions=[1],
            versions=[dict(version=1, status='success')], tables={'bibtex': bib}))
        _checkpoint(ms2, 'deduplicate', paper, {'bibtex': bib, 'parsing_manifest': manifest})
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


@pytest.mark.parametrize('status', ['success', 'no_data'])
def test_references_require_declared_artifact_even_for_no_data(tmp_path, status):
    from arxiv_pipeline.matching.io import load_publication
    ctx = ready_snapshot(tmp_path)
    _checkpoint(ctx['ms1_root'], 'references', '2310.12345', {}, status=status, schema='1.0')
    bundle = load_publication('2310.12345', ctx)
    assert bundle.readiness == 'invalid'
    assert any('Missing declared references artifact' in issue.message for issue in bundle.issues)


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
