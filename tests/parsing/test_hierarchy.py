"""Unit tests for TV2 Hierarchy Construction (tests/parsing/test_hierarchy.py).

All tests run completely offline with synthetic LaTeX samples in < 0.2s.
"""

import json
from pathlib import Path
import pytest

from arxiv_pipeline.parsing.hierarchy import (
    build_hierarchy_tree,
    extract_braced_argument,
    run,
)
from arxiv_pipeline.common.schemas import StageResult


def test_extract_braced_argument_nested():
    """Verify extracting braced arguments with nested braces and math."""
    text = r"\section{Analysis of \textbf{Deep} and $\mathcal{O}(n^2)$ Models} Following text."
    title, end_pos = extract_braced_argument(text, text.find("{"))
    assert title == r"Analysis of \textbf{Deep} and $\mathcal{O}(n^2)$ Models"
    assert text[end_pos:].strip() == "Following text."


def test_standard_hierarchy_tree():
    """Verify standard document -> section -> subsection -> prose hierarchy."""
    tex = r"""
    \section{Introduction}
    This is the first introductory paragraph.
    
    This is the second paragraph.
    \subsection{Background}
    Background text explaining the context.
    """
    nodes = build_hierarchy_tree(version=1, merged_body_tex=tex)

    assert len(nodes) >= 5
    assert nodes[0].kind == "document"
    assert nodes[0].level == 0
    assert nodes[0].parent_temp_id is None

    sec = next(n for n in nodes if n.kind == "heading" and "Introduction" in n.raw_content)
    assert sec.parent_temp_id == nodes[0].temp_node_id
    assert sec.level == 3

    subsec = next(n for n in nodes if n.kind == "heading" and "Background" in n.raw_content)
    assert subsec.parent_temp_id == sec.temp_node_id
    assert subsec.level == 4

    bg_prose = next(n for n in nodes if n.kind == "prose_block" and "Background text" in n.raw_content)
    assert bg_prose.parent_temp_id == subsec.temp_node_id


def test_dynamic_heading_jumping():
    """Verify stack handling when ranks jump (e.g. section -> subsubsection -> section)."""
    tex = r"""
    \section{First Section}
    Text 1.
    \subsubsection{Jumped Deep Heading}
    Deep text.
    \section{Second Section}
    Text 2.
    """
    nodes = build_hierarchy_tree(version=1, merged_body_tex=tex)

    sec1 = next(n for n in nodes if "First Section" in n.raw_content)
    subsub = next(n for n in nodes if "Jumped Deep Heading" in n.raw_content)
    sec2 = next(n for n in nodes if "Second Section" in n.raw_content)

    assert subsub.parent_temp_id == sec1.temp_node_id
    assert subsub.level == 5

    assert sec2.parent_temp_id == nodes[0].temp_node_id
    assert sec2.level == 3


def test_inclusions_and_exclusions():
    """Verify that Acknowledgements/Appendices are kept and References are dropped."""
    tex = r"""
    \section{Main Content}
    Normal body.
    \section*{Acknowledgements}
    We thank the reviewers.
    \section*{Appendix A: Proofs}
    Mathematical proofs here.
    \section*{References}
    \bibitem{dummy} Should be dropped!
    """
    nodes = build_hierarchy_tree(version=1, merged_body_tex=tex)

    assert any(n.kind == "heading" and "Acknowledgements" in n.raw_content for n in nodes)
    assert any(n.kind == "heading" and "Appendix A" in n.raw_content for n in nodes)
    assert any(n.kind == "prose_block" and "We thank the reviewers" in n.raw_content for n in nodes)

    assert not any("References" in n.raw_content for n in nodes)
    assert not any("Should be dropped" in n.raw_content for n in nodes)


def test_nested_itemize_environment():
    """Verify that itemize creates itemize -> item -> nested itemize -> item branches."""
    tex = r"""
    \section{Lists}
    \begin{itemize}
        \item First main point.
        \item Second main point with sublist:
        \begin{enumerate}
            \item Sub-point A.
        \end{enumerate}
    \end{itemize}
    """
    nodes = build_hierarchy_tree(version=1, merged_body_tex=tex)

    itemizes = [n for n in nodes if n.kind == "itemize"]
    items = [n for n in nodes if n.kind == "item"]

    assert len(itemizes) == 2
    assert len(items) == 3

    second_item = items[1]
    inner_itemize = itemizes[1]
    assert inner_itemize.parent_temp_id == second_item.temp_node_id


def test_figure_and_table_as_figure():
    """Verify that both table and figure environments produce kind='figure' leaves."""
    tex = r"""
    \section{Visuals}
    \begin{figure}[htpb]
        \centering
        \includegraphics{plot.png}
        \caption{Sample figure}
    \end{figure}
    
    \begin{table*}
        \centering
        \begin{tabular}{cc} 1 & 2 \end{tabular}
        \caption{Sample table}
    \end{table*}
    """
    nodes = build_hierarchy_tree(version=1, merged_body_tex=tex)

    fig_nodes = [n for n in nodes if n.kind == "figure"]
    assert len(fig_nodes) == 2
    assert "plot.png" in fig_nodes[0].raw_content
    assert "tabular" in fig_nodes[1].raw_content


def test_block_formulas():
    """Verify that block formulas ($$, \\[, equation, align) become kind='block_formula'."""
    tex = r"""
    \section{Math}
    Standard inline $x = 1$ in prose.
    
    $$ E = mc^2 $$
    
    \[ \int_0^1 f(x) dx \]
    
    \begin{align*}
        a &= b + c \\
        d &= e
    \end{align*}
    """
    nodes = build_hierarchy_tree(version=1, merged_body_tex=tex)

    formulas = [n for n in nodes if n.kind == "block_formula"]
    assert len(formulas) == 3
    assert any("mc^2" in f.raw_content for f in formulas)
    assert any("int_0^1" in f.raw_content for f in formulas)
    assert any("align*" in f.raw_content for f in formulas)


def test_adapter_run_interface(tmp_path: Path):
    """Verify run() adapter writes raw_tree.json and returns valid StageResult."""
    record = {"arxiv_id": "2301.00001"}
    version = {"version": 1}

    # Giả lập artifact gathered.json
    gathered_rel = "parsed/2301-00001/v1/gathered.json"
    gathered_path = tmp_path / gathered_rel
    gathered_path.parent.mkdir(parents=True, exist_ok=True)
    gathered_path.write_text(
        json.dumps({
            "arxiv_id": "2301.00001",
            "version": 1,
            "is_empty_source": False,
            "merged_body_tex": r"\section{Intro} Hello world.",
        }),
        encoding="utf-8"
    )

    upstream = {
        "status": "success",
        "artifacts": {"gathered": gathered_rel}
    }

    stage_result = run(record, tmp_path, version=version, upstream=upstream)

    assert isinstance(stage_result, StageResult)
    assert stage_result.stage == "hierarchy"
    assert stage_result.paper_id == "2301.00001"
    assert stage_result.version == 1
    assert stage_result.status == "success"
    assert "raw_tree" in stage_result.artifacts

    tree_file = tmp_path / stage_result.artifacts["raw_tree"]
    assert tree_file.exists()
    data = json.loads(tree_file.read_text(encoding="utf-8"))
    assert len(data) >= 3  # root + heading + prose