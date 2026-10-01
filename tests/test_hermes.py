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
        # skills.external_dirs is how /sort-inbox becomes a slash command (README: The inbox and proposals).
        (home / "config.yaml").write_text(
            "memory:\n  provider: memory-buckets\n  memory_enabled: false\n  user_profile_enabled: false\n"
            f"skills:\n  external_dirs:\n    - {skills_dir}\n")
        env = {"PATH": os.environ.get("PATH", ""), "HOME": tmp, "HERMES_HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1",
               "MEMORY_BUCKETS_EMBEDDINGS_AUTO_DOWNLOAD": "0"}
        if pythonpath:
            env["PYTHONPATH"] = pythonpath
        proc = subprocess.run([HERMES_PYTHON, str(Path(__file__).with_name("hermes_probe.py"))],
                              env=env, cwd=tmp, capture_output=True, text=True, timeout=120)
        lines = [l for l in proc.stdout.splitlines() if l.startswith("PROBE-RESULT ")]
        self.assertTrue(lines, f"probe failed:\n{proc.stdout}\n{proc.stderr}")
        return json.loads(lines[-1][len("PROBE-RESULT "):])

    def test_real_hermes_loads_the_plugin_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "hermes-home"
            (home / "plugins").mkdir(parents=True)
            shutil.copytree(PLUGIN_DIR, home / "plugins/memory-buckets", ignore=IGNORE)
            out = self.probe(tmp, home)
            self.check(out)
            self.assertTrue(out["skill_ok"], "skill_view('memory-buckets:sort-inbox')")
            self.assertEqual(out["skill_name"], "memory-buckets:sort-inbox")

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
            self.assertIn("/site/memory_buckets", out["provider_file"])
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
        self.assertEqual(len(out["tools"]), 8)
        self.assertEqual(out["core_clash"], [])
        self.assertTrue(out["store_root"].endswith("hermes-home/memory-buckets"))
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
        self.assertEqual(out["slash"], ["memory-apply", "memory-pending", "memory-reject"])
        self.assertTrue(out["slash_skill"], "/sort-inbox via skills.external_dirs")
        self.assertTrue(out["propose_ok"])
        self.assertTrue(out["staged_not_written"])
        self.assertIn("Applied", out.get("apply_reply", ""))
        self.assertTrue(out["applied"])
        self.assertEqual(out["project_scope"]["project"], "proj-1")
        self.assertEqual(out["project_scope"]["source"], "hermes-project")
        self.assertEqual(out["project_scope"]["writes"], ["proj-1/", "global/"])
        self.assertIsNone(out["elsewhere_scope"])


if __name__ == "__main__":
    unittest.main()
