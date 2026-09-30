"""Shared test fixtures: temp stores, memory files, a fake embedder, a fake Hermes."""

import argparse
import hashlib
import math
import os
import re
import sys
import tempfile
import time
import types
import unittest
from array import array
from contextlib import contextmanager
from pathlib import Path

from memory_buckets import embeddings, store

DIMS = 64


def doc(name, description, body="- a fact\n", extra=""):
    return f"---\nname: {name}\ndescription: {description}\n{extra}---\n{body}"


def fake_vector(text, dims=DIMS):
    """Hashed bag of words: texts sharing words get similar vectors."""
    vec = [0.0] * dims
    for word in re.findall(r"\w+", text.lower()):
        h = int(hashlib.sha256(word.encode()).hexdigest(), 16)
        vec[h % dims] += 1.0 if (h >> 8) & 1 else -1.0
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


@contextmanager
def fake_hermes(projects=None, *, pinned="", agent_cwd="/nowhere"):
    """Stand-ins for Hermes's ``projects_db`` and ``runtime_cwd``, as ``memory_buckets.projects``
    uses them. ``projects`` maps a slug to its folder; ``pinned`` is the per-turn session cwd."""
    rows = [types.SimpleNamespace(slug=slug, name=slug.title(), primary_path=folder)
            for slug, folder in (projects or {}).items()]

    @contextmanager
    def connect_closing(db_path=None):
        yield None

    def project_for_path(conn, path):
        owners = [r for r in rows if path == r.primary_path or path.startswith(r.primary_path.rstrip("/") + os.sep)]
        return max(owners, key=lambda r: len(r.primary_path)) if owners else None

    pdb = types.ModuleType("hermes_cli.projects_db")
    pdb.connect_closing, pdb.project_for_path = connect_closing, project_for_path
    pdb.list_projects = lambda conn, include_archived=False: list(rows)
    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.__path__, hermes_cli.projects_db = [], pdb
    rc = types.ModuleType("agent.runtime_cwd")
    rc.scoped_session_cwd = lambda: pinned
    rc.resolve_agent_cwd = lambda: agent_cwd
    agent = types.ModuleType("agent")
    agent.__path__, agent.runtime_cwd = [], rc
    fakes = {"hermes_cli": hermes_cli, "hermes_cli.projects_db": pdb, "agent": agent, "agent.runtime_cwd": rc}
    saved = {name: sys.modules.get(name) for name in fakes}
    sys.modules.update(fakes)
    try:
        yield
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def run_hermes_cli(*argv) -> int:
    """``hermes memory-buckets <argv>``, wired the way Hermes wires a plugin's CLI."""
    from memory_buckets import cli

    parser = argparse.ArgumentParser(prog="hermes memory-buckets")
    cli.register_cli(parser)
    args = parser.parse_args(list(argv))
    try:
        args.func(args)
    except SystemExit as exc:
        return exc.code
    return 0


class FakeEmbedder:
    """Stands in for the built-in model: the same interface, with hashed bag-of-words
    vectors. Knobs: ``down`` (a reason, or None), ``delay`` (seconds per embed call),
    ``name`` (part of ``identity``: changing it means re-embedding)."""

    default_min_similarity = 0.27
    batch = 32

    def __init__(self, name="fake"):
        self.name = name
        self.down = None
        self.delay = 0.0
        self.calls = []

    @property
    def identity(self):
        return f"fake:{self.name}"

    def describe(self):
        return f"fake model {self.name}"

    def down_reason(self):
        return self.down

    def embed(self, texts):
        if self.down:
            raise embeddings.EmbeddingError(self.down)
        if self.delay:
            time.sleep(self.delay)
        self.calls.append(list(texts))
        return [array("f", fake_vector(t)) for t in texts]

    def embed_documents(self, texts):
        return self.embed(texts)

    def embed_query(self, text):
        return self.embed([text])[0]


class StoreCase(unittest.TestCase):
    """A fresh store per test. ``self.put(path, content)`` writes a file directly."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.store = store.Store(self.home / "memory-buckets")
        self.store.memories.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def put(self, path, content):
        target = self.store.memories / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return target
