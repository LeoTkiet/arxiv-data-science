"""Dataset-level matching orchestration; business algorithms remain with their owners.

Adapters receive typed inputs and return JSON payloads (or StageOutput for
binary/exported files). Fixture adapters are always explicit. Checkpoints bind
inputs, config, implementation and byte hashes; failures are never cached.
"""
from dataclasses import asdict, dataclass, field
import hashlib
import inspect
import json
from pathlib import Path
import subprocess
import uuid

from arxiv_pipeline.common.schemas import MATCHING_SCHEMA_VERSION
from arxiv_pipeline.common.utils import paper_key, utc_now
from arxiv_pipeline.matching.state import atomic_json
from arxiv_pipeline.matching.contracts import (
    ContractError, FeatureBatch, FeatureSchema, FeatureSpec, LabelRecord, PairFeature,
    PartitionManifest, PartitionRow, RankedPrediction, ScoreRecord, canonical_id,
    validate_features, validate_labels, validate_predictions, validate_scores,
    validate_split_quotas,
)
from arxiv_pipeline.matching.io import digest, load_with_provenance
from arxiv_pipeline.reporting.milestone2.validation import safe_path

STAGES = ('clean', 'label', 'split', 'features', 'train', 'score', 'rank', 'evaluate', 'export')
TABLES = {'papers', 'elements', 'hierarchy', 'bibtex', 'references', 'predictions'}


@dataclass
class StageOutput:
    payload: dict
    artifacts: list[str] = field(default_factory=list)


def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def _hash(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _schema(data):
    return FeatureSchema([FeatureSpec(**s) for s in data['features']], data['schema_version'])


def _batch(data):
    rows = []
    for raw in data['rows']:
        row = dict(raw)
        keys = [row.pop(k) for k in ('arxiv_id', 'citation_key', 'ref_arxiv_id')]
        rows.append(PairFeature(*keys, row))
    return FeatureBatch(rows, data['feature_schema_id'])


def _manifest(data):
    data = {k: v for k, v in data.items() if k not in {'counts', 'split_id'}}
    return PartitionManifest(**dict(data, rows=[PartitionRow(**r) for r in data['rows']]))


def _require(report):
    if not report.valid:
        raise ContractError('; '.join(sorted({i.code for i in report.errors})))


def _validate(stage, payload, ctx):
    if not isinstance(payload, dict):
        raise ContractError('Stage payload must be a JSON object')
    # Check serialization (especially NaN) before committing a successful stage.
    digest(payload)
    if stage == 'clean':
        rows = payload['publications']
        if not isinstance(rows, list) or [r['arxiv_id'] for r in rows] != [b.arxiv_id for b in ctx['bundles']]:
            raise ContractError('Cleaned publication identity/coverage mismatch')
    elif stage == 'label':
        labels = [LabelRecord(**r) for r in payload['labels']]
        _require(validate_labels(ctx['bundles'], labels))
        if any(l.label_source == 'manual' and not l.annotator for l in labels):
            raise ContractError('Manual labels require annotator provenance')
    elif stage == 'split':
        manifest = _manifest(payload['partitions'])
        available = {b.arxiv_id for b in ctx['bundles']}
        if not {r.arxiv_id for r in manifest.rows} <= available:
            raise ContractError('Split contains missing/incomplete publications')
        if manifest.label_snapshot_id != digest([l.to_dict() for l in ctx['labels']]):
            raise ContractError('Split label snapshot does not match current labels')
        if manifest.population_snapshot_id != ctx['population_snapshot_id']:
            raise ContractError('Split population snapshot does not match this run')
        if not ctx['config'].get('fixture_mode'):
            _require(validate_split_quotas(manifest, population_count=ctx['config']['population_count']))
    elif stage == 'features':
        schema, batch = _schema(payload['feature_schema']), _batch(payload['features'])
        _require(validate_features(batch, schema))
        # One feature row per full Cartesian candidate pair, including negatives.
        expected = {(b.arxiv_id, q.citation_key, r.ref_arxiv_id)
                    for b in ctx['bundles'] for q in b.bibtex for r in b.references}
        if {r.key for r in batch.rows} != expected:
            raise ContractError('Feature pair coverage/ownership mismatch')
    elif stage == 'train':
        if payload.get('synthetic') is True:
            if not ctx['config'].get('fixture_mode'):
                raise ContractError('Synthetic model forbidden in production')
        elif not isinstance(payload.get('model_dir'), str):
            raise ContractError('Training must hand over a persisted model')
    elif stage == 'score':
        _require(validate_scores(ctx['split_bundles'], [ScoreRecord(**r) for r in payload['scores']]))
    elif stage == 'rank':
        split_ids = {b.arxiv_id for b in ctx['split_bundles']}
        _require(validate_predictions(ctx['split_bundles'], [RankedPrediction(**r) for r in payload['predictions']],
                                      [l for l in ctx['labels'] if l.arxiv_id in split_ids], ctx['partition_manifest']))
    elif stage == 'evaluate':
        value = payload['mrr_at_5']
        if type(value) not in (float, int) or not 0 <= value <= 1 or type(payload['queries']) is not int or payload['queries'] <= 0:
            raise ContractError('Evaluation must report a finite MRR and nonempty labeled query set')
    elif stage == 'export':
        if set(payload['tables']) != TABLES:
            raise ContractError('Export must declare exactly six tables')
        if not ctx['config'].get('fixture_mode') and payload.get('synthetic'):
            raise ContractError('Synthetic export forbidden in production')


def _source_version():
    try:
        result = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=Path(__file__).parent,
                                capture_output=True, text=True, timeout=5)
        dirty = subprocess.run(['git', 'status', '--porcelain', '--untracked-files=normal'],
            cwd=Path(__file__).parent, capture_output=True, text=True, timeout=5)
        return result.stdout.strip() + ('-dirty' if dirty.stdout.strip() else '') if result.returncode == 0 else 'unavailable'
    except (OSError, subprocess.SubprocessError):
        return 'unavailable'


def _code(adapter):
    paths = list(Path(__file__).parent.glob('*.py'))
    paths.append(Path(__file__).parents[1] / 'common/schemas.py')
    try:
        file = inspect.getsourcefile(adapter)
        if file: paths.append(Path(file))
        body = inspect.getsource(adapter)
    except (TypeError, OSError):
        body = repr(adapter)
    return digest(dict(files={str(p.resolve()): _hash(p) for p in paths}, adapter=body))


def _json_config(config):
    return {k: str(v) if isinstance(v, Path) else v for k, v in config.items()
            if k not in {'resume', 'force', 'run_id', 'output'}}


class _Runner:
    def __init__(self, config, adapters, manifest):
        self.config, self.adapters, self.manifest = config, adapters, manifest
        self.output = Path(config['output']).resolve()
        self.rows = manifest['stages']

    def publish(self):
        atomic_json(safe_path(self.output, 'manifests/run.json'), self.manifest)

    def stage(self, stage, ctx, dependencies, paper=None):
        adapter = self.adapters.get(stage)
        key = paper_key(paper) if paper else '_run'
        checkpoint = safe_path(self.output, f'state/stages/{stage}/{key}/paper.json')
        relative = f'matching/{stage}/{key}/result.json'
        fingerprint = digest(dict(stage=stage, schema=MATCHING_SCHEMA_VERSION,
            dependencies=dependencies, config=_json_config(self.config), implementation=_code(adapter),
            bundles=[asdict(b) for b in ctx['bundles']], labels=[l.to_dict() for l in ctx['labels']]))
        old = None
        if self.config.get('resume') and not self.config.get('force'):
            try:
                old = _read(checkpoint)
                if (old['fingerprint'] != fingerprint or old['stage'] != stage
                        or old['paper_id'] != paper or old['schema_version'] != MATCHING_SCHEMA_VERSION
                        or old['status'] != 'success' or not old['artifacts']):
                    old = None
                if old and any(_hash(safe_path(self.output, p)) != h for p, h in old['artifacts'].items()):
                    old = None
                if old:
                    payload = _read(safe_path(self.output, old['payload_path']))
                    _validate(stage, payload, ctx)
            except (OSError, ValueError, KeyError, TypeError):
                old = None
        if old:
            row = dict(old, cached=True, run_id=self.manifest['run_id'])
        else:
            row = dict(schema_version=MATCHING_SCHEMA_VERSION, stage=stage, paper_id=paper,
                scope='publication' if paper else 'run', run_id=self.manifest['run_id'],
                origin_run_id=self.manifest['run_id'], source_version=self.manifest['source_version'],
                timestamp=utc_now(), fingerprint=fingerprint, artifacts={}, cached=False)
            try:
                if adapter is None:
                    raise NotImplementedError(f'{stage} has not been handed over')
                result = adapter(ctx, self.output)
                result = result if isinstance(result, StageOutput) else StageOutput(result)
                payload = result.payload
                _validate(stage, payload, ctx)
                if stage == 'train' and not payload.get('synthetic'):
                    root = safe_path(self.output, payload['model_dir'])
                    required = [(root / name).relative_to(self.output).as_posix()
                                for name in ('model.joblib', 'metadata.json')]
                    if not set(required) <= set(result.artifacts):
                        raise ContractError('Training must declare persisted model and metadata files')
                for path in result.artifacts:
                    target = safe_path(self.output, path)
                    if not target.is_file(): raise ContractError(f'Missing declared artifact: {path}')
                    row['artifacts'][path] = _hash(target)
                if stage == 'export':
                    declared = {p for paths in payload['tables'].values()
                                for p in (paths if isinstance(paths, list) else [paths])}
                    if not declared or not declared <= row['artifacts'].keys():
                        raise ContractError('Export table files must be declared and hashed')
                atomic_json(safe_path(self.output, relative), payload)
                row.update(status='success', payload_path=relative)
                row['artifacts'][relative] = _hash(safe_path(self.output, relative))
            except NotImplementedError as exc:
                row.update(status='not_implemented', error=str(exc))
            except (ContractError, ValueError, TypeError, KeyError) as exc:
                row.update(status='data_error', error=str(exc))
            except Exception as exc:
                row.update(status='error', error=f'{type(exc).__name__}: {exc}')
            atomic_json(checkpoint, row)
        self.rows.append(row)
        self.publish()
        return (payload, row) if row['status'] == 'success' else (None, row)


def run_matching(config, *, adapters=None):
    """Execute a selected dataset. Unknown/unavailable stages make it incomplete.

    Required config: ms1_root, ms2_root, output, ids. Optional labels/partitions
    are file paths. Adapters are explicit callables (context, output_root).
    Only fixture_mode permits synthetic adapters/data and reduced split quotas.
    """
    config = dict(config)
    if type(config.get('fixture_mode', False)) is not bool:
        raise ContractError('fixture_mode must be an explicit boolean')
    if config.get('resume') and config.get('force'):
        raise ContractError('resume and force are mutually exclusive')
    if type(config.get('seed', 0)) is not int or config.get('seed', 0) < 0:
        raise ContractError('seed must be a non-negative integer')
    config.setdefault('seed', 0)
    roots = [Path(config[k]).resolve() for k in ('ms1_root', 'ms2_root', 'output')]
    if any(a == b or a in b.parents or b in a.parents for a, b in ((roots[0], roots[2]), (roots[1], roots[2]))):
        raise ContractError('Output must be disjoint from both input snapshots')
    for k, root in zip(('ms1_root', 'ms2_root', 'output'), roots): config[k] = root
    ids = [canonical_id(v) for v in config['ids']]
    if not ids or len(ids) != len(set(ids)):
        raise ContractError('Select a nonempty list of unique canonical publication IDs')
    config['ids'] = sorted(ids)
    output = roots[2]
    # Check every write destination before the first mkdir/manifest update.
    for path in ('manifests/run.json', 'state/stages/probe/_run/paper.json', 'matching/probe/result.json', 'publications/probe/input_manifest.json', 'models/probe/model.joblib'):
        safe_path(output, path)
    raw_labels = _read(config['labels']) if config.get('labels') else []
    labels = [LabelRecord(**r) for r in raw_labels]
    selected = set(ids)
    if any(l.arxiv_id not in selected for l in labels):
        raise ContractError('Label file contains publications outside the selected dataset')
    config['label_snapshot_id'] = digest(raw_labels)
    if config.get('partitions'):
        config['partition_file_hash'] = _hash(config['partitions'])
    control_files = {str(config[k]): _hash(config[k]) for k in ('labels', 'partitions') if config.get(k)}
    manifest = dict(schema_version=MATCHING_SCHEMA_VERSION, run_id=config.get('run_id') or uuid.uuid4().hex,
        source_version=_source_version(), timestamp=utc_now(), complete=False, status='running',
        synthetic=bool(config.get('fixture_mode')), seed=config['seed'], config_hash=digest(_json_config(config)),
        publications=[], stages=[], input_errors=[])
    manifest['contract_hash'] = _hash(Path(__file__).parent / 'contracts.py')
    from arxiv_pipeline.matching.adapters import default_adapters
    runner = _Runner(config, default_adapters() if adapters is None else adapters, manifest)
    runner.publish()  # Invalidate a previous complete receipt before any work.
    loaded = [load_with_provenance(p, config) for p in sorted(ids)]
    for item in loaded:
        bundle = item.bundle
        manifest['publications'].append(dict(arxiv_id=bundle.arxiv_id, readiness=bundle.readiness,
            provenance=item.provenance, issues=[asdict(i) for i in bundle.issues]))
        path = f'publications/{paper_key(bundle.arxiv_id)}'
        for name, data in (('input_manifest', manifest['publications'][-1]),
                           ('queries', [asdict(b) for b in bundle.bibtex]),
                           ('references', [asdict(r) for r in bundle.references]),
                           ('issues', [asdict(i) for i in bundle.issues])):
            atomic_json(safe_path(output, f'{path}/{name}.json'), data)
    manifests = [i.provenance['input_fingerprint'] for i in loaded]
    for source in ('ms1', 'ms2'):
        manifest[f'{source}_snapshot_id'] = digest({i.bundle.arxiv_id:
            {p: h for p, h in i.provenance['files'].items() if p.startswith(source + '/')}
            for i in loaded})
    population_id = digest(dict(ids=sorted(ids), snapshots=manifests))
    ctx = dict(config=config, bundles=[i.bundle for i in loaded if i.bundle.readiness == 'ready'],
               labels=labels, population_snapshot_id=population_id)
    ready = len(ctx['bundles']) == len(ids)
    all_labels, dependencies = [], []
    # Keep processing other publications when one loader/adapter fails.
    for item in loaded:
        bundle = item.bundle
        if bundle.readiness != 'ready': continue
        local = dict(ctx, bundles=[bundle], labels=[l for l in labels if l.arxiv_id == bundle.arxiv_id])
        deps = item.provenance
        for stage in ('clean', 'label'):
            payload, row = runner.stage(stage, local, deps, bundle.arxiv_id)
            if payload is None:
                ready = False
                break
            deps = row['artifacts']
            if stage == 'label':
                all_labels.extend(LabelRecord(**r) for r in payload['labels'])
        dependencies.append(deps)
    ctx['labels'] = all_labels
    try:
        _require(validate_labels(ctx['bundles'], all_labels))
    except ContractError as exc:
        ready = False; manifest['input_errors'].append(str(exc))
    if ready:
        payload, row = runner.stage('split', ctx, dependencies)
        ready = payload is not None
        if ready:
            ctx['partition_manifest'] = _manifest(payload['partitions'])
            manifest['split_id'] = ctx['partition_manifest'].split_id
            manifest['label_snapshot_id'] = ctx['partition_manifest'].label_snapshot_id
            dependencies.append(row['artifacts'])
    if ready:
        feature_rows, schema = [], None
        feature_ok = True
        for item in loaded:
            local = dict(ctx, bundles=[item.bundle])
            payload, row = runner.stage('features', local, [item.provenance, dependencies], item.bundle.arxiv_id)
            if payload is None:
                feature_ok = False
                continue
            current = _schema(payload['feature_schema'])
            if schema is not None and schema.schema_id != current.schema_id:
                feature_ok = False; manifest['input_errors'].append('Publication feature schemas disagree')
                continue
            schema = current
            feature_rows.extend(_batch(payload['features']).rows)
            dependencies.append(row['artifacts'])
        ready = feature_ok
        if ready:
            ctx['feature_schema'] = schema
            ctx['feature_batch'] = FeatureBatch(feature_rows, schema.schema_id)
            manifest['feature_schema_id'] = schema.schema_id
            split_ids = {r.arxiv_id for r in ctx['partition_manifest'].rows}
            ctx['split_bundles'] = [b for b in ctx['bundles'] if b.arxiv_id in split_ids]
            ctx['score_features'] = FeatureBatch([r for r in feature_rows if r.arxiv_id in split_ids], schema.schema_id)
    if ready:
        for stage in ('train', 'score', 'rank', 'evaluate', 'export'):
            payload, row = runner.stage(stage, ctx, dependencies)
            if payload is None:
                ready = False
                break
            ctx[stage] = payload
            dependencies.append(row['artifacts'])
    # Input snapshots must stay frozen until the run is committed.
    for item in loaded:
        for path, expected in item.provenance['files'].items():
            source, relative = path.split('/', 1)
            try: same = _hash(safe_path(config[f'{source}_root'], relative)) == expected
            except (OSError, ValueError): same = False
            if not same:
                ready = False; manifest['input_errors'].append(f'Input changed during run: {path}')
    for path, expected in control_files.items():
        try: same = _hash(path) == expected
        except OSError: same = False
        if not same:
            ready = False; manifest['input_errors'].append(f'Input changed during run: {path}')
    manifest.update(complete=ready, status='complete' if ready else 'incomplete', finished_at=utc_now())
    runner.publish()
    return manifest
