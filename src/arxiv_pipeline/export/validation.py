"""Read-only corpus QA, using stage checkpoints available before manifests are built.

`success` means that artifacts and recorded outcomes are consistent, not that every
version has source. Missing source/index/data are counted separately in the report.
Duplicate source content is a warning (versions may legitimately share content).
"""

import hashlib
import json
from datetime import date
from pathlib import Path, PureWindowsPath

from arxiv_pipeline.common.schemas import SCHEMA_VERSION, ErrorInfo, StageResult
from arxiv_pipeline.common.utils import atomic_json, paper_key, utc_now
from arxiv_pipeline.crawling.metadata import build_metadata


def run(record, output_dir, *, version=None, upstream=None):
    """Validate one discovered paper and persist validation/<paper_key>.json.

    No API calls or corpus mutations. Unreadable/malformed inputs become structured
    errors; failure to write the report is left to the orchestrator's I/O handling.
    Paths are relative to output_dir; symlinks and paths escaping it are rejected.
    """
    output = Path(output_dir).resolve()
    paper_id = record['arxiv_id']
    key = paper_key(paper_id)
    errors, warnings, duplicates, outcomes = [], [], [], []
    counts = dict(versions=0, source_versions=0, no_source_versions=0,
                  source_files=0, references=0)

    def issue(code, path, message, number=None, *, warning=False):
        (warnings if warning else errors).append(dict(
            code=code, paper_id=paper_id, version=number, path=str(path), message=message))

    def safe_path(relative):
        if not isinstance(relative, str) or not relative:
            raise ValueError('Artifact path must be a non-empty string')
        candidate = Path(relative)
        if (candidate.is_absolute() or PureWindowsPath(relative).drive
                or '..' in relative.replace('\\', '/').split('/')):
            raise ValueError('Absolute/parent artifact paths are forbidden')
        path = output / candidate
        if not path.resolve().is_relative_to(output):
            raise ValueError('Artifact escapes output root')
        if any(p.is_symlink() for p in (path, *path.parents) if p != output and output in p.parents):
            raise ValueError('Symlink artifacts are forbidden')
        return path

    def read_json(relative, number=None):
        try:
            return json.loads(safe_path(relative).read_text(encoding='utf-8'))
        except (OSError, ValueError, RuntimeError) as exc:
            issue('unreadable_json', relative, str(exc), number)
            return None

    def checkpoint(stage, number=None):
        relative = f'state/stages/{stage}/{key}/{number or "paper"}.json'
        doc = read_json(relative, number)
        if not isinstance(doc, dict):
            issue('invalid_checkpoint', relative, 'Expected a stage object', number)
            return {}
        if (doc.get('schema_version'), doc.get('stage'), doc.get('paper_id'), doc.get('version')) != (
                SCHEMA_VERSION, stage, paper_id, number):
            issue('checkpoint_identity', relative, 'Schema/stage/paper/version mismatch', number)
        status = doc.get('status')
        outcomes.append(dict(stage=stage, version=number, status=status, error=doc.get('error')))
        allowed = {'download': {'success', 'no_source'},
                   'extract': {'success', 'no_source', 'skipped'},
                   'metadata': {'success'},
                   'references': {'success', 'no_data', 'not_indexed'}}
        if not isinstance(status, str) or status not in allowed[stage]:
            issue('upstream_failure', relative, f'{stage} status: {status!r}', number)
        artifacts = doc.get('artifacts')
        if not isinstance(artifacts, dict):
            issue('invalid_artifacts', relative, 'Expected artifact mapping', number)
            doc['artifacts'] = {}
        else:
            for value in artifacts.values():
                try:
                    if not safe_path(value).exists():
                        raise ValueError('Artifact does not exist')
                except (OSError, ValueError, RuntimeError) as exc:
                    issue('invalid_artifact', value, str(exc), number)
        return doc

    def artifact(doc, name, expected, number=None):
        relative = doc.get('artifacts', {}).get(name)
        if relative != expected:
            issue('artifact_location', expected, f'{name} must point to {expected}', number)
            return None
        try:
            return safe_path(relative)
        except (OSError, ValueError, RuntimeError) as exc:
            issue('invalid_artifact', relative, str(exc), number)
            return None

    # The metadata builder independently enforces contiguous versions and dates.
    expected_metadata = None
    try:
        expected_metadata = build_metadata(record)
    except (ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
        issue('invalid_discovery', 'discovery', str(exc))
    versions = record.get('versions')
    if not isinstance(versions, list):
        versions = []
    seen_versions, content_hashes = set(), {}
    for v in versions:
        if not isinstance(v, dict) or type(v.get('version')) is not int or v['version'] < 1:
            issue('invalid_version', 'discovery.versions', 'Expected a positive integer version')
            continue
        number = v['version']
        if number in seen_versions:
            issue('duplicate_version', 'discovery.versions', 'Repeated version', number)
        seen_versions.add(number)
        counts['versions'] += 1
        download, extract = checkpoint('download', number), checkpoint('extract', number)
        if download.get('status') == 'no_source':
            counts['no_source_versions'] += 1
            if extract.get('status') not in {'skipped', 'no_source'}:
                issue('inconsistent_status', 'extract', 'No download source but extraction succeeded', number)
            continue
        if download.get('status') != 'success':
            continue
        source_rel = f'raw/{key}/v{number}/source'
        corpus_rel = f'corpus/{key}/v{number}'
        if v.get('source_path') != source_rel or v.get('corpus_path') != corpus_rel:
            issue('version_paths', 'discovery.versions', 'Version paths differ from contract', number)
        source = artifact(download, 'source', source_rel, number)
        if source is not None:
            try:
                if not source.is_file() or source.stat().st_size == 0:
                    raise ValueError('Source is missing, empty or not a file')
                sidecar = download.get('artifacts', {}).get('download_metadata')
                if sidecar:
                    info = read_json(sidecar, number)
                    if isinstance(info, dict) and info.get('sha256'):
                        digest = hashlib.sha256()
                        with source.open('rb') as stream:
                            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                                digest.update(chunk)
                        if digest.hexdigest() != info['sha256']:
                            raise ValueError('Raw source checksum mismatch')
            except (OSError, ValueError) as exc:
                issue('invalid_source', source_rel, str(exc), number)
        if extract.get('status') == 'no_source':
            counts['no_source_versions'] += 1
            continue
        if extract.get('status') != 'success':
            issue('inconsistent_status', 'extract', 'Successful download needs extraction outcome', number)
            continue
        corpus = artifact(extract, 'corpus', corpus_rel, number)
        files = 0
        try:
            if corpus is None or not corpus.is_dir():
                raise ValueError('Corpus directory is missing')
            for path in sorted(corpus.rglob('*')):
                relative = path.relative_to(output).as_posix()
                try:
                    safe_path(relative)
                    if path.is_dir():
                        continue
                    if path == corpus / 'extract_metadata.json':
                        if not isinstance(read_json(relative, number), dict):
                            issue('invalid_sidecar', relative, 'Expected extraction metadata object', number)
                        continue
                    if path.suffix.lower() not in {'.tex', '.bib'}:
                        raise ValueError('Unexpected file type in corpus')
                    data = path.read_bytes()
                    if not data.strip() or b'\x00' in data or data.startswith((b'%PDF-', b'\x1f\x8b', b'PK\x03\x04')):
                        raise ValueError('Empty or binary corpus file')
                    files += 1
                    digest = hashlib.sha256(data).hexdigest()
                    content_hashes.setdefault(digest, []).append(relative)
                except (OSError, ValueError, RuntimeError) as exc:
                    issue('invalid_corpus_file', relative, str(exc), number)
            if not files:
                raise ValueError('Successful extraction contains no usable .tex/.bib files')
            counts['source_versions'] += 1
            counts['source_files'] += files
        except (OSError, ValueError) as exc:
            issue('invalid_corpus', corpus_rel, str(exc), number)

    # Only inspect this paper; other papers in the output root are valid neighbors.
    corpus_base = f'corpus/{key}'
    try:
        for path in safe_path(corpus_base).glob('v*'):
            if path.name not in {f'v{n}' for n in seen_versions}:
                issue('unexpected_version', path.relative_to(output), 'Version absent from discovery')
    except (OSError, ValueError, RuntimeError) as exc:
        issue('invalid_corpus', corpus_base, str(exc))

    metadata_stage = checkpoint('metadata')
    metadata_rel = f'metadata/{key}.json'
    if artifact(metadata_stage, 'metadata', metadata_rel) is not None:
        metadata = read_json(metadata_rel)
        if not isinstance(metadata, dict):
            issue('invalid_metadata', metadata_rel, 'Expected metadata object')
        elif expected_metadata is not None:
            for field, expected in expected_metadata.items():
                if field not in metadata or metadata[field] != expected:
                    issue('metadata_mismatch', metadata_rel, f'Missing/inconsistent field: {field}')

    refs_stage = checkpoint('references')
    refs_rel = f'references/{key}.json'
    if artifact(refs_stage, 'references', refs_rel) is not None:
        references = read_json(refs_rel)
        if not isinstance(references, list):
            issue('invalid_references', refs_rel, 'Expected reference array')
        else:
            counts['references'] = len(references)
            if bool(references) != (refs_stage.get('status') == 'success'):
                issue('reference_status', refs_rel, 'Reference count disagrees with stage outcome')
            identities = set()
            scalar_fields = ('ref_arxiv_id', 'ref_title', 'ref_submission_date', 'ref_s2_id')
            for index, ref in enumerate(references):
                try:
                    if not isinstance(ref, dict) or ref.get('arxiv_id') != paper_id:
                        raise ValueError('Reference has wrong owner or is not an object')
                    for field in scalar_fields:
                        if field not in ref or (ref[field] is not None and (
                                not isinstance(ref[field], str) or not ref[field].strip())):
                            raise ValueError(f'Invalid/missing scalar field: {field}')
                    authors = ref.get('ref_authors')
                    if not isinstance(authors, list) or any(not isinstance(a, str) or not a.strip() for a in authors):
                        raise ValueError('ref_authors must be a list of names')
                    missing = [f for f in (*scalar_fields, 'ref_authors') if ref[f] is None or ref[f] == []]
                    markers = ref.get('missing_fields')
                    if not isinstance(markers, list) or sorted(markers) != sorted(missing):
                        raise ValueError('missing_fields does not describe missing values')
                    submitted = ref['ref_submission_date']
                    if submitted is not None:
                        if len(submitted) == 4:
                            date(int(submitted), 1, 1)
                        else:
                            date.fromisoformat(submitted)
                    identity = ('s2', ref['ref_s2_id']) if ref['ref_s2_id'] else ('arxiv', ref['ref_arxiv_id'])
                    if identity[1] is not None:
                        if identity in identities:
                            raise ValueError('Duplicate reference identifier')
                        identities.add(identity)
                except (ValueError, TypeError, KeyError) as exc:
                    issue('invalid_reference', refs_rel, f'Row {index}: {exc}')
    for digest, paths in content_hashes.items():
        if len(paths) > 1:
            duplicates.append(dict(sha256=digest, paths=paths))
            issue('duplicate_content', paths[0], 'Identical content: ' + ', '.join(paths), warning=True)
    relative = f'validation/{key}.json'
    report = dict(schema_version=SCHEMA_VERSION, paper_id=paper_id, timestamp=utc_now(),
                  valid=not errors, counts=counts, outcomes=outcomes, errors=errors,
                  warnings=warnings, duplicate_files=duplicates)
    # Validate the destination too, before atomic_json creates any parent directory.
    destination = safe_path(relative)
    atomic_json(destination, report)
    return StageResult('validate', paper_id, None, 'data_error' if errors else 'success', utc_now(),
                       artifacts={'validation': relative}, attempts=1,
                       size_bytes=destination.stat().st_size,
                       error=ErrorInfo('validation_failed', f'{len(errors)} validation issue(s)') if errors else None)
