"""Train, compare and freeze matching models on precomputed pair features.

    python -m arxiv_pipeline.matching.runner --handoff <split_handoff.zip> \
        --features <features_dir> --output <runs_dir> [--run-id ID] [--freeze]

Inputs are the split handoff (partition manifest + train/valid labels, no test
labels) and the precomputed pair features (features_{train,valid,test_unlabeled}
.parquet + feature_schema.json). Stages: inputs -> features -> train (select on
valid) -> analysis -> [freeze + score]. Every stage writes a receipt under
`<output>/<run_id>/state/`, so rerunning the same --run-id skips finished
stages (e.g. add --freeze after reviewing the selection and the analysis).

Leakage rules (contract 3.0 section 6):
- test labels are never read: files that contain them are refused by name and
  any label row for a test publication aborts the run;
- models are fitted on train and selected by the official MRR@5 on valid only;
- test pairs are scored once, after `model.freeze`, through `model.score_test`.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import platform
import random
import uuid
import zipfile

from arxiv_pipeline.common.utils import atomic_json
from arxiv_pipeline.matching.contracts import (
    ContractError, FeatureBatch, PairFeature, canonical_id,
)

# Shared files that contain test labels; labels are read only from
# inside the handoff ZIP and refuses these names anywhere as a second safeguard.
FORBIDDEN_LABEL_FILES = ("labels_test_tv5_only.json", "labels_all_merged.json")
HANDOFF_FILES = ("partition_manifest.json", "labels_train_valid.json", "split_report.json")
PARTITIONS = ("train", "valid", "test")
DEFAULT_KINDS = ("logreg", "rf", "hgb", "lambdamart")
FEATURE_FILES = {"train": "features_train.parquet", "valid": "features_valid.parquet",
             "test": "features_test_unlabeled.parquet"}
KEY_COLUMNS = ("arxiv_id", "citation_key", "ref_arxiv_id")
LABEL_COLUMNS = {"label", "y", "groundtruth_arxiv_id", "decision", "label_source", "confidence"}
# Real-data RF grid: 300 fully grown trees on ~3.7M pairs is too slow. max_samples
# is RF's bootstrap size, not negative sampling: every tree still sees all queries.
REAL_GRIDS = {
    "rf": [{"n_estimators": 200, "max_samples": 0.2, "min_samples_leaf": leaf,
            "max_depth": depth, "n_jobs": -1}
           for depth in (None, 16) for leaf in (5, 20)],
}
# Settings that determine results; a resumed run must keep them unchanged.
RESULT_SETTINGS = ("handoff_zip", "features_dir", "kinds", "seed", "neg_per_query", "use_silver",
                   "train_eval_queries", "ablation_kind", "grids")

log = logging.getLogger("matching.runner")


@dataclass(frozen=True)
class RunConfig:
    handoff_zip: Path
    features_dir: Path
    output_root: Path
    run_id: str = ""
    kinds: tuple[str, ...] = DEFAULT_KINDS
    seed: int = 42
    neg_per_query: int | None = None
    use_silver: bool = True
    train_eval_queries: int = 5000
    ablation_kind: str = ""
    grids: dict = field(default_factory=dict)


# ----------------------------------------------------------------------------- utils
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _forbid_test_labels(path) -> None:
    if any(name in str(path) for name in FORBIDDEN_LABEL_FILES):
        raise ContractError(f"Test labels must never be read: {path}")


def _read_json(path: Path):
    _forbid_test_labels(path)
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _jsonable(value):
    return json.loads(json.dumps(value, default=str))


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]


class Run:
    """Output directory of one run: `<output_root>/<run_id>`."""

    def __init__(self, config: RunConfig):
        self.config = config
        self.run_id = config.run_id or new_run_id()
        self.root = Path(config.output_root) / self.run_id
        self.root.mkdir(parents=True, exist_ok=True)
        settings = _jsonable({k: v for k, v in asdict(config).items() if k in RESULT_SETTINGS})
        recorded = self.root / "state" / "config.json"
        if recorded.is_file():
            previous = _read_json(recorded)
            changed = sorted(k for k in RESULT_SETTINGS if previous.get(k) != settings.get(k))
            if changed:
                raise ContractError(f"RUN_ID {self.run_id} was started with other settings {changed}; "
                                    "use a new RUN_ID")
        else:
            atomic_json(recorded, settings)
        handler = logging.FileHandler(self.root / "run.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.handlers[:] = [handler, logging.StreamHandler()]
        log.setLevel(logging.INFO)

    def path(self, *parts) -> Path:
        return self.root.joinpath(*parts)

    def done(self, stage: str) -> dict | None:
        receipt = self.path("state", f"{stage}.json")
        return _read_json(receipt) if receipt.is_file() else None

    def finish(self, stage: str, **info) -> dict:
        record = dict(stage=stage, run_id=self.run_id, finished_at=_now(), **info)
        atomic_json(self.path("state", f"{stage}.json"), record)
        return record


# ---------------------------------------------------------------------- 1. inputs
def stage_inputs(run: Run) -> dict:
    """Verify the split handoff and stage partition manifest + train/valid labels."""
    if (receipt := run.done("inputs")):
        return receipt
    from arxiv_pipeline.matching.labeling import load_labels_json
    from arxiv_pipeline.matching.split import load_partition_manifest
    handoff = Path(run.config.handoff_zip)
    _forbid_test_labels(handoff)
    target = run.path("inputs")
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(handoff) as archive:
        for name in archive.namelist():
            _forbid_test_labels(name)
        manifest = json.loads(archive.read("handoff_manifest.json"))
        if manifest.get("test_labels_excluded") is not True:
            raise ContractError("Handoff does not declare test_labels_excluded=true")
        for name in HANDOFF_FILES:
            raw = archive.read(name)
            if hashlib.sha256(raw).hexdigest() != manifest["files"][name]:
                raise ContractError(f"Handoff hash mismatch: {name}")
            (target / name).write_bytes(raw)
        (target / "handoff_manifest.json").write_bytes(archive.read("handoff_manifest.json"))
    partition = load_partition_manifest(target / "partition_manifest.json")
    if partition.split_id != manifest["split_id"]:
        raise ContractError("partition_manifest split_id differs from handoff_manifest")
    labels = load_labels_json(target / "labels_train_valid.json")
    test_pubs = {r.arxiv_id for r in partition.rows if r.partition == "test"}
    leaked = sorted({l.arxiv_id for l in labels if l.arxiv_id in test_pubs})
    if leaked:
        raise ContractError(f"Train/valid labels contain test publications: {leaked[:5]}")
    counts = {p: sum(r.partition == p for r in partition.rows) for p in PARTITIONS}
    label_counts = dict(
        by_source=dict(Counter(l.label_source for l in labels)),
        by_decision=dict(Counter(l.decision for l in labels)),
        by_review_status=dict(Counter(l.review_status for l in labels)),
        supervised=sum(l.is_supervised for l in labels),
        excluded_not_supervised=sum(not l.is_supervised for l in labels))
    log.info("labels: %s", label_counts)
    log.info("Handoff OK: split_id=%s counts=%s labels=%d", partition.split_id, counts, len(labels))
    return run.finish("inputs", handoff_sha256=_sha(handoff), split_id=partition.split_id,
                      label_snapshot_id=partition.label_snapshot_id,
                      handoff_run_id=manifest.get("run_id"), handoff_code_commit=manifest.get("code_commit"),
                      partition_counts=counts, labels=len(labels), label_counts=label_counts)


def load_inputs(run: Run):
    from arxiv_pipeline.matching.labeling import load_labels_json
    from arxiv_pipeline.matching.split import load_partition_manifest
    partition = load_partition_manifest(run.path("inputs", "partition_manifest.json"))
    labels = load_labels_json(run.path("inputs", "labels_train_valid.json"))
    return partition, labels


# -------------------------------------------------------------------- 2. features
def _save_matrix(path: Path, matrix) -> None:
    import numpy as np
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".partial.npz")
    np.savez_compressed(tmp, X=matrix.X,
                        arxiv_id=np.array([k[0] for k in matrix.keys], dtype=str),
                        citation_key=np.array([k[1] for k in matrix.keys], dtype=str),
                        ref_arxiv_id=np.array([k[2] for k in matrix.keys], dtype=str))
    os.replace(tmp, path)


def _load_matrix(path: Path, columns, schema_id):
    import numpy as np
    from arxiv_pipeline.matching.model import PairMatrix
    data = np.load(path, allow_pickle=False)
    keys = list(zip(data["arxiv_id"].tolist(), data["citation_key"].tolist(),
                    data["ref_arxiv_id"].tolist()))
    return PairMatrix(keys, data["X"].reshape(len(keys), len(columns)), list(columns), schema_id)


def _find(root: Path, name: str) -> Path:
    matches = sorted(Path(root).rglob(name))
    if len(matches) != 1:
        raise FileNotFoundError(f"Expected exactly one {name} under {root}, found {len(matches)}")
    return matches[0]


def stage_features(run: Run) -> dict:
    """Import the precomputed pair features.

    Checks: the shipped feature schema equals the one in this code (same schema_id),
    columns = keys + schema features and no label column, no duplicate pair, no
    pair outside its partition, values within the schema's ranges. A split
    publication without rows is accepted only when the coverage report
    confirms it (0 references -> 0 pairs, contract section 5; its queries still
    count as 0 in MRR@5).
    """
    import numpy as np
    import pyarrow.parquet as pq
    from arxiv_pipeline.matching.contracts import FeatureSchema, FeatureSpec
    from arxiv_pipeline.matching.features import get_feature_schema
    from arxiv_pipeline.matching.model import PairMatrix, _check_matrix_schema
    schema = get_feature_schema()
    if (prior := run.done("features")) and prior.get("feature_schema_id") == schema.schema_id:
        return prior
    root = Path(run.config.features_dir)
    raw_schema = _read_json(_find(root, "feature_schema.json"))
    specs = raw_schema.get("features", raw_schema) if isinstance(raw_schema, dict) else raw_schema
    version = raw_schema.get("schema_version", schema.schema_version) if isinstance(raw_schema, dict) \
        else schema.schema_version
    shipped = FeatureSchema([FeatureSpec(**f) for f in specs], version)
    if shipped.schema_id != schema.schema_id:
        raise ContractError(f"Feature schema {shipped.schema_id[:12]} differs from code schema "
                            f"{schema.schema_id[:12]}; features were built by another features.py")
    columns = [f.name for f in schema.features]
    partition, _ = load_inputs(run)
    report_path = next(iter(sorted(root.rglob("missing_bundles_report.json"))), None)
    coverage_ok = False
    if report_path is not None:
        report = _read_json(report_path)
        if report.get("split_id") not in (None, partition.split_id):
            raise ContractError("Coverage report belongs to another split")
        coverage_ok = report.get("status") == "COMPLETE" and report.get("missing_publications") == 0
    canon: dict[str, str] = {}

    def cid(value):
        if value not in canon:
            canon[value] = canonical_id(value)
        return canon[value]

    stats, files, missing_train, zero_candidate = {}, {}, [], {}
    for part, name in FEATURE_FILES.items():
        path = _find(root, name)
        _forbid_test_labels(path)
        table = pq.read_table(path)
        names = set(table.column_names)
        if not set(KEY_COLUMNS) | set(columns) <= names:
            raise ContractError(f"{name} lacks columns {sorted(set(KEY_COLUMNS) | set(columns) - names)}")
        if names & LABEL_COLUMNS:
            raise ContractError(f"{name} contains label columns; features only are expected")
        keys = list(zip(map(cid, table.column("arxiv_id").to_pylist()),
                        table.column("citation_key").to_pylist(),
                        map(cid, table.column("ref_arxiv_id").to_pylist())))
        X = (np.column_stack([table.column(c).to_numpy(zero_copy_only=False).astype(np.float64)
                              for c in columns]) if keys else np.empty((0, len(columns))))
        matrix = PairMatrix(keys, X, columns, schema.schema_id)
        if len(set(keys)) != len(keys):
            raise ContractError(f"{name} has duplicate pair keys")
        _check_matrix_schema(matrix, schema, f"features {part}")
        expected = {r.arxiv_id for r in partition.rows if r.partition == part}
        present = {k[0] for k in keys}
        if present - expected:
            raise ContractError(f"{name} has publications outside {part}: {sorted(present - expected)[:5]}")
        absent = sorted(expected - present)
        if absent and coverage_ok:
            zero_candidate[part] = absent
        elif absent and part != "train":
            raise ContractError(f"{name} misses fixed {part} publications {absent} and the "
                                "coverage report does not confirm them (0-candidate papers)")
        else:
            missing_train += absent
        _save_matrix(run.path("features", f"{part}.npz"), matrix)
        files[part] = dict(file=str(path.relative_to(root)), sha256=_sha(path))
        stats[part] = _matrix_stats(matrix)
        log.info("features %s: %d pairs, %d publications", part, len(keys), len(present))
    atomic_json(run.path("features", "feature_schema.json"), schema.to_dict())
    return run.finish("features", feature_schema_id=schema.schema_id, stats=stats, files=files,
                      coverage_complete=coverage_ok,
                      skipped_train_publications=len(missing_train), skipped_train_ids=missing_train,
                      zero_candidate_publications={p: len(v) for p, v in zero_candidate.items()},
                      zero_candidate_ids=zero_candidate)


def load_partition_matrix(run: Run, part: str):
    from arxiv_pipeline.matching.features import get_feature_schema
    schema = get_feature_schema()
    return _load_matrix(run.path("features", f"{part}.npz"),
                        [f.name for f in schema.features], schema.schema_id)


def _matrix_stats(matrix) -> dict:
    import numpy as np
    per_query = np.array(list(Counter(k[:2] for k in matrix.keys).values()) or [0])
    return dict(publications=len({k[0] for k in matrix.keys}), pairs=len(matrix.keys),
                queries_with_candidates=int((per_query > 0).sum()),
                candidates_per_query=dict(p50=float(np.percentile(per_query, 50)),
                                          p90=float(np.percentile(per_query, 90)),
                                          p99=float(np.percentile(per_query, 99)),
                                          max=int(per_query.max())))


# -------------------------------------------------------------------- 3. train
def _labeled_view(matrix, labels, *, allow_sources):
    """Rows of `matrix` whose query has a supervised label, and their targets."""
    from arxiv_pipeline.matching.train import targets_for_keys
    targets = targets_for_keys(matrix.keys, labels, allow_sources=allow_sources)
    return matrix.take([i for i, k in enumerate(matrix.keys) if k in targets]), targets


def _training_data(run: Run, labels):
    config = run.config
    sources = ("manual", "heuristic") if config.use_silver else ("manual",)
    return _labeled_view(load_partition_matrix(run, "train"), labels, allow_sources=sources)


def _sample_queries(keys, n, seed):
    queries = sorted({k[:2] for k in keys})
    return set(queries) if len(queries) <= n else set(random.Random(seed).sample(queries, n))


def to_feature_batch(matrix, schema) -> FeatureBatch:
    """PairMatrix -> FeatureBatch (small partitions only: baseline, row-level analysis)."""
    kinds = {f.name: f.dtype for f in schema.features}
    rows = []
    for key, values in zip(matrix.keys, matrix.X.tolist()):
        row = {}
        for name, v in zip(matrix.columns, values):
            if v is None or (isinstance(v, float) and math.isnan(v)):
                row[name] = None
            elif kinds[name] == "int64":
                row[name] = int(v)
            elif kinds[name] == "bool":
                row[name] = bool(v)
            else:
                row[name] = float(v)
        rows.append(PairFeature(key[0], key[1], key[2], row))
    return FeatureBatch(rows, matrix.feature_schema_id)


def stage_train(run: Run) -> dict:
    """Fit each model family on train, choose hyperparameters + model on valid."""
    if (receipt := run.done("train")):
        return receipt
    from arxiv_pipeline.matching.baseline import predict_baseline_scores
    from arxiv_pipeline.matching.features import get_feature_schema
    from arxiv_pipeline.matching.model import (
        DEFAULT_GRIDS, evaluator, fit_model, predict_scores, save_artifact,
    )
    from arxiv_pipeline.matching.train import labeled_queries
    config = run.config
    schema = get_feature_schema()
    score_fn = evaluator()
    partition, labels = load_inputs(run)
    valid_pubs = [r.arxiv_id for r in partition.rows if r.partition == "valid"]
    train, train_t = _training_data(run, labels)
    valid, valid_t = _labeled_view(load_partition_matrix(run, "valid"), labels,
                                   allow_sources=("manual", "heuristic"))
    valid_q = labeled_queries(labels, valid_pubs)
    log.info("train: %d pairs / %d queries; valid: %d pairs / %d labeled queries",
             len(train.keys), len({k[:2] for k in train.keys}), len(valid.keys), len(valid_q))
    sample_q = _sample_queries(train.keys, config.train_eval_queries, config.seed)
    train_eval = train.take([i for i, k in enumerate(train.keys) if k[:2] in sample_q])
    train_eval_t = {k: train_t[k] for k in train_eval.keys}
    provenance = dict(split_id=partition.split_id, label_snapshot_id=partition.label_snapshot_id,
                      use_silver=config.use_silver)
    base_scores = predict_baseline_scores(to_feature_batch(valid, schema))
    results = [dict(kind="baseline", model_id=None, params=None, train_mrr_at_5=None,
                    valid_mrr_at_5=score_fn(base_scores, valid_t, queries=valid_q))]
    for kind in config.kinds:
        grid = config.grids.get(kind) or REAL_GRIDS.get(kind) or DEFAULT_GRIDS[kind]
        log.info("fit %s with %d configs", kind, len(grid))
        started = datetime.now(timezone.utc)
        art = fit_model(train, train_t, valid, valid_t,
                        dict(kind=kind, seed=config.seed, grid=grid, neg_per_query=config.neg_per_query,
                             feature_schema=schema, evaluator=score_fn, valid_queries=valid_q,
                             provenance=provenance))
        directory = save_artifact(art, run.path("models"))
        results.append(dict(kind=kind, model_id=art.model_id, params=art.params,
                            selection=art.selection, artifact=str(directory.relative_to(run.root)),
                            train_mrr_at_5=score_fn(predict_scores(art, train_eval), train_eval_t),
                            valid_mrr_at_5=max(s["valid_mrr_at_5"] for s in art.selection),
                            fit_seconds=(datetime.now(timezone.utc) - started).total_seconds()))
        atomic_json(run.path("metrics", "model_selection.json"), results)
        log.info("%s: valid MRR@5 %.4f", kind, results[-1]["valid_mrr_at_5"])
    best = max((r for r in results if r["model_id"]), key=lambda r: r["valid_mrr_at_5"])  # ties: first
    run.path("metrics", "model_selection.md").write_text(_selection_table(results, best), encoding="utf-8")
    return run.finish("train", selected=best["model_id"], selected_kind=best["kind"],
                      train_pairs=len(train.keys), train_queries=len({k[:2] for k in train.keys}),
                      valid_pairs=len(valid.keys), valid_queries=len(valid_q),
                      train_eval_queries=len(sample_q), neg_per_query=config.neg_per_query,
                      use_silver=config.use_silver)


def _selection_table(results, best) -> str:
    lines = ["| Mô hình | Tham số chọn | MRR@5 train (mẫu) | MRR@5 valid |", "|---|---|---|---|"]
    for r in results:
        mark = " **(chọn)**" if r is best else ""
        train = "—" if r["train_mrr_at_5"] is None else f"{r['train_mrr_at_5']:.4f}"
        lines.append(f"| {r['kind']}{mark} | {json.dumps(r['params']) if r['params'] else '—'} "
                     f"| {train} | {r['valid_mrr_at_5']:.4f} |")
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------- 4. analysis
def stage_analysis(run: Run) -> dict:
    """Ablation, permutation importance, slices and error cases on valid."""
    if (receipt := run.done("analysis")):
        return receipt
    from arxiv_pipeline.matching import analysis
    from arxiv_pipeline.matching.features import get_feature_schema
    from arxiv_pipeline.matching.model import evaluator, load_artifact
    from arxiv_pipeline.matching.train import labeled_queries
    config = run.config
    schema = get_feature_schema()
    partition, labels = load_inputs(run)
    selection = _read_json(run.path("metrics", "model_selection.json"))
    best_id = run.done("train")["selected"]
    best = load_artifact(run.path(next(r for r in selection if r["model_id"] == best_id)["artifact"]))
    valid_pubs = [r.arxiv_id for r in partition.rows if r.partition == "valid"]
    valid, valid_t = _labeled_view(load_partition_matrix(run, "valid"), labels,
                                   allow_sources=("manual", "heuristic"))
    valid_small = to_feature_batch(valid, schema)
    valid_q = labeled_queries(labels, valid_pubs)
    train, train_t = _training_data(run, labels)
    ab_kind = config.ablation_kind or best.kind
    ab_params = best.params if ab_kind == best.kind else None
    ab_cfg = dict(kind=ab_kind, seed=config.seed, grid=[ab_params] if ab_params else None,
                  neg_per_query=config.neg_per_query, feature_schema=schema,
                  evaluator=evaluator(), valid_queries=valid_q)
    out = dict(
        selected=best.model_id,
        ablation_kind=ab_kind,
        ablation=analysis.ablation(train, train_t, valid, valid_t, ab_cfg,
                                   base_artifact=best if ab_kind == best.kind else None),
        permutation_importance=analysis.permutation_importance(best, valid_small, valid_t,
                                                               seed=config.seed, queries=valid_q),
        slices=analysis.slices(best, valid_small, valid_t, labels),
        errors=analysis.error_cases(best, valid_small, valid_t, limit=50),
    )
    atomic_json(run.path("analysis", "error_analysis.json"), out)
    return run.finish("analysis", selected=best.model_id, ablation_kind=ab_kind)


# ------------------------------------------------- 4b. hard cases (grouped CV on train)
CV_SLICES = ("no_title_signal", "no_author_signal", "bib_year_missing", "other")


def query_slices(matrix) -> dict:
    """Slice per query from its candidates' features only (never from the answer).

    no_title_signal: every candidate has title cosine 0 (empty/missing BibTeX title);
    no_author_signal: no candidate shares any author; bib_year_missing: BibTeX year
    absent. A query can be in several slices; "other" has none of them.
    """
    import numpy as np
    cols = {c: i for i, c in enumerate(matrix.columns)}
    title = matrix.X[:, cols["title_char_ngram_cosine"]]
    author = np.maximum(matrix.X[:, cols["author_jaccard_last"]], matrix.X[:, cols["first_author_match"]])
    year_missing = matrix.X[:, cols["is_bib_year_missing"]]
    best: dict = {}
    for key, t, a, y in zip(matrix.keys, title.tolist(), author.tolist(), year_missing.tolist()):
        q = best.setdefault(key[:2], [0.0, 0.0, 0.0])
        q[0], q[1], q[2] = max(q[0], t), max(q[1], a), max(q[2], y)
    out = {}
    for q, (t, a, y) in best.items():
        tags = [n for n, hit in (("no_title_signal", t == 0), ("no_author_signal", a == 0),
                                 ("bib_year_missing", y == 1)) if hit]
        out[q] = tags or ["other"]
    return out


def _fold_of(pubs, folds, seed):
    order = sorted(pubs)
    random.Random(seed).shuffle(order)
    return {p: i % folds for i, p in enumerate(order)}


def stage_cv(run: Run, *, folds: int = 3, eval_queries: int = 10000) -> dict:
    """Compare models with the baseline on held-out train publications, by hard-case slice.

    Valid is saturated, so this grouped K-fold over train publications tests
    whether learning helps where the title heuristic has nothing to use. Each
    model uses the configuration selected on valid (no extra tuning); test is
    never touched. Per fold, held-out queries are all manual ones plus a seeded
    sample of `eval_queries` heuristic ones. Reported per slice and label source:
    MRR@5 over labeled queries (official) and over matched queries only.
    """
    if (receipt := run.done("cv")):
        return receipt
    from arxiv_pipeline.matching.baseline import score_pair_heuristics
    from arxiv_pipeline.matching.features import get_feature_schema
    from arxiv_pipeline.matching.model import evaluator, fit_model, predict_scores
    from arxiv_pipeline.matching.contracts import ScoreRecord
    config = run.config
    schema = get_feature_schema()
    score_fn = evaluator()
    _, labels = load_inputs(run)
    source = {l.key: l.label_source for l in labels if l.is_supervised}
    selection = [r for r in _read_json(run.path("metrics", "model_selection.json")) if r["model_id"]]
    train, train_t = _training_data(run, labels)
    fold = _fold_of({k[0] for k in train.keys}, folds, config.seed)
    slices = query_slices(train)
    rows = []
    for f in range(folds):
        fit_idx = [i for i, k in enumerate(train.keys) if fold[k[0]] != f]
        held = [i for i, k in enumerate(train.keys) if fold[k[0]] == f]
        held_q = sorted({train.keys[i][:2] for i in held})
        manual_q = [q for q in held_q if source.get(q) == "manual"]
        auto_q = [q for q in held_q if source.get(q) != "manual"]
        rng = random.Random(config.seed + f)
        chosen = set(manual_q) | set(auto_q if len(auto_q) <= eval_queries else rng.sample(auto_q, eval_queries))
        ev = train.take([i for i in held if train.keys[i][:2] in chosen])
        ev_t = {k: train_t[k] for k in ev.keys}
        fit = train.take(fit_idx)
        fit_t = {k: train_t[k] for k in fit.keys}
        named = [("baseline", [ScoreRecord(k[0], k[1], k[2], score_pair_heuristics(dict(zip(ev.columns, v))))
                               for k, v in zip(ev.keys, ev.X.tolist())])]
        for r in selection:
            log.info("cv fold %d/%d: fit %s %s", f + 1, folds, r["kind"], r["params"])
            art = fit_model(fit, fit_t, ev, ev_t, dict(kind=r["kind"], seed=config.seed, grid=[r["params"]],
                                                       neg_per_query=config.neg_per_query,
                                                       feature_schema=schema, evaluator=score_fn))
            named.append((r["kind"], predict_scores(art, ev)))
        del fit, fit_t
        for name, scores in named:
            by_q: dict = {}
            for s in scores:
                by_q.setdefault((s.arxiv_id, s.citation_key), []).append(s)
            for group in ("all", "manual", "heuristic"):
                for sl in ("all", *CV_SLICES):
                    qs = {q for q in chosen if (group == "all" or (source.get(q) == "manual") == (group == "manual"))
                          and (sl == "all" or sl in slices[q])}
                    matched = {k[:2] for k, y in ev_t.items() if y == 1 and k[:2] in qs}
                    if not qs:
                        continue
                    sub = [s for q in qs for s in by_q.get(q, [])]
                    t_all = {k: y for k, y in ev_t.items() if k[:2] in qs}
                    t_m = {k: y for k, y in ev_t.items() if k[:2] in matched}
                    rows.append(dict(fold=f, model=name, labels=group, slice=sl, queries=len(qs),
                                     matched=len(matched), mrr_at_5=score_fn(sub, t_all),
                                     mrr_matched_only=(score_fn([s for s in sub if (s.arxiv_id, s.citation_key)
                                                                 in matched], t_m) if matched else None)))
        atomic_json(run.path("analysis", "cv_hard_cases.json"), rows)
    summary = _cv_summary(rows)
    run.path("analysis", "cv_hard_cases.md").write_text(summary, encoding="utf-8")
    return run.finish("cv", folds=folds, eval_queries=eval_queries, rows=len(rows))


def _cv_summary(rows) -> str:
    """Mean over folds of MRR@5 on matched queries, per slice (labels=all), with counts."""
    from statistics import mean
    models = list(dict.fromkeys(r["model"] for r in rows))
    lines = ["| Nhóm query | Query (có đáp án) | " + " | ".join(models) + " |",
             "|---|---|" + "---|" * len(models)]
    for group in ("all", "manual", "heuristic"):
        for sl in ("all", *CV_SLICES):
            cells, counts = [], None
            for m in models:
                sel = [r for r in rows if r["model"] == m and r["labels"] == group and r["slice"] == sl]
                vals = [r["mrr_matched_only"] for r in sel if r["mrr_matched_only"] is not None]
                cells.append(f"{mean(vals):.4f}" if vals else "—")
                if counts is None and sel:
                    counts = f"{sum(r['queries'] for r in sel)} ({sum(r['matched'] for r in sel)})"
            if counts:
                lines.append(f"| {group} / {sl} | {counts} | " + " | ".join(cells) + " |")
    return ("MRR@5 chỉ trên query có đáp án, trung bình các fold (train, chia theo publication):\n\n"
            + "\n".join(lines) + "\n")


# ----------------------------------------------------------- 5. freeze + test
def _write_scores(path: Path, keys, scores) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(dict(arxiv_id=[k[0] for k in keys], citation_key=[k[1] for k in keys],
                          ref_arxiv_id=[k[2] for k in keys], score=list(map(float, scores))))
    tmp = path.with_name(path.name + ".partial")
    pq.write_table(table, tmp)
    os.replace(tmp, path)


def stage_freeze_and_score(run: Run) -> dict:
    """Freeze the selected model, then score train/valid/test.

    Train/valid scores come from the frozen model too (the predictions table
    needs every BibTeX entry of every split publication). Test goes through
    `score_test`, which refuses an unfrozen or modified model and logs access.
    """
    if (receipt := run.done("freeze")):
        return receipt
    from arxiv_pipeline.matching.model import freeze, load_artifact, predict_score_array, score_test
    partition, _ = load_inputs(run)
    selection = _read_json(run.path("metrics", "model_selection.json"))
    best_id = run.done("train")["selected"]
    directory = run.path(next(r for r in selection if r["model_id"] == best_id)["artifact"])
    frozen_path = directory / "frozen.json"
    frozen = _read_json(frozen_path) if frozen_path.is_file() else freeze(
        directory, split_id=partition.split_id, label_snapshot_id=partition.label_snapshot_id,
        reason="Selected by valid MRR@5 (official evaluator) in stage_train")
    artifact = load_artifact(directory)
    counts = {}
    for part in ("train", "valid"):
        keys, scores = predict_score_array(artifact, load_partition_matrix(run, part))
        _write_scores(run.path("scores", part, "scores.parquet"), keys, scores)
        counts[part] = len(keys)
    keys, scores = score_test(directory, load_partition_matrix(run, "test"), split_id=partition.split_id)
    _write_scores(run.path("scores", "test", "scores.parquet"), keys, scores)
    counts["test"] = len(keys)
    atomic_json(run.path("frozen.json"), frozen)
    return run.finish("freeze", model_id=frozen["model_id"], frozen_at=frozen["frozen_at"],
                      scored_pairs=counts)


# --------------------------------------------------------------------- 6. manifest
def write_run_manifest(run: Run) -> Path:
    from arxiv_pipeline.matching.model import _source_version, _versions
    manifest = dict(schema_version="3.0", task="MS3-matching-models", run_id=run.run_id, created_at=_now(),
                    source_version=_source_version(), python=platform.python_version(),
                    versions=_versions(), config=_jsonable(asdict(run.config)),
                    stages={s: run.done(s) for s in ("inputs", "features", "train", "analysis", "cv", "freeze")},
                    test_labels_read=False)
    atomic_json(run.path("run_manifest.json"), manifest)
    return run.path("run_manifest.json")


def run_all(config: RunConfig, *, freeze: bool = False, cv: bool = False) -> Run:
    run = Run(config)
    stage_inputs(run)
    stage_features(run)
    stage_train(run)
    stage_analysis(run)
    if cv:
        stage_cv(run)
    if freeze:
        stage_freeze_and_score(run)
    write_run_manifest(run)
    return run


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Train, compare and freeze matching models")
    ap.add_argument("--handoff", required=True, type=Path, help="split handoff ZIP (manifest + train/valid labels)")
    ap.add_argument("--features", required=True, type=Path, help="folder with features_*.parquet")
    ap.add_argument("--output", required=True, type=Path)
    ap.add_argument("--run-id", default="")
    ap.add_argument("--kinds", default=",".join(DEFAULT_KINDS))
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--neg-per-query", type=int, default=0)
    ap.add_argument("--cv", action="store_true",
                    help="compare models vs baseline by hard-case slice with grouped CV on train")
    ap.add_argument("--freeze", action="store_true",
                    help="freeze the selected model and score test (only after reviewing selection and analysis)")
    args = ap.parse_args(argv)
    config = RunConfig(handoff_zip=args.handoff, features_dir=args.features,
                       output_root=args.output, run_id=args.run_id or new_run_id(),
                       kinds=tuple(k.strip() for k in args.kinds.split(",") if k.strip()),
                       seed=args.seed, neg_per_query=args.neg_per_query or None)
    run = run_all(config, freeze=args.freeze, cv=args.cv)
    print("RUN_ID:", run.run_id)
    print(run.path("metrics", "model_selection.md").read_text(encoding="utf-8"))
    print("Kết quả:", run.root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
