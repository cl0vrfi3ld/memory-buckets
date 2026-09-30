"""``hermes memory-buckets <cmd>``: Hermes imports this file (not ``__init__.py``)
while memory-buckets is the active provider and calls ``register_cli``."""

from .memory_buckets.cli import register_cli  # noqa: F401
