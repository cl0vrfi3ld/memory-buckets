"""memory-buckets: a Claude-style memory provider for Hermes (MemoryProvider plugin).

Hermes calls ``register(ctx)`` once per ``AIAgent``, so each call hands back a
fresh provider. Concurrent chats get separate instances that share the store
through its lock. See ``05 Plan/MEM-1 Plan.md`` §5 in the design vault.

It also registers the ``memory-buckets:sort-inbox`` skill and the
``/memory-pending``, ``/memory-apply`` and ``/memory-reject`` slash commands
(ADR-0016). Those are optional: a context without them still gets the provider.
"""

from ._paths import SKILLS_DIR


def register(ctx) -> None:
    # Imported here, not at the top: Hermes's argparse setup imports cli.py through this
    # package and shouldn't pay for loading the provider.
    from .provider import MemoryBucketsProvider

    ctx.register_memory_provider(MemoryBucketsProvider())
    _register_extras(ctx)


def _register_extras(ctx) -> None:
    from . import pending, snapshot

    register_skill = getattr(ctx, "register_skill", None)
    if register_skill is not None:
        # Hermes namespaces the skill by the name it loaded us under. As a plugin directory
        # that's "memory-buckets"; a pip install loads from the package directory
        # ("memory_buckets"), and Hermes then prunes the skill as not belonging to the active
        # provider, so it can't be loaded there (README: Sorting the inbox).
        namespace = getattr(ctx, "name", None)
        snapshot.SORT_SKILL = "memory-buckets:sort-inbox" if namespace in (None, "memory-buckets") else None
        # The documented bundle-skills layout: skills/<name>/SKILL.md, loaded with
        # skill_view("<plugin>:<name>") and never listed in the prompt's skill index.
        for child in sorted(SKILLS_DIR.iterdir()):
            skill_md = child / "SKILL.md"
            if child.is_dir() and skill_md.exists():
                register_skill(child.name, skill_md)
    register_command = getattr(ctx, "register_command", None)
    if register_command is not None:
        register_command("memory-pending", pending.cmd_pending,
                         description="List pending memory proposals, or show one", args_hint="[id]")
        register_command("memory-apply", pending.cmd_apply,
                         description="Commit one pending memory proposal", args_hint="<id>")
        register_command("memory-reject", pending.cmd_reject,
                         description="Discard one pending memory proposal", args_hint="<id>")
