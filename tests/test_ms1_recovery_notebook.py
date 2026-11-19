"""Execute distributed notebook cells, including bundled source, offline."""
import ast
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
from unittest.mock import patch
import zipfile

import pytest

from tests.pipeline.test_ms1_recovery import ASSIGNMENT, PAPER, snapshot

ROOT = Path(__file__).resolve().parents[1]


def notebook(tmp_path):
    builder = ROOT/'scripts/build_ms1_recovery_notebook.py'
    assert builder.is_file(), 'Notebook builder must package the current local fixes'
    target = tmp_path/'recovery.ipynb'
    runpy.run_path(str(builder))['build'](ROOT, target)
    generated = json.loads(target.read_text(encoding='utf-8'))
    distributed = json.loads((ROOT/'colab/MS1_Selective_Recovery_CPU.ipynb').read_text(encoding='utf-8'))
    assert distributed['cells'] == generated['cells'], 'Rebuild the distributed notebook after changing source/cells'
    return distributed


def compiled(cell, overrides=None):
    tree = ast.parse(''.join(cell['source']))
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            name=node.targets[0].id
            if name in (overrides or {}):
                node.value=ast.parse(repr(overrides[name]), mode='eval').body
    return compile(ast.fix_missing_locations(tree), '<recovery-notebook>', 'exec')


def test_notebook_default_is_read_only_and_runtime_contains_only_public_code(tmp_path, monkeypatch):
    n = notebook(tmp_path)
    codes = [c for c in n['cells'] if c['cell_type']=='code']
    for c in codes:
        assert c['outputs']==[] and c['execution_count'] is None
        ast.parse(''.join(c['source']))
    assert len(codes)==3
    # Decode the actual embedded runtime and check the archive boundary.
    assignments = {node.targets[0].id:ast.literal_eval(node.value)
                   for node in ast.parse(''.join(codes[0]['source'])).body
                   if isinstance(node,ast.Assign) and isinstance(node.targets[0],ast.Name)
                   and node.targets[0].id in {'RUNTIME_B64','RUNTIME_SHA256'}}
    payload=base64.b64decode(assignments['RUNTIME_B64'])
    assert hashlib.sha256(payload).hexdigest()==assignments['RUNTIME_SHA256']
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names=archive.namelist()
        assert 'src/arxiv_pipeline/pipeline/ms1_recovery.py' in names
        assert 'src/arxiv_pipeline/pipeline/ms1_recovery_colab.py' in names
        assert not any(name.startswith(('.git/','data/','artifacts/')) or '/.env' in name for name in names)
    from arxiv_pipeline.pipeline.ms1_recovery_colab import RecoverySession
    source=snapshot(tmp_path,monkeypatch)
    session=RecoverySession('TV1',source,tmp_path/'out',tmp_path/'local',ASSIGNMENT)
    session.inspect()
    ns={'session':session}
    exec(compiled(codes[1]),ns)
    assert ns['recovery_result']['dry_run']
    assert not session.persistent.exists()


def test_notebook_repair_and_handoff_cells_use_real_session(tmp_path, monkeypatch):
    n=notebook(tmp_path)
    codes=[c for c in n['cells'] if c['cell_type']=='code']
    from arxiv_pipeline.pipeline.ms1_recovery_colab import RecoverySession
    source=snapshot(tmp_path,monkeypatch)
    session=RecoverySession('TV1',source,tmp_path/'out',tmp_path/'local',ASSIGNMENT)
    session.inspect()
    (session.data/f'corpus/{PAPER}/v2/main.tex').unlink()
    session.inspect()
    ns={'session':session}
    exec(compiled(codes[1],{'RUN_MODE':'Phục hồi local'}),ns)
    exec(compiled(codes[2]),ns)
    assert ns['handoff']['handoff_ready']
    assert Path(ns['handoff']['evidence_zip']).is_file()


def test_all_three_cells_boot_from_embedded_code_in_clean_process(tmp_path, monkeypatch):
    source=snapshot(tmp_path,monkeypatch)
    n=notebook(tmp_path)
    overrides=dict(MEMBER='TV1', SOURCE_DATA=str(source), RECOVERY_FOLDER=str(tmp_path/'output'),
                   LOCAL_BASE=str(tmp_path/'local'), RUNTIME_BASE=str(tmp_path/'runtime'),
                   assignment=dict(ASSIGNMENT,assigned_input_count=1))
    script='import json\n'
    script+='cells=json.loads('+repr(json.dumps(n))+")[\"cells\"]\n"
    script+='import ast\noverrides='+repr(overrides)+'\n'
    script+='''
for cell in cells:
    if cell['cell_type']!='code': continue
    tree=ast.parse(''.join(cell['source']))
    for node in tree.body:
        if isinstance(node,ast.Assign) and isinstance(node.targets[0],ast.Name):
            name=node.targets[0].id
            if name in overrides: node.value=ast.parse(repr(overrides[name]),mode='eval').body
    exec(compile(ast.fix_missing_locations(tree),'<notebook>','exec'))
assert session.plan['summary']['repair']==0
assert recovery_result['dry_run']
assert not (session.persistent/'data').exists()
assert Path(handoff['evidence_zip']).is_file()
import arxiv_pipeline
assert Path(arxiv_pipeline.__file__).resolve().is_relative_to(runtime.resolve())
'''
    result=subprocess.run([os.sys.executable,'-'],input=script,encoding='utf-8',capture_output=True,
                          env=dict(os.environ,PYTHONIOENCODING='utf-8'),timeout=60)
    assert result.returncode==0, result.stdout+'\n'+result.stderr
