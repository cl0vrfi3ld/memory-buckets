"""The conftest fixture that keeps MEMORY_BUCKETS_* variables in the caller's shell out of the tests."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

CONFTEST = Path(__file__).with_name("conftest.py")

PROBE = '''
import os


def test_probe():
    seen = sorted(k for k in os.environ if k.startswith("MEMORY_BUCKETS_"))
    assert seen == ["MEMORY_BUCKETS_EMBEDDINGS_AUTO_DOWNLOAD", "MEMORY_BUCKETS_MODEL_DIR"], seen
    assert os.environ["MEMORY_BUCKETS_EMBEDDINGS_AUTO_DOWNLOAD"] == "0"
    assert os.environ["MEMORY_BUCKETS_MODEL_DIR"] == "/opt/in"
'''


class HermeticEnvironmentTest(unittest.TestCase):
    def test_shell_overrides_are_cleared_and_opt_ins_kept(self):
        # Run the real conftest in a throwaway pytest session, since this process's own
        # fixture has already scrubbed the variables by the time the test body runs.
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "conftest.py").write_text(CONFTEST.read_text(encoding="utf-8"), encoding="utf-8")
            (Path(tmp) / "test_probe.py").write_text(PROBE, encoding="utf-8")
            env = {k: v for k, v in os.environ.items() if not k.startswith("MEMORY_BUCKETS_")}
            env.update(MEMORY_BUCKETS_STORE="/elsewhere", MEMORY_BUCKETS_TRACE="1",
                       MEMORY_BUCKETS_EMBEDDINGS_AUTO_DOWNLOAD="1", MEMORY_BUCKETS_MODEL_DIR="/opt/in")
            proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                                  "--rootdir", tmp, tmp],
                                 env=env, cwd=tmp, capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)