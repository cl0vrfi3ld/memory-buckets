"""``hermes memory-buckets``: status, lint, reindex, search, import, proposals.

Hermes imports the plugin directory's ``cli.py`` (which re-exports ``register_cli``
from here) only while memory-buckets is the active provider. It looks for
``<provider>_command``, which can't exist for a hyphenated name, so
``register_cli`` sets ``func`` itself. Config comes from Hermes's config.yaml.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import List

from . import frontmatter, inbox, pending, projects
from . import config as config_mod
from . import static_model
from ._paths import SKILLS_DIR
from .embeddings import EmbeddingError, make_embedder, min_similarity
from .index import Index
from .store import GLOBAL, MAX_FILE_BYTES, Store, StoreError, stem, validate_path
from .tools import TOOL_NAMES

ENTRY_DELIMITER = "\n§\n"  # Hermes tools/memory_tool_store.py

SKILL_FILE = SKILLS_DIR / "sort-inbox" / "SKILL.md"


def sort_prompt() -> str:
    """The sort-inbox skill's body: the same instructions, pasted as a prompt."""
    text = SKILL_FILE.read_text()
    return frontmatter.parse(text)[1].strip() if text.startswith("---") else text.strip()


LINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")


# -- setup ---------------------------------------------------------------------

def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except ImportError:
        return Path(os.environ.get("HERMES_HOME") or "~/.hermes").expanduser()


def _open(args):
    cfg = config_mod.load()
    home = _hermes_home()
    root = Path(cfg.store_path or home / "memory-buckets").expanduser()
    embedder = make_embedder(cfg, root)
    store = Store(root)
    return cfg, home, store, Index(store, embedder)


def _out(line: str = "") -> None:
    print(line)


# -- commands --------------------------------------------------------------------

def cmd_status(args) -> int:
    cfg, home, store, index = _open(args)
    problems = 0
    counts = defaultdict(int)
    for path in store.iter_paths():
        counts[path.split("/", 1)[0]] += 1
    scopes = ", ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: (kv[0] != GLOBAL, kv[0])))
    _out(f"store       {store.root} ({'exists' if store.memories.is_dir() else 'not created yet'}"
         f"{'; ' + scopes if scopes else ''})")
    if not store.memories.is_dir():
        _out("            (created when an agent loads the provider: set memory.provider, then start a new "
             "session or restart the gateway; with profiles, look under that profile's home)")
    if store.memories.is_dir():
        index.reconcile()
        s = index.stats()
        _out(f"index       {s['files']} files, {s['chunks']} chunks, {s['embedded']} embedded, {s['backlog']} waiting")
    if index.embedder is None:
        _out("embeddings  off (keyword search only)")
    else:
        started = time.monotonic()
        try:
            vec = index.embedder.embed_query("memory-buckets status probe")
            _out(f"embeddings  {index.embedder.describe()}: ok, {len(vec)} dims, "
                 f"{(time.monotonic() - started) * 1000:.0f} ms; prefetch threshold "
                 f"{min_similarity(cfg, index.embedder):g}")
        except EmbeddingError as err:
            # Not a fault: search is keyword-only until the model is here.
            _out(f"embeddings  built-in {static_model.MODEL_NAME}: not ready ({err})")
    problems += _status_hermes(home)
    _status_projects(store)
    if (store.memories / inbox.PATH).exists():
        _out(f"inbox       {inbox.PATH} is waiting to be sorted")
    index.close()
    return 1 if problems else 0


def _status_hermes(home: Path) -> int:
    """Hermes-side checks, only possible when running inside Hermes's Python."""
    try:
        from hermes_cli.config import cfg_get, load_config_readonly
    except ImportError:
        _out("hermes      not importable here; run `hermes memory-buckets status` for config checks")
        return 0
    problems = 0
    cfg = load_config_readonly()
    provider = cfg_get(cfg, "memory", "provider")
    if provider != "memory-buckets":
        problems += 1
    _out(f"hermes      home {home}; memory.provider = {provider or '(unset)'}"
         f"{'' if provider == 'memory-buckets' else '  <- set it to memory-buckets'}")
    try:
        from tools.memory_tool import get_builtin_memory_store_flags
        mem, user = get_builtin_memory_store_flags(cfg)
        state = "off" if not (mem or user) else f"ON (memory_enabled={mem}, user_profile_enabled={user})"
        _out(f"built-in    memory {state}")
    except ImportError:
        pass
    try:
        from toolsets import _HERMES_CORE_TOOLS
        clashes = sorted(set(TOOL_NAMES) & set(_HERMES_CORE_TOOLS))
        if clashes:
            problems += 1
        _out(f"tools       {len(TOOL_NAMES)} memory tools; "
             + (f"CLASH with Hermes core tools (ours are ignored): {', '.join(clashes)}" if clashes else "no clashes with core tools"))
    except ImportError:
        pass
    return problems


def _status_projects(store: Store) -> None:
    """Hermes projects against the store's buckets: a bucket is used only by the project with its slug."""
    known = projects.list_projects()
    if known is None:
        return
    buckets = set(pending.projects(store)) if store.memories.is_dir() else set()
    linked = sorted(p.bucket for p in known if p.bucket in buckets)
    _out(f"projects    {len(known)} Hermes project(s); {len(linked)} with a bucket"
         + (f" ({', '.join(linked)})" if linked else ""))
    orphans = sorted(buckets - {p.bucket for p in known})
    if orphans:
        _out(f"            buckets with no Hermes project (no session is scoped to them): {', '.join(orphans)}")
        _out("            link one with: hermes project create \"<name>\" <folder> --slug <bucket>")


def cmd_diagnose(args) -> int:
    """Why an agent would or wouldn't see the memory tools. Read-only; needs Hermes's Python."""
    try:
        from hermes_cli.config import cfg_get, load_config_readonly
        from agent.memory_manager import memory_provider_tools_enabled
        from plugins.memory import find_provider_dir, find_provider_entry_point, load_memory_provider
    except ImportError:
        _out("diagnose needs Hermes's Python: run `hermes memory-buckets diagnose`")
        return 2
    problems = 0

    def say(ok: bool, text: str) -> None:
        nonlocal problems
        problems += 0 if ok else 1
        _out(f"{'ok  ' if ok else 'FAIL'}  {text}")

    cfg = load_config_readonly()
    provider = cfg_get(cfg, "memory", "provider")
    say(provider == "memory-buckets", f"memory.provider = {provider or '(unset)'}")
    disabled = cfg_get(cfg, "plugins", "disabled") or []
    say("memory-buckets" not in (disabled if isinstance(disabled, list) else []),
        "not in plugins.disabled" if "memory-buckets" not in (disabled or []) else "memory-buckets is in plugins.disabled")

    where = find_provider_dir("memory-buckets")
    ep = None if where else find_provider_entry_point("memory-buckets")
    say(bool(where or ep), f"found at {where}" if where else (f"found by entry point {ep.value}" if ep else "not found in any plugin dir or entry point"))
    instance = load_memory_provider("memory-buckets", register_skills=False) if (where or ep) else None
    say(instance is not None, "loads and registers" if instance is not None else "failed to load (see Hermes's log for the traceback)")
    if instance is not None:
        say(instance.is_available(), "is_available()" if instance.is_available() else f"unavailable: {instance.unavailable_reason()}")
        loaded_from = sys.modules[type(instance).__module__].__file__
        _out(f"      running code from {Path(loaded_from).parent}")
        try:
            from toolsets import _HERMES_CORE_TOOLS
            clashes = sorted(set(TOOL_NAMES) & set(_HERMES_CORE_TOOLS))
            say(not clashes, "no tool-name clashes with Hermes core" if not clashes else f"core tools shadow ours: {clashes}")
        except ImportError:
            pass
        _diagnose_skill(SKILLS_DIR, say)

    try:
        from tools.memory_tool import get_builtin_memory_store_flags
        builtin_on = any(get_builtin_memory_store_flags(cfg))
    except ImportError:
        builtin_on = False
    try:
        from hermes_cli.tools_config import _get_platform_tools
    except ImportError:
        _out("      (this Hermes has no hermes_cli.tools_config._get_platform_tools: can't check per-platform toolsets)")
        return 1 if problems else 0
    agent_disabled = cfg_get(cfg, "agent", "disabled_toolsets") or None
    platforms = args.platform or sorted({"cli", "api_server", *((cfg.get("platform_toolsets") or {}).keys())})
    for platform in platforms:
        try:
            enabled = sorted(_get_platform_tools(cfg, platform))
        except Exception as err:  # noqa: BLE001 - diagnostic: report, don't crash
            say(False, f"{platform}: couldn't resolve its toolsets ({type(err).__name__}: {err})")
            continue
        memory_tool = builtin_on and memory_provider_tools_enabled(enabled, agent_disabled)
        exposed = memory_provider_tools_enabled(enabled, agent_disabled, memory_tool_present=memory_tool)
        say(exposed, f"{platform}: memory tools and prompt block "
            + ("exposed" if exposed else "WITHHELD, because the `memory` toolset isn't enabled for this platform "
               "(platform_toolsets / agent.disabled_toolsets)"))
    if not problems:
        _out("")
        _out("Everything checks out. If a chat still can't see the tools, it's probably an agent built before the change:")
        _out("start a new session (/new), or restart the gateway.")
    return 1 if problems else 0


def _diagnose_skill(skills_dir: Path, say) -> None:
    """The sort-inbox skill and its /sort-inbox command (README: Sorting the inbox)."""
    skill_md = skills_dir / "sort-inbox" / "SKILL.md"
    say(skill_md.is_file(), f"skill file {skill_md}" if skill_md.is_file()
        else f"no sort-inbox/SKILL.md in {skills_dir}: this copy of the plugin is out of date")
    if not skill_md.is_file():
        return
    try:
        from agent.skill_commands import scan_skill_commands
        from agent.skill_utils import get_external_skills_dirs, get_skills_dir
    except ImportError:
        _out("      (this Hermes has no agent.skill_commands: can't check /sort-inbox)")
        return
    skills_dir = skills_dir.resolve()
    external = [d.resolve() for d in get_external_skills_dirs()]
    if skills_dir in external:
        _out(f"ok    skills.external_dirs includes {skills_dir}")
    else:
        _out("note  /sort-inbox is off: skills.external_dirs doesn't include this plugin's skills.")
        _out("      Asking the agent to sort the inbox still works. To get /sort-inbox, add to config.yaml:")
        _out(f"        skills:\n          external_dirs:\n            - {skills_dir}")
    entry = scan_skill_commands().get("/sort-inbox")
    if entry is None:
        if skills_dir in external:
            say(False, "/sort-inbox isn't registered, although the directory is configured (see Hermes's log)")
    else:
        ours = Path(entry["skill_md_path"]).resolve() == skill_md.resolve()
        say(ours, "/sort-inbox → this plugin's skill" if ours
            else f"/sort-inbox is claimed by another skill: {entry['skill_md_path']}")
        _out("      A running chat or gateway keeps its old skill list: run /reload-skills, or restart the gateway.")
    # Copies of an older sort prompt saved as local skills shadow the real one.
    local = get_skills_dir()
    stale = []
    if local.is_dir():
        for md in local.rglob("SKILL.md"):
            try:
                text = md.read_text(errors="replace")
            except OSError:
                continue
            if "global/inbox.md" in text and md.resolve() != skill_md.resolve():
                stale.append(md)
    for md in stale:
        say(False, f"local skill {md} is a copy of a sort prompt; it goes stale, so delete it")


def lint(store: Store) -> List[str]:
    """Problems in the store, as human-readable lines."""
    problems: List[str] = []
    names = defaultdict(list)  # (scope, name) -> paths
    stems = defaultdict(set)   # scope -> stems, for links
    texts = {}
    if not store.memories.is_dir():
        return problems
    for dirpath, dirnames, filenames in os.walk(store.memories):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        rel_dir = os.path.relpath(dirpath, store.memories)
        for d in dirnames:
            if os.path.islink(os.path.join(dirpath, d)):
                problems.append(f"{os.path.normpath(os.path.join(rel_dir, d))}: symlinked directory (ignored)")
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                problems.append(f"{rel}: symlink (ignored)")
                continue
            try:
                validate_path(rel)
            except StoreError as err:
                problems.append(f"{rel}: not a memory path, ignored ({err.message.split(': ', 1)[-1]})")
                continue
            data = Path(full).read_bytes()
            if len(data) > MAX_FILE_BYTES:
                problems.append(f"{rel}: {len(data)} bytes, over the {MAX_FILE_BYTES}-byte cap")
            text = data.decode("utf-8", errors="replace")
            try:
                fm, body = frontmatter.parse(text)
                if not fm.entries:
                    raise frontmatter.FrontmatterError("no frontmatter")
                frontmatter.validate(fm, stem(rel))
            except frontmatter.FrontmatterError as err:
                problems.append(f"{rel}: bad frontmatter ({err}); tools won't edit it until it's fixed")
                body = text
            scope = rel.split("/", 1)[0]
            names[(scope, stem(rel))].append(rel)
            stems[scope].add(stem(rel))
            texts[rel] = body
    for (scope, name), paths in sorted(names.items()):
        if len(paths) > 1:
            problems.append(f"{scope}/: name '{name}' is used by {', '.join(paths)}; [[{name}]] is ambiguous")
    for rel, body in sorted(texts.items()):
        scope = rel.split("/", 1)[0]
        for target in LINK_RE.findall(body):
            target = target.strip().removesuffix(".md")
            if "/" in target:
                ok = (store.memories / f"{target}.md").exists()
            else:
                ok = target in stems[scope] or target in stems[GLOBAL]
            if not ok:
                problems.append(f"{rel}: broken link [[{target}]]")
    return problems


def cmd_lint(args) -> int:
    _, _, store, _ = _open(args)
    problems = lint(store)
    for line in problems:
        _out(line)
    if (store.memories / inbox.PATH).exists():
        _out(f"note: {inbox.PATH} still exists; sort it into files, then delete it")
    _out(f"{len(problems)} problem(s)")
    return 1 if problems else 0


def cmd_reindex(args) -> int:
    _, _, store, index = _open(args)
    index.rebuild()
    counts = index.reconcile()
    _out(f"indexed {counts['changed']} file(s)")
    if args.embed:
        if index.embedder is None:
            _out("embeddings are off; nothing to embed")
        else:
            done = index.embed_backlog(max_batches=None)
            left = index.backlog()
            _out(f"embedded {done} chunk(s)" + (f"; {left} left (is the model here yet?)" if left else ""))
    index.close()
    return 0


def cmd_search(args) -> int:
    _, _, store, index = _open(args)
    prefix = args.prefix or ""
    found = index.search(args.query, lambda p: p.startswith(prefix), limit=args.limit)
    index.close()
    if args.json:
        _out(json.dumps(found, indent=2, ensure_ascii=False))
        return 0
    if found.get("note"):
        _out(f"({found['note']})")
    for r in found["results"]:
        _out(f"{r['score']:.4f}  {r['path']}: {r['description']}")
        _out(f"        {r['snippet']}")
    if not found["results"]:
        _out("no matches")
    return 0


def cmd_hints(args) -> int:
    """Raw similarities for a query, to tune prefetch_min_similarity for your model."""
    cfg, _, store, index = _open(args)
    if index.embedder is None:
        _out("embeddings are off (plugins.memory-buckets.embeddings.backend: off)")
        return 1
    index.reconcile()
    index.embed_backlog(max_batches=None)
    threshold = min_similarity(cfg, index.embedder) if args.threshold is None else args.threshold
    hints = index.hints(args.query, lambda p: True, min_similarity=-1.0, max_hints=args.limit)
    index.close()
    if not hints:
        _out(f"no vectors to compare against ({index.embedder.down_reason() or 'is the store empty?'})")
        return 1
    shown_cut = False
    for path, description, sim in hints:
        if sim < threshold and not shown_cut:
            _out(f"------  prefetch_min_similarity = {threshold:g}: prefetch hints only the files above this line")
            shown_cut = True
        _out(f"{sim:.3f}  {path}: {description}")
    if not shown_cut:
        _out(f"------  prefetch_min_similarity = {threshold:g}: every file above would be hinted")
    return 0


def cmd_fetch_model(args) -> int:
    """Download the built-in embedding model now (normally automatic, in the background)."""
    cfg, _, store, _ = _open(args)
    found = static_model.locate(store.root, cfg.embeddings.local_model_dir)
    if found is not None and not args.force:
        _out(f"built-in model already available at {found}")
        return 0
    try:
        dest = static_model.fetch(store.root, progress=lambda msg: _out(msg), timeout=120.0)
    except (OSError, static_model.ModelUnavailable) as err:
        _out(f"error: {err}")
        return 1
    _out(f"built-in model ready at {dest}")
    return 0


def _builtin_entries(memories_dir: Path) -> List[tuple]:
    out = []
    for name in ("USER.md", "MEMORY.md"):
        path = memories_dir / name
        if path.is_file():
            entries = [e.strip() for e in path.read_text(errors="replace").split(ENTRY_DELIMITER)]
            out.append((name, path, [e for e in entries if e]))
    return out


def cmd_import(args) -> int:
    _, home, store, _ = _open(args)
    source_home = Path(args.from_home).expanduser() if args.from_home else home
    found = _builtin_entries(source_home / "memories")
    if not found:
        _out(f"nothing to import: no MEMORY.md or USER.md in {source_home / 'memories'}")
        return 1
    today = date.today().isoformat()
    for name, path, entries in found:
        if args.dry_run:
            _out(f"would import {len(entries)} entr{'y' if len(entries) == 1 else 'ies'} from {path}")
            continue
        heading = f"imported {name} from {source_home} ({today})"
        inbox.append(store, heading, entries, source="cli:import")
        _out(f"imported {len(entries)} entr{'y' if len(entries) == 1 else 'ies'} from {path} into {inbox.PATH}")
    if not args.dry_run:
        _print_sort_prompt(store)
    return 0


def _print_sort_prompt(store: Store) -> None:
    _out()
    _out("Next:")
    _out(f"  1. Back up the store first: cp -a {store.root} {store.root}.before-sort")
    _out("  2. In a new chat that isn't in a project, ask the agent to sort your memory inbox")
    _out("     (it uses memory-buckets's sort-inbox skill), or paste the prompt below.")
    _out("  3. It files general facts itself and proposes project facts (and new projects).")
    _out("     Apply each proposal yourself: /memory-pending, /memory-apply <id>, /memory-reject <id>")
    _out("     in chat, or hermes memory-buckets pending / apply / reject here.")
    _out(f"  4. Review with: diff -ru {store.root}.before-sort/memories {store.memories}")
    _out(f"     then delete {inbox.PATH}. (Print this again with: hermes memory-buckets sort-prompt)")
    _out()
    _out("----- prompt -----")
    _out(sort_prompt())
    _out("------------------")


def cmd_sort_prompt(args) -> int:
    _, _, store, _ = _open(args)
    if not (store.memories / inbox.PATH).exists():
        _out(f"(no {inbox.PATH} in {store.root}: run import first)")
        return 1
    _print_sort_prompt(store)
    return 0


def cmd_pending(args) -> int:
    _, _, store, _ = _open(args)
    if not args.id:
        _out(pending.summary_text(store).replace("/memory-pending <id>", "hermes memory-buckets pending <id>")
             .replace("/memory-apply <id>, /memory-reject <id>",
                      "hermes memory-buckets apply <id>, hermes memory-buckets reject <id>"))
        return 0
    try:
        _out(pending.render(pending.load(store, args.id), store))
    except StoreError as err:
        _out(f"error: {err.message}")
        return 1
    return 0


def cmd_apply(args) -> int:
    _, _, store, _ = _open(args)
    try:
        proposal = pending.load(store, args.id)
    except StoreError as err:
        _out(f"error: {err.message}")
        return 1
    # Applying is the user's decision: insist on a person at a terminal.
    if not sys.stdin.isatty():
        _out("error: apply asks for confirmation, so it needs an interactive terminal")
        return 2
    _out(pending.render(proposal, store))
    try:
        answer = input(f"Apply {proposal['id']}? [y/N] ")
    except EOFError:
        answer = ""
    if answer.strip().lower() not in ("y", "yes"):
        _out("not applied")
        return 1
    try:
        done = pending.apply(store, proposal["id"])
    except StoreError as err:
        _out(f"not applied: {err.message}")
        return 1
    _out(f"applied {done['id']} to {done['project']}: " + ", ".join(done["written"]))
    note = projects.unlinked_note(done["project"])
    if note:
        _out(note)
    return 0


def cmd_reject(args) -> int:
    _, _, store, _ = _open(args)
    try:
        p = pending.reject(store, args.id)
    except StoreError as err:
        _out(f"error: {err.message}")
        return 1
    _out(f"rejected {p['id']} ({p['project']}); its lines stay in {inbox.PATH}")
    return 0


# -- argparse ----------------------------------------------------------------------

def _add_commands(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="bm_command", metavar="<command>")
    p = sub.add_parser("status", help="Store, index, embeddings and Hermes config at a glance")
    p.set_defaults(bm_func=cmd_status)
    p = sub.add_parser("diagnose", help="Why an agent would or wouldn't see the memory tools (inside Hermes)")
    p.add_argument("--platform", action="append", help="Platform to check (repeatable); default: cli, api_server and configured ones")
    p.set_defaults(bm_func=cmd_diagnose)
    p = sub.add_parser("lint", help="Check paths, frontmatter, duplicate names and [[links]]")
    p.set_defaults(bm_func=cmd_lint)
    p = sub.add_parser("reindex", help="Delete and rebuild the search index")
    p.add_argument("--embed", action="store_true", help="Also embed everything now")
    p.set_defaults(bm_func=cmd_reindex)
    p = sub.add_parser("search", help="Search the whole store, like memory_search")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=8)
    p.add_argument("--prefix", help="Only paths starting with this, e.g. global/")
    p.add_argument("--json", action="store_true")
    p.set_defaults(bm_func=cmd_search)
    p = sub.add_parser("hints", help="Show raw similarities for a query, to tune prefetch_min_similarity")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--threshold", type=float, help="Draw the cut here instead of the configured value")
    p.set_defaults(bm_func=cmd_hints)
    p = sub.add_parser("fetch-model", help="Download the built-in embedding model now (otherwise it's automatic)")
    p.add_argument("--force", action="store_true", help="Download even if a model is already available")
    p.set_defaults(bm_func=cmd_fetch_model)
    p = sub.add_parser("import", help="Append built-in MEMORY.md/USER.md entries to global/inbox.md")
    p.add_argument("--from", dest="from_home", metavar="HERMES_HOME",
                   help="Import another instance's built-in memory (its HERMES_HOME); default: this one")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(bm_func=cmd_import)
    p = sub.add_parser("sort-prompt", help="Print the prompt that asks the agent to sort global/inbox.md")
    p.set_defaults(bm_func=cmd_sort_prompt)
    p = sub.add_parser("pending", help="List proposals from inbox sorting, or show one")
    p.add_argument("id", nargs="?")
    p.set_defaults(bm_func=cmd_pending)
    p = sub.add_parser("apply", help="Commit one proposal (asks for confirmation)")
    p.add_argument("id")
    p.set_defaults(bm_func=cmd_apply)
    p = sub.add_parser("reject", help="Discard one proposal")
    p.add_argument("id")
    p.set_defaults(bm_func=cmd_reject)


def _dispatch(args) -> int:
    func = getattr(args, "bm_func", None)
    if func is None:
        print("usage: pick a command: status, diagnose, lint, reindex, search, hints, fetch-model, import, sort-prompt, pending, apply, reject (--help for more)", file=sys.stderr)
        return 2
    return func(args)


def register_cli(subparser) -> None:
    """``hermes memory-buckets ...``"""
    _add_commands(subparser)
    subparser.set_defaults(func=lambda args: sys.exit(_dispatch(args)))
