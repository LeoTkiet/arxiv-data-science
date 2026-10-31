"""PR10 regressions: canonical MS2 envelopes, invalid inputs and literal headings."""

import json
from pathlib import Path

import pytest

from arxiv_pipeline.common.schemas import StageResult
from arxiv_pipeline.parsing.hierarchy import build_hierarchy_tree, run
from parsing.ms2_contract_examples import examples


def case(output: Path, *, empty=False):
    output.mkdir(parents=True, exist_ok=True)
    payload = examples()['empty_gathered' if empty else 'gathered']
    payload.update(arxiv_id='2310.12345', version=1)
    path = output / 'custom/gathered.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding='utf-8')
    upstream = dict(
        context=dict(input_root=str(output.parent / 'ms1'), input_snapshot_id=payload['input_snapshot_id']),
        stages=dict(resolve=StageResult('resolve', '2310.12345', 1,
                    'no_source' if empty else 'success', '2026-10-31T00:00:00+00:00',
                    {'gathered': 'custom/gathered.json'}, schema_version='2.0').to_dict()),
    )
    return payload, path, upstream


def invoke(output, upstream):
    return run({'arxiv_id': '2310.12345'}, output, version={'version': 1}, upstream=upstream)


def tree(output, result):
    return json.loads((output / result.artifacts['raw_tree']).read_text(encoding='utf-8'))


def test_canonical_envelope_uses_explicit_nondefault_path(tmp_path):
    _, _, upstream = case(tmp_path)
    result = invoke(tmp_path, upstream)
    assert result.status == 'success'
    assert len(tree(tmp_path, result)['nodes']) == 3


def test_canonical_no_source_is_propagated(tmp_path):
    _, _, upstream = case(tmp_path, empty=True)
    result = invoke(tmp_path, upstream)
    assert result.status == 'no_source'
    assert tree(tmp_path, result)['nodes'] == []
    assert all('source_path' in w for w in tree(tmp_path, result)['warnings'])


@pytest.mark.parametrize('problem', ['missing', 'malformed', 'array', 'wrong_paper',
                         'wrong_version', 'wrong_schema', 'wrong_type', 'missing_body',
                         'false_empty', 'snapshot_mismatch'])
def test_invalid_artifact_never_succeeds(tmp_path, problem):
    payload, path, upstream = case(tmp_path)
    if problem == 'missing':
        path.unlink()
    elif problem == 'malformed':
        path.write_text('{broken JSON', encoding='utf-8')
    elif problem == 'array':
        path.write_text('[]', encoding='utf-8')
    else:
        changes = {'wrong_paper': ('arxiv_id', '2310.99999'), 'wrong_version': ('version', 2),
                   'wrong_schema': ('schema_version', '1.0'), 'wrong_type': ('merged_body_tex', []),
                   'false_empty': ('is_empty_source', True),
                   'snapshot_mismatch': ('input_snapshot_id', 'different-snapshot')}
        if problem == 'missing_body':
            payload.pop('merged_body_tex')
        else:
            name, value = changes[problem]
            payload[name] = value
        path.write_text(json.dumps(payload), encoding='utf-8')
    result = invoke(tmp_path, upstream)
    assert result.status == 'data_error'
    assert result.error is not None
    assert result.artifacts == {}
    assert not (tmp_path / 'parsed/2310.12345/v1/raw_tree.json').exists()


@pytest.mark.parametrize('status', ['data_error', 'error', 'skipped', 'not_implemented'])
def test_failed_upstream_does_not_parse_stale_default_file(tmp_path, status):
    payload, _, upstream = case(tmp_path)
    stale = tmp_path / 'parsed/2310.12345/v1/gathered.json'
    stale.parent.mkdir(parents=True)
    stale.write_text(json.dumps(payload), encoding='utf-8')
    upstream['stages']['resolve']['status'] = status
    result = invoke(tmp_path, upstream)
    assert result.status == 'skipped'
    assert result.error is not None
    assert status in result.error.message
    assert result.artifacts == {}


def test_missing_artifact_key_does_not_fall_back_to_default(tmp_path):
    payload, _, upstream = case(tmp_path)
    stale = tmp_path / 'parsed/2310.12345/v1/gathered.json'
    stale.parent.mkdir(parents=True)
    stale.write_text(json.dumps(payload), encoding='utf-8')
    upstream['stages']['resolve']['artifacts'] = {}
    assert invoke(tmp_path, upstream).status == 'data_error'


@pytest.mark.parametrize('path', ['../outside.json', '/absolute.json', r'C:\outside.json'])
def test_artifact_paths_cannot_escape_output(tmp_path, path):
    _, _, upstream = case(tmp_path)
    upstream['stages']['resolve']['artifacts']['gathered'] = path
    assert invoke(tmp_path, upstream).status == 'data_error'


@pytest.mark.parametrize('field,value', [('stage', 'normalize'), ('paper_id', '2310.99999'),
                                      ('version', 2), ('schema_version', '1.0')])
def test_mismatched_upstream_identity_is_rejected(tmp_path, field, value):
    _, _, upstream = case(tmp_path)
    upstream['stages']['resolve'][field] = value
    assert invoke(tmp_path, upstream).status == 'data_error'


@pytest.mark.parametrize('literal', [r'\verb|\section{Fake}|',
                         '\\begin{verbatim}\n\\section{Fake}\n\\end{verbatim}',
                         '\\begin{lstlisting}\n\\section{Fake}\n\\end{lstlisting}',
                         '\\begin{minted}{tex}\n\\section{Fake}\n\\end{minted}'])
def test_heading_in_literal_does_not_change_tree_parent(literal):
    nodes = build_hierarchy_tree(1, r'\section{Examples}' + '\n' + literal + '\nActual prose.')
    headings = [n for n in nodes if n.kind == 'heading']
    assert len(headings) == 1
    actual = next(n for n in nodes if 'Actual prose.' in n.raw_content)
    assert actual.parent_temp_id == headings[0].temp_id
    assert any('Fake' in n.raw_content and n.kind == 'prose_block' for n in nodes)


def test_heading_inside_figure_caption_keeps_figure_atomic():
    tex = r'\section{Visuals}\begin{figure}\caption{Example \section{Fake}}\end{figure}After.'
    nodes = build_hierarchy_tree(1, tex)
    assert len([n for n in nodes if n.kind == 'heading']) == 1
    assert len([n for n in nodes if n.kind == 'figure']) == 1


def test_literal_begin_tag_inside_verbatim_does_not_swallow_real_heading():
    tex = ('\\section{Intro}\n\\begin{verbatim}\n\\begin{verbatim}\n'
           '\\end{verbatim}\n\\section{Results}\nActual result.')
    nodes = build_hierarchy_tree(1, tex)
    headings = [n for n in nodes if n.kind == 'heading']
    assert len(headings) == 2
    actual = next(n for n in nodes if 'Actual result.' in n.raw_content)
    assert actual.parent_temp_id == headings[1].temp_id


def test_inline_verb_does_not_create_fake_figure():
    tex = r'\section{Intro}Example \verb|\begin{figure}| actual prose.'
    nodes = build_hierarchy_tree(1, tex)
    assert not any(n.kind == 'figure' for n in nodes)
    assert any('actual prose.' in n.raw_content for n in nodes)
