"""Session scope: which prefixes a session reads and writes.

Resolved on every tool call, so moving the session into another Hermes project
mid-chat applies straight away. The project comes from Hermes's own Projects
(``projects.session_project``); a session outside every project is unscoped.

Reads: explicit paths anywhere are allowed ("project subtrees on request").
Default reads (listing, search, snapshot) are ``global/`` plus the session's
project.

Writes (ADR-0015): unscoped ⇒ ``global/``. Project-scoped ⇒ the project plus
all of ``global/``. Any writable session may also append to existing files and
create new ones in another project's bucket; overwriting, ``str_replace`` and
deletes there stay with that project's own sessions. cron/subagent/flush
contexts, or ``readonly``, write nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from .config import Config
from .store import GLOBAL, StoreError, is_project_id

READ_ONLY_CONTEXTS = {"cron", "subagent", "flush"}


@dataclass
class Scope:
    project: Optional[str] = None
    source: str = "unscoped"  # hermes-project | unscoped
    read_only: bool = False
    read_only_reason: str = ""
    write_prefixes: List[str] = field(default_factory=list)
    # Buckets this session may append to or create files in, but not edit or
    # delete: every known bucket except the session's own.
    other_buckets: List[str] = field(default_factory=list)

    @property
    def read_prefixes(self) -> List[str]:
        return [f"{GLOBAL}/"] + ([f"{self.project}/"] if self.project else [])

    def reads_by_default(self, path: str) -> bool:
        return any(path.startswith(p) for p in self.read_prefixes)

    def can_write(self, path: str) -> bool:
        return any(path == p or (p.endswith("/") and path.startswith(p)) for p in self.write_prefixes)

    @staticmethod
    def _bucket_of(path: str) -> str:
        return path.split("/", 1)[0]

    def cross_project_write(self, path: str) -> bool:
        """True for an add into another known bucket (provenance, status line)."""
        return not self.can_write(path) and self._bucket_of(path) in self.other_buckets

    def check_write(self, path: str, *, append: bool = False, create: bool = False) -> None:
        """``append``/``create`` name what the caller is doing; without them the
        write may edit. In another bucket only append and create pass."""
        if self.read_only:
            raise StoreError("read_only", f"memory is read-only in this session ({self.read_only_reason})")
        if self.can_write(path):
            return
        if self.cross_project_write(path):
            if append or create:
                return
            raise StoreError(
                "out_of_scope",
                f"you may only add to {self._bucket_of(path)}/. Use memory_append, or memory_write "
                'with if_version "new" (no edits or deletes there)',
                allowed_prefixes=list(self.write_prefixes),
                add_only_buckets=list(self.other_buckets),
            )
        where = f"project '{self.project}'" if self.project else "an unscoped session"
        raise StoreError(
            "out_of_scope",
            f"{path} is outside what {where} may write",
            allowed_prefixes=list(self.write_prefixes),
        )

    def describe(self) -> dict:
        return {
            "project": self.project,
            "source": self.source,
            "reads": self.read_prefixes,
            "writes": [] if self.read_only else list(self.write_prefixes),
            "read_only": self.read_only,
        }


def known_buckets(on_disk: List[str], hermes: Optional[List[str]]) -> List[str]:
    """Buckets the session could write into across projects: on-disk ∪ Hermes's, sorted."""
    return sorted({b for b in [*on_disk, *(hermes or [])] if is_project_id(b)})


def resolve(config: Config, project: Optional[str] = None, agent_context: str = "primary",
            buckets: Optional[List[str]] = None) -> Scope:
    """``project`` is the session's bucket (a Hermes project slug), or None.
    ``buckets`` are the known buckets (on-disk ∪ Hermes); anything in them but
    not the session's own may be appended to or created in."""
    if project is not None and not is_project_id(project):
        project = None
    scope = Scope(project=project, source="hermes-project" if project else "unscoped")
    if project is None:
        scope.write_prefixes = [f"{GLOBAL}/"]
    else:
        scope.write_prefixes = [f"{project}/", f"{GLOBAL}/"]
    scope.other_buckets = [b for b in (buckets or []) if b != project]

    if config.readonly:
        scope.read_only, scope.read_only_reason = True, "readonly is set in config"
    elif agent_context in READ_ONLY_CONTEXTS and not (agent_context == "cron" and config.cron_writes):
        scope.read_only, scope.read_only_reason = True, f"{agent_context} context"
    return scope
