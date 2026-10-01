"""Tool schemas and handlers.

Every result carries ``ok``. Errors are ``{"ok": false, "error": {"code", "message"}, ...}``:

- ``conflict`` adds ``current: {path, version, content}`` so the agent can merge and retry;
- ``out_of_scope`` adds ``allowed_prefixes``.

The contract codes come from ``store.py``. The tool layer adds ``out_of_scope``,
``read_only`` and ``invalid_args``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from . import frontmatter, inbox, pending
from .config import Config
from .index import Index
from .scopes import Scope
from .store import NEW, Store, StoreError, check_content, stamp, stem, validate_path

MAX_READ_PATHS = 20
MAX_SEARCH_LIMIT = 20

_PATH_HELP = ("Path relative to the memory root: lower-case, hyphens between words, ending in .md. Allowed forms: "
              "global/profile.md, global/preferences.md, global/inbox.md, global/topics/<name>.md, "
              "global/areas/<name>.md, global/people/<name>.md, <project>/profile.md, <project>/preferences.md, "
              "<project>/topics/<name>.md, <project>/areas/<name>.md, <project>/people/<name>.md.")


def _fn(name: str, description: str, properties: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required, "additionalProperties": False}}


SCHEMAS: List[Dict[str, Any]] = [
    _fn("memory_list",
        "List memory files with their descriptions and versions. By default it lists global/ and the current "
        "project. To see another project, pass its directory as prefix (for example 'home-server/'), or set "
        "include_projects to true to list every project.",
        {"prefix": {"type": "string", "description": "Only list paths that start with this, for example 'global/people/'."},
         "include_projects": {"type": "boolean", "description": "Set to true to also list other projects' files."}}, []),
    _fn("memory_read",
        "Read one or more memory files in full. Each result has the file's content and its version. To edit a "
        "file later, pass that version as if_version.",
        {"paths": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": MAX_READ_PATHS,
                   "description": _PATH_HELP}}, ["paths"]),
    _fn("memory_search",
        "Search memory by meaning and by keywords. Use it when no file description in the list matches what you "
        "need. Returns matching files, each with its best-matching passage.",
        {"query": {"type": "string", "description": "What you are looking for, in plain words."},
         "limit": {"type": "integer", "minimum": 1, "maximum": MAX_SEARCH_LIMIT},
         "prefix": {"type": "string", "description": "Only search paths that start with this."}}, ["query"]),
    _fn("memory_write",
        "Create a memory file, or replace the whole body of an existing one. To create a file, set if_version to "
        "\"new\". To replace a file, set if_version to the version you got from memory_read. For small changes, "
        "use memory_append or memory_str_replace instead.",
        {"path": {"type": "string", "description": _PATH_HELP},
         "description": {"type": "string", "description": "One line that says when to read this file."},
         "body": {"type": "string", "description": "The file's content: short bullet points, one fact per line."},
         "if_version": {"type": "string", "description": "\"new\" to create the file, or the file's current version to replace it."},
         "aliases": {"type": "array", "items": {"type": "string"}, "description": "Optional other names for the subject."}},
        ["path", "description", "body", "if_version"]),
    _fn("memory_str_replace",
        "Replace one piece of text in a memory file. The text in old must appear exactly once in the file. Use it "
        "to correct or update a fact.",
        {"path": {"type": "string", "description": _PATH_HELP},
         "old": {"type": "string", "description": "The exact text to replace. It must appear exactly once."},
         "new": {"type": "string", "description": "The replacement text. Use an empty string to delete the text."},
         "if_version": {"type": "string", "description": "Optional: the version you read. The edit fails if the file has changed since."}},
        ["path", "old", "new"]),
    _fn("memory_append",
        "Add facts to the end of an existing memory file, one bullet point per fact. The file must already "
        "exist; to create a file, use memory_write. global/inbox.md is the exception: it is created if missing.",
        {"path": {"type": "string", "description": _PATH_HELP},
         "lines": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                   "description": "One fact per item. '- ' is added to the start if it is missing."},
         "if_version": {"type": "string", "description": "Optional: the version you read. The edit fails if the file has changed since."}},
        ["path", "lines"]),
    _fn("memory_delete",
        "Delete a memory file. Set if_version to the file's current version, from memory_read or memory_list.",
        {"path": {"type": "string", "description": _PATH_HELP},
         "if_version": {"type": "string", "description": "The file's current version."}},
        ["path", "if_version"]),
    _fn("memory_propose",
        "Stage facts for a project you cannot write in this session, or for any project while sorting "
        "global/inbox.md. It writes no memory files: the user applies the proposal with /memory-apply <id>. "
        "Make one call per project. The facts must already be lines in global/inbox.md; those lines are removed "
        "from the inbox when the proposal is applied.",
        {"project": {"type": "string", "description": "The project id: lower-case, hyphens between words, for example "
                                                      "'home-server'."},
         "new_project": {"type": "boolean",
                         "description": "Set to true only for a project that is not in your list of projects. Then "
                                        "files must include <project>/profile.md with a description."},
         "files": {"type": "array", "minItems": 1, "maxItems": pending.MAX_FILES,
                   "items": {"type": "object", "additionalProperties": False, "required": ["path", "lines"],
                             "properties": {
                                 "path": {"type": "string", "description": "<project>/profile.md, <project>/preferences.md, "
                                          "or <project>/topics/<name>.md, <project>/areas/<name>.md, "
                                          "<project>/people/<name>.md."},
                                 "description": {"type": "string",
                                                 "description": "Required when the file does not exist yet: one line "
                                                                "that says when to read it."},
                                 "lines": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                                           "description": "The facts to add, one per item, written as short bullet points."}}},
                   "description": "The files to create, or to add to if they already exist."},
         "inbox_lines": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                         "description": "The lines from global/inbox.md that this proposal covers, copied exactly."},
         "summary": {"type": "string", "description": "One sentence for the user: what the facts are and why they "
                                                      "belong to this project. The user sees it."}},
        ["project", "files", "inbox_lines", "summary"]),
]
TOOL_NAMES = [s["name"] for s in SCHEMAS]


@dataclass
class Context:
    store: Store
    index: Index
    scope: Scope
    config: Config
    source: str  # platform name, recorded in `sources`
    on_write: Optional[Callable[[str], None]] = None
    # Shows the user a one-line notice; returns True if it was shown. None: no way to
    # reach the user directly (gateways; see provider._notifier), so the result asks
    # the agent to say it.
    notify: Optional[Callable[[str], bool]] = None
    hermes_buckets: Optional[List[str]] = None  # buckets of every Hermes project; None outside Hermes

    def tell_user(self, message: str) -> bool:
        try:
            return bool(self.notify and self.notify(message))
        except Exception:  # a broken UI callback must never fail the write
            return False


class ArgError(Exception):
    pass


def _str(args: Dict[str, Any], key: str, required: bool = True, allow_empty: bool = False) -> Optional[str]:
    value = args.get(key)
    if value is None:
        if required:
            raise ArgError(f"'{key}' is required")
        return None
    if not isinstance(value, str) or (not value and not allow_empty):
        raise ArgError(f"'{key}' must be a non-empty string")
    return value


def _str_list(args: Dict[str, Any], key: str, required: bool = True) -> Optional[List[str]]:
    value = args.get(key)
    if value is None:
        if required:
            raise ArgError(f"'{key}' is required")
        return None
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ArgError(f"'{key}' must be a list of strings")
    return value


def error(err: StoreError, path: Optional[str] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"ok": False, "error": {"code": err.code, "message": err.message}}
    if err.code == "conflict":
        out["error"]["message"] += "; merge with the current content and retry with its version"
        out["current"] = {"path": path, "version": err.fields.get("current_version"),
                          "content": err.fields.get("current_content")}
    elif err.fields:
        out.update(err.fields)
    return out


def handle(name: str, args: Dict[str, Any], ctx: Context) -> Dict[str, Any]:
    handler = _HANDLERS.get(name)
    if handler is None:
        return {"ok": False, "error": {"code": "invalid_args", "message": f"unknown tool {name!r}"}}
    if not isinstance(args, dict):
        return {"ok": False, "error": {"code": "invalid_args", "message": "arguments must be an object"}}
    try:
        return handler(args, ctx)
    except ArgError as err:
        return {"ok": False, "error": {"code": "invalid_args", "message": str(err)}}
    except StoreError as err:
        return error(err, args.get("path") if isinstance(args.get("path"), str) else None)


# -- reads -----------------------------------------------------------------------

def _list(args, ctx: Context):
    prefix = _str(args, "prefix", required=False, allow_empty=True) or ""
    include_projects = args.get("include_projects") is True
    ctx.index.reconcile()
    files = []
    for path, ver, description, size in ctx.index.files():
        if not path.startswith(prefix):
            continue
        if not (prefix or include_projects or ctx.scope.reads_by_default(path)):
            continue
        files.append({"path": path, "description": description, "version": ver, "bytes": size})
    return {"ok": True, "scope": ctx.scope.describe(), "files": files}


def _read(args, ctx: Context):
    paths = _str_list(args, "paths")
    if not paths or len(paths) > MAX_READ_PATHS:
        raise ArgError(f"'paths' needs 1–{MAX_READ_PATHS} paths")
    files, errors = [], []
    for path in dict.fromkeys(paths):
        try:
            got = ctx.store.read(path)
            files.append({"path": path, "version": got["version"], "content": got["content"]})
        except StoreError as err:
            errors.append({"path": path, "code": err.code, "message": err.message})
    out: Dict[str, Any] = {"ok": bool(files) or not errors, "files": files}
    if errors:
        out["errors"] = errors
    return out


def _search(args, ctx: Context):
    query = _str(args, "query")
    prefix = _str(args, "prefix", required=False, allow_empty=True) or ""
    limit = args.get("limit", ctx.config.search_limit)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_SEARCH_LIMIT:
        raise ArgError(f"'limit' must be an integer 1–{MAX_SEARCH_LIMIT}")
    read_ok = (lambda p: p.startswith(prefix)) if prefix else ctx.scope.reads_by_default
    found = ctx.index.search(query, read_ok, project=ctx.scope.project, limit=limit, boost=ctx.config.project_boost)
    out: Dict[str, Any] = {"ok": True, "mode": found["mode"], "results": found["results"]}
    if found.get("note"):
        out["note"] = found["note"]
    return out


# -- writes ----------------------------------------------------------------------

def _existing(path: str, current: Optional[bytes]):
    """(frontmatter, body) of the current file; refuses to rewrite one we can't parse."""
    text = current.decode("utf-8", errors="replace")
    try:
        return frontmatter.parse(text)
    except frontmatter.FrontmatterError as err:
        raise StoreError("invalid_frontmatter",
                         f"{path}: can't parse its frontmatter ({err}); fix it by hand before editing") from None


def _add_source(fm: frontmatter.Frontmatter, source: str) -> None:
    sources = fm.get("sources")
    sources = list(sources) if isinstance(sources, list) else []
    if source and source not in sources:
        sources.append(source)
    if sources:
        fm.set("sources", sources)


def _body_text(body: str) -> str:
    body = body.replace("\r\n", "\n")
    return body if body.endswith("\n") or not body else body + "\n"


def _mutate(ctx: Context, path: str, if_version: Optional[str],
            edit: Callable[[Optional[bytes]], str], *, append: bool = False) -> Dict[str, Any]:
    validate_path(path)
    ctx.scope.check_write(path, append=append)
    result = ctx.store.update(path, lambda current: check_content(path, edit(current)), if_version)
    if ctx.on_write:
        ctx.on_write(path)
    return {"ok": True, "path": path, "version": result["version"]}


def _write(args, ctx: Context):
    path = _str(args, "path")
    description = " ".join(_str(args, "description").split())
    body = _str(args, "body", allow_empty=True)
    if_version = _str(args, "if_version")
    aliases = _str_list(args, "aliases", required=False)

    def edit(current):
        if current is None:
            fm = frontmatter.Frontmatter()
            fm.set("name", stem(path))
        else:
            fm, _ = _existing(path, current)
            fm.set("name", stem(path))
        fm.set("description", description)
        if aliases is not None:
            fm.set("aliases", aliases)
        _add_source(fm, ctx.source)
        stamp(fm)
        return frontmatter.render(fm, _body_text(body))

    return _mutate(ctx, path, if_version, edit)


def _str_replace(args, ctx: Context):
    path = _str(args, "path")
    old = _str(args, "old")
    new = _str(args, "new", allow_empty=True)
    if_version = _str(args, "if_version", required=False)

    def edit(current):
        if current is None:
            raise StoreError("not_found", f"{path} doesn't exist")
        fm, body = _existing(path, current)
        count = body.count(old)
        if count == 0:
            raise StoreError("no_match", f"the text to replace isn't in {path}'s body; memory_read it for the exact text")
        if count > 1:
            raise StoreError("ambiguous_match", f"the text occurs {count} times in {path}; include more context so it's unique")
        _add_source(fm, ctx.source)
        stamp(fm)
        return frontmatter.render(fm, body.replace(old, new, 1))

    return _mutate(ctx, path, if_version, edit)


def _append(args, ctx: Context):
    path = _str(args, "path")
    lines = _str_list(args, "lines")
    if not lines or not all(line.strip() for line in lines):
        raise ArgError("'lines' needs at least one non-empty line")
    if_version = _str(args, "if_version", required=False)
    if path == inbox.PATH:
        return _append_inbox(ctx, lines, if_version)

    def edit(current):
        if current is None:
            raise StoreError("not_found", f"{path} doesn't exist; create it with memory_write (if_version \"new\")")
        fm, body = _existing(path, current)
        if body and not body.endswith("\n"):
            body += "\n"
        for line in lines:
            line = " ".join(line.strip().split("\n"))
            body += (line if line.startswith(("- ", "* ")) else f"- {line}") + "\n"
        _add_source(fm, ctx.source)
        stamp(fm)
        return frontmatter.render(fm, body)

    return _mutate(ctx, path, if_version, edit)


def _append_inbox(ctx: Context, lines: List[str], if_version: Optional[str]) -> Dict[str, Any]:
    """Appends to the inbox: create it if needed, go under a dated heading and tell the user."""
    entries = inbox.bullets(" ".join(line.split("\n")) for line in lines)
    heading = inbox.conversation_heading(ctx.source)

    def edit(current):
        if current is not None:
            _existing(inbox.PATH, current)  # refuse to rewrite an inbox we can't parse
        return inbox.with_entries(current, heading, entries, ctx.source)

    out = _mutate(ctx, inbox.PATH, if_version, edit, append=True)
    n = len(entries)
    if not ctx.tell_user(f"📥 Memory inbox: {n} new entr{'y' if n == 1 else 'ies'} ({inbox.PATH})"):
        out["next"] = ("Tell the user in one line that you added this to their memory inbox, "
                       "unless you propose it next.")
    return out


def _delete(args, ctx: Context):
    path = _str(args, "path")
    if_version = _str(args, "if_version")
    validate_path(path)
    ctx.scope.check_write(path)
    if if_version == NEW:
        raise ArgError("'if_version' must be the file's current version")
    ctx.store.delete(path, if_version)
    if ctx.on_write:
        ctx.on_write(path)
    return {"ok": True, "path": path}


def _propose(args, ctx: Context):
    if ctx.scope.read_only:
        raise StoreError("read_only", f"memory is read-only in this session ({ctx.scope.read_only_reason})")
    p = pending.propose(ctx.store, project=args.get("project"), new_project=args.get("new_project"),
                        files=args.get("files"), inbox_lines=args.get("inbox_lines"), summary=args.get("summary"),
                        known_projects=ctx.hermes_buckets)
    pid, commands = p["id"], f"/memory-apply {p['id']} saves it, /memory-reject {p['id']} discards it."
    shown = ctx.tell_user(f"📥 Memory proposal {pid} for {p['project']}: {p['summary']} {commands}")
    out = {"ok": True, "id": pid, "preview": pending.render(p, ctx.store)}
    if shown:
        out["next"] = "The user has been shown the proposal. Nothing is saved until they apply it."
    else:
        out["next"] = (f"Nothing is saved yet. Tell the user: {p['summary']} Then tell them: {commands} "
                       "Do not try to apply it yourself.")
    return out


_HANDLERS = {
    "memory_list": _list,
    "memory_read": _read,
    "memory_search": _search,
    "memory_write": _write,
    "memory_str_replace": _str_replace,
    "memory_append": _append,
    "memory_delete": _delete,
    "memory_propose": _propose,
}
