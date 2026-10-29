import re
from dataclasses import dataclass

MAX_CHARS = 500
TARGET_CHARS = 400
OVERLAP_CHARS = 80
KEY_CLAUSE_MARKER = "【关键条款】"
PATH_SEPARATOR = " › "

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_TABLE_SEPARATOR = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")


@dataclass(frozen=True)
class Chunk:
    category: str
    questions: str
    answer: str
    section_path: str | None
    content_type: str
    is_key_clause: bool
    order: int


@dataclass
class _Section:
    path: tuple[str, ...]
    body: list[str]


def split_markdown(
    text: str,
    *,
    content_type: str,
    doc_title: str | None = None,
    max_chars: int = MAX_CHARS,
    target_chars: int = TARGET_CHARS,
    overlap_chars: int = OVERLAP_CHARS,
) -> list[Chunk]:
    title = doc_title or _first_h1(text) or "知识库"
    sections = _sections(text, title)
    chunks: list[Chunk] = []
    for section in sections:
        body = "\n".join(section.body).strip()
        if not body:
            continue
        key_clause = KEY_CLAUSE_MARKER in body
        body = body.replace(KEY_CLAUSE_MARKER, "").strip()
        if not body:
            continue
        path = section.path
        category = PATH_SEPARATOR.join(path[:-1]) or title
        section_path = PATH_SEPARATOR.join(path)
        for answer in _split_body(body, max_chars, target_chars, overlap_chars):
            chunks.append(
                Chunk(
                    category=category,
                    questions=path[-1],
                    answer=answer,
                    section_path=section_path,
                    content_type=content_type,
                    is_key_clause=key_clause,
                    order=len(chunks),
                )
            )
    return chunks


def _first_h1(text: str) -> str | None:
    in_fence = False
    for line in text.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _HEADING.match(line)
        if match and len(match.group(1)) == 1:
            return match.group(2).strip()
    return None


def _sections(text: str, doc_title: str) -> list[_Section]:
    sections: list[_Section] = []
    stack: list[tuple[int, str]] = []
    current: _Section | None = None
    in_fence = False
    for line in text.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            if current is not None:
                current.body.append(line)
            continue
        match = None if in_fence else _HEADING.match(line)
        if match:
            if current is not None:
                sections.append(current)
            level = len(match.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, match.group(2).strip()))
            current = _Section(path=tuple(item[1] for item in stack), body=[])
            continue
        if current is None:
            current = _Section(path=(doc_title,), body=[])
        current.body.append(line)
    if current is not None:
        sections.append(current)
    return sections


def _split_body(body: str, max_chars: int, target_chars: int, overlap_chars: int) -> list[str]:
    if len(body) <= max_chars:
        return [body]
    chunks: list[str] = []
    for kind, content in _segments(body):
        if kind == "table":
            chunks.extend(_split_table(content, max_chars))
        elif kind == "text":
            chunks.extend(_split_text(content, max_chars, target_chars, overlap_chars))
        else:
            chunks.extend(_split_fence(content, max_chars))
    return [item for item in chunks if item.strip()]


def _segments(body: str) -> list[tuple[str, str]]:
    lines = body.splitlines()
    segments: list[tuple[str, str]] = []
    buffer: list[str] = []
    index = 0

    def flush_text() -> None:
        if buffer:
            text = "\n".join(buffer).strip()
            if text:
                segments.append(("text", text))
            buffer.clear()

    while index < len(lines):
        line = lines[index]
        if _FENCE.match(line):
            flush_text()
            fence = [line]
            index += 1
            while index < len(lines):
                fence.append(lines[index])
                if _FENCE.match(lines[index]):
                    index += 1
                    break
                index += 1
            segments.append(("fence", "\n".join(fence)))
            continue
        if _is_table_start(lines, index):
            flush_text()
            table = []
            while index < len(lines) and lines[index].lstrip().startswith("|"):
                table.append(lines[index])
                index += 1
            segments.append(("table", "\n".join(table)))
            continue
        buffer.append(line)
        index += 1
    flush_text()
    return segments


def _is_table_start(lines: list[str], index: int) -> bool:
    if not lines[index].lstrip().startswith("|") or index + 1 >= len(lines):
        return False
    return bool(_TABLE_SEPARATOR.match(lines[index + 1]))


def _split_table(block: str, max_chars: int) -> list[str]:
    lines = block.strip().splitlines()
    if len(lines) <= 2:
        return [block.strip()]
    header, rows = lines[:2], lines[2:]
    chunks: list[str] = []
    current = list(header)
    for row in rows:
        candidate = current + [row]
        if len("\n".join(candidate)) > max_chars and len(current) > 2:
            chunks.append("\n".join(current))
            current = header + [row]
        else:
            current = candidate
    if len(current) > 2:
        chunks.append("\n".join(current))
    return chunks


def _split_fence(block: str, max_chars: int) -> list[str]:
    if len(block) <= max_chars:
        return [block]
    lines = block.splitlines()
    chunks: list[str] = []
    current: list[str] = []
    for line in lines:
        if current and len("\n".join(current + [line])) > max_chars:
            chunks.append("\n".join(current))
            current = []
        current.append(line)
    if current:
        chunks.append("\n".join(current))
    return chunks


def _split_text(text: str, max_chars: int, target_chars: int, overlap_chars: int) -> list[str]:
    units = _hard_split(_units(text), max_chars)
    chunks: list[str] = []
    start = 0
    while start < len(units):
        length = 0
        end = start
        while end < len(units):
            size = len(units[end])
            if end > start and length + size > max_chars:
                break
            length += size
            end += 1
            if length >= target_chars:
                break
        piece = "".join(units[start:end]).strip()
        if piece:
            chunks.append(piece)
        if end >= len(units):
            break
        overlap_start = end
        overlap = 0
        while overlap_start > start and overlap + len(units[overlap_start - 1]) <= overlap_chars:
            overlap += len(units[overlap_start - 1])
            overlap_start -= 1
        start = overlap_start if overlap_start > start else end
    return chunks


def _units(text: str) -> list[str]:
    units: list[str] = []
    start = 0
    for index, char in enumerate(text):
        if char in "。！？!?；;":
            units.append(text[start : index + 1])
            start = index + 1
        elif char == "\n":
            units.append(text[start : index + 1])
            start = index + 1
    if start < len(text):
        units.append(text[start:])
    return units


def _hard_split(units: list[str], max_chars: int) -> list[str]:
    result: list[str] = []
    for unit in units:
        if len(unit) <= max_chars:
            result.append(unit)
            continue
        for index in range(0, len(unit), max_chars):
            result.append(unit[index : index + max_chars])
    return result
