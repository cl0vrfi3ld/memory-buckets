"""``global/inbox.md``: imported memory waiting to be sorted.

Used by ``hermes memory-buckets import`` and by the provider's ``on_memory_write`` bridge,
which mirrors built-in memory writes while built-in memory is still on.
Appends go under a ``## <heading>`` section, one bullet per entry.
"""

from __future__ import annotations

from typing import Iterable, Optional

from . import frontmatter
from .store import GLOBAL, Store, check_content, stamp

PATH = f"{GLOBAL}/inbox.md"
DESCRIPTION = "Imported memory waiting to be sorted into proper files (migration only); sort it, then delete it"


def _bullet(entry: str) -> str:
    text = " ".join(entry.split())
    return text if text.startswith("- ") else f"- {text}"


def append(store: Store, heading: str, entries: Iterable[str], source: str) -> Optional[str]:
    """Append ``entries`` under ``## heading`` (reusing the section if it's the
    last one). Returns the new version, or None if there was nothing to add."""
    bullets = [_bullet(e) for e in entries if e and e.strip()]
    if not bullets:
        return None

    def edit(current: Optional[bytes]) -> bytes:
        if current is None:
            fm = frontmatter.Frontmatter()
            fm.set("name", "inbox")
            fm.set("description", DESCRIPTION)
            body = ""
        else:
            fm, body = frontmatter.parse(current.decode("utf-8", errors="replace"))
        sources = fm.get("sources") if isinstance(fm.get("sources"), list) else []
        if source not in sources:
            fm.set("sources", [*sources, source])
        stamp(fm)
        header = f"## {heading}"
        last_header = next((line for line in reversed(body.split("\n")) if line.startswith("## ")), None)
        if last_header != header:
            body = (body.rstrip("\n") + "\n\n" if body.strip() else "") + header + "\n"
        elif not body.endswith("\n"):
            body += "\n"
        body += "\n".join(bullets) + "\n"
        return check_content(PATH, frontmatter.render(fm, body))

    return store.update(PATH, edit)["version"]
