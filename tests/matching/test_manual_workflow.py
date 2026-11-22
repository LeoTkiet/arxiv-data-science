"""Offline tests for IDs, workbook integrity and T3-04 acceptance gates."""
from dataclasses import asdict
import json
import zipfile
import xml.etree.ElementTree as ET

import pytest

from arxiv_pipeline.matching.annotation_workbook import (
    NS, export_annotation_workbook, load_annotation_workbook,
)
from arxiv_pipeline.matching.contracts import ContractError, LabelRecord, publication_from_rows
from arxiv_pipeline.matching.manual_workflow import (
    audit_manual_labels, collect_workbooks, load_prepared, main, prepare_forms,
)


def bundle(pid='2411.00001', count=20):
    bib = [dict(arxiv_id=pid, citation_key=f'key-{i}', entry_type='article',
        title=f'Synthetic title {i}', authors=['Nguyễn An'], year=2020,
        doi=None, journal=None, url=None) for i in range(count)]
    refs = [dict(arxiv_id=pid, ref_arxiv_id='2001.00001', ref_title='Synthetic reference',
        ref_authors=['Nguyễn An'], ref_submission_date='2020-01-01', ref_s2_id=None)]
    return publication_from_rows(pid, bib, refs, readiness='ready')


def change_workbook(source, output, edit, shared_strings=False):
    with zipfile.ZipFile(source) as archive:
        files = {n: archive.read(n) for n in archive.namelist()}
    sheet = ET.fromstring(files['xl/worksheets/sheet1.xml'])
    edit(sheet)
    if shared_strings:
        shared = ET.Element(f'{{{NS}}}sst')
        for cell in sheet.iter(f'{{{NS}}}c'):
            text = ''.join(t.text or '' for t in cell.findall(f'{{{NS}}}is//{{{NS}}}t'))
            for child in list(cell):
                cell.remove(child)
            cell.set('t', 's')
            ET.SubElement(cell, f'{{{NS}}}v').text = str(len(shared))
            item = ET.SubElement(shared, f'{{{NS}}}si')
            ET.SubElement(item, f'{{{NS}}}t').text = text
        files['xl/sharedStrings.xml'] = ET.tostring(shared)
    files['xl/worksheets/sheet1.xml'] = ET.tostring(sheet)
    with zipfile.ZipFile(output, 'w') as archive:
        for name, raw in files.items():
            archive.writestr(name, raw)


def put(sheet, address, text):
    cell = next(c for c in sheet.iter(f'{{{NS}}}c') if c.get('r') == address)
    for child in list(cell):
        cell.remove(child)
    cell.set('t', 'inlineStr')
    inline = ET.SubElement(cell, f'{{{NS}}}is')
    ET.SubElement(inline, f'{{{NS}}}t').text = text


def fill(sheet, member='tv1', accepted=False):
    reviewer = 'tv2' if member != 'tv2' else 'tv3'
    for row in list(sheet.find(f'{{{NS}}}sheetData'))[1:]:
        n = row.get('r')
        put(sheet, f'I{n}', 'unmatched')
        put(sheet, f'K{n}', member)
        put(sheet, f'M{n}', 'accepted' if accepted else 'pending')
        if accepted:
            put(sheet, f'L{n}', reviewer)


@pytest.fixture
def cohort():
    assignments = {f'tv{i}': [f'2411.{(i-1)*5+j:05d}' for j in range(1, 6)] for i in range(1, 6)}
    bundles = [bundle(pid) for ids in assignments.values() for pid in ids]
    return assignments, bundles


@pytest.fixture
def prepared(tmp_path, cohort):
    assignments, bundles = cohort
    source = tmp_path / 'inputs'
    for b in bundles:
        member = next(m for m, ids in assignments.items() if b.arxiv_id in ids)
        folder = source / member / b.arxiv_id
        folder.mkdir(parents=True)
        snapshot = f'synthetic-{b.arxiv_id}'
        values = {
            'bibtex.json': [asdict(row) for row in b.bibtex],
            'references.json': [row.to_dict() for row in b.references],
            'ms1_validation.json': dict(schema_version='1.0', paper_id=b.arxiv_id, valid=True,
                errors=[], counts={'references':1}, outcomes=[{'stage':'references','status':'success'}]),
            'ms2_validation.json': dict(schema_version='2.0', arxiv_id=b.arxiv_id, valid=True,
                errors=[], input_snapshot_id=snapshot, counts={'bibtex_entries':20}),
            'input_manifest.json': dict(schema_version='2.0', arxiv_id=b.arxiv_id, complete=True,
                input_snapshot_id=snapshot, expected_versions=[1], versions=[{'version':1,'status':'success'}]),
        }
        for name, data in values.items():
            (folder / name).write_text(json.dumps(data), encoding='utf-8')
    output = tmp_path / 'prepared'
    prepare_forms(assignments, source, output)
    return output, source


def accepted_labels(cohort):
    assignments, bundles = cohort
    owners = {pid: m for m, ids in assignments.items() for pid in ids}
    return [LabelRecord(b.arxiv_id, q.citation_key, None, 'unmatched', 'manual',
        annotator=owners[b.arxiv_id], reviewer='tv2' if owners[b.arxiv_id]!='tv2' else 'tv3',
        review_status='accepted') for b in bundles for q in b.bibtex]


def test_export_keeps_ids_as_text_two_sheets_and_refuses_overwrite(tmp_path):
    path = tmp_path / 'form.xlsx'
    assert export_annotation_workbook([bundle()], path) == 20
    with zipfile.ZipFile(path) as archive:
        book = ET.fromstring(archive.read('xl/workbook.xml'))
        assert [s.get('name') for s in book.find(f'{{{NS}}}sheets')] == ['Gan_nhan','Candidates']
        sheet = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
        a2 = next(c for c in sheet.iter(f'{{{NS}}}c') if c.get('r') == 'A2')
        assert a2.get('t') == 'inlineStr'
        assert a2.find(f'{{{NS}}}is/{{{NS}}}t').text == '2411.00001'
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        export_annotation_workbook([bundle()], path)
    assert path.read_bytes() == before


@pytest.mark.parametrize('shared', [False, True])
def test_import_inline_and_shared_strings_preserves_pending(tmp_path, shared):
    b = bundle(); source = tmp_path/'original.xlsx'; target = tmp_path/'edited.xlsx'
    export_annotation_workbook([b], source)
    def edit(sheet):
        put(sheet,'I2','matched'); put(sheet,'J2','2001.00001'); put(sheet,'K2','tv1')
        put(sheet,'I3','uncertain'); put(sheet,'K3','tv1'); put(sheet,'N3','Need another reader')
    change_workbook(source, target, edit, shared)
    labels = load_annotation_workbook(target, [b], expected_annotator='tv1')
    assert len(labels) == 2 and labels[0].groundtruth_arxiv_id == '2001.00001'
    assert all(l.review_status == 'pending' and not l.is_supervised for l in labels)


@pytest.mark.parametrize('problem', ['numeric_id','formula','duplicate_blank','deleted','source_edit','outside_candidate','wrong_annotator'])
def test_rejects_corrupt_workbooks_even_unlabeled_rows(tmp_path, problem):
    source=tmp_path/'source.xlsx'; target=tmp_path/'bad.xlsx'; b=bundle()
    export_annotation_workbook([b],source)
    def edit(sheet):
        if problem=='numeric_id':
            cell=next(c for c in sheet.iter(f'{{{NS}}}c') if c.get('r')=='A2')
            cell.clear();cell.set('r','A2');ET.SubElement(cell,f'{{{NS}}}v').text='241100001'
        elif problem=='formula':
            cell=next(c for c in sheet.iter(f'{{{NS}}}c') if c.get('r')=='N2')
            ET.SubElement(cell,f'{{{NS}}}f').text='1+1'
        elif problem=='duplicate_blank':put(sheet,'B3','key-0')
        elif problem=='deleted':
            data=sheet.find(f'{{{NS}}}sheetData');data.remove(list(data)[1])
        elif problem=='source_edit':put(sheet,'C2','Different title')
        else:
            put(sheet,'I2','matched');put(sheet,'J2','2001.00002' if problem=='outside_candidate' else '2001.00001')
            put(sheet,'K2','tv5' if problem=='wrong_annotator' else 'tv1')
    change_workbook(source,target,edit)
    with pytest.raises(ContractError):load_annotation_workbook(target,[b],expected_annotator='tv1')


def test_prepare_rejects_incomplete_evidence_without_creating_output(prepared, tmp_path, cohort):
    _, source=prepared;assignments,_=cohort
    pm=source/'tv1/2411.00001/input_manifest.json';value=json.loads(pm.read_text());value['complete']=False;pm.write_text(json.dumps(value))
    with pytest.raises(ContractError, match='Incomplete/invalid'):
        prepare_forms(assignments,source,tmp_path/'bad')
    assert not (tmp_path/'bad').exists()


def test_prepared_hash_prevents_changed_inputs(prepared):
    root,_=prepared
    manifest=json.loads((root/'prepared_manifest.json').read_text())
    path=root/manifest['publications'][0]['bundle'];path.write_text('{}')
    with pytest.raises(ContractError,match='hash mismatch'):load_prepared(root)


def test_audit_accepts_500_independently_reviewed_queries(cohort):
    report=audit_manual_labels(*cohort,accepted_labels(cohort))
    assert report['ready_for_handoff'] and report['accepted_entries']==500
    assert report['qualifying_publications']==25


@pytest.mark.parametrize('problem',['pending','self_review','missing_member','duplicate','wrong_target','nineteen_twentyone'])
def test_audit_rejects_inflated_quotas(cohort,problem):
    assignments,bundles=cohort;labels=accepted_labels(cohort)
    if problem=='missing_member':labels=[l for l in labels if l.annotator!='tv5']
    elif problem=='duplicate':labels.append(labels[0])
    elif problem=='nineteen_twentyone':
        # Still 500 entries globally: 19+21+20+20+20 for TV1 is NOT five qualifying papers.
        first,second=bundles[:2]; bundles=list(bundles)
        bundles[0]=bundle(first.arxiv_id,19);bundles[1]=bundle(second.arxiv_id,21)
        labels=accepted_labels((assignments,bundles))
    else:
        value=labels[0].to_dict()
        if problem=='pending':value.update(review_status='pending',reviewer=None)
        elif problem=='self_review':value['reviewer']=value['annotator']
        elif problem=='wrong_target':value.update(decision='matched',groundtruth_arxiv_id='2001.00002')
        labels[0]=LabelRecord(**value)
    report=audit_manual_labels(assignments,bundles,labels)
    assert not report['ready_for_handoff']
    if problem=='nineteen_twentyone':
        assert report['accepted_entries']==500 and report['members']['tv1']['qualifying_publications']==4


def test_collect_draft_partial_roster_reports_not_ready_and_no_overwrite(prepared,tmp_path):
    root,_=prepared;edited=tmp_path/'tv3.xlsx'
    change_workbook(root/'annotation_tv3.xlsx',edited,lambda s:fill(s,'tv3'))
    report=collect_workbooks(root,{'tv3':edited},tmp_path/'collected',origin='ai_assisted')
    assert not report['ready_for_handoff'] and report['accepted_entries']==0
    labels=json.loads((tmp_path/'collected/labels.json').read_text())
    assert len(labels)==100 and all(l['review_status']=='pending' for l in labels)
    with pytest.raises(FileExistsError):collect_workbooks(root,{'tv3':edited},tmp_path/'collected')


def test_ai_draft_cannot_be_imported_as_accepted(prepared,tmp_path):
    root,_=prepared;edited=tmp_path/'tv3.xlsx'
    change_workbook(root/'annotation_tv3.xlsx',edited,lambda s:fill(s,'tv3',True))
    with pytest.raises(ContractError,match='AI drafts'):collect_workbooks(root,{'tv3':edited},tmp_path/'bad',origin='ai_assisted')
    assert not (tmp_path/'bad').exists()


def test_cli_full_collection_and_audit_can_be_reproduced(prepared,tmp_path):
    root,_=prepared;workbooks=[]
    for i in range(1,6):
        member=f'tv{i}';output=tmp_path/f'{member}.xlsx'
        change_workbook(root/f'annotation_{member}.xlsx',output,lambda s,m=member:fill(s,m,True))
        workbooks += ['--workbook',f'{member}={output}']
    assert main(['collect','--prepared',str(root),*workbooks,'--output',str(tmp_path/'out')])==0
    assert main(['audit','--prepared',str(root),'--labels',str(tmp_path/'out/labels.json'),'--output',str(tmp_path/'audit')])==0
    report=json.loads((tmp_path/'audit/manual_audit.json').read_text())
    assert report['ready_for_handoff'] and report['accepted_entries']==500
