"""Run by test_hermes.py under Hermes's own Python, with a throwaway HERMES_HOME.

Drives real Hermes code paths against the plugin: provider discovery and
loading, MemoryManager registration and tool routing, the system prompt block,
session switching, prefetch on Hermes's timeout thread, CLI discovery through
the synthetic package, and our overrides' signatures against the real ABC. It also
exercises the private Hermes code the plugin falls back on silently (workspace override, cwd
pinning, project lookup, TUI status line) and lists whatever has moved.
Prints one JSON object; the test asserts on it.
"""

import argparse
import importlib
import inspect
import json
import os
import sys
from pathlib import Path

home = Path(os.environ["HERMES_HOME"])
out = {}
# tui_gateway.server points sys.stdout at stderr on import (stdout is its JSON-RPC channel),
# which the internals checks below import; keep the real stream for the result line.
result_stream = sys.stdout

from agent.memory_manager import MemoryManager  # noqa: E402
from agent.memory_provider import MemoryProvider  # noqa: E402
from plugins.memory import discover_memory_providers, discover_plugin_cli_commands, load_memory_provider  # noqa: E402
from toolsets import _HERMES_CORE_TOOLS  # noqa: E402

out["discovered"] = [d for d in discover_memory_providers() if d[0] == "memory-buckets"]
p1, p2 = load_memory_provider("memory-buckets"), load_memory_provider("memory-buckets")
out["is_provider"] = isinstance(p1, MemoryProvider)
out["provider_file"] = sys.modules[type(p1).__module__].__file__

# Hermes's package manager must not treat the plugin as a dependency-managed
# workspace member (enabling one runs `uv lock` against Hermes's uv.lock).
from pm.plugin_declarations import read_python_declaration  # noqa: E402
plugin_root = next(d for d in Path(out["provider_file"]).parents if (d / "plugin.yaml").exists())
out["pm_member"] = read_python_declaration(plugin_root).is_member
out["distinct_instances"] = p1 is not p2

# Our overrides must accept everything the base signature can pass.
bad = []
for name, ours in inspect.getmembers(type(p1), inspect.isfunction):
    base = getattr(MemoryProvider, name, None)
    if base is None or name.startswith("_") or base is ours:
        continue
    ours_params = inspect.signature(ours).parameters
    has_var_kw = any(p.kind is p.VAR_KEYWORD for p in ours_params.values())
    for pname, param in inspect.signature(base).parameters.items():
        if param.kind in (param.VAR_KEYWORD, param.VAR_POSITIONAL):
            continue
        if pname not in ours_params and not has_var_kw:
            bad.append(f"{name}: missing {pname}")
out["signature_problems"] = bad

manager = MemoryManager()
manager.add_provider(p1)
manager.initialize_all("s1", platform="cli")
out["store_created_on_init"] = (home / "memory-buckets/memories/global").is_dir()
out["tools"] = sorted(s["name"] for s in manager.get_all_tool_schemas())
out["core_clash"] = sorted(set(out["tools"]) & set(_HERMES_CORE_TOOLS))
out["store_root"] = str(p1.store.root)

written = json.loads(manager.handle_tool_call("memory_write", {
    "path": "global/profile.md", "description": "Who the user is", "body": "- likes tea", "if_version": "new"}))
out["write_ok"] = written.get("ok")
out["file_on_disk"] = (home / "memory-buckets/memories/global/profile.md").is_file()
out["prompt_has_block"] = "- likes tea" in manager.build_system_prompt()
out["prefetch"] = manager.prefetch_all("what do you know about tea", session_id="s1")
manager.on_session_switch("s2", parent_session_id="s1", reset=True)
out["current_after_switch"] = p1._current

cmds = discover_plugin_cli_commands()
out["cli_names"] = [c["name"] for c in cmds]
parser = argparse.ArgumentParser()
sub = parser.add_subparsers()
plugin_parser = sub.add_parser("memory-buckets")
cmds[0]["setup_fn"](plugin_parser)
args = parser.parse_args(["memory-buckets", "status"])
try:
    args.func(args)
    out["cli_exit"] = None
except SystemExit as exc:
    out["cli_exit"] = exc.code

args = parser.parse_args(["memory-buckets", "diagnose", "--platform", "cli"])
try:
    args.func(args)
    out["diagnose_exit"] = None
except SystemExit as exc:
    out["diagnose_exit"] = exc.code

# The sort-imports skill (the plugin registers no slash commands of its own).
from hermes_cli.plugins import get_plugin_command_handler  # noqa: E402
from tools.skills_tool import skill_view  # noqa: E402


skill = sys.modules[type(p1).__module__.rsplit(".", 1)[0] + ".snapshot"].SORT_SKILL
out["skill_name"] = skill
# The canonical name, whatever the prompt says: tracks whether Hermes still can't serve pip installs' skills.
viewed = json.loads(skill_view("memory-buckets:sort-imports"))
out["skill_ok"] = bool(viewed.get("success")) and "global/inbox.md" in json.dumps(viewed)
from agent.skill_commands import build_skill_invocation_message, scan_skill_commands  # noqa: E402

out["slash_skill"] = ("/sort-imports" in scan_skill_commands()
                      and "global/inbox.md" in (build_skill_invocation_message("/sort-imports", "") or ""))
out["slash"] = sorted(n for n in ("memory-pending", "memory-apply", "memory-reject") if get_plugin_command_handler(n))

# Scoping follows Hermes's own Projects: the project owning the session's cwd names the bucket.
from hermes_cli import projects_db as pdb  # noqa: E402

project_dir = home.parent / "work" / "proj-1"
(project_dir / "src").mkdir(parents=True)
with pdb.connect_closing() as conn:
    pdb.create_project(conn, name="Proj 1", slug="proj-1", folders=[str(project_dir)], primary_path=str(project_dir))
scoped = load_memory_provider("memory-buckets")
scoped.initialize("s9", hermes_home=str(home), platform="cli", cwd=str(project_dir / "src"))
out["project_scope"] = scoped._scope().describe()
elsewhere = load_memory_provider("memory-buckets")
elsewhere.initialize("s10", hermes_home=str(home), platform="cli", cwd=str(home.parent))
out["elsewhere_scope"] = elsewhere._scope().project
scoped.shutdown()
elsewhere.shutdown()

# The private Hermes code the plugin reaches into. Every one of these has a silent fallback in
# the plugin (an ignored workspace override, no status line, an unscoped session), so a Hermes
# refactor would show up as behaviour drift. Each check records a problem rather than raising,
# so one run lists everything that moved; test_hermes.py asserts the list is empty.
pkg = type(p1).__module__.rsplit(".", 1)[0]
plugin_projects = sys.modules[pkg + ".projects"]
plugin_compat = sys.modules[pkg + "._compat"]
internals = []


def need(module, name):
    try:
        return getattr(importlib.import_module(module), name)
    except Exception as exc:  # a rename is an AttributeError, a moved module an ImportError
        internals.append(f"{module}.{name}: {type(exc).__name__}: {exc}")
        return None


def accepts(label, fn, *args):
    """Problem unless ``fn`` can be called with ``args``, the way the plugin calls it."""
    if fn is None:
        return
    try:
        inspect.signature(fn).bind(*args)
    except (TypeError, ValueError) as exc:
        internals.append(f"{label}: {exc}")


def expect(label, got, want):
    if got != want:
        internals.append(f"{label}: expected {want!r}, got {got!r}")


# Desktop workspace override and gateway per-turn cwd: signatures first, then behaviour through
# the plugin's own lookup, so a changed return shape fails too, not only a changed signature.
sessions = need("tui_gateway.server", "_sessions")
resolve_task_overrides = need("tools.terminal_tool", "resolve_task_overrides")
# Hermes rebinds tui_gateway's helper modules onto server.py's namespace; only the server's copy runs.
register_session_cwd = need("tui_gateway.server", "_register_session_cwd")
get_session_env = need("gateway.session_context", "get_session_env")
set_session_vars = need("gateway.session_context", "set_session_vars")
clear_session_vars = need("gateway.session_context", "clear_session_vars")
scoped_session_cwd = need("agent.runtime_cwd", "scoped_session_cwd")
resolve_agent_cwd = need("agent.runtime_cwd", "resolve_agent_cwd")
set_session_cwd = need("agent.runtime_cwd", "set_session_cwd")
reset_session_cwd = need("agent.runtime_cwd", "reset_session_cwd")
accepts("resolve_task_overrides(task_id)", resolve_task_overrides, "task")
accepts("get_session_env(name, default)", get_session_env, "HERMES_SESSION_KEY", "")
accepts("scoped_session_cwd()", scoped_session_cwd)
accepts("resolve_agent_cwd()", resolve_agent_cwd)

workspaces = home.parent / "workspaces"
override_dir, keyed_dir, pinned_dir = (str(workspaces / n) for n in ("override", "keyed", "pinned"))
if resolve_agent_cwd is not None:
    expect("session_cwd falls back to resolve_agent_cwd", plugin_projects.session_cwd("", session_id="nobody"),
           str(resolve_agent_cwd()))
if register_session_cwd is not None:
    # The TUI's own writer of the override table, so the key and value names the plugin reads
    # (``cwd``, ``cwd_source``) are the ones Hermes writes, not ones this probe made up.
    register_session_cwd({"session_key": "probe-session", "cwd": override_dir})
    expect("workspace override", plugin_projects.session_cwd("/init", session_id="probe-session"), override_dir)
    # No workspace of its own: Hermes records the process-wide fallback, tagged "process".
    register_session_cwd({"session_key": "probe-process"})
    expect("process-sourced cwd is not a workspace", plugin_projects.session_cwd("/init", session_id="probe-process"),
           "/init")
    if set_session_vars is not None and clear_session_vars is not None:
        # The gateway keys overrides by session key, which only get_session_env knows.
        register_session_cwd({"session_key": "probe-key", "cwd": keyed_dir})
        tokens = set_session_vars(session_key="probe-key")
        try:
            expect("workspace override by gateway session key",
                   plugin_projects.session_cwd("/init", session_id="other"), keyed_dir)
        finally:
            clear_session_vars(tokens)
if set_session_cwd is not None and reset_session_cwd is not None:
    token = set_session_cwd(pinned_dir)
    try:
        expect("per-turn pinned cwd", plugin_projects.session_cwd("/init", session_id="nobody"), pinned_dir)
    finally:
        reset_session_cwd(token)

# Project lookup: _to_project reads these attributes with getattr defaults, so a rename would
# quietly drop the folder list or the description instead of failing.
pdb_list_projects = need("hermes_cli.projects_db", "list_projects")
found = []
if pdb_list_projects is not None:
    with pdb.connect_closing() as conn:
        found = pdb_list_projects(conn)
if not found:
    internals.append("projects_db.list_projects: the project created above is missing")
else:
    for attr in ("slug", "name", "primary_path", "description", "folders"):
        if not hasattr(found[0], attr):
            internals.append(f"projects_db.Project.{attr}: missing")
    if not all(hasattr(f, "path") for f in getattr(found[0], "folders", None) or []):
        internals.append("projects_db.Project.folders[].path: missing")
listed = [p for p in plugin_projects.list_projects() or [] if p.slug == "proj-1"]
expect("list_projects folders", [str(Path(f).resolve()) for f in listed[0].folders] if listed else None,
       [str(project_dir.resolve())])

# TUI status line: tui_gateway.server._sessions, agent._memory_manager.providers, status_callback.
if sessions is not None and not isinstance(sessions, dict):
    internals.append(f"tui_gateway.server._sessions: expected a dict, got {type(sessions).__name__}")
AIAgent = need("run_agent", "AIAgent")
if AIAgent is not None and "status_callback" not in inspect.signature(AIAgent.__init__).parameters:
    internals.append("AIAgent.__init__: no status_callback parameter")
statuses = []
if AIAgent is not None and isinstance(sessions, dict):
    # A real agent, built the way the TUI builds one (memory provider from config, status_callback
    # given), filed under _sessions the way the TUI files it. Nothing is called on the network.
    try:
        agent = AIAgent(model="probe", api_key="probe", base_url="http://127.0.0.1:9", quiet_mode=True,
                        skip_context_files=True, platform="tui", save_trajectories=False,
                        status_callback=lambda kind, text=None: statuses.append((kind, text)))
    except Exception as exc:
        agent = None
        internals.append(f"AIAgent(...): {type(exc).__name__}: {exc}")
    if agent is not None:
        try:
            if not hasattr(agent, "suppress_status_output"):
                internals.append("AIAgent.suppress_status_output: missing")
            mine = [p for p in getattr(getattr(agent, "_memory_manager", None), "providers", None) or []
                    if getattr(p, "name", "") == "memory-buckets"]
            if not mine:
                internals.append("AIAgent._memory_manager.providers: no memory-buckets provider")
            else:
                sessions["probe-tui"] = {"agent": agent}
                try:
                    show = plugin_compat.tui_status_callback(mine[0])
                    if show is None:
                        internals.append("tui_status_callback: found no status line for a TUI agent")
                    else:
                        show("probe notice")
                        expect("TUI status line", statuses, [("lifecycle", "probe notice")])
                finally:
                    del sessions["probe-tui"]
        finally:
            if getattr(agent, "_memory_manager", None):
                agent._memory_manager.shutdown_all()
out["internals_problems"] = internals

manager.shutdown_all()
print("PROBE-RESULT " + json.dumps(out), file=result_stream)
