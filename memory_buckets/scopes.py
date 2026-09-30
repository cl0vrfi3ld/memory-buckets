"""Session scope: which prefixes a session reads and writes.

Resolved on every tool call, so moving the session into another Hermes project
mid-chat applies straight away. The project comes from Hermes's own Projects
(``projects.session_project``); a session outside every project is unscoped.

Reads: explicit paths anywhere are allowed ("project subtrees on request").
Default reads (listing, search, snapshot) are ``global/`` plus the session's
project. Writes: unscoped ⇒ ``global/``. Project-scoped ⇒ the project plus all
of ``global/`` (``write_policy: shared``; the prompt keeps project-specific facts
in the project), or only the project (``confined``). cron/subagent/flush contexts, or ``readonly``, write nothing.
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

    @property
    def read_prefixes(self) -> List[str]:
        return [f"{GLOBAL}/"] + ([f"{self.project}/"] if self.project else [])

    def reads_by_default(self, path: str) -> bool:
        return any(path.startswith(p) for p in self.read_prefixes)

    def can_write(self, path: str) -> bool:
        return any(path == p or (p.endswith("/") and path.startswith(p)) for p in self.write_prefixes)

    def check_write(self, path: str) -> None:
        if self.read_only:
            raise StoreError("read_only", f"memory is read-only in this session ({self.read_only_reason})")
        if not self.can_write(path):
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


def resolve(config: Config, project: Optional[str] = None, agent_context: str = "primary") -> Scope:
    """``project`` is the session's bucket (a Hermes project slug), or None."""
    if project is not None and not is_project_id(project):
        project = None
    scope = Scope(project=project, source="hermes-project" if project else "unscoped")
    if project is None:
        scope.write_prefixes = [f"{GLOBAL}/"]
    elif config.write_policy == "confined":
        scope.write_prefixes = [f"{project}/"]
    else:
        scope.write_prefixes = [f"{project}/", f"{GLOBAL}/"]

    if config.readonly:
        scope.read_only, scope.read_only_reason = True, "readonly is set in config"
    elif agent_context in READ_ONLY_CONTEXTS and not (agent_context == "cron" and config.cron_writes):
        scope.read_only, scope.read_only_reason = True, f"{agent_context} context"
    return scope
