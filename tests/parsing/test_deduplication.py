"""Unit tests for TV4 cross-version deduplication (offline, synthetic data only)."""

import json
from copy import deepcopy

import pytest

from arxiv_pipeline.common.schemas import PARSING_SCHEMA_VERSION
from arxiv_pipeline.parsing.deduplication import (
    deduplicate_elements_and_build_hierarchy, normalize_title, rename_citations, render_bibtex, run,
    unionize_bibtex,
)
from arxiv_pipeline.parsing.bibliography import parse_bib_text

PAPER, KEY, SNAPSHOT = "2310.12345", "2310.12345", "snap-1"
PREFIX = "2310-12345#"


def entry(key, **fields):
    base = dict(arxiv_id=PAPER, citation_key=key, entry_type="misc", title="", authors=[], year=None,
                doi=None, journal=None, url=None)
    return dict(base, **fields)


# ------------------------------------------------------------------ level 1: the example of the statement
def statement_entries():
    return [
        (1, [entry("vaswani2017", title="Attention is all you need", authors=["A. Vaswani"], year=2017)]),
        (2, [entry("vit_transformer", title="Attention is all you need", authors=["A. Vaswani"], year=2017,
                   doi="10.5555/3295222.3295349")]),
        (3, [entry("vit_transformer", title="Attention Is All You Need", authors=["A. Vaswani"])]),   # year dropped
    ]


def test_statement_example_three_versions_one_entry():
    rows, mapping, conflicts = unionize_bibtex(statement_entries())
    assert mapping == {"vit_transformer": "vaswani2017"}
    assert len(rows) == 1
    row = rows[0]
    assert row["citation_key"] == "vaswani2017"                              # earliest key is chosen
    assert (row["title"], row["authors"], row["year"], row["doi"]) == (
        "Attention is all you need", ["A. Vaswani"], 2017, "10.5555/3295222.3295349")     # union of all fields
    assert conflicts == []


def test_same_doi_merges_even_with_different_titles_and_case():
    rows, mapping, _ = unionize_bibtex([(1, [entry("a", doi="10.1/ABC", title="Short")]),
                                        (1, [entry("b", doi="https://doi.org/10.1/abc", title="Other")])])
    assert [r["citation_key"] for r in rows] == ["a"] and mapping == {"b": "a"}


def test_duplicates_inside_one_version_are_merged():
    rows, mapping, _ = unionize_bibtex([(1, [entry("x1", title="A sufficiently long shared title"),
                                             entry("x2", title="A sufficiently long, shared title!")])])
    assert len(rows) == 1 and mapping == {"x2": "x1"}


def test_short_titles_are_not_proof_of_identity():
    rows, mapping, _ = unionize_bibtex([(1, [entry("a", title="Survey"), entry("b", title="Survey")])])
    assert len(rows) == 2 and mapping == {}


def test_transitive_merge_keeps_one_key():
    rows, mapping, _ = unionize_bibtex([
        (1, [entry("a", doi="10.1/x")]),
        (2, [entry("b", doi="10.1/x", title="A sufficiently long shared title")]),
        (3, [entry("c", title="A sufficiently long shared title")]),
    ])
    assert len(rows) == 1 and mapping == {"b": "a", "c": "a"} and rows[0]["title"] == "A sufficiently long shared title"


def test_conflicting_values_keep_the_earliest_and_are_reported():
    rows, _, conflicts = unionize_bibtex([(1, [entry("k", year=2017, title="Same title of a paper")]),
                                          (2, [entry("k", year=2018, title="Same title of a paper")])])
    assert rows[0]["year"] == 2017
    assert conflicts == [dict(citation_key="k", field="year", kept=2017, dropped=2018, source_key="k")]


def test_bibitem_default_type_gives_way_to_a_real_type():
    rows, _, conflicts = unionize_bibtex([(1, [entry("k", doi="10.1/x", entry_type="misc")]),
                                          (2, [entry("k2", doi="10.1/x", entry_type="article")])])
    assert rows[0]["entry_type"] == "article" and conflicts == []


def test_unrelated_entries_stay_separate_and_ordered():
    rows, mapping, _ = unionize_bibtex([(1, [entry("b", title="Second work in the list"), entry("a", title="First work in the list")])])
    assert [r["citation_key"] for r in rows] == ["b", "a"] and mapping == {}


def test_normalize_title():
    assert normalize_title(r"The {\em Deep}  Learning: A Survey!") == "the deep learning a survey"


# ------------------------------------------------------------------ rename \cite
@pytest.mark.parametrize("text, expected", [
    (r"as shown in \cite{vit_transformer}.", r"as shown in \cite{vaswani2017}."),
    (r"\citep[see][p.~3]{vit_transformer, other}", r"\citep[see][p.~3]{vaswani2017, other}"),
    (r"\citet*{vit_transformer}", r"\citet*{vaswani2017}"),
    (r"\nocite{vit_transformer}", r"\nocite{vaswani2017}"),
    (r"\textcite{other,vit_transformer}", r"\textcite{other,vaswani2017}"),
    (r"\cite{vit_transformer,vaswani2017}", r"\cite{vaswani2017}"),                 # both now the same key
    (r"no citation here", r"no citation here"),
    (r"\citeyear{vit_transformer}", r"\citeyear{vaswani2017}"),
])
def test_rename_citations(text, expected):
    assert rename_citations(text, {"vit_transformer": "vaswani2017"}, {"vit_transformer", "vaswani2017", "other"})[0] == expected


def test_unknown_keys_are_kept_and_reported():
    text, unknown = rename_citations(r"\cite{ghost, vit_transformer}", {"vit_transformer": "vaswani2017"}, {"vit_transformer"})
    assert text == r"\cite{ghost, vaswani2017}" and unknown == {"ghost"}


# ------------------------------------------------------------------ level 2: the example of the statement
def node(temp_id, parent, position, kind, content):
    return dict(temp_id=temp_id, parent_temp_id=parent, position=position, kind=kind, cleaned_content=content)


def statement_trees():
    v1 = [node("r", None, 0, "document", "Version 1 root"), node("h", "r", 0, "heading", "Introduction"),
          node("s", "h", 0, "sentence", "We study the citation matching problem")]
    v2 = [node("r", None, 0, "document", "Version 2 root"), node("h", "r", 0, "heading", "Background and model"),
          node("s", "h", 0, "sentence", "We study the citation matching problem")]
    return [(1, v1), (2, v2)]


def test_statement_example_five_elements_four_edges():
    elements, hierarchy, occurrences, unknown = deduplicate_elements_and_build_hierarchy(PAPER, statement_trees())
    assert [(e["element_id"], e["kind"], e["content"]) for e in elements] == [
        (PREFIX + "root1", "document", "Version 1 root"), (PREFIX + "h1", "heading", "Introduction"),
        (PREFIX + "s1", "sentence", "We study the citation matching problem"),
        (PREFIX + "root2", "document", "Version 2 root"), (PREFIX + "h2", "heading", "Background and model")]
    assert all(e["arxiv_id"] == PAPER for e in elements)
    assert [(h["version"], h["element_id"], h["parent_id"]) for h in hierarchy] == [
        (1, PREFIX + "h1", PREFIX + "root1"), (1, PREFIX + "s1", PREFIX + "h1"),
        (2, PREFIX + "h2", PREFIX + "root2"), (2, PREFIX + "s1", PREFIX + "h2")]     # s1 reused in v2
    assert not any(h["element_id"].endswith("root1") for h in hierarchy)               # roots have no edge
    assert unknown == set()
    assert [o["element_id"] for o in occurrences[2]] == [PREFIX + "root2", PREFIX + "h2", PREFIX + "s1"]


def test_repeated_sentence_gives_repeated_occurrences_and_edges():
    tree = [node("r", None, 0, "document", "Root"), node("h", "r", 0, "heading", "Intro"),
            node("a", "h", 0, "sentence", "Same."), node("b", "h", 1, "sentence", "Same.")]
    elements, hierarchy, occurrences, _ = deduplicate_elements_and_build_hierarchy(PAPER, [(1, tree)])
    assert len(elements) == 3                                                     # root, heading, one sentence
    assert [h["element_id"] for h in hierarchy] == [PREFIX + "h1", PREFIX + "s1", PREFIX + "s1"]   # no edge dropped
    assert [(o["occurrence_id"], o["parent_occurrence_id"], o["position"]) for o in occurrences[1]] == [
        ("o1", None, 0), ("o2", "o1", 0), ("o3", "o2", 0), ("o4", "o2", 1)]


def test_kind_is_part_of_the_identity():
    tree = [node("r", None, 0, "document", "Root"), node("h", "r", 0, "heading", "Same text"),
            node("s", "r", 1, "sentence", "Same text"), node("e", "r", 2, "block_formula", "x = 1")]
    elements, _, _, _ = deduplicate_elements_and_build_hierarchy(PAPER, [(1, tree)])
    assert [e["element_id"] for e in elements] == [PREFIX + s for s in ("root1", "h1", "s1", "eq1")]


def test_citations_are_renamed_before_content_is_compared():
    v1 = [node("r", None, 0, "document", "R1"), node("s", "r", 0, "sentence", r"See \cite{vaswani2017}")]
    v2 = [node("r", None, 0, "document", "R2"), node("s", "r", 0, "sentence", r"See \cite{vit_transformer}")]
    elements, hierarchy, _, _ = deduplicate_elements_and_build_hierarchy(
        PAPER, [(1, v1), (2, v2)], {"vit_transformer": "vaswani2017"}, {"vaswani2017", "vit_transformer"})
    sentences = [e for e in elements if e["kind"] == "sentence"]
    assert [e["content"] for e in sentences] == [r"See \cite{vaswani2017}"]       # one shared element
    assert len(hierarchy) == 2


def test_legacy_id_prefix():
    elements, _, _, _ = deduplicate_elements_and_build_hierarchy(
        "hep-th/9901001", [(1, [node("r", None, 0, "document", "Root")])])
    assert elements[0]["element_id"] == "hep-th_9901001#root1"


@pytest.mark.parametrize("mutate", [
    lambda t: t.append(deepcopy(t[0])),                                  # two roots
    lambda t: t[1].update(parent_temp_id="ghost"),                       # dangling parent
    lambda t: t[0].update(kind="heading"),                               # root is not a document
    lambda t: t.append(node("h", "r", 5, "sentence", "dup id")),         # duplicate temp_id
    lambda t: t.append(node("z", "r", 5, "mystery", "unknown kind")),
])
def test_invalid_trees_are_rejected(mutate):
    tree = statement_trees()[0][1]
    mutate(tree)
    with pytest.raises(ValueError):
        deduplicate_elements_and_build_hierarchy(PAPER, [(1, tree)])


def test_unsorted_nodes_are_ordered_by_position():
    tree = [node("s2", "r", 1, "sentence", "Second"), node("r", None, 0, "document", "Root"),
            node("s1", "r", 0, "sentence", "First")]
    elements, _, _, _ = deduplicate_elements_and_build_hierarchy(PAPER, [(1, tree)])
    assert [e["content"] for e in elements] == ["Root", "First", "Second"]


def test_render_bibtex_round_trips():
    rows, _, _ = unionize_bibtex(statement_entries())
    text = render_bibtex(rows)
    parsed, warnings = parse_bib_text(text)
    assert warnings == [] and parsed[0]["citation_key"] == "vaswani2017"
    assert (parsed[0]["title"], parsed[0]["year"], parsed[0]["doi"]) == ("Attention is all you need", 2017, "10.5555/3295222.3295349")


# ------------------------------------------------------------------ adapter
def write_version(tmp_path, number, nodes, entries, *, normalize_status=None, bib_status=None, warnings=()):
    out = tmp_path / "out"
    header = dict(schema_version=PARSING_SCHEMA_VERSION, arxiv_id=PAPER, version=number)
    tree_rel, bib_rel = f"parsed/{KEY}/v{number}/normalized_tree.json", f"parsed/{KEY}/v{number}/raw_bibtex.json"
    for relative, payload in ((tree_rel, dict(header, nodes=nodes, warnings=[])),
                              (bib_rel, dict(header, entries=entries, warnings=list(warnings)))):
        (out / relative).parent.mkdir(parents=True, exist_ok=True)
        (out / relative).write_text(json.dumps(payload), encoding="utf-8")

    def stage(name, artifact, relative, status):
        return dict(stage=name, paper_id=PAPER, version=number, status=status, schema_version=PARSING_SCHEMA_VERSION,
                    artifacts={artifact: relative})
    return dict(version=number,
                normalize=stage("normalize", "normalized_tree", tree_rel, normalize_status or ("success" if nodes else "no_source")),
                bibliography=stage("bibliography", "raw_bibtex", bib_rel, bib_status or ("success" if entries else "no_source")))


def paper_record(count):
    return dict(arxiv_id=PAPER, versions=[dict(version=n) for n in range(1, count + 1)])


def run_dedup(tmp_path, versions, count=None):
    upstream = dict(context=dict(input_root=str(tmp_path / "in"), input_snapshot_id=SNAPSHOT), stages={}, versions=versions)
    return run(paper_record(count or len(versions)), tmp_path / "out", upstream=upstream)


def statement_versions(tmp_path):
    entries = statement_entries()
    trees = statement_trees()
    t1, t2 = trees[0][1], deepcopy(trees[1][1])
    t1[2]["cleaned_content"] = r"We study this \cite{vaswani2017}"
    t2[2]["cleaned_content"] = r"We study this \cite{vit_transformer}"
    return [write_version(tmp_path, 1, t1, entries[0][1]), write_version(tmp_path, 2, t2, entries[1][1]),
            write_version(tmp_path, 3, [], entries[2][1], normalize_status="no_source")]


def test_run_writes_tables_manifest_and_bib(tmp_path):
    result = run_dedup(tmp_path, statement_versions(tmp_path))
    assert result.status == "success" and result.schema_version == PARSING_SCHEMA_VERSION
    assert (result.stage, result.paper_id, result.version) == ("deduplicate", PAPER, None)
    assert set(result.artifacts) == {"elements", "hierarchy", "bibtex", "bibtex_file", "parsing_manifest"}
    out = tmp_path / "out"
    read = lambda name: json.loads((out / result.artifacts[name]).read_text())
    elements, hierarchy, bibtex, manifest = read("elements"), read("hierarchy"), read("bibtex"), read("parsing_manifest")
    assert [e["element_id"] for e in elements if e["kind"] == "sentence"] == [PREFIX + "s1"]    # shared across v1, v2
    assert len(hierarchy) == 4 and len(bibtex) == 1 and bibtex[0]["citation_key"] == "vaswani2017"
    assert "@misc{vaswani2017" in (out / result.artifacts["bibtex_file"]).read_text()
    assert manifest["expected_versions"] == [1, 2, 3] and manifest["complete"] is True
    assert manifest["counts"] == dict(elements_before=6, elements_after=5, bibtex_before=3, bibtex_after=1)
    assert [(v["version"], v["status"]) for v in manifest["versions"]] == [(1, "success"), (2, "success"), (3, "no_source")]
    assert manifest["versions"][2]["reason"] == "no TeX source; BibTeX inventory kept"           # v3 kept its .bib
    assert manifest["input_snapshot_id"] == SNAPSHOT and set(manifest["tables"]) == {"elements", "hierarchy", "bibtex", "bibtex_file"}
    assert json.loads((out / manifest["versions"][1]["artifacts"]["occurrences"]).read_text())["occurrences"][0]["parent_occurrence_id"] is None
    assert json.loads((out / manifest["versions"][2]["artifacts"]["occurrences"]).read_text())["occurrences"] == []


def test_run_all_versions_empty_still_exports_empty_tables(tmp_path):
    result = run_dedup(tmp_path, [write_version(tmp_path, 1, [], [])])
    assert result.status == "no_source"
    out = tmp_path / "out"
    assert all(json.loads((out / result.artifacts[n]).read_text()) == [] for n in ("elements", "hierarchy", "bibtex"))
    manifest = json.loads((out / result.artifacts["parsing_manifest"]).read_text())
    assert manifest["complete"] is True and manifest["counts"] == dict(elements_before=0, elements_after=0, bibtex_before=0, bibtex_after=0)


def test_run_bib_only_paper_keeps_bibtex(tmp_path):
    result = run_dedup(tmp_path, [write_version(tmp_path, 1, [], [entry("k", title="Only in bib")])])
    assert result.status == "no_source"
    assert len(json.loads((tmp_path / "out" / result.artifacts["bibtex"]).read_text())) == 1


def test_run_warnings_for_conflicts_and_unknown_keys(tmp_path):
    t = [node("r", None, 0, "document", "R"), node("s", "r", 0, "sentence", r"\cite{ghost}")]
    versions = [write_version(tmp_path, 1, t, [entry("k", year=2017, title="Same title of a paper")]),
                write_version(tmp_path, 2, t, [entry("k", year=2018, title="Same title of a paper")],
                              warnings=[dict(code="bib_source_missing", message="x", source_path=None)])]
    result = run_dedup(tmp_path, versions)
    codes = [w["code"] for w in json.loads((tmp_path / "out" / result.artifacts["parsing_manifest"]).read_text())["warnings"]]
    assert {"bib_field_conflict", "citation_key_unknown", "bib_source_missing", "bib_source_missing_versions"} <= set(codes)


@pytest.mark.parametrize("break_it", ["missing_version", "normalize_error", "bibliography_skipped", "unexpected_version"])
def test_run_skips_the_paper_when_a_version_is_not_ready(tmp_path, break_it):
    versions = statement_versions(tmp_path)
    count = 3
    if break_it == "missing_version":
        versions.pop(1)
    elif break_it == "normalize_error":
        versions[1]["normalize"]["status"] = "error"
    elif break_it == "bibliography_skipped":
        versions[2]["bibliography"]["status"] = "skipped"
    else:
        count = 2
    result = run_dedup(tmp_path, versions, count)
    assert result.status == "skipped" and result.artifacts == {} and result.error.kind == "upstream_unavailable"
    assert not (tmp_path / "out" / "tables").exists()                              # nothing half written


def test_run_data_errors(tmp_path):
    versions = statement_versions(tmp_path)
    assert run_dedup(tmp_path, versions[::-1]).status == "data_error"              # not ascending
    bad = statement_versions(tmp_path)
    bad[2]["normalize"]["status"] = "no_source"
    nodes = json.loads((tmp_path / "out" / bad[0]["normalize"]["artifacts"]["normalized_tree"]).read_text())["nodes"]
    path = tmp_path / "out" / bad[2]["normalize"]["artifacts"]["normalized_tree"]
    path.write_text(json.dumps(dict(schema_version=PARSING_SCHEMA_VERSION, arxiv_id=PAPER, version=3, nodes=nodes, warnings=[])))
    assert run_dedup(tmp_path, bad).status == "data_error"                          # no_source with content
    foreign = statement_versions(tmp_path)
    foreign[0]["normalize"]["paper_id"] = "9999.99999"
    assert run_dedup(tmp_path, foreign).status == "data_error"
    assert run({"arxiv_id": PAPER}, tmp_path / "out", upstream=None).status == "data_error"
    no_snapshot = run(paper_record(1), tmp_path / "out", upstream=dict(context={}, stages={}, versions=[]))
    assert no_snapshot.status == "data_error"


def test_run_is_deterministic_and_idempotent(tmp_path):
    first = run_dedup(tmp_path, statement_versions(tmp_path))
    snapshot = {n: (tmp_path / "out" / p).read_bytes() for n, p in first.artifacts.items()}
    second = run_dedup(tmp_path, statement_versions(tmp_path))
    assert {n: (tmp_path / "out" / p).read_bytes() for n, p in second.artifacts.items()} == snapshot
