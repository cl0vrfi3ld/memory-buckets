"""Tool schemas and handlers.

Every result carries ``ok``. Errors are ``{"ok": false, "error": {"code", "message"}, ...}``:

- ``conflict`` adds ``current: {path, version, content}`` so the agent can merge and retry;
- ``out_of_scope`` adds ``allowed_prefixes``; the add-only refusal also adds ``add_only_buckets``.

The contract codes come from ``store.py``. The tool layer adds ``out_of_scope``,
``read_only`` and ``invalid_args``.

Write rights: a session writes fully inside ``scope.write_prefixes``.
Into another known bucket it may only append to existing files
(``memory_append``) and create new ones (``memory_write`` with ``if_version``
"new"); edits and deletes there are refused with an error naming both add tools.
Cross-project writes record ``<platform>@<session project or
"*">`` in ``sources`` and show the user a status line where one exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from . import frontmatter, inbox
from .config import Config
from .index import Index
from .scopes import Scope
from .store import MAX_FILE_BYTES, NEW, Store, StoreError, check_content, stamp, stem, validate_path, version

MAX_READ_PATHS = 20
MAX_SEARCH_LIMIT = 20

_PATH_HELP = ("Path relative to the memory root: lower-case, hyphens between words, ending in .md. Allowed forms: "
              "global/profile.md, global/preferences.md, global/topics/<name>.md, "
              "global/areas/<name>.md, global/people/<name>.md, <project>/profile.md, <project>/preferences.md, "
              "<project>/topics/<name>.md, <project>/areas/<name>.md, <project>/people/<name>.md.")
_EXISTING_PATH_HELP = "Path relative to the memory root, e.g. 'global/topics/nix.md' or '<project>/profile.md'."


def _fn(name: str, description: str, properties: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required, "additionalProperties": False}}


SCHEMAS: List[Dict[str, Any]] = [
    _fn("memory_list",
        "List memory files with their descriptions and versions. By default it lists global/ and the current "
        "project. To see another project, pass its directory as prefix (for example 'home-server/'). To list "
        "every project, set include_projects to true.",
        {"prefix": {"type": "string", "description": "Only list paths that start with this, for example 'global/people/'."},
         "include_projects": {"type": "boolean", "description": "Set to true to also list other projects' files."}}, []),
    _fn("memory_read",
        "Read one or more memory files. Each result has the file's content and its version; to edit a "
        "file later, pass that version as if_version. A file over 48 KiB comes back in pages: the result "
        "sets truncated and gives next_offset, which you pass back as offset to read the rest.",
        {"paths": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": MAX_READ_PATHS,
                   "description": _EXISTING_PATH_HELP},
         "offset": {"type": "integer", "minimum": 0,
                    "description": "Byte offset into a file that was truncated (use next_offset from the "
                                   "previous read). Only needed for files over 48 KiB."}}, ["paths"]),
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
        {"path": {"type": "string", "description": _EXISTING_PATH_HELP},
         "old": {"type": "string", "description": "The exact text to replace. It must appear exactly once."},
         "new": {"type": "string", "description": "The replacement text. Use an empty string to delete the text."},
         "if_version": {"type": "string", "description": "Optional: the version you read. The edit fails if the file has changed since."}},
        ["path", "old", "new"]),
    _fn("memory_append",
        "Add facts to the end of an existing memory file, one bullet point per fact. The file must already "
        "exist. To create a file, use memory_write with if_version \"new\".",
        {"path": {"type": "string", "description": _EXISTING_PATH_HELP},
         "lines": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                   "description": "One fact per item. '- ' is added to the start if it is missing."},
         "if_version": {"type": "string", "description": "Optional: the version you read. The edit fails if the file has changed since."}},
        ["path", "lines"]),
    _fn("memory_delete",
        "Delete a memory file. Set if_version to the file's current version, from memory_read or memory_list.",
        {"path": {"type": "string", "description": _EXISTING_PATH_HELP},
         "if_version": {"type": "string", "description": "The file's current version."}},
        ["path", "if_version"]),
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
    # reach the user directly (gateways; see provider._notifier). The notice is
    # best-effort and the result never asks the agent to relay it (D5).
    notify: Optional[Callable[[str], bool]] = None

    def tell_user(self, message: str) -> bool:
        try:
            return bool(self.notify and self.notify(message))
        except Exception:  # a broken UI callback must never fail the write
            return False

    def provenance(self, path: str) -> str:
        """The ``sources`` entry for a write to ``path``: ``<platform>@<project>``
        for a cross-project write (D4), the bare platform for the session's own."""
        if self.scope.cross_project_write(path):
            return f"{self.source}@{self.scope.project or '*'}"
        return self.source


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


def error(err: StoreError, path: Optional[str] = None, scope: Optional[Scope] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"ok": False, "error": {"code": err.code, "message": err.message}}
    if err.code == "conflict":
        if scope is not None and path and scope.cross_project_write(path):
            out["error"]["message"] = (
                f"{path} already exists and you may only add to {path.split('/', 1)[0]}/; "
                "use memory_append to add new facts to it"
            )
        else:
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
        return error(err, args.get("path") if isinstance(args.get("path"), str) else None, ctx.scope)


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
        raise ArgError(f"'paths' needs 1-{MAX_READ_PATHS} paths")
    offset = args.get("offset")
    if offset is None:
        offset = 0
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ArgError("'offset' must be a byte offset of 0 or more")
    files, errors = [], []
    for path in dict.fromkeys(paths):
        data = None
        try:
            data = ctx.store.read_bytes(path)
        except StoreError as err:
            errors.append({"path": path, "code": err.code, "message": err.message})
            continue
        if data is None:
            errors.append({"path": path, "code": "not_found", "message": f"{path} doesn't exist"})
            continue
        window = data[offset:offset + MAX_FILE_BYTES]
        # Don't cut inside a UTF-8 sequence: back up to its start. The next page
        # starts at the end of what was returned, so nothing is skipped. The
        # clamp keeps a hand-made file whose whole window is continuation bytes
        # from walking the boundary past the start of the file.
        end = offset + len(window)
        while end > offset and end < len(data) and (data[end] & 0xC0) == 0x80:
            end -= 1
        if end == offset and offset < len(data):
            # Degenerate: no character boundary anywhere in the window (only a
            # hand edit or sync can make one). Cut at the cap instead of
            # pinning the next offset in place; the next page starts
            # mid-sequence and decodes with replacement characters.
            end = offset + len(window)
        window = data[offset:end]
        entry = {"path": path, "version": version(data), "content": window.decode("utf-8", errors="replace")}
        if end < len(data):
            # A file the write path can't create (a hand edit or sync over the cap)
            # would flood the context whole; page it instead.
            entry["truncated"] = True
            entry["total_bytes"] = len(data)
            entry["next_offset"] = end
        elif offset > 0:
            entry["total_bytes"] = len(data)  # the last page says how big the file is
        files.append(entry)
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
                         f"{path}: can't parse its frontmatter ({err}); tell the user to fix it by hand") from None


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
            edit: Callable[[Optional[bytes]], str], *, append: bool = False,
            create: bool = False) -> Dict[str, Any]:
    validate_path(path)
    ctx.scope.check_write(path, append=append, create=create)
    result = ctx.store.update(path, lambda current: check_content(path, edit(current)), if_version)
    if ctx.on_write:
        ctx.on_write(path)
    if ctx.scope.cross_project_write(path):
        ctx.tell_user(f"📝 Saved to {path}")
    return {"ok": True, "path": path, "version": result["version"]}


def _has_frontmatter(text: str) -> bool:
    fn = getattr(frontmatter, "has_frontmatter", None)
    return fn(text) if fn is not None else text.replace("\r\n", "\n").startswith("---\n")


def _write(args, ctx: Context):
    path = _str(args, "path")
    description = " ".join(_str(args, "description").split())
    body = _str(args, "body", allow_empty=True)
    if_version = _str(args, "if_version")
    aliases = _str_list(args, "aliases", required=False)
    source = ctx.provenance(path)
    if _has_frontmatter(body):
        try:
            inner_fm, inner_body = frontmatter.parse(body)
        except frontmatter.FrontmatterError:
            pass
        else:
            if inner_fm.get("name") in (None, stem(path)) and inner_fm.get("description") is not None:
                body = inner_body

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
        _add_source(fm, source)
        stamp(fm)
        return frontmatter.render(fm, _body_text(body))

    return _mutate(ctx, path, if_version, edit, create=(if_version == NEW))


def _str_replace(args, ctx: Context):
    path = _str(args, "path")
    old = _str(args, "old")
    new = _str(args, "new", allow_empty=True)
    if_version = _str(args, "if_version", required=False)

    def edit(current):
        if current is None:
            raise StoreError("not_found", f"{path} doesn't exist")
        fm, body = _existing(path, current)
        if new == "" and "\n" not in old and f"\n{body}".count(f"\n{old}\n") == 1:
            replaced = body[len(old) + 1:] if body.startswith(f"{old}\n") else body.replace(f"\n{old}\n", "\n", 1)
        else:
            count = body.count(old)
            if count == 0:
                if old in current.decode("utf-8", errors="replace"):
                    raise StoreError(
                        "no_match",
                        f"memory_str_replace edits {path}'s body only, not its frontmatter; "
                        "use memory_write to change description or aliases",
                    )
                raise StoreError("no_match", f"the text to replace isn't in {path}'s body; memory_read it for the exact text")
            if count > 1:
                raise StoreError("ambiguous_match", f"the text occurs {count} times in {path}; include more context so it's unique")
            replaced = body.replace(old, new, 1)
        _add_source(fm, ctx.provenance(path))
        stamp(fm)
        return frontmatter.render(fm, _body_text(replaced))

    return _mutate(ctx, path, if_version, edit)


def _append(args, ctx: Context):
    path = _str(args, "path")
    lines = _str_list(args, "lines")
    if not lines or not all(line.strip() for line in lines):
        raise ArgError("'lines' needs at least one non-empty line")
    if_version = _str(args, "if_version", required=False)
    if path == inbox.PATH:
        raise StoreError("out_of_scope", "the inbox only holds imported memory; save the fact where it belongs.")

    def edit(current):
        if current is None:
            raise StoreError("not_found", f"{path} doesn't exist; to create it, use memory_write with if_version \"new\"")
        fm, body = _existing(path, current)
        if body and not body.endswith("\n"):
            body += "\n"
        for line in lines:
            line = " ".join(line.strip().split("\n"))
            body += (line if line.startswith(("- ", "* ")) else f"- {line}") + "\n"
        _add_source(fm, ctx.provenance(path))
        stamp(fm)
        return frontmatter.render(fm, body)

    return _mutate(ctx, path, if_version, edit, append=True)


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


_HANDLERS = {
    "memory_list": _list,
    "memory_read": _read,
    "memory_search": _search,
    "memory_write": _write,
    "memory_str_replace": _str_replace,
    "memory_append": _append,
    "memory_delete": _delete,
}
