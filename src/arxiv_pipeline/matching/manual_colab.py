"""Local staging and publication for the T3-04 Colab notebook.

Calls the existing workflow CLI. It never accepts labels, trains a model,
downloads papers, or rewrites the team's annotation guide.
"""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import stat
import uuid
import zipfile

from arxiv_pipeline.common.utils import atomic_json, paper_key
from arxiv_pipeline.matching.contracts import ContractError
from arxiv_pipeline.matching.labeling import load_labels_json
from arxiv_pipeline.matching.manual_workflow import (
    audit_manual_labels, load_prepared, main as workflow_main, validate_assignments,
)


@dataclass(frozen=True)
class RunConfig:
    input_source: Path
    assignments_file: Path
    workbook_folder: Path
    output_root: Path
    local_root: Path
    input_mode: str = 'staged_zip'
    members: tuple[str, ...] = ('tv1', 'tv2', 'tv3', 'tv4', 'tv5')
    file_pattern: str = 'annotation_{member}_reviewed.xlsx'
    origin: str = 'human'
    mode: str = 'reviewed'
    run_id: str = ''


@dataclass(frozen=True)
class RunResult:
    root: Path
    run_id: str


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for part in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(part)
    return digest.hexdigest()


def _read(path: Path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def _safe(root: Path, relative: str) -> Path:
    posix = PurePosixPath(relative)
    path = root.joinpath(*posix.parts).resolve()
    if posix.is_absolute() or '..' in posix.parts or '\\' in relative or not path.is_relative_to(root.resolve()):
        raise ContractError(f'Unsafe input path: {relative}')
    return path


def _copy(source: Path, target: Path):
    before = _sha(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open('rb') as reader, target.open('xb') as writer:
        for part in iter(lambda: reader.read(1024 * 1024), b''):
            writer.write(part)
    if before != _sha(target) or before != _sha(source):
        raise ContractError(f'Input changed while copying: {source.name}')
    return before


def extract_staged_zip(source: Path, target: Path, assignments: dict):
    """Allow exactly the selected staged artifacts, with no ZIP extraction API."""
    names = ('bibtex.json', 'references.json', 'ms1_validation.json',
        'ms2_validation.json', 'input_manifest.json')
    allowed = {f'{member}/{paper_key(pid)}/{name}' for member, ids in assignments.items()
        for pid in ids for name in names}
    with zipfile.ZipFile(source) as archive:
        files = [i for i in archive.infolist() if not i.is_dir()]
        if sum(i.file_size for i in files) > 128 * 1024 * 1024:
            raise ContractError('Staged ZIP exceeds the 128 MiB uncompressed limit')
        if len(files) != len(allowed) or {i.filename for i in files} != allowed:
            raise ContractError('ZIP must contain exactly the selected member/paper staged artifacts')
        for info in files:
            path = _safe(target, info.filename)
            if stat.S_ISLNK(info.external_attr >> 16):
                raise ContractError('Symlinks are not allowed in staged ZIP')
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('xb') as handle:
                handle.write(archive.read(info))


def _copy_prepared(source: Path, target: Path, assignments: dict):
    source_manifest = source / 'prepared_manifest.json'
    before = _sha(source_manifest)
    manifest = _read(source_manifest)
    if manifest['assignments'] != assignments:
        raise ContractError('Prepared snapshot and assignment config differ')
    files = [(row['bundle'], row['sha256']) for row in manifest['publications']]
    files.extend((row['path'], row['sha256']) for row in manifest['forms'].values())
    for relative, expected in files:
        actual = _copy(_safe(source, relative), _safe(target, relative))
        if actual != expected:
            raise ContractError(f'Prepared artifact hash mismatch: {relative}')
    _copy(source_manifest, target / 'prepared_manifest.json')
    if _sha(source_manifest) != before:
        raise ContractError('Prepared manifest changed while copying')
    load_prepared(target)


def _invoke(arguments: list[str], log: Path) -> int:
    stream = io.StringIO()
    with redirect_stdout(stream), redirect_stderr(stream):
        code = workflow_main(arguments)
    message = stream.getvalue()
    with log.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(arguments, ensure_ascii=False) + '\n')
        handle.write(message + f'exit_code={code}\n')
    print(message.strip(), flush=True)
    if code not in {0, 1}:
        raise ContractError(f'T3-04 CLI failed (exit {code}); see {log.name}')
    return code


def run_audit(config: RunConfig, *, runtime_sha256: str, code_base_commit: str) -> RunResult:
    """Use a new run directory; a completed audit may still be progress-only."""
    if config.input_mode not in {'staged_zip', 'prepared_folder'} or config.mode not in {'progress', 'reviewed'}:
        raise ContractError('Unknown input mode/run mode')
    if config.origin not in {'human', 'ai_assisted'}:
        raise ContractError('Unknown annotation origin')
    if config.mode == 'reviewed' and config.origin != 'human':
        raise ContractError('Use progress mode for AI drafts')
    assignment_config = _read(config.assignments_file)
    if (assignment_config.get('schema_version'), assignment_config.get('task')) != ('3.0', 'T3-04'):
        raise ContractError('Expected T3-04 schema 3.0 assignment config')
    assignments = validate_assignments(assignment_config['assignments'])
    if not config.members or len(set(config.members)) != len(config.members) or set(config.members) - set(assignments):
        raise ContractError('Choose distinct members from tv1..tv5')
    workbooks = {}
    for member in config.members:
        name = config.file_pattern.format(member=member)
        if Path(name).name != name or not name.endswith('.xlsx'):
            raise ContractError('Workbook pattern must be a plain .xlsx filename')
        source = config.workbook_folder / name
        if not source.is_file():
            raise FileNotFoundError(f'Missing workbook: {source}')
        workbooks[member] = source
    if not config.input_source.exists():
        raise FileNotFoundError(f'Missing input snapshot: {config.input_source}')
    protected = [config.workbook_folder.resolve()]
    if config.input_mode == 'prepared_folder':
        protected.append(config.input_source.resolve())
    if any(root.resolve().is_relative_to(source) for root in (config.local_root, config.output_root) for source in protected):
        raise ContractError('Run/output roots must be separate from input folders')
    run_id = config.run_id or datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}', run_id):
        raise ContractError('Invalid run ID')
    root = config.local_root / run_id
    root.mkdir(parents=True, exist_ok=False)
    log = root / 'run.log'
    receipt = dict(schema_version='3.0', task='T3-04', run_id=run_id,
        runtime_sha256=runtime_sha256, code_base_commit=code_base_commit,
        mode=config.mode, origin=config.origin, members=list(config.members),
        status='running', handoff_ready=False, input_mode=config.input_mode)
    atomic_json(root / 'run_manifest.json', receipt)
    atomic_json(root / 'assignment_config.json', assignment_config)
    try:
        if config.input_mode == 'staged_zip':
            snapshot = root / 'input_snapshot.zip'
            receipt['input_sha256'] = _copy(config.input_source, snapshot)
            extract_staged_zip(snapshot, root / 'staged', assignments)
            code = _invoke(['prepare', '--assignments', str(root / 'assignment_config.json'),
                '--inputs', str(root / 'staged'), '--output', str(root / 'prepared')], log)
            if code != 0:
                raise ContractError('Snapshot preparation failed')
        else:
            _copy_prepared(config.input_source, root / 'prepared', assignments)
            receipt['input_sha256'] = _sha(root / 'prepared/prepared_manifest.json')
        arguments = ['collect', '--prepared', str(root / 'prepared'),
            '--origin', config.origin, '--output', str(root / 'collected')]
        for member, source in workbooks.items():
            target = root / 'workbooks' / source.name
            _copy(source, target)
            arguments += ['--workbook', f'{member}={target}']
        collect_code = _invoke(arguments, log)
        audit_code = _invoke(['audit', '--prepared', str(root / 'prepared'),
            '--labels', str(root / 'collected/labels.json'), '--output', str(root / 'recheck')], log)
        audit = _read(root / 'collected/manual_audit.json')
        if audit != _read(root / 'recheck/manual_audit.json'):
            raise ContractError('Collection audit and independent recheck disagree')
        receipt.update(status='complete', collect_exit_code=collect_code, audit_exit_code=audit_code,
            handoff_ready=bool(config.mode == 'reviewed' and config.origin == 'human'
                and collect_code == audit_code == 0 and audit['ready_for_handoff']))
        atomic_json(root / 'run_manifest.json', receipt)
        print(f"Run {run_id}: handoff_ready={receipt['handoff_ready']} accepted_entries={audit['accepted_entries']}")
        return RunResult(root, run_id)
    except Exception as exc:
        receipt.update(status='failed', handoff_ready=False, error=str(exc))
        atomic_json(root / 'run_manifest.json', receipt)
        raise


def publish_run(result: RunResult, output_root: Path | str) -> Path:
    """Revalidate immutable local inputs, then publish a new handoff/progress ZIP."""
    root = result.root
    receipt = _read(root / 'run_manifest.json')
    if receipt.get('status') != 'complete' or receipt.get('run_id') != result.run_id:
        raise ContractError('Only a completed run can be published')
    assignments, bundles = load_prepared(root / 'prepared')
    labels = load_labels_json(root / 'collected/labels.json')
    audit = audit_manual_labels(assignments, bundles, labels)
    manifest = _read(root / 'collected/label_manifest.json')
    if (audit != _read(root / 'collected/manual_audit.json')
            or manifest['labels_sha256'] != _sha(root / 'collected/labels.json')
            or manifest['prepared_manifest_sha256'] != _sha(root / 'prepared/prepared_manifest.json')):
        raise ContractError('Run artifacts changed after audit')
    for evidence in manifest['workbooks'].values():
        if _sha(_safe(root / 'workbooks', evidence['file'])) != evidence['sha256']:
            raise ContractError('Workbook changed after audit')
    handoff = bool(receipt['mode'] == 'reviewed' and receipt['origin'] == 'human'
        and audit['ready_for_handoff'] and receipt['collect_exit_code'] == receipt['audit_exit_code'] == 0)
    if handoff != receipt['handoff_ready']:
        raise ContractError('Run receipt and audited readiness disagree')
    output_root = Path(output_root)
    if output_root.resolve().is_relative_to(root.resolve()):
        raise ContractError('Publish outside the local run directory')
    target = output_root / result.run_id
    target.mkdir(parents=True, exist_ok=False)
    archive_path = target / ('handoff.zip' if handoff else 'progress.zip')
    with zipfile.ZipFile(archive_path, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob('*')):
            if path.is_file():
                if not path.resolve().is_relative_to(root.resolve()) or path.is_symlink():
                    raise ContractError('Run contains a file outside the run root')
                archive.write(path, path.relative_to(root).as_posix())
    for name, path in {
        'labels.json': root / 'collected/labels.json',
        'manual_audit.json': root / 'collected/manual_audit.json',
        'label_manifest.json': root / 'collected/label_manifest.json',
        'run_manifest.json': root / 'run_manifest.json',
        'run.log': root / 'run.log',
    }.items():
        _copy(path, target / name)
    atomic_json(target / 'published_manifest.json', dict(schema_version='3.0', run_id=result.run_id,
        handoff_ready=handoff, archive=archive_path.name, archive_sha256=_sha(archive_path),
        files={path.name: _sha(path) for path in target.iterdir() if path.is_file()}))
    print(f"Saved {'HANDOFF' if handoff else 'PROGRESS ONLY'}: {target}")
    return target
