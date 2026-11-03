"""Normalization module for Milestone 2 (TV3).

Complies with docs/CONTRACTS.md, docs/MS2_CONTRACTS.md, and the Milestone 2 specifications:
- Receives hierarchical syntax tree (raw_tree.json) from TV2 (Hierarchy).
- LaTeX Cleanup: Strips purely presentational formatting commands (\\centering, [htpb],
  \\midrule, spacing, font sizes, text styles) while PRESERVING semantic citations
  (\\cite, \\citep, \\citet), references (\\ref, \\eqref), labels, and math expressions.
- Math Normalization: Normalizes inline math to $...$ and block math to \\begin{equation}...\\end{equation}.
- Sentence Segmentation: Safely splits prose_block nodes into individual sentence leaves (kind="sentence")
  using protected token masking (protecting math, citations, URLs, numbers, and academic abbreviations).
- Re-indexes position strictly from 0 under each parent and outputs normalized_tree.json in Preorder.
"""

from dataclasses import asdict
import json
import logging
from pathlib import Path, PureWindowsPath
import re
from typing import Any

from arxiv_pipeline.common.schemas import ErrorInfo, PARSING_SCHEMA_VERSION, StageResult
from arxiv_pipeline.common.utils import atomic_json, paper_key, utc_now

LOG = logging.getLogger("arxiv_pipeline")

STAGE = "normalize"


def _safe_artifact_path(root: Path, relative: str) -> Path:
    """Resolve a relative path under root safely without escaping."""
    if (not isinstance(relative, str) or not relative or "\\" in relative
            or Path(relative).is_absolute() or PureWindowsPath(relative).drive
            or PureWindowsPath(relative).root or ".." in Path(relative).parts):
        raise ValueError(f"Unsafe artifact path: {relative!r}")
    base = Path(root).resolve()
    path = (base / relative).resolve()
    if not path.is_relative_to(base):
        raise ValueError(f"Artifact path escapes output root: {relative!r}")
    return path


# -----------------------------------------------------------------------------
# 1. Math Normalization
# -----------------------------------------------------------------------------

_INLINE_MATH_PAREN = re.compile(r"\\\((.*?)\\\)", re.DOTALL)
_INLINE_MATH_ENV = re.compile(r"\\begin\{math\}(.*?)\\end\{math\}", re.DOTALL)


def normalize_inline_math(text: str) -> str:
    """Normalize inline math expressions \\(...\\) and \\begin{math}...\\end{math} to $...$."""
    def _to_dollar(m: re.Match) -> str:
        inner = m.group(1).strip()
        return f"${inner}$"

    text = _INLINE_MATH_PAREN.sub(_to_dollar, text)
    text = _INLINE_MATH_ENV.sub(_to_dollar, text)

    # Normalize whitespace inside $...$: e.g. "$  x + y  $" -> "$x + y$"
    def _clean_dollar_spaces(m: re.Match) -> str:
        inner = m.group(1).strip()
        # Collapse multi-spaces inside math
        inner = re.sub(r"\s+", " ", inner)
        return f"${inner}$"

    text = re.compile(r"(?<!\\)\$(?!\$)(.*?)(?<!\\)\$", re.DOTALL).sub(_clean_dollar_spaces, text)
    return text


def normalize_block_math(raw_content: str) -> str:
    """Normalize any block math expression to standard \\begin{equation}...\\end{equation}."""
    content = raw_content.strip()

    # Case 1: $$ ... $$
    if content.startswith("$$") and content.endswith("$$") and len(content) >= 4:
        inner = content[2:-2].strip()
    # Case 2: \[ ... \]
    elif content.startswith(r"\[") and content.endswith(r"\]") and len(content) >= 4:
        inner = content[2:-2].strip()
    # Case 3: \begin{env} ... \end{env}
    else:
        env_match = re.match(r"^\\begin\{([a-zA-Z*]+)\}(.*?)\\end\{\1\}$", content, re.DOTALL)
        if env_match:
            inner = env_match.group(2).strip()
        else:
            inner = content

    # Clean redundant whitespace inside equation
    lines = [line.strip() for line in inner.splitlines() if line.strip()]
    cleaned_inner = "\n".join(lines) if len(lines) > 1 else (lines[0] if lines else "")

    return f"\\begin{{equation}}\n{cleaned_inner}\n\\end{{equation}}"


# -----------------------------------------------------------------------------
# 2. LaTeX Cleanup
# -----------------------------------------------------------------------------

# Placement specifiers on tables/figures: [htpb], [!htb], [H], etc.
_PLACEMENT_RE = re.compile(r"\\begin\{(?:figure|table)\*?\}\s*\[[a-zA-Z!]+\]", re.I)

# Layout & spacing commands to eliminate
_LAYOUT_COMMANDS_RE = re.compile(
    r"\\(?:centering|raggedright|raggedleft|noindent|indent|newpage|clearpage|"
    r"smallskip|medskip|bigskip|vfill|hfill)\b"
)

# Explicit dimension spacing: \vspace*{...}, \hspace*{...}, \setlength{...}{...}
_SPACING_ARGS_RE = re.compile(r"\\(?:vspace|hspace)\*?\s*\{[^{}]*\}|\\setlength\s*\{[^{}]*\}\s*\{[^{}]*\}")

# Table rule decorations
_TABLE_RULES_RE = re.compile(
    r"\\(?:toprule|midrule|bottomrule|hline)\b|\\(?:cline|cmidrule)\s*(?:\([^)]*\))?\s*\{[^{}]*\}"
)

# Font size modifiers
_FONT_SIZES_RE = re.compile(
    r"\\(?:tiny|scriptsize|footnotesize|small|normalsize|large|Large|LARGE|huge|Huge)\b"
)

# Text formatting wrappers: \textbf{...}, \textit{...}, etc.
_TEXT_STYLE_NAMES = (
    "textbf", "textit", "emph", "underline", "textsf", "texttt",
    "textsc", "textmd", "textup", "textsl"
)
_TEXT_STYLE_PATTERN = re.compile(r"\\(" + "|".join(_TEXT_STYLE_NAMES) + r")\s*\{")


def _strip_text_styles(text: str) -> str:
    """Unwrap \\textbf{content} -> content, supporting nested styles."""
    while True:
        match = _TEXT_STYLE_PATTERN.search(text)
        if not match:
            break
        start_idx = match.start()
        brace_start = match.end() - 1

        # Find matching closing brace
        depth = 1
        pos = brace_start + 1
        text_len = len(text)
        while pos < text_len and depth > 0:
            char = text[pos]
            if char == "{" and (pos == 0 or text[pos - 1] != "\\"):
                depth += 1
            elif char == "}" and (pos == 0 or text[pos - 1] != "\\"):
                depth -= 1
            pos += 1

        if depth == 0:
            inner_content = text[brace_start + 1:pos - 1]
            text = text[:start_idx] + inner_content + text[pos:]
        else:
            # Unbalanced brace; leave as is to prevent infinite loop
            break
    return text


def clean_latex_formatting(text: str) -> str:
    """Clean presentational LaTeX commands while strictly preserving citations and math."""
    if not text:
        return ""

    # 1. Normalize inline math first (\(...) -> $...$)
    text = normalize_inline_math(text)

    # 2. IMMEDIATELY mask all math expressions before ANY text-level replacements or cleanup!
    math_tokens: list[str] = []

    def _mask_math(m: re.Match) -> str:
        math_tokens.append(m.group(0))
        return f"__MATH_{len(math_tokens) - 1}__"

    # Mask display math if present ($$...$$ or \[...\])
    text = re.sub(r"\$\$.*?\$\$", _mask_math, text, flags=re.DOTALL)
    text = re.sub(r"\\\[.*?\\\]", _mask_math, text, flags=re.DOTALL)
    # Mask inline math ($...$)
    text = re.sub(r"(?<!\\)\$(?!\$).*?(?<!\\)\$", _mask_math, text, flags=re.DOTALL)

    # 3. Remove placement options like \begin{figure}[htpb] -> \begin{figure}
    text = _PLACEMENT_RE.sub(lambda m: m.group(0).split("[")[0], text)

    # 4. Remove layout commands
    text = _LAYOUT_COMMANDS_RE.sub(" ", text)

    # 5. Remove spacing commands with arguments
    text = _SPACING_ARGS_RE.sub(" ", text)

    # 6. Remove table rules
    text = _TABLE_RULES_RE.sub(" ", text)

    # 7. Remove font size declarations
    text = _FONT_SIZES_RE.sub(" ", text)

    # 8. Unwrap text styles (\textbf{text} -> text)
    text = _strip_text_styles(text)

    # 9. Replace LaTeX quotation styles outside math (safe: math is masked, f''(x) is protected)
    text = re.sub(r"``|''", '"', text)
    text = re.sub(r"`|'", "'", text)

    # 10. Replace non-breaking space (tie) with normal space
    text = re.sub(r"(?<!\\)~", " ", text)

    # 11. Unescape common escaped symbols outside math
    text = text.replace(r"\%", "%")
    text = text.replace(r"\&", "&")
    text = text.replace(r"\_", "_")
    text = text.replace(r"\#", "#")
    text = text.replace(r"\{", "{")
    text = text.replace(r"\}", "}")

    # 12. Restore math tokens
    for i, token in enumerate(math_tokens):
        text = text.replace(f"__MATH_{i}__", token)

    # 13. Collapse multiple whitespaces and newlines
    text = re.sub(r"\s+", " ", text).strip()
    return text


# -----------------------------------------------------------------------------
# 3. Sentence Segmentation with Token Masking
# -----------------------------------------------------------------------------

# Academic and reference abbreviations
_ACADEMIC_ABBREVS = (
    r"i\.e\.", r"e\.g\.", r"et al\.", r"cf\.", r"vs\.", r"etc\.", r"approx\.",
    r"Fig\.", r"Figs\.", r"Tab\.", r"Tabs\.", r"Eq\.", r"Eqs\.", r"Sec\.", r"Secs\.",
    r"Alg\.", r"Thm\.", r"Lem\.", r"Cor\.", r"Def\.", r"Prop\.", r"Ref\.", r"Refs\.",
    r"Dr\.", r"Prof\.", r"Mr\.", r"Mrs\.", r"Ms\."
)
_ABBREV_RE = re.compile(r"\b(" + "|".join(_ACADEMIC_ABBREVS) + r")", re.I)

# Counter words that often have letter/number identifiers like "Sentence A.", "Theorem 1."
_COUNTER_WORDS = {
    "sentence", "theorem", "lemma", "proposition", "corollary", "definition",
    "case", "step", "phase", "option", "part", "appendix", "table", "figure",
    "algorithm", "method", "model", "class", "group", "equation", "section"
}
_INITIALS_PATTERN = re.compile(r"(?:(\b[A-Za-z]+)\s+)?\b([A-Z])\.\s+(?=[A-Z][a-z]{2,}|\b[A-Z]\.)")

# Decimals like "3.14" or version "v1.2"
_DECIMALS_RE = re.compile(r"(?<=\d)\.(?=\d)")


def split_into_sentences(text: str) -> list[str]:
    """Split prose text into sentence leaves using protected token masking."""
    cleaned = text.strip()
    if not cleaned:
        return []

    protected_tokens: list[str] = []

    def _protect(content: str) -> str:
        idx = len(protected_tokens)
        protected_tokens.append(content)
        return f"__PROTECTED_{idx}__"

    # Step 1: Protect inline math expressions $...$
    def _mask_math(m: re.Match) -> str:
        return _protect(m.group(0))

    masked_text = re.sub(r"(?<!\\)\$(?:\\\$|[^\$])+(?<!\\)\$", _mask_math, cleaned)

    # Protect LaTeX commands with arguments (\cite{...}, \ref{...}, \url{...}, etc.)
    _PROTECT_COMMANDS = re.compile(
        r"\\(?:cite|citep|citet|citealp|citealt|citeauthor|citeyear|citeyearpar|citenum|nocite|"
        r"parencite|textcite|autocite|footcite|smartcite|ref|eqref|label|url|href|includegraphics)"
        r"\*?(?:\s*\[[^\]]*\])*\s*\{[^{}]*\}"
    )

    def _mask_command(m: re.Match) -> str:
        return _protect(m.group(0))

    masked_text = _PROTECT_COMMANDS.sub(_mask_command, masked_text)

    # Protect URLs
    masked_text = re.sub(r"https?://\S+", lambda m: _protect(m.group(0)), masked_text)

    # Step 2: Protect dots in decimals and abbreviations
    # Decimal numbers: 3.14 -> 3<DOT>14
    masked_text = _DECIMALS_RE.sub("<DOT>", masked_text)

    # Academic abbreviations: e.g. -> e<DOT>g<DOT>
    def _mask_abbrev(m: re.Match) -> str:
        return m.group(0).replace(".", "<DOT>")

    masked_text = _ABBREV_RE.sub(_mask_abbrev, masked_text)

    # Initials: A. Smith -> A<DOT> Smith, J. K. Rowling -> J<DOT> K<DOT> Rowling
    def _mask_initial(m: re.Match) -> str:
        prev_word = m.group(1)
        if prev_word and prev_word.lower() in _COUNTER_WORDS:
            return m.group(0)
        prefix = f"{prev_word} " if prev_word else ""
        return f"{prefix}{m.group(2)}<DOT> "

    masked_text = _INITIALS_PATTERN.sub(_mask_initial, masked_text)

    # Step 3: Split into sentences at genuine sentence-ending punctuation (. ! ?)
    # Sentence boundary: [.!?] followed by whitespace and a starting character
    sentence_boundary = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[\$]|__PROTECTED_)")
    raw_sentences = sentence_boundary.split(masked_text)

    results: list[str] = []
    for s in raw_sentences:
        # Unmask abbreviation dots
        s = s.replace("<DOT>", ".")
        # Unmask protected tokens
        for i, token in enumerate(protected_tokens):
            s = s.replace(f"__PROTECTED_{i}__", token)
        s = s.strip()
        if s:
            results.append(s)

    return results


# -----------------------------------------------------------------------------
# 4. Tree Transformation
# -----------------------------------------------------------------------------

def transform_tree_to_normalized(raw_nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Transform raw_tree nodes into normalized_tree nodes.

    - prose_block nodes are replaced by sentence leaves under the same parent.
    - All non-prose nodes have raw_content converted to cleaned_content.
    - Positions of siblings under each parent are re-indexed strictly 0, 1, 2, ...
    - Returned nodes are strictly in Preorder traversal.
    """
    if not raw_nodes:
        return []

    # Map raw nodes by temp_id and track child order
    children_by_parent: dict[str | None, list[dict[str, Any]]] = {}
    node_by_id: dict[str, dict[str, Any]] = {}
    root_node: dict[str, Any] | None = None

    for node in raw_nodes:
        temp_id = node.get("temp_id")
        parent_id = node.get("parent_temp_id")
        node_by_id[temp_id] = node
        children_by_parent.setdefault(parent_id, []).append(node)
        if parent_id is None:
            root_node = node

    if root_node is None:
        raise ValueError("raw_tree does not contain a document root")

    roots = children_by_parent.get(None, [])
    if len(roots) != 1:
        raise ValueError("raw_tree must contain exactly one document root")

    existing_ids: set[str] = {n.get("temp_id") for n in raw_nodes if n.get("temp_id")}
    sentence_counter = 1
    normalized_children: dict[str | None, list[dict[str, Any]]] = {}

    for parent_id, children in children_by_parent.items():
        # Keep original sequence sorted by position
        sorted_children = sorted(children, key=lambda n: n.get("position", 0))
        new_children: list[dict[str, Any]] = []

        for ch in sorted_children:
            kind = ch.get("kind")
            raw_content = ch.get("raw_content", "")

            if kind == "prose_block":
                # Clean prose text and split into sentences
                cleaned_text = clean_latex_formatting(raw_content)
                sentences = split_into_sentences(cleaned_text)
                for s in sentences:
                    while f"s{sentence_counter}" in existing_ids:
                        sentence_counter += 1
                    s_id = f"s{sentence_counter}"
                    existing_ids.add(s_id)
                    new_children.append({
                        "temp_id": s_id,
                        "parent_temp_id": parent_id,
                        "position": 0,  # Will be re-indexed below
                        "kind": "sentence",
                        "cleaned_content": s,
                    })
                    sentence_counter += 1
            else:
                # Other nodes keep kind, but map raw_content -> cleaned_content
                if kind == "document":
                    cleaned_content = raw_content or "Document root"
                elif kind == "block_formula":
                    cleaned_content = normalize_block_math(raw_content)
                elif kind in {"itemize", "item"}:
                    cleaned_content = clean_latex_formatting(raw_content)
                else:
                    cleaned_content = clean_latex_formatting(raw_content)

                node_dict = {
                    "temp_id": ch["temp_id"],
                    "parent_temp_id": parent_id,
                    "position": 0,
                    "kind": kind,
                    "cleaned_content": cleaned_content,
                }
                # Preserve optional levels/ranks if present
                if "level" in ch and ch["level"] is not None:
                    node_dict["level"] = ch["level"]
                if "heading_rank" in ch and ch["heading_rank"] is not None:
                    node_dict["heading_rank"] = ch["heading_rank"]

                new_children.append(node_dict)

        # Re-index position strictly from 0 to len-1
        for idx, child in enumerate(new_children):
            child["position"] = idx

        normalized_children[parent_id] = new_children

    # Preorder traversal from root
    preorder_nodes: list[dict[str, Any]] = []
    root_dict = normalized_children[None][0]
    visited: set[str] = set()

    def _traverse(node: dict[str, Any]) -> None:
        t_id = node["temp_id"]
        if t_id in visited:
            raise ValueError(f"Cycle detected at node {t_id!r}")
        visited.add(t_id)
        preorder_nodes.append(node)
        for child in normalized_children.get(t_id, []):
            _traverse(child)

    _traverse(root_dict)

    total_expected = sum(len(c) for c in normalized_children.values())
    if len(preorder_nodes) != total_expected:
        raise ValueError("raw_tree contains unreachable nodes or invalid parents")

    return preorder_nodes


# -----------------------------------------------------------------------------
# 5. Adapter run() Interface
# -----------------------------------------------------------------------------

def run(record: dict, output_dir: Path, *, version: dict | None = None,
        upstream: dict | None = None) -> StageResult:
    """Normalize a version's hierarchical syntax tree into normalized_tree.json."""
    paper_id = record.get("arxiv_id", "") if isinstance(record, dict) else ""
    number = version.get("version") if isinstance(version, dict) else None

    def result(status: str, artifacts: dict[str, str] | None = None,
               error: ErrorInfo | None = None, size: int = 0) -> StageResult:
        return StageResult(
            stage=STAGE,
            paper_id=paper_id,
            version=number,
            status=status,
            timestamp=utc_now(),
            artifacts=artifacts or {},
            error=error,
            attempts=1,
            size_bytes=size,
            schema_version=PARSING_SCHEMA_VERSION,
        )

    try:
        key = paper_key(paper_id)
        if type(number) is not int or number < 1:
            raise ValueError("Normalization requires a positive integer version")

        output = Path(output_dir).resolve()

        if not isinstance(upstream, dict):
            raise ValueError("Normalization requires an upstream envelope")

        # Resolve upstream hierarchy stage result
        stages = upstream.get("stages")
        if stages is not None:
            if not isinstance(stages, dict):
                raise ValueError("Missing stages envelope")
            hierarchy_res = stages.get("hierarchy")
        elif upstream.get("stage") == "hierarchy":
            hierarchy_res = upstream
        else:
            hierarchy_res = upstream.get("hierarchy")

        if hierarchy_res is None:
            return result("skipped", error=ErrorInfo("upstream_unavailable", "hierarchy checkpoint is missing"))

        if not isinstance(hierarchy_res, dict) or (
                hierarchy_res.get("stage"), hierarchy_res.get("paper_id"), hierarchy_res.get("version"),
                hierarchy_res.get("schema_version")) != ("hierarchy", paper_id, number, PARSING_SCHEMA_VERSION):
            raise ValueError("Hierarchy stage identity/schema does not match requested paper/version")

        if type(hierarchy_res.get("version")) is not int:
            raise ValueError("Hierarchy version must be an integer")

        status = hierarchy_res.get("status")
        if status not in {"success", "no_source"}:
            return result("skipped", error=ErrorInfo("upstream_unavailable", f"hierarchy status: {status}"))

        artifacts = hierarchy_res.get("artifacts")
        if not isinstance(artifacts, dict) or "raw_tree" not in artifacts:
            raise ValueError("Hierarchy result has no raw_tree artifact")

        for relative in artifacts.values():
            _safe_artifact_path(output, relative)

        raw_tree_path = _safe_artifact_path(output, artifacts["raw_tree"])
        if not raw_tree_path.is_file():
            raise ValueError("Hierarchy raw_tree artifact does not exist on disk")

        raw_tree_payload = json.loads(raw_tree_path.read_text(encoding="utf-8"))
        if not isinstance(raw_tree_payload, dict):
            raise ValueError("Invalid raw_tree JSON payload")

        if (raw_tree_payload.get("schema_version"), raw_tree_payload.get("arxiv_id"),
                raw_tree_payload.get("version")) != (PARSING_SCHEMA_VERSION, paper_id, number):
            raise ValueError("raw_tree header identity mismatch")

        if type(raw_tree_payload.get("version")) is not int:
            raise ValueError("raw_tree version must be an integer")

        if "nodes" not in raw_tree_payload or not isinstance(raw_tree_payload.get("nodes"), list):
            raise ValueError("raw_tree payload missing required 'nodes' list")

        raw_nodes = raw_tree_payload["nodes"]

        warnings = raw_tree_payload.get("warnings", [])
        if not isinstance(warnings, list) or not all(isinstance(w, dict) for w in warnings):
            raise ValueError("raw_tree warnings must be a list of objects")
        warnings = list(warnings)

        # Handle empty/no_source version vs success
        if status == "no_source":
            if raw_nodes:
                raise ValueError("Hierarchy status contradicts raw_tree: status is 'no_source' but raw_tree contains nodes")
            if not any(w.get("code") == "no_tex_source" for w in warnings):
                warnings.append(dict(code="no_tex_source", message="No TeX source available.", source_path=None))
            normalized_nodes: list[dict[str, Any]] = []
        else:
            if not raw_nodes and not any(w.get("code") == "empty_body" for w in warnings):
                warnings.append(dict(code="empty_body", message="TeX source has no body content.", source_path=None))
            normalized_nodes = transform_tree_to_normalized(raw_nodes)

        relative = f"parsed/{key}/v{number}/normalized_tree.json"
        tree_path = _safe_artifact_path(output, relative)

        payload = {
            "schema_version": PARSING_SCHEMA_VERSION,
            "arxiv_id": paper_id,
            "version": number,
            "nodes": normalized_nodes,
            "warnings": warnings,
        }
        atomic_json(tree_path, payload)

        return result(status, {"normalized_tree": relative}, size=tree_path.stat().st_size)

    except (ValueError, TypeError, KeyError) as exc:
        return result("data_error", error=ErrorInfo("invalid_normalization_input", str(exc)))
    except OSError as exc:
        return result("error", error=ErrorInfo("io_error", str(exc)))
