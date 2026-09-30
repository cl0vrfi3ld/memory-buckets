"""The store contract.

Owns: path rules, version tokens, read/list, compare-and-swap write and delete,
and the ``.lock`` flock. The store is a plain directory, not a git repo.

Layout::

    <root>/                 $HERMES_HOME/memory-buckets
      memories/             every memory file; paths below are relative to this
        global/...          the global scope
        <project-id>/...    one directory per Hermes project (its slug)
      .index/               search cache (index.py)
      .lock
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional

from . import frontmatter

MAX_FILE_BYTES = 48 * 1024
LOCK_TIMEOUT_S = 5.0
NEW = "new"

# Paths are relative to <store>/memories. Each scope is a directory: `global/`
# or a project id. `global` is reserved, so it can't be a project id.
GLOBAL = "global"
MEMORIES_DIR = "memories"

_SEG = r"[a-z0-9]+(?:-[a-z0-9]+)*"
SEGMENT_RE = re.compile(rf"^{_SEG}$")
_PROJECT = rf"(?!{GLOBAL}/){_SEG}"
_PATH_RES = tuple(
    re.compile(p)
    for p in (
        rf"^{GLOBAL}/(?:profile|preferences|inbox)\.md$",
        rf"^{GLOBAL}/(?:topics|areas|people)/{_SEG}\.md$",
        rf"^{_PROJECT}/(?:index|profile|preferences)\.md$",
        rf"^{_PROJECT}/(?:topics|areas|people)/{_SEG}\.md$",
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
            "global/{topics,areas,people}/<name>.md, <project>/{index,profile,preferences}.md, "
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


def stamp(fm) -> None:
    """Set ``edited_at`` to now (UTC, ISO 8601). Every writer that changes a file's
    content calls this; hand edits (Obsidian, an editor) don't, so it's best-effort."""
    fm.set(EDITED_AT, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))


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
    def __init__(self, root: os.PathLike) -> None:
        self.root = Path(root)
        self.memories = self.root / MEMORIES_DIR

    # -- paths ----------------------------------------------------------------

    def resolve(self, path: str) -> Path:
        """Validated absolute path. Refuses symlinks at any level below the root."""
        validate_path(path)
        current = self.memories
        for part in [""] + path.split("/"):
            current = current / part if part else current
            if current.is_symlink():
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
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and not os.path.islink(os.path.join(dirpath, d))]
            rel_dir = os.path.relpath(dirpath, self.memories)
            for name in filenames:
                rel = name if rel_dir == "." else f"{rel_dir}/{name}"
                if os.path.islink(os.path.join(dirpath, name)):
                    continue
                try:
                    validate_path(rel)
                except StoreError:
                    continue
                found.append(rel)
        yield from sorted(found)

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
        """Exclusive ``flock`` on ``<root>/.lock``. Per open file description, so it
        also excludes other provider instances and threads in this process."""
        self.root.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.root / ".lock", os.O_RDWR | os.O_CREAT, 0o666)
        try:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as err:
                    if err.errno not in (errno.EAGAIN, errno.EACCES):
                        raise
                    if time.monotonic() >= deadline:
                        raise StoreError("locked", f"store is locked by another writer (waited {timeout:g}s)") from None
                    time.sleep(0.02)
            yield
        finally:
            os.close(fd)  # closing the fd releases the flock

    def _atomic_write(self, target: Path, data: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.parent / f".{target.name}.tmp.{os.getpid()}.{threading.get_ident()}"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666)
            try:
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)  # don't leave a partial .tmp behind
            except OSError:
                pass
            raise
        _fsync_dir(target.parent)

    def update(self, path: str, fn: Callable[[Optional[bytes]], bytes], if_version: Optional[str] = None) -> Dict[str, str]:
        """Compare-and-swap. Under the lock: read the current bytes, check
        ``if_version`` (skipped when None), then write ``fn(current)``."""
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
            target.unlink()
            _fsync_dir(target.parent)
        return {"path": path}


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
