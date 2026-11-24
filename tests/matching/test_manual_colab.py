"""Colab staging/publication tests without Google Drive or network access."""
import json
from pathlib import Path
import zipfile

import pytest

from arxiv_pipeline.matching.contracts import ContractError
from arxiv_pipeline.matching.manual_colab import RunConfig, extract_staged_zip, publish_run, run_audit
from tests.matching.test_manual_workflow import cohort, prepared, change_workbook, fill


def run_config(tmp_path, prepared, *, accepted=True, input_mode='prepared_folder'):
    snapshot, inputs = prepared
    assignment = json.loads((snapshot/'prepared_manifest.json').read_text())['assignments']
    config = tmp_path/'assignments.json'
    config.write_text(json.dumps({'schema_version':'3.0','task':'T3-04','assignments':assignment}))
    folder = tmp_path/'returned'; folder.mkdir()
    members = tuple(assignment) if accepted else ('tv3',)
    for member in members:
        change_workbook(snapshot/f'annotation_{member}.xlsx', folder/f'annotation_{member}_reviewed.xlsx',
            lambda sheet, m=member: fill(sheet,m,accepted))
    source = snapshot
    if input_mode=='staged_zip':
        source=tmp_path/'staged.zip'
        with zipfile.ZipFile(source,'w') as archive:
            for path in inputs.rglob('*.json'):
                archive.write(path,path.relative_to(inputs).as_posix())
    return RunConfig(input_source=source, assignments_file=config, workbook_folder=folder,
        output_root=tmp_path/'published', local_root=tmp_path/'runs', input_mode=input_mode,
        members=members, origin='human' if accepted else 'ai_assisted',
        mode='reviewed' if accepted else 'progress', run_id='test-run')


@pytest.mark.parametrize('input_mode',['prepared_folder','staged_zip'])
def test_publish_handoff_calls_real_workflow_and_preserves_sources(tmp_path,prepared,input_mode):
    config=run_config(tmp_path,prepared,input_mode=input_mode)
    source_bytes={p:p.read_bytes() for p in config.workbook_folder.iterdir()}
    result=run_audit(config,runtime_sha256='synthetic-runtime',code_base_commit='synthetic-base')
    output=publish_run(result,config.output_root)
    receipt=json.loads((output/'published_manifest.json').read_text())
    assert receipt['handoff_ready'] and receipt['archive']=='handoff.zip'
    assert json.loads((output/'manual_audit.json').read_text())['accepted_entries']==500
    with zipfile.ZipFile(output/'handoff.zip') as archive:
        assert 'prepared/prepared_manifest.json' in archive.namelist()
        assert 'collected/labels.json' in archive.namelist()
        assert 'workbooks/annotation_tv3_reviewed.xlsx' in archive.namelist()
    assert all(path.read_bytes()==raw for path,raw in source_bytes.items())
    with pytest.raises(FileExistsError):publish_run(result,config.output_root)
    with pytest.raises(FileExistsError):run_audit(config,runtime_sha256='synthetic-runtime',code_base_commit='synthetic-base')


def test_pending_ai_draft_is_progress_not_handoff(tmp_path,prepared):
    config=run_config(tmp_path,prepared,accepted=False)
    result=run_audit(config,runtime_sha256='synthetic',code_base_commit='synthetic')
    output=publish_run(result,config.output_root)
    report=json.loads((output/'manual_audit.json').read_text())
    assert report['accepted_entries']==0 and not report['ready_for_handoff']
    assert (output/'progress.zip').is_file() and not (output/'handoff.zip').exists()
    receipt=json.loads((output/'run_manifest.json').read_text())
    assert receipt['collect_exit_code']==receipt['audit_exit_code']==1


def test_tampered_audit_does_not_publish(tmp_path,prepared):
    config=run_config(tmp_path,prepared)
    result=run_audit(config,runtime_sha256='synthetic',code_base_commit='synthetic')
    report=result.root/'collected/manual_audit.json'
    value=json.loads(report.read_text());value['accepted_entries']=99999;report.write_text(json.dumps(value))
    with pytest.raises(ContractError,match='changed after audit'):publish_run(result,config.output_root)
    assert not config.output_root.exists()


def test_missing_workbook_does_not_create_run(tmp_path,prepared):
    config=run_config(tmp_path,prepared)
    (config.workbook_folder/'annotation_tv5_reviewed.xlsx').unlink()
    with pytest.raises(FileNotFoundError):run_audit(config,runtime_sha256='synthetic',code_base_commit='synthetic')
    assert not config.local_root.exists()


def test_staged_zip_rejects_extra_paths_before_extracting(tmp_path,cohort):
    assignments,_=cohort;source=tmp_path/'bad.zip';target=tmp_path/'staged'
    with zipfile.ZipFile(source,'w') as archive:archive.writestr('../outside.json','{}')
    with pytest.raises(ContractError,match='exactly'):extract_staged_zip(source,target,assignments)
    assert not target.exists() and not (tmp_path/'outside.json').exists()
