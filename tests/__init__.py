"""Tests for the ``memory_buckets`` package, run from the repo root (``check``).

``PLUGIN_DIR`` is the plugin directory as Hermes loads it: the repo root, with
``plugin.yaml`` and the ``__init__.py`` / ``cli.py`` shims.
"""

import os
from pathlib import Path

# Never download the built-in model during tests; tests that want it point MEMORY_BUCKETS_MODEL_DIR at one.
os.environ.setdefault("MEMORY_BUCKETS_EMBEDDINGS_AUTO_DOWNLOAD", "0")

import memory_buckets  # noqa: E402,F401

PLUGIN_DIR = Path(__file__).resolve().parent.parent
