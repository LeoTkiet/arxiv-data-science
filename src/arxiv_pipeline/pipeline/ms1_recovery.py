"""Local selective MS1 recovery. Inventory is read-only; mutations require a verified copy.

No implicit network calls, status fabrication or original-data writes. The plan is
bound to assignment, source bytes and implementation. Re-inventory after an
interruption; committed stage checkpoints are inspected before scheduling again.
"""
from collections import Counter
import hashlib
import importlib
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory

from arxiv_pipeline.common.schemas import StageResult, STAGE_STATUSES
from arxiv_pipeline.common.utils import atomic_json, normalize_id, paper_key, utc_now
from arxiv_pipeline.crawling.metadata import build_metadata
from arxiv_pipeline.export.validation import run as validate
from arxiv_pipeline.main import expand_range
from arxiv_pipeline.reporting.milestone2.validation import safe_path

MARKER = '.recovery-origin.json'
ORDER = ('discovery', 'download', 'extract', 'metadata', 'references', 'validate')
NETWORK = {'discovery', 'download', 'references'}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                   allow_nan=False).encode('utf-8')).hexdigest()


def _hash(path):
    h = hashlib.sha256()
    before = path.stat()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError(f'Input changed while reading: {path.name}')
    return h.hexdigest()


def _snapshot(root):
    if not root.is_dir():
        raise ValueError('Data root must be an existing directory')
    result = {}
    for p in sorted(root.rglob('*')):
        relative = p.relative_to(root).as_posix()
        path = safe_path(root, relative)
        if relative != MARKER and path.is_file():
            result[relative] = _hash(path)
    return result


def _read(root, relative):
    path = safe_path(root, relative)
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return None


def _ids(assignment):
    result = []
    for row in assignment['ranges']:
        values = expand_range(row['range'])
        if 'count' in row and row['count'] != len(values):
            raise ValueError('Assignment count differs from range')
        result.extend(values)
    if not result or len(result) != len(set(result)):
        raise ValueError('Assignment must have nonempty, nonoverlapping ranges')
    return sorted(result)


def _records(root, selected):
    records, diagnostics = {}, []
    for path in sorted(safe_path(root, 'state/discovery').glob('*.json')):
        row = _read(root, path.relative_to(root).as_posix())
        if not isinstance(row, dict):
            diagnostics.append(f'Unreadable discovery: {path.name}')
            continue
        try:
            paper, version = normalize_id(row.get('input_id') or row.get('arxiv_id'))
            if version is not None or paper not in selected:
                continue
        except (ValueError, TypeError, AttributeError):
            diagnostics.append(f'Invalid discovery identity: {path.name}')
            continue
        old = records.get(paper)
        if old is None or str(row.get('timestamp', '')) > str(old.get('timestamp', '')):
            records[paper] = row
        elif row.get('timestamp') == old.get('timestamp') and row != old:
            diagnostics.append(f'Conflicting discovery checkpoints: {paper}')
    manifest = _read(root, 'manifests/discovery.json')
    if isinstance(manifest, list):
        for row in manifest:
            if not isinstance(row, dict):
                continue
            paper = row.get('input_id') or row.get('arxiv_id')
            if paper in selected:
                records.setdefault(paper, row)
    return records, diagnostics


def _checkpoint(root, stage, paper, version=None):
    row = _read(root, f'state/stages/{stage}/{paper_key(paper)}/{version or "paper"}.json')
    if not isinstance(row, dict) or (row.get('schema_version'), row.get('stage'),
            row.get('paper_id'), row.get('version')) != ('1.0', stage, paper, version):
        return None
    artifacts = row.get('artifacts')
    if (not isinstance(artifacts, dict) or not isinstance(row.get('status'), str)
            or row['status'] not in STAGE_STATUSES):
        return None
    try:
        for relative in artifacts.values():
            safe_path(root, relative)
    except ValueError:
        return None
    return row


def _download(root, paper, version):
    row = _checkpoint(root, 'download', paper, version['version'])
    if not row:
        return 'repair', 'Missing or invalid download checkpoint'
    error = row.get('error') if isinstance(row.get('error'), dict) else {}
    if row['status'] not in {'success', 'no_source'}:
        if error.get('http_status') == 404 or error.get('retryable') is False:
            return 'review', 'Nonretryable download error requires source/version review'
        return 'repair', 'Download has not succeeded'
    artifacts = row['artifacts']
    meta_path = artifacts.get('download_metadata')
    meta = _read(root, meta_path) if meta_path else None
    if not isinstance(meta, dict) or (meta.get('arxiv_id'), meta.get('version')) != (paper, version['version']):
        return 'repair', 'Missing/mismatched download provenance'
    if row['status'] == 'no_source':
        valid = (meta.get('status') == 'no_source' and meta.get('source_format') == 'pdf'
                 and 'source' not in artifacts)
        return ('keep', 'Verified PDF-only outcome') if valid else ('review', 'Unverified empty-source outcome')
    source = artifacts.get('source')
    if source != version.get('source_path'):
        return 'repair', 'Source path differs from discovery'
    path = safe_path(root, source)
    if not path.is_file() or path.stat().st_size == 0:
        return 'repair', 'Raw source is missing or empty'
    if meta.get('status') != 'success' or meta.get('raw_size_bytes') != path.stat().st_size or meta.get('sha256') != _hash(path):
        return 'repair', 'Raw source size/SHA-256 differs from provenance'
    return 'keep', 'Verified source bytes and provenance'


def _corpus(root, paper, version, download, extract, errors):
    """Authenticate current corpus, or compare legacy corpus against its raw archive."""
    from arxiv_pipeline.crawling.extractor import extract_source, ExtractionDataError, ExtractionSecurityError
    if not extract or extract['status'] not in {'success','no_source'}:
        return 'repair', 'Missing/failed extraction checkpoint'
    corpus = safe_path(root, version['corpus_path'])
    source = safe_path(root, version['source_path'])
    actual = {p.relative_to(corpus).as_posix(): _hash(safe_path(root,p.relative_to(root).as_posix()))
              for p in corpus.rglob('*') if p.is_file() and p.name != 'extract_metadata.json'}
    meta = _read(root, f'{version["corpus_path"]}/extract_metadata.json')
    raw_meta = _read(root, download['artifacts']['download_metadata'])
    if (extract['status']=='success' and extract['artifacts'].get('corpus') == version['corpus_path']
            and isinstance(meta,dict)
            and meta.get('source_sha256') == raw_meta.get('sha256')
            and isinstance(meta.get('corpus_sha256'),dict)):
        expected = meta['corpus_sha256']
        if expected != actual:
            return 'repair', 'Corpus bytes/inventory differ from extraction hashes'
    else:
        # Legacy sidecars lack per-file hashes. Never overwrite them just to mark
        # success: unpack verified raw locally and compare the actual byte sets.
        try:
            with TemporaryDirectory(prefix='ms1-verify-') as temporary:
                checked = Path(temporary)/'corpus'
                status, paths, _ = extract_source(source,checked)
                if status=='no_source':
                    return ('keep','Verified archive has no TeX/BibTeX') if extract['status']=='no_source' and not actual else ('repair','Empty archive checkpoint/corpus disagree')
                expected = {p.relative_to(checked).as_posix(): _hash(p) for p in paths}
        except (ExtractionDataError,ExtractionSecurityError,OSError,ValueError) as exc:
            return 'review', f'Raw archive cannot be verified safely: {exc}'
        if expected != actual or extract['artifacts'].get('corpus') != version['corpus_path']:
            return 'repair','Corpus differs from verified raw archive'
        if extract['status'] != 'success':
            return 'repair', 'Raw archive contains TeX/BibTeX but checkpoint claims no source'
    if errors:
        return 'review','Corpus bytes match producer, but QA requires review (e.g. empty author file)'
    return 'keep','Verified corpus bytes and inventory'


def _paper(root, paper, record):
    from arxiv_pipeline.pipeline.parse import _versions
    actions = []
    def add(stage, number, decision, reason):
        actions.append(dict(stage=stage, version=number, decision=decision, reason=reason))
    if (record and record.get('status') == 'not_found' and record.get('input_id') == paper
            and record.get('arxiv_id') == paper and record.get('schema_version') == '1.0'
            and isinstance(record.get('error'), dict)
            and record['error'].get('kind') == 'not_found' and not record.get('versions')):
        return dict(paper_id=paper, record=record, actions=[], excluded=True, validation=None)
    try:
        if not isinstance(record, dict) or record.get('arxiv_id') != paper:
            raise ValueError('Missing discovery record or mismatched identity')
        versions = _versions(record)
        build_metadata(record)
        for v in versions:
            key, number = paper_key(paper), v['version']
            if (v['source_path'], v['corpus_path']) != (f'raw/{key}/v{number}/source', f'corpus/{key}/v{number}'):
                raise ValueError('Discovery version paths differ from the MS1 contract')
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        add('discovery', None, 'repair', str(exc))
        return dict(paper_id=paper, record=record, actions=actions, excluded=False, validation=None)

    report = validate(record, root, write_report=False)
    for v in versions:
        number = v['version']
        decision, reason = _download(root, paper, v)
        add('download', number, decision, reason)
        if decision != 'keep':
            add('extract', number, 'blocked', 'Requires a verified download')
            continue
        download = _checkpoint(root, 'download', paper, number)
        extract = _checkpoint(root, 'extract', paper, number)
        corpus_errors = [e for e in report['errors'] if e.get('version') == number and
                         (e.get('path', '').startswith('corpus/') or e.get('path') == 'extract')]
        if download['status'] == 'no_source':
            if extract and extract['status'] in {'skipped', 'no_source'}:
                add('extract', number, 'keep', 'Verified empty source; no extraction needed')
            else:
                add('extract', number, 'repair', 'Empty-source extraction checkpoint missing/inconsistent')
        else:
            decision, reason = _corpus(root,paper,v,download,extract,corpus_errors)
            add('extract', number, decision, reason)

    for stage, artifact, expected, statuses in (
            ('metadata', 'metadata', f'metadata/{paper_key(paper)}.json', {'success'}),
            ('references', 'references', f'references/{paper_key(paper)}.json', {'success','no_data','not_indexed'})):
        cp = _checkpoint(root, stage, paper)
        errors = [e for e in report['errors'] if e.get('path', '').startswith(stage + '/')
                  or f'/{stage}/' in e.get('path', '') or e.get('message','').startswith(stage + ' status:')]
        okay = cp and cp['status'] in statuses and cp['artifacts'].get(artifact) == expected and not errors
        if okay and stage == 'metadata':
            okay = _read(root, expected) == build_metadata(record)
        if okay and stage == 'references':
            provenance_path = cp['artifacts'].get('provenance')
            provenance = _read(root, provenance_path) if provenance_path else None
            refs = _read(root, expected)
            okay = (isinstance(provenance, dict) and isinstance(refs, list)
                    and provenance.get('source') == 'semantic_scholar'
                    and provenance.get('status') == cp['status']
                    and provenance.get('reference_count') == len(refs))
        add(stage, None, 'keep' if okay else 'repair', 'Verified artifact' if okay else 'Missing/invalid artifact or checkpoint')
    qa = _checkpoint(root, 'validate', paper)
    qa_path = f'validation/{paper_key(paper)}.json'
    saved = _read(root, qa_path)
    dirty = any(a['decision'] != 'keep' for a in actions)
    good = (report['valid'] and qa and qa['status'] == 'success' and not dirty
            and qa['artifacts'].get('validation') == qa_path
            and isinstance(saved, dict) and saved.get('valid') is True and saved.get('paper_id') == paper)
    add('validate', None, 'keep' if good else 'repair', 'Verified QA' if good else 'QA must be refreshed after repair')
    return dict(paper_id=paper, record=record, actions=actions, excluded=False, validation=report)


def _implementation():
    root = Path(__file__).resolve().parents[1]
    paths = [Path(__file__), root/'export/validation.py', root/'pipeline/parse.py',
             root/'main.py', root/'reporting/milestone2/validation.py',
             root/'pipeline/ms1_recovery_colab.py']
    paths += list((root/'crawling').rglob('*.py')) + list((root/'common').rglob('*.py'))
    return _digest({p.relative_to(root).as_posix(): _hash(p) for p in sorted(set(paths))})


def inventory(data_root, assignment, *, observer=None):
    """Inspect a frozen local snapshot; no reports/checkpoints or network writes."""
    root = Path(data_root).resolve()
    ids = _ids(assignment)
    before = _snapshot(root)
    records, diagnostics = _records(root, set(ids))
    papers = []
    for index, paper in enumerate(ids, 1):
        papers.append(_paper(root, paper, records.get(paper)))
        if observer:
            observer(dict(kind='inventory', done=index, total=len(ids), paper_id=paper))
    if before != _snapshot(root):
        raise ValueError('Input changed during inventory; stop writers and retry')
    counts = Counter(a['decision'] for p in papers for a in p['actions'])
    plan = dict(format='ms1-recovery-plan-1', created_at=utc_now(), data_root=str(root),
                implementation=_implementation(), assignment=assignment,
                snapshot_hash=_digest(before), papers=papers, diagnostics=diagnostics,
                summary={name: counts[name] for name in ('keep','repair','review','blocked')})
    plan['plan_id'] = _digest(plan)
    return plan


def prepare_snapshot(source_root, destination_root):
    """Explicit full local copy, verified byte-for-byte. Never overwrites a target.

    Requires disk space for a complete copy. No hardlinks: repairing a target
    must not change original bytes. Interrupted copy is not marked writable.
    """
    source, destination = Path(source_root).resolve(), Path(destination_root).resolve()
    if destination == source or destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError('Original and recovery roots must be disjoint')
    if destination.exists():
        raise ValueError('Recovery destination already exists; inspect it instead of overwriting')
    before = _snapshot(source)
    if safe_path(source, MARKER).exists():
        raise ValueError('Select the original snapshot, not another recovery copy')
    shutil.copytree(source, destination, copy_function=shutil.copy2)
    if _snapshot(source) != before or _snapshot(destination) != before:
        raise ValueError('Snapshot changed or copy verification failed; recovery is not enabled')
    marker = dict(format='ms1-recovery-copy-1', source=str(source), original_hash=_digest(before), created_at=utc_now())
    atomic_json(destination/MARKER, marker)
    return marker


def recover(plan, data_root, evidence_root, *, apply=False, allow_network=False, adapters=None, client=None, observer=None):
    """Execute approved scopes on a marked copy; default is a read-only dry run.

    Inject adapters/client for offline tests. HTTP 404/nonretryable errors remain
    review items. A fresh inventory is required before resuming after interruption.
    """
    root, evidence = Path(data_root).resolve(), Path(evidence_root).resolve()
    if plan.get('format') != 'ms1-recovery-plan-1' or plan.get('data_root') != str(root):
        raise ValueError('Plan context differs from selected data root')
    signed = dict(plan); claimed = signed.pop('plan_id', None)
    if _digest(signed) != claimed or plan['implementation'] != _implementation():
        raise ValueError('Plan or implementation changed; inventory again')
    if plan['snapshot_hash'] != _digest(_snapshot(root)):
        raise ValueError('Input changed after inventory; generate a fresh plan')
    if not apply:
        return dict(dry_run=True, plan_id=claimed, summary=plan['summary'])
    marker = _read(root, MARKER)
    if not isinstance(marker, dict) or marker.get('format') != 'ms1-recovery-copy-1':
        raise ValueError('Recovery writes require an explicitly prepared snapshot copy')
    original = Path(marker['source']).resolve()
    if (root == original or root.is_relative_to(original) or original.is_relative_to(root)
            or evidence == root or evidence.is_relative_to(root) or root.is_relative_to(evidence)
            or evidence == original or evidence.is_relative_to(original) or original.is_relative_to(evidence)):
        raise ValueError('Original, recovery and evidence roots must be disjoint')
    if plan['diagnostics']:
        raise ValueError('Resolve discovery diagnostics before recovery')
    adapters = adapters or {}
    evidence.mkdir(parents=True, exist_ok=True)
    atomic_json(evidence/'plan.json', plan)
    result = dict(format='ms1-recovery-run-1', plan_id=claimed, status='running', complete=False,
                  scope='assignment-stages', handoff_ready=False,
                  events=[], blocked=[], started_at=utc_now())
    atomic_json(evidence/'run.json', result)

    def execute(stage, paper, record, version=None):
        upstream = _checkpoint(root, 'download', paper, version['version']) if stage == 'extract' else None
        callback = adapters.get(stage)
        if callback is None:
            name = {'download':'downloader','extract':'extractor'}.get(stage,stage)
            module = importlib.import_module('arxiv_pipeline.export.validation' if stage=='validate'
                                            else f'arxiv_pipeline.crawling.{name}')
            callback = module.run
        value = callback(record, root, version=version, upstream=upstream)
        if not isinstance(value, StageResult) or (value.stage,value.paper_id,value.version) != (stage,paper,version['version'] if version else None):
            raise ValueError('Adapter returned mismatched recovery identity')
        row = value.to_dict()
        for relative in row['artifacts'].values():
            safe_path(root, relative)
        atomic_json(safe_path(root, f'state/stages/{stage}/{paper_key(paper)}/{row["version"] or "paper"}.json'),row)
        result['events'].append(dict(stage=stage,paper_id=paper,version=row['version'],status=row['status']))
        atomic_json(evidence/'run.json', result)
        if observer:
            observer(dict(kind='stage', **result['events'][-1]))

    try:
        for selected in plan['papers']:
            paper, record = selected['paper_id'], selected['record']
            if selected['excluded']:
                continue
            dirty = False
            if any(a['stage']=='discovery' for a in selected['actions']):
                if not allow_network:
                    result['blocked'].append(dict(paper_id=paper,stage='discovery',reason='Network disabled'))
                    continue
                from arxiv_pipeline.crawling.discovery import discover, ArxivClient
                record = discover(paper, client or ArxivClient()).to_dict()
                atomic_json(safe_path(root, f'state/discovery/{hashlib.sha256(paper.encode()).hexdigest()}.json'),record)
                result['events'].append(dict(stage='discovery',paper_id=paper,version=None,status=record['status']))
                atomic_json(evidence/'run.json',result)
                if observer:
                    observer(dict(kind='stage', **result['events'][-1]))
                dirty = True
            inspected = _paper(root,paper,record)
            if any(a['stage']=='discovery' for a in inspected['actions']):
                result['blocked'].append(dict(paper_id=paper,stage='discovery',reason='Discovery still unresolved'))
                continue
            for stage in ORDER[1:]:
                for action in [a for a in inspected['actions'] if a['stage']==stage]:
                    if action['decision']=='keep' and not (stage=='validate' and dirty):
                        continue
                    if action['decision'] in {'review','blocked'}:
                        result['blocked'].append(dict(paper_id=paper,**action))
                        continue
                    if stage in NETWORK and not allow_network:
                        result['blocked'].append(dict(paper_id=paper,**action,network_disabled=True))
                        continue
                    version = next((v for v in record['versions'] if v['version']==action['version']),None)
                    execute(stage,paper,record,version)
                    dirty = True
                if stage == 'download' and dirty:
                    inspected = _paper(root,paper,record)
        final = inventory(root,plan['assignment'], observer=observer)
        excluded = [p['paper_id'] for p in final['papers'] if p['excluded']]
        result.update(status='finished', complete=not excluded and not final['diagnostics'] and all(
            a['decision']=='keep' for p in final['papers'] for a in p['actions']),
            assigned_count=len(final['papers']), excluded=excluded,
            remaining=final['summary'], finished_at=utc_now())
        atomic_json(evidence/'after-inventory.json',final)
    except BaseException as exc:
        result.update(status='interrupted' if isinstance(exc,KeyboardInterrupt) else 'failed',
                      complete=False,error=f'{type(exc).__name__}: {exc}')
        atomic_json(evidence/'run.json',result)
        raise
    atomic_json(evidence/'run.json',result)
    return result
