"""Local Colab staging uses real parser modules and filesystem checkpoints."""
import json
from unittest.mock import patch
import zipfile

import pytest

from arxiv_pipeline.pipeline import local_staging
from tests.parsing.test_ms2_benchmark import PAPER, make_parsed_output


def job(ids=None, count=1):
    return dict(member='tv1', ids=ids, total_assigned=count,
                assignment_range=f'{PAPER}:2310.{12344 + count}')


def test_local_run_preserves_legacy_resume_and_recovers_from_archives(tmp_path):
    output = make_parsed_output(tmp_path)
    item = dict(job([PAPER]), input=str(tmp_path / 'ms1'))
    before = {p.relative_to(tmp_path / 'ms1'): p.read_bytes()
              for p in (tmp_path / 'ms1').rglob('*') if p.is_file()}
    result = local_staging.run(item, output, local_root=tmp_path / 'work', code_sha='test',
                               batch_size=1, workers=2)
    assert result['complete'] and result['cached_stages'] == result['total_stages'] == 10
    receipt = json.loads((output / 'delivery.json').read_text())
    with zipfile.ZipFile(output / receipt['archive']) as bundle:
        assert json.loads(bundle.read(f'tv1/validation/parsing/{PAPER}.json'))['valid']
        archived_table = bundle.read(f'tv1/tables/{PAPER}/elements.json')
    # Force recovery from packaged checkpoints, with no legacy output available.
    recovered = tmp_path / 'recovered'
    recovered.mkdir()
    import shutil
    shutil.copytree(output / 'local-batches', recovered / 'local-batches')
    result = local_staging.run(item, recovered, local_root=tmp_path / 'work2',
                               code_sha='test', batch_size=1, workers=2)
    assert result['skipped_completed_papers'] == 1
    assert result['cached_stages'] == result['total_stages'] == 0
    assert len(list((recovered / 'local-batches').glob('*.json'))) == 1
    with zipfile.ZipFile(recovered / 'MS2_RESULTS.zip') as bundle:
        assert bundle.read(f'tv1/tables/{PAPER}/elements.json') == archived_table
    assert before == {p.relative_to(tmp_path / 'ms1'): p.read_bytes()
                      for p in (tmp_path / 'ms1').rglob('*') if p.is_file()}


def test_changed_input_recomputes_affected_stages(tmp_path):
    output = make_parsed_output(tmp_path)
    item = dict(job([PAPER]), input=str(tmp_path / 'ms1'))
    local_staging.run(item, output, local_root=tmp_path / 'work', code_sha='test')
    tex = tmp_path / f'ms1/corpus/{PAPER}/v2/main.tex'
    tex.write_text(tex.read_text().replace('Text.', 'Updated text.'), encoding='utf-8')
    item['snapshot_id'] = 'source-after-edit'
    result = local_staging.run(item, output, local_root=tmp_path / 'work2', code_sha='test')
    assert result['complete']
    assert result['cached_stages'] == 4


def test_interrupted_second_batch_keeps_first_batch_and_can_resume(tmp_path):
    output = make_parsed_output(tmp_path, count=1)
    item = dict(job(None, count=2), input=str(tmp_path / 'ms1'))
    original = local_staging._stage_inputs
    calls = 0

    def interrupt(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt()
        return original(*args, **kwargs)

    with patch.object(local_staging, '_stage_inputs', side_effect=interrupt), pytest.raises(KeyboardInterrupt):
        local_staging.run(item, output, local_root=tmp_path / 'work', code_sha='test', batch_size=1)
    partial = json.loads((output / 'parsing_summary.json').read_text())
    assert partial['processed_ids'] == 1 and partial['complete'] is False
    assert len(list((output / 'local-batches').glob('*.json'))) == 1
    result = local_staging.run(item, output, local_root=tmp_path / 'work2', code_sha='test', batch_size=1)
    assert result['complete'] is False and result['incomplete_papers'] == ['2310.12346']
    assert result['unseen_assigned_ids'] == ['2310.12346']


def test_corrupt_archive_is_not_used_as_success(tmp_path):
    output = make_parsed_output(tmp_path, count=1)
    item = dict(job([PAPER]), input=str(tmp_path / 'ms1'))
    local_staging.run(item, output, local_root=tmp_path / 'work', code_sha='test')
    next((output / 'local-batches').glob('*.zip')).write_bytes(b'corrupt')
    with pytest.raises(ValueError, match='checksum'):
        local_staging.run(item, output, local_root=tmp_path / 'work2', code_sha='test')
    assert json.loads((output / 'acceptance.json').read_text())['complete'] is False


def test_failed_upload_has_no_committed_batch_and_can_retry(tmp_path):
    output = make_parsed_output(tmp_path, count=1)
    item = dict(job([PAPER]), input=str(tmp_path / 'ms1'))
    with patch.object(local_staging, '_publish_archive', side_effect=OSError('Drive unavailable')):
        with pytest.raises(OSError, match='Drive unavailable'):
            local_staging.run(item, output, local_root=tmp_path / 'work', code_sha='test')
    assert not list((output / 'local-batches').glob('*.json'))
    assert json.loads((output / 'delivery.json').read_text())['status'] == 'running'
    result = local_staging.run(item, output, local_root=tmp_path / 'work2', code_sha='test')
    assert result['complete']
    assert json.loads((output / 'delivery.json').read_text())['status'] == 'published'


def test_malformed_ms1_checkpoint_is_reported_as_incomplete(tmp_path):
    output = make_parsed_output(tmp_path, count=1)
    checkpoint = tmp_path / f'ms1/state/stages/download/{PAPER}/1.json'
    data = json.loads(checkpoint.read_text())
    data['artifacts'] = None
    checkpoint.write_text(json.dumps(data))
    result = local_staging.run(
        dict(job([PAPER]), input=str(tmp_path / 'ms1')),
        output, local_root=tmp_path / 'work', code_sha='test')
    assert result['complete'] is False and result['incomplete_papers'] == [PAPER]


def test_legacy_completion_import_skips_all_source_staging(tmp_path):
    output = make_parsed_output(tmp_path, count=1)
    item = dict(job([PAPER]), input=str(tmp_path / 'ms1'))
    (output / 'acceptance-config.json').write_text(json.dumps(dict(
        job=item, code_sha='40103bacb5c34a85baecaa16fc78a856044961e8', frozen_input=True)))
    with patch.object(local_staging, '_stage_inputs', side_effect=AssertionError('completed input read')):
        result = local_staging.run(item, output, local_root=tmp_path / 'work', code_sha='test')
    assert result['complete'] and result['skipped_completed_papers'] == 1
    assert result['total_stages'] == 0


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
    local_staging.run(item, output, local_root=tmp_path / 'work', code_sha='test')
    item['ids'] = None
    stage = local_staging._stage_inputs
    with patch.object(local_staging, '_stage_inputs', wraps=stage) as staged:
        result = local_staging.run(item, output, local_root=tmp_path / 'work2', code_sha='test')
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
        job=item, code_sha='40103bacb5c34a85baecaa16fc78a856044961e8')))
    report = output / f'validation/parsing/{PAPER}.json'
    data = json.loads(report.read_text())
    data['valid'] = False
    report.write_text(json.dumps(data))
    result = local_staging.run(item, output, local_root=tmp_path / 'work', code_sha='test')
    assert result['skipped_completed_papers'] == 0
    assert result['total_stages'] == 6


def test_code_revision_change_disables_paper_skip(tmp_path):
    output = make_parsed_output(tmp_path, count=1)
    item = dict(job([PAPER]), input=str(tmp_path / 'ms1'))
    local_staging.run(item, output, local_root=tmp_path / 'work', code_sha='test')
    identity = local_staging.completion_index.context((tmp_path / 'ms1').resolve(), item)
    identity['processing_revision'] = 'different-processing-code'
    with patch.object(local_staging.completion_index, 'context', return_value=identity):
        result = local_staging.run(item, output, local_root=tmp_path / 'work2', code_sha='new')
    assert result['skipped_completed_papers'] == 0
    assert result['total_stages'] == 6


def test_interruption_does_not_certify_unknown_legacy_producer(tmp_path):
    output = make_parsed_output(tmp_path, count=1)
    item = dict(job([PAPER]), input=str(tmp_path / 'ms1'))
    (output / 'acceptance-config.json').write_text(json.dumps(dict(
        job=item, code_sha='unknown-code')))
    with patch.object(local_staging, '_stage_inputs', side_effect=KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            local_staging.run(item, output, local_root=tmp_path / 'work', code_sha='new')
    result = local_staging.run(item, output, local_root=tmp_path / 'work2', code_sha='new')
    assert result['skipped_completed_papers'] == 0 and result['total_stages'] == 6
