"""Bounded local MS2 processing with durable, checksummed Drive batch archives."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from pathlib import Path
import re
import shutil
from tempfile import TemporaryDirectory
from time import perf_counter
from uuid import uuid4
import zipfile

from arxiv_pipeline.common.utils import atomic_json, paper_key, utc_now
from arxiv_pipeline.export.benchmark import (
    MemorySampler, generate_parsing_summary,
)
from arxiv_pipeline.main import expand_range
from arxiv_pipeline.pipeline import parse, completion_index, stage_migration
from arxiv_pipeline.pipeline.parse import _file_hash as _hash
from arxiv_pipeline.reporting.milestone2.validation import safe_path


def _files(root, relative, files, directories):
    """Enumerate a relative subtree, rejecting linked input and output paths."""
    path = safe_path(root, relative)
    if path.is_file():
        files.add(relative)
    elif path.is_dir():
        directories.add(relative)
        for child in path.iterdir():
            _files(root, child.relative_to(root).as_posix(), files, directories)


def _copy_files(source, destination, files, directories, workers):
    parents = {Path(relative).parent.as_posix() for relative in files}
    for relative in sorted(directories | parents):
        safe_path(destination, relative).mkdir(parents=True, exist_ok=True)

    def copy(relative):
        original = safe_path(source, relative)
        target = safe_path(destination, relative)
        before = original.stat()
        shutil.copyfile(original, target)
        after = original.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError(f'File changed while copying: {relative}')

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(copy, sorted(files)))


def _paper_files(root, paper):
    files, directories = set(), set()
    key = paper_key(paper)
    for relative in (f'parsed/{key}', f'tables/{key}',
                     f'validation/parsing/{key}.json', f'state/parsing/{key}.json'):
        _files(root, relative, files, directories)
    for stage in parse.MODULES:
        prefix = f'state/stages/{stage}/{key}'
        _files(root, prefix, files, directories)
        for checkpoint in safe_path(root, prefix).glob('*.json'):
            if checkpoint.name.endswith('.dependencies.json'):
                continue
            data = parse._read(checkpoint, cache=True)
            if isinstance(data, dict) and isinstance(data.get('artifacts'), dict):
                for relative in data['artifacts'].values():
                    _files(root, relative, files, directories)
    return files, directories




def _load_journal(output):
    papers = {}
    for path in sorted(safe_path(output, 'local-batches').glob('*.json')):
        receipt = json.loads(path.read_text(encoding='utf-8'))
        if receipt.get('format') != 'ms2-local-batch-1':
            raise ValueError(f'Unknown local checkpoint receipt: {path.name}')
        for paper in receipt['paper_files']:
            papers[paper] = receipt
    return papers




def _publish_archive(local_archive, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = safe_path(destination.parent, destination.name + '.partial')
    shutil.copyfile(local_archive, temporary)
    temporary.replace(destination)




def _discovery_index(source, output, destination, workspace, identity, workers):
    cache_identity = {k: identity[k] for k in ('source', 'snapshot_id')}
    cached = parse._read(safe_path(output, 'resume-discovery.json'), cache=True)
    archive = workspace / 'discovery.zip'
    if isinstance(cached, dict) and cached.get('context') == cache_identity:
        shutil.copyfile(safe_path(output, cached['archive']), archive)
        if _hash(archive) == cached['sha256']:
            with zipfile.ZipFile(archive) as bundle:
                for member in bundle.infolist():
                    target = safe_path(destination, member.filename)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.open(member) as src, target.open('wb') as dst:
                        shutil.copyfileobj(src, dst)
            print('Discovery restored from one compact archive', flush=True)
            return
        print('Discovery cache checksum changed; rebuilding from the frozen snapshot', flush=True)
    checkpoints = safe_path(source, 'state/discovery')
    files = {p.relative_to(source).as_posix() for p in checkpoints.glob('*.json')}
    if not files and safe_path(source, 'manifests/discovery.json').is_file():
        files.add('manifests/discovery.json')
    _copy_files(source, destination, files, set(), workers)
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as bundle:
        for relative in sorted(files):
            bundle.write(safe_path(destination, relative), relative)
    relative = f'discovery-cache/{uuid4().hex}.zip'
    _publish_archive(archive, safe_path(output, relative))
    atomic_json(output / 'resume-discovery.json',
                dict(context=cache_identity, archive=relative, sha256=_hash(archive)))


def _merge_summary(parts, total, input_errors):
    outcomes = [p for part in parts for p in part['paper_results']]
    excluded = [p for part in parts for p in part['excluded_inputs']]
    processed = len(outcomes) + len(excluded)
    statuses = Counter()
    for part in parts:
        statuses.update(part['stage_statuses'])
    return dict(
        schema_version='2.0', mode='parse', timestamp=utc_now(), frozen_input=True,
        complete=processed == total and not input_errors and all(p['complete'] for p in outcomes),
        papers=len(outcomes), expected_versions=sum(len(p['expected_versions']) for p in outcomes),
        stage_statuses=dict(statuses), paper_results=outcomes, excluded_inputs=excluded,
        input_errors=input_errors, processed_ids=processed, selected_ids_count=total,
        storage_mode='colab-local-batches',
    )


_archive_verified = set()


def _cached_archive(output, receipt, cache):
    cache.mkdir(parents=True, exist_ok=True)
    digest = receipt['sha256']
    if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
        raise ValueError('Invalid archive SHA-256')
    archive = cache / (digest + '.zip')
    key = (str(archive), digest)
    if key not in _archive_verified:
        if not archive.is_file() or _hash(archive) != digest:
            temporary = archive.with_suffix('.partial')
            shutil.copyfile(safe_path(output, receipt['archive']), temporary)
            if _hash(temporary) != digest:
                raise ValueError('Checkpoint archive checksum mismatch: ' + receipt['archive'])
            temporary.replace(archive)
        _archive_verified.add(key)
    return archive


def _restore(output, local, papers, journal, workers):
    packages = {}
    loose = []
    for paper in papers:
        if paper in journal:
            receipt = journal[paper]
            packages.setdefault(receipt['archive'], (receipt, set()))[1].update(
                receipt['paper_files'][paper])
        else:
            loose.append(paper)
    def restore_loose(paper):
        files, directories = _paper_files(output, paper)
        _copy_files(output, local, files, directories, 1)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(restore_loose, loose))
    for receipt, selected in packages.values():
        archive = _cached_archive(output, receipt, local.parent / 'archive-cache')
        with zipfile.ZipFile(archive) as bundle:
            for relative in sorted(selected):
                target = safe_path(local, relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(relative) as src, target.open('wb') as dst:
                    shutil.copyfileobj(src, dst)


def _stage_inputs(source, destination, records, workers, *, discovery_root):
    """Enumerate independent papers concurrently, then copy a deduplicated file set."""
    def inventory(item):
        record, paths, conflict = item
        files, directories = set(), set()
        discovery = {p.relative_to(discovery_root).as_posix() for p in paths}
        if conflict or record.get('status') != 'success':
            return files, directories, discovery
        try:
            versions = parse._versions(record)
        except (ValueError, TypeError, KeyError):
            return files, directories, discovery
        for version in versions:
            key, number = paper_key(record['arxiv_id']), version['version']
            for name in ('source_path', 'corpus_path'):
                _files(source, version[name], files, directories)
            for stage in ('download', 'extract'):
                relative = f'state/stages/{stage}/{key}/{number}.json'
                _files(source, relative, files, directories)
                checkpoint = parse._read(safe_path(source, relative), cache=True)
                if isinstance(checkpoint, dict) and isinstance(checkpoint.get('artifacts'), dict):
                    for artifact in checkpoint['artifacts'].values():
                        _files(source, artifact, files, directories)
        return files, directories, discovery
    files, directories, discovery = set(), set(), set()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for f, d, s in pool.map(inventory, records):
            files.update(f)
            directories.update(d)
            discovery.update(s)
    _copy_files(source, destination, files, directories, workers)
    _copy_files(discovery_root, destination, discovery, set(), workers)


def validate_legacy(source, member):
    """Reject a parent folder or another member's output before processing starts."""
    source = Path(source).resolve()
    if not source.is_dir() or not any(safe_path(source, name).is_dir()
            for name in ('parsed', 'state/stages', 'local-batches')):
        raise ValueError('Legacy MS2 root must contain parsed/, state/stages/ or local-batches/; select the member folder')
    for name in ('acceptance-config.json', 'acceptance.json'):
        info = parse._read(safe_path(source, name))
        if info is None:
            continue
        if not isinstance(info, dict):
            raise ValueError('Legacy MS2 member metadata is unreadable')
        previous_job = info.get('job') or {}
        if not isinstance(previous_job, dict):
            raise ValueError('Legacy MS2 job metadata must be an object')
        owner = info.get('member') or previous_job.get('member')
        if owner is not None and owner != member:
            raise ValueError(f'Legacy MS2 member mismatch: expected {member}, found {owner}')
    return source


def select_smoke_ids(job, output, *, local_root, workers=8, limit=3):
    """Select a smoke sample from a compact, frozen discovery copy on local disk."""
    source, output = parse.validate_roots(job['input'], output)
    local_root = Path(local_root).resolve()
    for boundary in (source, output):
        if local_root.is_relative_to(boundary) or boundary.is_relative_to(local_root):
            raise ValueError('Local workspace must be disjoint from input and Drive output')
    identity = completion_index.context(source, job)
    _validate_context(output, identity, parse._read(safe_path(output, 'acceptance-config.json')))
    assigned = set(expand_range(job['assignment_range']))
    output.mkdir(parents=True, exist_ok=True)
    with _persistent_workspace(local_root, output, identity, sorted(assigned)) as temporary:
        workspace = Path(temporary)
        discovery = workspace / 'discovery'
        discovery.mkdir(parents=True, exist_ok=True)
        _discovery_index(source, output, discovery, workspace, identity, workers)
        records, errors, _ = parse._discover(discovery, None)
        if errors:
            raise ValueError(f'Discovery could not be read: {errors[:3]}')
        return [record['arxiv_id'] for record, _, conflict in records
                if not conflict and record.get('status') == 'success'
                and record['arxiv_id'] in assigned][:limit]


def _stage_legacy(source, destination, records, workers, *, catalog=None, versions_filter=None):
    """Read only selected tree evidence, from loose files or checksummed batches.

    Optional legacy evidence must never block normal parsing. Invalid files are
    reported and left for the current producers to recompute.
    """
    destination.mkdir(parents=True, exist_ok=True)
    selected = {}
    for record, _, conflict in records:
        if conflict or record.get('status') != 'success':
            continue
        try:
            versions = parse._versions(record)
        except (ValueError, TypeError, KeyError):
            continue
        files = set()
        key = paper_key(record['arxiv_id'])
        for version in versions:
            number = version['version']
            if versions_filter is not None and (record['arxiv_id'], number) not in versions_filter:
                continue
            for stage in ('resolve', 'hierarchy', 'normalize'):
                base = f'state/stages/{stage}/{key}/{number}'
                files.update((base + '.json', base + '.dependencies.json'))
            for name in ('gathered', 'raw_tree', 'normalized_tree'):
                files.add(f'parsed/{key}/v{number}/{name}.json')
        selected[record['arxiv_id']] = files

    files = sorted(set().union(*selected.values()) if selected else set())
    # Path.resolve on Windows can race with another worker creating ancestors.
    # Validate/create the local directories before parallel file copies start.
    for parent in sorted({Path(relative).parent.as_posix() for relative in files}):
        safe_path(destination, parent).mkdir(parents=True, exist_ok=True)

    def copy_one(relative):
        try:
            original = safe_path(source, relative)
            if not original.is_file():
                return relative, 'missing'
            if original.stat().st_size > 128 * 1024 * 1024:
                raise ValueError('Legacy evidence exceeds size limit')
            before = original.stat()
            shutil.copyfile(original, safe_path(destination, relative))
            after = original.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError('Legacy evidence changed while copying: ' + relative)
            return relative, None
        except (ValueError, OSError) as exc:
            return relative, str(exc)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        failures = {p: reason for p, reason in pool.map(copy_one, files) if reason}
    missing = {paper: files & failures.keys() for paper, files in selected.items()}
    issues = []
    if any(missing.values()):
        catalog = {} if catalog is None else catalog
        if 'journal' not in catalog:
            try:
                catalog['journal'] = _load_journal(source)
            except (ValueError, TypeError, KeyError, OSError, AttributeError) as exc:
                catalog['journal'] = {}
                catalog['error'] = 'Legacy journal unreadable: ' + str(exc)
        journal = catalog['journal']
        if catalog.get('error'):
            issues.append(dict(reason=catalog['error']))
        packages = {}
        for paper, files in missing.items():
            receipt = journal.get(paper)
            if files and receipt:
                archive = receipt.get('archive')
                if not isinstance(archive, str):
                    issues.append(dict(paper_id=paper, reason='Legacy receipt has no archive path'))
                    continue
                packages.setdefault(archive, (receipt, set()))[1].update(selected[paper])
        for receipt, files in packages.values():
            try:
                archive = _cached_archive(source, receipt, destination.parent / 'legacy-archive-cache')
                with zipfile.ZipFile(archive) as bundle:
                    names = Counter(info.filename for info in bundle.infolist())
                    # Read a complete set from a receipt, rather than mix revisions.
                    for relative in sorted(files):
                        target = safe_path(destination, relative)
                        target.unlink(missing_ok=True)
                        if names[relative] != 1:
                            failures[relative] = 'ZIP evidence missing or duplicated'
                            continue
                        info = bundle.getinfo(relative)
                        if info.file_size > 128 * 1024 * 1024 or (info.external_attr >> 16) & 0o170000 == 0o120000:
                            raise ValueError('Invalid legacy ZIP member: ' + relative)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with bundle.open(info) as src, target.open('wb') as dst:
                            shutil.copyfileobj(src, dst)
                        failures.pop(relative, None)
            except (ValueError, TypeError, KeyError, OSError, zipfile.BadZipFile) as exc:
                issues.append(dict(archive=receipt.get('archive'), reason=str(exc)))
    # Missing evidence is expected for unfinished versions; retain counts only.
    issues.extend(dict(path=p, reason=reason) for p, reason in failures.items() if reason != 'missing')
    return dict(staging_issues=issues, missing_files=sum(reason == 'missing' for reason in failures.values()))


def load_completed(output, identity, records, journal, previous_config, workspace, workers):
    """Do not reopen known failed contextual ZIPs or reimport the entire legacy tree."""
    saved = parse._read(safe_path(output, 'completion-index.json'))
    selected = {r['arxiv_id']: r for r, _, conflict in records if not conflict}
    def matches(paper, entry):
        return (paper in selected and isinstance(entry, dict)
                and entry.get('record_hash') == parse._digest(selected[paper])
                and isinstance(entry.get('outcome'), dict)
                and entry['outcome'].get('complete') is True
                and entry['outcome'].get('status') in parse.TERMINAL)
    entries = {}
    if saved is not None:
        if not isinstance(saved, dict) or saved.get('format') != 'ms2-completion-index-1':
            raise ValueError('Unreadable completion index')
        if saved.get('context') == identity:
            entries = {p: e for p, e in saved['papers'].items() if matches(p, e)}
        previous_config = saved.get('origin') or {}
    for paper, receipt in journal.items():
        if receipt.get('context') == identity:
            entry = receipt.get('completed', {}).get(paper)
            if matches(paper, entry):
                entries[paper] = entry
            else:
                entries.pop(paper, None)
    previous_job = previous_config.get('job', {}) if isinstance(previous_config, dict) else {}
    compatible = (previous_config.get('processing_revision') == identity['processing_revision']
                  or (previous_config.get('code_sha') in completion_index._LEGACY_SHAS
                      and identity['processing_revision'] == completion_index._LEGACY_REVISION))
    same_source = (previous_job.get('input')
                   and str(Path(previous_job['input']).resolve()) == identity['source'])
    same_snapshot = previous_job.get('snapshot_id', identity['source']) == identity['snapshot_id']
    if not (compatible and same_source and same_snapshot):
        return entries
    catalog_path = safe_path(output, 'optimized-legacy-catalog.json')
    catalog = parse._read(catalog_path, cache=True)
    if not isinstance(catalog, dict) or catalog.get('context') != identity:
        catalog = dict(context=identity, archives={}, loose_done=False)
    archives = {}
    for paper in selected.keys() - entries.keys():
        receipt = journal.get(paper)
        # A contextual receipt already says whether its paper is complete.
        if receipt and receipt.get('context') is None:
            archives.setdefault(receipt['archive'], (receipt, []))[1].append(paper)
    for receipt, papers in archives.values():
        digest = receipt['sha256']
        imported = catalog['archives'].get(digest)
        # Cache attempted papers too: an empty certificate set is a valid negative result.
        if not isinstance(imported, dict) or not set(papers).issubset(imported['attempted']):
            print('Importing legacy archive once:', receipt['archive'], flush=True)
            archive = _cached_archive(output, receipt, workspace / 'archive-cache')
            imported = dict(attempted=list(receipt['paper_files']), entries={})
            with zipfile.ZipFile(archive) as bundle:
                for paper in receipt['paper_files']:
                    if paper not in selected:
                        continue
                    key = paper_key(paper)
                    try:
                        values = [json.loads(bundle.read(relative)) for relative in (
                            f'state/parsing/{key}.json', f'tables/{key}/parsing_manifest.json',
                            f'validation/parsing/{key}.json')]
                    except (KeyError, ValueError):
                        continue
                    entry = completion_index._certificate(selected[paper], *values)
                    if entry:
                        imported['entries'][paper] = entry
            catalog['archives'][digest] = imported
        entries.update({p: e for p, e in imported['entries'].items()
                        if p in papers and matches(p, e)})
    if not catalog['loose_done']:
        missing = selected.keys() - entries.keys() - journal.keys()
        missing_keys = {paper_key(x) for x in missing}
        paths = [p for p in safe_path(output, 'state/parsing').glob('*.json')
                 if p.stem in missing_keys]
        def import_one(path):
            outcome = parse._read(path, cache=True)
            if not isinstance(outcome, dict) or outcome.get('complete') is not True:
                return None
            paper = outcome.get('arxiv_id')
            if paper not in missing:
                return None
            key = paper_key(paper)
            entry = completion_index._certificate(selected[paper], outcome,
                parse._read(safe_path(output, f'tables/{key}/parsing_manifest.json'), cache=True),
                parse._read(safe_path(output, f'validation/parsing/{key}.json'), cache=True))
            return (paper, entry) if entry else None
        print(f'Importing {len(paths)} unindexed legacy records once', flush=True)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for result in pool.map(import_one, paths):
                if result:
                    entries[result[0]] = result[1]
                    catalog.setdefault('loose_entries', {})[result[0]] = result[1]
        catalog['loose_done'] = True
    entries.update({p: e for p, e in catalog.get('loose_entries', {}).items()
                    if p not in journal and matches(p, e)})
    atomic_json(catalog_path, catalog)
    return entries


@contextmanager
def _persistent_workspace(root, output, identity, identifiers):
    # An interrupted runtime can reuse its downloaded archives. No Drive data is removed.
    token = parse._digest(dict(output=str(output), context=identity, ids=identifiers))[:20]
    path = root / ('optimized-' + token)
    path.mkdir(parents=True, exist_ok=True)
    yield str(path)


def _save_batch(local, output, papers, directory, *, identity, completed):
    paper_files = {paper: sorted(_paper_files(local, paper)[0]) for paper in papers}
    name = utc_now().replace(':', '-') + '-' + uuid4().hex
    archive = directory / 'checkpoint.zip'
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=1) as bundle:
        for relative in sorted({p for values in paper_files.values() for p in values}):
            bundle.write(safe_path(local, relative), relative)
    relative = f'local-batches/{name}.zip'
    receipt = dict(format='ms2-local-batch-1', archive=relative, sha256=_hash(archive),
                   paper_files=paper_files, context=identity, completed=completed,
                   optimization_revision='20261113-io-v1')
    _publish_archive(archive, safe_path(output, relative))
    atomic_json(output / 'local-batches' / f'{name}.json', receipt)
    return receipt


def _timed(label, function, *args, **kwargs):
    start = perf_counter()
    try:
        return function(*args, **kwargs)
    finally:
        print(f'  {label}: {perf_counter() - start:.2f}s', flush=True)


def _validate_context(output, identity, previous_config):
    """Reject a different snapshot or producer before changing any Drive files."""
    if previous_config is not None and not isinstance(previous_config, dict):
        raise ValueError('Unreadable legacy context; preserve the existing output')
    saved = parse._read(safe_path(output, 'completion-index.json'))
    if saved is not None:
        if (not isinstance(saved, dict) or saved.get('format') != 'ms2-completion-index-1'
                or not isinstance(saved.get('papers'), dict) or saved.get('context') != identity):
            raise ValueError('Completion index context differs; preserve this output and select its original snapshot/runtime')
    elif previous_config:
        job = previous_config.get('job', {})
        compatible = (previous_config.get('processing_revision') == identity['processing_revision']
                      or (previous_config.get('code_sha') in completion_index._LEGACY_SHAS
                          and identity['processing_revision'] == completion_index._LEGACY_REVISION))
        if (not job.get('input') or str(Path(job['input']).resolve()) != identity['source']
                or job.get('member') != identity['member']
                or job.get('snapshot_id', identity['source']) != identity['snapshot_id']
                or not compatible):
            raise ValueError('Legacy output context differs; preserve it and use a separate output for this snapshot/runtime')
    for receipt in _load_journal(output).values():
        if receipt.get('context') is not None and receipt['context'] != identity:
            raise ValueError('Checkpoint receipt context differs; preserve the existing output')


def run(job, output, *, local_root, code_sha, workers=8, batch_size=50, check_resume=False, finalize=True):
    """Copy frozen inputs, parse locally, commit each batch, and export one ZIP.

    Existing PR #20 Drive files and local-batches archives are both supported.
    The caller must stop other writers on the selected input and output roots.
    """
    source, output = parse.validate_roots(job['input'], output)
    legacy = Path(job['legacy_output']).resolve() if job.get('legacy_output') else None
    previous_config = parse._read(safe_path(output, 'acceptance-config.json'))
    identity = completion_index.context(source, job)
    _validate_context(output, identity, previous_config)
    local_root = Path(local_root).resolve()
    for boundary in (source, output):
        if local_root.is_relative_to(boundary) or boundary.is_relative_to(local_root):
            raise ValueError('Local workspace must be disjoint from input and Drive output')
    if legacy is not None:
        for boundary in (source, output, local_root):
            if legacy.is_relative_to(boundary) or boundary.is_relative_to(legacy):
                raise ValueError('Legacy MS2 input must be disjoint from current input/output/workspace')
        validate_legacy(legacy, job['member'])
    if type(workers) is not int or not 1 <= workers <= 32:
        raise ValueError('workers must be between 1 and 32')
    if type(batch_size) is not int or batch_size < 1:
        raise ValueError('batch_size must be positive')
    assigned = set(expand_range(job['assignment_range']))
    if job['total_assigned'] != len(assigned):
        raise ValueError('Assignment range and denominator differ')
    identifiers = sorted(assigned) if job['ids'] is None else list(dict.fromkeys(job['ids']))
    if not identifiers or not set(identifiers).issubset(assigned):
        raise ValueError('Selected IDs must belong to the assigned range')
    if job['member'] not in {'tv1', 'tv2', 'tv3', 'tv4', 'tv5'}:
        raise ValueError('Unknown member')
    output.mkdir(parents=True, exist_ok=True)
    local_root.mkdir(parents=True, exist_ok=True)
    if not safe_path(output, 'completion-index.json').exists():
        # Preserve the actual legacy producer before replacing run configuration,
        # including if discovery copy is interrupted on the first migration.
        completion_index.save(output, identity, {}, origin=previous_config)
    atomic_json(output / 'acceptance.json', dict(
        member=job['member'], code_sha=code_sha, complete=False,
        full_assignment_accepted=False, status='running'))
    config = dict(job=job, code_sha=code_sha, resume=True, frozen_input=True,
                  storage_mode='colab-local-batches', copy_workers=workers, batch_size=batch_size,
                  optimization_revision='20261113-io-v1',
                  processing_revision=identity['processing_revision'])
    atomic_json(output / 'acceptance-config.json', config)
    atomic_json(output / 'delivery.json', dict(status='running', code_sha=code_sha))
    started = perf_counter()
    print(f'Reading discovery for {len(identifiers)} IDs', flush=True)
    journal = _load_journal(output)
    parts, all_stages, issues = [], [], []
    resume_events = []
    resume_ok = True
    cached_stages = total_stages = migrated_stages = 0
    legacy_catalog = {}
    with _persistent_workspace(local_root, output, identity, identifiers) as temporary:
        workspace = Path(temporary)
        local = workspace / 'output'
        local.mkdir(exist_ok=True)
        with MemorySampler() as sampler:
            discovery_root = workspace / 'discovery'
            discovery_root.mkdir(exist_ok=True)
            _discovery_index(source, output, discovery_root, workspace, identity, workers)
            records, input_errors, _ = parse._discover(discovery_root, identifiers)
            completed = load_completed(
                output, identity, records, journal, previous_config, workspace, workers)
            skipped = [r['arxiv_id'] for r, _, conflict in records
                       if not conflict and r['arxiv_id'] in completed]
            pending = [(r, p, c) for r, p, c in records if c or r['arxiv_id'] not in completed]
            completion_index.save(output, identity, completed, origin=previous_config)
            pending.sort(key=lambda item: item[0]['arxiv_id'] in journal)
            if skipped:
                parts.append(dict(paper_results=[completed[p]['outcome'] for p in skipped],
                                  excluded_inputs=[], stage_statuses={}))
            retries = sum(r['arxiv_id'] in journal for r, _, _ in pending)
            print(f'{len(skipped)} completed papers skipped | {len(pending) - retries} new IDs | {retries} retry IDs', flush=True)
            try:
                for offset in range(0, len(pending), batch_size):
                    batch = pending[offset:offset + batch_size]
                    papers = [r['arxiv_id'] for r, _, _ in batch]
                    batch_started = perf_counter()
                    with TemporaryDirectory(prefix='input-', dir=workspace) as batch_dir:
                        staged = Path(batch_dir)
                        print(f'{len(skipped) + offset}/{len(records)} IDs | copying {len(batch)} pending papers with {workers} workers', flush=True)
                        _timed('input inventory + copy', _stage_inputs, source, staged, batch, workers, discovery_root=discovery_root)
                        _timed('checkpoint restore', _restore, output, local, papers, journal, workers)
                        migration = None
                        candidates = stage_migration.pending_versions(staged, local, batch) if legacy is not None else set()
                        if candidates:
                            with TemporaryDirectory(prefix='legacy-', dir=workspace) as legacy_dir:
                                legacy_local = Path(legacy_dir)
                                staging = _timed('legacy evidence copy', _stage_legacy,
                                    legacy, legacy_local, batch, workers, catalog=legacy_catalog, versions_filter=candidates)
                                migration = _timed('verify legacy stages', stage_migration.prepare_batch,
                                    staged, local, legacy_local, batch)
                                atomic_json(output / f'migration-reports/{uuid4().hex}.json', dict(
                                    format=migration['format'], timestamp=utc_now(), legacy_source=str(legacy),
                                    papers=papers, decisions=migration['decisions'], **staging))
                                if staging['missing_files'] or staging['staging_issues']:
                                    print(f"  legacy evidence incomplete: {staging['missing_files']} missing files, "
                                          f"{len(staging['staging_issues'])} read issues; unproven stages will be recomputed", flush=True)
                        events = []
                        def paper_progress(index, total, paper, status):
                            print(f'  finished {index}/{total} | {paper} | {status}', flush=True)
                            if index < len(papers):
                                print(f'  parsing {index + 1}/{total} | {papers[index]}', flush=True)

                        if papers:
                            print(f'  parsing 1/{len(papers)} | {papers[0]}', flush=True)
                        summary = _timed('parse + validation', parse.run,
                            papers, local, input_root=staged, resume=True,
                            frozen_input=True, observer=events.append, paper_observer=paper_progress)
                        if migration is not None:
                            events = stage_migration.merge_events(migration['events'], events)
                            count = sum(bool(e.get('migrated')) for e in events)
                            reasons = Counter(d['reason'] for d in migration['decisions'] if d['action'] == 'recompute')
                            print(f'  legacy stages reused: {count} | recompute reasons: {dict(reasons)}', flush=True)
                        stages = json.loads((local / 'manifests/stages.json').read_text(encoding='utf-8'))
                        for stage in stages:
                            if stage['status'] not in parse.TERMINAL:
                                issues.append(dict(stage=stage['stage'], paper_id=stage['paper_id'],
                                                   version=stage['version'], status=stage['status'],
                                                   error=stage.get('error')))
                            if stage['stage'] == 'validate_parse' and stage.get('artifacts', {}).get('validation'):
                                report = parse._read(safe_path(local, stage['artifacts']['validation']))
                                issues.extend(dict(stage='validate_parse', **issue)
                                              for issue in report['errors'] + report['warnings'])
                        if check_resume:
                            verified_events = []
                            verified = parse.run(
                                papers, local, input_root=staged, resume=True,
                                frozen_input=True, observer=verified_events.append)
                            resume_ok = bool(resume_ok and verified['complete'] and verified_events
                                             and all(event['cached'] for event in verified_events))
                            resume_events.extend(verified_events)
                        receipt = None
                        certified = {}
                        by_id = {r['arxiv_id']: r for r, _, _ in batch}
                        for outcome in summary['paper_results']:
                            if outcome['complete']:
                                paper = outcome['arxiv_id']
                                manifest = parse._read(safe_path(local, outcome['artifacts']['parsing_manifest']))
                                report = parse._read(safe_path(local, f'validation/parsing/{paper_key(paper)}.json'))
                                entry = completion_index._certificate(by_id[paper], outcome, manifest, report)
                                if entry:
                                    certified[paper] = entry
                        if not (all(e['cached'] for e in events) and all(p in journal for p in papers)):
                            receipt = _timed('checkpoint ZIP + upload', _save_batch, local, output, papers, staged,
                                                  identity=identity, completed=certified)
                    if receipt is not None:
                        for paper in papers:
                            journal[paper] = receipt
                    for paper in papers:
                        completed.pop(paper, None)
                    completed.update(certified)
                    if (offset // batch_size + 1) % 5 == 0:
                        completion_index.save(output, identity, completed)
                    parts.append(summary)
                    all_stages.extend(stages)
                    cached_stages += sum(e['cached'] for e in events)
                    total_stages += len(events)
                    migrated_stages += sum(bool(e.get('migrated')) for e in events)
                    merged = _merge_summary(parts, len(identifiers), input_errors)
                    atomic_json(output / 'optimized-progress.json', dict(
                        timestamp=utc_now(), processed_ids=merged['processed_ids'],
                        certified_papers=len(completed), selected_ids_count=len(identifiers),
                        migrated_stages=migrated_stages,
                        incomplete_ids=[p['arxiv_id'] for p in merged['paper_results'] if not p['complete']],
                        elapsed_seconds=round(perf_counter() - started, 2)))
                    print(f"{merged['processed_ids']}/{len(identifiers)} IDs | saved to Drive | "
                          f"{(perf_counter() - batch_started) / len(batch):.2f} s/ID | "
                          f"{sum(e['cached'] for e in events)}/{len(events)} stages cached", flush=True)
            except BaseException:
                atomic_json(output / 'parsing_summary.json', _merge_summary(parts, len(identifiers), input_errors))
                completion_index.save(output, identity, completed)
                raise
            completion_index.save(output, identity, completed)
            merged = _merge_summary(parts, len(identifiers), input_errors)
            atomic_json(output / 'parsing_summary.json', merged)
            if not finalize:
                progress = dict(status='checkpointed_not_exported', certified_papers=len(completed),
                    migrated_stages=migrated_stages, cached_stages=cached_stages, total_stages=total_stages,
                    processed_ids=merged['processed_ids'], complete=merged['complete'],
                    full_assignment_accepted=False, incomplete_papers=[p['arxiv_id']
                        for p in merged['paper_results'] if not p['complete']])
                atomic_json(output / 'optimized-processing-receipt.json', progress)
                atomic_json(output / 'delivery.json', dict(status='checkpointed_not_exported'))
                return progress
            peak, average, _ = sampler.stop()

        # Only after pending IDs are handled, read previous output for the final
        # complete handoff ZIP. No MS1 input of a skipped paper is copied.
        for offset in range(0, len(skipped), batch_size):
            papers = skipped[offset:offset + batch_size]
            print(f'Final export: collecting completed output {offset + len(papers)}/{len(skipped)}', flush=True)
            _restore(output, local, papers, journal, workers)
            for paper in papers:
                for stage in parse.MODULES:
                    for checkpoint in safe_path(local, f'state/stages/{stage}/{paper_key(paper)}').glob('*.json'):
                        if not checkpoint.name.endswith('.dependencies.json'):
                            all_stages.append(parse._read(checkpoint))
                report = parse._read(safe_path(local, f'validation/parsing/{paper_key(paper)}.json'))
                issues.extend(dict(stage='validate_parse', **issue)
                              for issue in report['errors'] + report['warnings'])
        merged = _merge_summary(parts, len(identifiers), input_errors)
        merged['stage_statuses'] = dict(Counter(s['status'] for s in all_stages))
        merged['skipped_completed_papers'] = len(skipped)
        accounted = {p['arxiv_id'] for p in merged['paper_results']
                     if p.get('reason') != 'MS1 discovery record is missing'}
        accounted.update(p.get('arxiv_id') or p.get('input_id') for p in merged['excluded_inputs'])
        acceptance = dict(
            member=job['member'], code_sha=code_sha, scope='all_assigned_ids' if job['ids'] is None else 'selected_sample',
            total_assigned=job['total_assigned'], selected_ids=job['ids'],
            attempted_papers=merged['papers'], accounted_inputs=len(accounted),
            unseen_assigned_ids=sorted(assigned - accounted), out_of_range_ids=sorted(accounted - assigned),
            complete=merged['complete'], input_errors=input_errors,
            paper_statuses=dict(Counter(p['status'] for p in merged['paper_results'])),
            version_statuses=dict(Counter(v['status'] for p in merged['paper_results'] for v in p['versions'])),
            incomplete_papers=[p['arxiv_id'] for p in merged['paper_results'] if not p['complete']],
            resume_enabled=True, resume_all_cached=resume_ok if check_resume and resume_events else None,
            cached_stages=cached_stages,
            migrated_stages=migrated_stages,
            total_stages=total_stages, issues=issues, storage_mode='colab-local-batches',
            skipped_completed_papers=len(skipped),
            full_assignment_accepted=bool(job['ids'] is None and merged['complete'] and accounted == assigned),
        )
        atomic_json(local / 'parsing_summary.json', merged)
        atomic_json(local / 'manifests/parsing.json', merged['paper_results'])
        atomic_json(local / 'manifests/stages.json', all_stages)
        atomic_json(local / 'summaries/parse-run.json', merged)
        if check_resume:
            atomic_json(local / 'summaries/resume-events.json', resume_events)
        atomic_json(local / 'acceptance-config.json', config)
        atomic_json(local / 'acceptance.json', acceptance)
        # Recompute totals from the complete local handoff, including skipped
        # papers, without loading their source archives.
        aggregate = generate_parsing_summary(
            local, wall_time_sec=perf_counter() - started, peak_ram_mb=peak,
            avg_ram_mb=average, total_assigned=job['total_assigned'])
        benchmark = aggregate
        benchmark['measurement_scope'] = 'discovery, pending processing and previous output collection; ZIP export measured separately'
        benchmark['ram_measurement_scope'] = 'discovery and pending processing'
        atomic_json(local / 'summaries/parsing_summary.json', benchmark)
        archive = workspace / 'results.zip'
        export_started = perf_counter()
        with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=1) as bundle:
            for file in sorted(local.rglob('*')):
                if file.is_file():
                    bundle.write(file, job['member'] + '/' + file.relative_to(local).as_posix())
        destination = safe_path(output, 'MS2_RESULTS.zip')
        _publish_archive(archive, destination)
        for relative in ('parsing_summary.json', 'acceptance-config.json', 'acceptance.json',
                         'manifests/parsing.json', 'manifests/stages.json',
                         'summaries/parse-run.json', 'summaries/parsing_summary.json'):
            path = safe_path(output, relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(safe_path(local, relative), path)
        atomic_json(output / 'delivery.json', dict(
            status='published', archive='MS2_RESULTS.zip', sha256=_hash(archive), code_sha=code_sha,
            final_export_seconds=round(perf_counter() - export_started, 2),
            total_elapsed_seconds=round(perf_counter() - started, 2)))
        print(f"Result ZIP saved: {destination}", flush=True)
        return acceptance
