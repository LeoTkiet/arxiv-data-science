"""Matching models: fit pair scorers and return keyed scores.

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
import os
import platform
import shutil
import subprocess
import tempfile
import random
from typing import Callable

from arxiv_pipeline.matching.contracts import (
    ContractError, FeatureBatch, FeatureSchema, PairKey, ScoreRecord, validate_features,
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
def mrr_at_5(scores: list[ScoreRecord], targets: dict[PairKey, int], *,
             queries=None) -> float:
    """Local MRR@5 following docs/MS3_CONTRACTS.md section 7 (synthetic tests).

    Reported metrics use `evaluator()` from matching.metrics. Denominator = every validly labeled query:
    the queries present in `targets` (matched or unmatched) plus `queries`, the
    labeled query keys from the registry (needed for queries with 0 candidates,
    which have no pair targets). No-hit and unmatched queries score 0 but stay
    in the denominator. Rank by score desc, ties by ref_arxiv_id asc. An empty
    query set is an error, never MRR=0.
    """
    by_query: dict[tuple[str, str], list[ScoreRecord]] = {}
    for s in scores:
        by_query.setdefault((s.arxiv_id, s.citation_key), []).append(s)
    labeled = {k[:2] for k in targets} | set(queries or ())
    if not labeled:
        raise ContractError("MRR@5 needs at least one labeled query")
    gold = {k[:2]: k[2] for k, y in targets.items() if y == 1}
    total = 0.0
    for query in sorted(labeled):
        if query not in gold:
            continue  # unmatched or no candidates: reciprocal rank 0
        ranked = sorted(by_query.get(query, []), key=lambda s: (-s.score, s.ref_arxiv_id))
        top = [s.ref_arxiv_id for s in ranked[:5]]
        total += 1.0 / (top.index(gold[query]) + 1) if gold[query] in top else 0.0
    return total / len(labeled)


def evaluator() -> Callable:
    """Official MRR@5 (matching.metrics.score_mrr_at_5): f(scores, targets, *, queries=None)."""
    from arxiv_pipeline.matching.metrics import score_mrr_at_5
    return score_mrr_at_5


# ------------------------------------------------------------------- feature matrix
@dataclass
class PairMatrix:
    """Compact pair features for real-data scale: keys + dense float64 matrix.

    Same information as a FeatureBatch (PairKey per row, feature-schema column order, schema
    ID) without one Python object per pair, so millions of pairs fit in Colab RAM.
    Missing values are NaN.
    """
    keys: list
    X: object  # numpy.ndarray, shape (len(keys), len(columns))
    columns: list[str]
    feature_schema_id: str

    def __post_init__(self):
        if getattr(self.X, "shape", None) != (len(self.keys), len(self.columns)):
            raise ContractError("PairMatrix shape does not match keys/columns")

    @classmethod
    def from_batch(cls, batch: FeatureBatch, columns: list[str]) -> "PairMatrix":
        keys, X = _matrix(batch, columns)
        return cls(keys, X, list(columns), batch.feature_schema_id)

    @classmethod
    def concat(cls, parts: list["PairMatrix"], *, columns: list[str], feature_schema_id: str):
        import numpy as np
        for p in parts:
            if p.columns != columns or p.feature_schema_id != feature_schema_id:
                raise ContractError("Cannot concatenate PairMatrix parts with different schemas")
        keys = [k for p in parts for k in p.keys]
        X = (np.vstack([p.X for p in parts]) if parts
             else np.empty((0, len(columns)), dtype=np.float64))
        return cls(keys, X, list(columns), feature_schema_id)

    def take(self, index) -> "PairMatrix":
        return PairMatrix([self.keys[i] for i in index], self.X[index], self.columns,
                          self.feature_schema_id)


def _as_matrix(features, columns: list[str]):
    """(keys, X) in `columns` order from a FeatureBatch or PairMatrix; rejects duplicate keys."""
    if isinstance(features, PairMatrix):
        if features.columns != list(columns):
            raise ContractError("Feature columns do not match the trained model")
        if len(set(features.keys)) != len(features.keys):
            raise ContractError("Duplicate pair key in PairMatrix")
        return features.keys, features.X
    return _matrix(features, columns)


def feature_columns(batch) -> list[str]:
    if isinstance(batch, PairMatrix):
        return list(batch.columns)
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
    if type(per_query) is not int or per_query < 0:
        raise ContractError("neg_per_query must be None or a non-negative int")
    rng = random.Random(seed)
    groups: dict[tuple[str, str], list[int]] = {}
    for i, k in enumerate(keys):
        groups.setdefault(k[:2], []).append(i)
    keep = []
    # Iterate queries and negatives in key order so the sample depends only on
    # (seed, data), never on the incoming row order.
    for q in sorted(groups):
        idx = sorted(groups[q], key=lambda i: keys[i])
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
                             RandomForestClassifier(**{"class_weight": "balanced", "n_jobs": 1,
                                                       "random_state": seed, **params}))
    if kind == "hgb":
        return HistGradientBoostingClassifier(class_weight="balanced", random_state=seed,
                                              early_stopping=False, **params)
    if kind == "lambdamart":
        from lightgbm import LGBMRanker
        return LGBMRanker(**{"objective": "lambdarank", "random_state": seed, "n_jobs": 1,
                             "deterministic": True, "force_row_wise": True, "verbose": -1,
                             **params})
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
    feature_schema: dict | None = None  # FeatureSchema used at train time
    source_version: str | None = None   # git commit of the training code
    data_fingerprint: str = ""          # hash of train/valid matrices, targets, grid
    code_fingerprint: str = ""          # hash of the training code + library versions

    @property
    def model_id(self) -> str:
        payload = dict(kind=self.kind, params=self.params, columns=self.feature_columns,
                       schema=self.feature_schema_id, seed=self.seed,
                       neg_per_query=self.neg_per_query, provenance=self.provenance,
                       data=self.data_fingerprint, code=self.code_fingerprint)
        blob = json.dumps(payload, sort_keys=True, default=str).encode()
        return f"{self.kind}-{hashlib.sha256(blob).hexdigest()[:12]}"

    def metadata(self) -> dict:
        return dict(model_id=self.model_id, kind=self.kind, params=self.params,
                    feature_columns=self.feature_columns,
                    feature_schema_id=self.feature_schema_id, seed=self.seed,
                    neg_per_query=self.neg_per_query, selection=self.selection,
                    provenance=self.provenance, versions=self.versions,
                    feature_schema=self.feature_schema, source_version=self.source_version,
                    data_fingerprint=self.data_fingerprint,
                    code_fingerprint=self.code_fingerprint)


def _source_version() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                             cwd=Path(__file__).resolve().parent, timeout=5)
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"],
                               capture_output=True, text=True,
                               cwd=Path(__file__).resolve().parent, timeout=5).stdout.strip()
        return out.stdout.strip() + ("-dirty" if dirty else "") if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _code_fingerprint() -> str:
    h = hashlib.sha256(Path(__file__).read_bytes())
    h.update(json.dumps(_versions(), sort_keys=True).encode())
    return h.hexdigest()


def _data_fingerprint(train_keys, X_train, y_train, valid_keys, X_valid, valid_targets,
                      valid_queries, grid) -> str:
    """Hash of exactly what fit/selection saw (row order normalized by key)."""
    h = hashlib.sha256()
    for keys, X, y in ((train_keys, X_train, y_train),
                       (valid_keys, X_valid, [valid_targets.get(k) for k in valid_keys])):
        order = sorted(range(len(keys)), key=lambda i: keys[i])
        # Streamed row by row: real runs have millions of pairs (no giant JSON string).
        for i in order:
            h.update(json.dumps([*keys[i], y[i]]).encode())  # y may be None
        h.update(b"|")
        h.update(X[order].tobytes() if order else b"")
    h.update(json.dumps(sorted(map(list, valid_queries or ()))).encode())
    h.update(json.dumps(grid, sort_keys=True, default=str).encode())
    return h.hexdigest()


def _check_schema(batch, schema: FeatureSchema, name: str):
    if isinstance(batch, PairMatrix):
        _check_matrix_schema(batch, schema, name)
        return
    report = validate_features(batch, schema)
    if not report.valid:
        codes = sorted({i.code for i in report.errors})
        raise ContractError(f"{name} features violate the feature schema: {codes}")


def _check_matrix_schema(pm: PairMatrix, schema: FeatureSchema, name: str):
    """Vectorized equivalent of validate_features for a PairMatrix."""
    import numpy as np
    if pm.feature_schema_id != schema.schema_id:
        raise ContractError(f"{name} features violate the feature schema: ['feature_schema_mismatch']")
    if pm.columns != [f.name for f in schema.features]:
        raise ContractError(f"{name} features violate the feature schema: ['feature_columns_mismatch']")
    errors = set()
    for j, spec in enumerate(schema.features):
        col = pm.X[:, j]
        missing = np.isnan(col)
        if missing.any() and not spec.nullable:
            errors.add("null_not_allowed")
        present = col[~missing]
        if spec.minimum is not None and (present < spec.minimum).any():
            errors.add("below_minimum")
        if spec.maximum is not None and (present > spec.maximum).any():
            errors.add("above_maximum")
        if spec.dtype in ("int64", "bool") and (present != np.round(present)).any():
            errors.add("dtype_mismatch")
    if errors:
        raise ContractError(f"{name} features violate the feature schema: {sorted(errors)}")


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
def fit_model(train_features, train_targets: dict[PairKey, int],
              valid_features, valid_targets: dict[PairKey, int],
              config: dict) -> ModelArtifact:
    """Fit one model family, choosing hyperparameters by MRR@5 on valid only.

    config keys: kind (required), feature_schema (FeatureSchema; required for
    real data - fixes column order and is validated + stored in the artifact;
    omitted only for synthetic mock batches), seed=0, grid=DEFAULT_GRIDS[kind],
    neg_per_query=None, provenance={} (split_id, label_snapshot_id, ...),
    evaluator=evaluator() (any f(scores, valid_targets, queries=) -> float),
    valid_queries=None (labeled valid query keys, incl. queries with 0 candidates).
    The refit-on-train+valid step is intentionally absent: the artifact is the
    exact model that was validated.
    """
    kind = config["kind"]
    seed = int(config.get("seed", 0))
    grid = config.get("grid") or DEFAULT_GRIDS[kind]
    neg = config.get("neg_per_query")
    score_fn: Callable = config.get("evaluator") or evaluator()
    if train_features.feature_schema_id != valid_features.feature_schema_id:
        raise ContractError("Train/valid feature schema IDs differ")
    schema: FeatureSchema | None = config.get("feature_schema")
    if schema is not None:
        if schema.schema_id != train_features.feature_schema_id:
            raise ContractError("Feature schema ID differs from the training batch")
        _check_schema(train_features, schema, "train")
        _check_schema(valid_features, schema, "valid")
        columns = [spec.name for spec in schema.features]  # order fixed by the schema
    else:
        columns = feature_columns(train_features)
    source_version = _source_version()
    code_fp = _code_fingerprint()

    keys, X = _as_matrix(train_features, columns)
    y = _targets_for(keys, train_targets)
    keep = _sample_negatives(keys, y, neg, seed)
    keys_s, X_s, y_s = [keys[i] for i in keep], X[keep], [y[i] for i in keep]
    if len(set(y_s)) < 2:
        raise ContractError("Training data needs both positive and negative pairs")
    v_keys, X_v = _as_matrix(valid_features, columns)
    data_fp = _data_fingerprint(keys_s, X_s, y_s, v_keys, X_v, valid_targets,
                                config.get("valid_queries"), grid)

    best, selection = None, []
    for params in grid:
        est = _fit(_build(kind, params, seed), kind, keys_s, X_s, y_s)
        art = ModelArtifact(est, kind, dict(params), columns, train_features.feature_schema_id,
                            seed, neg, provenance=dict(config.get("provenance", {})),
                            versions=_versions(),
                            feature_schema=schema.to_dict() if schema is not None else None,
                            source_version=source_version,
                            data_fingerprint=data_fp, code_fingerprint=code_fp)
        score = score_fn(predict_scores(art, valid_features), valid_targets,
                          queries=config.get("valid_queries"))
        selection.append(dict(params=params, valid_mrr_at_5=score))
        # Strict '>' keeps the earlier (simpler, listed first) config on ties.
        if best is None or score > best[0]:
            best = (score, art)
    best[1].selection = selection
    return best[1]


def predict_score_array(model_artifact: ModelArtifact, features):
    """(keys, float64 scores) for a FeatureBatch or PairMatrix; rejects non-finite scores."""
    import numpy as np
    if features.feature_schema_id != model_artifact.feature_schema_id:
        raise ContractError("Feature schema ID differs from the trained model")
    keys, X = _as_matrix(features, model_artifact.feature_columns)
    if not keys:
        return [], np.empty(0, dtype=np.float64)
    raw = np.asarray(_raw_scores(model_artifact.estimator, model_artifact.kind, X),
                     dtype=np.float64)
    bad = ~np.isfinite(raw)
    if bad.any():
        raise ContractError(f"Non-finite score for {keys[int(np.argmax(bad))]}")
    return keys, raw


def predict_scores(model_artifact: ModelArtifact, feature_batch) -> list[ScoreRecord]:
    """Score every pair; output stays keyed by (arxiv_id, citation_key, ref_arxiv_id)."""
    keys, raw = predict_score_array(model_artifact, feature_batch)
    return [ScoreRecord(k[0], k[1], k[2], float(v)) for k, v in zip(keys, raw)]


def save_artifact(artifact: ModelArtifact, root: str | Path) -> Path:
    """Atomically publish models/<model_id>/{model.joblib,metadata.json}.

    Files are written to a temp directory and renamed into place, so readers
    never see a half-written artifact. model_id is a content hash: an existing
    directory with the same id is reused after its metadata is verified.
    """
    import joblib
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    directory = root / artifact.model_id
    if directory.exists():
        # model_id covers config + data + labels + code, so a match means the
        # same model; still verify the stored fingerprints before reusing it.
        existing = load_artifact(directory)
        if not artifact.data_fingerprint or not existing.data_fingerprint:
            raise ContractError(f"Refusing to reuse {directory}: artifact has no data "
                                "fingerprint (built outside fit_model or by an older version); "
                                "retrain with fit_model or save to a fresh root")
        if (existing.data_fingerprint, existing.code_fingerprint) != \
                (artifact.data_fingerprint, artifact.code_fingerprint):
            raise ContractError(f"Refusing to reuse {directory}: fingerprints differ")
        return directory
    tmp = Path(tempfile.mkdtemp(prefix=f".{artifact.model_id}.", dir=root))
    try:
        joblib.dump(artifact, tmp / "model.joblib")
        (tmp / "metadata.json").write_text(
            json.dumps(artifact.metadata(), indent=2, sort_keys=True, default=str,
                       allow_nan=False), encoding="utf-8")
        os.replace(tmp, directory)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp)
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


# ------------------------------------------------------------------- freeze / test
FROZEN_FILE = "frozen.json"
TEST_ACCESS_LOG = "test_access.jsonl"


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def freeze(artifact_dir: str | Path, *, split_id: str, label_snapshot_id: str,
           reason: str = "") -> dict:
    """Lock the selected model before any test scoring.

    Writes <artifact_dir>/frozen.json once: model_id, split/schema/label IDs,
    code version, UTC time and the model.joblib hash. A second freeze of the
    same directory is refused, so the lock time cannot be moved after the fact.
    """
    from datetime import datetime, timezone
    from arxiv_pipeline.common.utils import atomic_json
    directory = Path(artifact_dir)
    artifact = load_artifact(directory)
    target = directory / FROZEN_FILE
    if target.exists():
        raise ContractError(f"Model already frozen: {target}")
    prov = artifact.provenance or {}
    if prov.get("split_id") not in (None, split_id):
        raise ContractError("Artifact was trained on a different split")
    record = dict(model_id=artifact.model_id, kind=artifact.kind, params=artifact.params,
                  split_id=split_id, label_snapshot_id=label_snapshot_id,
                  feature_schema_id=artifact.feature_schema_id,
                  source_version=artifact.source_version,
                  model_sha256=_file_sha256(directory / "model.joblib"),
                  selection=artifact.selection, reason=reason,
                  frozen_at=datetime.now(timezone.utc).isoformat())
    atomic_json(target, record)
    return record


def score_test(artifact_dir: str | Path, test_features, *, split_id: str):
    """The only sanctioned way to score test pairs: requires a valid freeze.

    Takes features only (never labels). Refuses when frozen.json is missing,
    the model file changed after freezing, or the split/schema differ. Every
    call is appended to test_access.jsonl so repeated test access is visible.
    Returns (keys, scores) like predict_score_array.
    """
    from datetime import datetime, timezone
    directory = Path(artifact_dir)
    lock = directory / FROZEN_FILE
    if not lock.is_file():
        raise ContractError("Refusing to score test: model is not frozen (call freeze first)")
    frozen = json.loads(lock.read_text(encoding="utf-8"))
    if _file_sha256(directory / "model.joblib") != frozen["model_sha256"]:
        raise ContractError("Refusing to score test: model.joblib changed after freezing")
    if frozen["split_id"] != split_id:
        raise ContractError("Refusing to score test: split_id differs from the frozen one")
    artifact = load_artifact(directory)
    if artifact.model_id != frozen["model_id"]:
        raise ContractError("Refusing to score test: model_id differs from the frozen one")
    keys, scores = predict_score_array(artifact, test_features)
    entry = dict(model_id=artifact.model_id, split_id=split_id, pairs=len(keys),
                 publications=len({k[0] for k in keys}),
                 at=datetime.now(timezone.utc).isoformat())
    with (directory / TEST_ACCESS_LOG).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")
    return keys, scores
