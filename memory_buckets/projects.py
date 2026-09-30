"""Which Hermes Project a session is in (spec § Scopes).

Projects are Hermes's own: named workspaces kept in the profile's ``projects.db``
(``hermes project create <name> <folder>``, the desktop sidebar, the
``desktop_project`` tool). A session belongs to the project that owns its working
directory, the same test Hermes uses (``projects_db.project_for_path``: the
innermost project folder containing it). The project's slug names its bucket,
``memories/<slug>/``; Hermes never changes a slug after creation.

The working directory, first found wins:

1. the session's pinned cwd for this turn (``runtime_cwd.scoped_session_cwd``,
   set by multi-session gateways),
2. the ``cwd`` Hermes passed to ``initialize`` (the session's workspace),
3. the agent's working directory (``runtime_cwd.resolve_agent_cwd``: the
   configured ``terminal.cwd``, else where Hermes was started).

Outside Hermes (tests, a broken ``projects.db``) every lookup answers "no project",
so the session is unscoped: it reads and writes ``global/`` only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

from .store import is_project_id

logger = logging.getLogger("memory_buckets")


@dataclass(frozen=True)
class Project:
    bucket: str  # the directory under memories/
    slug: str
    name: str
    primary_path: Optional[str] = None


def bucket_for_slug(slug: str) -> Optional[str]:
    """Hermes slugs allow ``_``; bucket names are kebab-case."""
    bucket = (slug or "").strip().lower().replace("_", "-")
    return bucket if is_project_id(bucket) else None


def _to_project(proj) -> Optional[Project]:
    bucket = bucket_for_slug(proj.slug)
    if bucket is None:
        logger.warning("memory-buckets: Hermes project %r has slug %r, which can't name a bucket; "
                       "treating it as no project", proj.name, proj.slug)
        return None
    return Project(bucket=bucket, slug=proj.slug, name=proj.name, primary_path=getattr(proj, "primary_path", None))


def session_cwd(init_cwd: str = "") -> str:
    try:
        from agent.runtime_cwd import resolve_agent_cwd, scoped_session_cwd
    except ImportError:
        return init_cwd or ""
    try:
        pinned = scoped_session_cwd()
        if pinned:
            return pinned
        if init_cwd:
            return init_cwd
        return str(resolve_agent_cwd())
    except Exception:  # a deleted cwd raises; that's no project, not a failed tool call
        logger.debug("memory-buckets: couldn't resolve the session's cwd", exc_info=True)
        return init_cwd or ""


def project_for_path(path: str) -> Optional[Project]:
    if not path:
        return None
    try:
        from hermes_cli import projects_db as pdb
    except ImportError:
        return None
    try:
        with pdb.connect_closing() as conn:
            proj = pdb.project_for_path(conn, path)
    except Exception:  # never let projects.db trouble break memory
        logger.warning("memory-buckets: couldn't read Hermes projects; this session is unscoped", exc_info=True)
        return None
    return _to_project(proj) if proj is not None else None


def session_project(init_cwd: str = "") -> Optional[Project]:
    """The Hermes project the current session is working in, or None."""
    return project_for_path(session_cwd(init_cwd))


def list_projects() -> Optional[List[Project]]:
    """Every live Hermes project that can name a bucket; None when Hermes isn't importable."""
    try:
        from hermes_cli import projects_db as pdb
    except ImportError:
        return None
    try:
        with pdb.connect_closing() as conn:
            found = pdb.list_projects(conn)
    except Exception:
        logger.warning("memory-buckets: couldn't read Hermes projects", exc_info=True)
        return []
    return [p for p in (_to_project(proj) for proj in found) if p is not None]


def unlinked_note(bucket: str) -> str:
    """A reminder when no Hermes project uses ``bucket`` yet, so no session will be scoped to it."""
    known = list_projects()
    if known is None or any(p.bucket == bucket for p in known):
        return ""
    return (f"No Hermes project uses the bucket '{bucket}' yet, so no session is scoped to it. "
            f"Create one: hermes project create \"<name>\" <folder> --slug {bucket}")
