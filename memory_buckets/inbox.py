"""``global/inbox.md``: memory waiting to be sorted.

It holds general unsorted memory: entries from ``hermes memory-buckets import``,
built-in memory writes mirrored by the provider's ``on_memory_write`` bridge,
facts the agent couldn't place, and facts for a project the session can't write
(which the agent then proposes). It's a standing file: sorting empties it, but
nothing needs to delete it.

Appends go under a ``## <heading>`` section, one bullet per entry, so a fact
added in conversation never lands under an "imported USER.md" heading.
"""

from __future__ import annotations

from datetime import date
from typing import Iterable, List, Optional

from . import frontmatter
from .store import GLOBAL, Store, StoreError, check_content, stamp

PATH = f"{GLOBAL}/inbox.md"
DESCRIPTION = "Memory waiting to be sorted, from imports and facts with no clear home yet"


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


def conversation_heading(platform: str = "") -> str:
    return f"added in conversation {date.today().isoformat()}" + (f" ({platform})" if platform else "")


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

    return store.update(PATH, edit)["version"]
