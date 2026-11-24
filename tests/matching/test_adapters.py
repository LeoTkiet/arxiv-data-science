"""Production ranking uses delivered scores and never masks provider failures."""
from dataclasses import asdict

import pytest


def ranking_context():
    from arxiv_pipeline.matching.contracts import (
        LabelRecord, PartitionManifest, PartitionRow, ScoreRecord, publication_from_rows,
    )
    from tests.fixtures.matching.examples import labels, publications
    bundles = [publication_from_rows(p['arxiv_id'], p['bibtex'], p['references'], readiness='ready')
               for p in publications()]
    manual = [b.arxiv_id for b in bundles[:2]]
    auto = [b.arxiv_id for b in bundles[2:]]
    manifest = PartitionManifest(7, 1, 'population', 'labels', manual, auto,
        [PartitionRow(b.arxiv_id, 'test', 'manual' if b.arxiv_id in manual else 'auto')
         for b in bundles])
    scores = [ScoreRecord(b.arxiv_id, q.citation_key, r.ref_arxiv_id,
                        0.9 if r.ref_arxiv_id == '2001.00003' else 0.2)
              for b in bundles for q in b.bibtex for r in b.references]
    return dict(split_bundles=bundles, labels=[LabelRecord(**r) for r in labels()],
                partition_manifest=manifest, score={'scores': [asdict(s) for s in scores]})


def test_rank_uses_tv2_ranker_when_tv5_api_is_absent(tmp_path, monkeypatch):
    from arxiv_pipeline.matching import adapters, baseline, prediction
    from arxiv_pipeline.matching.pipeline import _validate
    monkeypatch.delattr(prediction, 'build_predictions', raising=False)
    original = baseline.rank_top5_predictions
    ctx = ranking_context()
    seen = []

    def rank(bundles, scores, *, manifest, labels):
        assert bundles is ctx['split_bundles']
        assert manifest is ctx['partition_manifest']
        assert labels is ctx['labels']
        assert [asdict(s) for s in scores] == ctx['score']['scores']
        seen.append(True)
        return original(bundles, scores, manifest, labels)

    def forbidden_scoring(*args):
        pytest.fail('Ranking must not replace model scores with baseline scores')

    monkeypatch.setattr(baseline, 'rank_top5_predictions', rank)
    monkeypatch.setattr(baseline, 'predict_baseline_scores', forbidden_scoring)
    payload = adapters.rank(ctx, tmp_path)
    _validate('rank', payload, ctx)
    assert seen == [True]
    rows = {r['arxiv_id']: r for r in payload['predictions']}
    assert rows['2310.12346']['cand_1'] == '2001.00003'
    assert rows['2310.12345']['groundtruth_arxiv_id'] == '2001.00001'
    assert rows['2310.12346']['groundtruth_arxiv_id'] is None
    assert rows['2310.12347']['groundtruth_arxiv_id'] is None
    assert all(rows['hep-th/9901001'][f'cand_{i}'] is None for i in range(1, 6))
    assert payload['ranker'] == 'baseline.rank_top5_predictions'


def test_rank_prefers_delivered_tv5_api(tmp_path, monkeypatch):
    from arxiv_pipeline.matching import adapters, baseline, prediction
    original = baseline.rank_top5_predictions
    ctx = ranking_context()

    def delivered(bundles, candidates, scores, labels, manifest):
        assert candidates is None
        assert labels is ctx['labels'] and manifest is ctx['partition_manifest']
        return original(bundles, scores, manifest, labels)

    def forbidden(*args, **kwargs):
        pytest.fail('Delivered TV5 implementation must be used')

    monkeypatch.setattr(prediction, 'build_predictions', delivered, raising=False)
    monkeypatch.setattr(baseline, 'rank_top5_predictions', forbidden)
    payload = adapters.rank(ctx, tmp_path)
    assert len(payload['predictions']) == 4
    assert payload['ranker'] == 'prediction.build_predictions'


@pytest.mark.parametrize('error', [NotImplementedError, TypeError, ValueError])
def test_rank_does_not_hide_errors_from_delivered_tv5(tmp_path, monkeypatch, error):
    from arxiv_pipeline.matching import adapters, baseline, prediction

    def delivered(*args):
        raise error('delivered provider failed')

    def forbidden(*args, **kwargs):
        pytest.fail('A delivered provider failure must not trigger fallback')

    monkeypatch.setattr(prediction, 'build_predictions', delivered, raising=False)
    monkeypatch.setattr(baseline, 'rank_top5_predictions', forbidden)
    with pytest.raises(error, match='delivered provider failed'):
        adapters.rank(ranking_context(), tmp_path)


def test_rank_does_not_hide_tv5_import_failure(tmp_path, monkeypatch):
    from arxiv_pipeline.matching import adapters
    ctx = ranking_context()
    original = adapters.importlib.import_module

    def import_module(name):
        if name == 'arxiv_pipeline.matching.prediction':
            raise ImportError('TV5 dependency failed')
        return original(name)

    monkeypatch.setattr(adapters.importlib, 'import_module', import_module)
    with pytest.raises(ImportError, match='TV5 dependency failed'):
        adapters.rank(ctx, tmp_path)
