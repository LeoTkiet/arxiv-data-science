"""Offline T3-04 preparation, XLSX collection and manual-quota audit.

Consumes staged MS1/MS2 artifacts, not live Drive/API data. Snapshot ingestion
and its full producer checkpoint verification remain the responsibility of TV1.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

from arxiv_pipeline.common.schemas import MATCHING_SCHEMA_VERSION
from arxiv_pipeline.common.utils import atomic_json, paper_key
from arxiv_pipeline.matching.annotation_workbook import (
    export_annotation_workbook, load_annotation_workbook,
)
from arxiv_pipeline.matching.contracts import (
    ContractError, LabelRecord, canonical_id, publication_from_rows,
)
from arxiv_pipeline.matching.labeling import load_labels_json, validate_label_manifest


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read(path: Path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def _inside(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ContractError(f'Path escapes input root: {relative}')
    return path


def validate_assignments(assignments: dict) -> dict[str, list[str]]:
    if not isinstance(assignments, dict) or set(assignments) != {f'tv{i}' for i in range(1, 6)}:
        raise ContractError('T3-04 requires an explicit tv1..tv5 assignment roster')
    result, seen = {}, set()
    for member, ids in assignments.items():
        if not isinstance(ids, list) or len(ids) < 5:
            raise ContractError(f'{member} needs at least five publications')
        result[member] = []
        for raw in ids:
            pid = canonical_id(raw)
            if raw != pid or pid in seen:
                raise ContractError(f'Duplicate/noncanonical publication: {raw}')
            seen.add(pid)
            result[member].append(pid)
    return result


def prepare_forms(assignments: dict, inputs: Path | str, output: Path | str) -> dict:
    """Preflight every publication before writing a fresh output directory."""
    assignments = validate_assignments(assignments)
    inputs, output = Path(inputs), Path(output)
    if output.exists():
        raise FileExistsError(f'Output already exists; preserve annotations: {output}')
    staged, all_sources = [], []
    for member, ids in assignments.items():
        for pid in ids:
            base = f'{member}/{paper_key(pid)}'
            names = ['bibtex.json', 'references.json', 'ms1_validation.json',
                'ms2_validation.json', 'input_manifest.json']
            raw = {name: _inside(inputs, f'{base}/{name}').read_bytes() for name in names}
            rows = {name: json.loads(value.decode('utf-8-sig')) for name, value in raw.items()}
            bib, refs = rows['bibtex.json'], rows['references.json']
            q1, q2, pm = (rows[n] for n in names[2:])
            if (q1.get('schema_version'), q1.get('paper_id')) != ('1.0', pid):
                raise ContractError(f'MS1 validation identity/schema mismatch: {pid}')
            if any((q.get('schema_version'), q.get('arxiv_id')) != ('2.0', pid) for q in (q2, pm)):
                raise ContractError(f'MS2 identity/schema mismatch: {pid}')
            if any(q.get('valid') is not True or q.get('errors') != [] for q in (q1, q2)) or pm.get('complete') is not True:
                raise ContractError(f'Incomplete/invalid upstream publication: {pid}')
            snapshot = pm.get('input_snapshot_id')
            if not isinstance(snapshot, str) or not snapshot or q2.get('input_snapshot_id') != snapshot:
                raise ContractError(f'MS2 snapshot mismatch: {pid}')
            versions = pm.get('expected_versions')
            coverage = pm.get('versions')
            if (not isinstance(versions, list) or not versions
                    or any(type(v) is not int for v in versions)
                    or versions != list(range(1, len(versions) + 1))
                    or not isinstance(coverage, list)
                    or [v.get('version') for v in coverage] != versions
                    or any(v.get('status') not in {'success', 'no_source'} for v in coverage)):
                raise ContractError(f'Incomplete MS2 version coverage: {pid}')
            outcomes = [r.get('status') for r in q1.get('outcomes', []) if r.get('stage') == 'references']
            if len(outcomes) != 1 or outcomes[0] not in {'success', 'no_data'}:
                raise ContractError(f'Missing/error references outcome: {pid}')
            if not isinstance(bib, list) or not isinstance(refs, list):
                raise ContractError(f'Expected BibTeX/reference arrays: {pid}')
            if outcomes[0] == 'no_data' and refs:
                raise ContractError(f'no_data disagrees with references: {pid}')
            if q1.get('counts', {}).get('references') != len(refs) or q2.get('counts', {}).get('bibtex_entries') != len(bib):
                raise ContractError(f'Validation counts disagree with rows: {pid}')
            bundle = publication_from_rows(pid, bib, refs, readiness='ready')
            if len(bundle.bibtex) < 20:
                raise ContractError(f'Manual selection needs at least 20 queries: {pid}')
            sources = {f'{base}/{name}': _sha(value) for name, value in raw.items()}
            staged.append((member, bundle, snapshot, sources))
            all_sources.extend(sources.items())
    if any(_sha(_inside(inputs, name).read_bytes()) != digest for name, digest in all_sources):
        raise ContractError('Inputs changed during preparation; freeze the snapshot')
    output.mkdir(parents=True)
    publications, forms = [], {}
    for member, bundle, snapshot, sources in staged:
        relative = f'publications/{paper_key(bundle.arxiv_id)}/bundle.json'
        atomic_json(output / relative, bundle.to_dict())
        publications.append(dict(member=member, arxiv_id=bundle.arxiv_id,
            bundle=relative, sha256=_sha((output / relative).read_bytes()),
            input_snapshot_id=snapshot, source_hashes=sources))
    for member in assignments:
        path = output / f'annotation_{member}.xlsx'
        export_annotation_workbook([b for m, b, _, _ in staged if m == member], path)
        forms[member] = dict(path=path.name, sha256=_sha(path.read_bytes()))
    manifest = dict(schema_version=MATCHING_SCHEMA_VERSION, task='T3-04',
        assignments=assignments, publications=publications, forms=forms,
        evidence_level='staged_rows_and_validation_reports',
        production_snapshot_verified=False)
    # Published last: an interrupted output is not a usable prepared snapshot.
    atomic_json(output / 'prepared_manifest.json', manifest)
    return manifest


def load_prepared(root: Path | str):
    root = Path(root)
    manifest = _read(root / 'prepared_manifest.json')
    if manifest.get('schema_version') != MATCHING_SCHEMA_VERSION:
        raise ContractError('Unsupported prepared schema version')
    assignments = validate_assignments(manifest['assignments'])
    owners = {pid: member for member, ids in assignments.items() for pid in ids}
    bundles, seen = [], set()
    for row in manifest['publications']:
        pid = row['arxiv_id']
        if pid in seen or owners.get(pid) != row['member']:
            raise ContractError('Prepared publication identity/owner mismatch')
        raw = _inside(root, row['bundle']).read_bytes()
        if _sha(raw) != row['sha256']:
            raise ContractError(f'Prepared bundle hash mismatch: {pid}')
        value = json.loads(raw)
        if value['arxiv_id'] != pid or value['readiness'] != 'ready':
            raise ContractError('Prepared bundle identity/readiness mismatch')
        bundles.append(publication_from_rows(pid, value['bibtex'], value['references'], readiness='ready'))
        seen.add(pid)
    if seen != set(owners):
        raise ContractError('Prepared snapshot is missing assigned publications')
    return assignments, bundles


def audit_manual_labels(assignments: dict, bundles: list, labels: list[LabelRecord]) -> dict:
    """Count distinct accepted decisions per paper AND per assigned member."""
    assignments = validate_assignments(assignments)
    owners = {pid: member for member, ids in assignments.items() for pid in ids}
    if set(owners) != {b.arxiv_id for b in bundles}:
        raise ContractError('Assignments and bundle publications differ')
    report = validate_label_manifest(bundles, labels)
    errors = [e.to_dict() for e in report.errors]
    targets = {b.arxiv_id: {r.ref_arxiv_id for r in b.references} for b in bundles}
    query_keys = {(b.arxiv_id, q.citation_key) for b in bundles for q in b.bibtex}
    duplicates = Counter(label.key for label in labels)
    eligible = set()
    for label in labels:
        problems = []
        if label.label_source != 'manual':
            problems.append('non_manual_label')
        if label.annotator != owners.get(label.arxiv_id):
            problems.append('wrong_or_missing_annotator')
        if label.decision == 'uncertain' and not label.notes:
            problems.append('uncertain_without_notes')
        if label.review_status == 'accepted':
            if label.reviewer not in assignments or label.reviewer == label.annotator:
                problems.append('independent_reviewer_required')
            if label.decision == 'uncertain':
                problems.append('accepted_uncertain')
        for code in problems:
            errors.append(dict(arxiv_id=label.arxiv_id, citation_key=label.citation_key, code=code))
        valid_target = label.groundtruth_arxiv_id is None or label.groundtruth_arxiv_id in targets.get(label.arxiv_id, set())
        if not problems and valid_target and duplicates[label.key] == 1 and label.key in query_keys and label.is_supervised:
            eligible.add(label.key)
    per_paper = {pid: sum(key[0] == pid for key in eligible) for pid in owners}
    qualified = {pid for pid, count in per_paper.items() if count >= 20}
    members = {}
    for member, ids in assignments.items():
        good = [pid for pid in ids if pid in qualified]
        count = sum(per_paper[pid] for pid in good)
        members[member] = dict(assigned_publications=len(ids), qualifying_publications=len(good),
            accepted_entries=count, meets_quota=len(good) >= 5 and count >= 100)
    quota = len(qualified) >= 25 and len(eligible) >= 500 and all(m['meets_quota'] for m in members.values())
    return dict(valid=not errors, ready_for_handoff=not errors and quota,
        manual_quota_met=quota, errors=errors, warnings=[w.to_dict() for w in report.warnings],
        counts=report.counts, by_decision=dict(Counter(l.decision for l in labels)),
        by_review_status=dict(Counter(l.review_status for l in labels)),
        accepted_entries=len(eligible), qualifying_publications=len(qualified),
        per_publication=per_paper, members=members)


def collect_workbooks(prepared: Path | str, workbooks: dict[str, Path], output: Path | str,
        *, origin: str = 'human') -> dict:
    assignments, bundles = load_prepared(prepared)
    if origin not in {'human', 'ai_assisted'}:
        raise ContractError('Unknown annotation origin')
    output = Path(output)
    if output.exists():
        raise FileExistsError(f'Output already exists: {output}')
    if not workbooks or set(workbooks) - set(assignments):
        raise ContractError('Provide workbooks with assigned tv1..tv5 owners')
    labels, evidence = [], {}
    for member, raw_path in workbooks.items():
        path = Path(raw_path)
        before = _sha(path.read_bytes())
        selected = [b for b in bundles if b.arxiv_id in assignments[member]]
        incoming = load_annotation_workbook(path, selected, expected_annotator=member)
        if origin == 'ai_assisted' and any(l.review_status == 'accepted' for l in incoming):
            raise ContractError('AI drafts cannot be imported as accepted human reviews')
        if _sha(path.read_bytes()) != before:
            raise ContractError('Workbook changed during collection')
        labels.extend(incoming)
        evidence[member] = dict(file=path.name, sha256=before, origin=origin)
    audit = audit_manual_labels(assignments, bundles, labels)
    output.mkdir(parents=True)
    atomic_json(output / 'labels.json', [label.to_dict() for label in labels])
    atomic_json(output / 'manual_audit.json', audit)
    atomic_json(output / 'label_manifest.json', dict(schema_version=MATCHING_SCHEMA_VERSION,
        task='T3-04', workbooks=evidence, ready_for_handoff=audit['ready_for_handoff'],
        labels_sha256=_sha((output / 'labels.json').read_bytes()),
        prepared_manifest_sha256=_sha((Path(prepared) / 'prepared_manifest.json').read_bytes())))
    return audit


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='T3-04 offline XLSX annotation workflow')
    commands = parser.add_subparsers(dest='command', required=True)
    prepare = commands.add_parser('prepare')
    prepare.add_argument('--assignments', required=True, type=Path)
    prepare.add_argument('--inputs', required=True, type=Path)
    prepare.add_argument('--output', required=True, type=Path)
    collect = commands.add_parser('collect')
    collect.add_argument('--prepared', required=True, type=Path)
    collect.add_argument('--workbook', action='append', required=True, help='tvN=path.xlsx (repeat per member)')
    collect.add_argument('--origin', choices=['human', 'ai_assisted'], default='human')
    collect.add_argument('--output', required=True, type=Path)
    audit_cmd = commands.add_parser('audit')
    audit_cmd.add_argument('--prepared', required=True, type=Path)
    audit_cmd.add_argument('--labels', required=True, type=Path)
    audit_cmd.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == 'prepare':
            config = _read(args.assignments)
            if config.get('schema_version') != MATCHING_SCHEMA_VERSION or config.get('task') != 'T3-04':
                raise ContractError('Expected a T3-04 schema 3.0 assignment config')
            result = prepare_forms(config['assignments'], args.inputs, args.output)
            print(f"Prepared {len(result['publications'])} publications in {args.output}")
            return 0
        if args.command == 'collect':
            inputs = {}
            for value in args.workbook:
                member, separator, path = value.partition('=')
                if not separator or member in inputs:
                    raise ContractError('Expected unique --workbook tvN=path.xlsx arguments')
                inputs[member] = Path(path)
            report = collect_workbooks(args.prepared, inputs, args.output, origin=args.origin)
        else:
            assignments, bundles = load_prepared(args.prepared)
            report = audit_manual_labels(assignments, bundles, load_labels_json(args.labels))
            if args.output.exists():
                raise FileExistsError(f'Output already exists: {args.output}')
            args.output.mkdir(parents=True)
            atomic_json(args.output / 'manual_audit.json', report)
        print(f"accepted_entries={report['accepted_entries']} ready_for_handoff={report['ready_for_handoff']}")
        return 0 if report['ready_for_handoff'] else 1
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f'Error: {exc}')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
