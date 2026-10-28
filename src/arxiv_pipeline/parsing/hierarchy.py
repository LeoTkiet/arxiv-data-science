"""Hierarchy Construction module for Milestone 2 (TV2).

Complies with docs/CONTRACTS.md and the Milestone 2 specifications:
- Builds hierarchical tree: Document -> Headings -> Paragraphs/Itemize -> Leaves
- Handles dynamic heading ranks: part < chapter < section < subsection < subsubsection < paragraph
- Includes unnumbered Acknowledgements and Appendices (\\section*)
- Excludes References / Bibliography sections completely
- Branches itemize/enumerate into item child nodes with nested support
- Extracts leaf blocks: figure (including table), block_formula ($$, \\[, equation, align), prose_block
"""

from dataclasses import asdict, dataclass
import json
import logging
import os
from pathlib import Path
import re
from typing import Any

from arxiv_pipeline.common.schemas import StageResult, ErrorInfo
from arxiv_pipeline.common.utils import atomic_json, paper_key, utc_now

LOG = logging.getLogger("arxiv_pipeline")


# Tự định nghĩa ParsedNode nội bộ (sẽ chuyển sang schemas.py khi TV1 cập nhật)
@dataclass
class ParsedNode:
    """Represents a node in the hierarchical syntax tree of a paper version."""
    temp_node_id: str
    parent_temp_id: str | None
    kind: str  # "document" | "heading" | "itemize" | "item" | "figure" | "block_formula" | "prose_block"
    level: int
    raw_content: str
    cleaned_content: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Trọng số thứ bậc của các lệnh tiêu đề LaTeX
HEADING_RANKS: dict[str, int] = {
    "part": 1,
    "chapter": 2,
    "section": 3,
    "subsection": 4,
    "subsubsection": 5,
    "paragraph": 6,
    "subparagraph": 7,
}

# Các môi trường hình ảnh và bảng biểu (tất cả được xếp vào kind="figure")
FIGURE_ENVIRONMENTS = {
    "figure", "figure*", "table", "table*", "subfigure",
    "wrapfigure", "wraptable", "sidewaystable", "sidewaysfigure",
    "tabular", "tabular*", "longtable"
}

# Các môi trường khối toán
BLOCK_MATH_ENVIRONMENTS = {
    "equation", "equation*", "align", "align*", "gather", "gather*",
    "multline", "multline*", "eqnarray", "eqnarray*", "flalign", "flalign*"
}

EXCLUDED_HEADING_KEYWORDS = {"references", "bibliography", "works cited", "tài liệu tham khảo"}


def extract_braced_argument(text: str, start_pos: int) -> tuple[str, int]:
    """Extracts a balanced {...} argument starting at or after start_pos.
    
    Returns (argument_content, end_pos_after_closing_brace).
    """
    open_idx = text.find("{", start_pos)
    if open_idx == -1:
        return "", start_pos

    depth = 0
    i = open_idx
    while i < len(text):
        char = text[i]
        if char == "{" and (i == 0 or text[i - 1] != "\\"):
            depth += 1
        elif char == "}" and (i == 0 or text[i - 1] != "\\"):
            depth -= 1
            if depth == 0:
                return text[open_idx + 1:i], i + 1
        i += 1

    return text[open_idx + 1:], len(text)


def find_matching_end_env(text: str, start_pos: int, env_name: str) -> int:
    """Finds the character index immediately after the matching \\end{env_name}."""
    begin_tag = f"\\begin{{{env_name}}}"
    end_tag = f"\\end{{{env_name}}}"
    depth = 1
    pos = start_pos + len(begin_tag)
    while pos <= len(text) and depth > 0:
        next_begin = text.find(begin_tag, pos)
        next_end = text.find(end_tag, pos)
        if next_end == -1:
            return len(text)
        if next_begin != -1 and next_begin < next_end:
            depth += 1
            pos = next_begin + len(begin_tag)
        else:
            depth -= 1
            pos = next_end + len(end_tag)
    return pos


def split_top_level_items(content: str) -> list[str]:
    """Splits an itemize/enumerate body on \\item, ignoring \\item inside nested environments."""
    token_re = re.compile(r"\\begin\{[a-zA-Z*]+\}|\\end\{[a-zA-Z*]+\}|\\item(?:\[[^\]]*\])?")
    items = []
    env_depth = 0
    last_item_start = None

    for m in token_re.finditer(content):
        matched = m.group(0)
        if matched.startswith(r"\begin"):
            env_depth += 1
        elif matched.startswith(r"\end"):
            env_depth = max(0, env_depth - 1)
        elif matched.startswith(r"\item") and env_depth == 0:
            if last_item_start is not None:
                items.append(content[last_item_start:m.start()].strip())
            last_item_start = m.end()

    if last_item_start is not None:
        items.append(content[last_item_start:].strip())

    return items


class HierarchyBuilder:
    """Builds a hierarchical tree from merged LaTeX body text."""

    def __init__(self, version: int):
        self.version = version
        self.node_counter = 0
        self.nodes: list[ParsedNode] = []

    def _next_id(self) -> str:
        self.node_counter += 1
        return f"v{self.version}_n{self.node_counter}"

    def build(self, merged_body_tex: str) -> list[ParsedNode]:
        if not merged_body_tex or not merged_body_tex.strip():
            return []

        # Nút gốc: document
        root_id = f"v{self.version}_root"
        root_node = ParsedNode(
            temp_node_id=root_id,
            parent_temp_id=None,
            kind="document",
            level=0,
            raw_content=f"Version {self.version} root",
        )
        self.nodes.append(root_node)

        # Ngăn xếp phân cấp: list of (rank, node_id, is_excluded)
        stack: list[tuple[int, str, bool]] = [(0, root_id, False)]

        # Regex tìm lệnh tiêu đề: \section*, \subsection, v.v.
        heading_re = re.compile(
            r"\\(part|chapter|section|subsection|subsubsection|paragraph|subparagraph)\*?(?:\[[^\]]*\])?\s*\{"
        )

        pos = 0
        text_len = len(merged_body_tex)

        while pos < text_len:
            match = heading_re.search(merged_body_tex, pos)
            if not match:
                # Không còn tiêu đề nào -> xử lý toàn bộ đoạn văn bản còn lại dưới nút cha hiện tại
                remaining_text = merged_body_tex[pos:].strip()
                if remaining_text:
                    current_parent_id, is_excluded = self._get_current_parent(stack)
                    if not is_excluded:
                        self._parse_body_content(remaining_text, current_parent_id)
                break

            match_start = match.start()
            heading_cmd = match.group(1).lower()
            rank = HEADING_RANKS.get(heading_cmd, 3)

            # Văn bản nằm trước lệnh tiêu đề này thuộc về tiêu đề trước đó
            preceding_text = merged_body_tex[pos:match_start].strip()
            if preceding_text:
                current_parent_id, is_excluded = self._get_current_parent(stack)
                if not is_excluded:
                    self._parse_body_content(preceding_text, current_parent_id)

            # Lấy toàn bộ tiêu đề (xử lý ngoặc nhọn lồng nhau)
            brace_start = match.end() - 1
            title_content, pos = extract_braced_argument(merged_body_tex, brace_start)

            # Kiểm tra xem tiêu đề này có phải là References / Bibliography bị loại trừ không
            clean_title = re.sub(r"\\[a-zA-Z]+|\s+", " ", title_content).strip().lower()
            is_excluded_section = clean_title in EXCLUDED_HEADING_KEYWORDS

            # Lùi stack tìm cha phù hợp (cha phải có rank nhỏ hơn tiêu đề hiện tại)
            while len(stack) > 1 and stack[-1][0] >= rank:
                stack.pop()

            parent_rank, parent_id, parent_is_excluded = stack[-1]

            if not is_excluded_section and not parent_is_excluded:
                new_node_id = self._next_id()
                heading_raw = merged_body_tex[match_start:pos]
                node = ParsedNode(
                    temp_node_id=new_node_id,
                    parent_temp_id=parent_id,
                    kind="heading",
                    level=rank,
                    raw_content=heading_raw.strip(),
                )
                self.nodes.append(node)
                stack.append((rank, new_node_id, False))
            else:
                # Đánh dấu nhánh này bị loại trừ
                stack.append((rank, parent_id, True))

        return self.nodes

    def _get_current_parent(self, stack: list[tuple[int, str, bool]]) -> tuple[str, bool]:
        """Returns (parent_id, is_excluded) for the top of the stack."""
        return stack[-1][1], stack[-1][2]

    def _parse_body_content(self, text: str, parent_id: str) -> None:
        """Parses elements inside a section: figure, table, block math, itemize, and prose blocks."""
        if not text.strip():
            return

        # Các khối đặc biệt: \begin{...}, $$ or \[ (không tính \\[...])
        start_re = re.compile(r"\\begin\{([a-zA-Z*]+)\}|\$\$|(?<!\\)\\\[")

        pos = 0
        text_len = len(text)

        while pos < text_len:
            match = start_re.search(text, pos)
            if not match:
                remaining_prose = text[pos:].strip()
                if remaining_prose:
                    self._emit_prose_blocks(remaining_prose, parent_id)
                break

            match_start = match.start()
            preceding_prose = text[pos:match_start].strip()
            if preceding_prose:
                self._emit_prose_blocks(preceding_prose, parent_id)

            token = match.group(0)
            if token.startswith(r"\begin"):
                env_name = match.group(1).lower()
                end_pos = find_matching_end_env(text, match_start, match.group(1))
                matched_str = text[match_start:end_pos]
                pos = end_pos

                if env_name in FIGURE_ENVIRONMENTS:
                    self.nodes.append(ParsedNode(
                        temp_node_id=self._next_id(),
                        parent_temp_id=parent_id,
                        kind="figure",
                        level=-1,
                        raw_content=matched_str.strip(),
                    ))
                elif env_name in BLOCK_MATH_ENVIRONMENTS:
                    self.nodes.append(ParsedNode(
                        temp_node_id=self._next_id(),
                        parent_temp_id=parent_id,
                        kind="block_formula",
                        level=-1,
                        raw_content=matched_str.strip(),
                    ))
                elif env_name in {"itemize", "enumerate", "description"}:
                    self._parse_list_environment(matched_str, match.group(1), parent_id)
                elif env_name in {"center", "flushleft", "flushright", "minipage"}:
                    # Môi trường bố cục trong suốt: bóc vỏ và tiếp tục phân tích nội dung bên trong
                    inner_start = match.end()
                    close_tag = f"\\end{{{match.group(1)}}}"
                    inner_end = end_pos - len(close_tag)
                    self._parse_body_content(text[inner_start:inner_end], parent_id)
                elif env_name in {"lstlisting", "verbatim", "minted"}:
                    # Khối mã nguồn / verbatim nguyên khối: không cắt vụn theo dòng trống
                    self.nodes.append(ParsedNode(
                        temp_node_id=self._next_id(),
                        parent_temp_id=parent_id,
                        kind="prose_block",
                        level=-1,
                        raw_content=matched_str.strip(),
                    ))
                else:
                    self._emit_prose_blocks(matched_str, parent_id)

            elif token == "$$":
                end_idx = text.find("$$", match.end())
                if end_idx != -1:
                    matched_str = text[match_start:end_idx + 2]
                    pos = end_idx + 2
                else:
                    matched_str = text[match_start:]
                    pos = text_len
                self.nodes.append(ParsedNode(
                    temp_node_id=self._next_id(),
                    parent_temp_id=parent_id,
                    kind="block_formula",
                    level=-1,
                    raw_content=matched_str.strip(),
                ))
            elif token == r"\[":
                end_idx = text.find(r"\]", match.end())
                if end_idx != -1:
                    matched_str = text[match_start:end_idx + 2]
                    pos = end_idx + 2
                else:
                    matched_str = text[match_start:]
                    pos = text_len
                self.nodes.append(ParsedNode(
                    temp_node_id=self._next_id(),
                    parent_temp_id=parent_id,
                    kind="block_formula",
                    level=-1,
                    raw_content=matched_str.strip(),
                ))

    def _parse_list_environment(self, list_env_text: str, env_tag: str, parent_id: str) -> None:
        """Parses an itemize/enumerate block into an itemize node and item children."""
        itemize_id = self._next_id()
        self.nodes.append(ParsedNode(
            temp_node_id=itemize_id,
            parent_temp_id=parent_id,
            kind="itemize",
            level=-1,
            raw_content="",
        ))

        begin_str = f"\\begin{{{env_tag}}}"
        end_str = f"\\end{{{env_tag}}}"
        content = list_env_text.strip()
        if content.startswith(begin_str) and content.endswith(end_str):
            content = content[len(begin_str):-len(end_str)]

        items = split_top_level_items(content)
        for item_text in items:
            if not item_text.strip():
                continue
            item_id = self._next_id()
            self.nodes.append(ParsedNode(
                temp_node_id=item_id,
                parent_temp_id=itemize_id,
                kind="item",
                level=-1,
                raw_content="",
            ))
            # Nội dung bên trong item (có thể chứa văn bản, công thức toán hoặc itemize con)
            self._parse_body_content(item_text, item_id)

    def _emit_prose_blocks(self, text: str, parent_id: str) -> None:
        """Splits prose text into paragraph blocks by empty lines or \\par."""
        paragraphs = re.split(r"\n\s*\n|\\par\b", text)
        for p in paragraphs:
            cleaned = p.strip()
            if cleaned:
                self.nodes.append(ParsedNode(
                    temp_node_id=self._next_id(),
                    parent_temp_id=parent_id,
                    kind="prose_block",
                    level=-1,
                    raw_content=cleaned,
                ))


def build_hierarchy_tree(version: int, merged_body_tex: str) -> list[ParsedNode]:
    """Helper entrypoint to build hierarchy tree from merged LaTeX body string."""
    builder = HierarchyBuilder(version)
    return builder.build(merged_body_tex)


def run(record: dict, output_dir: Path, *, version: dict | None = None, upstream: dict | None = None) -> StageResult:
    """Adapter entrypoint adhering to docs/CONTRACTS.md for stage 'hierarchy'."""
    output_dir = Path(output_dir)
    arxiv_id = record["arxiv_id"]
    if not version:
        raise ValueError("Hierarchy stage requires a version record")

    v_num = version["version"]
    key = paper_key(arxiv_id)

    dest_dir = output_dir / "parsed" / key / f"v{v_num}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    tree_artifact_rel = f"parsed/{key}/v{v_num}/raw_tree.json"
    tree_path = output_dir / tree_artifact_rel

    # Kiểm tra nếu upstream là PDF-only hoặc no_source
    if upstream and upstream.get("status") == "no_source":
        atomic_json(tree_path, [])
        return StageResult(
            stage="hierarchy",
            paper_id=arxiv_id,
            version=v_num,
            status="no_source",
            timestamp=utc_now(),
            artifacts={"raw_tree": tree_artifact_rel},
            attempts=1,
            size_bytes=0,
        )

    # Đọc dữ liệu từ Trạm 1 (gathered.json)
    gathered_data = None
    gathered_rel = upstream.get("artifacts", {}).get("gathered") if upstream else None
    if gathered_rel and (output_dir / gathered_rel).exists():
        gathered_path = output_dir / gathered_rel
    else:
        gathered_path = dest_dir / "gathered.json"

    if gathered_path.exists():
        try:
            gathered_data = json.loads(gathered_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            gathered_data = None

    if gathered_data and gathered_data.get("is_empty_source"):
        atomic_json(tree_path, [])
        return StageResult(
            stage="hierarchy",
            paper_id=arxiv_id,
            version=v_num,
            status="no_source",
            timestamp=utc_now(),
            artifacts={"raw_tree": tree_artifact_rel},
            attempts=1,
            size_bytes=0,
        )

    merged_body_tex = gathered_data.get("merged_body_tex", "") if gathered_data else ""

    # Dựng cây phân cấp
    nodes = build_hierarchy_tree(v_num, merged_body_tex)
    serialized_nodes = [node.to_dict() for node in nodes]
    atomic_json(tree_path, serialized_nodes)

    file_size = tree_path.stat().st_size if tree_path.exists() else 0

    return StageResult(
        stage="hierarchy",
        paper_id=arxiv_id,
        version=v_num,
        status="success",
        timestamp=utc_now(),
        artifacts={"raw_tree": tree_artifact_rel},
        attempts=1,
        size_bytes=file_size,
    )
