import tempfile
import unittest
from pathlib import Path

from . import PLUGIN_DIR, memory_buckets
from memory_buckets import _compat, frontmatter, snapshot, tools


BUNDLED_SKILLS = ["sort-imports", "tidy-memory"]


class _Ctx:
    def __init__(self, name=None, register_skill=None, register_command=None):
        self.name = name
        self.providers = []
        self.skills = []
        self.commands = {}
        if register_skill is not None:
            self.register_skill = register_skill
        if register_command is not None:
            self.register_command = register_command

    def register_memory_provider(self, provider):
        self.providers.append(provider)


class RegisterTest(unittest.TestCase):
    def test_register_hands_back_a_provider(self):
        ctx = _Ctx()
        memory_buckets.register(ctx)
        [provider] = ctx.providers
        self.assertIsInstance(provider, _compat.MemoryProvider)
        self.assertEqual(provider.name, "memory-buckets")
        self.assertTrue(provider.is_available())
        self.assertEqual([s["name"] for s in provider.get_tool_schemas()], tools.TOOL_NAMES)

    def test_each_register_call_is_a_fresh_instance(self):
        # Hermes calls register() once per AIAgent.
        ctx = _Ctx()
        memory_buckets.register(ctx)
        memory_buckets.register(ctx)
        self.assertIsNot(ctx.providers[0], ctx.providers[1])

    def test_initialize_puts_the_store_under_hermes_home(self):
        ctx = _Ctx()
        memory_buckets.register(ctx)
        provider = ctx.providers[0]
        with tempfile.TemporaryDirectory() as home:
            provider.initialize("s1", hermes_home=home, platform="telegram", agent_context="primary")
            self.assertEqual(provider.store.root, Path(home) / "memory-buckets")
            self.assertEqual(provider._state("s1").platform, "telegram")
            self.assertTrue((provider.store.memories / "global").is_dir(), "store exists as soon as it's active")
            provider.shutdown()

    def test_registers_the_skills_and_no_slash_commands(self):
        for skill in BUNDLED_SKILLS:
            with self.subTest(skill=skill):
                self._check_skill_registers(skill)

    def _check_skill_registers(self, skill):
        skill_md = PLUGIN_DIR / f"skills/{skill}/SKILL.md"
        fm, _ = frontmatter.parse(skill_md.read_text(encoding="utf-8"))
        as_dict = getattr(fm, "as_dict", None)
        expected_fm = as_dict() if callable(as_dict) else dict(fm.pairs())
        expected_desc = str(fm.get("description") or "")
        for namespace in (None, "memory-buckets"):
            with self.subTest(namespace=namespace):
                ctx = _Ctx()
                if namespace is not None:
                    ctx.name = namespace
                ctx.skills, ctx.commands = [], {}
                ctx.register_skill = (
                    lambda name, path, description="", frontmatter=None, skills=ctx.skills:
                    skills.append((name, path, description, frontmatter))
                )
                ctx.register_command = (
                    lambda name, handler, description="", args_hint="", commands=ctx.commands:
                    commands.__setitem__(name, handler)
                )
                memory_buckets.register(ctx)
                self.assertEqual([s[0] for s in ctx.skills], BUNDLED_SKILLS)
                [(name, path, description, fm_dict)] = [s for s in ctx.skills if s[0] == skill]
                self.assertEqual(name, skill)
                self.assertEqual(path, skill_md)
                self.assertTrue(path.is_file())
                self.assertEqual(description, expected_desc)
                self.assertTrue(description)
                self.assertEqual(fm_dict, expected_fm)
                self.assertEqual(fm_dict["name"], skill)
                self.assertEqual(fm_dict["description"], expected_desc)
                self.assertEqual(snapshot.SORT_SKILL, "memory-buckets:sort-imports")
                self.assertEqual(ctx.commands, {}, "the skill is the only slash command; the plugin registers none")

    def test_skill_directories_are_all_registered(self):
        on_disk = sorted(d.name for d in (PLUGIN_DIR / "skills").iterdir() if (d / "SKILL.md").is_file())
        self.assertEqual(on_disk, BUNDLED_SKILLS, "a new skill needs adding to BUNDLED_SKILLS here")

    def test_tidy_memory_asks_before_changing_anything(self):
        text = (PLUGIN_DIR / "skills/tidy-memory/SKILL.md").read_text(encoding="utf-8")
        _, body = frontmatter.parse(text)
        first_paragraph = body.split("\n\n")[1]  # after the "# Tidy the memory" heading
        self.assertIn("asks before it changes anything", first_paragraph)

    def test_tidy_memory_wording_follows_the_style_rules(self):
        import re
        text = (PLUGIN_DIR / "skills/tidy-memory/SKILL.md").read_text(encoding="utf-8")
        self.assertNotIn("\u2014", text, "no em dashes")
        self.assertNotIn("\u2013", text, "no en dashes")
        for word in ("robust", "seamless", "leverage", "crucial", "utilize", "organize", "behavior", "color"):
            self.assertNotRegex(text.lower(), rf"\b{word}", word)
        mentioned = set(re.findall(r"\bmemory_[a-z_]+", text))
        self.assertLessEqual(mentioned, set(tools.TOOL_NAMES), "every memory_* name is a real tool")
        self.assertTrue({"memory_write", "memory_str_replace", "memory_append", "memory_delete"} <= mentioned)
        self.assertIn("until the user has answered your proposal", text)
        self.assertIn("global/inbox.md", text)

    def test_register_skill_falls_back_to_two_arg_stub(self):
        ctx = _Ctx()
        ctx.skills = []
        ctx.register_skill = lambda name, path: ctx.skills.append((name, path))
        memory_buckets.register(ctx)
        self.assertEqual([name for name, _ in ctx.skills], BUNDLED_SKILLS)
        for _, path in ctx.skills:
            self.assertTrue(path.is_file())
        self.assertEqual(snapshot.SORT_SKILL, "memory-buckets:sort-imports")

    def test_entry_point_namespace_skips_register_skill(self):
        self.addCleanup(setattr, snapshot, "SORT_SKILL", "memory-buckets:sort-imports")
        ctx = _Ctx()
        ctx.name = "memory_buckets"
        ctx.skills = []
        ctx.register_skill = lambda *args, **kwargs: ctx.skills.append((args, kwargs))
        memory_buckets.register(ctx)
        self.assertEqual(ctx.skills, [], "entry-point installs must not register a skill Hermes will prune")
        self.assertIsNone(snapshot.SORT_SKILL)

    def test_hermes_discovery_heuristic_matches(self):
        # plugins/memory/__init__.py::_is_memory_provider_dir greps __init__.py.
        text = (PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8")
        self.assertTrue("MemoryProvider" in text or "register_memory_provider" in text)

    def test_manifest_declares_no_kind(self):
        manifest = (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8")
        self.assertIn("name: memory-buckets", manifest)
        self.assertRegex(manifest, r"(?m)^manifest_version: 2$")
        self.assertRegex(manifest, r"(?m)^api_version: 1$")
        self.assertNotRegex(manifest, r"(?m)^kind:")
        self.assertRegex(manifest, r"(?m)^python_runtime: external$")

    @unittest.skipUnless((PLUGIN_DIR / "pyproject.toml").exists(), "installed package: no pyproject.toml")
    def test_pip_and_plugin_metadata_agree(self):
        import re
        pyproject = (PLUGIN_DIR / "pyproject.toml").read_text(encoding="utf-8")
        manifest = (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8")
        version = lambda text: re.search(r'(?m)^version\s*[:=]\s*"?([^"\s]+)', text).group(1)  # noqa: E731
        self.assertEqual(version(pyproject), version(manifest))
        self.assertIn('memory-buckets = "memory_buckets"', pyproject, "entry point name must be the provider name")
        self.assertIn("dependencies = []", pyproject, "stdlib only")
        self.assertIn('license = "MIT"', pyproject)
        self.assertTrue((PLUGIN_DIR / "LICENSE").is_file(), "the plugin dir ships its own LICENSE")

    def test_plugin_dir_shims_reach_the_package(self):
        # Hermes loads the repo root as a package and imports its cli.py for `hermes memory-buckets`.
        import importlib.util
        import sys
        name = "_probe_memory_buckets_plugin_dir"
        spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / "__init__.py",
                                                      submodule_search_locations=[str(PLUGIN_DIR)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        self.addCleanup(lambda: [sys.modules.pop(k) for k in list(sys.modules) if k.startswith(name)])
        spec.loader.exec_module(module)
        self.assertTrue(callable(module.register))
        cli = importlib.import_module(f"{name}.cli")
        self.assertTrue(callable(cli.register_cli))

    def test_tool_names_are_unique_and_not_hermes_core(self):
        # Mirrors toolsets._HERMES_CORE_TOOLS at rev 59004a62; hermes_probe.py checks the real list.
        core = {"memory", "session_search", "read_file", "write_file", "search_files", "todo_list"}
        self.assertEqual(len(set(tools.TOOL_NAMES)), 7)
        self.assertEqual(sorted(tools.TOOL_NAMES), sorted([
            "memory_list", "memory_read", "memory_search", "memory_write", "memory_str_replace",
            "memory_append", "memory_delete"]))
        self.assertFalse(core & set(tools.TOOL_NAMES))


if __name__ == "__main__":
    unittest.main()
