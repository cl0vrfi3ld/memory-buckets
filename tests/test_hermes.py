"""Integration with real Hermes: runs only when HERMES_PYTHON points at Hermes's interpreter.

Everything happens under a throwaway HERMES_HOME (and HOME): nothing is
installed into, or read from, a real Hermes profile.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from . import PLUGIN_DIR

HERMES_PYTHON = os.environ.get("HERMES_PYTHON")


CAN_PIP = importlib.util.find_spec("pip") is not None and importlib.util.find_spec("hatchling") is not None
IGNORE = shutil.ignore_patterns("tests", "__pycache__", "build", "dist", "*.egg-info", "model", ".*")


@unittest.skipUnless(HERMES_PYTHON, "set HERMES_PYTHON to Hermes's interpreter to run")
class HermesIntegrationTest(unittest.TestCase):
    def probe(self, tmp, home, pythonpath=None, skills_dir="plugins/memory-buckets/skills"):
        # skills.external_dirs is how /sort-imports becomes a slash command (README: Importing built-in memory).
        (home / "config.yaml").write_text(
            "memory:\n  provider: memory-buckets\n  memory_enabled: false\n  user_profile_enabled: false\n"
            f"skills:\n  external_dirs:\n    - {skills_dir}\n", encoding="utf-8")
        env = {"PATH": os.environ.get("PATH", ""), "HOME": tmp, "HERMES_HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1",
               "MEMORY_BUCKETS_EMBEDDINGS_AUTO_DOWNLOAD": "0"}
        if sys.platform == "win32":
            # Python won't start without SYSTEMROOT; the rest keep the probe out of the real user profile.
            env.update({k: os.environ[k] for k in ("SYSTEMROOT", "COMSPEC", "PATHEXT") if k in os.environ})
            env.update({k: tmp for k in ("USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP")})
        if pythonpath:
            env["PYTHONPATH"] = pythonpath
        proc = subprocess.run([HERMES_PYTHON, str(Path(__file__).with_name("hermes_probe.py"))],
                              env=env, cwd=tmp, capture_output=True, text=True, timeout=120)
        lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("PROBE-RESULT ")]
        self.assertTrue(lines, f"probe failed:\n{proc.stdout}\n{proc.stderr}")
        return json.loads(lines[-1][len("PROBE-RESULT "):])

    def test_real_hermes_loads_the_plugin_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "hermes-home"
            (home / "plugins").mkdir(parents=True)
            shutil.copytree(PLUGIN_DIR, home / "plugins/memory-buckets", ignore=IGNORE)
            out = self.probe(tmp, home)
            self.check(out)
            self.assertTrue(out["skill_ok"], "skill_view('memory-buckets:sort-imports')")
            self.assertEqual(out["skill_name"], "memory-buckets:sort-imports")

    @unittest.skipUnless(CAN_PIP, "needs pip and hatchling (uv sync installs them)")
    def test_real_hermes_finds_the_pip_install_by_entry_point(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, site, home = Path(tmp) / "src", Path(tmp) / "site", Path(tmp) / "hermes-home"
            shutil.copytree(PLUGIN_DIR, src, ignore=IGNORE)  # pip builds in-tree; keep the repo clean
            home.mkdir()  # no plugins/ directory: only the entry point can find it
            # Offline: skip the build hook's model download.
            subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--no-deps", "--no-index",
                            "--no-build-isolation", "--target", str(site), str(src)],
                           check=True, capture_output=True, timeout=300,
                           env={**os.environ, "MEMORY_BUCKETS_BUNDLE_MODEL": "0"})
            out = self.probe(tmp, home, pythonpath=str(site), skills_dir=str(site / "memory_buckets/skills"))
            self.assertTrue(Path(out["provider_file"]).resolve().is_relative_to((site / "memory_buckets").resolve()),
                            out["provider_file"])
            self.check(out)
            # Hermes namespaces a pip install's skills "memory_buckets:" and prunes them (README).
            # If this starts failing, Hermes fixed it: drop the fallback in __init__.py.
            self.assertFalse(out["skill_ok"])
            self.assertIsNone(out["skill_name"])

    def check(self, out):
        self.assertEqual([d[0] for d in out["discovered"]], ["memory-buckets"])
        self.assertTrue(out["discovered"][0][2], "is_available")
        self.assertTrue(out["is_provider"])
        self.assertTrue(out["distinct_instances"])
        self.assertEqual(out["signature_problems"], [])
        self.assertEqual(out["internals_problems"], [], "private Hermes internals the plugin falls back on")
        self.assertEqual(len(out["tools"]), 7)
        self.assertEqual(out["core_clash"], [])
        self.assertEqual(Path(out["store_root"]).parts[-2:], ("hermes-home", "memory-buckets"))
        self.assertTrue(out["store_created_on_init"])
        self.assertTrue(out["write_ok"])
        self.assertTrue(out["file_on_disk"])
        self.assertTrue(out["prompt_has_block"])
        self.assertEqual(out["prefetch"], "")
        self.assertEqual(out["current_after_switch"], "s2")
        self.assertEqual(out["cli_names"], ["memory-buckets"])
        self.assertEqual(out["cli_exit"], 0)
        self.assertEqual(out["diagnose_exit"], 0)
        self.assertFalse(out["pm_member"], "plugin.yaml needs python_runtime: external")
        self.assertEqual(out["slash"], [], "the plugin registers no slash commands")
        self.assertTrue(out["slash_skill"], "/sort-imports via skills.external_dirs")
        self.assertEqual(out["project_scope"]["project"], "proj-1")
        self.assertEqual(out["project_scope"]["source"], "hermes-project")
        self.assertEqual(out["project_scope"]["writes"], ["proj-1/", "global/"])
        self.assertIsNone(out["elsewhere_scope"])
        # The real threat matcher. tests/test_threats.py patches _compat._scan_for_threats
        # with a hand-written FakeScanner (outside Hermes there is no tools.threat_patterns),
        # so these probe fields are the only coverage of the real one: the plugin must have
        # resolved to Hermes's own scanner, Hermes must still flag the canonical injection
        # line, and flagged text must be blocked through the plugin's wiring.
        self.assertTrue(out["threat_scan_available"], "the plugin loaded no threat scanner")
        self.assertTrue(out["plugin_scan_is_real"], "the plugin did not resolve to Hermes's own scanner")
        self.assertTrue(out["scan_window_matches"], "the plugin's scan window is not Hermes's MAX_SCAN_CHARS")
        self.assertIn("prompt_injection", out["injection_patterns"],
                      "Hermes's matcher no longer flags test_threats.py's canonical INJECTION line")
        refused = out["write_refused"]
        self.assertFalse(refused["ok"], refused)
        self.assertEqual(refused["error"]["code"], "threat_detected")
        self.assertIn("prompt_injection", refused["patterns"])
        self.assertIn("prompt_injection", refused["error"]["message"])
        self.assertNotIn("previous instructions", refused["error"]["message"],
                         "a refusal must not quote the flagged text")
        self.assertFalse(out["evil_on_disk"], "a refused write reached the disk")
        self.assertTrue(out["prompt_has_clean_profile"], "the clean profile stopped inlining")
        self.assertTrue(out["prompt_blocked_body"],
                        "a flagged file body was not replaced by the [BLOCKED: ...] stand-in")
        self.assertTrue(out["prompt_blocked_description"],
                        "a flagged description was not replaced by the [BLOCKED: ...] stand-in")
        self.assertFalse(out["prompt_leaks"], "flagged text reached the system prompt")
        self.assertFalse(out["prefetch_leaks"], "flagged text reached the prefetch output")


if __name__ == "__main__":
    unittest.main()
