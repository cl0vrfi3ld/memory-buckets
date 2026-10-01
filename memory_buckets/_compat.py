"""The only place that imports Hermes's provider ABC, or reaches into Hermes internals.

Inside Hermes we subclass the real ``MemoryProvider``. Outside it (unit tests,
a bare Python) we fall back to a shim with the same abstract surface, so
the plugin stays importable. ``tests/hermes_probe.py`` checks the provider's
overrides against the real ABC when ``HERMES_PYTHON`` is set.
"""

from __future__ import annotations

import sys
import threading
from typing import Any, Callable, Dict, Optional

try:
    from agent.memory_provider import MemoryProvider, RecallStatus, spawn_context_thread

    HERMES_AVAILABLE = True
except ImportError:  # not running inside Hermes
    from abc import ABC, abstractmethod
    from dataclasses import dataclass

    HERMES_AVAILABLE = False

    @dataclass(frozen=True)
    class RecallStatus:  # type: ignore[no-redef]
        provider_label: str
        count: int
        glyph: str = "🧠"

    class MemoryProvider(ABC):  # type: ignore[no-redef]
        """Test shim mirroring agent.memory_provider.MemoryProvider (rev 59004a62)."""

        @property
        @abstractmethod
        def name(self) -> str: ...

        @abstractmethod
        def is_available(self) -> bool: ...

        @abstractmethod
        def initialize(self, session_id: str, **kwargs) -> None: ...

        @abstractmethod
        def get_tool_schemas(self): ...

        def shutdown(self) -> None:
            pass

        def get_config_schema(self):
            return []

        def save_config(self, values, hermes_home: str) -> None:
            pass

    def spawn_context_thread(  # type: ignore[no-redef]
        target: Callable[..., Any], *, name: str, daemon: bool = True,
        args: tuple = (), kwargs: Optional[Dict[str, Any]] = None,
    ) -> threading.Thread:
        return threading.Thread(target=target, args=args, kwargs=kwargs, name=name, daemon=daemon)


TUI_PLATFORMS = ("tui",)


def status_muted(agent: Any) -> bool:
    """Whether Hermes would drop a status line from ``agent`` right now: quiet
    and one-shot runs (``suppress_status_output``) and muted notification turns
    (``_mute_notification_reply``). Both are read live: a muted turn sets them
    only while it runs. Unknown agents (None) aren't muted."""
    return bool(getattr(agent, "suppress_status_output", False)
                or getattr(agent, "_mute_notification_reply", False))


def tui_status_callback(provider: Any) -> Optional[Callable[[str], None]]:
    """The TUI's status line for the agent that owns ``provider``, or None.

    Hermes passes ``status_callback`` to memory providers only on the classic CLI
    (``agent_init._memory_provider_init_kwargs``). A TUI agent has one too:
    ``tui_gateway`` builds each agent with ``status_callback=(kind, text)``, which
    emits ``status.update`` to the UI. The agent lives in ``tui_gateway.server._sessions``
    (``{sid: {"agent": AIAgent, ...}}``); its ``_memory_manager.providers`` holds this
    provider. Never imports the TUI: outside its process the module isn't loaded.
    Any surprise in that private structure means None, so callers fall back, and
    so does a muted agent (the TUI drops status updates on muted turns).
    """
    server = sys.modules.get("tui_gateway.server")
    sessions = getattr(server, "_sessions", None)
    if not isinstance(sessions, dict):
        return None
    try:
        for session in list(sessions.values()):
            agent = session.get("agent") if isinstance(session, dict) else None
            manager = getattr(agent, "_memory_manager", None)
            if manager is None or not any(p is provider for p in getattr(manager, "providers", None) or []):
                continue
            callback = getattr(agent, "status_callback", None)
            if callable(callback) and not status_muted(agent):
                def show(message: str) -> None:
                    callback("lifecycle", message)
                return show
    except Exception:  # a changed private structure must never break a tool call
        return None
    return None
