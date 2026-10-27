"""memory-buckets: a Claude-style memory provider for Hermes (MemoryProvider plugin).

Hermes calls ``register(ctx)`` once per ``AIAgent``, so each call hands back a
fresh provider. Concurrent chats get separate instances that share the store
through its lock.

It also registers the ``memory-buckets:sort-imports`` and
``memory-buckets:tidy-memory`` skills. That is optional: a context without
``register_skill`` still gets the provider.
"""

from ._paths import SKILLS_DIR


def register(ctx) -> None:
    # Imported here, not at the top: Hermes's argparse setup imports cli.py through this
    # package and shouldn't pay for loading the provider.
    from .provider import MemoryBucketsProvider

    ctx.register_memory_provider(MemoryBucketsProvider())
    _register_extras(ctx)


def _register_extras(ctx) -> None:
    from . import frontmatter, snapshot

    # Hermes namespaces the skill by the name it loaded us under. As a plugin directory
    # that's "memory-buckets"; an entry-point install (pip or Nix) loads from the package
    # directory ("memory_buckets"). Skip register_skill there: Hermes would list
    # "memory_buckets:sort-imports" in the system prompt's available_skills block and then
    # prune it as an inactive memory provider skill when skill_view is called (README:
    # Importing built-in memory).
    namespace = getattr(ctx, "name", None)
    if namespace not in (None, "memory-buckets"):
        snapshot.SORT_SKILL = None
        return
    snapshot.SORT_SKILL = "memory-buckets:sort-imports"
    register_skill = getattr(ctx, "register_skill", None)
    if callable(register_skill):
        # PluginContext.register_skill stores the description and frontmatter it is given
        # for the prompt's skill index without parsing SKILL.md itself, so pass both from
        # frontmatter.parse; fall back to the 2-arg call for minimal test stubs.
        for child in sorted(SKILLS_DIR.iterdir()):
            skill_md = child / "SKILL.md"
            if child.is_dir() and skill_md.exists():
                fm, _ = frontmatter.parse(skill_md.read_text(encoding="utf-8"))
                description = str(fm.get("description") or "")
                as_dict = getattr(fm, "as_dict", None)
                fm_dict = as_dict() if callable(as_dict) else dict(fm.pairs())
                try:
                    register_skill(child.name, skill_md, description=description, frontmatter=fm_dict)
                except TypeError:
                    register_skill(child.name, skill_md)
