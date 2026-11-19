"""Static TeX compilation-path resolver (MS2/TV1).

No TeX engine or shell is executed. Proven static branches can be selected;
unresolved conditions, dynamic filenames and malformed boundaries fail explicitly.
See docs/MS2_RESOLVER.md for the supported syntax and handoff example.
"""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PureWindowsPath
import re
import tarfile

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
_BIBLIOGRAPHY_ENVS = {'thebibliography', 'references'}
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
    archive_member: str | None = None


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


def _conditional_bounds(text, opening):
    """Find branch boundaries without parsing malformed inactive macro bodies."""
    level, alternative, pos = 1, None, opening.end()
    while match := _COMMAND.search(text, pos):
        name, pos = match.group(1), match.end()
        literal = _literal_end(text, match)
        if literal is not None:
            pos = literal
            continue
        if name in _MACROS:
            try: pos = _macro_end(text, match)
            except ResolveError: pass  # TeX skips inactive tokens, not their argument syntax.
            continue
        if name == 'newif':
            declaration = _COMMAND.match(text, _space(text, pos))
            if declaration: pos = declaration.end()
        elif name.startswith('if'):
            level += 1
        elif name == 'fi':
            level -= 1
            if level == 0: return alternative, match
        elif name == 'else' and level == 1:
            if alternative is not None:
                raise ResolveError('malformed_conditional', 'Multiple else branches')
            alternative = match
    raise ResolveError('malformed_conditional', f'Unclosed conditional {opening.group(1)}')


def _tokens(text, depth=0, *, booleans=None, unused_conditional=None):
    """Lex commands without interpreting commands in literals or macro definitions."""
    if depth > MAX_INCLUDE_DEPTH:
        raise ResolveError('syntax_depth', 'Nested command argument depth limit exceeded')
    pos, group_depth = 0, 0
    while match := _COMMAND.search(text, pos):
        name, start, end = match.group(1), match.start(), match.end()
        if booleans is not None:
            gap = re.sub(r'\\.', '', text[pos:start], flags=re.S)
            group_depth += gap.count('{') - gap.count('}')
        literal_end = _literal_end(text, match)
        if literal_end is not None:
            pos = literal_end
            continue
        if name in _MACROS:
            pos = _macro_end(text, match)
            if booleans is not None:
                defined = re.match(r'\\\w+\*?\s*(?:\{\s*)?\\([A-Za-z@]+)', text[start:pos])
                if defined:
                    target = defined.group(1)
                    if target in booleans or target in {'iftrue','iffalse'}:
                        raise ResolveError('unsupported_conditional', 'Redefined boolean command requires a TeX engine')
                    for ending in ('true','false'):
                        if target.endswith(ending) and 'if'+target[:-len(ending)] in booleans:
                            raise ResolveError('unsupported_conditional', 'Redefined boolean setter requires a TeX engine')
            yield _Token('definition', start, pos, text[start:pos])
            continue
        if booleans is not None:
            if name == 'let' and booleans:
                raise ResolveError('unsupported_conditional', 'Boolean aliases require a TeX engine')
            if name == 'begingroup': group_depth += 1
            elif name == 'endgroup': group_depth -= 1
            if name == 'newif':
                declaration = _COMMAND.match(text, _space(text, end))
                if declaration is None or not declaration.group(1).startswith('if'):
                    raise ResolveError('malformed_conditional', 'newif requires a conditional command')
                if group_depth or depth:
                    raise ResolveError('unsupported_conditional', 'Grouped boolean declarations require a TeX engine')
                flag, pos = declaration.group(1), declaration.end()
                booleans[flag] = False
                yield _Token('conditional_control', start, pos, f'Declared {flag}=false')
                continue
            assignment = next(((name[:-len(ending)], value) for ending,value in
                (('true',True),('false',False)) if name.endswith(ending)
                and 'if'+name[:-len(ending)] in booleans), None)
            if assignment:
                if group_depth or depth:
                    raise ResolveError('unsupported_conditional', 'Grouped boolean assignments require a TeX engine')
                booleans['if'+assignment[0]] = assignment[1]
                pos = end
                yield _Token('conditional_control', start, pos, f'Assigned if{assignment[0]}={assignment[1]}')
                continue
            if name.startswith('if') and name != 'ifCLASSOPTIONcaptionsoff':
                alternative, closing = _conditional_bounds(text, match)
                choice = {'iftrue':True,'iffalse':False}.get(name, booleans.get(name))
                if choice is None:
                    branches = [text[end:alternative.start() if alternative else closing.start()]]
                    if alternative: branches.append(text[alternative.end():closing.start()])
                    if unused_conditional and unused_conditional(text, start, closing.end(), branches):
                        pos = closing.end()
                        yield _Token('conditional_control', start, pos, f'Omitted unused macro definitions in {name}; branch not assumed')
                        continue
                    raise ResolveError('unsupported_conditional', f'Cannot determine active branch of {name}')
                lo = end if choice else (alternative.end() if alternative else closing.start())
                hi = (alternative.start() if alternative else closing.start()) if choice else closing.start()
                yield _Token('conditional_control', start, lo, f'Selected {name}={choice}')
                for nested in _tokens(text[lo:hi], depth+1, booleans=booleans, unused_conditional=unused_conditional):
                    yield _Token(nested.name, nested.start+lo, nested.end+lo, nested.value)
                pos = closing.end()
                yield _Token('conditional_control', hi, pos)
                continue
        if name == 'titlespacing':
            pos = end + (end < len(text) and text[end] == '*')
            pos = _space(text, pos)
            if pos < len(text) and text[pos] == '{':
                heading, pos = _group(text, pos)
            else:
                heading_match = _COMMAND.match(text, pos)
                if heading_match is None:
                    raise ResolveError('malformed_tex', 'titlespacing requires a command reference')
                heading, pos = heading_match.group(), heading_match.end()
            if not re.fullmatch(r'\\[A-Za-z@]+', heading.strip()):
                raise ResolveError('unsupported_tex', 'titlespacing requires a single command reference')
            arguments = []
            for _ in range(3):
                value, pos = _group(text, pos)
                arguments.append(value)
            pos = _space(text, pos)
            if pos < len(text) and text[pos] == '[':
                value, pos = _group(text, pos, '[', ']')
                arguments.append(value)
            # Its first argument names a heading command; it does not execute it.
            yield _Token('format_config', start, pos, ' '.join(arguments))
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
            for nested in _tokens(text[arg_start + 1:end - 1], depth + 1,
                                  booleans=booleans, unused_conditional=unused_conditional):
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
                                 piece.start + lo - offset, piece.archive_member))
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
    def __init__(self, input_root, corpus, paper_id, *, raw_source=None):
        self.input_root, self.corpus, self.paper_id = input_root, corpus, paper_id
        self.raw_source, self.archive_index = raw_source, None
        self.archive_paths, self.archive_sources = {}, {}
        self.archive_hash = None
        self.cache, self.warnings, self.visited, self.included = {}, [], set(), []
        self.includeonly, self.finished = None, False
        self.macros, self.environments = {}, {}
        self.booleans = {}
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
            member = self.archive_paths.get(path)
            if member is not None:
                data = self.archive_read(member)
            else:
                if path.stat().st_size + self.source_bytes > MAX_SOURCE_BYTES:
                    raise ResolveError('source_limit', 'Source byte limit exceeded')
                data = path.read_bytes()
            if len(data) + self.source_bytes > MAX_SOURCE_BYTES:
                raise ResolveError('source_limit', 'Source byte limit exceeded')
            self.source_bytes += len(data)
            try:
                text = data.decode('utf-8')
            except UnicodeDecodeError:
                text = data.decode('latin-1')
                self.warn('encoding_fallback', 'Decoded non-UTF8 source as Latin-1', path)
            self.cache[path] = _comment_pieces(text, self.relative(path))
            if member is not None:
                for piece in self.cache[path]: piece.archive_member = self.archive_sources[member]['member']
        return self.cache[path]

    def archive_members(self):
        """Inspect this version's raw archive; never extract or follow links."""
        if self.archive_index is None:
            if self.raw_source is None or not tarfile.is_tarfile(self.raw_source):
                self.archive_index = {}
                return self.archive_index
            members = {}
            try:
                with tarfile.open(self.raw_source, 'r:*') as archive:
                    for index, member in enumerate(archive):
                        if index >= 10000:
                            raise ResolveError('source_limit', 'Raw archive member limit exceeded')
                        name = member.name
                        while name.startswith('./'): name = name[2:]
                        if not name or member.isdir(): continue
                        # Validate even unused member names; no path is materialized.
                        name = _relative(self.corpus, name).relative_to(self.corpus).as_posix()
                        if name in members:
                            raise ResolveError('ambiguous_archive', f'Duplicate raw archive member: {name}')
                        members[name] = member
            except tarfile.TarError as exc:
                raise ResolveError('invalid_archive', str(exc)) from exc
            # Publish only a fully validated index. Root ranking may catch an
            # error to inspect other candidates; it must never reuse a partial index.
            self.archive_index = members
        return self.archive_index

    def archive_read(self, name):
        member = self.archive_members().get(name)
        if member is None: return None
        if not member.isfile():
            raise ResolveError('unsafe_path', 'Raw dependency is not a regular file')
        if member.size + self.source_bytes > MAX_SOURCE_BYTES:
            raise ResolveError('source_limit', 'Raw dependency byte limit exceeded')
        try:
            with tarfile.open(self.raw_source, 'r:*') as archive:
                stream = archive.extractfile(member)
                if stream is None: raise ResolveError('invalid_archive', 'Unreadable raw dependency')
                with stream: data = stream.read(member.size + 1)
        except tarfile.TarError as exc:
            raise ResolveError('invalid_archive', str(exc)) from exc
        if len(data) != member.size or any(c < 32 and c not in {9,10,12,13} for c in data):
            raise ResolveError('invalid_archive_source', 'Raw dependency is binary or truncated')
        if self.archive_hash is None:
            digest = hashlib.sha256()
            with self.raw_source.open('rb') as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b''): digest.update(chunk)
            self.archive_hash = digest.hexdigest()
        self.archive_sources[name] = dict(source_path=self.relative(self.raw_source),
            source_sha256=self.archive_hash, member=member.name, member_sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data))
        return data

    def target(self, value, origin, suffix):
        value = value.strip()
        if not value:
            raise ResolveError('empty_include', 'Source contains an empty dependency filename; no target can be inferred')
        if any(c in value for c in '\\{}#$~') or PureWindowsPath(value).drive or value.startswith('/'):
            raise ResolveError('dynamic_include', 'Filename must be a static relative path')
        # A dot in an extensionless TeX basename is not necessarily a suffix.
        names = [value] if value.lower().endswith(suffix) else [value, value + suffix]
        for directory in dict.fromkeys([origin.parent, self.corpus]):
            found = []
            for name in names:
                candidate = (directory / name).resolve()
                if not candidate.is_relative_to(self.corpus):
                    raise ResolveError('unsafe_path', 'Include escapes version corpus')
                if candidate.is_file():
                    found.append(candidate)
                elif suffix == '.tex' and not candidate.suffix:
                    member = candidate.relative_to(self.corpus).as_posix()
                    if member in self.archive_members():
                        self.archive_paths[candidate] = member
                        found.append(candidate)
            if len(found) > 1:
                raise ResolveError('ambiguous_include', f'Ambiguous include: {value}')
            if found:
                if not found[0].name.lower().endswith(suffix) and found[0] not in self.archive_paths:
                    raise ResolveError('unsupported_include', f'Only {suffix} source paths are supported')
                return found[0]
        if Path(value).suffix and not value.lower().endswith(suffix) and '.' not in Path(value).stem:
            # Preserve the explicit unsupported-extension diagnostic for missing files.
            raise ResolveError('unsupported_include', f'Only {suffix} source paths are supported')
        return None

    def dynamic_macro(self, name, seen=None):
        seen = set() if seen is None else seen
        if name not in self.macros or name in seen:
            return False
        seen.add(name)
        definition = self.macros[name]
        return bool(_COMPILATION_COMMAND.search(definition)) or any(
            any(m.group(1).endswith(ending) and
            'if'+m.group(1)[:-len(ending)] in self.booleans for ending in ('true','false')) or
            self.dynamic_macro(m.group(1), seen) for m in _COMMAND.finditer(definition))

    def unused_conditional(self, text, start, end, branches):
        """Prune only standalone, unused newcommand definitions, never prose/includes."""
        names = set()
        for branch in branches:
            pos = 0
            try: tokens = list(_tokens(branch))
            except ResolveError: return False
            for token in tokens:
                if branch[pos:token.start].strip() or token.name != 'definition': return False
                macro = re.match(r'\\newcommand\*?\s*(?:\{\s*)?\\([A-Za-z@]+)', token.value)
                if macro is None: return False
                names.add(macro.group(1)); pos = token.end
            if branch[pos:].strip(): return False
        if not names: return False
        remaining = '\n'.join(_text(self.read(p)) for p in self.tex_files).replace(text[start:end], '')
        # Includes or generated command names prevent a proof that these names are unused.
        if re.search(r'\\(?:input|include|csname|let)\b', remaining): return False
        return not any(re.search(r'\\'+re.escape(name)+r'(?![A-Za-z@])', remaining) for name in names)

    def find_root(self):
        candidates = []
        rejected = []
        preferred = ['main', 'paper', 'ms', self.paper_id.replace('/', '_')]
        for path in self.tex_files:
            # An unrelated damaged draft must not prevent using a valid publication root.
            try:
                tokens = []
                for token in _tokens(_text(self.read(path)), booleans={}, unused_conditional=self.unused_conditional):
                    tokens.append(token)
                    if token.name == 'endinput' or (token.name == 'end' and token.value == 'document'):
                        break
            except ResolveError as exc:
                rejected.append(f'{self.relative(path)}: {exc.code}: {exc}')
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
                malformed_document = (sum(t.name == 'documentclass' for t in tokens) > 1 or
                    sum(t.name == 'begin' and t.value == 'document' for t in tokens) > 1)
                candidates.append((malformed_document, rank, -count, self.relative(path), path))
        if not candidates:
            detail = '; rejected candidates: ' + '; '.join(rejected[:3]) if rejected else ''
            raise ResolveError('missing_root', 'No usable documentclass root found' + detail)
        candidates.sort()
        chosen = candidates[0][-1]
        if len(candidates) > 1:
            self.warn('ambiguous_root', 'Root selected by document viability, filename priority, valid include count, then path', chosen)
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
        for token in _tokens(text, booleans=self.booleans, unused_conditional=self.unused_conditional):
            if token.start < pos:
                continue
            if token.name == 'conditional_control':
                result.extend(self._emit(_slice(pieces, pos, token.start)))
                pos = token.end
                if token.value: self.warn('static_conditional', token.value, path)
                continue
            if token.name == 'format_config' and (
                    _COMPILATION_COMMAND.search(token.value) or any(
                        self.dynamic_macro(m.group(1)) or m.group(1).startswith('if')
                        for m in _COMMAND.finditer(token.value))):
                raise ResolveError('unsupported_tex', 'Formatting configuration contains dynamic compilation commands')
            # Both outcomes of this observed class switch affect page layout
            # only. Never generalize this rule to unknown conditional content.
            if token.name == 'ifCLASSOPTIONcaptionsoff':
                layout = re.match(r'\\ifCLASSOPTIONcaptionsoff\b(?:\s|\\(?:newpage|clearpage|relax)\b)*\\fi\b', text[token.start:])
                if layout:
                    result.extend(self._emit(_slice(pieces, pos, token.start)))
                    pos = token.start + layout.end()
                    self.warn('layout_conditional_omitted', 'Omitted layout-only class conditional', path)
                    continue
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
        # Validate boundaries before a References heading can consume the region.
        bibliography_ends, opening = {}, None
        for token in tokens:
            if token.name == 'begin' and token.value in _BIBLIOGRAPHY_ENVS:
                if opening is not None:
                    raise ResolveError('malformed_bibliography', 'Nested bibliography environments are unsupported')
                opening = token
            elif token.name == 'end' and token.value in _BIBLIOGRAPHY_ENVS:
                if opening is None or token.value != opening.value:
                    raise ResolveError('malformed_bibliography', 'Mismatched bibliography environment end')
                bibliography_ends[opening.start] = token
                opening = None
        if opening is not None:
            raise ResolveError('malformed_bibliography', f'{opening.value} has no closing environment')

        ranges, items = [], []
        index = 0
        while index < len(tokens):
            token = tokens[index]
            end = None
            if token.name == 'begin' and token.value in _BIBLIOGRAPHY_ENVS:
                closing = bibliography_ends[token.start]
                end, content_end = closing.end, closing.start
            elif token.name in _HEADINGS and token.value.strip().lower() in _REFERENCES:
                boundary = next((t for t in tokens[index + 1:] if t.name == 'appendix' or
                                 (t.name in _HEADINGS and _HEADINGS[t.name] <= _HEADINGS[token.name])), None)
                end = content_end = boundary.start if boundary else len(text)
                closing = next((t for t in tokens[index + 1:] if t.start < end
                                and t.name == 'end' and t.value in _BIBLIOGRAPHY_ENVS), None)
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
                                 and t.name == 'end' and t.value in _BIBLIOGRAPHY_ENVS), stop)
                    items.append(text[item.start:stop].strip())
                ranges.append((token.start, end))
                while index < len(tokens) and tokens[index].start < end:
                    index += 1
                continue
            index += 1
        referenced, unresolved = [], []
        # Bibliography declarations can occur in either the preamble or body.
        for pieces in (preamble, body):
            chunk = _text(pieces)
            for token in _tokens(chunk):
                if token.name in {'bibliography', 'addbibresource'}:
                    source = _slice(pieces, token.start, token.start + 1)[0].path
                    origin = self.input_root / source
                    for name in token.value.split(','):
                        try:
                            target = self.target(name, origin, '.bib')
                        except ResolveError as exc:
                            if exc.code != 'unsafe_path': raise
                            unresolved.append((name, origin, exc))
                            continue
                        if target is None:
                            unresolved.append((name, origin, None))
                        elif self.relative(target) not in referenced:
                            referenced.append(self.relative(target))
        if unresolved and not referenced and not items:
            fallback = self.archive_bibliography(_text(preamble), text)
            if fallback:
                items.extend(fallback)
                self.warn('archive_bibliography_fallback',
                          'Used same-jobname BBL from this version raw archive; all static cited keys are present',
                          self.raw_source)
                unresolved = []
        for name, origin, error in unresolved:
            if error is not None: raise error
            self.warn('missing_bibliography', f'Referenced BibTeX file not found: {name}', origin)
        ranges.extend((t.start, t.end) for t in tokens if t.name in
                      {'bibliography', 'bibliographystyle', 'addbibresource', 'printbibliography'})
        return _without(body, ranges), items, referenced

    def archive_bibliography(self, preamble, body, *, require_complete=True):
        from arxiv_pipeline.parsing.bibliography import _cited_keys
        selected = _cited_keys(dict(preamble_tex=preamble, merged_body_tex=body))
        if not selected: return None
        name = self.main_path.relative_to(self.corpus).with_suffix('.bbl').as_posix()
        data = self.archive_read(name)
        if data is None: return None
        try:
            decoded = data.decode('utf-8')
        except UnicodeDecodeError:
            decoded = data.decode('latin-1')
            self.warn('encoding_fallback', f'Decoded archive member {name} as Latin-1', self.raw_source)
        text = _text(_comment_pieces(decoded, name))
        tokens = list(_tokens(text))
        if any(t.name in {'input','include','bibliography','addbibresource','documentclass'} for t in tokens):
            return None
        begins = [t for t in tokens if t.name == 'begin' and t.value == 'thebibliography']
        ends = [t for t in tokens if t.name == 'end' and t.value == 'thebibliography']
        if len(begins) != 1 or len(ends) != 1 or begins[0].end > ends[0].start: return None
        items = [t for t in tokens if t.name == 'bibitem' and begins[0].end <= t.start < ends[0].start]
        if require_complete and not selected.issubset({t.value.strip() for t in items}): return None
        return [text[t.start:(items[i+1].start if i+1 < len(items) else ends[0].start)].strip()
                for i,t in enumerate(items)]

    def gather(self, payload):
        payload['bib_file_paths'] = [self.relative(p) for p in self.bib_files]
        if not self.tex_files:
            if not self.bib_files:
                raise ResolveError('empty_corpus', 'Successful extract contains no TeX/BibTeX files')
            self.warn('no_tex_source', 'Corpus inventory contains only BibTeX files')
            return 'no_source'
        root = self.find_root()
        self.main_path = root
        merged = self.expand(root)
        text = _text(merged)
        tokens = list(_tokens(text))
        begins = [t for t in tokens if t.name == 'begin' and t.value == 'document']
        ends = [t for t in tokens if t.name == 'end' and t.value == 'document']
        if len(begins) != 1 or len(ends) != 1 or begins[0].end > ends[0].start:
            raise ResolveError('document_boundary', 'Expected one begin/end document pair on compilation path')
        preamble = _slice(merged, 0, begins[0].start)
        body, items, referenced = self.references(preamble, _slice(merged, begins[0].end, ends[0].start))
        if referenced:
            supplemental = self.archive_bibliography(_text(preamble), _text(body), require_complete=False)
            if supplemental: payload['archive_bibitems'] = supplemental
        spans, offset = [], 0
        for piece in body:
            spans.append(dict(start=offset, end=offset + len(piece.text), source_path=piece.path,
                              source_start=piece.start, source_end=piece.start + len(piece.text)))
            if piece.archive_member is not None:
                spans[-1].update(source_path=self.relative(self.raw_source), archive_member=piece.archive_member)
            offset += len(piece.text)
        payload.update(is_empty_source=False, main_tex_path=self.relative(root),
                       included_files=self.included,
                       ignored_files=[self.relative(p) for p in self.tex_files if p not in self.visited],
                       preamble_tex=_text(preamble), merged_body_tex=_text(body), raw_bibitems=items,
                       referenced_bib_file_paths=referenced, source_spans=spans)
        if self.archive_sources:
            payload['archive_sources'] = list(self.archive_sources.values())
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
            gatherer = _Resolver(source_root, corpus, paper_id,
                                 raw_source=_relative(source_root, version['source_path']))
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
