"""The ``system_prompt_block`` snapshot.

Contents, in order: usage rules, ``global/profile.md`` and ``global/preferences.md``
in full, then, when the session has a project, the project index: generated
here from the Hermes project (name, description, folders) and the bucket's own
``profile.md`` and ``preferences.md`` in full. No file holds it. Then a path +
description listing of the other files the session reads by default (project
files first). Capped at ``snapshot_max_chars``. When over the cap, drop listing
lines first, then whole files in ``DROP_ORDER``. The global profile and the
project header are never dropped. The provider freezes the result per session
id for prefix caching.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional, Set

from . import frontmatter
from .config import Config
from .index import Index
from .projects import Project
from .scopes import Scope
from .store import GLOBAL, Store, StoreError, stem

PROFILE = f"{GLOBAL}/profile.md"
PREFERENCES = f"{GLOBAL}/preferences.md"
INBOX = f"{GLOBAL}/inbox.md"
SORT_SKILL: Optional[str] = (
    "memory-buckets:sort-inbox"  # None when Hermes can't serve it (pip installs)
)
INBOX_NOTE = (
    "### Inbox\n"
    f"{INBOX} holds memory waiting to be sorted. Sort it only when the user asks. "
    'To sort it, load skill_view("{skill}") and follow it step by step.'
)
INBOX_NOTE_NO_SKILL = (
    "### Inbox\n"
    f"{INBOX} holds memory waiting to be sorted. Sort it only when the user asks. "
    "To sort it, ask the user to paste the output of `hermes memory-buckets sort-prompt`, "
    "then follow it step by step."
)

# Prompt style: second person, one instruction per line, short sentences, no nested
# colons. It has to survive small models.
READING = """\
## Memory
Your memory is a set of Markdown files that lasts between conversations. Use the memory_* tools to read and write it.

### Reading
- Check memory before answering anything that may depend on an earlier conversation.
- Check memory before saying you don't know something about the user or their work.
- Pick files from the lists below by their description and read them with memory_read. If none fits, use memory_search."""

SAVING = """\
### Saving
Save a fact as soon as you learn it, without being asked, when it is:
- durable: still true and useful in later conversations;
- new: not saved yet;
- about the user, their preferences, people in their life, their activities, or their projects.
Do not save temporary plans or details of a one-off task.

- Add to an existing file before you create a new one.
- To add a fact, use memory_append. To change a fact, use memory_str_replace.
- To create a file, use memory_write with if_version "new" and a description that says when to read it.
- Write one short bullet per fact.
- Do not mention reading or saving memory to the user, unless a tool result tells you to."""

READ_ONLY = (
    "Memory is read-only in this session ({reason}). Read it; do not try to save."
)

PROJECT_ROUTES = """\
Project files:
- {p}/profile.md: what the project is (purpose, technology, status, where things are).
- {p}/preferences.md: how to work on the project (conventions, tools, things to do or avoid).
- {p}/people/<name>.md: a person's role in the project.
- {p}/areas/<name>.md: an ongoing part of the work.
- {p}/topics/<subject>.md: anything else durable (a decision, a component, a procedure)."""

_GLOBAL_ROUTES = """\
Global files:
- global/profile.md: who the user is (identity, background, circumstances, what they own and use).
- global/preferences.md: how the user wants you to work (tone, formatting, things to do or avoid).
- global/people/<name>.md: one person in the user's life.{people}
- global/areas/<name>.md: an ongoing activity with no end date (a job, a course, a hobby).
- global/topics/<subject>.md: any other durable subject."""
GLOBAL_ROUTES = _GLOBAL_ROUTES.format(people="")
GLOBAL_ROUTES_IN_PROJECT = _GLOBAL_ROUTES.format(
    people=" Their role in a project goes in that project's people/ file."
)

INBOX_BULLET = f"- When you are not sure where a fact goes, append it to {INBOX}."

UNSCOPED = (
    """\
### Where facts go
This session is not in a project. You can write only under global/.
- When a fact is about a project, follow "Facts for another project".
- When a fact is partly about a project, save the general part here. Treat the project part as above.
"""
    + INBOX_BULLET
    + "\n\n"
    + GLOBAL_ROUTES
)

SHARED = (
    """\
### Where facts go
This session is in the project {p}. Save each fact once, in one place:
- When a fact only matters to {p}, save it under {p}/.
- When a fact is also true outside {p}, save it under global/ only. This session reads global/ too, so do not copy it.
- When a fact is partly both, split it. "Prefers uv for Python; this project pins 3.10" becomes "prefers uv for Python" in global/preferences.md and "pins Python 3.10" in {p}/preferences.md.
- When a fact is about another project, follow "Facts for another project".
"""
    + INBOX_BULLET
    + "\n\n"
    + PROJECT_ROUTES
    + "\n\n"
    + GLOBAL_ROUTES_IN_PROJECT
)

CONFINED = (
    """\
### Where facts go
This session is in the project {p}. You can write only under {p}/, and append to """
    + INBOX
    + """.
- When a fact only matters to {p}, save it under {p}/.
- When a fact is also true outside {p}, do not save it. Tell the user in one sentence that a session outside the project can.
- When a fact is partly both, save the project part. Treat the general part as above.
- When a fact is about another project, follow "Facts for another project".
"""
    + INBOX_BULLET
    + "\n\n"
    + PROJECT_ROUTES
)

ELSEWHERE = (
    """\
### Facts for another project
1. Append the fact to """
    + INBOX
    + """ as "<project>: <fact>".
2. Call memory_propose for that project. Copy the inbox line exactly into inbox_lines. In summary, say what the fact is and why it belongs there.
3. Do what the result tells you.
Projects: {projects}."""
)


@dataclass
class Snapshot:
    text: str
    paths: Set[str] = field(default_factory=set)  # files inlined in full


def rules(scope: Scope, config: Config, projects: List[str]) -> str:
    """The fixed instructions. ``projects`` are the buckets this session can propose into."""
    if scope.read_only:
        return READING + "\n\n" + READ_ONLY.format(reason=scope.read_only_reason)
    if not scope.project:
        where = UNSCOPED
    elif config.write_policy == "confined":
        where = CONFINED.format(p=scope.project)
    else:
        where = SHARED.format(p=scope.project)
    elsewhere = ELSEWHERE.format(
        projects=", ".join(projects) if projects else "none yet"
    )
    return "\n\n".join([READING, SAVING, where, elsewhere])


def _body(store: Store, path: str) -> Optional[str]:
    try:
        text = store.read(path)["content"]
    except StoreError:
        return None
    try:
        _, body = frontmatter.parse(text)
    except frontmatter.FrontmatterError:
        body = text
    body = body.strip()
    return body or None


def _profile_state(store: Store, path: str) -> str:
    """missing | broken | empty | full: what the tools can do with a profile.
    ``broken`` means the write path's own validation would refuse the file — no
    frontmatter, frontmatter that won't parse, or a path that isn't a regular
    file — so only the user can fix that."""
    try:
        data = store.read_bytes(path)
    except StoreError:
        return "broken"
    if data is None:
        return "missing"
    try:
        fm, body = frontmatter.parse(data.decode("utf-8", errors="replace"))
        frontmatter.validate(fm, stem(path))
    except frontmatter.FrontmatterError:
        return "broken"
    return "empty" if not body.strip() else "full"


def _home_relative(path: str) -> str:
    home = os.path.expanduser("~").rstrip(os.sep)
    if home and (path == home or path.startswith(home + os.sep)):
        return "~" + path[len(home) :]
    return path


def project_header(
    bucket: str,
    project: Optional[Project],
    *,
    profile: str,  # _profile_state: what the tools can do with <bucket>/profile.md
    has_files: bool,
    read_only: bool,
) -> str:
    """The generated head of the project index: what Hermes knows about the project,
    plus a line about its profile — a nudge when there's nothing to read yet, or a
    "tell the user" line when no memory tool can edit the file."""
    lines = [f"### Project: {bucket}"]
    if project is not None:
        if project.name and project.name != bucket:
            lines.append(f"Hermes project name: {project.name}")
        if project.description:
            lines.append(f"Hermes project description: {project.description}")
        if project.folders:
            shown = [_home_relative(f) for f in project.folders]
            if len(shown) > 1:
                shown[0] += " (primary)"
            lines.append("Folders: " + ", ".join(shown))
    if profile == "broken":
        # The file exists, but memory_write would conflict and memory_append and
        # memory_str_replace refuse ("fix it by hand"): an instruction only the
        # user can carry out, so the agent must pass it on, not try anything.
        lines.append(
            f"{bucket}/profile.md exists, but its frontmatter is missing or can't be parsed: no memory "
            "tool can edit it. Tell the user in one line to fix it (hermes memory-buckets lint names the problem)."
        )
    elif profile == "missing":
        missing = (
            "This project has no memory yet."
            if not has_files
            else f"{bucket}/profile.md does not exist yet."
        )
        if not read_only:
            missing += (
                f" When you learn what the project is, create {bucket}/profile.md with memory_write: "
                "its purpose, technology, status, and where things are."
            )
        lines.append(missing)
    elif profile == "empty" and not read_only:
        lines.append(
            f"{bucket}/profile.md is empty. When you learn what the project is, add to it with "
            "memory_append: its purpose, technology, status, and where things are."
        )
    return "\n".join(lines)


def drop_order(project: Optional[str]) -> List[str]:
    """Whole files to drop, first to last, when the listing alone can't bring the
    block under the cap. The global profile is never dropped."""
    if not project:
        return [PREFERENCES]
    # TODO(ivy): decide the order. In a project session, which matters more: the
    # global preferences or the project's own files?
    return [f"{project}/preferences.md", PREFERENCES, f"{project}/profile.md"]


def build(
    store: Store,
    index: Index,
    scope: Scope,
    config: Config,
    project: Optional[Project] = None,
    hermes_buckets: Optional[List[str]] = None,
) -> Snapshot:
    """``project`` is the Hermes project behind ``scope.project``, for the index header;
    without it the header shows only the bucket name. ``hermes_buckets`` are the buckets of
    every Hermes project, so the agent can propose into projects with no memory yet."""
    index.reconcile()
    listing_files = [(p, d) for p, _, d, _ in index.files()]
    cap = max(1000, config.snapshot_max_chars)

    others = sorted(
        {p.split("/", 1)[0] for p, _ in listing_files if not scope.reads_by_default(p)}
    )
    proposable = sorted(
        (set(others) | set(hermes_buckets or [])) - {GLOBAL, scope.project}
    )
    head = rules(scope, config, proposable)
    profile = _body(store, PROFILE)

    # Files inlined in full, in render order. The project header sits between the
    # global and the project files and is never dropped.
    files = {PREFERENCES: _body(store, PREFERENCES)}
    header = None
    if scope.project:
        p = scope.project
        files[f"{p}/profile.md"] = _body(store, f"{p}/profile.md")
        files[f"{p}/preferences.md"] = _body(store, f"{p}/preferences.md")
        header = project_header(
            p,
            project,
            profile=_profile_state(store, f"{p}/profile.md"),
            has_files=any(path.startswith(f"{p}/") for path, _ in listing_files),
            read_only=scope.read_only,
        )
    files = {path: body for path, body in files.items() if body}

    inlined = set(files) | ({PROFILE} if profile else set())
    defaults = [
        (p, d)
        for p, d in listing_files
        if scope.reads_by_default(p) and p not in inlined
    ]
    if scope.project:
        defaults.sort(key=lambda pd: (not pd[0].startswith(f"{scope.project}/"), pd[0]))
    listing = [f"- {p}: {d}" if d else f"- {p}" for p, d in defaults]
    has_inbox = any(p == INBOX for p, _ in listing_files)

    def render(shown, kept, dropped):
        parts = [head]
        if profile:
            parts.append(f"### {PROFILE}\n{profile}")
        if PREFERENCES in shown:
            parts.append(f"### {PREFERENCES}\n{shown[PREFERENCES]}")
        if header:
            parts.append(header)
        parts.extend(
            f"### {path}\n{body}" for path, body in shown.items() if path != PREFERENCES
        )
        if dropped:
            parts.append(
                "These files are too long to show here. Read them with memory_read when they are relevant: "
                + ", ".join(dropped)
            )
        omitted = len(listing) - kept
        if kept or omitted:
            lines = ["### Other files", *listing[:kept]]
            if omitted:
                lines.append(
                    f"- … and {omitted} more files. Call memory_list to see all of them."
                )
            parts.append("\n".join(lines))
        if others:
            parts.append(
                ("Other projects" if scope.project else "Projects")
                + " with memory: "
                + ", ".join(others)
                + ". Read their files only when the user asks about that project. You cannot write them directly, you must propose edits to them."
            )
        if has_inbox and not scope.read_only:
            parts.append(
                INBOX_NOTE.format(skill=SORT_SKILL)
                if SORT_SKILL
                else INBOX_NOTE_NO_SKILL
            )
        return "\n\n".join(parts)

    def fit(shown, dropped):
        """The most listing lines that fit under the cap (binary search: the length
        grows with every line kept), and the text with them."""
        lo, hi = 0, len(listing)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if len(render(shown, mid, dropped)) <= cap:
                lo = mid
            else:
                hi = mid - 1
        return render(shown, lo, dropped)

    shown, dropped = dict(files), []
    text = fit(shown, dropped)
    for path in drop_order(
        scope.project
    ):  # the listing alone didn't fit: drop whole files
        if len(text) <= cap:
            break
        if path in shown:
            del shown[path]
            dropped.append(path)
            text = fit(shown, dropped)
    return Snapshot(text=text, paths=set(shown) | ({PROFILE} if profile else set()))
