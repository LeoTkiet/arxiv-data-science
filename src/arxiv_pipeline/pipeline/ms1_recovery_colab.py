"""Local staging, durable recovery copies and MS1 handoff for the Colab notebook.

The source is read-only. All parsing and content hashing runs on the local disk.
Persistent writes require an owned session and explicit repair/finalize calls.
"""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import csv
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
from uuid import uuid4
import zipfile

from arxiv_pipeline.common.utils import atomic_json, normalize_id, paper_key, utc_now
from arxiv_pipeline.pipeline import ms1_recovery as core
from arxiv_pipeline.reporting.milestone1.audit import audit
from arxiv_pipeline.reporting.milestone2.validation import safe_path


def _disjoint(*roots):
    if any(a.is_relative_to(b) or b.is_relative_to(a)
           for i, a in enumerate(roots) for b in roots[i + 1:]):
        raise ValueError('Source, local workspace and persistent output must be disjoint')


def _listing(root, progress=None):
    """One metadata pass; never follow links or hash content through Drive."""
    result = {}
    last = time.monotonic()
    for current, dirs, names in os.walk(root, followlinks=False):
        for name in dirs + names:
            path = safe_path(root, (Path(current)/name).relative_to(root).as_posix())
            if path.is_file():
                stat = path.stat()
                result[path.relative_to(root).as_posix()] = [stat.st_size, stat.st_mtime_ns]
            if progress and time.monotonic() - last >= 5:
                progress(dict(kind='phase', message=f'Đang liệt kê nguồn: {len(result)} file'))
                last = time.monotonic()
    return result


def _copy_directories(root, names):
    # Windows path resolution can race with creation of a missing ancestor.
    # Create and validate destination directories before starting copy workers.
    for relative in sorted({Path(name).parent.as_posix() for name in names}):
        safe_path(root, relative).mkdir(parents=True, exist_ok=True)


def _copy(source, target):
    """Stream once, hash the copied bytes, publish only the complete file."""
    before = source.stat()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.copy-', dir=target.parent)
    digest = hashlib.sha256()
    try:
        with source.open('rb') as reader, os.fdopen(fd, 'wb') as writer:
            for chunk in iter(lambda: reader.read(1024 * 1024), b''):
                writer.write(chunk)
                digest.update(chunk)
        after = source.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError('Source changed during copy; stop all writers')
        if Path(temporary).stat().st_size != before.st_size:
            raise ValueError('Copied file size differs from source')
        os.replace(temporary, target)
        return digest.hexdigest()
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def stage_snapshot(source_root, destination_root, *, workers=32, progress=None):
    """Copy a frozen snapshot locally, resuming verified files after interruption.

    The source content is read once per copied file. A local journal binds reused
    files to the same source path/size/mtime and a checked local SHA-256. Source
    metadata is checked again before enabling recovery. This requires frozen input.
    """
    source, destination = Path(source_root).resolve(), Path(destination_root).resolve()
    _disjoint(source, destination)
    if not source.is_dir():
        raise ValueError('MS1 source data directory does not exist')
    journal_path = destination.parent/f'.{destination.name}-staging.json'
    journal = core._read(journal_path.parent, journal_path.name)
    if destination.exists() and not isinstance(journal, dict):
        raise ValueError('Existing local data has no staging journal; select another workspace')
    if journal and journal.get('source') != str(source):
        raise ValueError('Staging source changed; select another workspace')
    if progress: progress(dict(kind='phase', message='Đang liệt kê file trong nguồn đã dừng ghi'))
    before = _listing(source, progress)
    if not before:
        raise ValueError('Source data directory is empty')
    destination.parent.mkdir(parents=True, exist_ok=True)
    required = sum(info[0] for name, info in before.items() if not (destination/name).is_file())
    if shutil.disk_usage(destination.parent).free < required + 64 * 1024 * 1024:
        raise ValueError('Insufficient local disk space for the snapshot copy')
    destination.mkdir(exist_ok=True)
    journal = journal or dict(source=str(source), files={})
    verified, pending, reused = {}, [], 0
    for name, stamp in before.items():
        previous = journal['files'].get(name)
        target = safe_path(destination, name)
        if previous and previous['stamp'] == stamp and target.is_file() and core._hash(target) == previous['sha256']:
            verified[name] = previous
            reused += 1
        else:
            pending.append(name)
    # Reject removals rather than silently keeping an unrelated/stale file.
    if set(_listing(destination)) - set(before) - {core.MARKER}:
        raise ValueError('Staging destination has files absent from the frozen source')
    journal['files'] = verified
    atomic_json(journal_path, journal)
    _copy_directories(destination, pending)
    done = reused
    started = time.monotonic()
    if progress: progress(dict(kind='copy', done=done, total=len(before), reused=reused, bytes=0))
    pool = ThreadPoolExecutor(max_workers=max(1, min(int(workers), 32)))
    futures = {}
    copied_bytes = 0
    try:
        queue = iter(pending)
        def submit():
            name = next(queue, None)
            if name is not None:
                futures[pool.submit(_copy, safe_path(source, name), safe_path(destination, name))] = name
        for _ in range(64): submit()
        while futures:
            completed, _ = wait(futures, timeout=2, return_when=FIRST_COMPLETED)
            for future in completed:
                name = futures.pop(future)
                journal['files'][name] = dict(stamp=before[name], sha256=future.result())
                done += 1
                copied_bytes += before[name][0]
                submit()
            if done == reused + 1 or done % 64 == 0 or done == len(before):
                atomic_json(journal_path, journal)
            if progress:
                progress(dict(kind='copy', done=done, total=len(before), reused=reused,
                              bytes=copied_bytes, seconds=time.monotonic() - started))
    finally:
        for future in futures: future.cancel()
        pool.shutdown(wait=True, cancel_futures=True)
        atomic_json(journal_path, journal)
    if _listing(source, progress) != before:
        raise ValueError('Source changed while staging; stop writers before retrying')
    marker = core._read(source, core.MARKER)
    if marker is None:
        marker = dict(format='ms1-recovery-copy-1', source=str(source),
                      original_hash=core._digest({k:v['sha256'] for k,v in journal['files'].items()}),
                      created_at=utc_now())
    atomic_json(safe_path(destination, core.MARKER), marker)
    return marker


def select_assignment(member, config_path):
    if member not in {'TV1', 'TV2', 'TV3', 'TV4', 'TV5'}:
        raise ValueError('Select TV1 through TV5')
    config = json.loads(Path(config_path).read_text(encoding='utf-8'))
    row = config['ranges'][int(member[-1]) - 1]
    assignment = dict(group_id=config['group_id'], scope=member, ranges=[row],
                      assigned_input_count=row['count'])
    core._ids(assignment)
    return assignment


def _csv(path, rows, fields):
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


class RecoverySession:
    def __init__(self, member, source, persistent, local, assignment, *, progress=None):
        self.member = member
        self.source = Path(source).resolve()
        self.persistent = Path(persistent).resolve()
        self.local = Path(local).resolve()
        _disjoint(self.source, self.persistent, self.local)
        self.assignment = assignment
        self.identity = dict(member=member, source=str(self.source), assignment=assignment)
        core._ids(assignment)
        self.data = self.local/'data'
        self.progress = progress
        self.plan = None
        self.applied = False
        self.events = []
        self.source_runs = []
        self._published = {}
        self._evidence_published = {}
        self.evidence = self.local/'evidence'/uuid4().hex

    def _emit(self, **event):
        if self.progress: self.progress(event)

    def _context(self, parent):
        context = core._read(parent, 'session.json')
        if parent.exists() and any(parent.iterdir()) and context != self.identity:
            raise ValueError('Workspace belongs to another input/member or is not a recovery session')
        return context

    def inspect(self):
        """Stage/restore once, then inspect local files. Never modifies source."""
        local_context = self._context(self.local)
        persistent_context = self._context(self.persistent)
        if local_context is None:
            self.local.mkdir(parents=True, exist_ok=True)
            atomic_json(self.local/'session.json', self.identity)
        if not (self.data/core.MARKER).is_file():
            restore = persistent_context and core._read(self.persistent, 'baseline-ready.json')
            origin = self.persistent/'data' if restore else self.source
            self._emit(kind='phase', message=f'Chép {origin} về ổ local bằng tối đa 32 luồng')
            stage_snapshot(origin, self.data, progress=self.progress)
            if restore:
                journal = core._read(self.local, '.data-staging.json')
                self._published = {name: row['sha256'] for name,row in journal['files'].items()}
        marker = core._read(self.data, core.MARKER)
        if not marker or Path(marker['source']).resolve() != self.source:
            raise ValueError('Recovery copy origin differs from selected original data')
        if persistent_context and core._read(self.persistent, 'baseline-ready.json'):
            self.source_runs = []
            for path in sorted(safe_path(self.persistent, 'evidence').glob('*/run.json')):
                relative = path.relative_to(self.persistent).as_posix()
                run = core._read(self.persistent, relative)
                if (isinstance(run, dict) and run.get('format') == 'ms1-recovery-run-1'
                        and run.get('status') == 'finished'
                        and run.get('assigned_count') == len(core._ids(self.assignment))):
                    self.source_runs.append(dict(evidence_id=path.parent.name, run=run))
            self.applied = self.applied or bool(self.source_runs)
            cached = core._read(self.local, 'published-files.json')
            if isinstance(cached, dict):
                self._published = cached
            elif not self._published:
                # Migration for notebooks that did not persist the local upload
                # cache: the verified baseline authenticated unchanged files.
                journal = core._read(self.local, '.data-staging.json')
                if isinstance(journal, dict):
                    changed = {'summary.json','manifests/recovery.json','manifests/run.json'}
                    for previous in self.source_runs:
                        for event in previous['run'].get('events', []):
                            changed.update(self._scopes(event))
                    self._published = {
                        name: row['sha256'] for name,row in journal.get('files', {}).items()
                        if not any(name == scope or name.startswith(scope+'/') for scope in changed)
                    }
            if self.applied:
                self._emit(kind='phase', message='Đã nhận lại lượt phục hồi đã lưu; bước 3 có thể kiểm định và xuất bàn giao')
        self._emit(kind='phase', message='Kiểm tra file và checkpoint trên local; không đọc lại nội dung qua Drive')
        self.plan = core.inventory(self.data, self.assignment, observer=self.progress)
        self.evidence.mkdir(parents=True, exist_ok=True)
        atomic_json(self.evidence/'assignment.json', self.assignment)
        atomic_json(self.evidence/'plan.json', self.plan)
        rows = [dict(paper_id=p['paper_id'], **a) for p in self.plan['papers'] for a in p['actions']]
        _csv(self.evidence/'inventory.csv', rows, ['paper_id','version','stage','decision','reason'])
        return self.plan

    def _sync(self, scopes=None):
        """Mirror owned scopes only; checkpoint files are copied after artifacts."""
        destination = self.persistent/'data'
        names = set()
        if scopes is None:
            names.update(_listing(self.data))
        else:
            for scope in scopes:
                path = safe_path(self.data, scope)
                if path.is_dir():
                    names.update(p.relative_to(self.data).as_posix() for p in path.rglob('*') if p.is_file())
                    remote = safe_path(destination, scope)
                    if remote.exists():
                        for name in _listing(remote):
                            relative = f'{scope}/{name}'
                            if relative not in names:
                                safe_path(destination, relative).unlink()
                elif path.is_file():
                    names.add(scope)
        closing = {'summary.json','manifests/recovery.json','manifests/run.json'}
        ordered = sorted(names, key=lambda n: (n in closing, n.startswith('state/'), n))
        _copy_directories(destination, ordered)
        self._emit(kind='phase', message=f'Đồng bộ {len(ordered)} file; bỏ qua nội dung đã lưu và còn khớp')
        def upload(name):
            source = safe_path(self.data, name)
            target = safe_path(destination, name)
            digest = core._hash(source)
            if self._published.get(name) != digest or not target.is_file():
                self._published[name] = _copy(source, target)
        done = 0
        # Artifacts first, checkpoints next, readiness/receipt last. Bound futures
        # to 64 so a large corpus does not allocate one Future per source file.
        phases = [[n for n in ordered if n not in closing and not n.startswith('state/')],
                  [n for n in ordered if n not in closing and n.startswith('state/')]]
        with ThreadPoolExecutor(max_workers=32) as pool:
            for phase in phases:
                for offset in range(0, len(phase), 64):
                    batch = phase[offset:offset+64]
                    futures = {pool.submit(upload, name) for name in batch}
                    while futures:
                        completed, futures = wait(futures, timeout=2, return_when=FIRST_COMPLETED)
                        for future in completed: future.result()
                        done += len(completed)
                        self._emit(kind='publish', done=done, total=len(ordered))
        for name in ('summary.json','manifests/recovery.json','manifests/run.json'):
            if name in names:
                upload(name)
                done += 1
                self._emit(kind='publish', done=done, total=len(ordered))

    def _save_evidence(self):
        for path in self.evidence.rglob('*'):
            if path.is_file():
                relative = path.relative_to(self.evidence).as_posix()
                digest = core._hash(path)
                if self._evidence_published.get(relative) != digest:
                    self._evidence_published[relative] = _copy(path, safe_path(self.persistent, f'evidence/{self.evidence.name}/{relative}'))
        atomic_json(self.local/'published-files.json', self._published)

    def _scopes(self, event):
        paper, number, stage = event['paper_id'], event['version'], event['stage']
        key = paper_key(paper)
        scopes = [f'state/stages/{stage}/{key}/{number or "paper"}.json']
        if stage == 'download': scopes += [f'raw/{key}/v{number}']
        elif stage == 'extract': scopes += [f'corpus/{key}/v{number}']
        elif stage == 'discovery': scopes = [f'state/discovery/{hashlib.sha256(paper.encode()).hexdigest()}.json']
        elif stage == 'references': scopes += [f'references/{key}.json', f'references/{key}.provenance.json']
        elif stage in {'metadata','validate'}:
            folder = 'validation' if stage == 'validate' else stage
            scopes += [f'{folder}/{key}.json']
        return scopes

    def _checkpoint(self, event):
        if event['kind'] != 'stage':
            if self.progress: self.progress(event)
            return
        self._sync(self._scopes(event))
        atomic_json(self.persistent/'last-stage.json', dict(event, saved_at=utc_now(), handoff_ready=False))
        self.events.append(event)
        if self.progress: self.progress(event)

    def repair(self, mode='inspect'):
        if mode not in {'inspect','local','network'}:
            raise ValueError('Mode must be inspect, local or network')
        if self.plan is None:
            raise ValueError('Run inventory first')
        if mode == 'inspect':
            return core.recover(self.plan, self.data, self.evidence)
        core.recover(self.plan, self.data, self.evidence)  # Reject a stale reviewed plan.
        self._context(self.persistent)
        self.persistent.mkdir(parents=True, exist_ok=True)
        atomic_json(self.persistent/'session.json', self.identity)
        self.applied = True
        # A failed/interrupted run must not leave an inherited ready receipt.
        receipt = safe_path(self.data, 'manifests/run.json')
        receipt.unlink(missing_ok=True)
        atomic_json(safe_path(self.data, 'manifests/recovery.json'), dict(handoff_ready=False, status='working'))
        atomic_json(safe_path(self.data, 'summary.json'), dict(schema_version='1.0',
                    complete=False, mode='selective_recovery', handoff_ready=False))
        if not core._read(self.persistent, 'baseline-ready.json'):
            self._sync()
            atomic_json(self.persistent/'baseline-ready.json', dict(ready=True, created_at=utc_now()))
        else:
            self._sync(['summary.json','manifests/recovery.json'])
            safe_path(self.persistent/'data', 'manifests/run.json').unlink(missing_ok=True)
        # Receipt invalidation changes the local fingerprint; regenerate the plan.
        self.plan = core.inventory(self.data, self.assignment, observer=self.progress)
        atomic_json(self.evidence/'plan.json', self.plan)
        self._save_evidence()
        try:
            result = core.recover(self.plan, self.data, self.evidence, apply=True,
                                  allow_network=mode == 'network', observer=self._checkpoint)
            return result
        finally:
            self._save_evidence()

    def finish(self):
        """Refresh owned QA/manifests, audit, bind snapshot, export evidence ZIP."""
        if self.plan is None:
            raise ValueError('Run inventory first')
        if not self.applied:
            return self._inspection_zip()
        atomic_json(self.evidence/'source-runs.json', self.source_runs)
        self._emit(kind='phase', message='Kiểm định QA và tạo lại manifest cho dữ liệu phục hồi')
        before = core.inventory(self.data, self.assignment, observer=self.progress)
        records = [p['record'] for p in before['papers'] if isinstance(p['record'], dict)]
        for paper in before['papers']:
            if paper['validation'] is None:
                continue
            result = core.validate(paper['record'], self.data).to_dict()
            atomic_json(safe_path(self.data, f'state/stages/validate/{paper_key(paper["paper_id"])}/paper.json'), result)
        atomic_json(safe_path(self.data, 'manifests/discovery.json'), records)
        stages = []
        for paper in before['papers']:
            if paper['validation'] is None: continue
            for stage in core.ORDER[1:]:
                versions = paper['record']['versions'] if stage in {'download','extract'} else [None]
                for version in versions:
                    row = core._checkpoint(self.data, stage, paper['paper_id'], version['version'] if version else None)
                    if row: stages.append(row)
        atomic_json(safe_path(self.data, 'manifests/stages.json'), stages)
        checked = core.inventory(self.data, self.assignment, observer=self.progress)
        # The next milestone reads every discovery checkpoint, not just the latest.
        # Verify against its actual loader to catch stale conflicting duplicates.
        from arxiv_pipeline.pipeline.parse import _discover
        imported, errors, _ = _discover(self.data, core._ids(self.assignment))
        diagnostics = checked['diagnostics'] + errors + [
            dict(paper_id=row[0]['arxiv_id'], message=row[2]) for row in imported if row[2]]
        unresolved = [p['paper_id'] for p in checked['papers']
                      if p['excluded'] or any(a['decision'] != 'keep' for a in p['actions'])]
        unresolved = sorted(set(unresolved) | {row[0]['arxiv_id'] for row in imported if row[2]})
        ready = not unresolved and not diagnostics
        report = audit(self.data, self.assignment)
        report['integrity_unresolved_ids'] = unresolved
        report['recovery_diagnostics'] = diagnostics
        report['audit_complete'] = report['audit_complete'] and ready
        ready = ready and report['audit_complete']
        atomic_json(self.evidence/'assignment-audit.json', report)
        atomic_json(self.evidence/'after-inventory.json', checked)
        _csv(self.evidence/'remaining.csv',
             [dict(paper_id=p['paper_id'], **a) for p in checked['papers']
              for a in p['actions'] if a['decision'] != 'keep'] +
             [dict(paper_id=p['paper_id'], stage='discovery', decision='review', reason='not_found')
              for p in checked['papers'] if p['excluded']],
             ['paper_id','version','stage','decision','reason'])
        atomic_json(self.evidence/'recovered.json', dict(scope='this_session', events=self.events))
        run_id = self.evidence.name
        summary = dict(schema_version='1.0', run_id=run_id, timestamp=utc_now(),
                       mode='selective_recovery', inputs=len(checked['papers']),
                       papers=sum(r.get('status') == 'success' for r in records),
                       versions=sum(len(r.get('versions', [])) for r in records if r.get('status') == 'success'),
                       discovery_statuses=dict(Counter(r.get('status','missing') for r in records)),
                       stage_statuses=dict(Counter(r['status'] for r in stages)),
                       complete=ready, handoff_ready=ready)
        atomic_json(safe_path(self.data, 'summary.json'), summary)
        content = core._snapshot(self.data)
        for name in ('manifests/run.json','manifests/recovery.json'): content.pop(name, None)
        receipt = dict(schema_version='1.0', run_id=run_id, member=self.member,
                       snapshot_id=core._digest(content), created_at=utc_now(),
                       assigned_input_count=len(checked['papers']), handoff_ready=ready,
                       unresolved_paper_ids=unresolved, diagnostics=diagnostics,
                       original=core._read(self.data, core.MARKER), implementation=core._implementation())
        atomic_json(self.evidence/'snapshot.json', dict(receipt, files=content, assignment=self.assignment))
        atomic_json(safe_path(self.data, 'manifests/recovery.json'), receipt)
        atomic_json(safe_path(self.data, 'manifests/run.json'), dict(run_id=run_id, sha256={
            name:core._hash(self.data/name) for name in ('manifests/discovery.json',
            'manifests/stages.json','summary.json','manifests/recovery.json')}))
        self._sync()
        self._save_evidence()
        receipt['evidence_zip'] = self._zip()
        return receipt

    def _zip(self):
        path = self.local/f'MS1_RECOVERY_{self.member}_{self.evidence.name}.zip'
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
            for item in self.evidence.rglob('*'):
                if item.is_file(): archive.write(item, item.relative_to(self.evidence).as_posix())
        self.persistent.mkdir(parents=True, exist_ok=True)
        atomic_json(self.persistent/'session.json', self.identity)
        saved = safe_path(self.persistent, path.name)
        _copy(path, saved)
        return str(saved)

    def _inspection_zip(self):
        atomic_json(self.evidence/'snapshot.json', dict(mode='inventory_only', handoff_ready=False,
                    source=str(self.source), assignment=self.assignment, plan_id=self.plan['plan_id']))
        self.persistent.mkdir(parents=True, exist_ok=True)
        atomic_json(self.persistent/'session.json', self.identity)
        self._save_evidence()
        return dict(mode='inventory_only', handoff_ready=False, evidence_zip=self._zip())
