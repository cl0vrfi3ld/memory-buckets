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

0.2.0:

- Proposals are gone, and so is ``<store>/_pending/``, where 0.1.0 staged them as
  ``<id>.json``. Each file in it is copied byte for byte to
  ``_backup/0.2.0/_pending/``, then removed, and the empty folder goes last. Nothing
  is applied: the user never confirmed those proposals, and their lines are still in
  the inbox, where the next import sort files them. A file whose copy fails or
  doesn't match stays where it is, and so does ``_pending/``.
- The inbox's lines stay as they are, "added in conversation" sections included.
  Empty ``## `` headings (a heading with no non-blank lines before the next one or
  the end of the file) are removed: they carry nothing, so they are not backed up.
  A headings-only run reports itself and writes no backup.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, NamedTuple, Optional, Tuple

from . import frontmatter, inbox
from .store import GLOBAL, Store, StoreError, _fsync_dir, check_content, is_project_id, stamp, version

logger = logging.getLogger("memory_buckets")

BACKUP_DIR = "_backup"
INDEX_MIGRATION = "0.1.0"
INBOX_MIGRATION = "0.2.0"  # the description change lands with the _pending/ removal
PENDING_MIGRATION = "0.2.0"
PENDING_DIR = "_pending"  # 0.1.0's pending.py PENDING_DIR: <store>/_pending/<id>.json
SOURCE = "migration"
MOVED_HEADING = "## Moved from index.md"
DEFAULT_PROFILE_DESCRIPTION = "What the {p} project is, its purpose, technology and status, and where things are"


@dataclass
class Step:
    project: str  # or PENDING_DIR for the 0.2.0 steps
    action: str  # rename | merge | remove | skip | inbox | pending
    detail: str = ""
    name: str = ""  # PENDING_DIR steps: the file in it, or "" for the folder itself
    removed: str = ""  # inbox step: the empty headings that went in the same run

    def describe(self) -> str:
        p = self.project
        if p == PENDING_DIR:
            where = f"{PENDING_DIR}/{self.name}"
            if self.action == "pending":
                what = f" ({self.detail})" if self.detail else ""
                return f"{where}{what} kept in {BACKUP_DIR}/{PENDING_MIGRATION}/{PENDING_DIR}/ and removed"
            if self.action == "remove":
                return f"{where} removed: {self.detail}"
            return f"{where} left in place: {self.detail}"
        if self.action == "rename":
            return f"{p}/index.md becomes {p}/profile.md"
        if self.action == "merge":
            return f"{p}/index.md: {self.detail} appended to {p}/profile.md, then index.md removed"
        if self.action == "remove":
            return f"{p}/index.md removed ({self.detail})"
        if self.action == "inbox":
            tail = f"; removed {self.removed}" if self.removed else ""
            return f"{inbox.PATH}: description set to the current one (was: {self.detail}){tail}"
        if self.action == "tidy":
            return f"{inbox.PATH}: removed {self.detail}"
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


def leftover_pending(store: Store) -> Optional[List[str]]:
    """What's in a leftover ``_pending/``, sorted. None when there's no ``_pending/``;
    an empty list for an empty folder, or for something there that isn't a folder."""
    folder = store.root / PENDING_DIR
    if not os.path.lexists(folder):
        return None
    if folder.is_symlink() or not folder.is_dir():
        return []
    try:
        return sorted(p.name for p in folder.iterdir())
    except OSError:
        return []


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


class _InboxChange(NamedTuple):
    text: str  # the inbox text after the change
    was: str  # the description it had, as a step shows it ("none" when it had none)
    desc_changed: bool
    changed: bool  # the description or the headings
    headings: List[str]  # the empty ``## `` headings removed


def _inbox_change(text: str) -> _InboxChange:
    """The description set to the current one, then the empty ``## `` headings
    removed, for one inbox text. Raises ``FrontmatterError`` for frontmatter it
    can't parse."""
    fm, body = frontmatter.parse(text)
    was = fm.get("description")
    shown = repr(was) if was else "none"
    desc_changed = was != inbox.DESCRIPTION
    headings = inbox.empty_headings(body)
    if not desc_changed and not headings:
        return _InboxChange(text, shown, False, False, [])
    if not fm.get("name"):
        fm.set("name", "inbox")
    if desc_changed:
        fm.set("description", inbox.DESCRIPTION)
        _add_source(fm)
    stamp(fm)
    return _InboxChange(frontmatter.render(fm, inbox.without_empty_sections(body)),
                        shown, desc_changed, True, headings)


def _inbox_step(change: _InboxChange) -> Step:
    if change.desc_changed:
        return Step(GLOBAL, "inbox", change.was, removed="; ".join(change.headings))
    return Step(GLOBAL, "tidy", "; ".join(change.headings))


def _inbox_plan(store: Store) -> Optional[Step]:
    """The next inbox change: the description, then empty headings. ``None``:
    nothing to do. Found on the current state, like the description step before it,
    so a store that has already migrated gets cleaned on the next writable session
    start, and each run after a sort tidies again."""
    try:
        current = store.read_bytes(inbox.PATH)
    except (OSError, StoreError) as err:
        return Step(GLOBAL, "skip", f"can't read it ({_reason(err)})")
    if current is None:
        return None
    try:
        change = _inbox_change(current.decode("utf-8", errors="replace"))
    except frontmatter.FrontmatterError:
        return None  # lint reports it, and appends refuse it until it's fixed by hand
    if not change.changed:
        return None
    try:
        check_content(inbox.PATH, change.text)
    except StoreError as err:
        return Step(GLOBAL, "skip", err.message)
    return _inbox_step(change)


def _apply_inbox(store: Store) -> Optional[Step]:
    """Apply the next inbox change. The caller holds the store lock — the same one
    the ``on_memory_write`` mirror takes for ``inbox.append`` — so a mirrored write
    lands entirely before this one (and is kept, and tidied) or entirely after, never
    halfway. The text is recomputed from a fresh read under the lock. Only a changed
    description loses anything, so only that is backed up: removing empty headings
    keeps no copy, or ``_backup/0.2.0/global/`` would fill with a near-identical
    inbox on every run after a sort. Frontmatter that can't be parsed is left for
    ``lint``, as in the plan, so the rest of the run still goes ahead."""
    current = store.read_bytes(inbox.PATH)
    if current is None:
        return None  # deleted after the plan (a sort can remove it): nothing to do
    try:
        change = _inbox_change(current.decode("utf-8", errors="replace"))
    except frontmatter.FrontmatterError:
        return None
    if not change.changed:
        return None  # changed after the plan: nothing left to do
    check_content(inbox.PATH, change.text)  # too large: raises, and the step becomes a skip
    if change.desc_changed:
        _keep(store, GLOBAL, "inbox.md", current, INBOX_MIGRATION)
    store._atomic_write(store.resolve(inbox.PATH), change.text.encode("utf-8"))
    return _inbox_step(change)


def inbox_needs_migrating(store: Store) -> bool:
    """Whether ``status`` and ``lint`` should ask for a migration: only an outdated
    description counts. Empty headings are not reported — the next writable session
    clears them, and a note after every sort would be noise."""
    step = _inbox_plan(store)
    return step is not None and step.action == "inbox"


def _keep(store: Store, project: str, name: str, data: Optional[bytes],
          migration: str = INDEX_MIGRATION) -> Optional[Path]:
    """Back up an original, and return where the copy is. A rerun never overwrites an
    earlier copy: the same bytes are already kept, and different bytes (an index.md
    that came back) go under their version."""
    if data is None:
        return None
    target = backup_dir(store, migration) / project / name
    if target.exists():
        if target.read_bytes() == data:
            return target
        target = target.with_name(f"{target.stem}-{version(data)}{target.suffix}")
        if target.exists():
            return target
    store._atomic_write(target, data)
    return target


def _apply(store: Store, project: str, data: Optional[bytes]) -> None:
    index = store.memories / project / "index.md"
    _keep(store, project, "index.md", index.read_bytes())
    if data is not None:
        _keep(store, project, "profile.md", store.read_bytes(f"{project}/profile.md"))
        store._atomic_write(store.resolve(f"{project}/profile.md"), data)
    index.unlink()
    _fsync_dir(index.parent)


RERUN = "run `hermes memory-buckets migrate` again"
FOLDER_GONE = f"its proposals weren't applied. Their lines are still in {inbox.PATH}; /sort-imports files them"


def _proposal_for(path: Path) -> str:
    """"a proposal for <project>", or "" when the file doesn't say (it's copied either way)."""
    try:
        project = json.loads(path.read_bytes()).get("project")
    except (OSError, ValueError, AttributeError):
        return ""
    return f"a proposal for {project}" if isinstance(project, str) and is_project_id(project) else ""


def _pending_plan(store: Store) -> List[Step]:
    """A ``pending`` step per file in ``_pending/``, then a ``remove`` step for the folder
    when nothing in it is a ``skip``. [] when there's no ``_pending/``."""
    folder = store.root / PENDING_DIR
    if not os.path.lexists(folder):
        return []
    if folder.is_symlink() or not folder.is_dir():
        return [Step(PENDING_DIR, "skip", "it isn't a folder. Remove it by hand")]
    try:
        names = sorted(p.name for p in folder.iterdir())
    except OSError as err:
        return [Step(PENDING_DIR, "skip", f"can't list it ({_reason(err)})")]
    steps = []
    for name in names:
        path = folder / name
        if path.is_symlink() or not path.is_file():
            steps.append(Step(PENDING_DIR, "skip", "it isn't a regular file. Move it out by hand, then run "
                                                   "`hermes memory-buckets migrate`", name))
        else:
            steps.append(Step(PENDING_DIR, "pending", _proposal_for(path), name))
    if not any(s.action == "skip" for s in steps):
        steps.append(Step(PENDING_DIR, "remove", FOLDER_GONE if steps else "it was empty"))
    return steps


def _apply_pending(store: Store, planned: List[Step]) -> List[Step]:
    """Copy each file to the backup, check the copy, then remove the original. The folder
    goes only once every file in it has: nothing is removed that wasn't copied."""
    folder = store.root / PENDING_DIR
    steps = []
    for step in planned:
        if step.action == "pending":
            path = folder / step.name
            try:
                data = path.read_bytes()
                kept = _keep(store, PENDING_DIR, step.name, data, PENDING_MIGRATION)
                if kept.read_bytes() != data:
                    raise OSError(f"the copy in {kept} doesn't match it")
                path.unlink()
            except OSError as err:
                step = Step(PENDING_DIR, "skip", f"{_reason(err)}; {RERUN}", step.name)
        elif step.action == "remove":
            if any(s.action == "skip" for s in steps):
                continue  # a file is still in it; its skip step says why
            try:
                folder.rmdir()
                _fsync_dir(store.root)
            except OSError as err:
                step = Step(PENDING_DIR, "skip", f"{_reason(err)}; {RERUN}")
        steps.append(step)
    return steps


def run(store: Store, *, dry_run: bool = False) -> List[Step]:
    """Run every pending migration. Raises ``StoreError("locked")`` if the store stays
    locked. Anything else that goes wrong with one file is a ``skip`` step for that file."""
    if not leftover_indexes(store) and _inbox_plan(store) is None and leftover_pending(store) is None:
        return []
    if dry_run:
        steps = [_plan(store, p)[0] for p in leftover_indexes(store)]
        inbox_step = _inbox_plan(store)
        return steps + ([inbox_step] if inbox_step else []) + _pending_plan(store)
    steps = []
    with store.lock():
        for project in leftover_indexes(store):  # again, under the lock
            step, data = _plan(store, project)
            if step.action != "skip":
                try:
                    _apply(store, project, data)
                except (OSError, StoreError) as err:
                    step = Step(project, "skip", f"{_reason(err)}; {RERUN}")
            steps.append(step)
        try:
            inbox_step = _apply_inbox(store)  # under the lock: no race with the mirror
        except (OSError, StoreError) as err:
            inbox_step = Step(GLOBAL, "skip", _reason(err))
        if inbox_step is not None:
            steps.append(inbox_step)
        steps.extend(_apply_pending(store, _pending_plan(store)))
    return steps
