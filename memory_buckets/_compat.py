"""The only place that imports Hermes's provider ABC.

Inside Hermes we subclass the real ``MemoryProvider``. Outside it (unit tests,
a bare Python) we fall back to a shim with the same abstract surface, so
the plugin stays importable. ``tests/test_abi.py`` (MEM-1 T2) checks the shim
hasn't drifted from the real ABC when ``HERMES_PYTHON`` is available.
"""

from __future__ import annotations

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

    def spawn_context_thread(  # type: ignore[no-redef]
        target: Callable[..., Any], *, name: str, daemon: bool = True,
        args: tuple = (), kwargs: Optional[Dict[str, Any]] = None,
    ) -> threading.Thread:
        return threading.Thread(target=target, args=args, kwargs=kwargs, name=name, daemon=daemon)
