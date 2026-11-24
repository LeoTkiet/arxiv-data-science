"""Explicit synthetic integration harness. Not corpus, labels, ML results or Parquet.

The fake business adapters are confined to this module and never selected by
the production runner. They exercise every orchestration seam offline in CI.
"""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path

from arxiv_pipeline.matching.state import atomic_json
from arxiv_pipeline.matching.contracts import (
    FeatureBatch, FeatureSchema, FeatureSpec, LabelRecord, PairFeature,
    PartitionManifest, PartitionRow, RankedPrediction, ScoreRecord,
)
from arxiv_pipeline.matching.io import digest


def _clean(ctx, output):
    return {'publications': [asdict(b) for b in ctx['bundles']]}


def _label(ctx, output):
    labels = []
    for b in ctx['bundles']:
        for q in b.bibtex:
            target = next((r.ref_arxiv_id for r in b.references if q.title and q.title == r.ref_title), None)
            labels.append(LabelRecord(b.arxiv_id, q.citation_key, target,
                'matched' if target else 'unmatched', 'manual', annotator='synthetic-tv1',
                reviewer='synthetic-tv5', review_status='accepted', notes='SYNTHETIC FIXTURE'))
    return {'labels': [r.to_dict() for r in labels]}


def _split(ctx, output):
    ids = sorted(b.arxiv_id for b in ctx['bundles'])
    rows = [PartitionRow(p, ('train', 'valid', 'test')[i % 3], 'manual') for i, p in enumerate(ids)]
    result = PartitionManifest(ctx['config']['seed'], 5, ctx['population_snapshot_id'],
        digest([r.to_dict() for r in ctx['labels']]), ids, [], rows)
    return {'partitions': result.to_dict()}


def _features(ctx, output):
    schema = FeatureSchema([FeatureSpec('synthetic_title_exact', 'float64', False,
        0.0, 1.0, 'zero when absent', 'Synthetic orchestration feature only', ['title', 'ref_title'])])
    rows = [PairFeature(b.arxiv_id, q.citation_key, r.ref_arxiv_id,
        {'synthetic_title_exact': float(bool(q.title) and q.title == r.ref_title)})
        for b in ctx['bundles'] for q in b.bibtex for r in b.references]
    return {'features': FeatureBatch(rows, schema.schema_id).to_dict(), 'feature_schema': schema.to_dict()}


def _train(ctx, output):
    return {'synthetic': True, 'note': 'Fixture scorer, not a trained ML model'}


def _score(ctx, output):
    return {'scores': [asdict(ScoreRecord(*r.key, r.values['synthetic_title_exact']))
                       for r in ctx['score_features'].rows]}


def _rank(ctx, output):
    scores = [ScoreRecord(**r) for r in ctx['score']['scores']]
    partitions = {r.arxiv_id: r.partition for r in ctx['partition_manifest'].rows}
    truth = {r.key: r.groundtruth_arxiv_id for r in ctx['labels'] if r.is_supervised}
    result = []
    for b in ctx['split_bundles']:
        for q in b.bibtex:
            ordered = sorted([s for s in scores if s.arxiv_id == b.arxiv_id and s.citation_key == q.citation_key],
                             key=lambda s: (-s.score, s.ref_arxiv_id))
            ids = [s.ref_arxiv_id for s in ordered[:5]]
            ids += [None] * (5 - len(ids))
            result.append(asdict(RankedPrediction(b.arxiv_id, partitions[b.arxiv_id],
                q.citation_key, truth.get((b.arxiv_id, q.citation_key)), *ids)))
    return {'predictions': result}


def _evaluate(ctx, output):
    truth = {r.key: r for r in ctx['labels'] if r.is_supervised}
    rows = [r for r in ctx['rank']['predictions'] if r['partition'] == 'test'
            and (r['arxiv_id'], r['citation_key']) in truth]
    if not rows: raise ValueError('No synthetic labeled test queries')
    total = 0.0
    for row in rows:
        label = truth[(row['arxiv_id'], row['citation_key'])]
        ids = [row[f'cand_{i}'] for i in range(1, 6)]
        if label.groundtruth_arxiv_id is not None and label.groundtruth_arxiv_id in ids:
            total += 1.0 / (ids.index(label.groundtruth_arxiv_id) + 1)
    return dict(synthetic=True, mrr_at_5=total / len(rows), queries=len(rows),
                note='SYNTHETIC smoke metric; not experimental evidence')


def _export(ctx, output):
    from arxiv_pipeline.matching.pipeline import StageOutput, TABLES
    tables = {}
    for name in sorted(TABLES):
        rel = f'smoke-export/{name}.json'
        atomic_json(output / rel, dict(synthetic=True,
            note='Fixture handoff; not production Parquet', table=name))
        tables[name] = rel
    return StageOutput(dict(synthetic=True, tables=tables), list(tables.values()))


def fixture_adapters():
    return dict(clean=_clean, label=_label, split=_split, features=_features,
                train=_train, score=_score, rank=_rank, evaluate=_evaluate, export=_export)


def prepare_fixture(root):
    """Create a separate synthetic M1/M2 handoff, or reuse it without rewriting."""
    from arxiv_pipeline.common.utils import paper_key
    root = Path(root)
    ids = ['2310.12345', '2310.12346', '2310.12347', 'hep-th/9901001']
    config = dict(ms1_root=root / 'ms1', ms2_root=root / 'ms2', ids=ids,
                  fixture_mode=True, seed=7, population_count=len(ids))
    marker = root / 'fixture_manifest.json'
    if root.exists():
        if not marker.is_file() or json.loads(marker.read_text(encoding='utf-8')).get('synthetic') is not True:
            raise ValueError('Refusing to overwrite a non-fixture input directory')
        return config
    root.mkdir(parents=True)
    atomic_json(marker, dict(synthetic=True, note='T1 offline smoke only; not quota evidence'))

    def checkpoint(source, stage, paper, artifacts, status, schema):
        relative = f'state/stages/{stage}/{paper_key(paper)}/paper.json'
        atomic_json(source / relative, dict(stage=stage, paper_id=paper, version=None,
            schema_version=schema, status=status, artifacts=artifacts, synthetic=True))
        if schema == '2.0':
            atomic_json(source / relative.replace('.json', '.dependencies.json'), dict(artifacts={
                rel: hashlib.sha256((source / rel).read_bytes()).hexdigest() for rel in artifacts.values()}))

    for index, paper in enumerate(ids):
        key = paper_key(paper)
        title = ('Synthetic exact title', 'Synthetic ambiguous title', 'Synthetic unmatched title', '')[index]
        bib = dict(arxiv_id=paper, citation_key='RefA', entry_type='article', title=title,
                   authors=['Synthetic Author'], year=2020, doi=None, journal=None, url=None)
        refs = [] if index == 3 else [dict(arxiv_id=paper, ref_arxiv_id=f'2001.{index+1:05d}',
            ref_title=title if index < 2 else 'Other synthetic work', ref_authors=['Synthetic Author'],
            ref_submission_date='2020-01-01', ref_s2_id=None)]
        if index == 1:
            refs.append(dict(refs[0], ref_arxiv_id='2001.00009'))
        if index == 0:
            refs.append(dict(refs[0], ref_arxiv_id='2001.00008', ref_title='Synthetic negative candidate'))
        ms1, ms2 = config['ms1_root'], config['ms2_root']
        rel = f'metadata/{key}.json'
        atomic_json(ms1 / rel, dict(schema_version='1.0', arxiv_id=paper,
            title=f'Synthetic publication {paper}', authors=['Synthetic Author'],
            submission_date='2023-10-01T00:00:00Z', revised_dates=[], venue=None))
        checkpoint(ms1, 'metadata', paper, {'metadata': rel}, 'success', '1.0')
        rel = f'references/{key}.json'; atomic_json(ms1 / rel, refs)
        checkpoint(ms1, 'references', paper, {'references': rel}, 'success' if refs else 'no_data', '1.0')
        rel = f'validation/{key}.json'
        atomic_json(ms1 / rel, dict(schema_version='1.0', paper_id=paper, valid=True, errors=[]))
        checkpoint(ms1, 'validate', paper, {'validation': rel}, 'success', '1.0')
        rel = f'tables/{key}/bibtex.json'; atomic_json(ms2 / rel, [bib])
        tables = dict(bibtex=rel)
        for name in ('elements', 'hierarchy'):
            tables[name] = f'tables/{key}/{name}.json'
            atomic_json(ms2 / tables[name], [])
        pm = f'tables/{key}/parsing_manifest.json'
        atomic_json(ms2 / pm, dict(schema_version='2.0', arxiv_id=paper, complete=True,
            input_snapshot_id='synthetic-ms1', expected_versions=[1],
            versions=[dict(version=1, status='success')], tables=tables))
        checkpoint(ms2, 'deduplicate', paper, dict(tables, parsing_manifest=pm), 'success', '2.0')
        rel = f'validation/parsing/{key}.json'
        atomic_json(ms2 / rel, dict(schema_version='2.0', arxiv_id=paper, valid=True,
                                  errors=[], input_snapshot_id='synthetic-ms1'))
        checkpoint(ms2, 'validate_parse', paper, {'validation': rel}, 'success', '2.0')
        atomic_json(ms2 / f'state/parsing/{key}.json', dict(arxiv_id=paper, complete=True,
            expected_versions=[1], selected_versions=[1], status='success'))
    return config
