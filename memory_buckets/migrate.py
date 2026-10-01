"""Store migrations. The provider runs them when it starts (unless the session is
read-only), and ``hermes memory-buckets migrate`` runs them on demand.

Each migration is idempotent and loses nothing. It changes files only under the
store lock, and before it rewrites or removes a file it keeps the original under
``<store>/_backup/<version>/``, which sits outside ``memories/``. A file it can't
read or parse is a ``skip`` step: it stays as it is, and the other files still migrate.

0.1.0:

- ``<project>/index.md`` is no longer a memory path, because the project index is
  now generated into the prompt. A leftover one becomes the project's ``profile.md``
  if there isn't one. Otherwise its body is appended to ``profile.md``, verbatim,
  under a heading. Then ``index.md`` is removed.
- ``global/inbox.md`` is now a standing file, and the plugin owns its description.
  Any other description (the 0.0.1 default said "… sort it, then delete it")
  becomes the current one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from . import frontmatter, inbox
from .store import GLOBAL, Store, StoreError, _fsync_dir, check_content, is_project_id, stamp, version

logger = logging.getLogger("memory_buckets")

BACKUP_DIR = "_backup"
INDEX_MIGRATION = "0.1.0"
SOURCE = "migration"
MOVED_HEADING = "## Moved from index.md"
DEFAULT_PROFILE_DESCRIPTION = "What the {p} project is, its purpose, technology and status, and where things are"


@dataclass
class Step:
    project: str
    action: str  # rename | merge | remove | skip | inbox
    detail: str = ""

    def describe(self) -> str:
        p = self.project
        if self.action == "rename":
            return f"{p}/index.md becomes {p}/profile.md"
        if self.action == "merge":
            return f"{p}/index.md: {self.detail} appended to {p}/profile.md, then index.md removed"
        if self.action == "remove":
            return f"{p}/index.md removed ({self.detail})"
        if self.action == "inbox":
            return f"{inbox.PATH}: description set to the current one (was: {self.detail})"
        if p == GLOBAL:
            return f"{inbox.PATH} left as it is: {self.detail}"
        return f"{p}/index.md left in place: {self.detail}"


def backup_dir(store: Store, version: str = INDEX_MIGRATION) -> Path:
    return store.root / BACKUP_DIR / version


def leftover_indexes(store: Store) -> List[str]:
    """Projects that still have a regular ``index.md`` file."""
    if not store.memories.is_dir():
        return []
    found = []
    for d in sorted(store.memories.iterdir()):
        if is_project_id(d.name) and d.is_dir() and not d.is_symlink():
            index = d / "index.md"
            if index.is_file() and not index.is_symlink():
                found.append(d.name)
    return found


def _add_source(fm: frontmatter.Frontmatter) -> None:
    sources = fm.get("sources")
    sources = list(sources) if isinstance(sources, list) else []
    if SOURCE not in sources:
        fm.set("sources", [*sources, SOURCE])


def _reason(err: Exception) -> str:
    return getattr(err, "message", None) or str(err)


def _plan(store: Store, project: str) -> Tuple[Step, Optional[bytes]]:
    """What to do with ``<project>/index.md``, and the new ``profile.md`` bytes if it changes.
    Never raises for a bad file: that's a ``skip`` step."""
    profile = f"{project}/profile.md"
    try:
        text = (store.memories / project / "index.md").read_bytes().decode("utf-8", errors="replace")
        current = store.read_bytes(profile)  # refuses a symlink; a directory raises OSError
    except (OSError, StoreError) as err:
        return Step(project, "skip", f"can't read it or {profile} ({_reason(err)}); move its content by hand"), None
    try:
        fm, body = frontmatter.parse(text)
    except frontmatter.FrontmatterError as err:
        return Step(project, "skip", f"can't parse its frontmatter ({err}); fix it, then run "
                                     "`hermes memory-buckets migrate`"), None
    block = body.strip("\n")
    try:
        if current is None:
            if not block.strip():
                return Step(project, "remove", "it was empty"), None
            fm.set("name", "profile")
            description = fm.get("description")
            if not isinstance(description, str) or not description.strip():
                fm.set("description", DEFAULT_PROFILE_DESCRIPTION.format(p=project))
            _add_source(fm)
            stamp(fm)
            return Step(project, "rename"), check_content(profile, frontmatter.render(fm, block + "\n"))

        try:
            pfm, pbody = frontmatter.parse(current.decode("utf-8", errors="replace"))
        except frontmatter.FrontmatterError as err:
            return Step(project, "skip", f"can't parse {profile}'s frontmatter ({err}); fix it, then run "
                                         "`hermes memory-buckets migrate`"), None
        if not block.strip():
            return Step(project, "remove", "it was empty"), None
        if block in pbody:  # also a rerun after a crash between writing profile.md and removing index.md
            return Step(project, "remove", f"{profile} already has all of it"), None
        # Verbatim, never line-by-line: dropping "duplicate" lines can break code fences
        # and headings. A repeated fact is visible and easy to tidy; a lost one isn't.
        pbody = (pbody.rstrip("\n") + "\n\n" if pbody.strip() else "") + MOVED_HEADING + "\n" + block + "\n"
        _add_source(pfm)
        stamp(pfm)
        n = len(block.splitlines())
        return Step(project, "merge", f"{n} line{'' if n == 1 else 's'}"), check_content(
            profile, frontmatter.render(pfm, pbody))
    except StoreError as err:  # too large, or frontmatter that wouldn't validate
        return Step(project, "skip", f"{err.message}; move its content by hand"), None


def _inbox_plan(store: Store) -> Tuple[Optional[Step], Optional[bytes]]:
    """Give the inbox the current description. None: nothing to do.

    The plugin owns the inbox's description, as it owns its name: the prompt's inbox
    note and the sort-inbox skill depend on what it says, and older versions (and
    older skills) wrote descriptions that contradict them. An edited one is replaced
    too; the original is kept in the backup."""
    try:
        current = store.read_bytes(inbox.PATH)
    except (OSError, StoreError) as err:
        return Step(GLOBAL, "skip", f"can't read it ({_reason(err)})"), None
    if current is None:
        return None, None
    try:
        fm, body = frontmatter.parse(current.decode("utf-8", errors="replace"))
    except frontmatter.FrontmatterError:
        return None, None  # lint reports it, and appends refuse it until it's fixed by hand
    was = fm.get("description")
    if was == inbox.DESCRIPTION:
        return None, None
    fm.set("description", inbox.DESCRIPTION)
    if not fm.get("name"):
        fm.set("name", "inbox")
    _add_source(fm)
    stamp(fm)
    try:
        return Step(GLOBAL, "inbox", repr(was) if was else "none"), check_content(inbox.PATH, frontmatter.render(fm, body))
    except StoreError as err:
        return Step(GLOBAL, "skip", err.message), None


def inbox_needs_migrating(store: Store) -> bool:
    step, _ = _inbox_plan(store)
    return step is not None and step.action == "inbox"


def _keep(store: Store, project: str, name: str, data: Optional[bytes]) -> None:
    """Back up an original. A rerun never overwrites an earlier copy: the same bytes are
    already kept, and different bytes (an index.md that came back) go under their version."""
    if data is None:
        return
    target = backup_dir(store) / project / name
    if target.exists():
        if target.read_bytes() == data:
            return
        target = target.with_name(f"{target.stem}-{version(data)}{target.suffix}")
        if target.exists():
            return
    store._atomic_write(target, data)


def _apply(store: Store, project: str, data: Optional[bytes]) -> None:
    index = store.memories / project / "index.md"
    _keep(store, project, "index.md", index.read_bytes())
    if data is not None:
        _keep(store, project, "profile.md", store.read_bytes(f"{project}/profile.md"))
        store._atomic_write(store.resolve(f"{project}/profile.md"), data)
    index.unlink()
    _fsync_dir(index.parent)


def run(store: Store, *, dry_run: bool = False) -> List[Step]:
    """Run every pending migration. Raises ``StoreError("locked")`` if the store stays
    locked. Anything else that goes wrong with one file is a ``skip`` step for that file."""
    if not leftover_indexes(store) and _inbox_plan(store)[0] is None:
        return []
    if dry_run:
        steps = [_plan(store, p)[0] for p in leftover_indexes(store)]
        inbox_step = _inbox_plan(store)[0]
        return steps + ([inbox_step] if inbox_step else [])
    steps = []
    with store.lock():
        for project in leftover_indexes(store):  # again, under the lock
            step, data = _plan(store, project)
            if step.action != "skip":
                try:
                    _apply(store, project, data)
                except (OSError, StoreError) as err:
                    step = Step(project, "skip", f"{_reason(err)}; run `hermes memory-buckets migrate` again")
            steps.append(step)
        step, data = _inbox_plan(store)
        if step is not None and data is not None:
            try:
                _keep(store, GLOBAL, "inbox.md", store.read_bytes(inbox.PATH))
                store._atomic_write(store.resolve(inbox.PATH), data)
            except (OSError, StoreError) as err:
                step = Step(GLOBAL, "skip", _reason(err))
        if step is not None:
            steps.append(step)
    return steps
