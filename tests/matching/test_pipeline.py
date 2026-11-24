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


@pytest.mark.parametrize('count', ['missing', None, True, 0, 2, '5000'])
def test_production_population_count_fails_before_loading_or_writing(tmp_path, monkeypatch, count):
    from arxiv_pipeline.matching import pipeline
    from arxiv_pipeline.matching.contracts import ContractError
    cfg, adapters = setup_run(tmp_path)
    cfg['fixture_mode'] = False
    cfg.pop('population_count')
    if count != 'missing':
        cfg['population_count'] = count

    def forbidden_load(*args):
        pytest.fail('Invalid population must be rejected before reading snapshots')

    monkeypatch.setattr(pipeline, 'load_with_provenance', forbidden_load)
    with pytest.raises(ContractError, match='population_count.*assigned population'):
        pipeline.run_matching(cfg, adapters=adapters)
    assert not cfg['output'].exists()


def test_production_preserves_assigned_count_for_a_selected_subset(tmp_path, monkeypatch):
    from arxiv_pipeline.matching import pipeline
    cfg, adapters = setup_run(tmp_path)
    cfg.update(fixture_mode=False, population_count=5000)
    original = pipeline.load_with_provenance
    seen = []

    def load(paper, config):
        seen.append(config['population_count'])
        return original(paper, config)

    monkeypatch.setattr(pipeline, 'load_with_provenance', load)
    result = pipeline.run_matching(cfg, adapters=adapters)
    assert seen == [5000] * len(cfg['ids'])
    assert cfg['population_count'] == 5000
    assert not result['complete']  # Synthetic snapshots remain forbidden.


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


def test_native_ranking_can_resume_without_tv5_prediction_api(tmp_path, monkeypatch):
    from arxiv_pipeline.matching import adapters as native, prediction
    from arxiv_pipeline.matching.pipeline import run_matching
    monkeypatch.delattr(prediction, 'build_predictions', raising=False)
    cfg, adapters = setup_run(tmp_path)
    adapters['rank'] = native.rank
    first = run_matching(cfg, adapters=adapters)
    assert first['complete']
    payload = json.loads((cfg['output'] / 'matching/rank/_run/result.json').read_text())
    assert payload['ranker'] == 'baseline.rank_top5_predictions'
    second = run_matching(dict(cfg, resume=True), adapters=adapters)
    assert second['complete'] and all(s['cached'] for s in second['stages'])


def test_pipeline_keeps_no_data_queries_without_declared_references(tmp_path):
    from arxiv_pipeline.matching.pipeline import run_matching
    from tests.matching.test_io import _checkpoint
    cfg, adapters = setup_run(tmp_path)
    paper = '2310.12345'
    _checkpoint(cfg['ms1_root'], 'references', paper, {}, status='no_data', schema='1.0')
    result = run_matching(cfg, adapters=adapters)
    assert result['complete']
    bundle = json.loads((cfg['output'] / f'publications/{paper}/queries.json').read_text())
    assert bundle
    payload = json.loads((cfg['output'] / 'matching/rank/_run/result.json').read_text())
    predictions = [p for p in payload['predictions'] if p['arxiv_id'] == paper]
    assert len(predictions) == len(bundle)
    assert all(p['groundtruth_arxiv_id'] is None for p in predictions)
    assert all(p[f'cand_{i}'] is None for p in predictions for i in range(1, 6))


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


@pytest.mark.parametrize('resume', [False, True])
def test_each_publication_schema_must_match_first_accepted_schema(tmp_path, resume):
    from arxiv_pipeline.matching.pipeline import run_matching, _schema
    cfg, adapters = setup_run(tmp_path)
    good = adapters['features']

    def differing_schema(ctx, output):
        payload = good(ctx, output)
        if ctx['bundles'][0].arxiv_id != '2310.12345':
            payload['feature_schema']['features'][0]['rationale'] = 'Different feature definition'
            payload['features']['feature_schema_id'] = _schema(payload['feature_schema']).schema_id
        return payload

    adapters['features'] = differing_schema
    if resume:
        run_matching(cfg, adapters=adapters)
    result = run_matching(dict(cfg, resume=resume), adapters=adapters)
    assert not result['complete']
    # Both later publications differ from the first, even though they agree with each other.
    assert result['input_errors'].count('Publication feature schemas disagree') == 2
    assert not any(s['stage'] in {'train', 'score', 'rank', 'evaluate', 'export'} for s in result['stages'])
    features = [s for s in result['stages'] if s['stage'] == 'features']
    assert len(features) == 3 and all(s['status'] == 'success' for s in features)
    if resume:
        assert all(s['cached'] for s in features)


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
