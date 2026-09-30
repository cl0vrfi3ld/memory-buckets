"""Frontmatter: a restricted YAML subset, since stdlib has no YAML.

Understood, per key:

- ``key: value``: a scalar. Bare, ``"double"`` (``\\"`` and ``\\\\`` escapes) or
  ``'single'`` (``''`` escape) quoted.
- ``key: [a, "b, c"]``: an inline list of scalars.
- ``key:`` followed by indented ``- item`` lines: a block list.
- ``key:`` alone: an empty string.

Anything else under a key (nested maps, multi-line strings, comments) is kept as
raw lines. Its value reads as ``None``, and it's re-emitted verbatim unless the
key is set. Key order is always preserved. Line endings are normalised to LF.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Union

Value = Union[str, List[str], None]

_KEY_RE = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_-]*):(?:[ \t]+(.*?))?[ \t]*$")
_BLOCK_ITEM_RE = re.compile(r"^[ \t]+-(?:[ \t]+(.*?))?[ \t]*$")
_NEEDS_QUOTES_RE = re.compile(r"""^[\s\-?:,\[\]{}#&*!|>'"%@`]|: | #|\s$|^$""")
_YAML_WORDS = {"true", "false", "yes", "no", "on", "off", "null", "~"}


class FrontmatterError(ValueError):
    pass


@dataclass
class Entry:
    key: str
    value: Value
    raw: Optional[List[str]] = None  # original lines; None once the value is set


@dataclass
class Frontmatter:
    entries: List[Entry] = field(default_factory=list)

    def keys(self) -> List[str]:
        return [e.key for e in self.entries]

    def get(self, key: str, default: Value = None) -> Value:
        for e in self.entries:
            if e.key == key:
                return e.value
        return default

    def __contains__(self, key: str) -> bool:
        return any(e.key == key for e in self.entries)

    def set(self, key: str, value: Value) -> None:
        for e in self.entries:
            if e.key == key:
                if e.value != value or e.raw is None:
                    e.value, e.raw = value, None
                return
        self.entries.append(Entry(key, value))

    def pairs(self) -> List[Tuple[str, Value]]:
        return [(e.key, e.value) for e in self.entries]


def _unquote(text: str) -> str:
    if len(text) >= 2 and text[0] == text[-1] == '"':
        return re.sub(r'\\(["\\])', r"\1", text[1:-1])
    if len(text) >= 2 and text[0] == text[-1] == "'":
        return text[1:-1].replace("''", "'")
    return text


def _split_inline(inner: str) -> Optional[List[str]]:
    items, buf, quote = [], "", None
    for ch in inner:
        if quote:
            buf += ch
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            buf += ch
        elif ch == ",":
            items.append(buf.strip())
            buf = ""
        elif ch in "[]{}":
            return None  # nested structure: not ours
        else:
            buf += ch
    if quote:
        return None
    if buf.strip() or items:
        items.append(buf.strip())
    if any(not item for item in items):
        return None
    return [_unquote(item) for item in items]


def _interpret(rest: Optional[str], continuation: List[str]) -> Tuple[bool, Value]:
    """(understood, value) for one key's text."""
    if continuation:
        if rest:
            return False, None
        items = []
        for line in continuation:
            if not line.strip():
                continue
            m = _BLOCK_ITEM_RE.match(line)
            if not m or m.group(1) is None:
                return False, None
            items.append(_unquote(m.group(1)))
        return True, items
    if rest is None or rest == "":
        return True, ""
    if rest.startswith("["):
        if not rest.endswith("]"):
            return False, None
        items = _split_inline(rest[1:-1])
        return (items is not None), items
    if rest[0] in "\"'":
        if len(rest) < 2 or rest[-1] != rest[0]:
            return False, None
        return True, _unquote(rest)
    if rest[0] in "{|>&*!" or " #" in rest:
        return False, None
    return True, rest


def split(text: str) -> Tuple[Optional[List[str]], str]:
    """(frontmatter lines or None, body). ``text`` must already be LF-normalised."""
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---\n", 3)
    if end == -1:
        if text.endswith("\n---"):
            return text[4:-4].split("\n") if len(text) > 8 else [], ""
        raise FrontmatterError("frontmatter has no closing '---'")
    block = text[4:end]
    return (block.split("\n") if block else []), text[end + 5:]


def parse(text: str) -> Tuple[Frontmatter, str]:
    """Parse ``text`` into (frontmatter, body). A file without frontmatter gives
    an empty ``Frontmatter`` and the whole text as the body."""
    text = text.replace("\r\n", "\n")
    lines, body = split(text)
    fm = Frontmatter()
    if lines is None:
        return fm, body
    current: Optional[Tuple[str, Optional[str], List[str], List[str]]] = None

    def flush():
        if current is None:
            return
        key, rest, cont, raw = current
        ok, value = _interpret(rest, cont)
        fm.entries.append(Entry(key, value if ok else None, raw))

    for line in lines:
        m = _KEY_RE.match(line)
        if m and not line[0].isspace():
            flush()
            if m.group(1) in fm:
                raise FrontmatterError(f"duplicate frontmatter key {m.group(1)!r}")
            current = (m.group(1), m.group(2), [], [line])
        elif current is not None and (not line.strip() or line[0].isspace()):
            current[2].append(line)
            current[3].append(line)
        elif not line.strip():
            continue
        else:
            raise FrontmatterError(f"can't parse frontmatter line {line!r}")
    flush()
    return fm, body


def _scalar(value: str) -> str:
    if _NEEDS_QUOTES_RE.search(value) or value.lower() in _YAML_WORDS or "\n" in value:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'
    return value


def render(fm: Frontmatter, body: str) -> str:
    lines = ["---"]
    for e in fm.entries:
        if e.raw is not None:
            lines.extend(e.raw)
        elif isinstance(e.value, list):
            if e.value:
                lines.append(f"{e.key}:")
                lines.extend(f"  - {_scalar(str(item))}" for item in e.value)
            else:
                lines.append(f"{e.key}: []")
        elif e.value is None or e.value == "":
            lines.append(f"{e.key}:")
        else:
            lines.append(f"{e.key}: {_scalar(str(e.value))}")
    lines.append("---")
    return "\n".join(lines) + "\n" + body


def validate(fm: Frontmatter, stem: str) -> None:
    """Raise FrontmatterError unless ``name`` == ``stem`` and ``description`` is one line."""
    name, description = fm.get("name"), fm.get("description")
    if not isinstance(name, str) or not name:
        raise FrontmatterError("frontmatter needs a 'name'")
    if name != stem:
        raise FrontmatterError(f"frontmatter name {name!r} must match the file name {stem!r}")
    if not isinstance(description, str) or not description.strip():
        raise FrontmatterError("frontmatter needs a one-line 'description' saying when to read the file")
    for key in ("aliases", "sources"):
        if key in fm and not isinstance(fm.get(key), list):
            raise FrontmatterError(f"frontmatter {key!r} must be a list")
