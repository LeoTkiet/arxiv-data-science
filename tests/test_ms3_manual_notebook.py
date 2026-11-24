"""Execute the distributed notebook's actual cells in isolated offline Python."""
import ast
import base64
import hashlib
import io
import json
from pathlib import Path
import runpy
import subprocess
import sys
import zipfile

import pytest

from tests.matching.test_manual_workflow import cohort, prepared, change_workbook, fill

ROOT=Path(__file__).resolve().parents[1]


def notebook(tmp_path):
    build=runpy.run_path(str(ROOT/'scripts/build_ms3_manual_notebook.py'))['build']
    path=tmp_path/'rebuilt.ipynb';build(ROOT,path)
    generated=json.loads(path.read_text(encoding='utf-8'))
    actual=json.loads((ROOT/'colab/MS3_Manual_Labeling_CPU.ipynb').read_text(encoding='utf-8'))
    assert generated==actual, 'Rebuild the distributed notebook after changing bundled code'
    return actual


def test_runtime_is_reproducible_source_only_and_cells_have_no_results(tmp_path):
    book=notebook(tmp_path)
    cells=[c for c in book['cells'] if c['cell_type']=='code']
    assert len(cells)==4
    for cell in cells:
        assert cell['execution_count'] is None and cell['outputs']==[]
        ast.parse(''.join(cell['source']))
    setup=ast.parse(''.join(cells[0]['source']))
    constants={n.targets[0].id:ast.literal_eval(n.value) for n in setup.body
        if isinstance(n,ast.Assign) and isinstance(n.targets[0],ast.Name) and isinstance(n.value,ast.Constant)}
    raw=base64.b64decode(constants['RUNTIME_B64'])
    assert hashlib.sha256(raw).hexdigest()==constants['RUNTIME_SHA256']
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names=archive.namelist()
        assert all(info.create_system == 3 and info.external_attr >> 16 == 0o100644
                   for info in archive.infolist())
        assert all(n.startswith('src/') or n in ['configs/ms3_manual_assignments.json','runtime_manifest.json'] for n in names)
        assert not any(n.endswith(('.xlsx','.csv','.env','.ipynb')) or n.startswith('artifacts/') for n in names)
        manifest=json.loads(archive.read('runtime_manifest.json'))
        assert all(hashlib.sha256(archive.read(n)).hexdigest()==digest for n,digest in manifest['files'].items())


@pytest.mark.parametrize('accepted',[True,False])
def test_actual_notebook_cells_end_to_end_without_network(tmp_path,prepared,accepted):
    book=notebook(tmp_path);snapshot,_=prepared
    assignments=json.loads((snapshot/'prepared_manifest.json').read_text())['assignments']
    config=tmp_path/'assignment.json';config.write_text(json.dumps({'schema_version':'3.0','task':'T3-04','assignments':assignments}))
    folder=tmp_path/'returned';folder.mkdir()
    members=list(assignments) if accepted else ['tv3']
    for member in members:
        change_workbook(snapshot/f'annotation_{member}.xlsx',folder/f'annotation_{member}_reviewed.xlsx',lambda s,m=member:fill(s,m,accepted))
    overrides=dict(MOUNT_DRIVE=False,RUNTIME_PARENT=str(tmp_path/'runtime'),
        INPUT_MODE='prepared_folder',INPUT_SOURCE=str(snapshot),WORKBOOK_FOLDER=str(folder),
        MEMBERS=','.join(members),MODE='reviewed' if accepted else 'progress',
        ORIGIN='human' if accepted else 'ai_assisted',OUTPUT_ROOT=str(tmp_path/'published'),
        ASSIGNMENTS_FILE=str(config),LOCAL_ROOT=str(tmp_path/'runs'),RUN_ID='notebook-test')
    # A separate interpreter models a fresh Colab runtime, without pytest imports.
    payload=tmp_path/'execution.json';payload.write_text(json.dumps({'book':book,'overrides':overrides}),encoding='utf-8')
    script=tmp_path/'execute.py'
    script.write_text('''import ast,json,sys
p=json.load(open(sys.argv[1],encoding='utf-8'));ns={}
for cell in p['book']['cells']:
 if cell['cell_type']!='code':continue
 tree=ast.parse(''.join(cell['source']))
 for node in tree.body:
  if isinstance(node,ast.Assign) and isinstance(node.targets[0],ast.Name) and node.targets[0].id in p['overrides']:
   node.value=ast.parse(repr(p['overrides'][node.targets[0].id]),mode='eval').body
 exec(compile(ast.fix_missing_locations(tree),'<distributed-notebook>','exec'),ns)
''',encoding='utf-8')
    result=subprocess.run([sys.executable,'-X','utf8',str(script),str(payload)],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=90)
    assert result.returncode==0,result.stdout+'\n'+result.stderr
    output=tmp_path/'published/notebook-test'
    receipt=json.loads((output/'published_manifest.json').read_text())
    assert receipt['handoff_ready'] is accepted
    assert (output/('handoff.zip' if accepted else 'progress.zip')).is_file()
