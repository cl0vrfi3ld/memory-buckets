"""MemoryBucketsProvider: Hermes hooks, per-session state and wiring.

Hermes runs ``register()`` once per ``AIAgent``, so one instance serves one
agent, which may rotate through several session ids (``on_session_switch``:
/new, /resume, /branch, compression). ``handle_tool_call`` and
``system_prompt_block`` get no session id, so they use the current one. That's
safe because an agent never runs two turns at once. Concurrent chats are
separate instances, sharing the store through its file lock and the index through
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
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import config as config_mod
from . import inbox, migrate, projects, scopes, snapshot, tools
from ._compat import (TUI_PLATFORMS, MemoryProvider, RecallStatus, spawn_context_thread, status_muted,
                      tui_status_callback)
from .embeddings import make_embedder, min_similarity
from .index import Index
from .store import GLOBAL, Store, StoreError

logger = logging.getLogger("memory_buckets")

MAX_SESSIONS = 64
HINT_COOLDOWN_TURNS = 5
NUDGE = ("Memory check: has this conversation produced new durable facts about the user or their work? If yes, "
         "save them now with the memory tools, following your memory instructions. If no, do nothing. Do not "
         "mention this check to the user.")


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
    prefetch_turns: int = 0
    hint_seen: Dict[str, int] = field(default_factory=dict)  # path -> prefetch_turn when hinted or read/written
    nudge_due: bool = False
    snapshot: Optional[snapshot.Snapshot] = None
    status: Optional[Callable[..., Any]] = None  # status_callback from initialize: only the classic CLI gets one


class MemoryBucketsProvider(MemoryProvider):
    def __init__(self) -> None:
        self.config: Optional[config_mod.Config] = None
        self.store: Optional[Store] = None
        self.index: Optional[Index] = None
        self._sessions: "OrderedDict[str, SessionState]" = OrderedDict()
        self._current = ""
        self._last_recall = 0
        self._lock = threading.Lock()
        self._embed_running = threading.Lock()
        self._embed_again = False
        self._embed_thread = None
        self._shut = False
        # queue_prefetch runs on Hermes's executor, which can outlive shutdown(): count the
        # calls in flight so shutdown can wait for them (the event is set while none run).
        self._prefetching = 0
        self._prefetch_idle = threading.Event()
        self._prefetch_idle.set()

    # -- identity and availability -----------------------------------------------

    @property
    def name(self) -> str:
        return "memory-buckets"

    def is_available(self) -> bool:
        # No network, no store I/O: this gates activation.
        return _sqlite_has_fts5()

    def unavailable_reason(self) -> str:
        return "" if _sqlite_has_fts5() else "Python's sqlite3 lacks FTS5"

    def backup_paths(self) -> List[str]:
        cfg = self.config if self.store is not None and self.config is not None else config_mod.load()
        return [str(Path(cfg.store_path).expanduser())] if cfg.store_path else []

    # -- lifecycle -----------------------------------------------------------------

    @_traced
    def initialize(self, session_id: str, **kwargs) -> None:
        self.config = config_mod.load()
        root = Path(self.config.store_path).expanduser() if self.config.store_path else Path(kwargs["hermes_home"]) / "memory-buckets"
        self.store = Store(root)
        # Create the store up front (not just on first write), so it exists as soon as the
        # provider is active and the log says where it went (profiles move HERMES_HOME).
        try:
            (self.store.memories / GLOBAL).mkdir(parents=True, exist_ok=True)
            logger.info("memory-buckets: active, store at %s (platform=%s, context=%s)", root,
                        kwargs.get("platform") or "cli", kwargs.get("agent_context") or "primary")
        except OSError as err:
            logger.warning("memory-buckets: can't create the store at %s: %s", root, err)
        self._migrate(kwargs.get("agent_context") or "primary")
        embedder = make_embedder(self.config, root, spawn=spawn_context_thread)
        self.index = Index(self.store, embedder)
        self._current = session_id
        self._remember(session_id, SessionState(
            platform=kwargs.get("platform") or "cli",
            agent_context=kwargs.get("agent_context") or "primary",
            cwd=kwargs.get("cwd") or "",
            status=kwargs.get("status_callback") if callable(kwargs.get("status_callback")) else None,
        ))

    def _migrate(self, agent_context: str) -> None:
        """Bring an older store up to date. Read-only sessions leave it for a writable one."""
        if self.store is None or scopes.resolve(self.config, None, agent_context).read_only:
            return
        try:
            for step in migrate.run(self.store):
                level = logging.WARNING if step.action == "skip" else logging.INFO
                logger.log(level, "memory-buckets: migration: %s", step.describe())
        except (StoreError, OSError) as err:  # never fail startup; the next session tries again
            logger.warning("memory-buckets: migration didn't run (%s); `hermes memory-buckets migrate` retries it",
                           getattr(err, "message", err))

    @_traced
    def shutdown(self) -> None:
        # Stop the embedding worker and any queue_prefetch before closing SQLite: closing a
        # connection another thread is using can crash the interpreter. Setting _shut under
        # the lock means a prefetch either is counted before we look or never starts.
        with self._lock:
            self._shut = True
        deadline = time.monotonic() + 10
        worker = self._embed_thread
        if worker is not None and worker.is_alive():
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
            if worker.is_alive():
                logger.warning("memory-buckets: embedding worker still running at shutdown; leaving the index open")
                return
        if not self._prefetch_idle.wait(timeout=max(0.0, deadline - time.monotonic())):
            logger.warning("memory-buckets: prefetch still running at shutdown; leaving the index open")
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
        return self._scope_and_project(session_id)[0]

    def _read_scope(self, session_id: str = "") -> scopes.Scope:
        """The session's read scope (project and read_only), resolved without
        listing every Hermes project or scanning on-disk project buckets."""
        sid = session_id or self._current
        state = self._state(sid)
        project = projects.session_project(state.cwd, session_id=sid)
        return scopes.resolve(self.config, project.bucket if project else None, state.agent_context)

    def _scope_and_project(self, session_id: str = "") \
            -> "tuple[scopes.Scope, Optional[projects.Project], Optional[List[str]]]":
        """The session's scope, its Hermes project (or None), and the buckets of
        every Hermes project (None outside Hermes), from one project-list read."""
        sid = session_id or self._current
        state = self._state(sid)
        project = projects.session_project(state.cwd, session_id=sid)
        hermes_buckets = self._hermes_buckets()
        scope = scopes.resolve(self.config, project.bucket if project else None, state.agent_context,
                               buckets=scopes.known_buckets(
                                   self.store.projects() if self.store else [], hermes_buckets))
        return scope, (project if scope.project else None), hermes_buckets

    @_traced
    def on_session_switch(self, new_session_id: str, *, parent_session_id: str = "", reset: bool = False,
                          rewound: bool = False, **kwargs) -> None:
        if rewound or not new_session_id:
            return
        if new_session_id == self._current:
            state = self._state(new_session_id)
            state.snapshot = None
            state.hint_seen.clear()
            state.prefetch_turns = 0
            return
        previous = self._state()
        # Same agent, same workspace: the project follows the cwd, whatever the switch.
        self._remember(new_session_id, SessionState(
            platform=previous.platform, agent_context=previous.agent_context, cwd=previous.cwd,
            status=previous.status,
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
                scope, project, hermes_buckets = self._scope_and_project()
                state.snapshot = snapshot.build(self.store, self.index, scope, self.config, project,
                                                hermes_buckets=hermes_buckets)
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
        scope = self._read_scope(sid)
        parts = []
        hints = []
        if query and query.strip() and self.index is not None and self.index.embedder is not None and self.config is not None:
            state.prefetch_turns += 1
            turn = state.prefetch_turns
            try:
                if state.snapshot is not None:
                    base_exclude = state.snapshot.paths
                else:
                    base_exclude = {"global/profile.md", "global/preferences.md"} | (
                        {f"{scope.project}/profile.md", f"{scope.project}/preferences.md"} if scope.project else set()
                    )
                cooldown_exclude = {
                    path for path, seen_turn in state.hint_seen.items()
                    if turn - seen_turn < HINT_COOLDOWN_TURNS
                }
                exclude = base_exclude | cooldown_exclude
                acquired = self._embed_running.acquire(blocking=False)
                try:
                    hints = self.index.hints(query, scope.reads_by_default, exclude=exclude,
                                             min_similarity=min_similarity(self.config, self.index.embedder),
                                             max_hints=self.config.prefetch_max_hints,
                                             project=scope.project, boost=self.config.project_boost,
                                             embed_backlog=acquired)
                finally:
                    if acquired:
                        self._embed_running.release()
                for path, _, _ in hints:
                    state.hint_seen[path] = turn
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
            with self._lock:
                if self._shut:  # the index may already be closed
                    return
                self._prefetching += 1
                self._prefetch_idle.clear()
            try:
                self.index.reconcile()
                if self._embed_running.acquire(blocking=False):
                    try:
                        self.index.embed_backlog(max_batches=4)
                    finally:
                        self._embed_running.release()
            except Exception:
                logger.warning("memory-buckets: background embedding failed", exc_info=True)
            finally:
                with self._lock:
                    self._prefetching -= 1
                    if not self._prefetching:
                        self._prefetch_idle.set()

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
                            source=state.platform, on_write=self._after_write, notify=self._notifier(state))
        try:
            res = tools.handle(tool_name, args or {}, ctx)
        except sqlite3.Error as err:
            # The index is only a cache: a corrupt SQLite file is rebuilt, never a crash.
            # Only sqlite3.Error: store writes never touch SQLite synchronously, so a retry here
            # can't repeat a write (an OSError from a write could, e.g. a double append).
            logger.warning("memory-buckets: index error (%s); rebuilding the cache and retrying", err)
            try:
                if self.index is not None:
                    self.index.rebuild()
                res = tools.handle(tool_name, args or {}, ctx)
            except sqlite3.Error as err2:
                logger.warning("memory-buckets: index still failing after rebuild: %s", err2)
                return json.dumps({"ok": False, "error": {"code": "index_error",
                                                          "message": f"the search index is broken and couldn't be rebuilt: {err2}"}},
                                  ensure_ascii=False)
        if res.get("ok"):
            effective_turn = max(state.prefetch_turns, 1)
            if tool_name == "memory_read":
                for entry in res.get("files", ()):
                    if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                        state.hint_seen[entry["path"]] = effective_turn
            elif tool_name in ("memory_write", "memory_append", "memory_str_replace") and isinstance(res.get("path"), str):
                state.hint_seen[res["path"]] = effective_turn
            elif tool_name == "memory_delete" and isinstance(res.get("path"), str):
                state.hint_seen.pop(res["path"], None)
        return json.dumps(res, ensure_ascii=False)

    @staticmethod
    def _hermes_buckets() -> Optional[List[str]]:
        """Buckets of every Hermes project, from one projects.list_projects() read;
        None outside Hermes."""
        found = projects.list_projects()
        return None if found is None else [p.bucket for p in found]

    def _notifier(self, state: SessionState) -> Optional[Callable[[str], bool]]:
        """Show the user a line on Hermes's status line, where there is one:
        the classic CLI hands it to ``initialize``; the terminal TUI has one on
        the agent, found at call time (the agent registers after ``initialize``).
        Desktop ignores this kind of status update and gateways have no status
        line: None, and the notice is simply not shown."""
        status = state.status
        if status is not None:
            # agent._emit_status: its __self__ is the agent, whose flags say
            # whether the line would actually print.
            agent = getattr(status, "__self__", None)

            def notify(message: str) -> bool:
                if status_muted(agent):
                    return False
                status(message)  # agent._emit_status(message): prints in the classic CLI
                return True
            return notify
        if state.platform in TUI_PLATFORMS:
            def notify_tui(message: str) -> bool:
                show = tui_status_callback(self)
                if show is None:
                    return False
                show(message)
                return True
            return notify_tui
        return None

    def _after_write(self, path: str) -> None:
        if self.index is None or self.index.embedder is None or self._shut:
            return
        if self.index.embedder.down_reason():  # the model isn't here yet
            return
        with self._lock:
            if not self._embed_running.acquire(blocking=False):
                self._embed_again = True
                return

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
                    self._embed_running.release()
                    return

        self._embed_thread = spawn_context_thread(run, name="memory-buckets-embed")
        self._embed_thread.start()

    # -- migration bridge ------------------------------------------------------------

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


