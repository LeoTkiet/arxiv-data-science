"""Offline MS2 orchestration over an immutable MS1 snapshot.

Stages own parsing/export semantics; this module owns dependencies, artifact
integrity, checkpoints and publication completeness. No crawling client is used.
"""

from collections import Counter
import hashlib
import importlib
import inspect
import json
from pathlib import Path, PureWindowsPath
from time import perf_counter
from uuid import uuid4

from arxiv_pipeline.common.schemas import ErrorInfo, PARSING_SCHEMA_VERSION, StageResult
from arxiv_pipeline.common.utils import atomic_json, normalize_id, paper_key, utc_now

MODULES = {
    'resolve': 'arxiv_pipeline.parsing.resolver',
    'hierarchy': 'arxiv_pipeline.parsing.hierarchy',
    'normalize': 'arxiv_pipeline.parsing.normalization',
    'bibliography': 'arxiv_pipeline.parsing.bibliography',
    'deduplicate': 'arxiv_pipeline.parsing.deduplication',
    'validate_parse': 'arxiv_pipeline.reporting.milestone2.validation',
}
REQUIRED = {'resolve': {'gathered'}, 'hierarchy': {'raw_tree'},
            'normalize': {'normalized_tree'}, 'bibliography': {'raw_bibtex'},
            'deduplicate': {'elements', 'hierarchy', 'bibtex', 'parsing_manifest'},
            'validate_parse': {'validation'}}
TERMINAL = {'success', 'no_source'}
# PR #20 fingerprints (LF and CRLF). The I/O optimization leaves stage inputs,
# outputs and validation rules compatible with these checkpoints.
_COMPATIBLE_PIPELINE_REVISIONS = (
    '151dd3b5bf092e299a49d5e65c024468cce7ef90efdcfa7d16f57ea752e55a2b',
    '1706a3f196873b1c7dce8ca996501833f15aa75e99763a4866072abeb99b4a60',
)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _file_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _path(root, relative):
    if (not isinstance(relative, str) or not relative or '\\' in relative
            or Path(relative).is_absolute() or PureWindowsPath(relative).drive
            or PureWindowsPath(relative).root or '..' in Path(relative).parts):
        raise ValueError('Expected a relative path using / without parent traversal')
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise ValueError('Path escapes its root')
    return target


def validate_roots(input_root, output):
    """Validate before logging/mkdir so invalid configuration never writes MS1."""
    source, destination = Path(input_root).resolve(), Path(output).resolve()
    if not source.is_dir():
        raise ValueError('MS1 input root must be an existing snapshot directory')
    if source.is_relative_to(destination) or destination.is_relative_to(source):
        raise ValueError('MS1 input and MS2 output roots must be disjoint')
    # Root separation alone does not stop a pre-existing logs/state junction
    # redirecting a writer into MS1 (logging runs before any parser adapter).
    for relative in ('logs/pipeline.log', 'state', 'parsed', 'tables', 'validation',
                     'manifests', 'parsing_summary.json'):
        _path(destination, relative)
    return source, destination


def _read(path, *, cache=False):
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except (ValueError, OSError):
        if cache:
            return None
        raise ValueError(f'Unreadable JSON input: {path.name}')


def _discover(source, identifiers):
    checkpoint_dir = _path(source, 'state/discovery')
    paths = sorted(checkpoint_dir.glob('*.json')) if checkpoint_dir.is_dir() else []
    if not paths:
        manifest = _path(source, 'manifests/discovery.json')
        paths = [manifest] if manifest.is_file() else []
    groups, errors, excluded = {}, [], []
    selected = None
    if identifiers:
        selected = []
        for value in identifiers:
            base, version = normalize_id(value)
            if version is not None:
                raise ValueError('Parsing selectors require unversioned IDs; use --versions')
            if base not in selected:
                selected.append(base)
    for path in paths:
        try:
            data = _read(path)
            rows = data if isinstance(data, list) else [data]
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError('Discovery record must be an object')
                try:
                    value = row.get('arxiv_id') or row.get('input_id', '')
                    if not isinstance(value, str):
                        raise ValueError('Discovery identifier must be a string')
                    base, _ = normalize_id(value)
                except (ValueError, TypeError):
                    if row.get('status') == 'invalid_id' and selected is None:
                        excluded.append({'input_id': row.get('input_id'), 'status': 'invalid_id'})
                        continue
                    raise ValueError('Discovery record has no usable paper ID')
                if selected is None or base in selected:
                    groups.setdefault(base, []).append((row, path))
        except ValueError as exc:
            errors.append(dict(path=path.relative_to(source).as_posix(), message=str(exc)))
    records = []
    for base in selected if selected is not None else sorted(groups):
        candidates = groups.get(base, [])
        if not candidates:
            records.append((dict(arxiv_id=base, status='missing'), [], 'MS1 discovery record is missing'))
            continue
        projection = lambda r: dict(arxiv_id=r.get('arxiv_id'), status=r.get('status'),
                                    versions=r.get('versions'), latest=r.get('metadata', {}).get('latest_version')
                                    if isinstance(r.get('metadata'), dict) else None)
        conflict = len({_digest(projection(r)) for r, _ in candidates}) != 1
        record = candidates[0][0]
        problem = 'Conflicting discovery records for the same paper' if conflict else None
        if record.get('arxiv_id') != base:
            problem = 'Discovery arxiv_id does not match the selected publication'
            record = dict(record, arxiv_id=base)
        records.append((record, list(dict.fromkeys(p for _, p in candidates)), problem))
    if not records and not excluded:
        errors.append(dict(path='state/discovery', message='No discovery records available'))
    return records, errors, excluded


def _versions(record):
    if record.get('schema_version') != '1.0' or record.get('status') != 'success':
        raise ValueError('MS1 discovery is not successful with schema 1.0')
    if not isinstance(record.get('metadata'), dict):
        raise ValueError('MS1 discovery metadata must be an object')
    versions = record.get('versions')
    latest = (record.get('metadata') or {}).get('latest_version')
    if (type(latest) is not int or latest < 1 or not isinstance(versions, list)
            or [v.get('version') for v in versions if isinstance(v, dict)] != list(range(1, latest + 1))
            or len(versions) != latest):
        raise ValueError('MS1 discovery must contain every version from v1 to latest')
    for version in versions:
        if type(version['version']) is not int or version.get('versioned_id') != f"{record['arxiv_id']}v{version['version']}":
            raise ValueError('MS1 version identity is inconsistent')
    return versions


def _snapshot(source, record, version):
    """Hash only this version so adding vN does not invalidate unchanged v1."""
    key, number = paper_key(record['arxiv_id']), version['version']
    stages, files = {}, {}
    for stage in ('download', 'extract'):
        relative = f'state/stages/{stage}/{key}/{number}.json'
        checkpoint = _path(source, relative)
        stages[stage] = _read(checkpoint)
        if stages[stage] is not None and not isinstance(stages[stage], dict):
            raise ValueError('MS1 stage checkpoint must be an object')
        for artifact in (stages[stage] or {}).get('artifacts', {}).values():
            target = _path(source, artifact)
            if artifact not in files and target.is_file():
                files[artifact] = _file_hash(target)
    source_path = version.get('source_path')
    raw = _path(source, source_path)
    if source_path not in files:
        files[source_path] = _file_hash(raw) if raw.is_file() else None
    corpus = _path(source, version.get('corpus_path'))
    files[version['corpus_path']] = 'directory' if corpus.is_dir() else None
    directories = [corpus] if corpus.is_dir() else []
    while directories:
        for path in sorted(directories.pop().iterdir()):
            target = path.resolve()
            if not target.is_relative_to(corpus) or not target.is_relative_to(source):
                raise ValueError('Corpus contains an escaping symlink')
            if path.is_dir():
                if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
                    raise ValueError('Corpus directory links are not supported in an immutable snapshot')
                directories.append(path)
            elif path.is_file():
                relative = path.relative_to(source).as_posix()
                if relative not in files:
                    files[relative] = _file_hash(path)
    return _digest(dict(paper_id=record['arxiv_id'], version=version, stages=stages, files=files)), stages


def _implementation(adapter, module_name):
    if adapter is None:
        return _digest(dict(module=module_name, missing=True))
    filename = inspect.getsourcefile(adapter) if inspect.isfunction(adapter) or inspect.ismethod(adapter) else None
    return _digest(dict(module=getattr(adapter, '__module__', module_name),
                        name=getattr(adapter, '__qualname__', type(adapter).__qualname__),
                        source=_file_hash(Path(filename)) if filename and Path(filename).is_file() else None,
                        revision=str(getattr(adapter, '__parsing_revision__', ''))))


def _stable(source, record, versions, snapshots, discovery_hashes):
    try:
        if not all(p.is_file() and _file_hash(p) == digest for p, digest in discovery_hashes.items()):
            return False
        return all(_snapshot(source, record, v)[0] == snapshots[v['version']]
                   for v in versions if v['version'] in snapshots)
    except (ValueError, TypeError, KeyError, AttributeError, OSError):
        return False


class _Runner:
    def __init__(self, output, resume, adapters, observer):
        self.output, self.resume, self.observer = output, resume, observer
        self.adapters, self.revisions, self.import_errors = {}, {}, {}
        self.pipeline_revision = _file_hash(Path(__file__))
        for stage, name in MODULES.items():
            try:
                adapter = (adapters or {}).get(stage)
                if adapter is None:
                    adapter = importlib.import_module(name).run
                self.adapters[stage] = adapter
            except (ImportError, AttributeError) as exc:
                self.adapters[stage] = None
                self.import_errors[stage] = str(exc)
            self.revisions[stage] = _implementation(self.adapters[stage], name)

    def artifacts(self, result):
        if not isinstance(result.get('artifacts'), dict):
            raise ValueError('Adapter artifacts must be a mapping')
        inventory, payloads = {}, {}
        for name, relative in result['artifacts'].items():
            target = _path(self.output, relative)
            if not target.is_file():
                raise ValueError('MS2 artifact must be a file inside the output root')
            data = target.read_bytes()
            if result.get('stage') == 'deduplicate' and name == 'bibtex_file':
                # TV4 also exports references.bib as UTF-8 text (possibly empty).
                # Its producer owns BibTeX semantics; resume still hashes its bytes.
                data.decode('utf-8')
            else:
                payloads[name] = json.loads(data.decode('utf-8-sig'))
            inventory[relative] = hashlib.sha256(data).hexdigest()
        return inventory, payloads

    def validate(self, result, stage, record, version, upstream):
        number = version['version'] if version else None
        if not isinstance(result, dict) or (result.get('stage'), result.get('paper_id'),
                result.get('version'), result.get('schema_version')) != (
                    stage, record['arxiv_id'], number, PARSING_SCHEMA_VERSION):
            raise ValueError('Adapter returned mismatched stage/paper/version/schema')
        if result.get('version') is not None and type(result['version']) is not int:
            raise ValueError('Adapter version must be an integer, not a boolean')
        inventory, payloads = self.artifacts(result)
        if result.get('status') in TERMINAL:
            if not REQUIRED[stage].issubset(result['artifacts']):
                raise ValueError('Adapter did not produce its required artifacts')
            if stage == 'deduplicate':
                artifacts = result['artifacts']
                manifest = payloads['parsing_manifest']
                expected = [v['version'] for v in record['versions']]
                if not isinstance(manifest, dict) or (
                    manifest.get('schema_version'), manifest.get('arxiv_id'), manifest.get('input_snapshot_id'),
                    manifest.get('expected_versions'), manifest.get('complete')) != (
                        PARSING_SCHEMA_VERSION, record['arxiv_id'], upstream['context']['input_snapshot_id'], expected, True):
                    raise ValueError('Export manifest is incomplete or belongs to another snapshot/paper')
                if manifest.get('complete') is not True:
                    raise ValueError('Export manifest complete must be a boolean')
                rows = manifest.get('versions')
                if (not isinstance(rows, list) or [v.get('version') for v in rows] != expected
                        or any(v.get('status') not in TERMINAL for v in rows)):
                    raise ValueError('Export manifest does not cover every version')
                for table in ('elements', 'hierarchy', 'bibtex'):
                    if (manifest.get('tables', {}).get(table) != artifacts[table]
                            or not isinstance(payloads[table], list)):
                        raise ValueError('Export tables/manifest do not agree')
            elif stage == 'validate_parse':
                validation = payloads['validation']
                if not isinstance(validation, dict) or (
                    validation.get('schema_version'), validation.get('arxiv_id'), validation.get('valid')) != (
                        PARSING_SCHEMA_VERSION, record['arxiv_id'], True):
                    raise ValueError('Successful parsing validation must explicitly confirm valid=True')
                if validation.get('valid') is not True:
                    raise ValueError('Validation valid must be a boolean')
        return inventory

    def stage(self, stage, record, version, upstream, *, blocked=None):
        started = perf_counter()
        number, paper = version['version'] if version else None, record['arxiv_id']
        checkpoint_rel = f'state/stages/{stage}/{paper_key(paper)}/{number or "paper"}.json'
        checkpoint = _path(self.output, checkpoint_rel)
        dependencies = _path(self.output, checkpoint_rel.removesuffix('.json') + '.dependencies.json')
        material = dict(upstream)
        material['context'] = {k: v for k, v in upstream['context'].items() if k != 'input_root'}
        def fingerprint_for(revision):
            return _digest(dict(stage=stage, schema=PARSING_SCHEMA_VERSION,
                                input=material, implementation=self.revisions[stage], pipeline=revision))

        fingerprint = fingerprint_for(self.pipeline_revision)
        cached = False
        if not blocked and self.resume:
            old, meta = _read(checkpoint, cache=True), _read(dependencies, cache=True)
            if isinstance(old, dict) and isinstance(meta, dict) and old.get('status') in TERMINAL:
                try:
                    saved = meta.get('fingerprint')
                    matches = saved == fingerprint or any(
                        saved == fingerprint_for(revision) for revision in _COMPATIBLE_PIPELINE_REVISIONS)
                    if matches and meta.get('checkpoint_hash') == _digest(old):
                        inventory = self.validate(old, stage, record, version, upstream)
                        cached = meta.get('artifacts') == inventory
                except (ValueError, TypeError, KeyError, OSError, AttributeError):
                    pass
                if cached:
                    result = old
        if not cached:
            inventory = {}
            if blocked:
                result = StageResult(stage, paper, number, 'skipped', utc_now(),
                    error=ErrorInfo('upstream_unavailable', blocked), schema_version=PARSING_SCHEMA_VERSION).to_dict()
            else:
                try:
                    adapter = self.adapters[stage]
                    if adapter is None:
                        raise NotImplementedError(self.import_errors.get(stage, 'Adapter not delivered'))
                    raw = adapter(record, self.output, version=version, upstream=upstream)
                    if not isinstance(raw, StageResult):
                        raise TypeError('Adapter must return StageResult')
                    result = raw.to_dict()
                    inventory = self.validate(result, stage, record, version, upstream)
                except NotImplementedError as exc:
                    result = StageResult(stage, paper, number, 'not_implemented', utc_now(),
                        error=ErrorInfo('module_pending', str(exc)), schema_version=PARSING_SCHEMA_VERSION).to_dict()
                except Exception as exc:
                    result = StageResult(stage, paper, number, 'error', utc_now(),
                        error=ErrorInfo(type(exc).__name__, str(exc)), schema_version=PARSING_SCHEMA_VERSION).to_dict()
            atomic_json(checkpoint, result)
            atomic_json(dependencies, dict(schema_version=PARSING_SCHEMA_VERSION, fingerprint=fingerprint,
                                           checkpoint_hash=_digest(result), artifacts=inventory))
        if self.observer:
            self.observer(dict(kind='stage', stage=stage, paper_id=paper, version=number,
                               status=result['status'], cached=cached, elapsed_seconds=perf_counter() - started))
        return result


def _blocked(results):
    failed = [f"{name}={result.get('status') if isinstance(result, dict) else 'missing'}"
              for name, result in results.items() if not isinstance(result, dict) or result.get('status') not in TERMINAL]
    return ', '.join(failed) if failed else None


def _invalidate_export(output, paper, reason):
    path = _path(output, f'tables/{paper_key(paper)}/parsing_manifest.json')
    value = _read(path, cache=True)
    if isinstance(value, dict):
        value.update(complete=False, pipeline_invalidation=reason)
        atomic_json(path, value)


def _invalidate_unseen_exports(output, seen):
    """A broken discovery file must not leave an earlier final export claiming coverage."""
    tables = _path(output, 'tables')
    outcomes = []
    if not tables.is_dir():
        return outcomes
    for directory in sorted(tables.iterdir()):
        relative = f'tables/{directory.name}/parsing_manifest.json'
        manifest_path = _path(output, relative)
        manifest = _read(manifest_path, cache=True)
        if not isinstance(manifest, dict):
            continue
        paper = manifest.get('arxiv_id')
        try:
            if paper in seen or paper_key(paper) != directory.name:
                continue
        except (ValueError, TypeError, AttributeError):
            continue
        reason = 'Discovery input is unreadable; previous export is not current evidence'
        _invalidate_export(output, paper, reason)
        outcome = dict(arxiv_id=paper, status='incomplete', complete=False, artifacts={},
                       expected_versions=manifest.get('expected_versions', []), selected_versions=[],
                       expected_versions_from_previous_run=True, versions=[], reason=reason)
        atomic_json(_path(output, f'state/parsing/{paper_key(paper)}.json'), outcome)
        outcomes.append(outcome)
    return outcomes


def run(identifiers, output: Path, *, input_root: Path, resume=False, force=False,
        selected_versions=None, adapters=None, observer=None, run_id=None,
        frozen_input=False, paper_observer=None):
    """Parse selected publications from MS1; absent identifiers select all records.

    ``adapters`` is an optional mapping for offline integration tests. A missing
    production module is not_implemented, never synthesized as successful output.
    ``frozen_input`` requires an MS1 snapshot with no writer for the entire run.
    It hashes the current bytes once per version, including when resuming.
    """
    source, output = validate_roots(input_root, output)
    identifiers = list(identifiers) if identifiers is not None else []
    if resume and force:
        raise ValueError('resume and force are mutually exclusive')
    if selected_versions is not None and (not selected_versions or any(type(v) is not int or v < 1 for v in selected_versions)):
        raise ValueError('Selected versions must be positive integers')
    records, input_errors, excluded = _discover(source, identifiers)
    runner = _Runner(output, resume and not force, adapters, observer)
    output.mkdir(parents=True, exist_ok=True)
    outcomes, all_stages = [], []
    for index, (record, record_paths, conflict) in enumerate(records, 1):
        paper = record['arxiv_id']
        if record.get('status') in {'not_found', 'invalid_id'} and not conflict:
            excluded.append(dict(input_id=record.get('input_id'), arxiv_id=paper, status=record['status']))
            if paper_observer:
                paper_observer(index, len(records), paper, record['status'])
            continue
        outcome = dict(arxiv_id=paper, expected_versions=[], selected_versions=[], versions=[],
                       status='incomplete', complete=False, artifacts={}, reason=None)
        try:
            if conflict:
                raise ValueError(conflict)
            versions = _versions(record)
            expected = [v['version'] for v in versions]
            selected = expected if selected_versions is None else sorted(set(selected_versions))
            outcome.update(expected_versions=expected, selected_versions=selected)
            if not set(selected).issubset(expected):
                raise ValueError('Selected version is absent from MS1 discovery')
            discovery_hashes = {} if frozen_input else {p: _file_hash(p) for p in record_paths}
            snapshots, bundle, stable = {}, [], True
            for version in versions:
                number = version['version']
                if number not in selected:
                    outcome['versions'].append(dict(version=number, status='skipped', reason='Version not selected'))
                    continue
                try:
                    snapshot, ms1_stages = _snapshot(source, record, version)
                    snapshot_error = None
                except (ValueError, TypeError, KeyError, AttributeError, OSError) as exc:
                    snapshot, ms1_stages, snapshot_error = _digest(str(exc)), {}, str(exc)
                snapshots[number] = snapshot
                context = dict(input_root=str(source), input_snapshot_id=snapshot)
                upstream = dict(context=context, stages=ms1_stages)
                blocked = snapshot_error or ('MS1 download/extract checkpoint missing' if
                                            any(ms1_stages.get(s) is None for s in ('download', 'extract')) else None)
                resolved = runner.stage('resolve', record, version, upstream, blocked=blocked)
                hierarchy = runner.stage('hierarchy', record, version,
                    dict(context=context, stages={'resolve': resolved}), blocked=_blocked({'resolve': resolved}))
                normalized = runner.stage('normalize', record, version,
                    dict(context=context, stages={'hierarchy': hierarchy}), blocked=_blocked({'hierarchy': hierarchy}))
                bibliography = runner.stage('bibliography', record, version,
                    dict(context=context, stages={'resolve': resolved}), blocked=_blocked({'resolve': resolved}))
                all_stages.extend((resolved, hierarchy, normalized, bibliography))
                bundle.append(dict(version=number, normalize=normalized, bibliography=bibliography))
                try:
                    unchanged = not snapshot_error and (
                        frozen_input or _snapshot(source, record, version)[0] == snapshot)
                except (ValueError, TypeError, KeyError, AttributeError, OSError):
                    unchanged = False
                stable = stable and unchanged
                failed = _blocked({'normalize': normalized, 'bibliography': bibliography})
                outcome['versions'].append(dict(version=number,
                    status='data_error' if not unchanged else ('skipped' if failed else resolved['status']),
                    reason='MS1 input changed/unreadable during parsing' if not unchanged else failed))
            # Validate the entire input boundary before publishing any paper-level export.
            stable = stable and (frozen_input or _stable(source, record, versions, snapshots, discovery_hashes))
            ready = selected == expected and stable and len(bundle) == len(expected) and all(
                not _blocked({'normalize': v['normalize'], 'bibliography': v['bibliography']}) for v in bundle)
            paper_snapshot = _digest(dict(record=record, snapshots=snapshots))
            context = dict(input_root=str(source), input_snapshot_id=paper_snapshot)
            export = runner.stage('deduplicate', record, None, dict(context=context, stages={}, versions=bundle),
                                  blocked=None if ready else 'Paper has unselected, failed, missing or changed versions')
            validation = runner.stage('validate_parse', record, None,
                dict(context=context, stages={'deduplicate': export}), blocked=_blocked({'deduplicate': export}))
            all_stages.extend((export, validation))
            # A producer may change MS1 while export/validation is running too.
            ready = ready and (frozen_input or _stable(source, record, versions, snapshots, discovery_hashes))
            if ready and export['status'] in TERMINAL:
                outcome['artifacts'] = export['artifacts']
            else:
                _invalidate_export(output, paper, 'Current parse run has incomplete upstream versions or export')
            complete = ready and export['status'] in TERMINAL and validation['status'] in TERMINAL
            outcome.update(complete=complete, status=('no_source' if export['status'] == 'no_source' else 'success')
                           if complete else 'incomplete', reason=None if complete else
                           'Parsing, export or MS2 validation has not completed for every expected version')
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            outcome['reason'] = str(exc)
            _invalidate_export(output, paper, str(exc))
        outcomes.append(outcome)
        atomic_json(_path(output, f'state/parsing/{paper_key(paper)}.json'), outcome)
        if paper_observer:
            paper_observer(index, len(records), paper, outcome['status'])
    if input_errors and not identifiers:
        outcomes.extend(_invalidate_unseen_exports(output, {p['arxiv_id'] for p in outcomes}))
    summary = dict(schema_version=PARSING_SCHEMA_VERSION, mode='parse', run_id=run_id or str(uuid4()),
                   timestamp=utc_now(), frozen_input=frozen_input,
                   complete=not input_errors and all(p['complete'] for p in outcomes),
                   papers=len(outcomes), expected_versions=sum(len(p['expected_versions']) for p in outcomes),
                   stage_statuses=dict(Counter(s['status'] for s in all_stages)),
                   paper_results=outcomes, excluded_inputs=excluded, input_errors=input_errors)
    atomic_json(_path(output, 'manifests/stages.json'), all_stages)
    atomic_json(_path(output, 'manifests/parsing.json'), outcomes)
    atomic_json(_path(output, 'parsing_summary.json'), summary)
    return summary
