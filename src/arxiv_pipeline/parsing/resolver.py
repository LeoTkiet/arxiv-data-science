"""Static TeX compilation-path resolver (MS2/TV1).

No TeX engine or shell is executed. Dynamic filenames, conditional compilation,
missing includes and malformed document/bibliography boundaries fail explicitly.
See docs/MS2_RESOLVER.md for the supported syntax and handoff example.
"""

from dataclasses import dataclass
import json
from pathlib import Path, PureWindowsPath
import re

from arxiv_pipeline.common.schemas import ErrorInfo, PARSING_SCHEMA_VERSION, StageResult
from arxiv_pipeline.common.utils import atomic_json, paper_key, utc_now

MAX_INCLUDE_DEPTH = 64
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_EXPANDED_CHARS = 64 * 1024 * 1024
_COMMAND = re.compile(r"\\([A-Za-z@]+|.)", re.DOTALL)
_LITERAL_ENVS = {'verbatim', 'verbatim*', 'lstlisting', 'minted'}
_MACROS = {'newcommand', 'renewcommand', 'providecommand', 'DeclareRobustCommand',
           'newenvironment', 'renewenvironment', 'def', 'gdef', 'edef', 'xdef'}
_HEADINGS = {'part': 1, 'chapter': 2, 'section': 3, 'subsection': 4,
             'subsubsection': 5, 'paragraph': 6, 'subparagraph': 7}
_REFERENCES = {'references', 'bibliography', 'works cited', 'tài liệu tham khảo'}
_COMPILATION_COMMAND = re.compile(
    r'\\(?:input|include|documentclass|bibliography|addbibresource)\b|\\(?:begin|end)\s*\{document\}')


class ResolveError(ValueError):
    """Input cannot be resolved faithfully within the supported static subset."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


@dataclass
class _Piece:
    text: str
    path: str
    start: int


@dataclass
class _Token:
    name: str
    start: int
    end: int
    value: str = ''


def _space(text, pos):
    while pos < len(text) and text[pos].isspace():
        pos += 1
    return pos


def _tex_space(text, pos):
    """Whitespace/comments between a command and a literal environment name."""
    while pos < len(text):
        if text[pos].isspace():
            pos += 1
        elif text[pos] == '%':
            newline = text.find('\n', pos)
            pos = len(text) if newline == -1 else newline + 1
        else:
            break
    return pos


def _group(text, pos, opening='{', closing='}'):
    pos = _space(text, pos)
    if pos >= len(text) or text[pos] != opening:
        raise ResolveError('malformed_tex', f'Expected {opening} at offset {pos}')
    start, depth, pos = pos + 1, 1, pos + 1
    while pos < len(text):
        if text[pos] == '\\':
            pos += 2
            continue
        if text[pos] == opening:
            depth += 1
        elif text[pos] == closing:
            depth -= 1
            if depth == 0:
                return text[start:pos], pos + 1
        pos += 1
    raise ResolveError('malformed_tex', f'Unclosed {opening} argument')


def _optional(text, pos):
    pos = _space(text, pos)
    while pos < len(text) and text[pos] == '[':
        _, pos = _group(text, pos, '[', ']')
        pos = _space(text, pos)
    return pos


def _literal_end(text, match):
    """Return the end of a literal region, or None for an ordinary command."""
    name, end = match.group(1), match.end()
    if name == 'verb':
        if end < len(text) and text[end] == '*':
            end += 1
        if end >= len(text) or text[end].isspace():
            raise ResolveError('malformed_tex', 'Invalid verb delimiter')
        close = text.find(text[end], end + 1)
        if close == -1 or '\n' in text[end:close]:
            raise ResolveError('malformed_tex', 'Unclosed verb literal')
        return close + 1
    if name == 'begin':
        # During comment stripping a valid begin may have a comment before its
        # argument. Only recognize an already-visible literal environment here;
        # ordinary/malformed begin arguments are handled by the later lexer.
        end = _tex_space(text, end)
        group = re.match(r'\{([^{}]+)\}', text[end:])
        if not group:
            return None
        env, end = group.group(1), end + group.end()
        if env in _LITERAL_ENVS or env == 'comment':
            tag = '\\end{' + env + '}'
            close = text.find(tag, end)
            if close == -1:
                raise ResolveError('malformed_tex', f'Unclosed {env} environment')
            return close + len(tag)
    return None


def _comment_pieces(text, path):
    """Remove comments (including their line ending), retaining exact source offsets."""
    pieces, start, pos = [], 0, 0
    while pos < len(text):
        if text[pos] == '\\':
            match = _COMMAND.match(text, pos)
            if match:
                end = _literal_end(text, match)
                if end is not None:
                    if match.group(1) == 'begin':
                        arg_start = _tex_space(text, match.end())
                        if _group(text, arg_start)[0] == 'comment':
                            if start < pos:
                                pieces.append(_Piece(text[start:pos], path, start))
                        else:
                            # Strip comments only in the begin-command header;
                            # preserve every percent/newline in the literal body.
                            pieces.append(_Piece(text[start:match.end()], path, start))
                            for gap in _comment_pieces(text[match.end():arg_start], path):
                                pieces.append(_Piece(gap.text, path, gap.start + match.end()))
                            pieces.append(_Piece(text[arg_start:end], path, arg_start))
                        start = end
                    pos = end
                else:
                    pos = match.end()
                continue
        if text[pos] == '%':
            if start < pos:
                pieces.append(_Piece(text[start:pos], path, start))
            newline = text.find('\n', pos)
            pos = len(text) if newline == -1 else newline + 1
            start = pos
        else:
            pos += 1
    if start < len(text):
        pieces.append(_Piece(text[start:], path, start))
    return pieces


def strip_latex_comments(text: str) -> str:
    """Strip real comments, preserving escaped percent and literal environments."""
    return ''.join(p.text for p in _comment_pieces(text, ''))


def _macro_end(text, match):
    name, pos = match.group(1), _space(text, match.end())
    if name in {'def', 'gdef', 'edef', 'xdef'}:
        brace = text.find('{', pos)
        if brace == -1:
            raise ResolveError('malformed_tex', 'Macro definition has no body')
        return _group(text, brace)[1]
    if pos < len(text) and text[pos] == '*':
        pos = _space(text, pos + 1)
    if pos < len(text) and text[pos] == '{':
        _, pos = _group(text, pos)
    else:
        command = _COMMAND.match(text, pos)
        if not command:
            raise ResolveError('malformed_tex', 'Macro definition has no name')
        pos = command.end()
    pos = _optional(text, pos)
    _, pos = _group(text, pos)
    if name in {'newenvironment', 'renewenvironment'}:
        _, pos = _group(text, pos)
    return pos


def _tokens(text, depth=0):
    """Lex commands without interpreting commands in literals or macro definitions."""
    if depth > MAX_INCLUDE_DEPTH:
        raise ResolveError('syntax_depth', 'Nested command argument depth limit exceeded')
    pos = 0
    while match := _COMMAND.search(text, pos):
        name, start, end = match.group(1), match.start(), match.end()
        literal_end = _literal_end(text, match)
        if literal_end is not None:
            pos = literal_end
            continue
        if name in _MACROS:
            pos = _macro_end(text, match)
            yield _Token('definition', start, pos, text[start:pos])
            continue
        value = ''
        if name in {'begin', 'end', 'documentclass', 'bibliography', 'bibliographystyle',
                    'addbibresource', 'includeonly', 'input', 'include', 'bibitem'} or name in _HEADINGS:
            arg_start = end
            if name in _HEADINGS and arg_start < len(text) and text[arg_start] == '*':
                arg_start += 1
            arg_start = _optional(text, arg_start)
            if name in {'input', 'include'} and (arg_start >= len(text) or text[arg_start] != '{'):
                filename = re.match(r'[^\s{}\\]+', text[arg_start:])
                if not filename:
                    raise ResolveError('dynamic_include', 'Include filename is not a static path')
                value, end = filename.group(), arg_start + filename.end()
            else:
                value, end = _group(text, arg_start)
        elif name == 'printbibliography':
            end = _optional(text, end)
        yield _Token(name, start, end, value)
        if name in _HEADINGS:
            # Headings can execute input/include inside their title arguments.
            for nested in _tokens(text[arg_start + 1:end - 1], depth + 1):
                yield _Token(nested.name, nested.start + arg_start + 1,
                             nested.end + arg_start + 1, nested.value)
        pos = end


def _slice(pieces, start, end):
    result, offset = [], 0
    for piece in pieces:
        stop = offset + len(piece.text)
        lo, hi = max(start, offset), min(end, stop)
        if lo < hi:
            result.append(_Piece(piece.text[lo - offset:hi - offset], piece.path,
                                 piece.start + lo - offset))
        offset = stop
        if offset >= end:
            break
    return result


def _text(pieces):
    return ''.join(piece.text for piece in pieces)


def _without(pieces, ranges):
    result, pos, text_len = [], 0, sum(len(p.text) for p in pieces)
    for start, end in sorted(ranges):
        if start > pos:
            result.extend(_slice(pieces, pos, start))
        pos = max(pos, end)
    result.extend(_slice(pieces, pos, text_len))
    return result


def _relative(root, value):
    """Validate serialized relative paths on both POSIX and Windows hosts."""
    if not isinstance(value, str) or not value or '\\' in value:
        raise ResolveError('unsafe_path', 'Expected a relative path using /')
    pure = PureWindowsPath(value)
    if Path(value).is_absolute() or pure.drive or pure.root or '..' in Path(value).parts:
        raise ResolveError('unsafe_path', 'Absolute/parent paths are forbidden')
    target = (root / value).resolve()
    if not target.is_relative_to(root):
        raise ResolveError('unsafe_path', 'Path escapes its root')
    return target


def _json(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, OSError) as exc:
        raise ResolveError('invalid_metadata', f'Cannot read metadata: {path.name}') from exc
    if not isinstance(value, dict):
        raise ResolveError('invalid_metadata', 'Metadata must be a JSON object')
    return value


class _Resolver:
    def __init__(self, input_root, corpus, paper_id):
        self.input_root, self.corpus, self.paper_id = input_root, corpus, paper_id
        self.cache, self.warnings, self.visited, self.included = {}, [], set(), []
        self.includeonly, self.finished = None, False
        self.macros, self.environments = {}, {}
        self.expanded_chars = self.source_bytes = 0
        self.tex_files, self.bib_files = [], []
        if not corpus.is_dir():
            raise ResolveError('missing_corpus', 'Successful extract has no corpus directory')
        for path in sorted(corpus.rglob('*')):
            resolved = path.resolve()
            if not resolved.is_relative_to(corpus):
                raise ResolveError('unsafe_path', 'Corpus contains an escaping symlink')
            if path.is_file() and path.suffix.lower() in {'.tex', '.bib'}:
                (self.tex_files if path.suffix.lower() == '.tex' else self.bib_files).append(path)

    def relative(self, path):
        return path.relative_to(self.input_root).as_posix()

    def warn(self, code, message, path=None):
        self.warnings.append(dict(code=code, message=message,
                                  source_path=self.relative(path) if path else None))

    def read(self, path):
        if path not in self.cache:
            if path.stat().st_size + self.source_bytes > MAX_SOURCE_BYTES:
                raise ResolveError('source_limit', 'Source byte limit exceeded')
            data = path.read_bytes()
            self.source_bytes += len(data)
            try:
                text = data.decode('utf-8')
            except UnicodeDecodeError:
                text = data.decode('latin-1')
                self.warn('encoding_fallback', 'Decoded non-UTF8 source as Latin-1', path)
            self.cache[path] = _comment_pieces(text, self.relative(path))
        return self.cache[path]

    def target(self, value, origin, suffix):
        value = value.strip()
        if not value or any(c in value for c in '\\{}#$~') or PureWindowsPath(value).drive or value.startswith('/'):
            raise ResolveError('dynamic_include', 'Filename must be a static relative path')
        if not Path(value).suffix:
            value += suffix
        if Path(value).suffix.lower() != suffix:
            raise ResolveError('unsupported_include', f'Only {suffix} source paths are supported')
        candidates = [(origin.parent / value).resolve(), (self.corpus / value).resolve()]
        for candidate in candidates:
            if not candidate.is_relative_to(self.corpus):
                raise ResolveError('unsafe_path', 'Include escapes version corpus')
            if candidate.is_file():
                return candidate
        return None

    def dynamic_macro(self, name, seen=None):
        seen = set() if seen is None else seen
        if name not in self.macros or name in seen:
            return False
        seen.add(name)
        definition = self.macros[name]
        return bool(_COMPILATION_COMMAND.search(definition)) or any(
            self.dynamic_macro(m.group(1), seen) for m in _COMMAND.finditer(definition))

    def find_root(self):
        candidates = []
        preferred = ['main', 'paper', 'ms', self.paper_id.replace('/', '_')]
        for path in self.tex_files:
            # An unrelated damaged draft must not prevent using a valid publication root.
            try:
                tokens = []
                for token in _tokens(_text(self.read(path))):
                    tokens.append(token)
                    if token.name == 'endinput' or (token.name == 'end' and token.value == 'document'):
                        break
            except ResolveError:
                continue
            if any(t.name == 'documentclass' for t in tokens):
                count = 0
                for token in tokens:
                    if token.name in {'input', 'include'}:
                        try:
                            count += self.target(token.value, path, '.tex') is not None
                        except ResolveError:
                            pass
                rank = preferred.index(path.stem.lower()) if path.stem.lower() in preferred else len(preferred)
                candidates.append((rank, -count, self.relative(path), path))
        if not candidates:
            raise ResolveError('missing_root', 'No usable documentclass root found')
        candidates.sort()
        chosen = candidates[0][-1]
        if len(candidates) > 1:
            self.warn('ambiguous_root', 'Root selected by filename priority, valid include count, then path', chosen)
        return chosen

    def _emit(self, pieces):
        self.expanded_chars += sum(len(p.text) for p in pieces)
        if self.expanded_chars > MAX_EXPANDED_CHARS:
            raise ResolveError('expansion_limit', 'Expanded source character limit exceeded')
        return pieces

    def expand(self, path, stack=()):
        if path in stack:
            raise ResolveError('include_cycle', 'Circular input/include detected')
        if len(stack) >= MAX_INCLUDE_DEPTH:
            raise ResolveError('include_depth', 'Include depth limit exceeded')
        if path not in self.visited:
            self.visited.add(path)
            if stack:
                self.included.append(self.relative(path))
        pieces, result, pos = self.read(path), [], 0
        text = _text(pieces)
        for token in _tokens(text):
            if token.name == 'definition':
                macro = re.match(r'\\[A-Za-z@]+\*?\s*(?:\{\s*)?\\([A-Za-z@]+)', token.value)
                if macro:
                    self.macros[macro.group(1)] = token.value
                env = re.match(r'\\(?:newenvironment|renewenvironment)\*?\s*\{([^}]+)\}', token.value)
                if env:
                    self.environments[env.group(1)] = token.value
            env_definition = self.environments.get(token.value, '') if token.name == 'begin' else ''
            dynamic_env = bool(_COMPILATION_COMMAND.search(env_definition)) or any(
                self.dynamic_macro(m.group(1)) for m in _COMMAND.finditer(env_definition))
            if self.dynamic_macro(token.name) or dynamic_env:
                raise ResolveError('dynamic_include', 'Invoked macro/environment controls compilation; expansion is unsupported')
            if token.name.startswith('if') or token.name in {
                    'else', 'fi', 'catcode', 'csname', 'loop', 'repeat', 'foreach', 'whiledo',
                    'import', 'subimport', 'subfile', 'inputfrom', 'includefrom', 'InputIfFileExists', 'IfFileExists'}:
                raise ResolveError('unsupported_tex', 'Conditional compilation/catcode/dynamic commands require a TeX engine')
            if token.name == 'includeonly':
                self.includeonly = {v.strip().removesuffix('.tex') for v in token.value.split(',') if v.strip()}
            if token.name in {'input', 'include'}:
                result.extend(self._emit(_slice(pieces, pos, token.start)))
                if token.name != 'include' or self.includeonly is None or token.value.removesuffix('.tex') in self.includeonly:
                    target = self.target(token.value, path, '.tex')
                    if target is None:
                        raise ResolveError('missing_include', f'Included TeX file not found: {token.value}')
                    result.extend(self.expand(target, stack + (path,)))
                pos = token.end
                if self.finished:
                    return result
            elif token.name == 'endinput':
                result.extend(self._emit(_slice(pieces, pos, token.start)))
                return result
            elif token.name == 'end' and token.value == 'document':
                result.extend(self._emit(_slice(pieces, pos, token.end)))
                self.finished = True
                return result
        result.extend(self._emit(_slice(pieces, pos, len(text))))
        return result

    def references(self, preamble, body):
        text = _text(body)
        tokens = list(_tokens(text))
        ranges, items = [], []
        index = 0
        while index < len(tokens):
            token = tokens[index]
            end = None
            if token.name == 'begin' and token.value == 'thebibliography':
                match = next((j for j in range(index + 1, len(tokens))
                              if tokens[j].name == 'end' and tokens[j].value == 'thebibliography'), None)
                if match is None:
                    raise ResolveError('malformed_bibliography', 'thebibliography has no closing environment')
                end, content_end = tokens[match].end, tokens[match].start
            elif token.name in _HEADINGS and token.value.strip().lower() in _REFERENCES:
                boundary = next((t for t in tokens[index + 1:] if t.name == 'appendix' or
                                 (t.name in _HEADINGS and _HEADINGS[t.name] <= _HEADINGS[token.name])), None)
                end = content_end = boundary.start if boundary else len(text)
                closing = next((t for t in tokens[index + 1:] if t.start < end
                                and t.name == 'end' and t.value == 'thebibliography'), None)
                if closing:
                    end, content_end = closing.end, closing.start
                if not any(t.name == 'bibitem' and token.end <= t.start < end for t in tokens):
                    index += 1
                    continue
            if end is not None:
                bibitems = [t for t in tokens if t.name == 'bibitem' and token.end <= t.start < content_end]
                for i, item in enumerate(bibitems):
                    stop = bibitems[i + 1].start if i + 1 < len(bibitems) else content_end
                    stop = next((t.start for t in tokens if item.end <= t.start < stop
                                 and t.name == 'end' and t.value == 'thebibliography'), stop)
                    items.append(text[item.start:stop].strip())
                ranges.append((token.start, end))
                while index < len(tokens) and tokens[index].start < end:
                    index += 1
                continue
            index += 1
        referenced = []
        # Bibliography declarations can occur in either the preamble or body.
        for pieces in (preamble, body):
            chunk = _text(pieces)
            for token in _tokens(chunk):
                if token.name in {'bibliography', 'addbibresource'}:
                    source = _slice(pieces, token.start, token.start + 1)[0].path
                    origin = self.input_root / source
                    for name in token.value.split(','):
                        target = self.target(name, origin, '.bib')
                        if target is None:
                            self.warn('missing_bibliography', f'Referenced BibTeX file not found: {name}', origin)
                        elif self.relative(target) not in referenced:
                            referenced.append(self.relative(target))
        ranges.extend((t.start, t.end) for t in tokens if t.name in
                      {'bibliography', 'bibliographystyle', 'addbibresource', 'printbibliography'})
        return _without(body, ranges), items, referenced

    def gather(self, payload):
        payload['bib_file_paths'] = [self.relative(p) for p in self.bib_files]
        if not self.tex_files:
            if not self.bib_files:
                raise ResolveError('empty_corpus', 'Successful extract contains no TeX/BibTeX files')
            self.warn('no_tex_source', 'Corpus inventory contains only BibTeX files')
            return 'no_source'
        root = self.find_root()
        merged = self.expand(root)
        text = _text(merged)
        tokens = list(_tokens(text))
        begins = [t for t in tokens if t.name == 'begin' and t.value == 'document']
        ends = [t for t in tokens if t.name == 'end' and t.value == 'document']
        if len(begins) != 1 or len(ends) != 1 or begins[0].end > ends[0].start:
            raise ResolveError('document_boundary', 'Expected one begin/end document pair on compilation path')
        preamble = _slice(merged, 0, begins[0].start)
        body, items, referenced = self.references(preamble, _slice(merged, begins[0].end, ends[0].start))
        spans, offset = [], 0
        for piece in body:
            spans.append(dict(start=offset, end=offset + len(piece.text), source_path=piece.path,
                              source_start=piece.start, source_end=piece.start + len(piece.text)))
            offset += len(piece.text)
        payload.update(is_empty_source=False, main_tex_path=self.relative(root),
                       included_files=self.included,
                       ignored_files=[self.relative(p) for p in self.tex_files if p not in self.visited],
                       preamble_tex=_text(preamble), merged_body_tex=_text(body), raw_bibitems=items,
                       referenced_bib_file_paths=referenced, source_spans=spans)
        return 'success'


def _checkpoint(value, name, paper_id, number):
    if value is None:
        return None
    if not isinstance(value, dict) or (value.get('schema_version'), value.get('stage'),
            value.get('paper_id'), value.get('version')) != ('1.0', name, paper_id, number):
        raise ResolveError('invalid_checkpoint', 'MS1 checkpoint identity/schema mismatch')
    if type(value.get('version')) is not int or not isinstance(value.get('artifacts'), dict):
        raise ResolveError('invalid_checkpoint', 'MS1 checkpoint version/artifacts are malformed')
    return value


def _readiness(stages, input_root, version, paper_id, number):
    download = _checkpoint(stages.get('download'), 'download', paper_id, number)
    extract = _checkpoint(stages.get('extract'), 'extract', paper_id, number)
    if download is None or extract is None:
        return 'skipped', 'MS1 download/extract checkpoint is not available'
    for stage in (download, extract):
        for path in stage['artifacts'].values():
            _relative(input_root, path)
    if download.get('status') == 'no_source' or extract.get('status') == 'no_source':
        corpus = _relative(input_root, version.get('corpus_path'))
        if corpus.is_dir() and any(p.suffix.lower() in {'.tex', '.bib'}
                                   for p in corpus.rglob('*') if p.is_file()):
            raise ResolveError('inconsistent_empty', 'Empty checkpoint contradicts existing TeX/BibTeX corpus')
    if download.get('status') == 'no_source':
        meta_rel = download['artifacts'].get('download_metadata')
        if not meta_rel:
            raise ResolveError('unverified_empty', 'no_source requires download metadata evidence')
        meta = _json(_relative(input_root, meta_rel))
        if (meta.get('arxiv_id'), meta.get('version'), meta.get('status'), meta.get('source_format')) != (
                paper_id, number, 'no_source', 'pdf') or extract.get('status') not in {'skipped', 'no_source'}:
            raise ResolveError('unverified_empty', 'Only confirmed PDF-only evidence is an empty download')
        if not 200 <= meta.get('http_status', 0) < 300:
            raise ResolveError('unverified_empty', 'PDF-only evidence must have successful HTTP status')
        return 'no_source', 'Confirmed PDF-only source'
    if download.get('status') != 'success' or extract.get('status') not in {'success', 'no_source'}:
        return 'skipped', 'MS1 upstream is not successful'
    source_rel = version.get('source_path')
    source = _relative(input_root, source_rel)
    if download['artifacts'].get('source') != source_rel or not source.is_file() or source.stat().st_size == 0:
        raise ResolveError('missing_source', 'Successful download has missing/empty/mismatched source')
    if extract['status'] == 'no_source':
        error = extract.get('error') or {}
        if error.get('kind') != 'no_tex_source' or error.get('message') != 'archive contains no valid .tex or .bib files':
            raise ResolveError('unverified_empty', 'Extraction no_source reason is not verified')
        return 'no_source', 'Extract confirmed archive without TeX/BibTeX files'
    if extract['artifacts'].get('corpus') != version.get('corpus_path'):
        raise ResolveError('invalid_checkpoint', 'Extract corpus path differs from VersionRecord')
    return 'success', None


def run(record: dict, output_dir: Path, *, version: dict | None = None,
        upstream: dict | None = None) -> StageResult:
    """Read immutable MS1 inputs and atomically emit gathered.json in a separate root."""
    paper_id = record.get('arxiv_id', '') if isinstance(record, dict) else ''
    number = version.get('version') if isinstance(version, dict) else None

    def result(status, artifacts=None, error=None, size=0):
        return StageResult('resolve', paper_id, number, status, utc_now(), artifacts or {},
                           error, attempts=1, size_bytes=size, schema_version=PARSING_SCHEMA_VERSION)

    try:
        key = paper_key(paper_id)
        if type(number) is not int or number < 1:
            raise ResolveError('invalid_version', 'Resolver requires a positive integer version')
        if version.get('versioned_id') != f'{paper_id}v{number}':
            raise ResolveError('invalid_version', 'Versioned identifier does not match paper/version')
        if not isinstance(upstream, dict) or not isinstance(upstream.get('context'), dict):
            raise ResolveError('invalid_context', 'Resolver requires an upstream context envelope')
        context = upstream['context']
        if not isinstance(context.get('input_root'), str) or not context['input_root']:
            raise ResolveError('invalid_context', 'Missing MS1 input root')
        if not isinstance(context.get('input_snapshot_id'), str) or not context['input_snapshot_id']:
            raise ResolveError('invalid_context', 'Missing immutable input snapshot identifier')
        source_root, output = Path(context['input_root']).resolve(), Path(output_dir).resolve()
        if output.is_relative_to(source_root) or source_root.is_relative_to(output):
            raise ResolveError('overlapping_roots', 'MS1 input and MS2 output roots must be disjoint')
        corpus = _relative(source_root, version.get('corpus_path'))
        if not isinstance(upstream.get('stages'), dict):
            raise ResolveError('invalid_context', 'Missing MS1 stages envelope')
        status, reason = _readiness(upstream['stages'], source_root, version, paper_id, number)
        if status == 'skipped':
            return result('skipped', error=ErrorInfo('upstream_unavailable', reason))
        payload = dict(schema_version=PARSING_SCHEMA_VERSION, arxiv_id=paper_id, version=number,
                       is_empty_source=True, main_tex_path=None, included_files=[], ignored_files=[],
                       merged_body_tex='', preamble_tex='', raw_bibitems=[], bib_file_paths=[],
                       referenced_bib_file_paths=[], input_snapshot_id=context['input_snapshot_id'],
                       warnings=[], source_spans=[])
        if status == 'no_source':
            payload['warnings'].append(dict(code='no_tex_source', message=reason, source_path=None))
        else:
            gatherer = _Resolver(source_root, corpus, paper_id)
            status = gatherer.gather(payload)
            payload['warnings'] = gatherer.warnings
        relative = f'parsed/{key}/v{number}/gathered.json'
        path = _relative(output, relative)
        atomic_json(path, payload)
        return result(status, {'gathered': relative}, size=path.stat().st_size)
    except (ResolveError, ValueError, TypeError, KeyError) as exc:
        return result('data_error', error=ErrorInfo(getattr(exc, 'code', 'invalid_input'), str(exc)))
    except OSError as exc:
        return result('error', error=ErrorInfo('io_error', str(exc)))
