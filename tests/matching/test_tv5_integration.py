"""Exercise TV1 against delivered TV5 APIs, never substitute production mocks."""
from dataclasses import asdict
import json

import pytest

tv5_split = pytest.importorskip('arxiv_pipeline.matching.split')
tv5_export = pytest.importorskip('arxiv_pipeline.matching.export')

from arxiv_pipeline.common.utils import atomic_json
from arxiv_pipeline.matching import adapters
from arxiv_pipeline.matching.contracts import LabelRecord, ScoreRecord
from arxiv_pipeline.matching.io import digest, load_with_provenance
from arxiv_pipeline.matching.pipeline import StageOutput, _validate, run_matching
from tests.matching.test_io import ready_snapshot


def dataset(tmp_path):
    items, labels = [], []
    for index in range(37):
        paper = f'2401.{index + 1:05d}'
        refs = [] if index == 36 else [dict(arxiv_id=paper, ref_arxiv_id=f'2001.{r:05d}',
            ref_title=f'Synthetic reference {r}', ref_authors=['Synthetic Author'],
            ref_submission_date='2020-01-01', ref_s2_id=None) for r in (1, 2)]
        bib = []
        for q in range(20):
            target = refs[q % 2]['ref_arxiv_id'] if refs and q % 5 else None
            key = f'Ref{q:02d}'
            bib.append(dict(arxiv_id=paper, citation_key=key, entry_type='article',
                title=f'Synthetic reference {q % 2 + 1}' if target else 'Synthetic external book',
                authors=['Synthetic Author'], year=2020, doi=None, journal=None, url=None))
            labels.append(LabelRecord(paper, key, target, 'matched' if target else 'unmatched',
                'manual' if index < 25 else 'heuristic',
                annotator=f'synthetic-tv{index % 5 + 1}' if index < 25 else None,
                confidence=None if index < 25 else 0.99,
                heuristic_version=None if index < 25 else 'synthetic-rule', review_status='accepted'))
        if index == 0:
            refs.append(dict(arxiv_id=paper, ref_arxiv_id=None, ref_title='Synthetic non-arXiv book',
                ref_authors=[], ref_submission_date=None, ref_s2_id=None))
        items.append(dict(arxiv_id=paper, bibtex=bib, references=refs))
    cfg = dict(ready_snapshot(tmp_path / 'source', items=items),
        ids=[p['arxiv_id'] for p in items], output=tmp_path / 'output', population_count=145,
        group_size=5, seed=42, model_kinds=['logreg'])
    path = tmp_path / 'labels.json'
    atomic_json(path, [r.to_dict() for r in labels])
    cfg['labels'] = path
    loaded = [load_with_provenance(p, cfg, include_export_tables=True) for p in cfg['ids']]
    assert all(p.bundle.readiness == 'ready' for p in loaded)
    ctx = dict(config=cfg, bundles=[p.bundle for p in loaded], labels=labels,
        population_snapshot_id='synthetic-population',
        source_tables={name: [row for p in loaded for row in p.source_tables[name]]
            for name in ('papers', 'references', 'elements', 'hierarchy', 'bibtex')})
    return cfg, ctx


def split_context(tmp_path):
    cfg, ctx = dataset(tmp_path)
    result = adapters.split(ctx, cfg['output'])
    _validate('split', result, ctx)
    from arxiv_pipeline.matching.pipeline import _manifest
    ctx['partition_manifest'] = _manifest(result['partitions'])
    ctx['split_bundles'] = ctx['bundles']
    ctx['score'] = {'scores': [asdict(ScoreRecord(b.arxiv_id, q.citation_key, r.ref_arxiv_id,
        float(q.title == r.ref_title))) for b in ctx['bundles'] for q in b.bibtex for r in b.references]}
    return cfg, ctx


def test_real_split_preserves_current_label_and_population_snapshots(tmp_path):
    _, ctx = split_context(tmp_path)
    manifest = ctx['partition_manifest']
    assert manifest.label_snapshot_id == digest([r.to_dict() for r in ctx['labels']])
    assert manifest.population_snapshot_id == ctx['population_snapshot_id']
    assert manifest.counts['valid'] == {'manual': 5, 'auto': 5}
    assert manifest.counts['test'] == {'manual': 5, 'auto': 5}


def test_real_prediction_and_evaluation_use_tv5_payloads(tmp_path):
    cfg, ctx = split_context(tmp_path)
    ctx['rank'] = adapters.rank(ctx, cfg['output'])
    _validate('rank', ctx['rank'], ctx)
    assert len(ctx['rank']['predictions']) == 740
    result = adapters.evaluate(ctx, cfg['output'])
    _validate('evaluate', result, ctx)
    assert result['queries'] == 200
    assert result['mrr_at_5'] == result['report']['metrics']['mrr_at_5']
    assert result['queries'] == result['report']['metrics']['counts']['total_labeled']
    empty = [r for r in ctx['rank']['predictions'] if r['arxiv_id'] == '2401.00037']
    assert len(empty) == 20 and all(r['cand_1'] is None for r in empty)


def test_real_export_declares_every_shard_and_preserves_source_rows(tmp_path):
    cfg, ctx = split_context(tmp_path)
    ctx['rank'] = adapters.rank(ctx, cfg['output'])
    before = json.dumps(ctx['source_tables'], sort_keys=True)
    cfg['max_rows_per_shard'] = 100
    result = adapters.export(ctx, cfg['output'])
    assert isinstance(result, StageOutput)
    _validate('export', result.payload, ctx)
    assert before == json.dumps(ctx['source_tables'], sort_keys=True)
    declared = set(result.artifacts)
    assert all((cfg['output'] / p).is_file() and '\\' not in p for p in declared)
    assert set(p for paths in result.payload['tables'].values() for p in paths) <= declared
    assert result.payload['reference_filter']['excluded_non_arxiv'] == 1
    assert result.payload['synthetic'] is True
    doc = json.loads((cfg['output'] / result.payload['export_manifest']).read_text())
    assert doc['readback_verified'] and not doc['ready_for_hf_upload']
    assert doc['tables']['papers']['row_count'] == 37
    assert doc['tables']['predictions']['row_count'] == 740


def test_native_pipeline_resume_and_corrupt_export_recovery(tmp_path):
    pytest.importorskip('sklearn')
    cfg, _ = dataset(tmp_path)
    first = run_matching(cfg)
    assert first['complete'] and first['synthetic']
    assert len(first['stages']) == 117
    warm = run_matching(dict(cfg, resume=True))
    assert warm['complete'] and all(r['cached'] for r in warm['stages'])
    export = next(r for r in warm['stages'] if r['stage'] == 'export')
    shard = next(p for p in export['artifacts'] if p.endswith('.parquet'))
    (cfg['output'] / shard).write_bytes(b'corrupted export')
    recovered = run_matching(dict(cfg, resume=True))
    assert recovered['complete']
    assert all(r['cached'] for r in recovered['stages'] if r['stage'] != 'export')
    assert not next(r for r in recovered['stages'] if r['stage'] == 'export')['cached']


def test_metadata_changed_during_native_run_prevents_completion(tmp_path, monkeypatch):
    pytest.importorskip('sklearn')
    cfg, _ = dataset(tmp_path)
    original = adapters.export

    def changed(ctx, output):
        result = original(ctx, output)
        path = cfg['ms1_root'] / 'metadata/2401.00001.json'
        doc = json.loads(path.read_text()); doc['title'] += ' changed'
        atomic_json(path, doc)
        return result

    native = adapters.default_adapters()
    native['export'] = changed
    result = run_matching(cfg, adapters=native)
    assert not result['complete']
    assert any('ms1/metadata/2401.00001.json' in e for e in result['input_errors'])
