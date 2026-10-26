"""``hermes memory-buckets``: status, lint, migrate, reindex, search, import.

Hermes imports the plugin directory's ``cli.py`` (which re-exports ``register_cli``
from here) only while memory-buckets is the active provider. It looks for
``<provider>_command``, which can't exist for a hyphenated name, so
``register_cli`` sets ``func`` itself. Config comes from Hermes's config.yaml.
"""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import re
import sys
import time
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import List, Optional, Tuple

from . import _compat, frontmatter, inbox, migrate, projects
from . import config as config_mod
from . import static_model
from ._paths import SKILLS_DIR
from .embeddings import EmbeddingError, make_embedder, min_similarity
from .index import Index, IndexInUse
from .store import GLOBAL, MAX_FILE_BYTES, Store, StoreError, is_link, stem, validate_path
from .tools import TOOL_NAMES

ENTRY_DELIMITER = "\n§\n"  # Hermes tools/memory_tool_store.py

SKILL_FILE = SKILLS_DIR / "sort-imports" / "SKILL.md"


def import_prompt() -> str:
    """The sort-imports skill's body: the same instructions, pasted as a prompt."""
    text = SKILL_FILE.read_text(encoding="utf-8")
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
    cfg.embeddings.auto_download = False
    home = _hermes_home()
    root = Path(cfg.store_path or home / "memory-buckets").expanduser()
    embedder = make_embedder(cfg, root)
    store = Store(root, history_keep=cfg.history_keep, history_days=cfg.history_days)
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
        oversized = [p for p in store.iter_paths()
                     if (store.memories / p).stat().st_size > MAX_FILE_BYTES]
        if oversized:
            # A file over the write cap can only have come from a hand edit or a
            # sync; no memory tool can rewrite it whole.
            _out(f"oversized   {'; '.join(oversized)}: over the {MAX_FILE_BYTES}-byte write cap; "
                 "the tools can't rewrite one, so fix or split it by hand")
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
    loose = store.loose_modes()
    if loose:
        first, mode = loose[0]
        _out(f"permissions {len(loose)} path(s) in the store can be read by group or other "
             f"(first: {'the store root' if first == '.' else first}, mode {mode:04o}); "
             "run `hermes memory-buckets migrate` to make them private")
    leftovers = migrate.leftover_indexes(store)
    if leftovers:
        _out(f"migration   index.md left over in: {', '.join(leftovers)}; run `hermes memory-buckets migrate`")
    if migrate.inbox_needs_migrating(store):
        _out(f"migration   {inbox.PATH} has an outdated description; run `hermes memory-buckets migrate`")
    pending = _leftover_pending(store)
    if pending:
        _out(f"migration   {migrate.PENDING_DIR}/ left over from 0.1.0 ({pending}); run `hermes memory-buckets migrate`")
    waiting = _inbox_entries(store)
    if waiting:
        _out(f"inbox       {inbox.PATH}: {_entries(waiting)} waiting to be sorted")
    _status_skipped(store)
    _status_blocked(store)
    index.close()
    return 1 if problems else 0


def _plural(n: int, singular: str, plural: str = "") -> str:
    return f"{n} {singular if n == 1 else plural or singular + 's'}"


def _status_skipped(store: Store) -> None:
    """What the agent can't see or edit in the store. Obsidian lets you create files the
    path rules reject, and they'd otherwise vanish without a word. Not a fault, so it
    doesn't fail the command; ``lint`` has the per-file detail."""
    # A leftover project index.md has its own `migration` line above.
    leftovers = {f"{d}/index.md" for d in migrate.leftover_indexes(store)}
    skipped = [(path, why) for path, why in store.iter_skipped() if path not in leftovers]
    outside = sum(1 for _, why in skipped if why == "path")
    links = len(skipped) - outside
    if outside:
        _out(f"ignored     {_plural(outside, 'file')} outside the path rules, so the agent can't see "
             f"{'it' if outside == 1 else 'them'}; run `hermes memory-buckets lint`")
    if links:
        _out(f"ignored     {_plural(links, 'symlink')} (never followed); run `hermes memory-buckets lint`")
    broken = 0
    for path in store.iter_paths():
        try:
            text = (store.memories / path).read_bytes().decode("utf-8", errors="replace")
        except OSError:
            continue  # gone since the walk; lint reports real read errors
        if _check_frontmatter(text, path)[1] is not None:
            broken += 1
    if broken:
        _out(f"frontmatter {_plural(broken, 'file')} with invalid frontmatter, so tools won't edit "
             f"{'it' if broken == 1 else 'them'} until it's fixed; run `hermes memory-buckets lint`")


def _check_frontmatter(text: str, rel: str):
    """``(body, error)``: ``error`` is the reason the file's frontmatter is unusable, else None.
    The one test both ``lint`` and ``status`` apply."""
    try:
        fm, body = frontmatter.parse(text)
        if not fm.entries:
            raise frontmatter.FrontmatterError("no frontmatter")
        frontmatter.validate(fm, stem(rel))
    except frontmatter.FrontmatterError as err:
        return text, err
    return body, None


INLINE_TAILS = ("profile.md", "preferences.md")  # snapshot.build inlines these whole


def _blocked_reason(text: str) -> Optional[str]:
    """Why Hermes's threat scanner keeps ``text`` out of the prompt, None when it passes.
    The two rejections of ``snapshot._screened``, as one question lint and status can ask
    without the warning that belongs to prompt time. Outside Hermes there is no scanner
    (scan_threats returns [] and scan_window_chars is None), so nothing is reported off."""
    window = _compat.scan_window_chars
    if window is not None and len(text) > window:
        return f"over the {window}-character threat-scan window"
    patterns = _compat.scan_threats(text)
    if patterns:
        return f"matched threat pattern(s) {', '.join(patterns)}"
    return None


def _scanner_blocking(store: Store) -> List[Tuple[str, str]]:
    """``(path, reason)`` for what Hermes's threat scanner keeps out of the prompt.

    ``snapshot._screened`` silently blocks two kinds of text: the file-listing
    descriptions (which also feed the prefetch hints), and the whole bodies of the
    files it inlines, */profile.md and */preferences.md. Hand edits and synced files never
    pass the write-time scan in ``tools``, so a store can lose a file from every prompt
    after an upgrade with only Hermes's log as signal; issue #49 asks lint and status to
    name them. Other files' bodies do not enter the prompt (memory_read returns them raw
    on request, a deliberate choice), so their body matches are not reported here;
    ``inbox.md`` is one of them, its note being fixed text, so only its description
    is checked."""
    findings: List[Tuple[str, str]] = []
    for rel in sorted(store.iter_paths()):
        try:
            text = (store.memories / rel).read_bytes().decode("utf-8", errors="replace")
        except OSError:
            continue  # gone since the walk; lint's own read reports real errors
        body, err = _check_frontmatter(text, rel)
        if err is None:
            fm, _ = frontmatter.parse(text)
            desc = fm.get("description")
            desc = desc if isinstance(desc, str) else ""
            if desc:
                reason = _blocked_reason(desc)
                if reason:
                    findings.append((rel, f"description {reason}; "
                                          "kept out of every prompt listing and prefetch hint; fix or delete it"))
        if rel.rsplit("/", 1)[-1] in INLINE_TAILS:
            screened = text if err is not None else body.strip()
            if screened:
                reason = _blocked_reason(screened)
                if reason:
                    findings.append((rel, f"body {reason}; the snapshot inlines the "
                                          "whole file; fix or delete it by hand"))
    return findings


def _status_blocked(store: Store) -> None:
    """What the scanner keeps out of the agent's prompt. Not a fault, so it doesn't
    fail the command; ``lint`` has the per-file detail."""
    blocked = _scanner_blocking(store)
    files = {rel for rel, _ in blocked}
    if files:
        _out(f"blocked     {_plural(len(files), 'file')} whose text Hermes's threat scanner "
             f"rejects, so it stays out of the prompt; run `hermes memory-buckets lint` for details")


def _builtin_memory_flags(cfg: dict, cfg_get) -> Tuple[bool, bool]:
    """(memory_enabled, user_profile_enabled) from config.yaml's memory section (default True)."""
    def flag(key: str) -> bool:
        val = cfg_get(cfg, "memory", key)
        if val is None and isinstance(cfg, dict) and isinstance(cfg.get("memory"), dict):
            val = cfg["memory"].get(key)
        if val is None:
            return True
        if isinstance(val, str):
            return val.strip().lower() in ("1", "true", "yes", "on")
        return bool(val)
    return flag("memory_enabled"), flag("user_profile_enabled")


def _core_tool_clashes() -> Optional[List[str]]:
    """Tool names in TOOL_NAMES that clash with Hermes's built-in tool registry, or None outside Hermes."""
    try:
        from tools.registry import discover_builtin_tools, registry
        discover_builtin_tools()
        return sorted(set(TOOL_NAMES) & set(registry.get_all_tool_names()))
    except ImportError:
        return None


def _parse_toolset_list(raw) -> Optional[List[str]]:
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    if isinstance(raw, str) and raw.strip().startswith("["):
        import ast
        try:
            parsed = ast.literal_eval(raw.strip())
        except (ValueError, SyntaxError):
            return None
        if isinstance(parsed, list):
            return [str(x).strip() for x in parsed if str(x).strip()]
    return None


def _platform_toolsets(cfg: dict, platform: str) -> List[str]:
    pt = cfg.get("platform_toolsets") if isinstance(cfg, dict) else None
    raw = pt.get(platform) if isinstance(pt, dict) else None
    configured = _parse_toolset_list(raw)
    if configured is None:
        return [f"hermes-{platform.replace('_', '-')}"]
    return [x for item in configured for x in (("hermes-cli", "hermes-api-server") if item == "hermes" else (item,))]


def _find_plugin_dir() -> Optional[Path]:
    """User or project plugin directory for memory-buckets, if installed as a directory plugin."""
    candidates = [_hermes_home() / "plugins" / "memory-buckets"]
    if os.environ.get("HERMES_ENABLE_PROJECT_PLUGINS", "").strip().lower() in ("1", "true", "yes", "on"):
        candidates.append(Path.cwd() / ".hermes" / "plugins" / "memory-buckets")
    for candidate in candidates:
        if candidate.is_dir() and (candidate / "__init__.py").is_file():
            return candidate
    return None


def _find_entry_point():
    """The hermes_agent.memory_providers entry point for memory-buckets, if installed as a package."""
    try:
        import importlib.metadata
        eps = importlib.metadata.entry_points()
        group = (eps.select(group="hermes_agent.memory_providers")
                 if hasattr(eps, "select") else eps.get("hermes_agent.memory_providers", ()))
        return next((ep for ep in group if getattr(ep, "name", None) == "memory-buckets"), None)
    except Exception:
        return None


def _probe_provider(where: Optional[Path], ep, *, is_disabled: bool):
    """Instantiate the provider through register(ctx) without side effects."""
    if is_disabled and where is not None:
        return None
    provider = None

    class _Collector:
        name = "memory-buckets"

        def register_memory_provider(self, p):
            nonlocal provider
            provider = p

        def register_skill(self, *args, **kwargs):
            pass

    try:
        if where is None and ep is not None:
            loaded = ep.load()
            target = getattr(loaded, "register", loaded)
            target(_Collector())
        else:
            from . import register
            register(_Collector())
    except Exception:
        return None
    return provider


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
    mem, user = _builtin_memory_flags(cfg, cfg_get)
    state = "off" if not (mem or user) else f"ON (memory_enabled={mem}, user_profile_enabled={user})"
    _out(f"built-in    memory {state}")
    clashes = _core_tool_clashes()
    if clashes is not None:
        if clashes:
            problems += 1
        _out(f"tools       {len(TOOL_NAMES)} memory tools; "
             + (f"CLASH with Hermes core tools (ours are ignored): {', '.join(clashes)}" if clashes else "no clashes with core tools"))
    return problems


def _status_projects(store: Store) -> None:
    """Hermes projects against the store's buckets: a bucket is used only by the project with its slug."""
    known = projects.list_projects()
    if known is None:
        return
    buckets = set(store.projects())
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
    is_disabled = "memory-buckets" in (disabled if isinstance(disabled, list) else [])
    say(not is_disabled,
        "not in plugins.disabled" if not is_disabled else "memory-buckets is in plugins.disabled")

    where = _find_plugin_dir()
    ep = None if where is not None else _find_entry_point()
    say(bool(where or ep), f"found by entry point {ep.value}" if ep else (f"found at {where}" if where else "not found in any plugin dir or entry point"))
    instance = _probe_provider(where, ep, is_disabled=is_disabled) if (where or ep) else None
    say(instance is not None, "loads and registers" if instance is not None else "failed to load (see Hermes's log for the traceback)")
    if instance is not None:
        say(instance.is_available(), "is_available()" if instance.is_available() else f"unavailable: {instance.unavailable_reason()}")
        loaded_from = sys.modules[type(instance).__module__].__file__
        _out(f"      running code from {Path(loaded_from).parent}")
        clashes = _core_tool_clashes()
        if clashes is not None:
            say(not clashes, "no tool-name clashes with Hermes core" if not clashes else f"core tools shadow ours: {clashes}")
        _diagnose_skill(SKILLS_DIR, say)

    builtin_on = any(_builtin_memory_flags(cfg, cfg_get))
    raw_disabled = cfg_get(cfg, "agent", "disabled_toolsets")
    agent_disabled = _parse_toolset_list(raw_disabled)
    if agent_disabled is None and isinstance(raw_disabled, str) and raw_disabled.strip():
        agent_disabled = [raw_disabled.strip()]
    platforms = args.platform or sorted({"cli", "api_server", *((cfg.get("platform_toolsets") or {}).keys())})
    for platform in platforms:
        try:
            enabled = _platform_toolsets(cfg, platform)
            memory_tool = builtin_on and memory_provider_tools_enabled(enabled, agent_disabled)
            exposed = memory_provider_tools_enabled(enabled, agent_disabled, memory_tool_present=memory_tool)
        except Exception as err:  # noqa: BLE001 - diagnostic: report, don't crash
            say(False, f"{platform}: couldn't resolve its toolsets ({type(err).__name__}: {err})")
            continue
        say(exposed, f"{platform}: memory tools and prompt block "
            + ("exposed" if exposed else "WITHHELD, because the `memory` toolset isn't enabled for this platform "
               "(platform_toolsets / agent.disabled_toolsets)"))
    if not problems:
        _out("")
        _out("Everything checks out. If a chat still can't see the tools, it's probably an agent built before the change:")
        _out("start a new session (/new), or restart the gateway.")
    return 1 if problems else 0


def _is_plugin_dir_install() -> bool:
    return _find_plugin_dir() is not None


def _local_skills_dir() -> Path:
    try:
        from hermes_constants import get_skills_dir
        return Path(get_skills_dir())
    except ImportError:
        return _hermes_home() / "skills"


def _external_skills_dirs() -> List[Path]:
    try:
        from hermes_cli.config import cfg_get, load_config_readonly
        raw = cfg_get(load_config_readonly(), "skills", "external_dirs")
    except ImportError:
        return []
    entries = [raw] if isinstance(raw, str) else (raw if isinstance(raw, list) else [])
    home = _hermes_home()
    local = _local_skills_dir().resolve()
    out: List[Path] = []
    for entry in entries:
        s = str(entry).strip() if entry is not None else ""
        if not s:
            continue
        p = Path(os.path.expanduser(os.path.expandvars(s)))
        resolved = (p if p.is_absolute() else home / p).resolve()
        if resolved != local and resolved not in out and resolved.is_dir():
            out.append(resolved)
    return out


def _has_external_skill(skills_dir: Path = SKILLS_DIR) -> bool:
    target = skills_dir.resolve()
    return any(d.resolve() == target for d in _external_skills_dirs())


def _diagnose_skill(skills_dir: Path, say) -> None:
    """The sort-imports skill and its /sort-imports command (README: Importing built-in memory)."""
    skill_md = skills_dir / "sort-imports" / "SKILL.md"
    say(skill_md.is_file(), f"skill file {skill_md}" if skill_md.is_file()
        else f"no sort-imports/SKILL.md in {skills_dir}: this copy of the plugin is out of date")
    if not skill_md.is_file():
        return
    try:
        from tools.skills_tool import find_all_skills, locate_skill, skill_search_dirs
    except ImportError:
        _out("      (this Hermes has no tools.skills_tool: can't check /sort-imports)")
        return
    skills_dir = skills_dir.resolve()
    external = [d.resolve() for d in _external_skills_dirs()]
    if skills_dir in external:
        _out(f"ok    skills.external_dirs includes {skills_dir}")
    elif _is_plugin_dir_install():
        _out("note  /sort-imports is off: skills.external_dirs doesn't include this plugin's skills.")
        _out("      Asking the agent to sort your imported memory still works. To get /sort-imports, add to config.yaml:")
        _out(f"        skills:\n          external_dirs:\n            - {skills_dir}")
    else:
        _out("note  /sort-imports is off: skills.external_dirs doesn't include this plugin's skills.")
        _out("      To sort imported memory, paste the prompt from `hermes memory-buckets import-prompt`, or add to config.yaml:")
        _out(f"        skills:\n          external_dirs:\n            - {skills_dir}")
    roots, _ = skill_search_dirs()
    loadable = {s.get("name") for s in find_all_skills() if isinstance(s, dict)}
    _err, _sdir, found_md = (locate_skill("sort-imports", None, roots)
                             if "sort-imports" in loadable else (None, None, None))
    entry = {"skill_md_path": str(found_md)} if found_md is not None else None
    if entry is None:
        if skills_dir in external:
            say(False, "/sort-imports isn't registered, although the directory is configured (see Hermes's log)")
    else:
        ours = Path(entry["skill_md_path"]).resolve() == skill_md.resolve()
        say(ours, "/sort-imports → this plugin's skill" if ours
            else f"/sort-imports is claimed by another skill: {entry['skill_md_path']}")
        _out("      A running chat or gateway keeps its old skill list: run /reload-skills, or restart the gateway.")
    # Copies of an older sort prompt saved as local skills shadow the real one.
    local = _local_skills_dir()
    stale = []
    if local.is_dir():
        for md in local.rglob("SKILL.md"):
            try:
                text = md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "global/inbox.md" in text and md.resolve() != skill_md.resolve():
                stale.append(md)
    for md in stale:
        say(False, f"local skill {md} is a copy of a sort prompt; it goes stale, so delete it")


def lint(store: Store) -> List[str]:
    """Problems in the store, as human-readable lines."""
    problems: List[str] = []
    leftovers = set(migrate.leftover_indexes(store))
    names = defaultdict(list)  # (scope, name) -> paths
    stems = defaultdict(set)   # scope -> stems, for links
    texts = {}
    if not store.memories.is_dir():
        return problems
    for dirpath, dirnames, filenames in os.walk(store.memories):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        rel_dir = os.path.relpath(dirpath, store.memories).replace(os.sep, "/")  # memory paths always use '/'
        for d in list(dirnames):
            if is_link(os.path.join(dirpath, d)):
                problems.append(f"{posixpath.normpath(posixpath.join(rel_dir, d))}: symlinked directory (ignored)")
                dirnames.remove(d)  # os.walk on Python < 3.12 would descend into a junction
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            full = os.path.join(dirpath, name)
            if is_link(full):
                problems.append(f"{rel}: symlink (ignored)")
                continue
            try:
                validate_path(rel)
            except StoreError as err:
                if name == "index.md" and rel_dir in leftovers:
                    problems.append(f"{rel}: left over from an older version; run `hermes memory-buckets migrate`")
                else:
                    problems.append(f"{rel}: not a memory path, ignored ({err.message.split(': ', 1)[-1]})")
                continue
            data = Path(full).read_bytes()
            if len(data) > MAX_FILE_BYTES:
                problems.append(f"{rel}: {len(data)} bytes, over the {MAX_FILE_BYTES}-byte cap")
            text = data.decode("utf-8", errors="replace")
            body, err = _check_frontmatter(text, rel)
            if err is not None:
                problems.append(f"{rel}: bad frontmatter ({err}); tools won't edit it until it's fixed")
            scope = rel.split("/", 1)[0]
            names[(scope, stem(rel))].append(rel)
            stems[scope].add(stem(rel))
            texts[rel] = body
    for (scope, name), paths in sorted(names.items()):
        if len(paths) > 1:
            problems.append(f"{scope}/: name '{name}' is used by {', '.join(paths)}; [[{name}]] is ambiguous")
    for rel, body in sorted(texts.items()):
        scope = rel.split("/", 1)[0]
        for line in inbox._outside_fences(body):
            for target in LINK_RE.findall(re.sub(r"`[^`\n]+`", "", line)):
                target = target.strip().removesuffix(".md")
                if "/" in target:
                    ok = f"{target}.md" in texts
                else:
                    ok = target in stems[scope] or target in stems[GLOBAL]
                if not ok:
                    problems.append(f"{rel}: broken link [[{target}]]")
    for rel, reason in _scanner_blocking(store):
        problems.append(f"{rel}: {reason}")
    return problems


def cmd_lint(args) -> int:
    _, _, store, _ = _open(args)
    problems = lint(store)
    if migrate.inbox_needs_migrating(store):
        problems.append(f"{inbox.PATH}: outdated description; run `hermes memory-buckets migrate`")
    pending = _leftover_pending(store)
    if pending:
        problems.append(f"{store.root / migrate.PENDING_DIR}/: left over from 0.1.0 ({pending}); "
                        "run `hermes memory-buckets migrate`")
    for line in problems:
        _out(line)
    waiting = _inbox_entries(store)
    if waiting:
        _out(f"note: {inbox.PATH} has {_entries(waiting)} waiting to be sorted")
    _out(f"{len(problems)} problem(s)")
    return 1 if problems else 0


def _entries(n: int) -> str:
    return f"{n} entr{'y' if n == 1 else 'ies'}"


def _leftover_pending(store: Store) -> str:
    """"N proposals" for a leftover ``_pending/``, or "" when there's none."""
    names = migrate.leftover_pending(store)
    if names is None:
        return ""
    n = sum(1 for name in names if name.endswith(".json"))
    return f"{n} proposal{'' if n == 1 else 's'}"


def _inbox_entries(store: Store) -> int:
    """Entries in the inbox; 0 when it's missing or unreadable (lint reports that)."""
    try:
        data = store.read_bytes(inbox.PATH)
        return inbox.count_entries(frontmatter.parse(data.decode("utf-8", errors="replace"))[1]) if data else 0
    except (OSError, StoreError, frontmatter.FrontmatterError):
        return 0


def cmd_migrate(args) -> int:
    _, _, store, _ = _open(args)
    try:
        steps = migrate.run(store, dry_run=args.dry_run)
    except StoreError as err:
        _out(f"error: {err.message}")
        return 1
    if not steps:
        _out("nothing to migrate")
        return 0
    for step in steps:
        _out(("would: " if args.dry_run else "") + step.describe())
    backup = store.root / migrate.BACKUP_DIR
    if not args.dry_run and backup.exists() and any(s.action not in ("skip", "tidy", "remove", "tighten") for s in steps):
        _out(f"originals kept in {backup}")
    return 1 if any(s.action == "skip" for s in steps) else 0


def cmd_reindex(args) -> int:
    _, _, store, index = _open(args)
    try:
        index.rebuild()
    except IndexInUse as err:
        _out(str(err))
        return 1
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
    # fetch mkdirs <root>/.models with parents, which would create the root itself at
    # the umask's mode; every other creation path goes through the store's ensure_dir.
    store.ensure_dir(store.root)
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
            entries = [e.strip() for e in path.read_text(encoding="utf-8", errors="replace").split(ENTRY_DELIMITER)]
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
        _print_import_prompt(store)
    return 0


def _backup_commands(store: Store) -> tuple:
    """(back up, review) shell commands for sorting imports. PowerShell has no
    ``cp -a`` or ``diff -ru``; git is there, since Hermes's Windows installer brings it."""
    backup = f"{store.root}.before-sort"
    if sys.platform == "win32":
        return (f'Copy-Item -Recurse "{store.root}" "{backup}"',
                f'git diff --no-index "{backup}\\memories" "{store.memories}"')
    return f"cp -a {store.root} {backup}", f"diff -ru {backup}/memories {store.memories}"


def _print_import_prompt(store: Store) -> None:
    backup, review = _backup_commands(store)
    _out()
    _out("Next:")
    _out(f"  1. Back up the store first: {backup}")
    if _is_plugin_dir_install() or _has_external_skill(SKILLS_DIR):
        _out("  2. In a new chat, ask the agent to sort your imported memory")
        _out("     (it uses memory-buckets's sort-imports skill), or paste the prompt below.")
    else:
        _out("  2. In a new chat, paste the prompt below (from `hermes memory-buckets import-prompt`),")
        _out(f"     or add {SKILLS_DIR} to skills.external_dirs so the agent can use the sort-imports skill.")
    _out("  3. It files every fact itself, and removes each line from the inbox as it goes.")
    _out(f"  4. Review with: {review}")
    _out("     (Print this again with: hermes memory-buckets import-prompt)")
    _out()
    _out("----- prompt -----")
    _out(import_prompt())
    _out("------------------")


def cmd_import_prompt(args) -> int:
    _, _, store, _ = _open(args)
    if not _inbox_entries(store):
        _out(f"({inbox.PATH} in {store.root} has nothing to sort)")
        return 1
    _print_import_prompt(store)
    return 0


def cmd_history(args) -> int:
    """Versions of one memory file recorded in .history/, newest first."""
    _, _, store, _ = _open(args)
    try:
        entries = store.history(args.path)
    except StoreError as err:
        _out(f"error: {err.message}")
        return 1
    if not entries:
        _out(f"no history for {args.path}")
        return 1
    for e in entries:
        _out(f"{e['timestamp']}  {e['version']}  {e['bytes']} bytes")
    _out("restore one with: hermes memory-buckets restore PATH [VERSION]")
    return 0


def cmd_restore(args) -> int:
    _, _, store, _ = _open(args)
    try:
        got = store.restore(args.path, args.version)
    except StoreError as err:
        _out(f"error: {err.message}")
        return 1
    if got["unchanged"]:
        _out(f"{args.path} already holds version {got['version']} (recorded {got['timestamp']})")
    elif got["recreated"]:
        _out(f"recreated {args.path} from recorded version {got['version']} (recorded {got['timestamp']})")
    else:
        _out(f"restored {args.path} to version {got['version']} (recorded {got['timestamp']}); "
             "the content it replaced is itself recorded")
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
    p = sub.add_parser("migrate", help="Update a store written by an older version (also runs on startup)")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(bm_func=cmd_migrate)
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
    p = sub.add_parser("import-prompt", help="Print the prompt that asks the agent to sort the imported memory in global/inbox.md")
    p.set_defaults(bm_func=cmd_import_prompt)
    p = sub.add_parser("history", help="List the recorded versions of one memory file (.history/)")
    p.add_argument("path", help="Memory path, e.g. global/topics/nix.md")
    p.set_defaults(bm_func=cmd_history)
    p = sub.add_parser("restore", help="Write a recorded version of one memory file back (itself recorded)")
    p.add_argument("path", help="Memory path, e.g. global/topics/nix.md")
    p.add_argument("version", nargs="?", help="Version from `history`; default: the newest recorded version")
    p.set_defaults(bm_func=cmd_restore)


def _dispatch(args) -> int:
    func = getattr(args, "bm_func", None)
    if func is None:
        print("usage: pick a command: status, diagnose, lint, migrate, reindex, search, hints, fetch-model, import, import-prompt, history, restore (--help for more)", file=sys.stderr)
        return 2
    return func(args)


def register_cli(subparser) -> None:
    """``hermes memory-buckets ...``"""
    _add_commands(subparser)
    subparser.set_defaults(func=lambda args: sys.exit(_dispatch(args)))
