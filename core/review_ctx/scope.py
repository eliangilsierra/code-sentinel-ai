"""Heuristic location of the function enclosing a changed line in Java and TypeScript sources.

The source is masked (comments, string contents and regular expressions replaced by spaces, keeping
offsets and line breaks) and braces are then matched. Each ``{`` that is not inside parentheses is
classified from the text that precedes it as a function, a type body or a plain block.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

JAVA = "java"
TYPESCRIPT = "typescript"
LANGUAGES = {
    ".java": JAVA,
    ".ts": TYPESCRIPT,
    ".tsx": TYPESCRIPT,
    ".js": TYPESCRIPT,
    ".jsx": TYPESCRIPT,
    ".mjs": TYPESCRIPT,
    ".cjs": TYPESCRIPT,
}
MIN_SCOPE_LINES = 5
CONTROL = frozenset(
    {"if", "for", "while", "switch", "catch", "try", "synchronized", "with", "else"}
)
REGEX_PRECEDERS = frozenset("(,=:[!&|?{};")
_TYPE = re.compile(r"\b(?:class|interface|enum|record|namespace|module)\s+[A-Za-z_$]")
_FUNCTION = re.compile(r"\bfunction\b\s*\*?\s*([A-Za-z_$][\w$]*)?")
_TS_METHOD = re.compile(
    r"^(?:(?:public|private|protected|static|readonly|async|override|abstract|get|set|declare)\s+)*"
    r"\*?\s*([A-Za-z_$][\w$]*)\s*(?:<[^()]*>)?\s*\(\)\s*(?::[^{};=]*)?$"
)
_JAVA_METHOD_TAIL = re.compile(r"^\s*(?:throws\s+[\w$.,\s<>?]+)?$")
_DECORATOR = re.compile(r"^(?:@[\w$.]+(?:\(\))?\s*)+")
_BINDING = re.compile(r"(?:(?:const|let|var)\s+|^|[,{]\s*)([A-Za-z_$][\w$]*)\s*[:=]")
_ANNOTATION = re.compile(r"@[\w$.]+\s*$")
_SPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class Scope:
    """A function-like region: 1-based inclusive lines, its name and a short signature."""

    name: str
    kind: str
    start: int
    end: int
    signature: str

    @property
    def length(self) -> int:
        return self.end - self.start + 1


def language_of(path: str) -> str | None:
    """Language of ``path`` by extension, or ``None`` when unsupported."""
    return LANGUAGES.get(PurePosixPath(path).suffix.lower())


def mask(source: str, language: str) -> str:
    """Replace comments, string contents and regular expressions by spaces, keeping offsets."""
    out = list(source)
    n = len(source)
    i = 0

    def blank(start: int, end: int) -> None:
        for k in range(start, min(end, n)):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        char = source[i]
        two = source[i : i + 2]
        if two == "//":
            end = source.find("\n", i)
            end = n if end == -1 else end
            blank(i, end)
            i = end
        elif two == "/*":
            end = source.find("*/", i + 2)
            end = n if end == -1 else end + 2
            blank(i, end)
            i = end
        elif language == JAVA and source.startswith('"""', i):
            end = source.find('"""', i + 3)
            end = n if end == -1 else end + 3
            blank(i + 3, end - 3)
            i = end
        elif char in "\"'":
            end = _closing_quote(source, i)
            if end is None:
                i += 1
            else:
                blank(i + 1, end)
                i = end + 1
        elif char == "`" and language == TYPESCRIPT:
            end = _skip_template(source, i)
            blank(i + 1, end - 1)
            i = end
        elif char == "/" and language == TYPESCRIPT and _starts_regex(source, i):
            end = _regex_end(source, i)
            if end is None:
                i += 1
            else:
                blank(i + 1, end)
                i = end + 1
        else:
            i += 1
    return "".join(out)


def _closing_quote(source: str, start: int) -> int | None:
    quote = source[start]
    i = start + 1
    while i < len(source) and source[i] != "\n":
        if source[i] == "\\":
            i += 2
            continue
        if source[i] == quote:
            return i
        i += 1
    return None


def _skip_template(source: str, start: int) -> int:
    """Index just after the template literal starting at ``start``."""
    i = start + 1
    while i < len(source):
        char = source[i]
        if char == "\\":
            i += 2
        elif char == "`":
            return i + 1
        elif source.startswith("${", i):
            i = _skip_substitution(source, i + 2)
        else:
            i += 1
    return len(source)


def _skip_substitution(source: str, start: int) -> int:
    depth = 1
    i = start
    while i < len(source) and depth:
        char = source[i]
        if char == "`":
            i = _skip_template(source, i)
            continue
        if char in "\"'":
            end = _closing_quote(source, i)
            i = (end + 1) if end is not None else i + 1
            continue
        depth += char == "{"
        depth -= char == "}"
        i += 1
    return i


def _starts_regex(source: str, index: int) -> bool:
    if source.startswith(("//", "/*"), index):
        return False
    j = index - 1
    while j >= 0 and source[j] in " \t":
        j -= 1
    if j < 0 or source[j] == "\n":
        return True
    if source[j] in REGEX_PRECEDERS:
        return True
    word = re.search(r"[A-Za-z_$][\w$]*$", source[: j + 1])
    return bool(word and word.group(0) in {"return", "typeof", "case", "in", "of"})


def _regex_end(source: str, start: int) -> int | None:
    i = start + 1
    in_class = False
    while i < len(source) and source[i] != "\n":
        char = source[i]
        if char == "\\":
            i += 2
            continue
        if char == "[":
            in_class = True
        elif char == "]":
            in_class = False
        elif char == "/" and not in_class:
            return i
        i += 1
    return None


@dataclass
class _Block:
    kind: str
    name: str
    start_offset: int
    signature: str
    in_group: bool = False
    resume: int = 0


@dataclass
class _Group:
    had_block: bool = False
    annotation: bool = False


def find_scopes(source: str, language: str) -> list[Scope]:
    """Every function-like block of ``source``, outermost first."""
    masked = mask(source, language)
    line_starts = [0] + [m.end() for m in re.finditer("\n", masked)]

    def line_of(offset: int) -> int:
        return bisect.bisect_right(line_starts, offset)

    scopes: list[Scope] = []
    blocks: list[_Block] = []
    groups: list[_Group] = []
    saved: list[list[_Group]] = []
    boundary = 0
    for index, char in enumerate(masked):
        if char in "([":
            annotation = char == "(" and bool(
                _ANNOTATION.search(masked[max(0, index - 80) : index])
            )
            groups.append(_Group(annotation=annotation))
        elif char in ")]":
            if groups:
                group = groups.pop()
                if group.had_block:
                    if groups:
                        groups[-1].had_block = True
                    if not group.annotation and not _continues_signature(
                        masked, index + 1, language
                    ):
                        boundary = index + 1
        elif char == "{":
            if groups:
                blocks.append(_Block("plain", "", index, "", in_group=True, resume=boundary))
            else:
                header = masked[boundary:index]
                kind, name, signature, skip = _classify(header, source[boundary:index], language)
                trimmed = header[skip:]
                begin = boundary + skip + (len(trimmed) - len(trimmed.lstrip()))
                blocks.append(_Block(kind, name, begin, signature))
            saved.append(groups)
            groups = []
            boundary = index + 1
        elif char == "}":
            if not blocks:
                continue
            block = blocks.pop()
            groups = saved.pop()
            if block.kind not in ("plain", "type"):
                scopes.append(
                    Scope(
                        block.name,
                        block.kind,
                        line_of(block.start_offset),
                        line_of(index),
                        block.signature,
                    )
                )
            if block.in_group:
                boundary = block.resume
                if groups:
                    groups[-1].had_block = True
            else:
                boundary = index + 1
        elif char == ";" and not groups:
            boundary = index + 1
    return sorted(scopes, key=lambda s: (s.start, -s.end))


def _continues_signature(masked: str, offset: int, language: str) -> bool:
    """Whether what follows a closed parenthesis group still belongs to the same declaration."""
    rest = masked[offset : offset + 40].lstrip()
    if rest[:1] in ("{", "=", ":"):
        return True
    return language == JAVA and rest.startswith("throws")


def _flatten(header: str) -> tuple[str, list[tuple[int, int]]]:
    """Collapse outermost parenthesis groups to ``()``; also return their spans in ``header``."""
    result: list[str] = []
    spans: list[tuple[int, int]] = []
    depth = 0
    opened = 0
    for index, char in enumerate(header):
        if char == "(":
            if depth == 0:
                opened = index
                result.append("(")
            depth += 1
        elif char == ")" and depth:
            depth -= 1
            if depth == 0:
                result.append(")")
                spans.append((opened, index))
        elif depth == 0:
            result.append(char)
    return "".join(result), spans


def _member_start(header: str) -> int:
    """Offset of the last member of an object literal: after its last top-level comma."""
    depth = 0
    cut = 0
    for index, char in enumerate(header):
        if char in "([<":
            depth += 1
        elif char in ")]":
            depth = max(0, depth - 1)
        elif char == ">" and header[index - 1 : index] != "=":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            cut = index + 1
    return cut


def _classify(header: str, original: str, language: str) -> tuple[str, str, str, int]:
    """Return ``(kind, name, signature, skip)`` for the block opened after ``header``.

    ``skip`` is the number of leading characters of ``header`` that belong to a previous member.
    """
    skip = _member_start(header) if language == TYPESCRIPT else 0
    header, original = header[skip:], original[skip:]
    flat, spans = _flatten(header)
    flat = _SPACE.sub(" ", flat).strip()
    if not flat or flat.endswith(("->", "=", ",", ":", "?", "(", "return")):
        return "plain", "", "", skip
    if _TYPE.search(flat):
        return "type", "", "", skip
    first = flat.split(" ", 1)[0].split("(", 1)[0]
    if first in CONTROL and (first != "synchronized" or re.match(r"synchronized\s*\(", flat)):
        return "plain", "", "", skip
    if language == JAVA:
        result = _classify_java(flat, header, original, spans)
    else:
        result = _classify_typescript(flat, header, original, spans)
    return (*result, skip)


def _classify_java(
    flat: str, header: str, original: str, spans: list[tuple[int, int]]
) -> tuple[str, str, str]:
    if flat == "static":
        return "initializer", "static", "static {}"
    if "->" in flat or not spans:
        return "plain", "", ""
    open_at, close_at = spans[-1]
    tail = header[close_at + 1 :]
    before = header[:open_at].rstrip()
    match = re.search(r"([A-Za-z_$][\w$]*)$", before)
    if not match or not _JAVA_METHOD_TAIL.match(tail) or match.group(1) in CONTROL:
        return "plain", "", ""
    if re.search(r"\bnew\s+[\w$.<>?,\s]*$", before):
        return "type", "", ""
    if before.endswith("."):
        return "plain", "", ""
    name = match.group(1)
    return "method", name, _signature(name, original[open_at : close_at + 1])


def _classify_typescript(
    flat: str, header: str, original: str, spans: list[tuple[int, int]]
) -> tuple[str, str, str]:
    params = original[spans[0][0] : spans[0][1] + 1] if spans else ""
    if flat.endswith("=>"):
        name = _binding_name(header) or "<arrow>"
        return "arrow", name, _signature(name, params)
    function = _FUNCTION.search(flat)
    if function and spans:
        name = function.group(1) or _binding_name(header) or "<anonymous>"
        return "function", name, _signature(name, params)
    method = _TS_METHOD.match(_DECORATOR.sub("", flat))
    if method and spans and method.group(1) not in CONTROL:
        name = method.group(1)
        kind = "constructor" if name == "constructor" else "method"
        return kind, name, _signature(name, params)
    return "plain", "", ""


def _binding_name(header: str) -> str | None:
    """Name a function value is bound to: ``const NAME =``, ``NAME:`` or ``NAME =``."""
    matches = _BINDING.findall(_SPACE.sub(" ", header))
    return matches[-1] if matches else None


def _signature(name: str, params: str) -> str:
    text = _SPACE.sub(" ", f"{name}{params}").strip()
    return text if len(text) <= 100 else text[:97] + "..."


def enclosing_scope(scopes: list[Scope], line: int) -> Scope | None:
    """Innermost scope containing ``line``; a very short one yields to its enclosing scope."""
    containing = [s for s in scopes if s.start <= line <= s.end]
    if not containing:
        return None
    ordered = sorted(containing, key=lambda s: s.length)
    for index, scope in enumerate(ordered):
        if scope.length >= MIN_SCOPE_LINES or index == len(ordered) - 1:
            return scope
    return ordered[-1]
