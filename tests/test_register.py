import tempfile
import unittest

from . import PLUGIN_DIR, memory_buckets
from memory_buckets import _compat, tools


class _Ctx:
    def __init__(self):
        self.providers = []

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
            self.assertEqual(str(provider.store.root), f"{home}/memory-buckets")
            self.assertEqual(provider._state("s1").platform, "telegram")
            self.assertTrue((provider.store.memories / "global").is_dir(), "store exists as soon as it's active")
            provider.shutdown()

    def test_registers_the_skill_and_slash_commands(self):
        ctx = _Ctx()
        ctx.skills, ctx.commands = [], {}
        ctx.register_skill = lambda name, path: ctx.skills.append((name, path))
        ctx.register_command = lambda name, handler, description="", args_hint="": ctx.commands.__setitem__(name, handler)
        memory_buckets.register(ctx)
        [(name, path)] = ctx.skills
        self.assertEqual(name, "sort-inbox")
        self.assertTrue(path.is_file())
        self.assertEqual(sorted(ctx.commands), ["memory-apply", "memory-pending", "memory-reject"])

    def test_hermes_discovery_heuristic_matches(self):
        # plugins/memory/__init__.py::_is_memory_provider_dir greps __init__.py.
        text = (PLUGIN_DIR / "__init__.py").read_text()
        self.assertTrue("MemoryProvider" in text or "register_memory_provider" in text)

    def test_manifest_declares_no_kind(self):
        manifest = (PLUGIN_DIR / "plugin.yaml").read_text()
        self.assertIn("name: memory-buckets", manifest)
        self.assertNotRegex(manifest, r"(?m)^kind:")
        self.assertRegex(manifest, r"(?m)^python_runtime: external$")

    @unittest.skipUnless((PLUGIN_DIR / "pyproject.toml").exists(), "installed package: no pyproject.toml")
    def test_pip_and_plugin_metadata_agree(self):
        import re
        pyproject = (PLUGIN_DIR / "pyproject.toml").read_text()
        manifest = (PLUGIN_DIR / "plugin.yaml").read_text()
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
        self.assertEqual(len(set(tools.TOOL_NAMES)), 8)
        self.assertFalse(core & set(tools.TOOL_NAMES))


if __name__ == "__main__":
    unittest.main()
