"""Regressions for observed MS1/MS2 failures; all network responses are offline."""
import hashlib
from pathlib import Path
from unittest.mock import Mock
import pytest
from arxiv_pipeline.common.utils import atomic_json
from arxiv_pipeline.crawling.downloader import ArxivDownloader
from tests.crawling.test_downloader import MockResponse


def test_downloader_rejects_truncated_content_length(tmp_path):
    body = b'\\documentclass{article}'
    session = Mock()
    session.get.return_value = MockResponse([body], headers={'Content-Length': str(len(body)+20)})
    dl = ArxivDownloader(session=session, max_retries=0)
    dl._wait_rate_limit = lambda: None
    result = dl.download_version('2410.03028', 1, tmp_path/'source', tmp_path/'meta.json')
    assert result['status'] != 'success'
    assert not (tmp_path/'source').exists()


def test_downloader_does_not_trust_tampered_cached_source(tmp_path):
    body = b'\\documentclass{article}'
    source = tmp_path/'source'; source.write_bytes(b'broken')
    atomic_json(tmp_path/'meta.json', dict(status='success', arxiv_id='2410.03028', version=1,
        sha256=hashlib.sha256(body).hexdigest(), raw_size_bytes=len(body)))
    session = Mock(); session.get.return_value = MockResponse([body])
    dl = ArxivDownloader(session=session, max_retries=0); dl._wait_rate_limit = lambda: None
    dl.download_version('2410.03028', 1, source, tmp_path/'meta.json')
    assert source.read_bytes() == body


def test_missing_source_is_not_pdf_only(tmp_path):
    from arxiv_pipeline.crawling.extractor import extract_source, ExtractionDataError
    with pytest.raises(ExtractionDataError): extract_source(tmp_path/'absent', tmp_path/'corpus')


def test_dotted_include_is_found(tmp_path):
    from arxiv_pipeline.parsing.resolver import _Resolver
    (tmp_path/'sec').mkdir(); expected=tmp_path/'sec/3.1_vqa.tex'; expected.write_text('text')
    assert _Resolver(tmp_path,tmp_path,'2410.03061').target('sec/3.1_vqa',tmp_path/'main.tex','.tex') == expected


def test_ambiguous_include_is_not_silently_selected(tmp_path):
    from arxiv_pipeline.parsing.resolver import _Resolver, ResolveError
    (tmp_path/'part').write_text('a'); (tmp_path/'part.tex').write_text('b')
    with pytest.raises(ResolveError, match='Ambiguous'):
        _Resolver(tmp_path,tmp_path,'2410.03061').target('part',tmp_path/'main.tex','.tex')


def test_percent_roundtrip_preserves_title():
    from arxiv_pipeline.parsing.bibliography import parse_bib_text
    from arxiv_pipeline.parsing.deduplication import render_bibtex
    row=dict(citation_key='x',entry_type='article',title='Quality 90%* full text',authors=['Author'],year=2023,doi=None,journal=None,url=None)
    assert parse_bib_text(render_bibtex([row]))[0][0]['title'] == row['title']


def test_shared_bibtex_strings_are_resolved():
    from arxiv_pipeline.parsing.bibliography import parse_bib_text
    rows,_=parse_bib_text('@article{x,title={Title},journal=localjournal}', strings={'localjournal':'Journal of Tests'})
    assert rows[0]['journal']=='Journal of Tests'


def test_s2_short_batch_never_silently_drops_ids(tmp_path):
    from arxiv_pipeline.crawling.s2.s2_source import S2Source
    s=S2Source(tmp_path/'last');s._batch_split=lambda ids: [None]
    outcomes=list(s.fetch(['2410.03028','2410.03029']))
    assert len(outcomes)==2 and all(o.status=='error' for o in outcomes)


@pytest.mark.parametrize('stage,oldstatus', [('extract','skipped'), ('validate','data_error')])
def test_ms1_does_not_cache_failed_stages_after_repair(tmp_path,monkeypatch,stage,oldstatus):
    from arxiv_pipeline.pipeline import crawl
    from arxiv_pipeline.common.schemas import StageResult
    from types import SimpleNamespace
    paper='2410.03028'; version={'version':1} if stage=='extract' else None
    atomic_json(tmp_path/f'state/stages/{stage}/{paper}/{1 if version else "paper"}.json',
        dict(stage=stage,paper_id=paper,version=1 if version else None,schema_version='1.0',status=oldstatus,artifacts={}))
    (tmp_path/'artifact.json').write_text('{}')
    run=Mock(return_value=StageResult(stage,paper,1 if version else None,'success','test',{'result':'artifact.json'}))
    monkeypatch.setattr(crawl.importlib,'import_module',lambda _:SimpleNamespace(run=run))
    result=crawl._run_stage(stage,{'arxiv_id':paper},tmp_path,version,{'status':'success'} if version else None,True)
    assert result['status']=='success' and run.called


def test_layout_only_conditional_does_not_block_real_paper(tmp_path):
    from arxiv_pipeline.parsing.resolver import _Resolver
    (tmp_path/'main.tex').write_text(r'\documentclass{article}\begin{document}Body\ifCLASSOPTIONcaptionsoff\newpage\fi\end{document}')
    payload={};_Resolver(tmp_path,tmp_path,'2410.03028').gather(payload)
    assert 'Body' in payload['merged_body_tex']
    assert 'ifCLASSOPTION' not in payload['merged_body_tex']


def test_bibliography_pipeline_uses_sibling_string_definitions(tmp_path):
    from tests.parsing.test_ms2_benchmark import make_parsed_output, PAPER
    from arxiv_pipeline.pipeline import parse
    out=make_parsed_output(tmp_path,count=1)
    corpus=tmp_path/f'ms1/corpus/{PAPER}/v1'
    (corpus/'main.tex').write_text(r'\documentclass{article}\begin{document}Text\bibliography{refs}\end{document}')
    (corpus/'refs.bib').write_text('@article{x,title={A reference},journal=testjournal}')
    (corpus/'strings.bib').write_text('@string{testjournal={Journal of Tests}}')
    assert parse.run([PAPER],out,input_root=tmp_path/'ms1',resume=True)['complete']


def test_bibitem_author_names_use_same_canonicalization_as_bib_file():
    from arxiv_pipeline.parsing.bibliography import _row,parse_bib_text
    from arxiv_pipeline.parsing.deduplication import render_bibtex
    fields=dict(citation_key='x',entry_type='article',title='Title',authors=['Sato, Y.'],year=2023,doi=None,journal=None,url=r'http://x.test/a{\_}b')
    row=_row('2410.03028',fields)
    parsed=parse_bib_text(render_bibtex([row]))[0][0]
    assert row['authors']==parsed['authors']
    assert row['url']==parsed['url']


def test_ms1_cache_rejects_source_hash_mismatch(tmp_path):
    from arxiv_pipeline.pipeline.crawl import _artifacts_exist
    (tmp_path/'source').write_bytes(b'changed')
    atomic_json(tmp_path/'meta.json',dict(sha256='0'*64,raw_size_bytes=7,status='success'))
    assert not _artifacts_exist(dict(stage='download',status='success',artifacts={'source':'source','download_metadata':'meta.json'}),tmp_path)


def test_ms1_validation_is_invalidated_by_newer_upstream(tmp_path,monkeypatch):
    from arxiv_pipeline.pipeline import crawl
    from arxiv_pipeline.common.schemas import StageResult
    from types import SimpleNamespace
    p='2410.03028'
    (tmp_path/'qa.json').write_text('{}')
    atomic_json(tmp_path/f'state/stages/validate/{p}/paper.json',dict(stage='validate',paper_id=p,version=None,schema_version='1.0',status='success',timestamp='2026-01-01T00:00:00+00:00',artifacts={'validation':'qa.json'}))
    atomic_json(tmp_path/f'state/stages/download/{p}/1.json',dict(timestamp='2026-02-01T00:00:00+00:00'))
    run=Mock(return_value=StageResult('validate',p,None,'success','2026-02-02T00:00:00+00:00',{'validation':'qa.json'}))
    monkeypatch.setattr(crawl.importlib,'import_module',lambda _:SimpleNamespace(run=run))
    crawl._run_stage('validate',{'arxiv_id':p},tmp_path,None,None,True)
    assert run.called
