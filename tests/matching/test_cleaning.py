"""Unit tests for TV2 cleaning module (text, LaTeX unescape, author parsing, identifiers)."""

from arxiv_pipeline.matching.cleaning import (
    canonical_doi,
    clean_latex_text,
    extract_arxiv_id,
    normalize_bibtex,
    normalize_reference,
    normalize_title,
    parse_authors,
    parse_year,
)


def test_clean_latex_accents_and_macros():
    # Accents with braces
    assert clean_latex_text(r"Schr\"{o}dinger and Poincar\'{e}") == "Schrödinger and Poincaré"
    # Accents without braces
    assert clean_latex_text(r"Schr\"odinger and Poincar\'e") == "Schrödinger and Poincaré"
    # Protective BibTeX braces
    assert clean_latex_text(r"{Deep} {Learning} in {LaTeX}") == "Deep Learning in LaTeX"
    # Style macros
    assert clean_latex_text(r"\textbf{Important} \emph{Survey}") == "Important Survey"
    # Math mode removal
    assert clean_latex_text(r"On the $O(N)$ and $\mathcal{H}_2$ space") == "On the and space"
    # Escaped characters
    assert clean_latex_text(r"AT\&T 100\% pure\_data") == "AT&T 100% pure_data"


def test_normalize_title():
    assert normalize_title(None) == ""
    assert normalize_title("") == ""
    # Casefolding and punctuation stripping
    raw = r"A Unified Framework for {Graph Neural Networks}: Theory \& Applications"
    expected = "a unified framework for graph neural networks theory applications"
    assert normalize_title(raw) == expected

    # Hyphen handling
    assert normalize_title("Self-Supervised Pre-Training") == "self supervised pre training"


def test_parse_authors():
    assert parse_authors(None) == []
    assert parse_authors([]) == []

    # Various author formats
    authors = ["LeCun, Yann", "Bengio, Yoshua", "Geoffrey Hinton"]
    parsed = parse_authors(authors)
    assert len(parsed) == 3
    assert parsed[0] == {"last_name": "lecun", "first_name": "yann", "initials": "y"}
    assert parsed[1] == {"last_name": "bengio", "first_name": "yoshua", "initials": "y"}
    assert parsed[2] == {"last_name": "hinton", "first_name": "geoffrey", "initials": "g"}

    # String with "and"
    raw_str = "John Doe and Jane Smith and Alan Turing et al."
    parsed_str = parse_authors(raw_str)
    assert len(parsed_str) == 3
    assert parsed_str[0]["last_name"] == "doe"
    assert parsed_str[1]["last_name"] == "smith"
    assert parsed_str[2]["last_name"] == "turing"


def test_canonical_doi():
    assert canonical_doi(None) is None
    assert canonical_doi("") is None
    assert canonical_doi("https://doi.org/10.1145/3372278.3390670") == "10.1145/3372278.3390670"
    assert canonical_doi("http://dx.doi.org/10.1000/182") == "10.1000/182"
    assert canonical_doi("doi:10.1038/nature12373") == "10.1038/nature12373"
    assert canonical_doi("10.1038/NATURE12373") == "10.1038/nature12373"


def test_extract_arxiv_id():
    assert extract_arxiv_id(None) is None
    assert extract_arxiv_id("https://arxiv.org/abs/2310.12345v2") == "2310.12345"
    assert extract_arxiv_id("arXiv:2001.00001") == "2001.00001"
    assert extract_arxiv_id("math.GT/0309136v1") == "math.gt/0309136"
    assert extract_arxiv_id("Unrelated string without ID") is None


def test_parse_year():
    assert parse_year(None) is None
    assert parse_year(2021) == 2021
    assert parse_year("2021") == 2021
    assert parse_year("2021-09-15T12:00:00Z") == 2021
    assert parse_year(1850) is None  # Out of range
    assert parse_year(2050) is None  # Out of range


def test_normalize_bibtex_and_reference():
    class DummyBib:
        arxiv_id = "2310.12345"
        citation_key = "Goodfellow2014"
        title = "Generative {Adversarial} Nets"
        authors = ["Goodfellow, Ian and Pouget-Abadie, Jean"]
        year = "2014"
        doi = "https://doi.org/10.5555/2969033.2969166"
        url = "https://arxiv.org/abs/1406.2661"
        journal = None

    norm_bib = normalize_bibtex(DummyBib())
    assert norm_bib["clean_title"] == "generative adversarial nets"
    assert len(norm_bib["clean_authors"]) == 2
    assert norm_bib["clean_year"] == 2014
    assert norm_bib["clean_doi"] == "10.5555/2969033.2969166"
    assert norm_bib["extracted_arxiv_id"] == "1406.2661"

    class DummyRef:
        arxiv_id = "2310.12345"
        ref_arxiv_id = "1406.2661"
        ref_title = "Generative Adversarial Networks"
        ref_authors = ["Ian Goodfellow"]
        ref_submission_date = "2014-06-10"

    norm_ref = normalize_reference(DummyRef())
    assert norm_ref["clean_title"] == "generative adversarial networks"
    assert norm_ref["clean_year"] == 2014
    assert norm_ref["clean_authors"][0]["last_name"] == "goodfellow"
