"""Staged project sorts from ``global/inbox.md`` (ADR-0016).

The agent sorts general inbox entries straight into ``global/``, but anything
bound for a project (including a new project) is only *proposed*: the
``memory_propose`` tool writes ``<store>/_pending/<id>.json``, and nothing
under ``memories/`` changes until the user runs ``/memory-apply <id>`` (or
``hermes memory-buckets apply <id>``) for that one proposal. ``_pending/`` sits outside
``memories/``, so the other tools can't reach it.

A proposal is one project: files to create or append to under ``<project>/``,
and the exact inbox lines it files, which are removed from the inbox when it's
applied. Applying takes the store lock once and checks everything before
writing anything, so a stale proposal fails whole rather than half-applying.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import frontmatter
from . import projects as hermes_projects
from .store import GLOBAL, Store, StoreError, check_content, is_project_id, scope_of, stamp, stem, validate_path

PENDING_DIR = "_pending"
INBOX = f"{GLOBAL}/inbox.md"
ID_RE = re.compile(r"^[0-9a-f]{6}$")
MAX_FILES = 20
SOURCE = "inbox-sort"


def _bullet(line: str) -> str:
    text = " ".join(line.split())
    return text if text.startswith(("- ", "* ")) else f"- {text}"


def _key(line: str) -> str:
    """Inbox lines compare without their bullet marker and extra whitespace."""
    text = " ".join(line.split())
    return text[2:] if text.startswith(("- ", "* ")) else text


def _dir(store: Store) -> Path:
    return store.root / PENDING_DIR


def projects(store: Store) -> List[str]:
    if not store.memories.is_dir():
        return []
    return sorted(p.name for p in store.memories.iterdir() if p.is_dir() and is_project_id(p.name))


def _inbox_body(store: Store) -> str:
    data = store.read_bytes(INBOX)
    if data is None:
        return ""
    try:
        return frontmatter.parse(data.decode("utf-8", errors="replace"))[1]
    except frontmatter.FrontmatterError:
        return data.decode("utf-8", errors="replace")


# -- proposals ---------------------------------------------------------------------

def load(store: Store, pid: str) -> Dict[str, Any]:
    pid = (pid or "").strip()
    if not ID_RE.match(pid):
        raise StoreError("not_found", f"{pid!r} isn't a proposal id (six hex digits)")
    try:
        return json.loads((_dir(store) / f"{pid}.json").read_text())
    except FileNotFoundError:
        raise StoreError("not_found", f"no pending proposal {pid}") from None
    except ValueError:
        raise StoreError("invalid_args", f"proposal {pid} is corrupt; reject it") from None


def list_all(store: Store) -> List[Dict[str, Any]]:
    out = []
    folder = _dir(store)
    if folder.is_dir():
        for path in sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime):
            try:
                out.append(json.loads(path.read_text()))
            except (OSError, ValueError):
                continue
    return out


def _check_files(project: str, files: Any) -> List[Dict[str, Any]]:
    if not isinstance(files, list) or not files:
        raise StoreError("invalid_args", "'files' needs at least one {path, lines} entry")
    if len(files) > MAX_FILES:
        raise StoreError("invalid_args", f"at most {MAX_FILES} files per proposal")
    clean, seen = [], set()
    for f in files:
        if not isinstance(f, dict):
            raise StoreError("invalid_args", "each entry in 'files' is an object {path, description?, lines}")
        path = f.get("path")
        validate_path(path)
        if scope_of(path) != project:
            raise StoreError("invalid_args", f"{path} isn't under {project}/; general facts go straight to global/ "
                                             "with the normal tools")
        if stem(path) == "index" and path.count("/") == 1:
            raise StoreError("invalid_args", f"{path} is maintained for you; use {project}/profile.md")
        if path in seen:
            raise StoreError("invalid_args", f"{path} appears twice; merge its lines")
        seen.add(path)
        lines = f.get("lines")
        if not isinstance(lines, list) or not lines or not all(isinstance(l, str) and l.strip() for l in lines):
            raise StoreError("invalid_args", f"{path}: 'lines' needs at least one non-empty fact")
        description = f.get("description")
        if description is not None and (not isinstance(description, str) or not description.strip()):
            raise StoreError("invalid_args", f"{path}: 'description' must be a non-empty string")
        entry = {"path": path, "lines": [_bullet(l) for l in lines]}
        if description:
            entry["description"] = " ".join(description.split())
        clean.append(entry)
    return clean


def propose(store: Store, *, project: Any, new_project: Any, files: Any, inbox_lines: Any, summary: Any,
            session: str = "") -> Dict[str, Any]:
    """Validate and stage a proposal. Raises ``StoreError`` with an actionable message."""
    if not isinstance(project, str) or not is_project_id(project):
        raise StoreError("invalid_args", f"{project!r} isn't a valid project id (lower-case kebab-case, not 'global')")
    new_project = new_project is True
    existing = projects(store)
    if new_project and project in existing:
        raise StoreError("invalid_args", f"project {project} already exists; propose into it with new_project false")
    if not new_project and project not in existing:
        raise StoreError("invalid_args", f"there's no project {project}. Existing: {', '.join(existing) or 'none'}. "
                                         "Pick one, or set new_project true to create it")
    clean = _check_files(project, files)
    if new_project and not any(f["path"] == f"{project}/profile.md" and f.get("description") for f in clean):
        raise StoreError("invalid_args", f"a new project needs {project}/profile.md with a description saying what "
                                         "the project is")
    for f in clean:
        if not f.get("description") and not (store.memories / f["path"]).is_file():
            raise StoreError("invalid_args", f"{f['path']} doesn't exist yet, so it needs a description")

    if not isinstance(inbox_lines, list) or not inbox_lines or not all(isinstance(l, str) and l.strip() for l in inbox_lines):
        raise StoreError("invalid_args", "'inbox_lines' needs the exact inbox lines this proposal files")
    present = {_key(l) for l in _inbox_body(store).splitlines() if l.strip()}
    wanted = list(dict.fromkeys(_key(l) for l in inbox_lines))
    missing = [l for l in wanted if l not in present]
    if missing:
        raise StoreError("invalid_args", "these aren't lines in global/inbox.md (copy them exactly): "
                         + "; ".join(missing[:5]))
    claimed = {_key(l): p["id"] for p in list_all(store) for l in p.get("inbox_lines", [])}
    taken = sorted({claimed[l] for l in wanted if l in claimed})
    if taken:
        raise StoreError("invalid_args", f"some of these lines are already in pending proposal(s) {', '.join(taken)}; "
                                         "the user must apply or reject those first")
    if not isinstance(summary, str) or not summary.strip():
        raise StoreError("invalid_args", "'summary' needs one line saying what this proposal files and why")

    folder = _dir(store)
    folder.mkdir(parents=True, exist_ok=True)
    while True:
        pid = secrets.token_hex(3)
        if not (folder / f"{pid}.json").exists():
            break
    proposal = {
        "id": pid, "project": project, "new_project": new_project, "summary": " ".join(summary.split()),
        "files": clean, "inbox_lines": wanted, "session": session,
        "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    with store.lock():
        store._atomic_write(folder / f"{pid}.json", json.dumps(proposal, indent=2, ensure_ascii=False).encode() + b"\n")
    return proposal


def render(p: Dict[str, Any], store: Optional[Store] = None) -> str:
    head = f"Proposal {p['id']}: {'new project' if p.get('new_project') else 'project'} {p['project']}"
    lines = [head, f"  {p.get('summary', '')}"]
    for f in p.get("files", []):
        exists = store is not None and (store.memories / f["path"]).is_file()
        mode = "append" if exists else "new file"
        lines.append(f"  {f['path']} ({mode})" + (f": {f['description']}" if f.get("description") and not exists else ""))
        lines.extend(f"      {l}" for l in f["lines"])
    n = len(p.get("inbox_lines", []))
    lines.append(f"  removes {n} line{'' if n == 1 else 's'} from {INBOX}")
    return "\n".join(lines)


def commands(pid: str) -> str:
    return f"Apply: /memory-apply {pid}    Reject: /memory-reject {pid}"


# -- apply / reject ------------------------------------------------------------------

def _new_file(path: str, description: str, lines: List[str]) -> str:
    fm = frontmatter.Frontmatter()
    fm.set("name", stem(path))
    fm.set("description", description)
    fm.set("sources", [SOURCE])
    stamp(fm)
    return frontmatter.render(fm, "\n".join(lines) + "\n")


def _appended(path: str, current: bytes, lines: List[str]) -> str:
    try:
        fm, body = frontmatter.parse(current.decode("utf-8", errors="replace"))
    except frontmatter.FrontmatterError as err:
        raise StoreError("invalid_frontmatter", f"{path}: can't parse its frontmatter ({err}); fix it by hand") from None
    have = {_key(l) for l in body.splitlines()}
    new = [l for l in lines if _key(l) not in have]
    if body and not body.endswith("\n"):
        body += "\n"
    sources = fm.get("sources") if isinstance(fm.get("sources"), list) else []
    if SOURCE not in sources:
        fm.set("sources", [*sources, SOURCE])
    stamp(fm)
    return frontmatter.render(fm, body + "".join(f"{l}\n" for l in new))


def _without(body: str, keys: List[str]) -> Tuple[str, List[str]]:
    remaining = list(keys)
    out = []
    for line in body.splitlines(keepends=True):
        k = _key(line)
        if k and k in remaining:
            remaining.remove(k)
            continue
        out.append(line)
    # Drop headings left with nothing under them.
    text = "".join(out)
    text = re.sub(r"(?m)^## [^\n]*\n(?:[ \t]*\n)*(?=## |\Z)", "", text)
    return text.strip("\n") + ("\n" if text.strip() else ""), remaining


def apply(store: Store, pid: str) -> Dict[str, Any]:
    """Commit one proposal. All checks run before any write."""
    p = load(store, pid)
    project = p.get("project")
    if not isinstance(project, str) or not is_project_id(project):
        raise StoreError("invalid_args", f"proposal {pid} names no valid project; reject it")
    files = _check_files(project, p.get("files"))
    with store.lock():
        exists = (store.memories / project).is_dir()
        if p.get("new_project") and exists:
            raise StoreError("conflict", f"project {project} was created since this was proposed; reject it and ask "
                                         "the agent to propose into the existing project")
        if not p.get("new_project") and not exists:
            raise StoreError("conflict", f"project {project} no longer exists; reject this proposal")
        writes: List[Tuple[str, bytes]] = []
        for f in files:
            current = store.read_bytes(f["path"])
            if current is None:
                if not f.get("description"):
                    raise StoreError("conflict", f"{f['path']} no longer exists and the proposal has no description for it")
                text = _new_file(f["path"], f["description"], f["lines"])
            else:
                text = _appended(f["path"], current, f["lines"])
            writes.append((f["path"], check_content(f["path"], text)))

        inbox = store.read_bytes(INBOX)
        leftover: List[str] = list(p.get("inbox_lines", []))
        if inbox is not None:
            try:
                fm, body = frontmatter.parse(inbox.decode("utf-8", errors="replace"))
            except frontmatter.FrontmatterError as err:
                raise StoreError("invalid_frontmatter", f"{INBOX}: can't parse its frontmatter ({err}); fix it by hand") from None
            body, leftover = _without(body, leftover)
            stamp(fm)
            writes.append((INBOX, check_content(INBOX, frontmatter.render(fm, body))))

        for path, data in writes:
            store._atomic_write(store.resolve(path), data)
        (_dir(store) / f"{p['id']}.json").unlink()
    return {"id": p["id"], "project": project, "written": [w[0] for w in writes if w[0] != INBOX],
            "not_in_inbox": leftover}


def reject(store: Store, pid: str) -> Dict[str, Any]:
    p = load(store, pid)
    with store.lock():
        try:
            os.unlink(_dir(store) / f"{p['id']}.json")
        except FileNotFoundError:
            pass
    return p


# -- slash commands (registered in __init__.register) ------------------------------------

def open_store() -> Store:
    from . import config as config_mod
    cfg = config_mod.load()
    if cfg.store_path:
        return Store(Path(cfg.store_path).expanduser())
    try:
        from hermes_constants import get_hermes_home
        home = Path(get_hermes_home())
    except ImportError:
        home = Path(os.environ.get("HERMES_HOME") or "~/.hermes").expanduser()
    return Store(home / "memory-buckets")


def summary_text(store: Store) -> str:
    found = list_all(store)
    if not found:
        return "No pending memory proposals."
    lines = [f"{len(found)} pending memory proposal{'' if len(found) == 1 else 's'}:"]
    lines += [f"  {p['id']}  {'new ' if p.get('new_project') else ''}{p['project']}: {p.get('summary', '')}" for p in found]
    lines.append("Show one: /memory-pending <id>. Apply or reject each one: /memory-apply <id>, /memory-reject <id>.")
    return "\n".join(lines)


def cmd_pending(raw_args: str = "") -> str:
    store = open_store()
    pid = (raw_args or "").strip()
    if not pid:
        return summary_text(store)
    try:
        p = load(store, pid)
    except StoreError as err:
        return err.message
    return render(p, store) + "\n" + commands(p["id"])


def cmd_apply(raw_args: str = "") -> str:
    args = (raw_args or "").split()
    if len(args) != 1:
        return "Usage: /memory-apply <id> (one proposal at a time; /memory-pending lists them)"
    store = open_store()
    try:
        done = apply(store, args[0])
    except StoreError as err:
        return f"Not applied: {err.message}"
    msg = f"Applied {done['id']} to {done['project']}: " + ", ".join(done["written"])
    if done["not_in_inbox"]:
        msg += f"\n({len(done['not_in_inbox'])} line(s) were already gone from {INBOX})"
    note = hermes_projects.unlinked_note(done["project"])
    return f"{msg}\n{note}" if note else msg


def cmd_reject(raw_args: str = "") -> str:
    args = (raw_args or "").split()
    if len(args) != 1:
        return "Usage: /memory-reject <id>"
    try:
        p = reject(open_store(), args[0])
    except StoreError as err:
        return err.message
    return f"Rejected {p['id']} ({p['project']}). Its lines stay in {INBOX}."
