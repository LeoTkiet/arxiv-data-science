"""Aggregate selected-run checkpoint outcomes with explicit denominators and missing data."""
import argparse
from collections import Counter
import json
from pathlib import Path

from arxiv_pipeline.common.utils import atomic_json, paper_key, utc_now


def ratio(numerator, denominator):
    return dict(numerator=numerator, denominator=denominator,
                value=numerator / denominator if denominator else None)


def summarize(output_dir):
    """Read only papers in the last completed discovery manifest, not every cached paper.

    Unknown discovery failures have unknown version counts and are reported separately.
    Metrics describe recorded outcomes; use validator reports to assess artifact integrity.
    """
    output = Path(output_dir).resolve()
    diagnostics = []

    def read(relative, required=False):
        path = output / relative
        try:
            if not path.resolve().is_relative_to(output):
                raise ValueError('Path escapes output root')
            return json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            if required:
                raise ValueError(f'Cannot read {relative}: {exc}') from exc
            diagnostics.append(dict(path=relative, error=str(exc)))
            return None

    records = read('manifests/discovery.json', required=True)
    if not isinstance(records, list):
        raise ValueError('Discovery manifest must be an array')
    selected = {}
    for rec in records:
        if not isinstance(rec, dict) or not isinstance(rec.get('versions', []), list):
            raise ValueError('Malformed discovery record')
        identity = rec.get('arxiv_id') or rec.get('input_id')
        # Keep a successful alias rather than an erroneous requested-version alias.
        if identity not in selected or rec.get('status') == 'success':
            selected[identity] = rec

    def stage(name, paper, number=None):
        relative = f'state/stages/{name}/{paper_key(paper)}/{number or "paper"}.json'
        doc = read(relative)
        if not isinstance(doc, dict) or (doc.get('stage'), doc.get('paper_id'), doc.get('version')) != (name, paper, number):
            diagnostics.append(dict(path=relative, error='Missing or mismatched stage identity'))
            return {'status': 'missing'}
        return doc

    versions, papers, failures = [], [], Counter()
    refs_total = refs_complete = retrieved_papers = ref_success = 0
    before_paper, after_paper, packages_paper = [], [], []
    discovery_counts = Counter()
    for rec in selected.values():
        discovery_counts[rec.get('status', 'missing')] += 1
        paper = rec.get('arxiv_id')
        if rec.get('status') != 'success' or not paper:
            failures['discovery:' + rec.get('status', 'missing')] += 1
            continue
        key = paper_key(paper)
        rows = []
        for v in rec['versions']:
            number = v.get('version') if isinstance(v, dict) else None
            if type(number) is not int or number < 1:
                diagnostics.append(dict(path='discovery.versions', error=f'Invalid version for {paper}'))
                continue
            dl, ex = stage('download', paper, number), stage('extract', paper, number)
            row = dict(paper_id=paper, version=number, download=dl['status'], extract=ex['status'])
            for name, doc in (('download', dl), ('extract', ex)):
                expected_skip = name == 'extract' and doc['status'] == 'skipped' and dl['status'] == 'no_source'
                if doc['status'] not in {'success', 'no_source'} and not expected_skip:
                    error = doc.get('error') or {}
                    failures[f'{name}:{doc["status"]}:{error.get("kind", "unspecified")}'] += 1
            download_meta = read(f'raw/{key}/v{number}/download_metadata.json') if dl['status'] == 'success' else None
            extract_meta = read(f'corpus/{key}/v{number}/extract_metadata.json') if ex['status'] == 'success' else None
            for field, doc, source_field in (
                ('package_bytes', download_meta, 'raw_size_bytes'),
                ('uncompressed_before_bytes', extract_meta, 'raw_size_bytes'),
                ('filtered_bytes', extract_meta, 'corpus_size_bytes')):
                value = doc.get(source_field) if isinstance(doc, dict) else None
                row[field] = value if type(value) is int and value >= 0 else None
            rows.append(row)
            versions.append(row)
        paired = bool(rows) and all(r['uncompressed_before_bytes'] is not None and r['filtered_bytes'] is not None for r in rows)
        if paired:
            before_paper.append(sum(r['uncompressed_before_bytes'] for r in rows))
            after_paper.append(sum(r['filtered_bytes'] for r in rows))
        if rows and all(r['package_bytes'] is not None for r in rows):
            packages_paper.append(sum(r['package_bytes'] for r in rows))
        meta, refs = stage('metadata', paper), stage('references', paper)
        for name, doc in (('metadata', meta), ('references', refs)):
            if doc['status'] not in {'success', 'no_data', 'not_indexed'}:
                failures[f'{name}:{doc["status"]}'] += 1
        ref_count = None
        if refs['status'] in {'success', 'no_data'}:
            refs_doc = read(f'references/{key}.json')
            if isinstance(refs_doc, list):
                retrieved_papers += 1
                ref_success += refs['status'] == 'success' and bool(refs_doc)
                ref_count = len(refs_doc)
                refs_total += ref_count
                for ref in refs_doc:
                    fields = ('ref_arxiv_id', 'ref_title', 'ref_authors', 'ref_submission_date', 'ref_s2_id')
                    if isinstance(ref, dict) and all(ref.get(f) not in (None, [], '') for f in fields):
                        refs_complete += 1
        validation = read(f'validation/{key}.json')
        papers.append(dict(paper_id=paper, versions=len(rows), metadata=meta['status'], references=refs['status'],
                           reference_count=ref_count, validation_valid=validation.get('valid') if isinstance(validation, dict) else None,
                           source_complete=bool(rows) and all(r['download'] == r['extract'] == 'success' for r in rows)))
    n_versions, n_papers = len(versions), len(selected)
    success_discovery = len(papers)

    def mean(values):
        return sum(values) / len(values) if values else None

    return dict(schema_version='1.0', generated_at=utc_now(),
        counts=dict(papers=n_papers, discovery_success_papers=success_discovery,
                    unknown_version_papers=n_papers - success_discovery, expected_versions=n_versions,
                    references=refs_total),
        rates=dict(discovery_success=ratio(success_discovery, n_papers),
            download_success=ratio(sum(r['download'] == 'success' for r in versions), n_versions),
            extract_success=ratio(sum(r['extract'] == 'success' for r in versions), n_versions),
            version_record_coverage=ratio(sum(r['download'] != 'missing' for r in versions), n_versions),
            source_complete_papers=ratio(sum(p['source_complete'] for p in papers), n_papers),
            validation_pass=ratio(sum(p['validation_valid'] is True for p in papers), success_discovery),
            reference_query_success=ratio(retrieved_papers, success_discovery),
            reference_nonempty_success=ratio(ref_success, success_discovery)),
        sizes=dict(paired_papers=len(before_paper), package_complete_papers=len(packages_paper),
            mean_uncompressed_before_per_paper_bytes=mean(before_paper),
            mean_filtered_per_paper_bytes=mean(after_paper),
            mean_download_package_per_paper_bytes=mean(packages_paper)),
        references=dict(mean_per_retrieved_paper=refs_total / retrieved_papers if retrieved_papers else None,
                        complete_metadata_rate=ratio(refs_complete, refs_total)),
        discovery_statuses=dict(discovery_counts),
        download_statuses=dict(Counter(r['download'] for r in versions)),
        extract_statuses=dict(Counter(r['extract'] for r in versions)),
        reference_statuses=dict(Counter(p['references'] for p in papers)),
        failure_causes=dict(failures), papers=papers, versions=versions, diagnostics=diagnostics)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='Pipeline corpus root')
    parser.add_argument('--save', type=Path, required=True)
    parser.add_argument('--revalidate', action='store_true', help='Recheck selected successful discovery records')
    args = parser.parse_args(argv)
    if args.revalidate:
        from arxiv_pipeline.export.validation import run
        records = json.loads((args.output / 'manifests/discovery.json').read_text(encoding='utf-8'))
        seen = set()
        for rec in records:
            if rec['status'] == 'success' and rec['arxiv_id'] not in seen:
                run(rec, args.output)
                seen.add(rec['arxiv_id'])
    atomic_json(args.save, summarize(args.output))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
