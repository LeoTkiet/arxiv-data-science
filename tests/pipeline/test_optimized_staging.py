import json
import shutil
import time
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
from arxiv_pipeline.pipeline import optimized_staging as fast
from arxiv_pipeline.pipeline import local_staging as original, completion_index, parse
from arxiv_pipeline.common.utils import atomic_json
from tests.parsing.test_ms2_benchmark import PAPER, make_parsed_output


def item(root):
    return dict(member='tv1', input=str(root / 'ms1'), ids=None,
                total_assigned=1, assignment_range=f'{PAPER}:{PAPER}')


def test_real_pipeline_tables_equal_and_input_immutable(tmp_path):
    output = make_parsed_output(tmp_path, count=2)
    source = tmp_path / 'ms1'
    before = {p.relative_to(source): p.read_bytes() for p in source.rglob('*') if p.is_file()}
    tables = {p.name: p.read_bytes() for p in (output / f'tables/{PAPER}').iterdir()}
    result = fast.run(item(tmp_path), output, local_root=tmp_path / 'work', code_sha='offline', workers=2)
    assert result['complete'] and result['full_assignment_accepted']
    assert result['cached_stages'] == result['total_stages'] == 10
    with zipfile.ZipFile(output / 'MS2_RESULTS.zip') as z:
        for name, content in tables.items():
            assert z.read(f'tv1/tables/{PAPER}/{name}') == content
    assert before == {p.relative_to(source): p.read_bytes() for p in source.rglob('*') if p.is_file()}


def test_warm_resume_no_source_copy_no_legacy_tree_scan(tmp_path):
    output = make_parsed_output(tmp_path, count=1)
    job = item(tmp_path)
    fast.run(job, output, local_root=tmp_path / 'work', code_sha='offline', workers=2)
    with patch.object(fast, '_stage_inputs', side_effect=AssertionError('source recopied')):
        result = fast.run(job, output, local_root=tmp_path / 'work', code_sha='offline', workers=2)
    assert result['skipped_completed_papers'] == 1 and result['total_stages'] == 0
    assert result['full_assignment_accepted']


def test_processing_only_never_publishes_handoff(tmp_path):
    output = make_parsed_output(tmp_path, count=1)
    result = fast.run(item(tmp_path), output, local_root=tmp_path / 'work',
                      code_sha='offline', workers=2, finalize=False)
    assert result['complete'] and not result['full_assignment_accepted']
    assert not (output / 'MS2_RESULTS.zip').exists()
    assert json.loads((output / 'delivery.json').read_text())['status'] == 'checkpointed_not_exported'
    # A subsequent full export still authenticates and includes all tables.
    assert fast.run(item(tmp_path), output, local_root=tmp_path / 'work',
                    code_sha='offline', workers=2)['full_assignment_accepted']


def test_failed_contextual_receipt_not_downloaded_during_index_load(tmp_path):
    output = make_parsed_output(tmp_path, count=1)
    source = tmp_path / 'ms1'
    job = item(tmp_path)
    identity = completion_index.context(source.resolve(), job)
    completion_index.save(output, identity, {}, origin=dict(job=job, code_sha='40103bacb5c34a85baecaa16fc78a856044961e8'))
    records, _, _ = parse._discover(source, [PAPER])
    journal = {PAPER: dict(context=identity, completed={}, archive='missing.zip', sha256='0' * 64)}
    workspace = tmp_path / 'work'
    workspace.mkdir()
    with patch.object(fast, '_cached_archive', side_effect=AssertionError('known failed ZIP redownloaded')):
        assert fast.load_completed(output, identity, records, journal, {}, workspace, 2) == {}


def test_legacy_failed_zip_negative_result_is_cached(tmp_path):
    output = make_parsed_output(tmp_path, count=1)
    job = item(tmp_path)
    identity = completion_index.context((tmp_path / 'ms1').resolve(), job)
    # The fixture was produced by today's parser; only the ZIP layout is legacy.
    completion_index.save(output, identity, {}, origin=dict(job=job, code_sha='fixture-compatible',
        processing_revision=identity['processing_revision']))
    archive = tmp_path / 'failed.zip'
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr(f'state/parsing/{PAPER}.json', json.dumps(dict(arxiv_id=PAPER, complete=False)))
    receipt = dict(archive='failed.zip', sha256=fast._hash(archive), paper_files={PAPER: []})
    records, _, _ = parse._discover(tmp_path / 'ms1', [PAPER])
    workspace = tmp_path / 'work'
    workspace.mkdir()
    with patch.object(fast, '_cached_archive', return_value=archive) as download:
        for _ in range(2):
            assert fast.load_completed(output, identity, records, {PAPER: receipt}, {}, workspace, 2) == {}
        assert download.call_count == 1


def test_newer_incomplete_receipt_invalidates_existing_success(tmp_path):
    output = make_parsed_output(tmp_path, count=1)
    job = item(tmp_path)
    identity = completion_index.context((tmp_path / 'ms1').resolve(), job)
    records, _, _ = parse._discover(tmp_path / 'ms1', [PAPER])
    outcome = parse._read(output / f'state/parsing/{PAPER}.json')
    entry = completion_index._certificate(records[0][0], outcome,
        parse._read(output / f'tables/{PAPER}/parsing_manifest.json'),
        parse._read(output / f'validation/parsing/{PAPER}.json'))
    completion_index.save(output, identity, {PAPER: entry})
    receipt = dict(context=identity, completed={})
    assert fast.load_completed(output, identity, records, {PAPER: receipt}, {}, tmp_path, 2) == {}


def test_archive_checksum_still_enforced(tmp_path):
    output = tmp_path / 'drive'
    output.mkdir()
    (output / 'bad.zip').write_bytes(b'bad')
    with pytest.raises(ValueError, match='checksum'):
        fast._cached_archive(output, dict(archive='bad.zip', sha256='0' * 64), tmp_path / 'cache')


def test_parallel_inventory_copies_exactly_the_same_files(tmp_path):
    make_parsed_output(tmp_path, count=2)
    source = tmp_path / 'ms1'
    records, _, _ = parse._discover(source, [PAPER])
    baseline, accelerated = tmp_path / 'old', tmp_path / 'new'
    original._stage_inputs(source, baseline, records, 2, discovery_root=source)
    fast._stage_inputs(source, accelerated, records, 2, discovery_root=source)
    def files(root):
        return {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    assert files(baseline) == files(accelerated)




def test_fresh_run_creates_index_and_publishes_without_legacy_output(tmp_path):
    make_parsed_output(tmp_path, count=1)
    output = tmp_path / 'fresh'
    result = fast.run(item(tmp_path), output, local_root=tmp_path / 'work', code_sha='test')
    assert result['complete'] and result['full_assignment_accepted']
    assert json.loads((output / 'completion-index.json').read_text())['papers'][PAPER]
    assert (output / 'MS2_RESULTS.zip').is_file()


@pytest.mark.parametrize('kind', ['source', 'snapshot_id', 'member', 'processing_revision'])
def test_incompatible_index_stops_before_mutating_output(tmp_path, kind):
    output = make_parsed_output(tmp_path, count=1)
    job = item(tmp_path)
    identity = completion_index.context((tmp_path / 'ms1').resolve(), job)
    identity[kind] = 'other'
    completion_index.save(output, identity, {})
    before = {p.relative_to(output): p.read_bytes() for p in output.rglob('*') if p.is_file()}
    with pytest.raises(ValueError, match='context'):
        fast.run(job, output, local_root=tmp_path / 'work', code_sha='test')
    assert before == {p.relative_to(output): p.read_bytes() for p in output.rglob('*') if p.is_file()}


def test_legacy_success_migrates_once_and_skips_input_copy(tmp_path):
    output = make_parsed_output(tmp_path, count=1)
    job = item(tmp_path)
    identity = completion_index.context((tmp_path / 'ms1').resolve(), job)
    atomic_json(output / 'acceptance-config.json', dict(job=job, processing_revision=identity['processing_revision']))
    with patch.object(fast, '_stage_inputs', side_effect=AssertionError('completed source recopied')):
        for _ in range(2):
            result = fast.run(job, output, local_root=tmp_path / 'work', code_sha='test')
            assert result['skipped_completed_papers'] == 1
    assert json.loads((output / 'optimized-legacy-catalog.json').read_text())['loose_done']


def test_interrupted_batch_recovers_committed_receipt(tmp_path):
    make_parsed_output(tmp_path, count=1)
    output = tmp_path / 'fresh'
    job = dict(item(tmp_path), total_assigned=2, assignment_range=f'{PAPER}:2310.12346')
    original = fast._stage_inputs
    calls = 0
    def interrupt(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt('runtime stopped')
        return original(*args, **kwargs)
    with patch.object(fast, '_stage_inputs', side_effect=interrupt):
        with pytest.raises(KeyboardInterrupt):
            fast.run(job, output, local_root=tmp_path / 'work', code_sha='test', batch_size=1)
    assert len(list((output / 'local-batches').glob('*.json'))) == 1
    # Simulate losing the last index write: durable batch receipt must reconstruct it.
    atomic_json(output / 'completion-index.json', dict(format='ms2-completion-index-1',
        context=completion_index.context((tmp_path / 'ms1').resolve(), job), papers={}, origin=None))
    result = fast.run(job, output, local_root=tmp_path / 'work2', code_sha='test', batch_size=1)
    assert result['skipped_completed_papers'] == 1
    assert result['unseen_assigned_ids'] == ['2310.12346']
    assert not result['full_assignment_accepted']


def test_notebook_uses_shared_runner_and_only_three_form_inputs():
    import ast
    nb = json.loads((Path(__file__).parents[2] / 'colab/MS2_Integration_CPU.ipynb').read_text(encoding='utf-8'))
    codes = [''.join(c['source']) for c in nb['cells'] if c['cell_type'] == 'code']
    for code in codes:
        ast.parse(code)
    assert sum(code.count('#@param') for code in codes) == 3
    run_cell = next(c for c in codes if 'results = []' in c)
    assert 'pipeline.optimized_staging import run' in run_cell
    assert 'COPY_WORKERS = 8' in run_cell and 'BATCH_SIZE = 50' in run_cell
    assert 'finalize=True' in run_cell
    setup = next(c for c in codes if 'git' in c and 'clone' in c)
    assert 'importlib.invalidate_caches()' in setup
    assert 'sys.modules.pop' in setup


def test_warm_resume_stages_only_new_paper(tmp_path):
    import shutil
    output = make_parsed_output(tmp_path, count=1)
    second = '2310.12346'
    with patch('tests.parsing.test_ms2_benchmark.PAPER', second):
        make_parsed_output(tmp_path / 'other', count=1)
    other = tmp_path / 'other/ms1'
    for folder in ['raw', 'corpus', 'state/stages']:
        shutil.copytree(other / folder, tmp_path / 'ms1' / folder, dirs_exist_ok=True)
    shutil.copyfile(other / 'state/discovery/paper.json',
                    tmp_path / 'ms1/state/discovery/second.json')
    item = dict(job([PAPER], count=2), input=str(tmp_path / 'ms1'))
    fast.run(item, output, local_root=tmp_path / 'work', code_sha='test')
    item['ids'] = None
    stage = fast._stage_inputs
    with patch.object(fast, '_stage_inputs', wraps=stage) as staged:
        result = fast.run(item, output, local_root=tmp_path / 'work2', code_sha='test')
    assert result['complete'] and result['skipped_completed_papers'] == 1
    assert [[record['arxiv_id'] for record, _, _ in call.args[2]]
            for call in staged.call_args_list] == [[second]]
    with zipfile.ZipFile(output / 'MS2_RESULTS.zip') as bundle:
        assert f'tv1/tables/{PAPER}/elements.json' in bundle.namelist()
        assert f'tv1/tables/{second}/elements.json' in bundle.namelist()


def test_invalid_validation_cannot_be_imported_as_complete(tmp_path):
    output = make_parsed_output(tmp_path, count=1)
    item = dict(job([PAPER]), input=str(tmp_path / 'ms1'))
    (output / 'acceptance-config.json').write_text(json.dumps(dict(
        job=item, code_sha='fixture-compatible',
        processing_revision=completion_index.context((tmp_path / 'ms1').resolve(), item)['processing_revision'])))
    report = output / f'validation/parsing/{PAPER}.json'
    data = json.loads(report.read_text())
    data['valid'] = False
    report.write_text(json.dumps(data))
    result = fast.run(item, output, local_root=tmp_path / 'work', code_sha='test')
    assert result['skipped_completed_papers'] == 0
    assert result['total_stages'] == 6


def test_failed_upload_has_no_committed_batch_and_can_retry(tmp_path):
    output = make_parsed_output(tmp_path, count=1)
    item = dict(job([PAPER]), input=str(tmp_path / 'ms1'))
    with patch.object(fast, '_publish_archive', side_effect=OSError('Drive unavailable')):
        with pytest.raises(OSError, match='Drive unavailable'):
            fast.run(item, output, local_root=tmp_path / 'work', code_sha='test')
    assert not list((output / 'local-batches').glob('*.json'))
    assert json.loads((output / 'delivery.json').read_text())['status'] == 'running'
    result = fast.run(item, output, local_root=tmp_path / 'work2', code_sha='test')
    assert result['complete']
    assert json.loads((output / 'delivery.json').read_text())['status'] == 'published'


def test_malformed_ms1_checkpoint_is_reported_as_incomplete(tmp_path):
    output = make_parsed_output(tmp_path, count=1)
    checkpoint = tmp_path / f'ms1/state/stages/download/{PAPER}/1.json'
    data = json.loads(checkpoint.read_text())
    data['artifacts'] = None
    checkpoint.write_text(json.dumps(data))
    result = fast.run(
        dict(job([PAPER]), input=str(tmp_path / 'ms1')),
        output, local_root=tmp_path / 'work', code_sha='test')
    assert result['complete'] is False and result['incomplete_papers'] == [PAPER]


def job(ids=None, count=1):
    return dict(member='tv1', ids=ids, total_assigned=count,
                assignment_range=f'{PAPER}:2310.{12344 + count}')


def test_malformed_legacy_configuration_is_not_overwritten(tmp_path):
    output = make_parsed_output(tmp_path, count=1)
    config = output / 'acceptance-config.json'
    config.write_text('broken{')
    with pytest.raises(ValueError):
        fast.run(item(tmp_path), output, local_root=tmp_path / 'work', code_sha='test')
    assert config.read_text() == 'broken{'


def test_changed_parser_rejects_legacy_index_before_overwriting_output(tmp_path):
    output = make_parsed_output(tmp_path, count=1)
    job = item(tmp_path)
    identity = completion_index.context((tmp_path / 'ms1').resolve(), job)
    legacy = dict(identity, processing_revision=completion_index._LEGACY_REVISION)
    completion_index.save(output, legacy, {})
    before = {p.relative_to(output):p.read_bytes() for p in output.rglob('*') if p.is_file()}
    with pytest.raises(ValueError, match='context differs'):
        fast.run(job, output, local_root=tmp_path/'work', code_sha='new-parser')
    assert before == {p.relative_to(output):p.read_bytes() for p in output.rglob('*') if p.is_file()}
