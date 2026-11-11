"""Bounded local MS2 processing with durable, checksummed Drive batch archives."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
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
from arxiv_pipeline.pipeline import parse, completion_index
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


def _stage_inputs(source, destination, records, workers, *, discovery_root):
    files, directories = set(), set()
    discovery_files = set()
    for record, paths, conflict in records:
        for path in paths:
            discovery_files.add(path.relative_to(discovery_root).as_posix())
        if conflict or record.get('status') != 'success':
            continue
        try:
            versions = parse._versions(record)
        except (ValueError, TypeError, KeyError):
            continue  # The parser records malformed discovery as incomplete.
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
    _copy_files(source, destination, files, directories, workers)
    _copy_files(discovery_root, destination, discovery_files, set(), workers)


def _load_journal(output):
    papers = {}
    for path in sorted(safe_path(output, 'local-batches').glob('*.json')):
        receipt = json.loads(path.read_text(encoding='utf-8'))
        if receipt.get('format') != 'ms2-local-batch-1':
            raise ValueError(f'Unknown local checkpoint receipt: {path.name}')
        for paper in receipt['paper_files']:
            papers[paper] = receipt
    return papers


def _restore(output, local, papers, journal, workers):
    packages = {}
    for paper in papers:
        if paper in journal:
            receipt = journal[paper]
            package = packages.setdefault(receipt['archive'], (receipt, set()))
            package[1].update(receipt['paper_files'][paper])
        else:
            files, directories = _paper_files(output, paper)
            _copy_files(output, local, files, directories, workers)
    for receipt, selected in packages.values():
        with TemporaryDirectory(prefix='restore-', dir=local.parent) as temporary:
            archive = Path(temporary) / 'batch.zip'
            shutil.copyfile(safe_path(output, receipt['archive']), archive)
            if _hash(archive) != receipt['sha256']:
                raise ValueError(f"Checkpoint archive checksum mismatch: {receipt['archive']}")
            with zipfile.ZipFile(archive) as bundle:
                for relative in selected:
                    target = safe_path(local, relative)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.open(relative) as src, target.open('wb') as dst:
                        shutil.copyfileobj(src, dst)


def _publish_archive(local_archive, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = safe_path(destination.parent, destination.name + '.partial')
    shutil.copyfile(local_archive, temporary)
    temporary.replace(destination)


def _save_batch(local, output, papers, directory, *, identity, completed):
    paper_files = {paper: sorted(_paper_files(local, paper)[0]) for paper in papers}
    name = utc_now().replace(':', '-') + '-' + uuid4().hex
    archive = directory / 'checkpoint.zip'
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as bundle:
        for relative in sorted({p for values in paper_files.values() for p in values}):
            bundle.write(safe_path(local, relative), relative)
    relative = f'local-batches/{name}.zip'
    receipt = dict(format='ms2-local-batch-1', archive=relative,
                   sha256=_hash(archive), paper_files=paper_files,
                   context=identity, completed=completed)
    _publish_archive(archive, safe_path(output, relative))
    # The receipt commits the archive. An interrupted .partial/uncommitted ZIP
    # cannot be mistaken for a completed checkpoint batch.
    atomic_json(output / 'local-batches' / f'{name}.json', receipt)
    return receipt


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


def run(job, output, *, local_root, code_sha, workers=32, batch_size=25, check_resume=False):
    """Copy frozen inputs, parse locally, commit each batch, and export one ZIP.

    Existing PR #20 Drive files and local-batches archives are both supported.
    The caller must stop other writers on the selected input and output roots.
    """
    source, output = parse.validate_roots(job['input'], output)
    previous_config = parse._read(safe_path(output, 'acceptance-config.json'), cache=True)
    identity = completion_index.context(source, job)
    local_root = Path(local_root).resolve()
    for boundary in (source, output):
        if local_root.is_relative_to(boundary) or boundary.is_relative_to(local_root):
            raise ValueError('Local workspace must be disjoint from input and Drive output')
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
                  processing_revision=identity['processing_revision'])
    atomic_json(output / 'acceptance-config.json', config)
    atomic_json(output / 'delivery.json', dict(status='running', code_sha=code_sha))
    started = perf_counter()
    print(f'Reading discovery for {len(identifiers)} IDs', flush=True)
    journal = _load_journal(output)
    parts, all_stages, issues = [], [], []
    resume_events = []
    resume_ok = True
    cached_stages = total_stages = 0
    with TemporaryDirectory(prefix='ms2-run-', dir=local_root) as temporary:
        workspace = Path(temporary)
        local = workspace / 'output'
        local.mkdir()
        with MemorySampler() as sampler:
            discovery_root = workspace / 'discovery'
            discovery_root.mkdir()
            _discovery_index(source, output, discovery_root, workspace, identity, workers)
            records, input_errors, _ = parse._discover(discovery_root, identifiers)
            completed = completion_index.load(
                output, identity, records, journal, previous_config, workspace, workers)
            skipped = [r['arxiv_id'] for r, _, conflict in records
                       if not conflict and r['arxiv_id'] in completed]
            pending = [(r, p, c) for r, p, c in records if c or r['arxiv_id'] not in completed]
            completion_index.save(output, identity, completed, origin=previous_config)
            if skipped:
                parts.append(dict(paper_results=[completed[p]['outcome'] for p in skipped],
                                  excluded_inputs=[], stage_statuses={}))
            print(f'{len(skipped)} completed papers skipped | {len(pending)} IDs pending', flush=True)
            for offset in range(0, len(pending), batch_size):
                batch = pending[offset:offset + batch_size]
                papers = [r['arxiv_id'] for r, _, _ in batch]
                batch_started = perf_counter()
                with TemporaryDirectory(prefix='input-', dir=workspace) as batch_dir:
                    staged = Path(batch_dir)
                    print(f'{len(skipped) + offset}/{len(records)} IDs | copying {len(batch)} pending papers with {workers} workers', flush=True)
                    _stage_inputs(source, staged, batch, workers, discovery_root=discovery_root)
                    _restore(output, local, papers, journal, workers)
                    events = []
                    summary = parse.run(
                        papers, local, input_root=staged, resume=True,
                        frozen_input=True, observer=events.append)
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
                        receipt = _save_batch(local, output, papers, staged,
                                              identity=identity, completed=certified)
                if receipt is not None:
                    for paper in papers:
                        journal[paper] = receipt
                for paper in papers:
                    completed.pop(paper, None)
                completed.update(certified)
                completion_index.save(output, identity, completed)
                parts.append(summary)
                all_stages.extend(stages)
                cached_stages += sum(e['cached'] for e in events)
                total_stages += len(events)
                merged = _merge_summary(parts, len(identifiers), input_errors)
                atomic_json(output / 'parsing_summary.json', merged)
                print(f"{merged['processed_ids']}/{len(identifiers)} IDs | saved to Drive | "
                      f"{(perf_counter() - batch_started) / len(batch):.2f} s/ID | "
                      f"{sum(e['cached'] for e in events)}/{len(events)} stages cached", flush=True)
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
        with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as bundle:
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
