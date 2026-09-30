"""MemoryBucketsProvider: Hermes hooks, per-session state and wiring (MEM-1 Plan §5).

Hermes runs ``register()`` once per ``AIAgent``, so one instance serves one
agent, which may rotate through several session ids (``on_session_switch``:
/new, /resume, /branch, compression). ``handle_tool_call`` and
``system_prompt_block`` get no session id, so they use the current one. That's
safe because an agent never runs two turns at once. Concurrent chats are
separate instances, sharing the store through its flock and the index through
SQLite.

Set ``MEMORY_BUCKETS_TRACE=1`` to log every hook call (the live smoke test uses it).
"""

from __future__ import annotations

import copy
import functools
import json
import logging
import os
import sqlite3
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config as config_mod
from . import inbox, projects, scopes, snapshot, tools
from ._compat import MemoryProvider, RecallStatus, spawn_context_thread
from .embeddings import make_embedder, min_similarity
from .index import Index
from .store import GLOBAL, Store, StoreError

logger = logging.getLogger("memory_buckets")

MAX_SESSIONS = 64
NUDGE = ("Memory check: has this conversation produced durable facts about the user or their work that are not "
         "saved yet? If yes, save them now with the memory tools, following your memory instructions. If no, do "
         "nothing. Do not mention this check to the user.")


def _sqlite_has_fts5() -> bool:
    try:
        con = sqlite3.connect(":memory:")
        try:
            con.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        finally:
            con.close()
        return True
    except sqlite3.Error:
        return False


def _traced(fn):
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        if os.environ.get("MEMORY_BUCKETS_TRACE"):
            shown = {k: v for k, v in kwargs.items() if k not in ("warning_callback", "status_callback")}
            logger.info("memory-buckets hook %s args=%.300r kwargs=%.300r session=%s",
                        fn.__name__, args, shown, getattr(self, "_current", ""))
        return fn(self, *args, **kwargs)
    return wrapper


@dataclass
class SessionState:
    platform: str = "cli"
    agent_context: str = "primary"
    cwd: str = ""  # the workspace Hermes gave initialize(); projects.py prefers a live one
    turns: int = 0
    nudge_due: bool = False
    snapshot: Optional[snapshot.Snapshot] = None


class MemoryBucketsProvider(MemoryProvider):
    def __init__(self) -> None:
        self.config: Optional[config_mod.Config] = None
        self.store: Optional[Store] = None
        self.index: Optional[Index] = None
        self._sessions: "OrderedDict[str, SessionState]" = OrderedDict()
        self._current = ""
        self._last_recall = 0
        self._lock = threading.Lock()
        self._embed_running = False
        self._embed_again = False
        self._embed_thread = None
        self._shut = False

    # -- identity and availability -----------------------------------------------

    @property
    def name(self) -> str:
        return "memory-buckets"

    def is_available(self) -> bool:
        # No network, no store I/O: this gates activation.
        return _sqlite_has_fts5()

    def unavailable_reason(self) -> str:
        return "" if _sqlite_has_fts5() else "Python's sqlite3 lacks FTS5"

    # -- lifecycle -----------------------------------------------------------------

    @_traced
    def initialize(self, session_id: str, **kwargs) -> None:
        self.config = config_mod.load()
        root = Path(self.config.store_path) if self.config.store_path else Path(kwargs["hermes_home"]) / "memory-buckets"
        self.store = Store(root)
        # Create the store up front (not just on first write), so it exists as soon as the
        # provider is active and the log says where it went (profiles move HERMES_HOME).
        try:
            (self.store.memories / GLOBAL).mkdir(parents=True, exist_ok=True)
            logger.info("memory-buckets: active, store at %s (platform=%s, context=%s)", root,
                        kwargs.get("platform") or "cli", kwargs.get("agent_context") or "primary")
        except OSError as err:
            logger.warning("memory-buckets: can't create the store at %s: %s", root, err)
        embedder = make_embedder(self.config, root, spawn=spawn_context_thread)
        self.index = Index(self.store, embedder)
        self._current = session_id
        self._remember(session_id, SessionState(
            platform=kwargs.get("platform") or "cli",
            agent_context=kwargs.get("agent_context") or "primary",
            cwd=kwargs.get("cwd") or "",
        ))

    @_traced
    def shutdown(self) -> None:
        # Stop the embedding worker before closing SQLite: closing a connection another
        # thread is using can crash the interpreter.
        self._shut = True
        worker = self._embed_thread
        if worker is not None and worker.is_alive():
            worker.join(timeout=10)
            if worker.is_alive():
                logger.warning("memory-buckets: embedding worker still running at shutdown; leaving the index open")
                return
        if self.index is not None:
            self.index.close()

    # -- sessions ------------------------------------------------------------------

    def _remember(self, session_id: str, state: SessionState) -> None:
        with self._lock:
            self._sessions[session_id] = state
            self._sessions.move_to_end(session_id)
            while len(self._sessions) > MAX_SESSIONS:
                self._sessions.popitem(last=False)

    def _state(self, session_id: str = "") -> SessionState:
        sid = session_id or self._current
        with self._lock:
            state = self._sessions.get(sid)
        if state is None:  # unknown id (evicted, or a hook ran before initialize saw it)
            state = SessionState()
            self._remember(sid, state)
        return state

    def _scope(self, session_id: str = "") -> scopes.Scope:
        sid = session_id or self._current
        state = self._state(sid)
        project = projects.session_project(state.cwd)
        return scopes.resolve(self.config, project.bucket if project else None, state.agent_context)

    @_traced
    def on_session_switch(self, new_session_id: str, *, parent_session_id: str = "", reset: bool = False,
                          rewound: bool = False, **kwargs) -> None:
        if rewound or not new_session_id or new_session_id == self._current:
            return
        previous = self._state()
        # Same agent, same workspace: the project follows the cwd, whatever the switch.
        self._remember(new_session_id, SessionState(
            platform=previous.platform, agent_context=previous.agent_context, cwd=previous.cwd,
        ))
        self._current = new_session_id

    # -- prompt and recall -----------------------------------------------------------

    @_traced
    def system_prompt_block(self) -> str:
        if self.store is None:
            return ""
        state = self._state()
        if state.snapshot is None:
            try:
                state.snapshot = snapshot.build(self.store, self.index, self._scope(), self.config)
            except Exception:  # never break the prompt build
                logger.warning("memory-buckets: snapshot failed", exc_info=True)
                return ""
        return state.snapshot.text

    @_traced
    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        interval = self.config.nudge_interval if self.config else 0
        state = self._state()
        state.turns += 1
        if interval > 0 and state.turns % interval == 0:
            state.nudge_due = True

    @_traced
    def prefetch(self, query: str, *, session_id: str = "") -> str:
        self._last_recall = 0
        if self.store is None:
            return ""
        sid = session_id or self._current
        state = self._state(sid)
        scope = self._scope(sid)
        parts = []
        try:
            exclude = state.snapshot.paths if state.snapshot else set()
            hints = self.index.hints(query, scope.reads_by_default, exclude=exclude,
                                     min_similarity=min_similarity(self.config, self.index.embedder),
                                     max_hints=self.config.prefetch_max_hints)
        except Exception:
            logger.warning("memory-buckets: prefetch hints failed", exc_info=True)
            hints = []
        if hints:
            self._last_recall = len(hints)
            lines = [f"- {path}: {desc}" if desc else f"- {path}" for path, desc, _ in hints]
            parts.append("Memory files that may be relevant to this message. Read them with memory_read if they could help "
                         "you answer:\n" + "\n".join(lines))
        if state.nudge_due and not scope.read_only:
            state.nudge_due = False
            parts.append(NUDGE)
        return "\n\n".join(parts)

    @_traced
    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        if self.index is not None and self.index.embedder is not None:
            try:
                self.index.reconcile()
                self.index.embed_backlog(max_batches=4)
            except Exception:
                logger.warning("memory-buckets: background embedding failed", exc_info=True)

    def recall_status(self) -> Optional[RecallStatus]:
        return RecallStatus("memory-buckets", self._last_recall) if self._last_recall else None

    # -- tools -----------------------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return copy.deepcopy(tools.SCHEMAS)

    @_traced
    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if self.store is None:
            return json.dumps({"ok": False, "error": {"code": "unavailable", "message": "memory-buckets isn't initialised"}})
        state = self._state()
        ctx = tools.Context(store=self.store, index=self.index, scope=self._scope(), config=self.config,
                            source=state.platform, on_write=self._after_write)
        return json.dumps(tools.handle(tool_name, args or {}, ctx), ensure_ascii=False)

    def _after_write(self, path: str) -> None:
        if self.index is None or self.index.embedder is None or self._shut:
            return
        if self.index.embedder.down_reason():  # backed off, or the model isn't here yet
            return
        with self._lock:
            if self._embed_running:
                self._embed_again = True
                return
            self._embed_running = True

        def run():
            while True:
                try:
                    self.index.reconcile()
                    while not self._shut and self.index.embed_backlog(max_batches=1):
                        pass
                except Exception:
                    logger.warning("memory-buckets: embedding after write failed", exc_info=True)
                with self._lock:
                    if self._embed_again and not self._shut:
                        self._embed_again = False
                        continue
                    self._embed_running = False
                    return

        self._embed_thread = spawn_context_thread(run, name="memory-buckets-embed")
        self._embed_thread.start()

    # -- migration bridge (ADR-0011) -------------------------------------------------

    @_traced
    def on_memory_write(self, action: str, target: str, content: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Mirror built-in memory writes into global/inbox.md while built-in memory is still on."""
        if self.store is None:
            return
        entry = f"[{target} {action}] {content}".strip()
        previous = (metadata or {}).get("previous_content")
        if action in ("replace", "remove") and previous:
            entry += f" (was: {previous})"
        try:
            inbox.append(self.store, f"mirrored from built-in memory {date.today().isoformat()}", [entry],
                         source=f"builtin:{self._state().platform}")
        except StoreError as err:
            logger.warning("memory-buckets: couldn't mirror a built-in memory write: %s", err.message)

    # -- setup -----------------------------------------------------------------------

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return copy.deepcopy(config_mod.SETUP_SCHEMA)

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        config_mod.save(values)
