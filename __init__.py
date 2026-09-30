"""Plugin-directory entry point: Hermes loads ``$HERMES_HOME/plugins/memory-buckets/``
from here. The code lives in the ``memory_buckets`` package beside this file.

Hermes spots a memory provider by this file mentioning ``register_memory_provider``
or ``MemoryProvider``; ``memory_buckets.register`` calls ``ctx.register_memory_provider``.
"""

if __package__:  # pytest also imports this file, as a bare module, because the repo root is the plugin dir
    from .memory_buckets import register  # noqa: F401
