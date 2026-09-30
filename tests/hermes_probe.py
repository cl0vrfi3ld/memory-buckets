"""Run by test_hermes.py under Hermes's own Python, with a throwaway HERMES_HOME.

Drives real Hermes code paths against the plugin: provider discovery and
loading, MemoryManager registration and tool routing, the system prompt block,
session switching, prefetch on Hermes's timeout thread, CLI discovery through
the synthetic package, and our overrides' signatures against the real ABC.
Prints one JSON object; the test asserts on it.
"""

import argparse
import inspect
import json
import os
import sys
from pathlib import Path

home = Path(os.environ["HERMES_HOME"])
out = {}

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

# ADR-0016: the sort-inbox skill and the user-only slash commands.
from hermes_cli.plugins import get_plugin_command_handler  # noqa: E402
from tools.skills_tool import skill_view  # noqa: E402


skill = sys.modules[type(p1).__module__.rsplit(".", 1)[0] + ".snapshot"].SORT_SKILL
out["skill_name"] = skill
# The canonical name, whatever the prompt says: tracks whether Hermes still can't serve pip installs' skills.
viewed = json.loads(skill_view("memory-buckets:sort-inbox"))
out["skill_ok"] = bool(viewed.get("success")) and "memory_propose" in json.dumps(viewed)
from agent.skill_commands import build_skill_invocation_message, scan_skill_commands  # noqa: E402

out["slash_skill"] = ("/sort-inbox" in scan_skill_commands()
                      and "memory_propose" in (build_skill_invocation_message("/sort-inbox", "") or ""))
out["slash"] = sorted(n for n in ("memory-pending", "memory-apply", "memory-reject") if get_plugin_command_handler(n))
(home / "memory-buckets/memories/proj-1").mkdir(parents=True, exist_ok=True)
manager.handle_tool_call("memory_write", {"path": "global/inbox.md", "description": "inbox", "if_version": "new",
                                          "body": "- proj-1 deploys with colmena\n"})
proposed = json.loads(manager.handle_tool_call("memory_propose", {
    "project": "proj-1", "summary": "deploy tool", "inbox_lines": ["proj-1 deploys with colmena"],
    "files": [{"path": "proj-1/topics/deploy.md", "description": "How proj-1 deploys", "lines": ["colmena"]}]}))
out["propose_ok"] = proposed.get("ok")
out["staged_not_written"] = not (home / "memory-buckets/memories/proj-1/topics/deploy.md").exists()
if proposed.get("ok"):
    out["apply_reply"] = get_plugin_command_handler("memory-apply")(proposed["id"])
out["applied"] = (home / "memory-buckets/memories/proj-1/topics/deploy.md").is_file()

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

manager.shutdown_all()
print("PROBE-RESULT " + json.dumps(out))
