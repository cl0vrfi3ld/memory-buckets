"""Shared pytest setup: keep the tests independent of the caller's environment."""

import os

import pytest

# Variables the real-model tests read themselves. They are inputs to those tests
# (config.load() only sees MODEL_DIR among them), so a test that wants one opts in
# by having it set.
OPT_IN = frozenset({
    "MEMORY_BUCKETS_MODEL_DIR",
    "MEMORY_BUCKETS_MODEL_F32",
    "MEMORY_BUCKETS_REFERENCE_TOKENIZER",
})


@pytest.fixture(autouse=True)
def hermetic_environment(monkeypatch):
    """Drop every MEMORY_BUCKETS_* variable the shell set, bar the opt-ins.

    config.load() applies these on top of whatever config a test builds, so a stray
    MEMORY_BUCKETS_STORE in the developer's shell used to fail 25 tests.
    """
    for name in list(os.environ):
        if name.startswith("MEMORY_BUCKETS_") and name not in OPT_IN:
            monkeypatch.delenv(name)
    # Never download the built-in model during tests; tests that want it point
    # MEMORY_BUCKETS_MODEL_DIR at one.
    monkeypatch.setenv("MEMORY_BUCKETS_EMBEDDINGS_AUTO_DOWNLOAD", "0")
