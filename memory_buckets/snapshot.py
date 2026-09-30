"""The ``system_prompt_block`` snapshot.

Contents, in order: usage rules, ``global/profile.md`` and ``global/preferences.md``
in full, the project's ``index.md`` when the session has a project, then a path
+ description listing of the other files the session reads by default (project
files first). Capped at ``snapshot_max_chars``. When over the cap, drop listing
lines first, then the project index, then preferences. The profile is never
dropped. The provider freezes the result per session id for prefix caching.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Set

from . import frontmatter
from .config import Config
from .index import Index
from .scopes import Scope
from .store import GLOBAL, Store, StoreError

PROFILE = f"{GLOBAL}/profile.md"
PREFERENCES = f"{GLOBAL}/preferences.md"
INBOX = f"{GLOBAL}/inbox.md"
SORT_SKILL: Optional[str] = "memory-buckets:sort-inbox"  # None when Hermes can't serve it (pip installs)
INBOX_NOTE = (
    "### Inbox\n"
    f"{INBOX} contains memory that has not been sorted yet. Sort it only when the user asks you to. "
    "To sort it, first load the instructions with skill_view(\"{skill}\"), then follow them step by step.")
INBOX_NOTE_NO_SKILL = (
    "### Inbox\n"
    f"{INBOX} contains memory that has not been sorted yet. Sort it only when the user asks you to. "
    "To sort it, you need the sorting instructions: ask the user to paste the output of "
    "`hermes memory-buckets sort-prompt`, then follow those instructions step by step.")

RULES = """\
## Memory
You have a persistent memory: a set of Markdown files that you read and write with the memory_* tools. Use it to remember facts about the user and their work from one conversation to the next.

### How memory is organised
- Each file covers one subject. Each file has a one-line description that says when to read it.
- Files are grouped into scopes. The first part of a path is its scope:
  - global/ holds general facts: who the user is, how they want you to work, the people in their life, and anything else that is true outside a single project.
  - <project>/ (for example home-server/) holds facts that only matter for that one project.
- Paths are lower-case, use hyphens between words, and end in .md. Example: global/topics/coffee.md.

### When to read memory
- Before you answer a question that could depend on an earlier conversation, check memory.
- Before you tell the user you don't know something about them or their work, check memory.
- How to check: find files in the list below whose descriptions match, and read them with memory_read. If no description matches, use memory_search.

### When to save a fact
Save a fact when all three are true:
1. It is durable: it will still be true and useful in future conversations.
2. It is not already saved.
3. It is about the user, their preferences, the people in their life, their ongoing activities, or their projects.
Do not save temporary plans, details of a one-off task, or anything that will soon be out of date.
Save facts as soon as you learn them. You do not need to be asked.

### How to save a fact
- Before you create a file, check the list below for a file on the same subject. If one exists, add to it instead of creating a new one.
- To add facts to an existing file, use memory_append.
- To correct a fact, use memory_str_replace.
- To create a file, use memory_write with if_version "new", and a description that says when to read the file.
- Write facts as short bullet points, one fact per line.
- If a write fails with a conflict, the result contains the file's current content and version. Merge your change into that content and retry with that version.
- Do not tell the user that you are reading or saving memory.

{scope}"""

GLOBAL_ROUTES = """\
- global/profile.md: who the user is. Their identity, background, circumstances, and what they own and use.
- global/preferences.md: how the user wants you to work. Tone, formatting, conventions, and things to do or avoid.
- global/people/<name>.md: one person in the user's life. One file per person. Example: global/people/sam.md.
- global/areas/<name>.md: an ongoing activity with no end date, such as a job, a course of study, or a hobby.
- global/topics/<subject>.md: any other durable subject. One subject per file."""

UNSCOPED = """\
### Where to save in this session
This session is not in a project. You can write files only under global/.

Choose the file by what the fact is about:
""" + GLOBAL_ROUTES + """

Facts that belong to one project:
- You cannot write project files in this session.
- If you are sorting global/inbox.md, follow the sort-inbox instructions: stage project facts with memory_propose. This works for existing projects and for new projects. memory_propose does not write any files, so it is allowed in this session. The user applies each proposal.
- Otherwise, save the fact to global/inbox.md with memory_append, written as "<project>: <fact>". If global/inbox.md does not exist, create it with memory_write and if_version "new".

Project files are not in the list below. When the user asks about a project, call memory_list with include_projects set to true, then read the files you need with memory_read."""

PROJECT = """\
### Where to save in this session
This session is in the project "{p}". For each fact, decide where it belongs:
1. The fact would still be true and useful outside {p}: it is a general fact. Save it under global/.
2. The fact only matters for {p}: it is a project fact. Save it under {p}/.
3. The fact has a general part and a project part: split it. Save each part in its own place.

Project facts, under {p}/:
- {p}/profile.md: what the project is. Its purpose, technology, status, and where things are.
- {p}/preferences.md: how the user wants you to work on this project. Its conventions, tools, and things to do or avoid.
- {p}/people/<name>.md: a person's role in this project.
- {p}/areas/<name>.md: an ongoing part of the work within this project.
- {p}/topics/<subject>.md: any other durable project subject, such as a decision, a component, or a procedure. One subject per file.
- Do not edit {p}/index.md. It is maintained automatically.

{general}

If you are sorting global/inbox.md, do not save project facts directly, not even facts for {p}. Follow the sort-inbox instructions and stage them with memory_propose.

Other projects' files are read-only in this session and are not in the list below. Read them with memory_read only when the user asks about that project."""

PROJECT_GENERAL_SHARED = """\
General facts, under global/ (the same files as in every session):
""" + GLOBAL_ROUTES + """
- If a person already has a file under global/people/, keep their general facts there. Put only their role in {p} under {p}/people/."""

PROJECT_GENERAL_CONFINED = """\
General facts:
- In this session you can write only under {p}/. You cannot write under global/.
- If you learn a general fact, do not save it. Tell the user in one sentence that it can be saved from a conversation outside the project."""


@dataclass
class Snapshot:
    text: str
    paths: Set[str] = field(default_factory=set)  # files inlined in full


def _scope_sentence(scope: Scope, config: Config) -> str:
    if scope.read_only:
        return f"Memory is read-only in this session ({scope.read_only_reason})."
    if not scope.project:
        return UNSCOPED
    confined = config.write_policy == "confined"
    general = (PROJECT_GENERAL_CONFINED if confined else PROJECT_GENERAL_SHARED).format(p=scope.project)
    return PROJECT.format(p=scope.project, general=general)


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


def build(store: Store, index: Index, scope: Scope, config: Config) -> Snapshot:
    index.reconcile()
    listing_files = [(p, d) for p, _, d, _ in index.files()]
    cap = max(1000, config.snapshot_max_chars)

    head = RULES.format(scope=_scope_sentence(scope, config))
    profile = _body(store, PROFILE)
    preferences = _body(store, PREFERENCES)
    project_index = _body(store, f"{scope.project}/index.md") if scope.project else None

    sections = []  # (path, text) in priority order after the profile
    if preferences:
        sections.append((PREFERENCES, f"### {PREFERENCES}\n{preferences}"))
    if project_index:
        sections.append((f"{scope.project}/index.md", f"### {scope.project}/index.md\n{project_index}"))

    inlined = {PROFILE} if profile else set()
    inlined |= {p for p, _ in sections}
    defaults = [(p, d) for p, d in listing_files if scope.reads_by_default(p) and p not in inlined]
    if scope.project:
        defaults.sort(key=lambda pd: (not pd[0].startswith(f"{scope.project}/"), pd[0]))
    listing = [f"- {p}: {d}" if d else f"- {p}" for p, d in defaults]
    has_inbox = any(p == INBOX for p, _ in listing_files)
    others = sorted({p.split("/", 1)[0] for p, _ in listing_files if not scope.reads_by_default(p)})

    def render(sections, listing, omitted, dropped=()):
        parts = [head]
        if profile:
            parts.append(f"### {PROFILE}\n{profile}")
        parts.extend(text for _, text in sections)
        if dropped:
            parts.append("These files are too long to show here. Read them with memory_read when they are relevant: "
                         + ", ".join(dropped))
        if listing or omitted:
            lines = ["### Other memory files", "Read a file when its description matches what you need.", *listing]
            if omitted:
                lines.append(f"- … and {omitted} more files. Call memory_list to see all of them.")
            parts.append("\n".join(lines))
        if others:
            parts.append("Other projects that have memory: " + ", ".join(others)
                         + ". Their files are not listed here. Read them only when the user asks about that project.")
        if has_inbox and not scope.read_only:
            parts.append(INBOX_NOTE.format(skill=SORT_SKILL) if SORT_SKILL else INBOX_NOTE_NO_SKILL)
        return "\n\n".join(parts)

    text = render(sections, listing, 0)
    kept = len(listing)
    while len(text) > cap and kept > 0:  # 1. trim the listing
        kept = max(0, kept - max(1, (len(text) - cap) // 40))
        text = render(sections, listing[:kept], len(listing) - kept)
    dropped: List[str] = []
    while len(text) > cap and sections:  # 2. project index, then preferences
        dropped.insert(0, sections.pop()[0])
        text = render(sections, [], len(listing), dropped)
    inlined = {p for p, _ in sections} | ({PROFILE} if profile else set())
    return Snapshot(text=text, paths=inlined)
