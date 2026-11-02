"""Hierarchy Construction module for Milestone 2 (TV2).

Complies with docs/CONTRACTS.md, docs/MS2_CONTRACTS.md, and the Milestone 2 specifications:
- Builds hierarchical tree: Document -> Headings -> Paragraphs/Itemize -> Leaves
- Handles dynamic heading ranks: part < chapter < section < subsection < subsubsection < paragraph
- Includes unnumbered Acknowledgements and Appendices (\\section*)
- Excludes References / Bibliography sections completely
- Branches itemize/enumerate into item child nodes with nested support
- Extracts leaf blocks: figure (including table), block_formula ($$, \\[, equation, align), prose_block
- Adheres to ParsedNode dataclass (temp_id, parent_temp_id, position, kind, raw_content, level, heading_rank)
- Wraps raw_tree.json inside official envelope with schema_version 2.0
"""

import json
import logging
from pathlib import Path, PureWindowsPath
import re
from typing import Any

from arxiv_pipeline.common.schemas import PARSING_SCHEMA_VERSION, ParsedNode, StageResult, ErrorInfo
from arxiv_pipeline.common.utils import atomic_json, paper_key, utc_now

LOG = logging.getLogger("arxiv_pipeline")


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
LITERAL_ENVIRONMENTS = {"verbatim", "verbatim*", "lstlisting", "minted"}


def _next_heading(text, pos, heading_re):
    """Find a structural heading without entering literal, math or leaf blocks."""
    command_re = re.compile(r"\\[A-Za-z]+|\\.|\$\$?", re.DOTALL)
    atomic_envs = FIGURE_ENVIRONMENTS | BLOCK_MATH_ENVIRONMENTS | LITERAL_ENVIRONMENTS
    while match := command_re.search(text, pos):
        token = match.group()
        heading = heading_re.match(text, match.start())
        if heading:
            return heading
        pos = match.end()
        if token == r"\begin":
            env_match = re.match(r"\s*\{([^}]+)\}", text[pos:])
            if env_match and env_match.group(1) in atomic_envs:
                # find_matching_end_env expects the original exact begin tag.
                tag_start = text.find('{', pos)
                end = find_matching_end_env(text, tag_start - len(r'\begin'), env_match.group(1))
                pos = max(pos, end)
        elif token == r"\verb":
            if pos < len(text) and text[pos] == '*':
                pos += 1
            if pos < len(text):
                close = text.find(text[pos], pos + 1)
                pos = len(text) if close == -1 else close + 1
        elif token in {'$', '$$', r'\[', r'\('}:
            closing = {r'\[': r'\]', r'\(': r'\)'}.get(token, token)
            close = text.find(closing, pos)
            while close > 0 and text[close - 1] == '\\':
                close = text.find(closing, close + len(closing))
            pos = len(text) if close == -1 else close + len(closing)
    return None


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
    if env_name in LITERAL_ENVIRONMENTS:
        # Begin commands inside code are literal text, not nested environments.
        close = text.find(end_tag, pos)
        return len(text) if close == -1 else close + len(end_tag)
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
        self.child_counters: dict[str | None, int] = {}

    def _next_id(self) -> str:
        self.node_counter += 1
        return f"v{self.version}_n{self.node_counter}"

    def _add_node(
        self,
        parent_temp_id: str | None,
        kind: str,
        raw_content: str,
        *,
        node_id: str | None = None,
        level: int | None = None,
        heading_rank: int | None = None,
    ) -> ParsedNode:
        nid = node_id or self._next_id()
        pos = self.child_counters.get(parent_temp_id, 0)
        self.child_counters[parent_temp_id] = pos + 1
        node = ParsedNode(
            temp_id=nid,
            parent_temp_id=parent_temp_id,
            position=pos,
            kind=kind,
            raw_content=raw_content,
            level=level,
            heading_rank=heading_rank,
        )
        self.nodes.append(node)
        return node

    def build(self, merged_body_tex: str) -> list[ParsedNode]:
        if not merged_body_tex or not merged_body_tex.strip():
            return []

        # Nút gốc: document
        root_id = f"v{self.version}_root"
        self._add_node(
            parent_temp_id=None,
            kind="document",
            raw_content=f"Version {self.version} root",
            node_id=root_id,
            level=0,
        )

        # Ngăn xếp phân cấp: list of (rank, node_id, is_excluded)
        stack: list[tuple[int, str, bool]] = [(0, root_id, False)]

        # Regex tìm lệnh tiêu đề: \section*, \subsection, v.v.
        heading_re = re.compile(
            r"\\(part|chapter|section|subsection|subsubsection|paragraph|subparagraph)\*?(?:\[[^\]]*\])?\s*\{"
        )

        pos = 0
        text_len = len(merged_body_tex)

        while pos < text_len:
            match = _next_heading(merged_body_tex, pos, heading_re)
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
                heading_raw = merged_body_tex[match_start:pos]
                node = self._add_node(
                    parent_temp_id=parent_id,
                    kind="heading",
                    raw_content=heading_raw.strip(),
                    level=rank,
                    heading_rank=rank,
                )
                stack.append((rank, node.temp_id, False))
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
        start_re = re.compile(r"\\verb\*?|\\begin\{([a-zA-Z*]+)\}|\$\$|(?<!\\)\\\[")

        pos = 0
        text_len = len(text)

        while pos < text_len:
            match = start_re.search(text, pos)
            # Inline verb content can mention block commands. Keep it inside the
            # surrounding prose rather than finding a false figure/math boundary.
            while match and match.group(0).startswith(r"\verb"):
                delimiter_pos = match.end()
                if delimiter_pos >= text_len:
                    match = None
                    break
                close = text.find(text[delimiter_pos], delimiter_pos + 1)
                match = None if close == -1 else start_re.search(text, close + 1)
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
                    self._add_node(
                        parent_temp_id=parent_id,
                        kind="figure",
                        raw_content=matched_str.strip(),
                    )
                elif env_name in BLOCK_MATH_ENVIRONMENTS:
                    self._add_node(
                        parent_temp_id=parent_id,
                        kind="block_formula",
                        raw_content=matched_str.strip(),
                    )
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
                    self._add_node(
                        parent_temp_id=parent_id,
                        kind="prose_block",
                        raw_content=matched_str.strip(),
                    )
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
                self._add_node(
                    parent_temp_id=parent_id,
                    kind="block_formula",
                    raw_content=matched_str.strip(),
                )
            elif token == r"\[":
                end_idx = text.find(r"\]", match.end())
                if end_idx != -1:
                    matched_str = text[match_start:end_idx + 2]
                    pos = end_idx + 2
                else:
                    matched_str = text[match_start:]
                    pos = text_len
                self._add_node(
                    parent_temp_id=parent_id,
                    kind="block_formula",
                    raw_content=matched_str.strip(),
                )

    def _parse_list_environment(self, list_env_text: str, env_tag: str, parent_id: str) -> None:
        """Parses an itemize/enumerate block into an itemize node and item children."""
        itemize_node = self._add_node(
            parent_temp_id=parent_id,
            kind="itemize",
            raw_content="",
        )
        itemize_id = itemize_node.temp_id

        begin_str = f"\\begin{{{env_tag}}}"
        end_str = f"\\end{{{env_tag}}}"
        content = list_env_text.strip()
        if content.startswith(begin_str) and content.endswith(end_str):
            content = content[len(begin_str):-len(end_str)]

        items = split_top_level_items(content)
        for item_text in items:
            if not item_text.strip():
                continue
            item_node = self._add_node(
                parent_temp_id=itemize_id,
                kind="item",
                raw_content="",
            )
            # Nội dung bên trong item (có thể chứa văn bản, công thức toán hoặc itemize con)
            self._parse_body_content(item_text, item_node.temp_id)

    def _emit_prose_blocks(self, text: str, parent_id: str) -> None:
        """Splits prose text into paragraph blocks by empty lines or \\par."""
        paragraphs = re.split(r"\n\s*\n|\\par\b", text)
        for p in paragraphs:
            cleaned = p.strip()
            if cleaned:
                self._add_node(
                    parent_temp_id=parent_id,
                    kind="prose_block",
                    raw_content=cleaned,
                )


def build_hierarchy_tree(version: int, merged_body_tex: str) -> list[ParsedNode]:
    """Helper entrypoint to build hierarchy tree from merged LaTeX body string."""
    builder = HierarchyBuilder(version)
    return builder.build(merged_body_tex)


def _artifact_path(output: Path, relative: str) -> Path:
    if (not isinstance(relative, str) or not relative or "\\" in relative
            or Path(relative).is_absolute() or PureWindowsPath(relative).drive
            or PureWindowsPath(relative).root or '..' in Path(relative).parts):
        raise ValueError("Artifact path must be relative to the MS2 output root")
    path = (output / relative).resolve()
    if not path.is_relative_to(output):
        raise ValueError("Artifact path escapes the MS2 output root")
    return path


def run(record: dict, output_dir: Path, *, version: dict | None = None,
        upstream: dict | None = None) -> StageResult:
    """Consume the canonical MS2 stages.resolve envelope; never guess an artifact path."""
    paper_id = record.get('arxiv_id', '') if isinstance(record, dict) else ''
    number = version.get('version') if isinstance(version, dict) else None

    def result(status, artifacts=None, error=None, size=0):
        return StageResult('hierarchy', paper_id, number, status, utc_now(),
                           artifacts or {}, error, attempts=1, size_bytes=size,
                           schema_version=PARSING_SCHEMA_VERSION)

    try:
        key = paper_key(paper_id)
        if type(number) is not int or number < 1:
            raise ValueError('Hierarchy requires a positive integer version')
        output = Path(output_dir).resolve()
        if not isinstance(upstream, dict) or not isinstance(upstream.get('context'), dict):
            raise ValueError('Hierarchy requires the MS2 context/stages envelope')
        context = upstream['context']
        if not isinstance(context.get('input_root'), str) or not context['input_root']:
            raise ValueError('Missing MS1 input root in runtime context')
        snapshot = context.get('input_snapshot_id')
        if not isinstance(snapshot, str) or not snapshot:
            raise ValueError('Missing input snapshot identifier')
        stages = upstream.get('stages')
        if not isinstance(stages, dict):
            raise ValueError('Missing stages envelope')
        resolved = stages.get('resolve')
        if resolved is None:
            return result('skipped', error=ErrorInfo('upstream_unavailable', 'resolve checkpoint is missing'))
        if not isinstance(resolved, dict) or (
                resolved.get('stage'), resolved.get('paper_id'), resolved.get('version'),
                resolved.get('schema_version')) != ('resolve', paper_id, number, PARSING_SCHEMA_VERSION):
            raise ValueError('Resolver stage identity/schema does not match requested paper/version')
        if type(resolved.get('version')) is not int:
            raise ValueError('Resolver version must be an integer')
        status = resolved.get('status')
        if status not in {'success', 'no_source'}:
            return result('skipped', error=ErrorInfo('upstream_unavailable', f'resolve status: {status}'))
        artifacts = resolved.get('artifacts')
        if not isinstance(artifacts, dict) or 'gathered' not in artifacts:
            raise ValueError('Resolver result has no gathered artifact')
        for relative in artifacts.values():
            _artifact_path(output, relative)
        gathered_path = _artifact_path(output, artifacts['gathered'])
        if not gathered_path.is_file():
            raise ValueError('Resolver gathered artifact does not exist')
        gathered = json.loads(gathered_path.read_text(encoding='utf-8'))
        if not isinstance(gathered, dict) or (
                gathered.get('schema_version'), gathered.get('arxiv_id'), gathered.get('version')) != (
                    PARSING_SCHEMA_VERSION, paper_id, number):
            raise ValueError('Gathered artifact identity/schema mismatch')
        if type(gathered.get('version')) is not int or gathered.get('input_snapshot_id') != snapshot:
            raise ValueError('Gathered version/snapshot mismatch')
        empty, body = gathered.get('is_empty_source'), gathered.get('merged_body_tex')
        if type(empty) is not bool or not isinstance(body, str):
            raise ValueError('Gathered requires a boolean is_empty_source and string merged_body_tex')
        if empty != (status == 'no_source') or (empty and (body.strip() or gathered.get('main_tex_path') is not None)):
            raise ValueError('Resolver status contradicts gathered empty/body fields')
        warnings = gathered.get('warnings', [])
        if not isinstance(warnings, list) or not all(isinstance(w, dict) for w in warnings):
            raise ValueError('Gathered warnings must be a list of objects')
        warnings = list(warnings)
        if empty and not any(w.get('code') == 'no_tex_source' for w in warnings):
            warnings.append(dict(code='no_tex_source', message='No TeX source available.', source_path=None))
        nodes = [] if empty else build_hierarchy_tree(number, body)
        if not empty and not nodes:
            warnings.append(dict(code='empty_body', message='TeX source has no body content.', source_path=None))
        payload = dict(schema_version=PARSING_SCHEMA_VERSION, arxiv_id=paper_id,
                       version=number, nodes=[node.to_dict() for node in nodes], warnings=warnings)
        relative = f'parsed/{key}/v{number}/raw_tree.json'
        tree_path = _artifact_path(output, relative)
        atomic_json(tree_path, payload)
        return result(status, {'raw_tree': relative}, size=tree_path.stat().st_size)
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        return result('data_error', error=ErrorInfo('invalid_hierarchy_input', str(exc)))
    except OSError as exc:
        return result('error', error=ErrorInfo('io_error', str(exc)))
