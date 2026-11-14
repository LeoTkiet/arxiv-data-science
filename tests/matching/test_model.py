import inspect
import math

import pytest

pytest.importorskip("sklearn")

from arxiv_pipeline.matching.contracts import ContractError, FeatureBatch, PairFeature
from arxiv_pipeline.matching.model import (
    fit_model, load_artifact, mrr_at_5, predict_scores, save_artifact,
)
from arxiv_pipeline.matching.train import (
    mock_dataset, partition_batch, targets_from_labels, train_and_compare,
)

SMALL = dict(n_manual=11, n_auto=11, queries=(4, 6), refs=(6, 10))


@pytest.fixture(scope="module")
def data():
    batch, labels, manifest = mock_dataset(seed=1, **SMALL)
    tr = partition_batch(batch, manifest, "train")
    va = partition_batch(batch, manifest, "valid")
    tr_t, va_t = targets_from_labels(tr, labels), targets_from_labels(va, labels)
    tr = FeatureBatch([r for r in tr.rows if r.key in tr_t], tr.feature_schema_id)
    va = FeatureBatch([r for r in va.rows if r.key in va_t], va.feature_schema_id)
    return batch, labels, manifest, tr, tr_t, va, va_t


def _fit(data, kind="logreg", **cfg):
    _, _, _, tr, tr_t, va, va_t = data
    grid = {"logreg": [{"C": 1.0}], "hgb": [{"max_iter": 50}],
            "rf": [{"n_estimators": 30}], "lambdamart": [{"n_estimators": 30}]}[kind]
    return fit_model(tr, tr_t, va, va_t, dict(kind=kind, seed=0, grid=grid, **cfg))


def _shift(batch, factor):
    rows = [PairFeature(r.arxiv_id, r.citation_key, r.ref_arxiv_id,
                        {k: (v * factor if type(v) is float else v) for k, v in r.values.items()})
            for r in batch.rows]
    return FeatureBatch(rows, batch.feature_schema_id)


@pytest.mark.parametrize("kind", ["logreg", "rf", "hgb", "lambdamart"])
def test_fit_predict_returns_keyed_finite_scores(data, kind):
    if kind == "lambdamart":
        pytest.importorskip("lightgbm")
    _, _, _, _, _, va, _ = data
    scores = predict_scores(_fit(data, kind), va)
    assert [s.key for s in scores] == [r.key for r in va.rows]
    assert all(math.isfinite(s.score) for s in scores)


def test_model_learns_better_than_chance(data):
    _, _, _, _, _, va, va_t = data
    assert mrr_at_5(predict_scores(_fit(data), va), va_t) > 0.6


def test_fit_model_signature_has_no_test_input():
    params = inspect.signature(fit_model).parameters
    assert list(params) == ["train_features", "train_targets", "valid_features",
                            "valid_targets", "config"]


def test_transformer_fit_on_train_only(data):
    """Changing valid values must not change any fitted transformer statistic."""
    _, _, _, tr, tr_t, va, va_t = data
    a = _fit(data)
    b = fit_model(tr, tr_t, _shift(va, 0.5), va_t, dict(kind="logreg", seed=0, grid=[{"C": 1.0}]))
    imp_a, sc_a = a.estimator[0], a.estimator[1]
    imp_b, sc_b = b.estimator[0], b.estimator[1]
    assert (imp_a.statistics_ == imp_b.statistics_).all()
    assert (sc_a.mean_ == sc_b.mean_).all() and (sc_a.scale_ == sc_b.scale_).all()


def test_features_exclude_ids_and_labels(data):
    art = _fit(data)
    forbidden = {"arxiv_id", "citation_key", "ref_arxiv_id", "label", "groundtruth_arxiv_id",
                 "label_source", "confidence", "partition"}
    assert not forbidden & set(art.feature_columns)
    with pytest.raises(ContractError):
        PairFeature("2301.00001", "k", "2001.00001", {"label_source": 1.0})


def test_scores_invariant_to_candidate_order(data):
    _, _, _, _, _, va, _ = data
    art = _fit(data, "hgb")
    fwd = {s.key: s.score for s in predict_scores(art, va)}
    rev = {s.key: s.score for s in predict_scores(art, FeatureBatch(va.rows[::-1], va.feature_schema_id))}
    assert fwd == rev


def test_negative_sampling_is_train_only_and_seeded(data):
    _, _, _, _, _, va, va_t = data
    a = _fit(data, neg_per_query=2)
    b = _fit(data, neg_per_query=2)
    assert len(predict_scores(a, va)) == len(va.rows)  # valid keeps every candidate
    assert [s.score for s in predict_scores(a, va)] == [s.score for s in predict_scores(b, va)]


def test_test_publications_never_reach_training(data):
    batch, labels, manifest, tr, _, va, _ = data
    test_pubs = {r.arxiv_id for r in manifest.rows if r.partition == "test"}
    assert test_pubs and not test_pubs & ({r.arxiv_id for r in tr.rows} | {r.arxiv_id for r in va.rows})
    seen = []
    def spy(scores, targets):
        seen.extend(s.arxiv_id for s in scores)
        return mrr_at_5(scores, targets)
    art, _ = train_and_compare(batch, labels, manifest, kinds=("logreg",), evaluator=spy)
    assert seen and not test_pubs & set(seen)
    assert art.provenance["split_id"] == manifest.split_id


def test_uncertain_and_unlabeled_queries_excluded(data):
    batch, labels, _, _, _, _, _ = data
    t = targets_from_labels(batch, labels)
    uncertain = {l.key for l in labels if l.decision == "uncertain"}
    assert uncertain and not uncertain & {k[:2] for k in t}
    for l in labels:
        if l.decision == "unmatched" and l.key in {k[:2] for k in t}:
            assert all(v == 0 for k, v in t.items() if k[:2] == l.key)
    silver_off = targets_from_labels(batch, labels, allow_sources=("manual",))
    heur = {l.key for l in labels if l.label_source == "heuristic"}
    assert not heur & {k[:2] for k in silver_off}


def test_save_load_round_trip_is_bit_identical(data, tmp_path):
    _, _, _, _, _, va, _ = data
    art = _fit(data, "hgb")
    loaded = load_artifact(save_artifact(art, tmp_path))
    assert loaded.model_id == art.model_id
    assert [s.score for s in predict_scores(loaded, va)] == [s.score for s in predict_scores(art, va)]
    assert (tmp_path / art.model_id / "metadata.json").exists()


def test_schema_mismatch_duplicates_rejected(data):
    _, _, _, _, _, va, _ = data
    art = _fit(data)
    with pytest.raises(ContractError):
        predict_scores(art, FeatureBatch(va.rows, "other-schema"))
    with pytest.raises(ContractError):
        predict_scores(art, FeatureBatch(va.rows + va.rows[:1], va.feature_schema_id))


def test_mrr_at_5_rules():
    from arxiv_pipeline.matching.contracts import ScoreRecord as S
    q = ("2301.00001", "k")
    t = {(*q, f"2001.0000{i}"): int(i == 6) for i in range(1, 7)}
    scores = [S(*q, f"2001.0000{i}", 1.0 - i / 10) for i in range(1, 7)]  # gold at rank 6
    assert mrr_at_5(scores, t) == 0.0
    tied = [S(*q, f"2001.0000{i}", 0.5) for i in range(1, 7)]
    t2 = {k: int(k[2] == "2001.00002") for k in t}
    assert mrr_at_5(tied, t2) == 0.5  # ties broken by ref_arxiv_id asc


def test_analysis_runs_on_valid_only(data):
    from arxiv_pipeline.matching.analysis import ablation, error_cases, permutation_importance, slices
    _, labels, _, tr, tr_t, va, va_t = data
    cfg = dict(kind="logreg", seed=0, grid=[{"C": 1.0}])
    art = _fit(data)
    assert ablation(tr, tr_t, va, va_t, cfg)[0]["dropped"] is None
    assert set(permutation_importance(art, va, va_t, repeats=1)) == set(art.feature_columns)
    assert all(v["n"] > 0 for v in slices(art, va, va_t, labels).values())
    errs = error_cases(art, va, va_t)
    assert errs["n_errors"] == sum(errs["tags"].values())
