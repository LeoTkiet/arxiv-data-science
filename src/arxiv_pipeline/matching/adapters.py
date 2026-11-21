"""Thin TV1 bindings to member-owned APIs; missing APIs fail explicitly."""
from dataclasses import asdict
import importlib
from arxiv_pipeline.matching.io import digest


def _function(module, name):
    try:
        return getattr(importlib.import_module(f'arxiv_pipeline.matching.{module}'), name)
    except (ImportError, AttributeError) as exc:
        raise NotImplementedError(f'{module}.{name} has not been handed over: {exc}') from exc


def clean(ctx, output):
    normalize_bibtex = _function('cleaning', 'normalize_bibtex')
    normalize_reference = _function('cleaning', 'normalize_reference')
    return {'publications': [dict(arxiv_id=b.arxiv_id,
        bibtex=[normalize_bibtex(r) for r in b.bibtex],
        references=[normalize_reference(r) for r in b.references]) for b in ctx['bundles']]}


def label(ctx, output):
    labels = ctx['labels']
    if not ctx['config'].get('labels'):
        if not ctx['config'].get('auto_label'):
            raise NotImplementedError('Supply reviewed --labels or explicitly select --auto-label')
        auto = _function('heuristics', 'auto_label')
        policy_type = _function('heuristics', 'HeuristicPolicy')
        policy = policy_type(**ctx['config'].get('label_policy', {}))
        labels = auto(ctx['bundles'], policy)
    return {'labels': [r.to_dict() for r in labels]}


def split(ctx, output):
    from arxiv_pipeline.matching.pipeline import _read
    path = ctx['config'].get('partitions')
    if path:
        return {'partitions': _read(path)}
    create = _function('splitting', 'create_split')
    result = create(ctx['labels'], dict(population_count=ctx['config']['population_count'],
        population_snapshot_id=ctx['population_snapshot_id'],
        label_snapshot_id=digest([r.to_dict() for r in ctx['labels']])),
        seed=ctx['config']['seed'], group_size=ctx['config'].get('group_size', 5))
    return {'partitions': result.to_dict()}


def features(ctx, output):
    schema = _function('features', 'get_feature_schema')()
    batch = _function('features', 'build_feature_batch')(ctx['bundles'], schema=schema)
    return {'features': batch.to_dict(), 'feature_schema': schema.to_dict()}


def train(ctx, output):
    import uuid
    from arxiv_pipeline.matching.pipeline import StageOutput, _read, _hash
    from arxiv_pipeline.matching.model import save_artifact, load_artifact
    from arxiv_pipeline.reporting.milestone2.validation import safe_path
    compare = _function('train', 'train_and_compare')
    # Test labels are not passed into model selection, even as unused records.
    permitted = {r.arxiv_id for r in ctx['partition_manifest'].rows if r.partition in {'train', 'valid'}}
    labels = [r for r in ctx['labels'] if r.arxiv_id in permitted]
    artifact, report = compare(ctx['feature_batch'], labels, ctx['partition_manifest'],
        kinds=tuple(ctx['config'].get('model_kinds', ['logreg', 'hgb'])),
        seed=ctx['config']['seed'], neg_per_query=ctx['config'].get('neg_per_query'),
        feature_schema=ctx['feature_schema'])
    existing = safe_path(output, f'models/{artifact.model_id}')
    if existing.exists():
        try:
            checkpoint = safe_path(output, 'state/stages/train/_run/paper.json')
            inventory = _read(checkpoint).get('artifacts', {}) if checkpoint.is_file() else {}
            for name in ('model.joblib', 'metadata.json'):
                path = safe_path(output, f'models/{artifact.model_id}/{name}')
                relative = path.relative_to(output).as_posix()
                if relative in inventory and _hash(path) != inventory[relative]:
                    raise ValueError('Persisted model differs from its checkpoint')
            load_artifact(existing)
        except Exception:
            # Preserve the broken evidence; rebuild only inside this output root.
            target = safe_path(output, f'quarantine/models/{artifact.model_id}-{uuid.uuid4().hex}')
            target.parent.mkdir(parents=True, exist_ok=True)
            existing.rename(target)
    root = save_artifact(artifact, output / 'models')
    return StageOutput(dict(model_dir=root.relative_to(output).as_posix(),
        model_id=artifact.model_id, comparison=report),
        [(root / name).relative_to(output).as_posix() for name in ('model.joblib', 'metadata.json')])


def score(ctx, output):
    from arxiv_pipeline.matching.model import load_artifact, predict_scores
    from arxiv_pipeline.reporting.milestone2.validation import safe_path
    artifact = load_artifact(safe_path(output, ctx['train']['model_dir']))
    return {'scores': [asdict(r) for r in predict_scores(artifact, ctx['score_features'])]}


def rank(ctx, output):
    from arxiv_pipeline.matching.contracts import ScoreRecord
    function = _function('prediction', 'build_predictions')
    rows = function(ctx['split_bundles'], [ScoreRecord(**r) for r in ctx['score']['scores']],
                    ctx['labels'], ctx['partition_manifest'])
    return {'predictions': [asdict(r) for r in rows]}


def evaluate(ctx, output):
    from arxiv_pipeline.matching.contracts import RankedPrediction
    function = _function('evaluation', 'evaluate')
    result = function([RankedPrediction(**r) for r in ctx['rank']['predictions']],
                      ctx['labels'], ctx['partition_manifest'], partition='test')
    return result.to_dict() if hasattr(result, 'to_dict') else result


def export(ctx, output):
    # Export owns six-table schema/read-back checks; TV1 only binds the seam.
    function = _function('publish', 'export_dataset')
    return function(ctx, output)


def default_adapters():
    return dict(clean=clean, label=label, split=split, features=features,
                train=train, score=score, rank=rank, evaluate=evaluate, export=export)
