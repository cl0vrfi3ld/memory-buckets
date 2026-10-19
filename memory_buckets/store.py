"""The store contract.

Owns: path rules, version tokens, read/list, compare-and-swap write and delete,
and the ``.lock`` file lock (``flock``, or ``msvcrt.locking`` on Windows). The
store is a plain directory, not a git repo.

Layout::

    <root>/                 $HERMES_HOME/memory-buckets
      memories/             every memory file; paths below are relative to this
        global/...          the global scope
        <project-id>/...    one directory per Hermes project (its slug)
      .index/               search cache (index.py)
      .history/             previous bytes of overwritten and deleted files, one
                            directory per memory path; see ``_record`` and ``restore``
      .lock
"""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import re
import stat
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Tuple

from . import frontmatter

logger = logging.getLogger(__name__)

if sys.platform == "win32":
    import msvcrt

    def _try_lock(fd: int) -> bool:
        """Lock byte 0 (the fd was just opened, so that's where it points)."""
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as err:
            # Held elsewhere: EACCES or EDEADLOCK on Windows, errno 0 under Wine.
            if err.errno not in (0, errno.EACCES, errno.EDEADLOCK):
                raise
            return False
        return True

    def _unlock(fd: int) -> None:
        # Closing would release it too, but Windows only promises "eventually".
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as err:
            if err.errno not in (errno.EAGAIN, errno.EACCES):
                raise
            return False
        return True

    def _unlock(fd: int) -> None:
        pass  # closing the fd releases the flock

MAX_FILE_BYTES = 48 * 1024
LOCK_TIMEOUT_S = 5.0
SHARING_RETRY_S = 1.0
NEW = "new"
_IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003  # a junction; stat only defines it on Windows

# Paths are relative to <store>/memories. Each scope is a directory: `global/`
# or a project id. `global` is reserved, so it can't be a project id.
GLOBAL = "global"
MEMORIES_DIR = "memories"
HISTORY_DIR = ".history"

# Retention defaults, shared with config.py: ``history_keep`` versions per file,
# ``history_days`` maximum age. 0 disables recording (keep) or expiry (days).
HISTORY_KEEP_DEFAULT = 20
HISTORY_DAYS_DEFAULT = 90

# A history entry is ``<UTC timestamp>-<version>.md``: fixed-width sortable
# timestamp, then the sha256[:12] of the recorded bytes, so one file's entries
# sort chronologically and carry their own version token.
_TS_FORMAT = "%Y%m%dT%H%M%S.%fZ"
_TS_LEN = 23  # fixed width: %Y%m%dT%H%M%S (15) + "." + six microsecond digits + "Z"

_SEG = r"[a-z0-9]+(?:-[a-z0-9]+)*"
# Anchors are \Z, not $: $ also matches before a trailing newline, which
# accepted paths like "global/topics/x.md\n" and created files named "x.md\n".
SEGMENT_RE = re.compile(rf"^{_SEG}\Z")
_PROJECT = rf"(?!{GLOBAL}/){_SEG}"
_PATH_RES = tuple(
    re.compile(p)
    for p in (
        rf"^{GLOBAL}/(?:profile|preferences|inbox)\.md\Z",
        rf"^{GLOBAL}/(?:topics|areas|people)/{_SEG}\.md\Z",
        rf"^{_PROJECT}/(?:profile|preferences)\.md\Z",
        rf"^{_PROJECT}/(?:topics|areas|people)/{_SEG}\.md\Z",
    )
)


class StoreError(Exception):
    """A contract error. ``code`` is a short machine-readable reason; ``fields``
    carry extra result data (e.g. ``current_version`` on a conflict)."""

    def __init__(self, code: str, message: str, **fields) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fields = fields


def is_project_id(value: str) -> bool:
    return bool(SEGMENT_RE.match(value or "")) and value != GLOBAL


def is_link(path: str | os.PathLike[str]) -> bool:
    """A symlink, or on Windows a junction: both can lead out of the store. Not any
    reparse point, since OneDrive's online-only files are reparse points too."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode) or getattr(st, "st_reparse_tag", 0) == _IO_REPARSE_TAG_MOUNT_POINT


def validate_path(path: str) -> str:
    """Return ``path`` if it's an allowed memory path, else raise ``invalid_path``.

    ``path`` is relative to ``<store>/memories``, e.g. ``global/topics/nix.md``
    or ``proj-1/preferences.md``. Pure string check. Symlinks are checked
    against the filesystem when the path is resolved.
    """
    if not isinstance(path, str) or not path:
        raise StoreError("invalid_path", "path must be a non-empty string")
    if path.startswith("/") or "\\" in path:
        raise StoreError("invalid_path", f"{path!r}: paths are relative and use '/'")
    if any(seg in ("", ".", "..") for seg in path.split("/")):
        raise StoreError("invalid_path", f"{path!r}: empty, '.' or '..' segment")
    if not any(r.match(path) for r in _PATH_RES):
        raise StoreError(
            "invalid_path",
            f"{path!r}: not an allowed memory path (global/{{profile,preferences,inbox}}.md, "
            "global/{topics,areas,people}/<name>.md, <project>/{profile,preferences}.md, "
            "<project>/{topics,areas,people}/<name>.md; kebab-case .md)",
        )
    return path


def version(data: Optional[bytes]) -> str:
    """``sha256(bytes)[:12]``, or ``"new"`` for a file that doesn't exist."""
    if data is None:
        return NEW
    return hashlib.sha256(data).hexdigest()[:12]


def stem(path: str) -> str:
    return path.rsplit("/", 1)[-1][: -len(".md")]


EDITED_AT = "edited_at"
EDITED_AT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def stamp(fm) -> None:
    """Set ``edited_at`` to now (UTC, ISO 8601). Every writer that changes a file's
    content calls this; hand edits (Obsidian, an editor) don't, so it's best-effort."""
    fm.set(EDITED_AT, datetime.now(timezone.utc).strftime(EDITED_AT_FORMAT))


def edited_at(fm) -> Optional[float]:
    """The ``edited_at`` stamp as a Unix time, or None when it is absent or isn't in
    the format ``stamp`` writes (a hand-written date, say)."""
    value = fm.get(EDITED_AT)
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, EDITED_AT_FORMAT).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def scope_of(path: str) -> str:
    return path.split("/", 1)[0]


def check_content(path: str, content: str) -> bytes:
    """Encode and validate a whole file: size cap, frontmatter, name == stem."""
    data = content.encode("utf-8")
    if len(data) > MAX_FILE_BYTES:
        raise StoreError(
            "too_large", f"{path}: {len(data)} bytes is over the {MAX_FILE_BYTES}-byte cap; split the file"
        )
    try:
        fm, _ = frontmatter.parse(content)
        if not fm.entries:
            raise frontmatter.FrontmatterError("file needs frontmatter with 'name' and 'description'")
        frontmatter.validate(fm, stem(path))
    except frontmatter.FrontmatterError as err:
        raise StoreError("invalid_frontmatter", f"{path}: {err}") from None
    return data


class Store:
    def __init__(self, root: os.PathLike, history_keep: int = HISTORY_KEEP_DEFAULT,
                 history_days: int = HISTORY_DAYS_DEFAULT) -> None:
        self.root = Path(root)
        self.memories = self.root / MEMORIES_DIR
        self.history_keep = int(history_keep)
        self.history_days = int(history_days)

    # -- paths ----------------------------------------------------------------

    def resolve(self, path: str) -> Path:
        """Validated absolute path. Refuses symlinks at any level below the root."""
        validate_path(path)
        current = self.memories
        for part in [""] + path.split("/"):
            current = current / part if part else current
            if is_link(current):
                raise StoreError("invalid_path", f"{path!r}: symlinks aren't allowed in the store")
        return current

    # -- reads ----------------------------------------------------------------

    def read_bytes(self, path: str) -> Optional[bytes]:
        target = self.resolve(path)
        try:
            return target.read_bytes()
        except FileNotFoundError:
            return None
        except IsADirectoryError:
            raise StoreError("invalid_path", f"{path!r} is a directory") from None

    def read(self, path: str) -> Dict[str, str]:
        data = self.read_bytes(path)
        if data is None:
            raise StoreError("not_found", f"{path} doesn't exist")
        return {"path": path, "content": data.decode("utf-8", errors="replace"), "version": version(data)}

    def iter_paths(self) -> Iterator[str]:
        """Every valid memory path on disk, sorted. Invalid files and symlinks are skipped (``lint`` reports them)."""
        if not self.memories.is_dir():
            return
        found = []
        for dirpath, dirnames, filenames in os.walk(self.memories):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and not is_link(os.path.join(dirpath, d))]
            rel_dir = os.path.relpath(dirpath, self.memories).replace(os.sep, "/")  # memory paths always use '/'
            for name in filenames:
                rel = name if rel_dir == "." else f"{rel_dir}/{name}"
                if is_link(os.path.join(dirpath, name)):
                    continue
                try:
                    validate_path(rel)
                except StoreError:
                    continue
                found.append(rel)
        yield from sorted(found)

    def iter_skipped(self) -> Iterator[Tuple[str, str]]:
        """``(path, reason)`` for what ``iter_paths`` passes over, sorted: ``"path"``
        for a file outside the path rules, ``"symlink"`` for a symlink (or junction)
        to a file or directory. This is what ``status`` counts, so the agent not
        seeing a file isn't silent. Dot files and directories (Obsidian's
        ``.obsidian/``, our own ``.tmp`` files) aren't counted, as in ``lint``."""
        if not self.memories.is_dir():
            return
        found = []
        for dirpath, dirnames, filenames in os.walk(self.memories):
            rel_dir = os.path.relpath(dirpath, self.memories).replace(os.sep, "/")  # memory paths always use '/'
            walk = []
            for d in dirnames:
                if d.startswith("."):
                    continue
                if is_link(os.path.join(dirpath, d)):
                    found.append((d if rel_dir == "." else f"{rel_dir}/{d}", "symlink"))  # not descended into
                else:
                    walk.append(d)
            dirnames[:] = walk
            for name in filenames:
                if name.startswith("."):
                    continue
                rel = name if rel_dir == "." else f"{rel_dir}/{name}"
                if is_link(os.path.join(dirpath, name)):
                    found.append((rel, "symlink"))
                    continue
                try:
                    validate_path(rel)
                except StoreError:
                    found.append((rel, "path"))
        yield from sorted(found)

    def projects(self) -> List[str]:
        """The project buckets that exist on disk, sorted."""
        if not self.memories.is_dir():
            return []
        return sorted(p.name for p in self.memories.iterdir() if p.is_dir() and not is_link(p) and is_project_id(p.name))

    def list(self, prefix: str = "") -> List[Dict[str, object]]:
        out = []
        for rel in self.iter_paths():
            if not rel.startswith(prefix):
                continue
            data = (self.memories / rel).read_bytes()
            out.append({"path": rel, "version": version(data), "bytes": len(data)})
        return out

    # -- writes ---------------------------------------------------------------

    @contextmanager
    def lock(self, timeout: float = LOCK_TIMEOUT_S):
        """Exclusive lock on ``<root>/.lock``. Per open file (``flock``) or per handle
        (``msvcrt.locking``), so it also excludes other provider instances and threads
        in this process."""
        self.root.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.root / ".lock", os.O_RDWR | os.O_CREAT, 0o666)
        locked = False
        try:
            deadline = time.monotonic() + timeout
            while not _try_lock(fd):
                if time.monotonic() >= deadline:
                    raise StoreError("locked", f"store is locked by another writer (waited {timeout:g}s)")
                time.sleep(0.02)
            locked = True
            yield
        finally:
            try:
                if locked:
                    _unlock(fd)
            finally:
                os.close(fd)

    def _atomic_write(self, target: Path, data: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.parent / f".{target.name}.tmp.{os.getpid()}.{threading.get_ident()}"
        try:
            # O_BINARY: Windows opens fds in text mode by default, which would turn
            # every \n into \r\n on disk and break the version of what we just wrote.
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o666)
            try:
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            _retrying(os.replace, tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)  # don't leave a partial .tmp behind
            except OSError:
                pass
            raise
        _fsync_dir(target.parent)

    def update(self, path: str, fn: Callable[[Optional[bytes]], bytes], if_version: Optional[str] = None,
               record_history: bool = True) -> Dict[str, str]:
        """Compare-and-swap. Under the lock: read the current bytes, check
        ``if_version`` (skipped when None), then write ``fn(current)``. When the
        write destroys content (an overwrite or edit, not a creation), the previous
        bytes go to ``.history`` first; purely additive writers (appends) pass
        ``record_history=False``, since nothing is lost by adding."""
        target = self.resolve(path)
        with self.lock():
            current = self.read_bytes(path)
            current_version = version(current)
            if if_version is not None and if_version != current_version:
                raise StoreError(
                    "conflict",
                    f"{path} is at version {current_version}, not {if_version}",
                    current_version=current_version,
                    current_content=None if current is None else current.decode("utf-8", errors="replace"),
                )
            data = fn(current)
            if record_history and current is not None and data != current:
                self._record(path, current)
            self._atomic_write(target, data)
        return {"path": path, "version": version(data)}

    def write(self, path: str, content: str, if_version: str) -> Dict[str, str]:
        """Write a whole file. ``if_version`` is required: a version, or ``"new"``."""
        if not isinstance(if_version, str) or not if_version:
            raise StoreError("conflict", "if_version is required (a version, or 'new' to create)")
        data = check_content(path, content)
        return self.update(path, lambda _current: data, if_version)

    def delete(self, path: str, if_version: str) -> Dict[str, str]:
        target = self.resolve(path)
        with self.lock():
            current = self.read_bytes(path)
            if current is None:
                raise StoreError("not_found", f"{path} doesn't exist")
            current_version = version(current)
            if if_version != current_version:
                raise StoreError(
                    "conflict",
                    f"{path} is at version {current_version}, not {if_version}",
                    current_version=current_version,
                    current_content=current.decode("utf-8", errors="replace"),
                )
            self._record(path, current)
            _retrying(target.unlink)
            surviving = target.parent
            for parent in (target.parent, target.parent.parent):
                if parent != self.memories and parent.is_dir():
                    try:
                        parent.rmdir()
                        surviving = parent.parent
                    except OSError:
                        pass
            _fsync_dir(surviving)
        return {"path": path}

    # -- history ----------------------------------------------------------------

    def _history_dir(self, path: str) -> Path:
        """Where ``path``'s recorded versions live: ``.history/<path>/``. ``path``
        is validated (no ``..``, no backslashes), so this stays inside the store."""
        return self.root.joinpath(HISTORY_DIR, *path.split("/"))

    def _record(self, path: str, data: bytes) -> None:
        """Save ``data`` (the file's previous bytes) to ``.history``. Called under
        the store lock, before the change that would destroy them. Best-effort by
        contract: any failure here (full disk, unwritable ``.history``, a clock the
        entry naming can't parse) must not fail the write, so everything is caught
        and only logged."""
        if self.history_keep <= 0:
            return
        try:
            target = self._history_dir(path)
            target.mkdir(parents=True, exist_ok=True)
            now_stamp = datetime.now(timezone.utc).strftime(_TS_FORMAT)
            self._atomic_write(target / f"{now_stamp}-{version(data)}.md", data)
            self._prune(target)
        except Exception as err:  # noqa: BLE001 - history is never worth a failed write
            logger.warning("memory-buckets: no history kept for %s (%s: %s); the write itself is unaffected",
                           path, type(err).__name__, err)

    def _prune(self, target: Path) -> None:
        """Keep the newest ``history_keep`` entries per file and drop anything older
        than ``history_days``. Entry names sort chronologically. Unparseable names
        are left alone: this directory is ours, but not deleting what we can't
        identify is the safer error. A single unlink failure stops the sweep and
        the next recorded write retries it."""
        names = sorted(name for name in os.listdir(target) if name.endswith(".md"))
        if not names:
            return
        max_age = timedelta(days=self.history_days) if self.history_days else None
        now = datetime.now(timezone.utc)
        kept = 0
        for name in reversed(names):  # newest first
            if kept >= self.history_keep:
                (target / name).unlink()
                continue
            entry_time = _entry_timestamp(name)
            if max_age is not None and entry_time is not None and now - entry_time > max_age:
                (target / name).unlink()
                continue
            kept += 1

    def history(self, path: str) -> List[Dict[str, object]]:
        """``path``'s recorded versions, newest first: ``{version, timestamp, bytes,
        file}`` with the timestamp in the ``edited_at`` format and ``file`` the
        entry's name in ``.history``. Empty when nothing is recorded; raises
        ``invalid_path`` for a path that was never valid, so ``restore`` can't be
        pointed outside the store."""
        validate_path(path)
        try:
            names = sorted(name for name in os.listdir(self._history_dir(path)) if name.endswith(".md"))
        except OSError:
            return []
        out = []
        for name in names:
            entry_time = _entry_timestamp(name)
            try:
                size = (self._history_dir(path) / name).stat().st_size
            except OSError:
                continue
            out.append({
                "version": _entry_version(name),
                "timestamp": entry_time.strftime("%Y-%m-%dT%H:%M:%SZ") if entry_time else "",
                "bytes": size,
                "file": name,
            })
        return out[::-1]

    def restore(self, path: str, entry_version: Optional[str] = None) -> Dict[str, object]:
        """Write a recorded version back through ``update``, so the content it
        replaces is recorded too and a concurrent change is refused, not clobbered.
        ``entry_version`` picks the version token from ``history()``; None picks
        the newest. A no-op returning ``unchanged: True`` when the file already
        holds that content."""
        entries = self.history(path)
        if not entries:
            raise StoreError("not_found", f"{path} has no history to restore")
        if entry_version is None:
            entry = entries[0]
        else:
            matches = [e for e in entries if e["version"] == entry_version]
            if not matches:
                matches = [e for e in entries if e["version"].startswith(entry_version)]
            if not matches:
                raise StoreError("not_found", f"{path}: no recorded version matches {entry_version!r}")
            if len(matches) > 1:
                raise StoreError(
                    "ambiguous_version",
                    f"{path}: several recorded versions match {entry_version!r}; give the full version",
                )
            entry = matches[0]
        try:
            data = (self._history_dir(path) / entry["file"]).read_bytes()
        except OSError as err:
            raise StoreError("not_found", f"{path}: can't read the recorded version ({err})") from None
        current = self.read_bytes(path)
        if current is not None and current == data:
            return {"path": path, "version": version(data), "timestamp": entry["timestamp"], "unchanged": True}
        self.update(path, lambda _current: data, if_version=NEW if current is None else version(current))
        return {"path": path, "version": version(data), "timestamp": entry["timestamp"], "unchanged": False}


def _entry_timestamp(name: str) -> Optional[datetime]:
    """The UTC time in an entry name, or None for a name we didn't write."""
    try:
        return datetime.strptime(name[:_TS_LEN], _TS_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _entry_version(name: str) -> str:
    """The version token in ``<timestamp>-<version>.md``: everything between the
    timestamp and ``.md``. Empty for a name we didn't write."""
    return name[_TS_LEN + 1:-3] if name.endswith(".md") and len(name) > _TS_LEN + 1 else ""


def _retrying(fn: Callable[..., None], *args) -> None:
    """Windows won't replace or delete a file while another process has it open
    (WinError 5 or 32): another Hermes session reading it, an editor, a virus
    scanner. Those holds usually last milliseconds, so retry for a moment, then
    report it like a held store lock: try again later."""
    deadline = time.monotonic() + SHARING_RETRY_S
    while True:
        try:
            fn(*args)
            return
        except PermissionError as err:
            if sys.platform != "win32":
                raise
            if time.monotonic() >= deadline:
                name = os.path.basename(err.filename2 or err.filename or "")
                raise StoreError("locked", f"{name} is open in another program (waited {SHARING_RETRY_S:g}s); "
                                 "try again shortly") from err
            time.sleep(0.02)


def _fsync_dir(path: Path) -> None:
    if sys.platform == "win32":
        # os.open can't open a directory on Windows, and there's no stdlib way to
        # flush one; the rename's durability is whatever NTFS's journal gives it.
        return
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
