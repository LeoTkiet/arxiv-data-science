"""Selective recovery tests: real adapters/QA, explicit offline network fixtures."""
import hashlib
import io
import json
from pathlib import Path
import tarfile
from unittest.mock import Mock

import pytest

from arxiv_pipeline.common.utils import atomic_json
from arxiv_pipeline.crawling.downloader import ArxivDownloader
from arxiv_pipeline.crawling.s2.s2_source import Outcome
from tests.crawling.test_downloader import MockResponse
from helpers import fixture_client

PAPER = '2301.00001'
ASSIGNMENT = {'ranges': [{'range': f'{PAPER}:{PAPER}', 'count': 1}]}


def files(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


def snapshot(tmp_path, monkeypatch, *, pdf=False):
    from arxiv_pipeline.crawling import downloader, references
    from arxiv_pipeline.pipeline.crawl import run
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as tar:
        body = b'\\documentclass{article}\\begin{document}Text.\\end{document}'
        member = tarfile.TarInfo('main.tex'); member.size = len(body)
        tar.addfile(member, io.BytesIO(body))
    payload = b'%PDF-1.4 synthetic' if pdf else buf.getvalue()
    session = Mock()
    session.get.side_effect = lambda *a, **kw: MockResponse([payload], headers={'Content-Length':str(len(payload))})
    dl = ArxivDownloader(session=session, max_retries=0); dl._wait_rate_limit = lambda: None
    monkeypatch.setattr(downloader, 'get_downloader', lambda: dl)
    class Source:
        n_requests = 0
        def fetch(self, ids):
            for paper in ids:
                self.n_requests += 1
                yield Outcome(paper, 'ok_empty', s2_paper_id='offline-fixture')
    monkeypatch.setattr(references, '_source', lambda _: Source())
    root = tmp_path/'original'
    client, _ = fixture_client()
    assert run([PAPER], root, client=client, full=True)['complete']
    return root


def prepare(tmp_path, monkeypatch, **kwargs):
    from arxiv_pipeline.pipeline import ms1_recovery as recovery
    source = snapshot(tmp_path, monkeypatch, **kwargs)
    target = tmp_path/'recovered'
    recovery.prepare_snapshot(source, target)
    return recovery, source, target


def test_inventory_is_read_only_and_keeps_verified_data(tmp_path, monkeypatch):
    recovery, source, target = prepare(tmp_path, monkeypatch)
    before = files(target)
    plan = recovery.inventory(target, ASSIGNMENT)
    assert plan['summary']['repair'] == plan['summary']['review'] == 0
    assert files(target) == before
    assert files(source) == {k:v for k,v in before.items() if k != '.recovery-origin.json'}


@pytest.mark.parametrize('relative,stage,version', [
    (f'raw/{PAPER}/v2/source','download',2),
    (f'corpus/{PAPER}/v2/main.tex','extract',2),
    (f'references/{PAPER}.json','references',None),
    (f'metadata/{PAPER}.json','metadata',None),
])
def test_inventory_identifies_exact_scope(tmp_path, monkeypatch, relative, stage, version):
    recovery, _, target = prepare(tmp_path, monkeypatch)
    (target/relative).unlink()
    plan = recovery.inventory(target, ASSIGNMENT)
    assert any(a['stage']==stage and a['version']==version and a['decision']=='repair'
               for p in plan['papers'] for a in p['actions'])


def test_extract_only_preserves_raw_other_version_and_original(tmp_path, monkeypatch):
    recovery, source, target = prepare(tmp_path, monkeypatch)
    original = files(source)
    raw = (target/f'raw/{PAPER}/v2/source').read_bytes()
    version1 = files(target/f'corpus/{PAPER}/v1')
    (target/f'corpus/{PAPER}/v2/main.tex').unlink()
    plan = recovery.inventory(target, ASSIGNMENT)
    result = recovery.recover(plan, target, tmp_path/'evidence', apply=True)
    assert result['complete']
    assert {e['stage'] for e in result['events']} == {'extract','validate'}
    assert (target/f'raw/{PAPER}/v2/source').read_bytes()==raw
    assert files(target/f'corpus/{PAPER}/v1')==version1
    assert files(source)==original
    # Resume from a fresh plan does no work, including no network calls.
    resumed = recovery.recover(recovery.inventory(target, ASSIGNMENT), target, tmp_path/'evidence2', apply=True)
    assert resumed['complete'] and resumed['events']==[]


def test_hash_mismatch_and_network_gate(tmp_path, monkeypatch):
    recovery, _, target = prepare(tmp_path, monkeypatch)
    (target/f'raw/{PAPER}/v1/source').write_bytes(b'bad')
    plan = recovery.inventory(target, ASSIGNMENT)
    assert any(a['stage']=='download' and a['decision']=='repair' for a in plan['papers'][0]['actions'])
    result = recovery.recover(plan,target,tmp_path/'evidence',apply=True)
    assert not result['complete']
    assert not any(e['stage']=='download' for e in result['events'])
    assert result['blocked']


def test_default_recovery_is_dry_run(tmp_path, monkeypatch):
    recovery, _, target = prepare(tmp_path, monkeypatch)
    (target/f'corpus/{PAPER}/v2/main.tex').unlink()
    before = files(target)
    result = recovery.recover(recovery.inventory(target,ASSIGNMENT),target,tmp_path/'evidence')
    assert result['dry_run'] and files(target)==before
    assert not (tmp_path/'evidence').exists()


def test_pdf_only_and_empty_references_are_preserved(tmp_path, monkeypatch):
    recovery, _, target = prepare(tmp_path,monkeypatch,pdf=True)
    assert recovery.inventory(target,ASSIGNMENT)['summary']['repair']==0
    assert recovery.inventory(target,ASSIGNMENT)['summary']['review']==0


def test_404_remains_review_not_automatic_retry(tmp_path, monkeypatch):
    recovery, _, target = prepare(tmp_path,monkeypatch)
    cp = target/f'state/stages/download/{PAPER}/2.json'
    data=json.loads(cp.read_text());data.update(status='api_error',error={'kind':'not_found','http_status':404,'retryable':False})
    atomic_json(cp,data)
    plan = recovery.inventory(target,ASSIGNMENT)
    assert any(a['stage']=='download' and a['version']==2 and a['decision']=='review' for a in plan['papers'][0]['actions'])
    assert not recovery.recover(plan,target,tmp_path/'evidence',apply=True,allow_network=True)['complete']


def test_stale_plan_and_original_are_rejected(tmp_path, monkeypatch):
    recovery, source, target = prepare(tmp_path,monkeypatch)
    plan = recovery.inventory(target,ASSIGNMENT)
    (target/f'corpus/{PAPER}/v1/main.tex').write_text('changed')
    with pytest.raises(ValueError,match='changed'):
        recovery.recover(plan,target,tmp_path/'evidence',apply=True)
    with pytest.raises(ValueError):
        recovery.recover(recovery.inventory(source,ASSIGNMENT),source,tmp_path/'evidence',apply=True)


def test_interrupted_stage_can_resume_without_repeating_success(tmp_path, monkeypatch):
    recovery, source, target = prepare(tmp_path,monkeypatch)
    (target/f'corpus/{PAPER}/v1/main.tex').unlink()
    (target/f'corpus/{PAPER}/v2/main.tex').unlink()
    from arxiv_pipeline.crawling import extractor
    original = extractor.run
    def interrupted(record,root,**kw):
        if kw['version']['version']==2:raise KeyboardInterrupt()
        return original(record,root,**kw)
    with pytest.raises(KeyboardInterrupt):
        recovery.recover(recovery.inventory(target,ASSIGNMENT),target,tmp_path/'evidence',apply=True,
                         adapters={'extract':interrupted})
    saved = files(target/f'corpus/{PAPER}/v1')
    result = recovery.recover(recovery.inventory(target,ASSIGNMENT),target,tmp_path/'evidence-resume',apply=True)
    assert result['complete'] and files(target/f'corpus/{PAPER}/v1')==saved
    assert [e['version'] for e in result['events'] if e['stage']=='extract']==[2]


def test_changed_corpus_bytes_are_repaired_without_downloading(tmp_path, monkeypatch):
    recovery, _, target = prepare(tmp_path,monkeypatch)
    tex=target/f'corpus/{PAPER}/v2/main.tex'
    expected=tex.read_bytes();tex.write_text('Tampered but still nonempty text')
    plan=recovery.inventory(target,ASSIGNMENT)
    assert any(a['stage']=='extract' and a['version']==2 and a['decision']=='repair' for a in plan['papers'][0]['actions'])
    result=recovery.recover(plan,target,tmp_path/'evidence',apply=True)
    assert result['complete'] and tex.read_bytes()==expected
    assert not any(e['stage']=='download' for e in result['events'])


def test_reference_only_repair_never_calls_download_or_extract(tmp_path, monkeypatch):
    recovery, source, target = prepare(tmp_path,monkeypatch)
    before_raw=files(target/'raw');before_corpus=files(target/'corpus')
    (target/f'references/{PAPER}.json').unlink()
    result=recovery.recover(recovery.inventory(target,ASSIGNMENT),target,tmp_path/'evidence',apply=True,allow_network=True)
    assert result['complete']
    assert {e['stage'] for e in result['events']}=={'references','validate'}
    assert files(target/'raw')==before_raw and files(target/'corpus')==before_corpus


def test_legacy_corpus_without_hash_manifest_is_verified_read_only(tmp_path, monkeypatch):
    recovery, _, target = prepare(tmp_path,monkeypatch)
    meta=target/f'corpus/{PAPER}/v2/extract_metadata.json'
    data=json.loads(meta.read_text());data.pop('corpus_sha256',None);data.pop('source_sha256',None);atomic_json(meta,data)
    before=files(target)
    plan=recovery.inventory(target,ASSIGNMENT)
    assert plan['summary']['repair']==plan['summary']['review']==0
    assert files(target)==before


def test_new_extraction_records_source_and_corpus_hashes(tmp_path, monkeypatch):
    recovery, _, target = prepare(tmp_path,monkeypatch)
    meta=json.loads((target/f'corpus/{PAPER}/v1/extract_metadata.json').read_text())
    assert meta['source_sha256']==files(target)[f'raw/{PAPER}/v1/source']
    assert meta['corpus_sha256']=={'main.tex':files(target)[f'corpus/{PAPER}/v1/main.tex']}


def test_raw_repair_targets_only_damaged_version(tmp_path, monkeypatch):
    recovery, source, target = prepare(tmp_path,monkeypatch)
    original=files(source);version2=files(target/f'raw/{PAPER}/v2')
    (target/f'raw/{PAPER}/v1/source').write_bytes(b'bad')
    result=recovery.recover(recovery.inventory(target,ASSIGNMENT),target,tmp_path/'evidence',
                            apply=True,allow_network=True)
    assert result['complete']
    assert [e['version'] for e in result['events'] if e['stage']=='download']==[1]
    assert files(target/f'raw/{PAPER}/v2')==version2 and files(source)==original


def test_missing_discovery_can_be_recovered_with_explicit_client(tmp_path, monkeypatch):
    recovery, source, target = prepare(tmp_path,monkeypatch)
    for path in (target/'state/discovery').glob('*.json'):path.unlink()
    (target/'manifests/discovery.json').unlink()
    client,_=fixture_client()
    result=recovery.recover(recovery.inventory(target,ASSIGNMENT),target,tmp_path/'evidence',
                            apply=True,allow_network=True,client=client)
    assert result['complete']
    assert any(e['stage']=='discovery' for e in result['events'])
    assert not any(e['stage']=='download' for e in result['events'])


def test_reference_api_error_never_becomes_valid_empty(tmp_path, monkeypatch):
    from arxiv_pipeline.crawling import references
    recovery, _, target=prepare(tmp_path,monkeypatch)
    (target/f'references/{PAPER}.json').unlink()
    class FailedSource:
        n_requests=0
        def fetch(self,ids):
            yield Outcome(ids[0],'error',error='HTTP 503')
    monkeypatch.setattr(references,'_source',lambda _:FailedSource())
    result=recovery.recover(recovery.inventory(target,ASSIGNMENT),target,tmp_path/'evidence',
                            apply=True,allow_network=True)
    assert not result['complete']
    assert not (target/f'references/{PAPER}.json').exists()
    assert any(e['stage']=='references' and e['status']=='api_error' for e in result['events'])


def test_invalid_success_checkpoint_is_repairable_not_crash(tmp_path, monkeypatch):
    recovery, _, target=prepare(tmp_path,monkeypatch)
    cp=target/f'state/stages/download/{PAPER}/1.json'
    data=json.loads(cp.read_text());data.pop('status');atomic_json(cp,data)
    plan=recovery.inventory(target,ASSIGNMENT)
    assert any(a['stage']=='download' and a['decision']=='repair' for a in plan['papers'][0]['actions'])


def test_no_source_checkpoint_cannot_hide_tex_in_raw(tmp_path, monkeypatch):
    recovery, _, target=prepare(tmp_path,monkeypatch)
    cp=target/f'state/stages/extract/{PAPER}/1.json'
    data=json.loads(cp.read_text());data.update(status='no_source',artifacts={});atomic_json(cp,data)
    plan=recovery.inventory(target,ASSIGNMENT)
    assert any(a['stage']=='extract' and a['version']==1 and a['decision']=='repair' for a in plan['papers'][0]['actions'])


def test_missing_validation_artifact_is_rebuilt_offline(tmp_path, monkeypatch):
    recovery, _, target=prepare(tmp_path,monkeypatch)
    (target/f'validation/{PAPER}.json').unlink()
    result=recovery.recover(recovery.inventory(target,ASSIGNMENT),target,tmp_path/'evidence',apply=True)
    assert result['complete']
    assert [e['stage'] for e in result['events']]==['validate']


def test_empty_references_require_producer_provenance(tmp_path, monkeypatch):
    recovery, _, target=prepare(tmp_path,monkeypatch)
    (target/f'references/{PAPER}.provenance.json').unlink()
    plan=recovery.inventory(target,ASSIGNMENT)
    assert any(a['stage']=='references' and a['decision']=='repair' for a in plan['papers'][0]['actions'])


def test_nonexistent_assigned_paper_is_accounted_but_not_complete(tmp_path, monkeypatch):
    from helpers import EMPTY
    recovery, _, target=prepare(tmp_path,monkeypatch)
    for path in (target/'state/discovery').glob('*.json'):path.unlink()
    (target/'manifests/discovery.json').unlink()
    client,_=fixture_client({PAPER:EMPTY})
    result=recovery.recover(recovery.inventory(target,ASSIGNMENT),target,tmp_path/'evidence',
                            apply=True,allow_network=True,client=client)
    assert not result['complete']
    assert result['excluded']==[PAPER] and result['assigned_count']==1


def test_bad_artifact_path_is_not_followed(tmp_path, monkeypatch):
    recovery, _, target=prepare(tmp_path,monkeypatch)
    cp=target/f'state/stages/download/{PAPER}/1.json'
    data=json.loads(cp.read_text());data['artifacts']['source']='../outside';atomic_json(cp,data)
    plan=recovery.inventory(target,ASSIGNMENT)
    assert any(a['stage']=='download' and a['decision']=='repair' for a in plan['papers'][0]['actions'])
