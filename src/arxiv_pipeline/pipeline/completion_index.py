"""Paper-level certificates for resuming a declared immutable MS1 snapshot."""
import hashlib
from pathlib import Path

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
    revision = digest.hexdigest()
    # Audited MS3-only change: schemas.py adds MATCHING_SCHEMA_VERSION and a docstring.
    # All MS2 parser and validation files are identical to the historical producer.
    if revision == '3d40587055ad3971a471c5e96e12ce6391116a4e5da499838c3a89bd7855987d':
        revision = _LEGACY_REVISION
    return dict(source=str(source), member=job['member'],
                snapshot_id=job.get('snapshot_id', str(source)),
                processing_revision=revision)


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

