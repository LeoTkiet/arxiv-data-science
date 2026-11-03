"""Unit tests for TV3 Normalization module (tests/parsing/test_normalization.py).

Complies with docs/MS2_CONTRACTS.md and verifies:
- Math normalization (inline & block).
- LaTeX formatting cleanup while preserving citation keys.
- Sentence segmentation with protected token masking.
- Tree transformation (prose_block replacement, position re-indexing, preorder traversal).
- Adapter run() integration and no_source handling.
- Compatibility with TV4 deduplication _preorder() validator.
"""

import json
from pathlib import Path
from typing import Any
import pytest

from arxiv_pipeline.common.schemas import PARSING_SCHEMA_VERSION, StageResult
from arxiv_pipeline.parsing.normalization import (
    clean_latex_formatting,
    normalize_block_math,
    normalize_inline_math,
    run,
    split_into_sentences,
    transform_tree_to_normalized,
)
from arxiv_pipeline.parsing.deduplication import _preorder


# -----------------------------------------------------------------------------
# 1. Math Normalization Tests
# -----------------------------------------------------------------------------

def test_normalize_inline_math():
    text = r"Here is \( x + y = z \) and \begin{math} a \le b \end{math}."
    normalized = normalize_inline_math(text)
    assert r"$x + y = z$" in normalized
    assert r"$a \le b$" in normalized
    assert r"\(" not in normalized
    assert r"\begin{math}" not in normalized


def test_normalize_inline_math_cleans_spaces():
    text = r"Equation $  f(x) = \alpha  $ with spaces."
    normalized = normalize_inline_math(text)
    assert r"$f(x) = \alpha$" in normalized


def test_normalize_block_math_double_dollar():
    raw = r"$$ \int_0^1 x dx = 0.5 $$"
    norm = normalize_block_math(raw)
    assert norm == "\\begin{equation}\n\\int_0^1 x dx = 0.5\n\\end{equation}"


def test_normalize_block_math_bracket():
    raw = r"\[ E = mc^2 \]"
    norm = normalize_block_math(raw)
    assert norm == "\\begin{equation}\nE = mc^2\n\\end{equation}"


def test_normalize_block_math_environments():
    raw = "\\begin{align*}\n  a &= b + c \\\\\n  d &= e\n\\end{align*}"
    norm = normalize_block_math(raw)
    assert norm.startswith("\\begin{equation}")
    assert norm.endswith("\\end{equation}")
    assert "a &= b + c" in norm


# -----------------------------------------------------------------------------
# 2. LaTeX Cleanup Tests
# -----------------------------------------------------------------------------

def test_clean_latex_formatting_strips_layout_and_tables():
    text = (
        r"\begin{figure}[htpb] \centering \vspace{10pt} "
        r"\toprule \midrule \bottomrule \hline "
        r"Table content. \newpage \noindent \small More text."
    )
    cleaned = clean_latex_formatting(text)
    assert r"[htpb]" not in cleaned
    assert r"\centering" not in cleaned
    assert r"\vspace" not in cleaned
    assert r"\toprule" not in cleaned
    assert r"\bottomrule" not in cleaned
    assert r"\noindent" not in cleaned
    assert r"\small" not in cleaned
    assert "Table content." in cleaned
    assert "More text." in cleaned


def test_clean_latex_formatting_strips_text_styles():
    text = r"We use \textbf{deep} neural networks and \emph{nested \textit{recurrent} architectures}."
    cleaned = clean_latex_formatting(text)
    assert cleaned == "We use deep neural networks and nested recurrent architectures."


def test_clean_latex_formatting_preserves_citations_and_math():
    text = r"According to \cite{vaswani2017} and \citep[p.~10]{devlin2018}, when $x > 0$ we obtain $y$."
    cleaned = clean_latex_formatting(text)
    assert r"\cite{vaswani2017}" in cleaned
    assert r"\citep[p. 10]{devlin2018}" in cleaned or r"\citep[p.~10]{devlin2018}" in cleaned or r"\citep" in cleaned
    assert r"$x > 0$" in cleaned


def test_clean_latex_formatting_unescapes_symbols():
    text = r"Accuracy is 95\% with cost \$100, & data\_set \#1 \{ok\}."
    cleaned = clean_latex_formatting(text)
    assert "95%" in cleaned
    assert "$100" in cleaned
    assert "&" in cleaned
    assert "data_set" in cleaned
    assert "#1" in cleaned
    assert "{ok}" in cleaned


def test_clean_latex_formatting_quotes():
    text = "``Quoted text'' and `single'."
    cleaned = clean_latex_formatting(text)
    assert '"Quoted text"' in cleaned
    assert "'single'" in cleaned


# -----------------------------------------------------------------------------
# 3. Sentence Segmentation Tests
# -----------------------------------------------------------------------------

def test_split_into_sentences_basic():
    text = "First sentence here. Second sentence starts now! Is this the third sentence? Yes it is."
    sentences = split_into_sentences(text)
    assert len(sentences) == 4
    assert sentences[0] == "First sentence here."
    assert sentences[1] == "Second sentence starts now!"
    assert sentences[2] == "Is this the third sentence?"
    assert sentences[3] == "Yes it is."


def test_split_into_sentences_protects_abbreviations_and_numbers():
    text = (
        r"We follow Vaswani et al. (2017) and use Fig. 1 as shown in Sec. 2.1. "
        r"The parameter is $p = 0.05$ with lr = 3.14e-4, i.e., quite standard. "
        r"Next sentence follows Dr. Smith."
    )
    sentences = split_into_sentences(text)
    assert len(sentences) == 3
    assert "et al." in sentences[0]
    assert "Fig. 1" in sentences[0]
    assert "Sec. 2.1" in sentences[0]
    assert "3.14e-4" in sentences[1]
    assert "i.e.," in sentences[1]
    assert "$p = 0.05$" in sentences[1]
    assert "Dr. Smith." in sentences[2]


def test_split_into_sentences_protects_citations_and_urls():
    text = (
        r"See \cite{ref1, ref2} for details. "
        r"Visit https://arxiv.org/abs/2310.12345 for the code. Done."
    )
    sentences = split_into_sentences(text)
    assert len(sentences) == 3
    assert r"\cite{ref1, ref2}" in sentences[0]
    assert "https://arxiv.org/abs/2310.12345" in sentences[1]


# -----------------------------------------------------------------------------
# 4. Tree Transformation Tests
# -----------------------------------------------------------------------------

def test_transform_tree_replaces_prose_blocks_and_reindexes():
    raw_nodes = [
        {"temp_id": "r1", "parent_temp_id": None, "position": 0, "kind": "document", "raw_content": "Version 1 root"},
        {"temp_id": "h1", "parent_temp_id": "r1", "position": 0, "kind": "heading", "raw_content": r"\textbf{Introduction}"},
        {"temp_id": "p1", "parent_temp_id": "h1", "position": 0, "kind": "prose_block",
         "raw_content": "First sentence. Second sentence here."},
        {"temp_id": "eq1", "parent_temp_id": "h1", "position": 1, "kind": "block_formula", "raw_content": "$$ x = y $$"},
        {"temp_id": "p2", "parent_temp_id": "h1", "position": 2, "kind": "prose_block",
         "raw_content": "Third sentence after math."},
    ]

    normalized = transform_tree_to_normalized(raw_nodes)

    # prose_block must NOT be present
    kinds = [n["kind"] for n in normalized]
    assert "prose_block" not in kinds
    assert kinds == ["document", "heading", "sentence", "sentence", "block_formula", "sentence"]

    # Heading cleaned
    h_node = next(n for n in normalized if n["kind"] == "heading")
    assert h_node["cleaned_content"] == "Introduction"

    # Block math normalized
    eq_node = next(n for n in normalized if n["kind"] == "block_formula")
    assert eq_node["cleaned_content"] == "\\begin{equation}\nx = y\n\\end{equation}"

    # Children of h1
    h1_children = [n for n in normalized if n["parent_temp_id"] == "h1"]
    assert len(h1_children) == 4
    # Positions must be strictly 0, 1, 2, 3
    positions = [n["position"] for n in h1_children]
    assert positions == [0, 1, 2, 3]


def test_transform_tree_validates_with_tv4_preorder():
    raw_nodes = [
        {"temp_id": "r1", "parent_temp_id": None, "position": 0, "kind": "document", "raw_content": "Root"},
        {"temp_id": "h1", "parent_temp_id": "r1", "position": 0, "kind": "heading", "raw_content": "Sec 1"},
        {"temp_id": "p1", "parent_temp_id": "h1", "position": 0, "kind": "prose_block", "raw_content": "Sentence A. Sentence B."},
    ]
    normalized = transform_tree_to_normalized(raw_nodes)
    # Must pass TV4's _preorder validation without ValueError
    order = _preorder(1, normalized)
    assert len(order) == 4
    assert order[0]["kind"] == "document"
    assert order[1]["kind"] == "heading"
    assert order[2]["kind"] == "sentence"
    assert order[3]["kind"] == "sentence"


# -----------------------------------------------------------------------------
# 5. Adapter run() Integration Tests
# -----------------------------------------------------------------------------

def test_run_adapter_success(tmp_path: Path):
    output_dir = tmp_path / "out"
    paper_id = "2310.12345"
    key = "2310.12345"

    raw_tree_rel = f"parsed/{key}/v1/raw_tree.json"
    raw_tree_file = output_dir / raw_tree_rel
    raw_tree_file.parent.mkdir(parents=True, exist_ok=True)

    raw_tree_payload = {
        "schema_version": PARSING_SCHEMA_VERSION,
        "arxiv_id": paper_id,
        "version": 1,
        "nodes": [
            {"temp_id": "r1", "parent_temp_id": None, "position": 0, "kind": "document", "raw_content": "Root"},
            {"temp_id": "h1", "parent_temp_id": "r1", "position": 0, "kind": "heading", "raw_content": "Title"},
            {"temp_id": "p1", "parent_temp_id": "h1", "position": 0, "kind": "prose_block", "raw_content": "Hello world."},
        ],
        "warnings": [],
    }
    raw_tree_file.write_text(json.dumps(raw_tree_payload), encoding="utf-8")

    upstream = {
        "context": {"input_root": str(tmp_path), "input_snapshot_id": "snap-1"},
        "stages": {
            "hierarchy": {
                "stage": "hierarchy",
                "paper_id": paper_id,
                "version": 1,
                "status": "success",
                "schema_version": PARSING_SCHEMA_VERSION,
                "artifacts": {"raw_tree": raw_tree_rel},
            }
        },
    }

    result = run({"arxiv_id": paper_id}, output_dir, version={"version": 1}, upstream=upstream)

    assert result.status == "success"
    assert result.stage == "normalize"
    assert "normalized_tree" in result.artifacts

    norm_path = output_dir / result.artifacts["normalized_tree"]
    assert norm_path.is_file()
    payload = json.loads(norm_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == PARSING_SCHEMA_VERSION
    assert len(payload["nodes"]) == 3
    assert payload["nodes"][2]["kind"] == "sentence"
    assert payload["nodes"][2]["cleaned_content"] == "Hello world."


def test_run_adapter_no_source(tmp_path: Path):
    output_dir = tmp_path / "out"
    paper_id = "2310.12345"
    key = "2310.12345"

    raw_tree_rel = f"parsed/{key}/v2/raw_tree.json"
    raw_tree_file = output_dir / raw_tree_rel
    raw_tree_file.parent.mkdir(parents=True, exist_ok=True)

    raw_tree_payload = {
        "schema_version": PARSING_SCHEMA_VERSION,
        "arxiv_id": paper_id,
        "version": 2,
        "nodes": [],
        "warnings": [{"code": "no_tex_source", "message": "PDF only"}],
    }
    raw_tree_file.write_text(json.dumps(raw_tree_payload), encoding="utf-8")

    upstream = {
        "stages": {
            "hierarchy": {
                "stage": "hierarchy",
                "paper_id": paper_id,
                "version": 2,
                "status": "no_source",
                "schema_version": PARSING_SCHEMA_VERSION,
                "artifacts": {"raw_tree": raw_tree_rel},
            }
        }
    }

    result = run({"arxiv_id": paper_id}, output_dir, version={"version": 2}, upstream=upstream)
    assert result.status == "no_source"
    norm_path = output_dir / result.artifacts["normalized_tree"]
    payload = json.loads(norm_path.read_text(encoding="utf-8"))
    assert payload["nodes"] == []
    assert len(payload["warnings"]) == 1


def test_transform_tree_multiple_roots_rejected():
    raw_nodes = [
        {"temp_id": "r1", "parent_temp_id": None, "position": 0, "kind": "document", "raw_content": "Root 1"},
        {"temp_id": "r2", "parent_temp_id": None, "position": 1, "kind": "document", "raw_content": "Root 2"},
    ]
    with pytest.raises(ValueError, match="exactly one document root"):
        transform_tree_to_normalized(raw_nodes)


def test_transform_tree_cycle_rejected():
    raw_nodes = [
        {"temp_id": "r1", "parent_temp_id": None, "position": 0, "kind": "document", "raw_content": "Root"},
        {"temp_id": "n1", "parent_temp_id": "r1", "position": 0, "kind": "heading", "raw_content": "Sec 1"},
        {"temp_id": "n2", "parent_temp_id": "n1", "position": 0, "kind": "heading", "raw_content": "Sec 2"},
    ]
    # Artificially create cycle
    raw_nodes[1]["parent_temp_id"] = "n2"
    with pytest.raises(ValueError, match="unreachable nodes or invalid parents"):
        transform_tree_to_normalized(raw_nodes)


def test_transform_tree_empty_prose_block_handled():
    raw_nodes = [
        {"temp_id": "r1", "parent_temp_id": None, "position": 0, "kind": "document", "raw_content": "Root"},
        {"temp_id": "h1", "parent_temp_id": "r1", "position": 0, "kind": "heading", "raw_content": "Sec 1"},
        {"temp_id": "p1", "parent_temp_id": "h1", "position": 0, "kind": "prose_block", "raw_content": "\\centering \\vspace{10pt}"},
        {"temp_id": "eq1", "parent_temp_id": "h1", "position": 1, "kind": "block_formula", "raw_content": "$$ x = 1 $$"},
    ]
    normalized = transform_tree_to_normalized(raw_nodes)
    assert len(normalized) == 3
    assert normalized[2]["kind"] == "block_formula"
    assert normalized[2]["position"] == 0  # Position re-indexed to 0 since p1 produced no sentences


def test_clean_latex_formatting_deeply_nested_styles():
    text = r"A \textbf{very \textit{deeply \underline{nested} style} test}."
    cleaned = clean_latex_formatting(text)
    assert cleaned == "A very deeply nested style test."


def test_run_adapter_missing_hierarchy(tmp_path: Path):
    result = run({"arxiv_id": "2310.12345"}, tmp_path / "out", version={"version": 1}, upstream={})
    assert result.status == "skipped"
    assert result.error.kind == "upstream_unavailable"


def test_run_adapter_header_mismatch(tmp_path: Path):
    output_dir = tmp_path / "out"
    paper_id = "2310.12345"
    key = "2310.12345"

    raw_tree_rel = f"parsed/{key}/v1/raw_tree.json"
    raw_tree_file = output_dir / raw_tree_rel
    raw_tree_file.parent.mkdir(parents=True, exist_ok=True)

    # Wrong paper_id in payload
    raw_tree_payload = {
        "schema_version": PARSING_SCHEMA_VERSION,
        "arxiv_id": "2310.99999",
        "version": 1,
        "nodes": [],
        "warnings": [],
    }
    raw_tree_file.write_text(json.dumps(raw_tree_payload), encoding="utf-8")

    upstream = {
        "stages": {
            "hierarchy": {
                "stage": "hierarchy",
                "paper_id": paper_id,
                "version": 1,
                "status": "success",
                "schema_version": PARSING_SCHEMA_VERSION,
                "artifacts": {"raw_tree": raw_tree_rel},
            }
        }
    }

    result = run({"arxiv_id": paper_id}, output_dir, version={"version": 1}, upstream=upstream)
    assert result.status == "data_error"
    assert result.error.kind == "invalid_normalization_input"

def test_clean_latex_formatting_preserves_derivatives_and_primes():
    text = r"Given $f''(x) > 0$ and $g'(x) = f'''(t)$, we see that ``quoted text'' is preserved."
    cleaned = clean_latex_formatting(text)
    assert r"$f''(x) > 0$" in cleaned
    assert r"$g'(x) = f'''(t)$" in cleaned
    assert '"quoted text"' in cleaned
    assert "f\"(x)" not in cleaned


@pytest.mark.parametrize("field,value", [
    ("stage", "resolve"),
    ("paper_id", "2310.99999"),
    ("version", 2),
    ("schema_version", "1.0"),
])
def test_mismatched_hierarchy_checkpoint_identity_rejected(tmp_path: Path, field: str, value: Any):
    output_dir = tmp_path / "out"
    paper_id = "2310.12345"
    key = "2310.12345"

    raw_tree_rel = f"parsed/{key}/v1/raw_tree.json"
    raw_tree_file = output_dir / raw_tree_rel
    raw_tree_file.parent.mkdir(parents=True, exist_ok=True)

    raw_tree_payload = {
        "schema_version": PARSING_SCHEMA_VERSION,
        "arxiv_id": paper_id,
        "version": 1,
        "nodes": [
            {"temp_id": "r1", "parent_temp_id": None, "position": 0, "kind": "document", "raw_content": "Root"},
        ],
        "warnings": [],
    }
    raw_tree_file.write_text(json.dumps(raw_tree_payload), encoding="utf-8")

    hierarchy_dict = {
        "stage": "hierarchy",
        "paper_id": paper_id,
        "version": 1,
        "status": "success",
        "schema_version": PARSING_SCHEMA_VERSION,
        "artifacts": {"raw_tree": raw_tree_rel},
    }
    hierarchy_dict[field] = value

    upstream = {"stages": {"hierarchy": hierarchy_dict}}

    result = run({"arxiv_id": paper_id}, output_dir, version={"version": 1}, upstream=upstream)
    assert result.status == "data_error"
    assert result.error.kind == "invalid_normalization_input"

def test_run_adapter_missing_nodes_list_fails(tmp_path: Path):
    output_dir = tmp_path / "out"
    paper_id = "2310.12345"
    key = "2310.12345"

    raw_tree_rel = f"parsed/{key}/v1/raw_tree.json"
    raw_tree_file = output_dir / raw_tree_rel
    raw_tree_file.parent.mkdir(parents=True, exist_ok=True)

    # Missing "nodes" key
    raw_tree_payload = {
        "schema_version": PARSING_SCHEMA_VERSION,
        "arxiv_id": paper_id,
        "version": 1,
        "warnings": [],
    }
    raw_tree_file.write_text(json.dumps(raw_tree_payload), encoding="utf-8")

    upstream = {
        "stages": {
            "hierarchy": {
                "stage": "hierarchy",
                "paper_id": paper_id,
                "version": 1,
                "status": "success",
                "schema_version": PARSING_SCHEMA_VERSION,
                "artifacts": {"raw_tree": raw_tree_rel},
            }
        }
    }

    result = run({"arxiv_id": paper_id}, output_dir, version={"version": 1}, upstream=upstream)
    assert result.status == "data_error"
    assert result.error.kind == "invalid_normalization_input"

def test_run_adapter_no_source_with_nodes_contradiction_fails(tmp_path: Path):
    output_dir = tmp_path / "out"
    paper_id = "2310.12345"
    key = "2310.12345"

    raw_tree_rel = f"parsed/{key}/v1/raw_tree.json"
    raw_tree_file = output_dir / raw_tree_rel
    raw_tree_file.parent.mkdir(parents=True, exist_ok=True)

    # raw_tree has nodes, but hierarchy status is no_source!
    raw_tree_payload = {
        "schema_version": PARSING_SCHEMA_VERSION,
        "arxiv_id": paper_id,
        "version": 1,
        "nodes": [
            {"temp_id": "r1", "parent_temp_id": None, "position": 0, "kind": "document", "raw_content": "Root"},
        ],
        "warnings": [],
    }
    raw_tree_file.write_text(json.dumps(raw_tree_payload), encoding="utf-8")

    upstream = {
        "stages": {
            "hierarchy": {
                "stage": "hierarchy",
                "paper_id": paper_id,
                "version": 1,
                "status": "no_source",
                "schema_version": PARSING_SCHEMA_VERSION,
                "artifacts": {"raw_tree": raw_tree_rel},
            }
        }
    }

    result = run({"arxiv_id": paper_id}, output_dir, version={"version": 1}, upstream=upstream)
    assert result.status == "data_error"
    assert result.error.kind == "invalid_normalization_input"

