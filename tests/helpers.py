"""Shared test fixtures: temp stores, memory files, and a fake embeddings server."""

import argparse
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import threading
import time
import types
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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


class FakeEmbeddings:
    """OpenAI-compatible /v1/embeddings on an ephemeral port. Knobs:
    ``mode`` = ok | error | slow | garbage | wrong_count; ``dims``."""

    def __init__(self):
        self.mode = "ok"
        self.dims = DIMS
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append({"path": self.path, "body": body, "auth": self.headers.get("Authorization")})
                if outer.mode == "error":
                    self.send_response(500)
                    self.end_headers()
                    return
                if outer.mode == "slow":
                    time.sleep(1.0)
                inputs = body["input"]
                data = [{"index": i, "embedding": fake_vector(t, outer.dims)} for i, t in enumerate(inputs)]
                if outer.mode == "wrong_count":
                    data = data[:-1]
                payload = b"not json" if outer.mode == "garbage" else json.dumps({"data": data}).encode()
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the client timed out first

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class StoreCase(unittest.TestCase):
    """A fresh store per test. ``self.put(path, content)`` writes a file directly."""

    def setUp(self):
        embeddings.reset_backoff()
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.store = store.Store(self.home / "memory-buckets")
        self.store.memories.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()
        embeddings.reset_backoff()

    def put(self, path, content):
        target = self.store.memories / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return target
