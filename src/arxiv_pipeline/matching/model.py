"""TV4 matching models: fit pair scorers and return keyed scores.

Contract (docs/MS3_CONTRACTS.md):
    fit_model(train_features, train_targets, valid_features, valid_targets, config) -> ModelArtifact
    predict_scores(model_artifact, feature_batch) -> list[ScoreRecord]

Anti-leakage by construction:
- fit_model has no parameter for test data; transformers (imputer/scaler) live
  inside the estimator pipeline and are fit on train rows only.
- Negative sampling touches train only; valid is scored on full candidate sets.
- X contains only the registered feature columns, never IDs/labels/provenance.
- Hyperparameters are chosen by MRR@5 on valid only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
import platform
import random
from typing import Callable

from arxiv_pipeline.matching.contracts import (
    ContractError, FeatureBatch, PairKey, ScoreRecord,
)

MODEL_KINDS = ("logreg", "rf", "hgb", "lambdamart")
DEFAULT_GRIDS = {
    "logreg": [{"C": c} for c in (0.01, 0.1, 1.0, 10.0)],
    "rf": [{"n_estimators": 300, "max_depth": d, "min_samples_leaf": m}
           for d in (None, 8) for m in (1, 5)],
    "hgb": [{"learning_rate": lr, "max_depth": d, "max_iter": 300}
            for lr in (0.05, 0.1) for d in (None, 4)],
    "lambdamart": [{"learning_rate": lr, "num_leaves": n, "n_estimators": 300}
                   for lr in (0.05, 0.1) for n in (15, 31)],
}


# --------------------------------------------------------------------------- metric
def mrr_at_5(scores: list[ScoreRecord], targets: dict[PairKey, int]) -> float:
    """Provisional MRR@5 for model selection on mock/valid data.

    TV5 owns the official evaluator; this local copy follows the same rule:
    rank by score desc, ties by ref_arxiv_id asc; reciprocal rank is 0 when the
    positive is not in the top 5. Only queries with a positive count.
    """
    by_query: dict[tuple[str, str], list[ScoreRecord]] = {}
    for s in scores:
        by_query.setdefault((s.arxiv_id, s.citation_key), []).append(s)
    positives = {k[:2]: k[2] for k, y in targets.items() if y == 1}
    if not positives:
        raise ContractError("MRR@5 needs at least one query with a positive")
    total = 0.0
    for query, gold in positives.items():
        ranked = sorted(by_query.get(query, []), key=lambda s: (-s.score, s.ref_arxiv_id))
        top = [s.ref_arxiv_id for s in ranked[:5]]
        total += 1.0 / (top.index(gold) + 1) if gold in top else 0.0
    return total / len(positives)


# ------------------------------------------------------------------- feature matrix
def feature_columns(batch: FeatureBatch) -> list[str]:
    if not batch.rows:
        raise ContractError("Cannot infer feature columns from an empty batch")
    columns = list(batch.rows[0].values)
    for row in batch.rows:
        if list(row.values) != columns:
            raise ContractError("Feature rows disagree on column order")
    return columns


def _matrix(batch: FeatureBatch, columns: list[str]):
    import numpy as np
    keys, seen = [], set()
    X = np.empty((len(batch.rows), len(columns)), dtype=np.float64)
    for i, row in enumerate(batch.rows):
        if row.key in seen:
            raise ContractError(f"Duplicate pair key {row.key}")
        seen.add(row.key)
        if set(row.values) != set(columns):
            raise ContractError("Feature columns do not match the trained model")
        keys.append(row.key)
        for j, name in enumerate(columns):
            v = row.values[name]
            X[i, j] = math.nan if v is None else float(v)
    return keys, X


def _targets_for(keys, targets: dict[PairKey, int]):
    missing = [k for k in keys if k not in targets]
    if missing:
        raise ContractError(f"{len(missing)} feature rows have no target, e.g. {missing[0]}")
    y = [targets[k] for k in keys]
    if any(v not in (0, 1) for v in y):
        raise ContractError("Targets must be 0/1")
    return y


def _sample_negatives(keys, y, per_query: int | None, seed: int):
    """Keep every positive and at most `per_query` negatives per query (train only)."""
    if per_query is None:
        return list(range(len(keys)))
    rng = random.Random(seed)
    groups: dict[tuple[str, str], list[int]] = {}
    for i, k in enumerate(keys):
        groups.setdefault(k[:2], []).append(i)
    keep = []
    for idx in groups.values():
        pos = [i for i in idx if y[i] == 1]
        neg = [i for i in idx if y[i] == 0]
        keep += pos + (neg if len(neg) <= per_query else rng.sample(neg, per_query))
    return sorted(keep)


# ------------------------------------------------------------------------ estimators
def _build(kind: str, params: dict, seed: int):
    from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    if kind == "logreg":
        return make_pipeline(SimpleImputer(strategy="median", add_indicator=True),
                             StandardScaler(),
                             LogisticRegression(class_weight="balanced", max_iter=2000,
                                                random_state=seed, **params))
    if kind == "rf":
        return make_pipeline(SimpleImputer(strategy="median", add_indicator=True),
                             RandomForestClassifier(class_weight="balanced", n_jobs=1,
                                                    random_state=seed, **params))
    if kind == "hgb":
        return HistGradientBoostingClassifier(class_weight="balanced", random_state=seed,
                                              early_stopping=False, **params)
    if kind == "lambdamart":
        from lightgbm import LGBMRanker
        return LGBMRanker(objective="lambdarank", random_state=seed, n_jobs=1,
                          deterministic=True, force_row_wise=True, verbose=-1, **params)
    raise ContractError(f"Unknown model kind {kind!r}; use one of {MODEL_KINDS}")


def _fit(estimator, kind, keys, X, y):
    if kind != "lambdamart":
        return estimator.fit(X, y)
    # LambdaMART needs contiguous query groups.
    order = sorted(range(len(keys)), key=lambda i: (keys[i][0], keys[i][1], keys[i][2]))
    sizes, last = [], None
    for i in order:
        q = keys[i][:2]
        if q != last:
            sizes.append(0)
            last = q
        sizes[-1] += 1
    return estimator.fit(X[order], [y[i] for i in order], group=sizes)


def _raw_scores(estimator, kind, X):
    if kind == "lambdamart":
        return estimator.predict(X)
    return estimator.predict_proba(X)[:, 1]


# -------------------------------------------------------------------------- artifact
@dataclass
class ModelArtifact:
    estimator: object
    kind: str
    params: dict
    feature_columns: list[str]
    feature_schema_id: str
    seed: int
    neg_per_query: int | None
    selection: list[dict] = field(default_factory=list)
    provenance: dict = field(default_factory=dict)
    versions: dict = field(default_factory=dict)

    @property
    def model_id(self) -> str:
        payload = dict(kind=self.kind, params=self.params, columns=self.feature_columns,
                       schema=self.feature_schema_id, seed=self.seed,
                       neg_per_query=self.neg_per_query, provenance=self.provenance)
        blob = json.dumps(payload, sort_keys=True, default=str).encode()
        return f"{self.kind}-{hashlib.sha256(blob).hexdigest()[:12]}"

    def metadata(self) -> dict:
        return dict(model_id=self.model_id, kind=self.kind, params=self.params,
                    feature_columns=self.feature_columns,
                    feature_schema_id=self.feature_schema_id, seed=self.seed,
                    neg_per_query=self.neg_per_query, selection=self.selection,
                    provenance=self.provenance, versions=self.versions)


def _versions() -> dict:
    import numpy, sklearn
    out = dict(python=platform.python_version(), numpy=numpy.__version__,
               sklearn=sklearn.__version__)
    try:
        import lightgbm
        out["lightgbm"] = lightgbm.__version__
    except ImportError:
        pass
    return out


# ---------------------------------------------------------------------------- API
def fit_model(train_features: FeatureBatch, train_targets: dict[PairKey, int],
              valid_features: FeatureBatch, valid_targets: dict[PairKey, int],
              config: dict) -> ModelArtifact:
    """Fit one model family, choosing hyperparameters by MRR@5 on valid only.

    config keys: kind (required), seed=0, grid=DEFAULT_GRIDS[kind],
    neg_per_query=None, provenance={} (split_id, label_snapshot_id, ...),
    evaluator=mrr_at_5 (inject TV5's evaluator: f(scores, valid_targets)->float).
    The refit-on-train+valid step is intentionally absent: the artifact is the
    exact model that was validated.
    """
    kind = config["kind"]
    seed = int(config.get("seed", 0))
    grid = config.get("grid") or DEFAULT_GRIDS[kind]
    neg = config.get("neg_per_query")
    evaluator: Callable = config.get("evaluator", mrr_at_5)
    if train_features.feature_schema_id != valid_features.feature_schema_id:
        raise ContractError("Train/valid feature schema IDs differ")
    columns = feature_columns(train_features)

    keys, X = _matrix(train_features, columns)
    y = _targets_for(keys, train_targets)
    keep = _sample_negatives(keys, y, neg, seed)
    keys_s, X_s, y_s = [keys[i] for i in keep], X[keep], [y[i] for i in keep]
    if len(set(y_s)) < 2:
        raise ContractError("Training data needs both positive and negative pairs")

    best, selection = None, []
    for params in grid:
        est = _fit(_build(kind, params, seed), kind, keys_s, X_s, y_s)
        art = ModelArtifact(est, kind, dict(params), columns, train_features.feature_schema_id,
                            seed, neg, provenance=dict(config.get("provenance", {})),
                            versions=_versions())
        score = evaluator(predict_scores(art, valid_features), valid_targets)
        selection.append(dict(params=params, valid_mrr_at_5=score))
        # Strict '>' keeps the earlier (simpler, listed first) config on ties.
        if best is None or score > best[0]:
            best = (score, art)
    best[1].selection = selection
    return best[1]


def predict_scores(model_artifact: ModelArtifact, feature_batch: FeatureBatch) -> list[ScoreRecord]:
    """Score every pair; output stays keyed by (arxiv_id, citation_key, ref_arxiv_id)."""
    if feature_batch.feature_schema_id != model_artifact.feature_schema_id:
        raise ContractError("Feature schema ID differs from the trained model")
    if not feature_batch.rows:
        return []
    keys, X = _matrix(feature_batch, model_artifact.feature_columns)
    raw = _raw_scores(model_artifact.estimator, model_artifact.kind, X)
    out = []
    for key, value in zip(keys, raw):
        value = float(value)
        if not math.isfinite(value):
            raise ContractError(f"Non-finite score for {key}")
        out.append(ScoreRecord(key[0], key[1], key[2], value))
    return out


def save_artifact(artifact: ModelArtifact, root: str | Path) -> Path:
    """Write models/<model_id>/model.joblib + metadata.json; return the directory."""
    import joblib
    directory = Path(root) / artifact.model_id
    directory.mkdir(parents=True, exist_ok=True)
    joblib.dump(artifact, directory / "model.joblib")
    (directory / "metadata.json").write_text(
        json.dumps(artifact.metadata(), indent=2, sort_keys=True, default=str), encoding="utf-8")
    return directory


def load_artifact(directory: str | Path) -> ModelArtifact:
    import joblib
    directory = Path(directory)
    artifact = joblib.load(directory / "model.joblib")
    meta = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    if meta["model_id"] != artifact.model_id or meta["feature_schema_id"] != artifact.feature_schema_id:
        raise ContractError("Model metadata does not match the serialized artifact")
    if meta["versions"].get("sklearn") != _versions()["sklearn"]:
        raise ContractError("scikit-learn version differs from the one used for training")
    return artifact
