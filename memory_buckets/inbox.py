"""``global/inbox.md``: imported memory waiting to be sorted.

It holds entries from ``hermes memory-buckets import`` and built-in memory writes
mirrored by the provider's ``on_memory_write`` bridge. The agent never appends to
it (``memory_append`` refuses the path); the sort-imports skill removes filed lines
with ``memory_str_replace``, ``memory_write`` or ``memory_delete``. It's a standing
file, but a deleted one is fine: the next ``append`` (import or mirrored built-in
write) recreates it with ``DESCRIPTION``. The store migration removes the headings
a sort empties; the skill leaves them for it.

Appends go under a ``## <heading>`` section, one bullet per entry, so entries from
different imports stay apart.
"""

from __future__ import annotations

from typing import Iterable, List, Optional, Tuple

from . import frontmatter
from .store import GLOBAL, Store, StoreError, check_content, stamp

PATH = f"{GLOBAL}/inbox.md"
DESCRIPTION = "Imported memory waiting to be sorted"


def _bullet(entry: str) -> str:
    text = " ".join(entry.split())
    return text if text.startswith(("- ", "* ")) else f"- {text}"


def bullets(entries: Iterable[str]) -> List[str]:
    return [_bullet(e) for e in entries if e and e.strip()]


def _fence(line: str) -> Optional[str]:
    """The fence marker (``` or ~~~) that ``line`` opens or closes, if any."""
    stripped = line.lstrip()
    for marker in ("```", "~~~"):
        if stripped.startswith(marker):
            return marker
    return None


def _outside_fences(body: str) -> List[str]:
    """``body``'s lines that aren't inside a fenced code block."""
    out, open_fence = [], None
    for line in body.split("\n"):
        marker = _fence(line)
        if open_fence is None and marker:
            open_fence = marker
        elif open_fence is not None and marker == open_fence:
            open_fence = None
        elif open_fence is None:
            out.append(line)
    return out


def count_entries(body: str) -> int:
    """How many entries (top-level bullets, outside code fences) the inbox body holds."""
    return sum(1 for line in _outside_fences(body) if line.startswith(("- ", "* ")))


def _empty_headings(body: str) -> List[Tuple[int, str]]:
    """(line number, line) for each ``## `` heading with no non-blank lines before
    the next ``## `` heading or the end of the body. Raw lines: a section holding
    only a fenced block counts as content, since dropping its heading would turn
    the fenced lines into plain ones. A ``## `` line inside a fence is content, not
    a heading, as in every other heading lookup here."""
    found, heading_at, heading, blank, open_fence = [], None, None, False, None
    for i, line in enumerate(body.split("\n")):
        marker = _fence(line)
        if open_fence is None and marker:
            open_fence, blank = marker, False  # a fence line is content
        elif open_fence is not None:
            if marker == open_fence:
                open_fence = None
            if line.strip():
                blank = False
        elif line.startswith("## "):
            if heading is not None and blank:
                found.append((heading_at, heading))
            heading_at, heading, blank = i, line, True
        elif line.strip():
            blank = False
    if heading is not None and blank:
        found.append((heading_at, heading))
    return found


def empty_headings(body: str) -> List[str]:
    """The ``## `` headings that ``_empty_headings`` finds."""
    return [line for _, line in _empty_headings(body)]


def without_empty_sections(body: str) -> str:
    """``body`` without the sections ``empty_headings`` finds: each heading line and
    the blank lines that follow it (the section's own) go; everything else stays
    where it is."""
    drop = {i for i, _ in _empty_headings(body)}
    if not drop:
        return body
    out, skipping = [], False
    for i, line in enumerate(body.split("\n")):
        if i in drop:
            skipping = True
        elif skipping and not line.strip():
            continue
        else:
            skipping = False
            out.append(line)
    text = "\n".join(out).rstrip("\n")
    return text + "\n" if text.strip() else ""


def with_entries(current: Optional[bytes], heading: str, lines: List[str], source: str) -> str:
    """The inbox with ``lines`` (already bullets) appended under ``## heading``,
    reusing that section when it's the last one. ``current`` None creates the file.
    An inbox written by hand without frontmatter gets the name and description it
    needs, rather than refusing every append (and every mirrored built-in write).
    Raises ``FrontmatterError`` for frontmatter that can't be parsed."""
    if current is None:
        fm, body = frontmatter.Frontmatter(), ""
    else:
        fm, body = frontmatter.parse(current.decode("utf-8", errors="replace"))
    if not fm.get("name"):
        fm.set("name", "inbox")
    if not fm.get("description"):
        fm.set("description", DESCRIPTION)
    raw_sources = fm.get("sources")
    sources = list(raw_sources) if isinstance(raw_sources, list) else []
    if source and source not in sources:
        fm.set("sources", [*sources, source])
    stamp(fm)
    header = f"## {heading}"
    last_header = next((line for line in reversed(_outside_fences(body)) if line.startswith("## ")), None)
    if last_header != header:
        body = (body.rstrip("\n") + "\n\n" if body.strip() else "") + header + "\n"
    elif not body.endswith("\n"):
        body += "\n"
    body += "\n".join(lines) + "\n"
    return frontmatter.render(fm, body)


def append(store: Store, heading: str, entries: Iterable[str], source: str) -> Optional[str]:
    """Append ``entries`` under ``## heading``. Returns the new version, or None if
    there was nothing to add."""
    lines = bullets(entries)
    if not lines:
        return None

    def edit(current: Optional[bytes]) -> bytes:
        try:
            return check_content(PATH, with_entries(current, heading, lines, source))
        except frontmatter.FrontmatterError as err:
            raise StoreError("invalid_frontmatter", f"{PATH}: can't parse its frontmatter ({err}); "
                                                    "fix it by hand before adding to it") from None

    return store.update(PATH, edit, record_history=False)["version"]
