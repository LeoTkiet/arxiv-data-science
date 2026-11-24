"""Read-only M1/M2 snapshot boundary for matching contract 3.0.

Only declared, completed, validated artifacts may become a ready bundle. The
inventory is also the input dependency for the matching resume mechanism.
"""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from arxiv_pipeline.common.utils import paper_key
from arxiv_pipeline.matching.contracts import (
    ContractError, Issue, PublicationBundle, canonical_id, publication_from_rows,
)
from arxiv_pipeline.reporting.milestone2.validation import safe_path


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


@dataclass
class LoadedPublication:
    bundle: PublicationBundle
    provenance: dict


class _NotReady(ValueError):
    pass


def load_with_provenance(arxiv_id, context) -> LoadedPublication:
    paper = canonical_id(arxiv_id)
    roots = {name: Path(context[f'{name}_root']).resolve() for name in ('ms1', 'ms2')}
    files = {}
    contents = {}
    snapshot = None

    def read_bytes(source, relative):
        key = f'{source}/{relative}'
        if key not in contents:
            raw = safe_path(roots[source], relative).read_bytes()
            files[key] = hashlib.sha256(raw).hexdigest()
            contents[key] = raw
        return contents[key]

    def read(source, relative):
        return json.loads(read_bytes(source, relative).decode('utf-8-sig'))

    def checkpoint(source, stage, schema, statuses):
        relative = f'state/stages/{stage}/{paper_key(paper)}/paper.json'
        row = read(source, relative)
        if not isinstance(row, dict) or (row.get('stage'), row.get('paper_id'),
                row.get('version'), row.get('schema_version')) != (stage, paper, None, schema):
            raise ContractError(f'Wrong identity/schema in {relative}')
        if row.get('synthetic') is True and not context.get('fixture_mode', False):
            raise ContractError('Synthetic handoff requires fixture_mode=True')
        if row.get('status') not in statuses:
            raise _NotReady(f'{stage} is {row.get("status")}')
        artifacts = row.get('artifacts')
        if not isinstance(artifacts, dict):
            raise ContractError(f'Missing artifact declarations in {stage}')
        # Validate paths even when an artifact is not needed for the basic join.
        for relative_artifact in artifacts.values():
            safe_path(roots[source], relative_artifact)
        if source == 'ms2':
            deps = read(source, relative.replace('.json', '.dependencies.json'))
            inventory = deps.get('artifacts') if isinstance(deps, dict) else None
            if not isinstance(inventory, dict) or set(inventory) != set(artifacts.values()):
                raise ContractError(f'Missing/inconsistent {stage} artifact inventory')
            for rel, expected in inventory.items():
                read_bytes(source, rel)
                if files[f'{source}/{rel}'] != expected:
                    raise ContractError(f'Artifact hash mismatch: {source}/{rel}')
        return row

    def artifact(source, artifacts, name):
        if name not in artifacts:
            raise ContractError(f'Missing declared {name} artifact')
        return read(source, artifacts[name])

    try:
        if not context.get('fixture_mode', False):
            for root in roots.values():
                for marker in (root / 'fixture_manifest.json', root.parent / 'fixture_manifest.json'):
                    if marker.is_file() and json.loads(marker.read_text(encoding='utf-8')).get('synthetic'):
                        raise ContractError('Synthetic handoff requires fixture_mode=True')
        refs_stage = checkpoint('ms1', 'references', '1.0', {'success', 'no_data'})
        refs_cp = refs_stage['artifacts']
        if refs_stage['status'] == 'no_data' and 'references' not in refs_cp:
            # The validated checkpoint itself certifies empty references. Do not
            # infer emptiness for failures or read an undeclared leftover file.
            refs = []
        else:
            refs = artifact('ms1', refs_cp, 'references')
        qa1 = artifact('ms1', checkpoint('ms1', 'validate', '1.0', {'success'})['artifacts'], 'validation')
        if not isinstance(qa1, dict) or (qa1.get('schema_version'), qa1.get('paper_id')) != ('1.0', paper):
            raise ContractError('M1 validation identity/schema mismatch')
        if qa1.get('valid') is not True or qa1.get('errors') != []:
            raise _NotReady('M1 validation did not pass')
        state = read('ms2', f'state/parsing/{paper_key(paper)}.json')
        if not isinstance(state, dict) or state.get('arxiv_id') != paper:
            raise ContractError('M2 parsing state has wrong publication')
        if state.get('complete') is not True:
            raise _NotReady('M2 parsing is incomplete')
        dedup = checkpoint('ms2', 'deduplicate', '2.0', {'success', 'no_source'})['artifacts']
        manifest = artifact('ms2', dedup, 'parsing_manifest')
        if not isinstance(manifest, dict) or (manifest.get('schema_version'), manifest.get('arxiv_id')) != ('2.0', paper):
            raise ContractError('M2 manifest identity/schema mismatch')
        if manifest.get('complete') is not True:
            raise _NotReady('M2 export is incomplete')
        snapshot = manifest.get('input_snapshot_id')
        if not isinstance(snapshot, str) or not snapshot:
            raise ContractError('Missing M2 input snapshot identity')
        expected = manifest.get('expected_versions')
        if (not isinstance(expected, list) or not expected
                or any(type(v) is not int for v in expected)
                or expected != list(range(1, len(expected) + 1))):
            raise ContractError('M2 versions must cover contiguous 1..N from MS1 discovery; '
                                'unavailable source must retain its version with no_source status')
        versions = manifest.get('versions')
        if (not isinstance(versions, list) or any(not isinstance(v, dict) for v in versions)
                or [v.get('version') for v in versions] != expected
                or any(v.get('status') not in {'success', 'no_source'} for v in versions)
                or state.get('expected_versions') != expected or state.get('selected_versions') != expected):
            raise ContractError('M2 version coverage disagrees with parsing state')
        if manifest.get('tables', {}).get('bibtex') != dedup.get('bibtex'):
            raise ContractError('M2 BibTeX path disagrees with manifest')
        qa2 = artifact('ms2', checkpoint('ms2', 'validate_parse', '2.0', {'success'})['artifacts'], 'validation')
        if not isinstance(qa2, dict) or (qa2.get('schema_version'), qa2.get('arxiv_id'),
                qa2.get('input_snapshot_id')) != ('2.0', paper, snapshot):
            raise ContractError('M2 QA identity/snapshot mismatch')
        if qa2.get('valid') is not True or qa2.get('errors') != []:
            raise _NotReady('M2 validation did not pass')
        bibtex = artifact('ms2', dedup, 'bibtex')
        if not isinstance(bibtex, list) or not isinstance(refs, list):
            raise ContractError('BibTeX/references artifacts must be lists')
        bundle = publication_from_rows(paper, bibtex, refs, readiness='ready')
        extra = []
        for row in bundle.bibtex:
            for name, value in (('title', row.title), ('authors', row.authors), ('year', row.year)):
                if value is None or value == '' or value == []:
                    extra.append(Issue(paper, 'match_input', f'missing_bibtex_{name}', 'warning',
                        f'BibTeX {name} is missing; preserved without fabrication', citation_key=row.citation_key))
        bundle = PublicationBundle(paper, bundle.bibtex, bundle.references, 'ready', bundle.issues + extra)
    except (FileNotFoundError, _NotReady) as exc:
        bundle = PublicationBundle(paper, [], [], 'not_ready', [Issue(paper, 'match_input',
            'upstream_not_ready', 'error', str(exc))])
    except (OSError, ValueError, TypeError, KeyError) as exc:
        bundle = PublicationBundle(paper, [], [], 'invalid', [Issue(paper, 'match_input',
            'invalid_snapshot', 'error', str(exc))])
    provenance = dict(files=files, input_fingerprint=digest(files), input_snapshot_id=snapshot,
                      synthetic=bool(context.get('fixture_mode', False)))
    return LoadedPublication(bundle, provenance)


def load_publication(arxiv_id, context) -> PublicationBundle:
    return load_with_provenance(arxiv_id, context).bundle
