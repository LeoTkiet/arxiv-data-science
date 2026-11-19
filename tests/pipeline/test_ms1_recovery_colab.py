"""Colab recovery exercises real file copies, adapters, finalization and ZIPs."""
import json
from pathlib import Path
import zipfile

import pytest

from arxiv_pipeline.common.utils import atomic_json
from tests.pipeline.test_ms1_recovery import ASSIGNMENT, PAPER, files, snapshot


def session_fixture(tmp_path, monkeypatch):
    from arxiv_pipeline.pipeline.ms1_recovery_colab import RecoverySession
    original = snapshot(tmp_path, monkeypatch)
    session = RecoverySession('TV1', original, tmp_path/'drive-recovery',
                              tmp_path/'colab', ASSIGNMENT)
    session.inspect()
    return session, original


def test_inventory_default_does_not_publish_or_mutate_original(tmp_path, monkeypatch):
    session, original = session_fixture(tmp_path, monkeypatch)
    before = files(original)
    result = session.repair()
    assert result['dry_run']
    assert files(original) == before
    assert not session.persistent.exists()


def test_repair_and_finalize_rebuild_handoff_with_evidence_only_zip(tmp_path, monkeypatch):
    session, original = session_fixture(tmp_path, monkeypatch)
    before = files(original)
    (session.data/f'corpus/{PAPER}/v2/main.tex').unlink()
    session.inspect()
    result = session.repair('local')
    assert result['complete']
    assert {event['stage'] for event in result['events']} == {'extract', 'validate'}
    raw = session.persistent/f'data/raw/{PAPER}/v1/source'
    raw_stamp = raw.stat().st_mtime_ns
    receipt = session.finish()
    assert receipt['handoff_ready'] and receipt['assigned_input_count'] == 1
    output = session.persistent/'data'
    assert (output/f'raw/{PAPER}/v1/source').is_file()
    assert (output/f'corpus/{PAPER}/v2/main.tex').is_file()
    assert json.loads((output/'summary.json').read_text())['complete']
    assert json.loads((output/'manifests/stages.json').read_text())
    with zipfile.ZipFile(receipt['evidence_zip']) as archive:
        names = archive.namelist()
        assert 'assignment-audit.json' in names and 'snapshot.json' in names
        assert not any(name.startswith(('raw/', 'corpus/', 'data/')) for name in names)
    assert files(original) == before
    assert raw.stat().st_mtime_ns == raw_stamp, 'Do not upload verified raw a second time at finalization'


def test_runtime_reset_restores_repaired_data_without_network(tmp_path, monkeypatch):
    from arxiv_pipeline.pipeline.ms1_recovery_colab import RecoverySession
    session, original = session_fixture(tmp_path, monkeypatch)
    (session.data/f'corpus/{PAPER}/v2/main.tex').unlink()
    session.inspect(); session.repair('local')
    restored = RecoverySession('TV1', original, session.persistent,
                               tmp_path/'colab-new-runtime', ASSIGNMENT)
    plan = restored.inspect()
    assert plan['summary']['repair'] == plan['summary']['review'] == 0
    result = restored.repair('local')
    assert result['complete'] and result['events'] == []


def test_pending_errors_never_receive_ready_receipt(tmp_path, monkeypatch):
    session, original = session_fixture(tmp_path, monkeypatch)
    (session.data/f'raw/{PAPER}/v1/source').write_bytes(b'bad')
    session.inspect(); session.repair('local')
    receipt = session.finish()
    assert not receipt['handoff_ready']
    assert not json.loads((session.data/'summary.json').read_text())['complete']
    assert receipt['unresolved_paper_ids'] == [PAPER]


def test_output_inside_original_is_rejected_before_writes(tmp_path, monkeypatch):
    from arxiv_pipeline.pipeline.ms1_recovery_colab import RecoverySession
    original = snapshot(tmp_path, monkeypatch)
    before = files(original)
    with pytest.raises(ValueError):
        RecoverySession('TV1', original, original/'recovery', tmp_path/'local', ASSIGNMENT)
    assert files(original) == before


def test_existing_output_from_different_input_cannot_be_reused(tmp_path, monkeypatch):
    from arxiv_pipeline.pipeline.ms1_recovery_colab import RecoverySession
    session, original = session_fixture(tmp_path, monkeypatch)
    session.repair('local')
    with pytest.raises(ValueError):
        RecoverySession('TV2', original, session.persistent, tmp_path/'other', ASSIGNMENT).inspect()


def test_interrupted_recovery_keeps_committed_stage_in_drive_copy(tmp_path, monkeypatch):
    from arxiv_pipeline.crawling import extractor
    from arxiv_pipeline.pipeline.ms1_recovery_colab import RecoverySession
    session, original = session_fixture(tmp_path, monkeypatch)
    for v in (1, 2): (session.data/f'corpus/{PAPER}/v{v}/main.tex').unlink()
    session.inspect()
    run = extractor.run
    def interrupt(record, output, **kwargs):
        if kwargs['version']['version'] == 2:
            raise KeyboardInterrupt()
        return run(record, output, **kwargs)
    monkeypatch.setattr(extractor, 'run', interrupt)
    with pytest.raises(KeyboardInterrupt): session.repair('local')
    monkeypatch.setattr(extractor, 'run', run)
    resumed = RecoverySession('TV1', original, session.persistent, tmp_path/'new', ASSIGNMENT)
    resumed.inspect()
    result = resumed.repair('local')
    assert result['complete']
    assert [e['version'] for e in result['events'] if e['stage'] == 'extract'] == [2]


def test_staging_interruption_resumes_verified_files_and_reports_progress(tmp_path, monkeypatch):
    from arxiv_pipeline.pipeline.ms1_recovery_colab import stage_snapshot
    original = snapshot(tmp_path, monkeypatch)
    target = tmp_path/'staging'
    events=[]
    def progress(event):
        events.append(event)
        if event['kind'] == 'copy' and event['done'] >= 1:
            raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt): stage_snapshot(original, target, progress=progress)
    stage_snapshot(original, target, progress=events.append)
    assert files(original) == {k:v for k,v in files(target).items() if k != '.recovery-origin.json'}
    assert any(e.get('reused', 0) > 0 for e in events)


def test_stale_plan_is_rejected_before_publishing_data(tmp_path, monkeypatch):
    session, original=session_fixture(tmp_path,monkeypatch)
    (session.data/f'raw/{PAPER}/v1/source').write_bytes(b'changed after review')
    with pytest.raises(ValueError, match='changed'):
        session.repair('network')
    assert not session.persistent.exists()


def test_conflicting_discovery_that_ms2_rejects_cannot_be_handed_off(tmp_path, monkeypatch):
    session, original=session_fixture(tmp_path,monkeypatch)
    session.repair('local')
    path=next((session.data/'state/discovery').glob('*.json'))
    older=json.loads(path.read_text());older['timestamp']='2000-01-01T00:00:00+00:00'
    older['metadata']['latest_version']=99
    atomic_json(session.data/'state/discovery/old-conflict.json',older)
    receipt=session.finish()
    assert not receipt['handoff_ready']
    assert receipt['diagnostics']


@pytest.mark.parametrize('restart', ['same', 'legacy', 'reset'])
def test_finalization_recovers_completed_run_after_new_session(tmp_path, monkeypatch, restart):
    from arxiv_pipeline.pipeline.ms1_recovery_colab import RecoverySession
    session, original=session_fixture(tmp_path,monkeypatch)
    (session.data/f'corpus/{PAPER}/v2/main.tex').unlink()
    session.inspect();session.repair('local')
    raw=session.persistent/f'data/raw/{PAPER}/v1/source'
    before=raw.stat().st_mtime_ns
    if restart=='legacy':
        (session.local/'published-files.json').unlink(missing_ok=True)
    local=tmp_path/'reset' if restart=='reset' else session.local
    reopened=RecoverySession('TV1',original,session.persistent,local,ASSIGNMENT)
    reopened.inspect()
    receipt=reopened.finish()
    assert receipt.get('mode')!='inventory_only'
    assert receipt['handoff_ready']
    assert raw.stat().st_mtime_ns==before
    with zipfile.ZipFile(receipt['evidence_zip']) as archive:
        assert 'assignment-audit.json' in archive.namelist()
        history=json.loads(archive.read('source-runs.json'))
        assert history[0]['run']['status']=='finished'
        assert any(e['stage']=='extract' for e in history[0]['run']['events'])
