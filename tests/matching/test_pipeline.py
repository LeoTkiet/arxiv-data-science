"""T1 orchestration: use explicit synthetic adapters, never fabricate production success."""
import json

import pytest

from arxiv_pipeline.common.utils import atomic_json
from tests.matching.test_io import ready_snapshot


def setup_run(tmp_path):
    from arxiv_pipeline.matching.smoke import fixture_adapters
    config = dict(ready_snapshot(tmp_path / 'input'), output=tmp_path / 'output',
        ids=['2310.12345', '2310.12346', 'hep-th/9901001'], seed=7,
        fixture_mode=True, population_count=3)
    return config, fixture_adapters()


def test_pipeline_seam_exists():
    from arxiv_pipeline.matching import pipeline
    assert callable(getattr(pipeline, 'run_matching', None)), 'T1-04 runner is missing'


def test_mock_end_to_end_and_resume_has_origin_and_hashes(tmp_path):
    from arxiv_pipeline.matching.pipeline import run_matching
    cfg, adapters = setup_run(tmp_path)
    first = run_matching(cfg, adapters=adapters)
    assert first['complete'] and first['synthetic']
    assert all(s['status'] == 'success' for s in first['stages'])
    second = run_matching(dict(cfg, resume=True), adapters=adapters)
    assert second['complete'] and all(s['cached'] for s in second['stages'])
    assert second['run_id'] != first['run_id']
    assert all(s['origin_run_id'] == first['run_id'] for s in second['stages'])
    assert all(s['artifacts'] for s in second['stages'])


def test_resume_rebuilds_tampered_artifact_and_downstream(tmp_path):
    from arxiv_pipeline.matching.pipeline import run_matching
    cfg, adapters = setup_run(tmp_path)
    first = run_matching(cfg, adapters=adapters)
    stage = next(s for s in first['stages'] if s['stage'] == 'features')
    artifact = next(iter(stage['artifacts']))
    (cfg['output'] / artifact).write_text('{}')
    second = run_matching(dict(cfg, resume=True), adapters=adapters)
    assert second['complete']
    rebuilt = next(s for s in second['stages'] if s['stage'] == 'features')
    assert not rebuilt['cached']
    assert next(s for s in second['stages'] if s['stage'] == 'clean')['cached']


def test_label_change_invalidates_split_and_model(tmp_path):
    from arxiv_pipeline.matching.pipeline import run_matching
    cfg, adapters = setup_run(tmp_path)
    run_matching(cfg, adapters=adapters)
    cfg['label_policy'] = {'revision': 'changed'}
    second = run_matching(dict(cfg, resume=True), adapters=adapters)
    assert second['complete']
    assert not next(s for s in second['stages'] if s['stage'] == 'train')['cached']


def test_error_in_one_paper_keeps_other_publications_and_blocks_global_export(tmp_path):
    from arxiv_pipeline.matching.pipeline import run_matching
    cfg, adapters = setup_run(tmp_path)
    good = adapters['label']
    def failing(ctx, output):
        if ctx['bundles'][0].arxiv_id == '2310.12345':
            raise ValueError('synthetic failure')
        return good(ctx, output)
    adapters['label'] = failing
    result = run_matching(cfg, adapters=adapters)
    assert not result['complete']
    assert any(s['stage'] == 'label' and s['paper_id'] == '2310.12346' and s['status'] == 'success'
               for s in result['stages'])
    assert not any(s['stage'] == 'export' and s['status'] == 'success' for s in result['stages'])


def test_missing_production_module_is_not_success(tmp_path):
    from arxiv_pipeline.matching.pipeline import run_matching
    cfg, _ = setup_run(tmp_path)
    result = run_matching(cfg)
    assert not result['complete']
    assert any(s['status'] == 'not_implemented' for s in result['stages'])


def test_invalid_inputs_do_not_overwrite_sources_or_previous_success(tmp_path):
    from arxiv_pipeline.matching.pipeline import run_matching
    cfg, adapters = setup_run(tmp_path)
    run_matching(cfg, adapters=adapters)
    before = {str(p): p.read_bytes() for p in cfg['ms1_root'].rglob('*') if p.is_file()}
    broken = cfg['ms2_root'] / 'state/parsing/2310.12345.json'
    atomic_json(broken, dict(arxiv_id='2310.12345', complete=False))
    result = run_matching(dict(cfg, resume=True), adapters=adapters)
    assert not result['complete']
    assert not json.loads((cfg['output'] / 'manifests/run.json').read_text())['complete']
    assert before == {str(p): p.read_bytes() for p in cfg['ms1_root'].rglob('*') if p.is_file()}


@pytest.mark.parametrize('bad', ['same_root', 'duplicate_id', 'unsafe_output'])
def test_bad_configuration_fails_before_writing(tmp_path, bad):
    from arxiv_pipeline.matching.pipeline import run_matching
    cfg, adapters = setup_run(tmp_path)
    if bad == 'same_root': cfg['output'] = cfg['ms1_root']
    if bad == 'duplicate_id': cfg['ids'].append('2310.12345v2')
    if bad == 'unsafe_output':
        cfg['output'].mkdir()
        try:
            (cfg['output'] / 'manifests').symlink_to(cfg['ms1_root'], target_is_directory=True)
        except OSError:
            pytest.skip('Host does not allow symlinks; traversal is covered by loader tests')
    with pytest.raises((ValueError, OSError)):
        run_matching(cfg, adapters=adapters)


def test_stage_cannot_return_foreign_labels_or_invalid_features(tmp_path):
    from arxiv_pipeline.matching.pipeline import run_matching
    cfg, adapters = setup_run(tmp_path)
    def wrong(ctx, output):
        return {'labels': [dict(arxiv_id='2410.00001', citation_key='RefA',
            groundtruth_arxiv_id=None, decision='unmatched', label_source='manual',
            annotator='synthetic', review_status='accepted')]}
    adapters['label'] = wrong
    result = run_matching(cfg, adapters=adapters)
    assert not result['complete']
    assert any(s['stage'] == 'label' and s['status'] == 'data_error' for s in result['stages'])


def test_interruption_can_resume_without_claiming_old_run_complete(tmp_path):
    from arxiv_pipeline.matching.pipeline import run_matching
    cfg, adapters = setup_run(tmp_path)
    original = adapters['score']
    def interrupt(ctx, output): raise KeyboardInterrupt()
    adapters['score'] = interrupt
    with pytest.raises(KeyboardInterrupt): run_matching(cfg, adapters=adapters)
    assert not json.loads((cfg['output'] / 'manifests/run.json').read_text())['complete']
    adapters['score'] = original
    assert run_matching(dict(cfg, resume=True), adapters=adapters)['complete']


def test_input_mutation_during_stage_prevents_complete(tmp_path):
    from arxiv_pipeline.matching.pipeline import run_matching
    cfg, adapters = setup_run(tmp_path)
    good = adapters['label']
    def mutate(ctx, output):
        path = cfg['ms1_root'] / 'references/2310.12345.json'
        path.write_text('[]')
        return good(ctx, output)
    adapters['label'] = mutate
    result = run_matching(cfg, adapters=adapters)
    assert not result['complete']
    assert any('Input changed' in e for e in result['input_errors'])


def test_feature_schema_violation_fails_before_training(tmp_path):
    from arxiv_pipeline.matching.pipeline import run_matching
    cfg, adapters = setup_run(tmp_path)
    good = adapters['features']
    def invalid(ctx, output):
        payload = good(ctx, output)
        if payload['features']['rows']:
            payload['features']['rows'][0]['synthetic_title_exact'] = 10.0
        return payload
    adapters['features'] = invalid
    result = run_matching(cfg, adapters=adapters)
    assert not result['complete']
    assert any(s['stage'] == 'features' and s['status'] == 'data_error' for s in result['stages'])


def test_label_file_mutation_during_run_prevents_complete(tmp_path):
    from arxiv_pipeline.matching.pipeline import run_matching
    cfg, adapters = setup_run(tmp_path)
    path = tmp_path / 'labels.json'; atomic_json(path, [])
    cfg['labels'] = path
    good = adapters['evaluate']
    def mutate(ctx, output):
        path.write_text('[{}]')
        return good(ctx, output)
    adapters['evaluate'] = mutate
    result = run_matching(cfg, adapters=adapters)
    assert not result['complete']


def test_real_tv4_binding_hides_test_labels_and_recovers_corrupt_model(tmp_path, monkeypatch):
    pytest.importorskip('sklearn')
    from arxiv_pipeline.matching.pipeline import run_matching
    from arxiv_pipeline.matching.smoke import prepare_fixture, fixture_adapters
    from arxiv_pipeline.matching import adapters as native, train as tv4
    config = dict(prepare_fixture(tmp_path / 'fixture'), output=tmp_path / 'output', model_kinds=['logreg'])
    original = tv4.train_and_compare
    def check(batch, labels, manifest, **kwargs):
        test_ids = {r.arxiv_id for r in manifest.rows if r.partition == 'test'}
        assert all(l.arxiv_id not in test_ids for l in labels)
        return original(batch, labels, manifest, **kwargs)
    monkeypatch.setattr(tv4, 'train_and_compare', check)
    adapters = fixture_adapters(); adapters.update(train=native.train, score=native.score)
    first = run_matching(config, adapters=adapters)
    assert first['complete']
    stage = next(s for s in first['stages'] if s['stage'] == 'train')
    binary = next(p for p in stage['artifacts'] if p.endswith('model.joblib'))
    (config['output'] / binary).write_bytes(b'corrupted model')
    resumed = run_matching(dict(config, resume=True), adapters=adapters)
    assert resumed['complete']
    assert not next(s for s in resumed['stages'] if s['stage'] == 'train')['cached']
