"""Unit tests for Reference Matching high-precision heuristics (Milestone 3 · TV3)."""

from __future__ import annotations

import pytest

from arxiv_pipeline.matching.contracts import (
    ContractError,
    publication_from_rows,
    validate_labels,
)
from arxiv_pipeline.matching.heuristics import (
    HeuristicPolicy,
    auto_label,
    extract_arxiv_ids,
    extract_author_last_names,
    normalize_title,
    summarize_auto_labels,
)
from tests.fixtures.matching.examples import publications


def _bundle(arxiv_id, bibtex_rows, reference_rows, *, readiness="ready"):
    return publication_from_rows(arxiv_id, bibtex_rows, reference_rows, readiness=readiness)


def test_normalize_title_and_extract_helpers():
    assert normalize_title(r"\textbf{Attention Is All You Need!}") == "attention is all you need"
    assert normalize_title(r"Café \& Crème: A $O(n^2)$ Study") == "cafe creme a o n 2 study"

    assert extract_arxiv_ids("10.48550/arXiv.2310.12345v2") == ["2310.12345"]
    assert extract_arxiv_ids("https://arxiv.org/abs/hep-th/9901001v1") == ["hep-th/9901001"]
    assert extract_author_last_names(["Vaswani, Ashish", "Noam Shazeer"]) == {"vaswani", "shazeer"}
    assert extract_author_last_names(["ATLAS Collaboration", "The CMS Collaboration", "others"]) == set()


def test_pilot_typo_matches_without_accepting_related_but_different_papers():
    paper = "2506.19423"
    bundle = _bundle(
        paper,
        [
            dict(arxiv_id=paper, citation_key="KloKuw", entry_type="article",
                 title="Explicit sections on Kuwata's elliptic surfaces",
                 authors=["R. Kloosterman"], year=2005, doi=None, journal=None, url=None),
            dict(arxiv_id=paper, citation_key="Bartosz", entry_type="article",
                 title="Geometry of the del Pezzo surface $y^2 = x^3 + A m^6 + B n^6$",
                 authors=["J. Desjardins", "B. Naskr\\kecki"], year=2024,
                 doi=None, journal=None, url=None),
        ],
        [
            dict(arxiv_id=paper, ref_arxiv_id="math/0502017",
                 ref_title="Explicit sectons on Kuwata's elliptic surfaces",
                 ref_authors=["R. Kloosterman"], ref_submission_date="2005-02-01"),
            dict(arxiv_id=paper, ref_arxiv_id="1911.02684",
                 ref_title="Geometry of the del Pezzo surface y^2=x^3+Am^6+Bn^6",
                 ref_authors=["J. Desjardins", "B. Naskrȩcki"],
                 ref_submission_date="2019-11-07"),
        ],
    )
    matched, conflict = auto_label([bundle])
    assert (matched.decision, matched.groundtruth_arxiv_id) == ("matched", "math/0502017")
    assert conflict.decision == "uncertain"

    related = _bundle(
        "2411.08799",
        [dict(arxiv_id="2411.08799", citation_key="el", entry_type="article",
              title="Number of prime factors with a given multiplicity",
              authors=["E. Elma", "Y.-R. Liu"], year=2022,
              doi=None, journal=None, url=None)],
        [dict(arxiv_id="2411.08799", ref_arxiv_id="2406.04574",
              ref_title="Distribution of the number of prime factors with a given multiplicity",
              ref_authors=["Ertan Elma", "Greg Martin"],
              ref_submission_date="2024-06-07")],
    )
    assert auto_label([related])[0].decision == "uncertain"


def test_auto_label_matches_explicit_arxiv_id_from_doi_or_url():
    b = _bundle(
        "2401.00001",
        [
            dict(
                arxiv_id="2401.00001",
                citation_key="vaswani2017",
                entry_type="article",
                title="Attention is all you need",
                authors=["Ashish Vaswani"],
                year=2017,
                doi="10.48550/arXiv.1706.03762",
                journal=None,
                url=None,
            ),
            dict(
                arxiv_id="2401.00001",
                citation_key="devlin2018",
                entry_type="article",
                title="BERT: Pre-training of Deep Bidirectional Transformers",
                authors=["Jacob Devlin"],
                year=2018,
                doi=None,
                journal=None,
                url="https://arxiv.org/abs/1810.04805v2",
            ),
        ],
        [
            dict(
                arxiv_id="2401.00001",
                ref_arxiv_id="1706.03762v5",
                ref_title="Attention Is All You Need",
                ref_authors=["A. Vaswani"],
                ref_submission_date="2017-06-12",
                ref_s2_id=None,
            ),
            dict(
                arxiv_id="2401.00001",
                ref_arxiv_id="1810.04805",
                ref_title="BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding",
                ref_authors=["J. Devlin"],
                ref_submission_date="2018-10-11",
                ref_s2_id=None,
            ),
        ],
    )
    labels = auto_label([b])
    assert len(labels) == 2
    assert labels[0].decision == "matched"
    assert labels[0].groundtruth_arxiv_id == "1706.03762"
    assert labels[0].confidence == 0.99
    assert labels[0].notes == "rule=explicit_arxiv_id"
    assert labels[0].label_source == "heuristic"
    assert labels[0].heuristic_version == "v1.2.0"

    assert labels[1].decision == "matched"
    assert labels[1].groundtruth_arxiv_id == "1810.04805"
    assert validate_labels([b], labels).valid


def test_auto_label_matches_normalized_title_and_abstains_on_author_mismatch():
    b = _bundle(
        "2401.00002",
        [
            dict(
                arxiv_id="2401.00002",
                citation_key="good_match",
                entry_type="inproceedings",
                title=r"Deep {Residual} Learning for Image Recognition",
                authors=["Kaiming He", "Xiangyu Zhang"],
                year=2016,
                doi=None,
                journal=None,
                url=None,
            ),
            dict(
                arxiv_id="2401.00002",
                citation_key="author_mismatch",
                entry_type="article",
                title="A Survey of Graph Neural Networks",
                authors=["Alice Smith"],
                year=2021,
                doi=None,
                journal=None,
                url=None,
            ),
        ],
        [
            dict(
                arxiv_id="2401.00002",
                ref_arxiv_id="1512.03385",
                ref_title="Deep Residual Learning for Image Recognition",
                ref_authors=["K. He", "X. Zhang"],
                ref_submission_date="2015-12-10",
                ref_s2_id=None,
            ),
            dict(
                arxiv_id="2401.00002",
                ref_arxiv_id="1901.00596",
                ref_title="A Survey of Graph Neural Networks",
                ref_authors=["Zonghan Wu", "Shirui Pan"],
                ref_submission_date="2019-01-03",
                ref_s2_id=None,
            ),
        ],
    )
    labels = auto_label([b])
    assert labels[0].decision == "matched"
    assert labels[0].groundtruth_arxiv_id == "1512.03385"
    assert labels[0].notes == "rule=exact_normalized_title"

    assert labels[1].decision == "uncertain"
    assert labels[1].groundtruth_arxiv_id is None
    assert labels[1].notes == "rule=title_match_author_mismatch"


def test_auto_label_on_shared_compatibility_fixtures():
    bundles = [
        _bundle(p["arxiv_id"], p["bibtex"], p["references"], readiness=p["readiness"])
        for p in publications()
    ]
    labels = auto_label(bundles)
    assert len(labels) == 4
    by_id = {lbl.arxiv_id: lbl for lbl in labels}

    # Exact fixture matches canonical ID
    assert by_id["2310.12345"].decision == "matched"
    assert by_id["2310.12345"].groundtruth_arxiv_id == "2001.00001"

    # Ambiguous fixture abstains
    assert by_id["2310.12346"].decision == "uncertain"
    assert by_id["2310.12346"].groundtruth_arxiv_id is None
    assert by_id["2310.12346"].notes == "rule=ambiguous_candidates"

    # Missing reference title abstains conservatively rather than mislabeling unmatched
    assert by_id["2310.12347"].decision == "uncertain"

    # Empty references resolves cleanly as unmatched
    assert by_id["hep-th/9901001"].decision == "unmatched"
    assert by_id["hep-th/9901001"].notes == "rule=empty_references"

    assert validate_labels(bundles, labels).valid


def test_auto_label_clear_no_match_and_year_conflict_and_summary():
    b = _bundle(
        "2401.00003",
        [
            dict(
                arxiv_id="2401.00003",
                citation_key="unrelated",
                entry_type="book",
                title="Classical Mechanics and Symplectic Geometry",
                authors=["Vladimir Arnold"],
                year=1989,
                doi=None,
                journal=None,
                url=None,
            ),
            dict(
                arxiv_id="2401.00003",
                citation_key="year_conflict",
                entry_type="article",
                title="Quantum Error Correction Codes",
                authors=["Peter Shor"],
                year=2024,
                doi=None,
                journal=None,
                url=None,
            ),
        ],
        [
            dict(
                arxiv_id="2401.00003",
                ref_arxiv_id="1001.0001",
                ref_title="Quantum Error Correction Codes",
                ref_authors=["Peter Shor"],
                ref_submission_date="2010-01-01",
                ref_s2_id=None,
            ),
        ],
    )
    policy = HeuristicPolicy(accept_clear_decisions=True)
    labels = auto_label([b], policy)

    assert labels[0].decision == "unmatched"
    assert labels[0].review_status == "accepted"
    assert labels[0].notes == "rule=clear_no_match"

    assert labels[1].decision == "uncertain"
    assert labels[1].review_status == "pending"
    assert labels[1].notes == "rule=title_match_year_conflict"

    summary = summarize_auto_labels(labels)
    assert summary["total_queries"] == 2
    assert summary["publication_count"] == 1
    assert summary["by_decision"] == {"matched": 0, "unmatched": 1, "uncertain": 1}
    assert summary["coverage_non_uncertain"] == 0.5


def test_exact_title_resolves_when_bibtex_year_is_malformed():
    paper = "2401.00004"
    bundle = _bundle(
        paper,
        [dict(arxiv_id=paper, citation_key="bad_year", entry_type="article",
              title="Bonsai: Diverse and shallow trees for extreme multi-label classification",
              authors=["Sujay Khandagale", "Han Xiao", "Rohit Babbar"], year=2099,
              doi=None, journal=None, url=None)],
        [dict(arxiv_id=paper, ref_arxiv_id="1904.08249",
              ref_title="Bonsai: diverse and shallow trees for extreme multi-label classification",
              ref_authors=["Sujay Khandagale", "Han Xiao", "Rohit Babbar"],
              ref_submission_date="2019-04-17")],
    )
    label = auto_label([bundle])[0]
    assert label.decision == "matched"
    assert label.groundtruth_arxiv_id == "1904.08249"
    assert label.notes == "rule=exact_normalized_title"


def test_auto_label_rejects_non_ready_or_duplicate_bundles():
    bad = _bundle("2401.00009", [], [], readiness="not_ready")
    with pytest.raises(ContractError, match="non-ready"):
        auto_label([bad])

    dup = _bundle("2401.00010", [], [], readiness="ready")
    with pytest.raises(ContractError, match="Duplicate publication"):
        auto_label([dup, dup])
