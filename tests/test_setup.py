"""The setup contract (issue #28): get_config_schema and save_config.

The schema is what `hermes memory setup` and the web dashboard walk (Hermes's
hermes_cli/memory_setup.py and web_server_memory.py); save_config is what the
wizard calls afterwards. The tests check both against config.py, the module
that reads the values back. The end-to-end round trip through Hermes's real
config writer lives in test_hermes.py via hermes_probe.py; here Hermes's
config module is a stand-in, because outside Hermes there is no config.yaml.
"""

import copy
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

from memory_buckets import config as config_mod
from memory_buckets._compat import MemoryProvider, HERMES_AVAILABLE
from memory_buckets.provider import CONFIG_SCHEMA, MemoryBucketsProvider

EXPECTED_KEYS = [
    "readonly", "cron_writes", "nudge_interval", "snapshot_max_chars",
    "prefetch_min_similarity", "prefetch_max_hints", "search_limit", "project_boost",
    "store_path", "embeddings.backend", "embeddings.auto_download", "embeddings.local_model_dir",
]


def _config_fields() -> Dict[str, str]:
    """Setting key -> dataclass type name from config.py, flattened the way the
    schema spells the nested embeddings ones."""
    import dataclasses

    flat = {f.name: f.type for f in dataclasses.fields(config_mod.Config)}
    flat.pop("embeddings")
    for f in dataclasses.fields(config_mod.EmbeddingsConfig):
        flat[f"embeddings.{f.name}"] = f.type
    return flat


def _type_name(type_annotation: Any) -> str:
    kind = type_annotation if isinstance(type_annotation, str) else getattr(type_annotation, "__name__", "str")
    return {"bool": "boolean", "int": "integer", "float": "number", "str": "text"}[kind]


def _fake_hermes_config(initial: Dict[str, Any], home: Path):
    """hermes_cli.config stand-in: load_config hands out a deepcopy of
    ``initial`` (Hermes's own load_config returns a deepcopy too), save_config
    records the dict and writes it to disk, the way Hermes's writer rewrites
    the whole file. The write lands under ``home`` on the path the test
    controls, so TMPDIR decides whether it is disk-backed."""
    fake_cfg = types.ModuleType("hermes_cli.config")
    state = {"current": copy.deepcopy(initial)}  # save_config feeds the next load, like Hermes
    fake_cfg.load_config = lambda: copy.deepcopy(state["current"])
    fake_cfg.save_config = lambda cfg: (
        state.__setitem__("current", copy.deepcopy(cfg)),
        (home / "config.yaml").write_text(json.dumps(cfg), encoding="utf-8"))
    return fake_cfg


def _fake_hermes_constants(overrides: List[str]):
    """hermes_constants stand-in recording the hermes-home override, so a test
    can see that save_config scoped its write to the home it was given."""
    fake = types.ModuleType("hermes_constants")

    def set_override(path):
        overrides.append(str(path))
        return ("token",)

    fake.set_hermes_home_override = set_override
    fake.reset_hermes_home_override = lambda token: overrides.append("reset")
    return fake


class SetupSchemaTest(unittest.TestCase):
    def test_every_field_has_key_description_default_and_type(self):
        self.assertEqual([f["key"] for f in CONFIG_SCHEMA], EXPECTED_KEYS)
        for field in CONFIG_SCHEMA:
            self.assertIn(field["type"], ("text", "integer", "number", "boolean"))
            self.assertTrue(field["description"], field["key"])
            self.assertIn("default", field)

    def test_defaults_and_types_match_config_py(self):
        fields = _config_fields()
        for field in CONFIG_SCHEMA:
            self.assertIn(field["key"], fields, field["key"])
            self.assertEqual(field["type"], _type_name(fields[field["key"]]), field["key"])
        defaults = {f["key"]: f["default"] for f in CONFIG_SCHEMA}
        # config.py's Config dataclass is the authority; compare the typed ones.
        self.assertEqual(defaults["readonly"], False)
        self.assertEqual(defaults["cron_writes"], False)
        self.assertEqual(defaults["nudge_interval"], 10)
        self.assertEqual(defaults["snapshot_max_chars"], 12000)
        self.assertEqual(defaults["prefetch_min_similarity"], -1.0)
        self.assertEqual(defaults["prefetch_max_hints"], 4)
        self.assertEqual(defaults["search_limit"], 8)
        self.assertEqual(defaults["project_boost"], 1.3)
        self.assertEqual(defaults["store_path"], "")
        self.assertEqual(defaults["embeddings.backend"], "local")
        self.assertEqual(defaults["embeddings.auto_download"], True)
        self.assertEqual(defaults["embeddings.local_model_dir"], "")

    def test_minimums_where_the_code_enforces_them(self):
        minimums = {f["key"]: f.get("minimum") for f in CONFIG_SCHEMA}
        self.assertEqual(minimums["nudge_interval"], 0)  # 0 turns the nudge off
        self.assertEqual(minimums["snapshot_max_chars"], 1000)  # snapshot.py floors the cap
        self.assertEqual(minimums["prefetch_max_hints"], 1)
        self.assertEqual(minimums["search_limit"], 1)  # tools.py requires limit >= 1
        for key in ("readonly", "cron_writes", "prefetch_min_similarity", "project_boost",
                    "store_path", "embeddings.backend", "embeddings.auto_download",
                    "embeddings.local_model_dir"):
            self.assertIsNone(minimums[key], key)

    def test_no_secret_fields(self):
        # Every setting belongs in config.yaml; nothing routes to .env.
        for field in CONFIG_SCHEMA:
            self.assertFalse(field.get("secret"), field["key"])
            self.assertFalse(field.get("env_var"), field["key"])
            self.assertFalse(field.get("required"), field["key"])

    def test_embeddings_backend_offers_the_two_backends(self):
        field = next(f for f in CONFIG_SCHEMA if f["key"] == "embeddings.backend")
        self.assertEqual(field.get("choices"), ["local", "off"])

    def test_returned_schema_is_a_copy(self):
        p = MemoryBucketsProvider()
        first = p.get_config_schema()
        first.append({"key": "sabotage"})
        first[0]["default"] = "sabotage"
        self.assertEqual([f["key"] for f in p.get_config_schema()], EXPECTED_KEYS)


class FakeHermesCase(unittest.TestCase):
    """save_config against a stand-in hermes_cli.config, on a disk-backed path."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="mb-setup-")
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.initial = {
            "memory": {"provider": "memory-buckets", "memory_enabled": False},
            "display": {"theme": "dracula"},
            "plugins": {"other-plugin": {"answer": 42}},
        }

    def patch_hermes(self, overrides=None):
        modules = {"hermes_cli": types.ModuleType("hermes_cli"),
                   "hermes_cli.config": _fake_hermes_config(self.initial, self.home),
                   "hermes_constants": _fake_hermes_constants(overrides if overrides is not None else [])}
        for name, module in modules.items():
            if name.startswith("hermes_cli"):
                module.__path__ = []  # a package, so `from hermes_cli.config import ...` resolves
        return mock.patch.dict(sys.modules, modules)

    def provider(self):
        return MemoryBucketsProvider()

    def saved(self) -> Dict[str, Any]:
        return json.loads((self.home / "config.yaml").read_text(encoding="utf-8"))


class SaveConfigTest(FakeHermesCase):
    def test_writes_one_key_and_leaves_the_rest_intact(self):
        p = self.provider()
        with self.patch_hermes():
            p.save_config({"search_limit": 5}, str(self.home))
        saved = self.saved()
        block = saved["plugins"]["memory-buckets"]
        self.assertEqual(block, {"search_limit": 5})
        # Everything unrelated survives: the memory block, another plugin's block, display.
        self.assertEqual(saved["memory"], self.initial["memory"])
        self.assertEqual(saved["plugins"]["other-plugin"], {"answer": 42})
        self.assertEqual(saved["display"], {"theme": "dracula"})

    def test_nests_dotted_keys_and_coerces_wizard_strings(self):
        # The setup wizard hands every answer over as a string.
        p = self.provider()
        values = {"embeddings.backend": "off", "readonly": "true", "cron_writes": "no",
                  "snapshot_max_chars": "8000", "project_boost": "1.5",
                  "nudge_interval": "banana"}  # won't coerce: kept as-is, config.py ignores it
        with self.patch_hermes():
            p.save_config(values, str(self.home))
        block = self.saved()["plugins"]["memory-buckets"]
        self.assertEqual(block["embeddings"], {"backend": "off"})
        self.assertIs(block["readonly"], True)
        self.assertIs(block["cron_writes"], False)
        self.assertEqual(block["snapshot_max_chars"], 8000)
        self.assertEqual(block["project_boost"], 1.5)
        self.assertEqual(block["nudge_interval"], "banana")

    def test_second_write_updates_instead_of_replacing_the_block(self):
        p = self.provider()
        with self.patch_hermes():
            p.save_config({"search_limit": 5}, str(self.home))
            p.save_config({"readonly": True}, str(self.home))
        block = self.saved()["plugins"]["memory-buckets"]
        self.assertEqual(block, {"search_limit": 5, "readonly": True})

    def test_scopes_the_write_to_the_hermes_home_it_was_given(self):
        p = self.provider()
        overrides = []
        with self.patch_hermes(overrides):
            p.save_config({"search_limit": 5}, str(self.home))
        self.assertEqual(overrides, [str(self.home), "reset"])

    def test_a_stray_scalar_plugins_block_is_replaced_not_crashed(self):
        p = self.provider()
        with mock.patch.object(self, "initial", {"plugins": "oops", "memory": {}}):
            with self.patch_hermes():
                p.save_config({"search_limit": 5}, str(self.home))
        self.assertEqual(self.saved()["plugins"]["memory-buckets"], {"search_limit": 5})


class NoHermesTest(unittest.TestCase):
    def test_save_config_outside_hermes_says_so(self):
        # Outside Hermes the config modules don't exist: an ImportError, not a silent no-op.
        missing = {"hermes_cli": None, "hermes_cli.config": None, "hermes_constants": None}
        with mock.patch.dict(sys.modules, missing):
            with self.assertRaises(ImportError):
                MemoryBucketsProvider().save_config({"search_limit": 5}, "/nowhere")

    def test_shim_defaults_are_untouched(self):
        # _compat.py must keep the ABC's own defaults (schema [], save_config a no-op):
        # the provider overrides both, the shim exists only to stay importable outside Hermes.
        if HERMES_AVAILABLE:
            self.skipTest("running inside Hermes; the shim is not in use")
        self.assertEqual(MemoryProvider.get_config_schema(object()), [])
        self.assertIsNone(MemoryProvider.save_config(object(), {}, ""))


if __name__ == "__main__":
    unittest.main()
