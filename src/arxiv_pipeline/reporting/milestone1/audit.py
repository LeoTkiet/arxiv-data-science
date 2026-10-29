"""Audit every assigned ID against available discovery records and real corpus files."""
import argparse
from collections import Counter
import json
from pathlib import Path

from arxiv_pipeline.common.utils import atomic_json, normalize_id, utc_now
from arxiv_pipeline.export.validation import run as validate
from arxiv_pipeline.main import expand_range


def audit(output_dir, assignment):
    """Offline audit; never treats absent assigned IDs as successfully crawled papers.

    Run after all writers stop. Existing checkpoints are preferred to the completed
    manifest, which may be older after interruption. This is an on-disk audit, not a
    benchmark-run snapshot. Validation reports are refreshed, corpus is not modified.
    """
    output = Path(output_dir)
    expected = set()
    for item in assignment['ranges']:
        expected.update(expand_range(item['range']))
    diagnostics, selected = [], {}

    def collect(record, origin, checkpoint):
        if not isinstance(record, dict):
            diagnostics.append(dict(path=origin, error='Discovery record must be an object'))
            return
        try:
            identifier, _ = normalize_id(record.get('arxiv_id') or record['input_id'])
        except (ValueError, KeyError, TypeError, AttributeError):
            diagnostics.append(dict(path=origin, error='Invalid discovery identity'))
            return
        if identifier not in expected:
            return
        previous = selected.get(identifier)
        if previous is None or (checkpoint and (record.get('timestamp') or '') >= (previous.get('timestamp') or '')):
            selected[identifier] = record

    for path in sorted((output / 'state/discovery').glob('*.json')):
        try:
            collect(json.loads(path.read_text(encoding='utf-8')), str(path.relative_to(output)), True)
        except (ValueError, OSError) as exc:
            diagnostics.append(dict(path=str(path.relative_to(output)), error=str(exc)))
    manifest = output / 'manifests/discovery.json'
    if manifest.exists():
        try:
            records = json.loads(manifest.read_text(encoding='utf-8'))
            if not isinstance(records, list):
                raise ValueError('Discovery manifest must be an array')
            for record in records:
                collect(record, 'manifests/discovery.json', False)
        except (ValueError, OSError) as exc:
            diagnostics.append(dict(path='manifests/discovery.json', error=str(exc)))
    unseen = sorted(expected - selected.keys())
    statuses = Counter(record.get('status', 'missing') for record in selected.values())
    details, passed, failed, known_versions = [], 0, 0, 0
    for identifier, record in sorted(selected.items()):
        if record.get('status') != 'success':
            continue
        try:
            result = validate(record, output)
            doc = json.loads((output / result.artifacts['validation']).read_text(encoding='utf-8'))
            valid = result.status == 'success' and doc['valid'] is True
            passed += valid
            failed += not valid
            known_versions += doc['counts']['versions']
            details.append(dict(paper_id=identifier, valid=valid, counts=doc['counts'],
                                errors=doc['errors'], warnings=doc['warnings']))
        except (ValueError, KeyError, TypeError, OSError) as exc:
            failed += 1
            details.append(dict(paper_id=identifier, valid=False, error=str(exc)))
    failed_ids = sorted(identifier for identifier, record in selected.items()
                        if record.get('status') not in {'success', 'not_found'})
    return dict(schema_version='1.0', generated_at=utc_now(), scope='on_disk_assignment_audit',
                assigned_input_count=len(expected), discovered_assigned_ids=len(selected),
                unseen_ids=unseen, unresolved_discovery_ids=failed_ids,
                discovery_statuses=dict(statuses), known_version_count=known_versions,
                validation_pass_papers=passed, validation_fail_papers=failed,
                audit_complete=not unseen and not failed_ids and not failed and not diagnostics,
                paper_results=details, diagnostics=diagnostics,
                note='not_found is resolved lookup, not a crawled publication; unseen/error version counts remain unknown')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--assignment', type=Path, required=True)
    parser.add_argument('--save', type=Path, required=True)
    args = parser.parse_args(argv)
    report = audit(args.output, json.loads(args.assignment.read_text(encoding='utf-8')))
    atomic_json(args.save, report)
    print(json.dumps({key: report[key] for key in ('assigned_input_count', 'discovered_assigned_ids',
        'validation_pass_papers', 'validation_fail_papers', 'audit_complete')}))
    return 0 if report['audit_complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
