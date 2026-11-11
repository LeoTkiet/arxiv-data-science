"""Paper-level certificates for resuming a declared immutable MS1 snapshot."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import shutil
import zipfile

from arxiv_pipeline.common.utils import atomic_json, paper_key
from arxiv_pipeline.pipeline import parse
from arxiv_pipeline.reporting.milestone2.validation import safe_path

_LEGACY_SHAS = {
    'b932d98eae90d50611004ccc5e2d91709a77b7bf',
    '25c657c20642a8b2a18bea1417c081835f790c4a',
    '40103bacb5c34a85baecaa16fc78a856044961e8',
}
_LEGACY_REVISION = '7fa20282d5b07636d62f9e6e88fa995494c07d4df1c546afc41cf076aa75cd50'


def context(source, job):
    root = Path(parse.__file__).parents[1]
    paths = [root / 'pipeline/parse.py', root / 'reporting/milestone2/validation.py']
    paths += list((root / 'parsing').rglob('*.py')) + list((root / 'common').rglob('*.py'))
    digest = hashlib.sha256()
    for path in sorted(set(paths)):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes().replace(b'\r\n', b'\n'))
    return dict(source=str(source), member=job['member'],
                snapshot_id=job.get('snapshot_id', str(source)),
                processing_revision=digest.hexdigest())


def _certificate(record, outcome, manifest, report):
    if not all(isinstance(value, dict) for value in (outcome, manifest, report)):
        return None
    try:
        versions = [v['version'] for v in parse._versions(record)]
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    paper = record['arxiv_id']
    snapshot = manifest.get('input_snapshot_id')
    if (outcome.get('arxiv_id') != paper or outcome.get('complete') is not True
            or outcome.get('status') not in parse.TERMINAL
            or outcome.get('expected_versions') != versions
            or outcome.get('selected_versions') != versions
            or manifest.get('arxiv_id') != paper or manifest.get('schema_version') != '2.0'
            or manifest.get('complete') is not True or manifest.get('expected_versions') != versions
            or not isinstance(snapshot, str) or not snapshot
            or report.get('arxiv_id') != paper or report.get('schema_version') != '2.0'
            or report.get('valid') is not True or report.get('errors') != []
            or report.get('input_snapshot_id') != snapshot):
        return None
    return dict(record_hash=parse._digest(record), outcome=outcome, input_snapshot_id=snapshot)


def save(output, identity, entries, *, origin=None):
    previous = parse._read(safe_path(output, 'completion-index.json'), cache=True)
    if isinstance(previous, dict) and 'origin' in previous:
        origin = previous['origin']
    atomic_json(output / 'completion-index.json',
                dict(format='ms2-completion-index-1', context=identity, papers=entries, origin=origin))


def load(output, identity, records, journal, previous_config, workspace, workers):
    saved = parse._read(safe_path(output, 'completion-index.json'))
    selected = {r['arxiv_id']: r for r, _, conflict in records if not conflict}
    entries = {}
    if saved is not None:
        if not isinstance(saved, dict) or saved.get('format') != 'ms2-completion-index-1':
            raise ValueError('Unreadable completion index; preserve it for diagnosis')
        if saved.get('context') == identity:
            entries = {p: e for p, e in saved['papers'].items()
                       if p in selected and e.get('record_hash') == parse._digest(selected[p])
                       and isinstance(e.get('outcome'), dict)
                       and e['outcome'].get('complete') is True
                       and e['outcome'].get('status') in parse.TERMINAL}
        previous_config = saved.get('origin') or {}

    # A receipt written by this runner carries enough metadata to rebuild a
    # missing index without downloading or extracting its archive.
    for paper, receipt in journal.items():
        entry = receipt.get('completed', {}).get(paper)
        if (paper in selected and entry and receipt.get('context') == identity
                and entry.get('record_hash') == parse._digest(selected[paper])):
            entries[paper] = entry
        elif paper in entries and receipt.get('context') == identity:
            entries.pop(paper)  # A newer committed incomplete outcome wins.

    previous_job = previous_config.get('job', {}) if isinstance(previous_config, dict) else {}
    same_source = previous_job.get('input') and str(Path(previous_job['input']).resolve()) == identity['source']
    compatible = previous_config and (
        previous_config.get('processing_revision') == identity['processing_revision']
        or (previous_config.get('code_sha') in _LEGACY_SHAS
            and identity['processing_revision'] == _LEGACY_REVISION))
    same_snapshot = previous_job.get('snapshot_id', identity['source']) == identity['snapshot_id']
    if not (same_source and same_snapshot and compatible):
        return entries

    paths = list(safe_path(output, 'state/parsing').glob('*.json'))
    missing = set(selected) - entries.keys() - journal.keys()

    def import_legacy(path):
        outcome = parse._read(path, cache=True)
        if not isinstance(outcome, dict) or outcome.get('complete') is not True:
            return None
        paper = outcome.get('arxiv_id')
        if paper not in missing:
            return None
        key = paper_key(paper)
        manifest = parse._read(safe_path(output, f'tables/{key}/parsing_manifest.json'), cache=True)
        report = parse._read(safe_path(output, f'validation/parsing/{key}.json'), cache=True)
        entry = _certificate(selected[paper], outcome, manifest, report)
        return (paper, entry) if entry else None

    print(f'Importing completed legacy paper receipts ({len(paths)} small records)', flush=True)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for result in pool.map(import_legacy, paths):
            if result:
                entries[result[0]] = result[1]

    archives = {}
    for paper in selected.keys() - entries.keys():
        if paper in journal:
            receipt = journal[paper]
            archives.setdefault(receipt['archive'], (receipt, []))[1].append(paper)
    for number, (receipt, papers) in enumerate(archives.values()):
        local_archive = workspace / f'legacy-{number}.zip'
        shutil.copyfile(safe_path(output, receipt['archive']), local_archive)
        if parse._file_hash(local_archive) != receipt['sha256']:
            raise ValueError(f"Checkpoint archive checksum mismatch: {receipt['archive']}")
        with zipfile.ZipFile(local_archive) as bundle:
            for paper in papers:
                key = paper_key(paper)
                try:
                    values = [json.loads(bundle.read(relative)) for relative in (
                        f'state/parsing/{key}.json', f'tables/{key}/parsing_manifest.json',
                        f'validation/parsing/{key}.json')]
                except (KeyError, ValueError):
                    continue
                entry = _certificate(selected[paper], *values)
                if entry:
                    entries[paper] = entry
        local_archive.unlink()
    return entries
