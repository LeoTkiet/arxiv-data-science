"""MS2 benchmark regressions using real offline pipeline checkpoints."""
import json
import platform
import shutil
from pathlib import Path

import pytest

from arxiv_pipeline.common.schemas import StageResult
from arxiv_pipeline.common.utils import atomic_json, utc_now
from arxiv_pipeline.export.benchmark import generate_parsing_summary, count_renamed_citations_for_paper
from arxiv_pipeline.pipeline import parse

PAPER = '2310.12345'


def make_parsed_output(tmp_path, count=2):
    source, output = tmp_path / 'ms1', tmp_path / 'ms2'
    versions = []
    for n in range(1, count + 1):
        raw = f'raw/{PAPER}/v{n}/source'
        corpus = f'corpus/{PAPER}/v{n}'
        versions.append(dict(version=n, versioned_id=f'{PAPER}v{n}', source_path=raw, corpus_path=corpus))
        target = source / raw
        target.parent.mkdir(parents=True)
        target.write_text('source archive', encoding='utf-8')
        body = r'\section{Intro} Text.'
        if n in (2, 10):
            key = f'k{n}'
            body += (r' \cite{' + key + '}.') * (3 if n == 10 else 1)
            body += r'\bibliography{refs}'
        tex = source / corpus / 'main.tex'
        tex.parent.mkdir(parents=True)
        tex.write_text(r'\documentclass{article}\begin{document}' + body + r'\end{document}', encoding='utf-8')
        if n in (2, 10):
            (tex.parent / 'refs.bib').write_text(
                '@article{' + key + ', title={The same long reference title}, '
                'author={Author}, year={2020}, doi={10.1234/same}}', encoding='utf-8')

        for stage, artifacts in [('download', {'source': raw}), ('extract', {'corpus': corpus})]:
            atomic_json(source / f'state/stages/{stage}/{PAPER}/{n}.json',
                        StageResult(stage, PAPER, n, 'success', utc_now(), artifacts).to_dict())
    atomic_json(source / 'state/discovery/paper.json', dict(
        schema_version='1.0', input_id=PAPER, arxiv_id=PAPER, status='success',
        metadata={'latest_version': count}, versions=versions))
    summary = parse.run([PAPER], output, input_root=source)
    assert summary['complete'], summary
    return output


def summarize(output, **kwargs):
    return generate_parsing_summary(output, wall_time_sec=20, peak_ram_mb=10, avg_ram_mb=8, **kwargs)


def test_note_only_source_reference_passes_real_pipeline_validation(tmp_path):
    output = make_parsed_output(tmp_path, count=1)
    corpus = tmp_path / f'ms1/corpus/{PAPER}/v1'
    (corpus / 'main.tex').write_text(
        r'\documentclass{article}\begin{document}\section{Intro}'
        r'A note \cite{footnote1}.\bibliography{refs}\end{document}', encoding='utf-8')
    (corpus / 'refs.bib').write_text(
        '@misc{footnote1, note={Additional explanation without bibliographic fields.}}', encoding='utf-8')
    result = parse.run([PAPER], output, input_root=tmp_path / 'ms1', resume=True)
    assert result['complete'], result
    report = json.loads((output / f'validation/parsing/{PAPER}.json').read_text())
    assert report['valid'] and report['errors'] == []
    assert report['counts']['bibtex_entries'] == 1


@pytest.mark.parametrize('full_scope,assigned_count', [(False, 2), (True, 2), (True, 1)])
def test_colab_acceptance_cell_runs_real_modules_and_checks_assignment_coverage(tmp_path, full_scope, assigned_count):
    make_parsed_output(tmp_path, count=1)
    notebook = json.loads((Path(__file__).parents[2] / 'colab/MS2_Integration_CPU.ipynb').read_text(encoding='utf-8'))
    code = next(''.join(cell['source']) for cell in notebook['cells']
                if cell['cell_type'] == 'code' and 'results = []' in ''.join(cell['source']))
    context = dict(JOBS=[dict(member='tv1', input=str(tmp_path / 'ms1'),
                             ids=None if full_scope else [PAPER], total_assigned=assigned_count,
                             assignment_range=f'{PAPER}:' + ('2310.12346' if assigned_count == 2 else PAPER))],
                   OUTPUT_BASE=tmp_path / 'acceptance', CODE_SHA='test-offline',
                   RESUME=True, CHECK_RESUME=True, Path=Path)
    exec(compile(code, 'MS2_Integration_CPU.ipynb', 'exec'), context)
    result = json.loads((tmp_path / 'acceptance/tv1/acceptance.json').read_text())
    expected_complete = not full_scope or assigned_count == 1
    assert result['complete'] is expected_complete
    assert result['resume_all_cached'] is expected_complete
    assert result['unseen_assigned_ids'] == (['2310.12346'] if assigned_count == 2 else [])
    assert result['full_assignment_accepted'] is (full_scope and assigned_count == 1)
    assert result['scope'] == ('all_assigned_ids' if full_scope else 'selected_sample')
    assert not (tmp_path / 'acceptance/group-acceptance.json').exists()


def test_colab_acceptance_rejects_missing_selected_discovery(tmp_path):
    make_parsed_output(tmp_path, count=1)
    notebook = json.loads((Path(__file__).parents[2] / 'colab/MS2_Integration_CPU.ipynb').read_text(encoding='utf-8'))
    code = next(''.join(cell['source']) for cell in notebook['cells']
                if cell['cell_type'] == 'code' and 'results = []' in ''.join(cell['source']))
    context = dict(JOBS=[dict(member='tv1', input=str(tmp_path / 'ms1'), ids=['2310.12346'],
                             total_assigned=2, assignment_range=f'{PAPER}:2310.12346')],
                   OUTPUT_BASE=tmp_path / 'acceptance', CODE_SHA='test-offline',
                   RESUME=True, CHECK_RESUME=True, Path=Path)
    exec(compile(code, 'MS2_Integration_CPU.ipynb', 'exec'), context)
    result = json.loads((tmp_path / 'acceptance/tv1/acceptance.json').read_text())
    assert result['complete'] is False
    assert result['incomplete_papers'] == ['2310.12346']
    assert result['full_assignment_accepted'] is False


@pytest.mark.parametrize('member,assignment_range', [
    ('tv1', '2508.12872:2508.17871'),
    ('tv2', '2506.15788:2506.20787'),
    ('tv3', '2411.08799:2411.13798'),
    ('tv4', '2410.18025:2410.23024'),
    ('tv5', '2410.03025:2410.08024'),
])
@pytest.mark.parametrize('mode', ['SMOKE', 'FULL'])
def test_member_notebook_selects_official_range_and_separate_output(tmp_path, member, assignment_range, mode):
    repo = Path(__file__).parents[2]
    notebook = json.loads((repo / 'colab/MS2_Integration_CPU.ipynb').read_text(encoding='utf-8'))
    code = next(''.join(cell['source']) for cell in notebook['cells']
                if cell['cell_type'] == 'code' and 'MEMBER_ROWS =' in ''.join(cell['source']))
    source = tmp_path / 'ms1'
    source.mkdir()
    smoke_id = assignment_range.split(':')[0]
    context = dict(Path=Path, REPO=repo, MEMBER=member, RUN_MODE=mode,
                   MS1_INPUT=str(source), OUTPUT_ROOT=str(tmp_path / 'outputs'),
                   RUN_TAG='same-team-run', SMOKE_IDS=smoke_id, VERIFY_RESUME=False,
                   CODE_SHA='test-pinned-code')
    exec(compile(code, 'member-config', 'exec'), context)
    job, = context['JOBS']
    assert job['member'] == member and job['assignment_range'] == assignment_range
    assert job['total_assigned'] == 5000
    assert job['ids'] == ([smoke_id] if mode == 'SMOKE' else None)
    assert context['OUTPUT_BASE'] / member == tmp_path / 'outputs/same-team-run' / mode.lower() / member
    assert context['CHECK_RESUME'] is (mode == 'SMOKE')


def test_member_zip_contains_only_selected_member(tmp_path):
    import zipfile
    notebook = json.loads((Path(__file__).parents[2] / 'colab/MS2_Integration_CPU.ipynb').read_text(encoding='utf-8'))
    code = next(''.join(cell['source']) for cell in notebook['cells']
                if cell['cell_type'] == 'code' and 'CREATE_ZIP = False' in ''.join(cell['source']))
    for member in ['tv1', 'tv2']:
        (tmp_path / member).mkdir()
        (tmp_path / member / 'acceptance.json').write_text('{}')
        (tmp_path / member / 'result.txt').write_text(member)
    context = dict(Path=Path, OUTPUT_BASE=tmp_path, MEMBER='tv1', RUN_TAG='test', RUN_MODE='FULL')
    exec(compile(code.replace('CREATE_ZIP = False', 'CREATE_ZIP = True'), 'member-zip', 'exec'), context)
    with zipfile.ZipFile(tmp_path / 'TV_tv1_MS2_RESULTS_test_full.zip') as bundle:
        assert set(bundle.namelist()) == {'tv1/acceptance.json', 'tv1/result.txt'}


def test_benchmark_uses_current_scope_and_measured_denominator(tmp_path):
    output = make_parsed_output(tmp_path)
    shutil.copytree(output / f'tables/{PAPER}', output / 'tables/2310.99999')
    summary = summarize(output, total_assigned=1000)
    manifest = json.loads((output / f'tables/{PAPER}/parsing_manifest.json').read_text())
    assert summary['deduplication_statistics']['elements_after_dedup'] == manifest['counts']['elements_after']
    assert summary['coverage_statistics']['total_assigned_publications'] == 1000
    assert summary['performance_metrics']['avg_time_per_publication_sec'] == 20
    assert summary['environment']['platform'] == platform.platform()


def test_benchmark_rejects_changed_snapshot_in_real_checkpoint_output(tmp_path):
    output = make_parsed_output(tmp_path)
    path = output / f'tables/{PAPER}/parsing_manifest.json'
    manifest = json.loads(path.read_text())
    manifest['input_snapshot_id'] = 'wrong-snapshot'
    atomic_json(path, manifest)
    with pytest.raises(ValueError):
        summarize(output)


def test_benchmark_rejects_changed_normalization_artifact(tmp_path):
    output = make_parsed_output(tmp_path)
    path = output / f'parsed/{PAPER}/v2/normalized_tree.json'
    tree = json.loads(path.read_text())
    tree['nodes'][-1]['cleaned_content'] = r'\cite{k2}'
    atomic_json(path, tree)
    with pytest.raises(ValueError):
        count_renamed_citations_for_paper(output, PAPER, expected_versions=[1, 2])


def test_citation_mapping_uses_numeric_version_order(tmp_path):
    output = make_parsed_output(tmp_path, count=10)
    assert count_renamed_citations_for_paper(output, PAPER, expected_versions=list(range(1, 11))) == 3
    assert summarize(output)['deduplication_statistics']['citations_renamed_count'] == 3


def test_missing_run_manifest_cannot_look_like_zero_measurements(tmp_path):
    with pytest.raises(ValueError):
        summarize(tmp_path)



def test_benchmark_cross_checks_validation_snapshot_after_export_hash_is_updated(tmp_path):
    import hashlib
    output = make_parsed_output(tmp_path)
    relative = f'tables/{PAPER}/parsing_manifest.json'
    path = output / relative
    manifest = json.loads(path.read_text())
    manifest['input_snapshot_id'] = 'another-snapshot'
    atomic_json(path, manifest)
    dependencies = output / f'state/stages/deduplicate/{PAPER}/paper.dependencies.json'
    meta = json.loads(dependencies.read_text())
    meta['artifacts'][relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    atomic_json(dependencies, meta)
    with pytest.raises(ValueError, match='snapshot mismatch'):
        summarize(output)


def test_live_benchmark_runs_real_pipeline_and_writes_measured_summary(tmp_path):
    from arxiv_pipeline.export.benchmark import execute_pipeline_benchmark
    output = make_parsed_output(tmp_path)
    target = tmp_path / 'summary.json'
    tex = tmp_path / f'ms1/corpus/{PAPER}/v2/main.tex'
    tex.write_text(tex.read_text().replace(r'\end{document}', 'Distinct content ' * 1000 + r'.\end{document}'), encoding='utf-8')
    summary = execute_pipeline_benchmark(tmp_path / 'ms1', output, identifiers=[PAPER], resume=True,
                                         summary_out=target, total_assigned=1000)
    assert json.loads(target.read_text()) == summary
    assert summary['coverage_statistics']['successfully_parsed_publications'] == 1
    assert summary['performance_metrics']['end_to_end_wall_time_sec'] >= 0
    assert summary['performance_metrics']['final_output_storage_mb'] > 0
