"""Unit tests for TV4 bibliography extraction (offline, synthetic strings only)."""

import json
from pathlib import Path

import pytest

from arxiv_pipeline.common.schemas import PARSING_SCHEMA_VERSION
from arxiv_pipeline.parsing.bibliography import clean_text, parse_bib_text, parse_bibitem, run, safe_join

PAPER, KEY, SNAPSHOT = "2310.12345", "2310.12345", "snap-1"


# ------------------------------------------------------------------ \bibitem
def test_plain_style_with_newblock():
    raw = (r"\bibitem{vaswani2017} A.~Vaswani, N.~Shazeer, and N.~Parmar. \newblock Attention is all you need. "
           r"\newblock In \emph{Advances in Neural Information Processing Systems}, 2017.")
    fields, provenance = parse_bibitem(raw)
    assert fields["citation_key"] == "vaswani2017"
    assert fields["authors"] == ["A. Vaswani", "N. Shazeer", "N. Parmar"]
    assert fields["title"] == "Attention is all you need"
    assert fields["year"] == 2017
    assert fields["journal"] == "Advances in Neural Information Processing Systems"
    assert fields["entry_type"] == "misc"                      # never guessed from a free-form item
    assert {"title", "authors", "year", "journal"} <= set(provenance["fields_extracted"])
    assert provenance["raw_text"] == raw


def test_label_option_and_doi_url():
    raw = (r"\bibitem[Dosovitskiy et~al.(2021)]{vit} Alexey Dosovitskiy. \newblock An image is worth 16x16 words. "
           r"\newblock \url{https://arxiv.org/abs/2010.11929}, 2021. doi: 10.1000/XYZ-123.")
    fields, _ = parse_bibitem(raw)
    assert fields["citation_key"] == "vit"
    assert fields["doi"] == "10.1000/XYZ-123"
    assert fields["url"] == "https://arxiv.org/abs/2010.11929"
    assert fields["year"] == 2021                              # not 2010 from the arXiv number


def test_doi_url_form():
    fields, _ = parse_bibitem(r"\bibitem{k} Someone. \newblock A title. \newblock https://doi.org/10.1145/3292500.3330701 2019.")
    assert fields["doi"] == "10.1145/3292500.3330701"
    assert fields["url"] is None                               # a doi.org link is a DOI, not a URL


def test_quoted_title_style():
    fields, _ = parse_bibitem(r"\bibitem{he2016} K. He, X. Zhang, S. Ren, ``Deep residual learning,'' \emph{CVPR}, 2016.")
    assert fields["title"] == "Deep residual learning"
    assert fields["authors"] == ["K. He", "X. Zhang", "S. Ren"]
    assert fields["journal"] == "CVPR"
    assert fields["year"] == 2016


def test_last_first_authors_stay_paired():
    fields, _ = parse_bibitem(r"\bibitem{k} Doe, J. and Smith, K. \newblock Some title. \newblock Venue, 2001.")
    assert fields["authors"] == ["Doe, J.", "Smith, K."]


def test_ambiguous_item_leaves_fields_empty():
    raw = r"\bibitem{mystery} Some free text that nobody can split reliably but written in 2019."
    fields, provenance = parse_bibitem(raw)
    assert fields["title"] == "" and fields["authors"] == [] and fields["journal"] is None
    assert fields["year"] == 2019
    assert provenance["fields_extracted"] == ["year"]


def test_missing_key_is_not_a_row():
    fields, provenance = parse_bibitem(r"\bibitem{} Nothing here 2020.")
    assert fields is None and provenance["raw_text"]
    assert parse_bibitem("not a bibitem at all")[0] is None


def test_clean_text_strips_markup():
    assert clean_text(r"{\em Deep} \textbf{learning}~for \'e dges \% done.") == "Deep learning for e dges % done"


# ------------------------------------------------------------------ .bib files
BIB = r"""
@string{nips = "Advances in Neural Information Processing Systems"}
@comment{ignored}
@article{vaswani2017,
  title = {Attention {Is} All You Need},
  author = {Vaswani, Ashish and Shazeer, Noam and {Google Brain}},
  year = {2017},
  journal = nips,
  doi = {https://doi.org/10.5555/3295222.3295349}
}
@inproceedings{he2016, title={Deep Residual Learning}, author={Kaiming He}, booktitle={CVPR}, year={2016}}
@MISC{nokey_title_only, title = {Only a title}}
"""


def test_bib_file_entries():
    rows, warnings = parse_bib_text(BIB)
    assert warnings == []
    by_key = {r["citation_key"]: r for r in rows}
    assert set(by_key) == {"vaswani2017", "he2016", "nokey_title_only"}
    vas = by_key["vaswani2017"]
    assert vas["entry_type"] == "article" and vas["title"] == "Attention Is All You Need"
    assert vas["authors"] == ["Ashish Vaswani", "Noam Shazeer", "Google Brain"]
    assert vas["year"] == 2017 and vas["doi"] == "10.5555/3295222.3295349"
    assert vas["journal"] == "Advances in Neural Information Processing Systems"
    assert by_key["he2016"]["entry_type"] == "inproceedings" and by_key["he2016"]["journal"] == "CVPR"
    misc = by_key["nokey_title_only"]
    assert misc["entry_type"] == "misc" and misc["authors"] == [] and misc["year"] is None and misc["doi"] is None


def test_broken_bib_reports_unparsed_entries():
    rows, warnings = parse_bib_text("@article{ok, title={Fine}, year={2020}}\n@article{broken, title={Unclosed")
    assert [r["citation_key"] for r in rows] == ["ok"]
    assert any(w["code"] == "bib_entries_unparsed" for w in warnings)


# ------------------------------------------------------------------ adapter
def write_gathered(tmp_path, number=1, **overrides):
    """Create a gathered.json and the resolve StageResult dict that points to it."""
    gathered = dict(schema_version=PARSING_SCHEMA_VERSION, arxiv_id=PAPER, version=number, is_empty_source=False,
                    main_tex_path=f"corpus/{KEY}/v{number}/main.tex", included_files=[], ignored_files=[],
                    merged_body_tex="body", preamble_tex="", raw_bibitems=[], bib_file_paths=[],
                    referenced_bib_file_paths=[], warnings=[], source_spans=[], input_snapshot_id=SNAPSHOT)
    gathered.update(overrides)
    relative = f"parsed/{KEY}/v{number}/gathered.json"
    path = tmp_path / "out" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(gathered), encoding="utf-8")
    status = "no_source" if gathered["is_empty_source"] else "success"
    return dict(stage="resolve", paper_id=PAPER, version=number, status=status, schema_version=PARSING_SCHEMA_VERSION,
                artifacts={"gathered": relative})


def envelope(tmp_path, resolve, **context):
    return dict(context=dict(input_root=str(tmp_path / "in"), input_snapshot_id=SNAPSHOT, **context),
                stages={"resolve": resolve} if resolve is not None else {})


def call(tmp_path, number, resolve):
    return run({"arxiv_id": PAPER}, tmp_path / "out", version={"version": number}, upstream=envelope(tmp_path, resolve))


def test_run_merges_bibitems_and_cited_bib(tmp_path):
    bib_dir = tmp_path / "in" / "corpus" / KEY / "v1"
    bib_dir.mkdir(parents=True)
    (bib_dir / "refs.bib").write_text("@article{cited, title={Cited work title}, year={2020}}", encoding="utf-8")
    (bib_dir / "unused.bib").write_text("@article{unused, title={Not cited}, year={2021}}", encoding="utf-8")
    resolve = write_gathered(tmp_path, raw_bibitems=[r"\bibitem{a} X. Y. \newblock Hand written. \newblock V, 2019."],
                             bib_file_paths=[f"corpus/{KEY}/v1/refs.bib", f"corpus/{KEY}/v1/unused.bib"],
                             referenced_bib_file_paths=[f"corpus/{KEY}/v1/refs.bib"])
    result = call(tmp_path, 1, resolve)
    assert result.status == "success" and result.schema_version == PARSING_SCHEMA_VERSION
    assert (result.stage, result.paper_id, result.version) == ("bibliography", PAPER, 1)
    raw = json.loads((tmp_path / "out" / result.artifacts["raw_bibtex"]).read_text())
    assert (raw["schema_version"], raw["arxiv_id"], raw["version"]) == (PARSING_SCHEMA_VERSION, PAPER, 1)
    assert [e["citation_key"] for e in raw["entries"]] == ["a", "cited"]      # uncited .bib ignored
    assert all(e["arxiv_id"] == PAPER for e in raw["entries"])
    side = json.loads((tmp_path / "out" / result.artifacts["provenance"]).read_text())
    assert side["items"][0]["raw_text"].startswith(r"\bibitem{a}")


def test_run_reads_whole_inventory_when_no_cited_bib(tmp_path):
    """A version without TeX (bib-only) keeps its .bib entries."""
    bib_dir = tmp_path / "in" / "corpus" / KEY / "v4"
    bib_dir.mkdir(parents=True)
    (bib_dir / "refs.bib").write_text("@article{only, title={Only bib}, year={2022}}", encoding="utf-8")
    resolve = write_gathered(tmp_path, 4, is_empty_source=True, main_tex_path=None, merged_body_tex="",
                             bib_file_paths=[f"corpus/{KEY}/v4/refs.bib"])
    result = call(tmp_path, 4, resolve)
    assert result.status == "success"
    assert json.loads((tmp_path / "out" / result.artifacts["raw_bibtex"]).read_text())["entries"][0]["citation_key"] == "only"


def test_run_without_any_reference_source(tmp_path):
    empty = call(tmp_path, 3, write_gathered(tmp_path, 3, is_empty_source=True, main_tex_path=None, merged_body_tex=""))
    assert empty.status == "no_source"
    assert json.loads((tmp_path / "out" / empty.artifacts["raw_bibtex"]).read_text())["entries"] == []
    with_tex = call(tmp_path, 1, write_gathered(tmp_path, 1))
    assert with_tex.status == "no_source"
    warnings = json.loads((tmp_path / "out" / with_tex.artifacts["raw_bibtex"]).read_text())["warnings"]
    assert [w["code"] for w in warnings] == ["bib_source_missing"]      # TeX present, nothing to extract


def test_run_reports_unparsed_bibitem_and_missing_file(tmp_path):
    resolve = write_gathered(tmp_path, raw_bibitems=[r"\bibitem{} broken", r"\bibitem{ok} T. \newblock Title. \newblock V, 2020."],
                             bib_file_paths=[f"corpus/{KEY}/v1/gone.bib"])
    result = call(tmp_path, 1, resolve)
    assert result.status == "success"
    codes = [w["code"] for w in json.loads((tmp_path / "out" / result.artifacts["raw_bibtex"]).read_text())["warnings"]]
    assert codes == ["bibitem_unparsed", "bib_file_missing"]


def test_run_input_errors(tmp_path):
    resolve = write_gathered(tmp_path)
    assert call(tmp_path, 1, None).status == "skipped"                          # resolve not ready
    assert call(tmp_path, 1, dict(resolve, status="error")).status == "skipped"
    assert call(tmp_path, 1, dict(resolve, version=2)).status == "data_error"   # identity mismatch
    bad = write_gathered(tmp_path, 2, referenced_bib_file_paths=["../../etc/passwd"])
    assert call(tmp_path, 2, bad).status == "data_error"                        # path escape
    assert run({"arxiv_id": PAPER}, tmp_path / "out", version={"version": 1}, upstream=None).status == "data_error"
    wrong_snapshot = run({"arxiv_id": PAPER}, tmp_path / "out", version={"version": 1},
                         upstream=envelope(tmp_path, resolve) | {"context": {"input_root": str(tmp_path), "input_snapshot_id": "other"}})
    assert wrong_snapshot.status == "data_error"


def test_safe_join_rejects_escapes(tmp_path):
    for bad in ("/abs", "../x", "a/../../x", "", "a\\b"):
        with pytest.raises(ValueError):
            safe_join(tmp_path, bad)
    assert safe_join(tmp_path, "a/b.json") == (tmp_path / "a/b.json").resolve()
