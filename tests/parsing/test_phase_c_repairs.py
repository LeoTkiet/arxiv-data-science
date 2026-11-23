"""Regressions reduced from the authenticated recovered TV5 pilot."""
import json
import io
import tarfile

import pytest

from arxiv_pipeline.parsing.bibliography import run
from tests.parsing.test_bibliography import write_gathered, envelope, PAPER, KEY


def bibliography_case(tmp_path, text, body=r'Text \cite{used}.', preamble=''):
    relative=f'corpus/{KEY}/v1/refs.bib'
    path=tmp_path/'in'/relative
    path.parent.mkdir(parents=True)
    path.write_text(text,encoding='utf-8')
    resolved=write_gathered(tmp_path,merged_body_tex=body,preamble_tex=preamble,
                            bib_file_paths=[relative],referenced_bib_file_paths=[relative])
    result=run({'arxiv_id':PAPER},tmp_path/'out',version={'version':1},upstream=envelope(tmp_path,resolved))
    payload=json.loads((tmp_path/'out'/result.artifacts['raw_bibtex']).read_text()) if result.artifacts else None
    return result,payload


def test_only_cited_entries_are_parsed_not_undefined_unused_library_rows(tmp_path):
    result,payload=bibliography_case(tmp_path,
        '@article{unused,title={Unused},journal=undefined_journal}\n'
        '@article{used,title={Selected title},year={2024}}')
    assert result.status=='success'
    assert [e['citation_key'] for e in payload['entries']]==['used']


def test_unused_string_alias_does_not_fail_an_unrelated_citation(tmp_path):
    result,payload=bibliography_case(tmp_path,
        '@string{unused=undefined_symbol}\n'
        '@string{usedjournal={Known Journal}}\n'
        '@article{used,title={Selected},journal=usedjournal}')
    assert result.status=='success'
    assert payload['entries'][0]['journal']=='Known Journal'


def test_undefined_symbol_in_cited_entry_is_still_a_real_error(tmp_path):
    result,_=bibliography_case(tmp_path,'@article{used,title={Selected},journal=undefined_symbol}')
    assert result.status=='data_error'


def test_nocite_wildcard_keeps_all_entries(tmp_path):
    result,payload=bibliography_case(tmp_path,
        '@article{used,title={Selected}}\n@book{other,title={Other}}',body=r'\nocite{*}')
    assert result.status=='success'
    assert {e['citation_key'] for e in payload['entries']}=={'used','other'}


def test_citation_selection_ignores_verbatim_and_unused_macro_definitions(tmp_path):
    result,payload=bibliography_case(tmp_path,
        '@article{used,title={Selected}}\n@article{unused,title={Unused}}',
        body=r'\verb|\cite{unused}| Text \citep[see][p. 2]{used}.',
        preamble=r'\newcommand{\unusedmacro}{\cite{unused}}')
    assert result.status=='success'
    assert [e['citation_key'] for e in payload['entries']]==['used']


def test_selected_entry_scanner_preserves_nested_braces_parentheses_and_at_sign(tmp_path):
    result,payload=bibliography_case(tmp_path,
        '@article{discard,title={Literal @article{not_an_entry} and {nested}}}\n'
        '@article(used,title="Selected {nested} title @ home",url={https://x.test/a(b)})')
    assert result.status=='success'
    assert len(payload['entries'])==1
    assert payload['entries'][0]['title']=='Selected nested title @ home'


def test_bib_parser_does_not_parse_uncited_bulk_entries(tmp_path, monkeypatch):
    # The public parser boundary must receive only selected entries + definitions.
    import bibtexparser
    actual=bibtexparser.loads
    def bounded(text,*args,**kwargs):
        assert len(text)<2000, 'Do not send the entire unrelated library to pyparsing'
        return actual(text,*args,**kwargs)
    monkeypatch.setattr(bibtexparser,'loads',bounded)
    library=''.join(f'@article{{unused{i},title={{Unused item {i}}}}}\n' for i in range(2000))
    result,payload=bibliography_case(tmp_path,library+'@article{used,title={Selected}}')
    assert result.status=='success' and len(payload['entries'])==1


@pytest.mark.parametrize('preamble,body',[
    (r'\newcommand{\mycite}[1]{\cite{#1}}',r'\cite{used}\mycite{other}'),
    (r'\newcommand{\inner}{\cite{other}}\newcommand{\outer}{\inner}',r'\cite{used}\outer'),
    ('',r'\cite{used}\Citep{other}'),
])
def test_dynamic_or_unrecognized_citation_keeps_conservative_inventory(tmp_path,preamble,body):
    result,payload=bibliography_case(tmp_path,
        '@article{used,title={Selected}}\n@article{other,title={Other}}',body=body,preamble=preamble)
    assert result.status=='success'
    assert {e['citation_key'] for e in payload['entries']}=={'used','other'}


def test_titlespacing_heading_reference_is_not_an_executed_heading(tmp_path):
    from arxiv_pipeline.parsing.resolver import _Resolver
    (tmp_path/'main.tex').write_text(
        r'\documentclass{article}\titlespacing{\paragraph}{0pt}{.01\baselineskip}{1em}'
        r'\begin{document}\paragraph{Result} Actual body.\end{document}')
    payload={}
    _Resolver(tmp_path,tmp_path,'2410.03577').gather(payload)
    assert r'\paragraph{Result}' in payload['merged_body_tex']


def test_titlespacing_does_not_hide_compilation_commands(tmp_path):
    from arxiv_pipeline.parsing.resolver import _Resolver,ResolveError
    (tmp_path/'main.tex').write_text(
        r'\documentclass{article}\titlespacing{\paragraph}{\input{hidden}}{0pt}{1em}'
        r'\begin{document}Body.\end{document}')
    with pytest.raises(ResolveError):_Resolver(tmp_path,tmp_path,'2410.03577').gather({})


def test_root_selection_prefers_viable_document_over_instruction_examples(tmp_path):
    from arxiv_pipeline.parsing.resolver import _Resolver
    (tmp_path/'aaa-instructions.tex').write_text(
        r'\documentclass{article}\begin{document}Example \documentclass{book}\begin{document}\end{document}')
    (tmp_path/'paper-actual.tex').write_text(r'\documentclass{article}\begin{document}Real paper.\end{document}')
    payload={};_Resolver(tmp_path,tmp_path,'2410.03259').gather(payload)
    assert payload['main_tex_path']=='paper-actual.tex'


def test_titlespacing_accepts_unbraced_command_reference(tmp_path):
    from arxiv_pipeline.parsing.resolver import _Resolver
    (tmp_path/'main.tex').write_text(
        r'\documentclass{article}\titlespacing\section{0pt}{8pt}{0pt}'
        r'\begin{document}\section{Result}Body.\end{document}')
    payload={};_Resolver(tmp_path,tmp_path,'2410.03577').gather(payload)
    assert r'\section{Result}' in payload['merged_body_tex']


def test_titlespacing_optional_argument_does_not_hide_compilation(tmp_path):
    from arxiv_pipeline.parsing.resolver import _Resolver,ResolveError
    (tmp_path/'main.tex').write_text(
        r'\documentclass{article}\titlespacing{\paragraph}{0pt}{0pt}{1em}[\input{hidden}]'
        r'\begin{document}Body.\end{document}')
    with pytest.raises(ResolveError):_Resolver(tmp_path,tmp_path,'2410.03577').gather({})


@pytest.mark.parametrize('body,expected',[(r'\cite{child}',{'child'}),(r'\nocite{*}',{'child','parent'})])
def test_crossref_inheritance_across_files_does_not_duplicate_parent(tmp_path,body,expected):
    relatives=[f'corpus/{KEY}/v1/{name}.bib' for name in ('children','parents')]
    for relative,text in zip(relatives,[
        '@inproceedings{child,title={Child},crossref={parent}}',
        '@proceedings{parent,title={Parent},year={2024}}']):
        path=tmp_path/'in'/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text)
    resolved=write_gathered(tmp_path,merged_body_tex=body,bib_file_paths=relatives,referenced_bib_file_paths=relatives)
    result=run({'arxiv_id':PAPER},tmp_path/'out',version={'version':1},upstream=envelope(tmp_path,resolved))
    assert result.status=='success'
    rows=json.loads((tmp_path/'out'/result.artifacts['raw_bibtex']).read_text())['entries']
    assert len(rows)==len(expected)
    assert {r['citation_key'] for r in rows}==expected
    assert next(r for r in rows if r['citation_key']=='child')['year']==2024


def archive_resolver(tmp_path, body, members, preamble=''):
    from arxiv_pipeline.parsing import resolver
    root=tmp_path/'in';corpus=root/f'corpus/{KEY}/v1';corpus.mkdir(parents=True)
    main=rf'\documentclass{{article}}{preamble}\begin{{document}}{body}\end{{document}}'
    (corpus/'main.tex').write_text(main,encoding='utf-8')
    for name,data in members.items():
        if name.endswith(('.tex','.bib')):
            target=corpus/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
    source=root/f'raw/{KEY}/v1/source';source.parent.mkdir(parents=True)
    with tarfile.open(source,'w:gz') as archive:
        for name,data in dict({'main.tex':main.encode()},**members).items():
            info=tarfile.TarInfo(name);info.size=len(data);archive.addfile(info,io.BytesIO(data))
    before={p:p.read_bytes() for p in root.rglob('*') if p.is_file()}
    version=dict(version=1,versioned_id=f'{PAPER}v1',source_path=source.relative_to(root).as_posix(),
                 corpus_path=corpus.relative_to(root).as_posix())
    stages={name:dict(stage=name,paper_id=PAPER,version=1,schema_version='1.0',status='success',
            artifacts={artifact:version[path]}) for name,artifact,path in [
                ('download','source','source_path'),('extract','corpus','corpus_path')]}
    result=resolver.run({'arxiv_id':PAPER},tmp_path/'out',version=version,
        upstream=dict(context=dict(input_root=str(root),input_snapshot_id='test'),stages=stages))
    assert {p:p.read_bytes() for p in root.rglob('*') if p.is_file()}==before
    payload=json.loads((tmp_path/'out'/result.artifacts['gathered']).read_text(encoding='utf-8')) if result.artifacts else None
    return result,payload


def test_resolver_recovers_extensionless_tex_dependency_from_same_raw_archive(tmp_path):
    result,payload=archive_resolver(tmp_path,r'Before.\input{table_obs}After.',
        {'table_obs':b'\\begin{table}Actual table.\\input{sub/rows}\\end{table}',
         'sub/rows':b'Exact row text.'})
    assert result.status=='success',result.error
    assert 'Actual table.Exact row text.' in payload['merged_body_tex']
    span=next(s for s in payload['source_spans'] if s.get('archive_member')=='sub/rows')
    assert span['source_path']==f'raw/{KEY}/v1/source'
    assert {s['member'] for s in payload['archive_sources']}=={'table_obs','sub/rows'}


def test_binary_extensionless_archive_dependency_is_rejected(tmp_path):
    result,_=archive_resolver(tmp_path,r'\input{table_obs}',{'table_obs':b'\x00\x01binary'})
    assert result.status=='data_error'


def test_bbl_in_same_archive_resolves_external_bibliography_without_reading_outside(tmp_path):
    result,payload=archive_resolver(tmp_path,r'Body \cite{a}.\bibliography{../../external}',
        {'main.bbl':br'\begin{thebibliography}{1}\bibitem{a}A. Author. \newblock Actual title.\end{thebibliography}'})
    assert result.status=='success',result.error
    assert len(payload['raw_bibitems'])==1
    assert 'Actual title' in payload['raw_bibitems'][0]
    assert all('bibliography' not in s for s in [payload['merged_body_tex']])
    assert any(s['member']=='main.bbl' for s in payload['archive_sources'])


def test_stale_bbl_missing_a_cited_key_does_not_override_unsafe_path(tmp_path):
    result,_=archive_resolver(tmp_path,r'Body \cite{new}.\bibliography{../../external}',
        {'main.bbl':br'\begin{thebibliography}{1}\bibitem{old}Old.\end{thebibliography}'})
    assert result.status=='data_error'


def test_raw_archive_fallback_never_uses_another_jobname_bibliography(tmp_path):
    result,_=archive_resolver(tmp_path,r'Body \cite{a}.\bibliography{../../external}',
        {'other.bbl':br'\begin{thebibliography}{1}\bibitem{a}A.\end{thebibliography}'})
    assert result.status=='data_error'


def test_uncited_unclosed_entry_does_not_swallow_later_cited_entries(tmp_path):
    result,payload=bibliography_case(tmp_path,
        '@article{bad,title={Uncited}\n\n@article{used,title={Real selected title},year={2024}}')
    assert result.status=='success',result.error
    assert [r['title'] for r in payload['entries']]==['Real selected title']
    assert any(w['code']=='bib_malformed_uncited_entry' for w in payload['warnings'])


def test_cited_unclosed_entry_is_not_silently_repaired(tmp_path):
    result,_=bibliography_case(tmp_path,
        '@article{used,title={Selected}\n\n@article{other,title={Other}}')
    assert result.status=='data_error'


def test_unclosed_nested_field_cannot_be_resynchronized_as_an_entry(tmp_path):
    result,_=bibliography_case(tmp_path,
        '@article{bad,title={Unclosed field\n@article{used,title={Not a real top level entry}}')
    assert result.status=='data_error'


def test_same_jobname_bbl_supplements_exact_keys_missing_from_bib_library(tmp_path):
    resolved,payload=archive_resolver(tmp_path,r'Body \cite{used,missing}.\bibliography{refs}',
        {'refs.bib':b'@article{used,title={Library title}}',
         'main.bbl':br'\begin{thebibliography}{1}\bibitem{missing}A. Author.\newblock Supplemental title.\end{thebibliography}'})
    assert resolved.status=='success'
    result=run({'arxiv_id':PAPER},tmp_path/'out',version={'version':1},upstream={
        'context':{'input_root':str(tmp_path/'in'),'input_snapshot_id':'test'},
        'stages':{'resolve':resolved.to_dict()}})
    assert result.status=='success'
    rows=json.loads((tmp_path/'out'/result.artifacts['raw_bibtex']).read_text())['entries']
    assert {r['citation_key']:r['title'] for r in rows}=={'used':'Library title','missing':'Supplemental title'}
    assert (tmp_path/'out'/f'parsed/{KEY}/v1/raw_bibtex.json').is_file()


def test_malformed_crossref_parent_is_not_treated_as_an_ignorable_uncited_entry(tmp_path):
    result,_=bibliography_case(tmp_path,
        '@proceedings{bad,title={Bad parent}\n'
        '@inproceedings{used,title={Child},crossref={bad}}')
    assert result.status=='data_error'


def test_declared_false_conditional_skips_malformed_unused_definition(tmp_path):
    result,payload=archive_resolver(tmp_path,'Real body.',{},preamble=(
        r'\newif\ifinstructions\instructionsfalse'
        r'\ifinstructions\newcommand{\broken}\else\newcommand{\valid}{ok}\fi'))
    assert result.status=='success',result.error
    assert r'\newcommand{\valid}{ok}' in payload['preamble_tex']
    assert 'broken' not in payload['preamble_tex']


def test_unknown_conditional_with_only_unused_macro_definitions_is_omitted(tmp_path):
    result,payload=archive_resolver(tmp_path,'Real body.',{},preamble=(
        r'\ifpackageflag\newcommand{\unused}{Final}\else\newcommand{\unused}{Draft}\fi'))
    assert result.status=='success',result.error
    assert 'unused' not in payload['preamble_tex']
    assert any(w['code']=='static_conditional' for w in payload['warnings'])


def test_unknown_conditional_defining_a_used_macro_still_requires_review(tmp_path):
    result,_=archive_resolver(tmp_path,r'\used Real body.',{},preamble=(
        r'\ifpackageflag\newcommand{\used}{Final}\else\newcommand{\used}{Draft}\fi'))
    assert result.status=='data_error'


def test_boolean_changes_in_called_macro_are_not_assumed_straight_line(tmp_path):
    result,_=archive_resolver(tmp_path,r'\change\ifchoice True.\else False.\fi',{},preamble=(
        r'\newif\ifchoice\newcommand{\change}{\choicetrue}'))
    assert result.status=='data_error'


def test_grouped_boolean_assignment_is_not_allowed_to_leak_outside_scope(tmp_path):
    result,_=archive_resolver(tmp_path,r'{\choicetrue}\ifchoice True.\else False.\fi',{},
        preamble=r'\newif\ifchoice')
    assert result.status=='data_error'


def test_nested_static_conditionals_keep_only_the_active_body(tmp_path):
    result,payload=archive_resolver(tmp_path,r'\iftrue A.\iffalse Wrong.\else B.\fi\else Wrong.\fi',{})
    assert result.status=='success',result.error
    assert payload['merged_body_tex']==' A. B.'


def test_inactive_include_is_not_read_or_reported_missing(tmp_path):
    result,payload=archive_resolver(tmp_path,r'\iffalse\input{missing}\fi Real body.',{})
    assert result.status=='success',result.error
    assert payload['merged_body_tex']==' Real body.'
    assert payload['included_files']==[]


@pytest.mark.parametrize('redefinition',[r'\let\ifchoice\iftrue',r'\def\choicetrue{\relax}'])
def test_redefined_boolean_commands_are_not_evaluated_as_original_switches(tmp_path,redefinition):
    result,_=archive_resolver(tmp_path,r'\choicetrue\ifchoice True.\else False.\fi',{},
        preamble=r'\newif\ifchoice'+redefinition)
    assert result.status=='data_error'


def test_empty_include_stays_an_explicit_source_error(tmp_path):
    result,_=archive_resolver(tmp_path,'Body.',{},preamble=r'\input{}')
    assert result.status=='data_error'
    assert result.error.kind=='empty_include'


@pytest.mark.parametrize('members',[
    {'table_obs':b'Table.', '../escape':b'Outside.'},
    {'table_obs':b'Table.', './table_obs':b'Duplicate.'},
    {'table_obs':br'\input{sub/rows}', 'sub/rows':b'First.', 'sub/./rows':b'Duplicate.'},
])
def test_raw_dependency_lookup_rejects_escaping_or_duplicate_archive_members(tmp_path,members):
    result,_=archive_resolver(tmp_path,r'\input{table_obs}',members)
    assert result.status=='data_error'
    assert result.error.kind in {'unsafe_path','ambiguous_archive'}


def test_bbl_latin1_names_are_preserved_instead_of_replacement_characters(tmp_path):
    result,payload=archive_resolver(tmp_path,r'\cite{a}\bibliography{../../external}',
        {'main.bbl':b'\\begin{thebibliography}{1}\\bibitem{a}Caf\xe9.\\end{thebibliography}'})
    assert result.status=='success'
    assert 'Caf\u00e9' in payload['raw_bibitems'][0]
