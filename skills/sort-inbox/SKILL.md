---
name: sort-inbox
description: Sort global/inbox.md into memory files. General facts are saved directly; project facts, including facts for new projects, are staged with memory_propose for the user to apply one proposal at a time.
---
# Sort the memory inbox

## Goal

global/inbox.md holds memory that has not been sorted yet. It can contain:
- entries imported from the old built-in memory. Entries under a USER.md heading are about the user. Entries under a MEMORY.md heading are notes you wrote earlier.
- entries saved there later, often written as "<project>: <fact>".
- entries parked by an earlier sort, under a heading such as "## project facts (to sort later)", grouped under a sub-heading that names the project. For these, the heading tells you the project. They are project facts that still need sorting: stage them with memory_propose like any other project fact.

Each bullet point is one entry. Your job is to move every entry into the right memory file, or to drop it if it is not worth keeping.

## The two kinds of entry

Every entry is either a general fact or a project fact. Decide which one before you do anything with it.

- **General fact**: still true and useful outside any single project. Examples: the user's name, their preferred tone, a friend's birthday, a hobby.
  → Save it yourself, straight away, under global/.
- **Project fact**: only matters for one project. Examples: how a specific app is deployed, the stack a specific repository uses, a decision made for one project.
  → Do not save it yourself. Stage it with memory_propose. The user applies each proposal.

If an entry has a general part and a project part, split it. Save the general part yourself. Stage the project part with memory_propose.

## Rules for memory_propose

- memory_propose does not write any memory files. It is allowed in every session, including sessions where you can otherwise write only under global/.
- It works for projects that do not exist yet. Creating a new project is part of this task. Do not refuse it, and do not leave project facts in the inbox because their project does not exist.
- Never save project facts with memory_write, memory_append or memory_str_replace during this task, not even when the session is in that project. Always use memory_propose.
- You cannot apply a proposal. Only the user can, with /memory-apply <id>.

## Steps

Follow these steps in order.

1. **Read.** Call memory_read with ["global/inbox.md"]. Then call memory_list with include_projects set to true. Note which files exist and which project ids exist. A project id is the first part of a path, for example home-server in home-server/profile.md.

2. **Label each entry.** For every entry, decide one of:
   - general fact
   - project fact, and which project
   - drop: stale, already saved, or not worth keeping
   - unsure

3. **Save the general facts.** For each general fact, choose the file:
   - who the user is: global/profile.md
   - how the user wants you to work: global/preferences.md
   - a person in the user's life: global/people/<name>.md, one file per person
   - an ongoing activity with no end date (a job, a course of study, a hobby): global/areas/<name>.md
   - any other durable subject: global/topics/<subject>.md, one subject per file

   Then:
   - If the file exists, read it first. Add only facts it does not already have, with memory_append. Correct outdated facts with memory_str_replace.
   - If the file does not exist, create it with memory_write, if_version "new", and a description that says when to read the file.
   - Rewrite each fact as a short bullet point, one fact per line.
   - After saving an entry, remove its line from global/inbox.md with memory_str_replace (replace the line with an empty string).

4. **Stage the project facts.** Group the project facts by project. Then make one memory_propose call per project.
   - **Existing project**: if one of the project ids from step 1 fits, use it. Read its files first, and include only facts they do not already have.
   - **New project**: if no existing project fits, create one:
     - Choose a short id: lower-case, hyphens between words, for example "home-server".
     - Set new_project to true.
     - Include <project>/profile.md, with a description that says what the project is, and the facts that describe it.
   - Choose the file for each fact:
     - what the project is (purpose, technology, status, where things are): <project>/profile.md
     - how the user wants you to work on it: <project>/preferences.md
     - a person's role in the project: <project>/people/<name>.md
     - an ongoing part of the work: <project>/areas/<name>.md
     - anything else durable (a decision, a component, a procedure): <project>/topics/<subject>.md
     - never <project>/index.md
   - A file that does not exist yet needs a description.
   - inbox_lines: copy the entries this proposal covers exactly as they appear in global/inbox.md. Copy only the bullet lines, not the headings above them.
   - Do not remove those lines from the inbox yourself. They are removed when the user applies the proposal.
   - summary: one sentence saying what the proposal saves and why it belongs to this project.
   - If memory_propose returns an error, read the message, fix the call, and try again.

5. **Drop entries.** Remove entries labelled "drop" from global/inbox.md with memory_str_replace. Drop an entry only if it is stale, already saved, or clearly not useful.

6. **Leave the uncertain entries.** Do not change entries labelled "unsure". Leave them in global/inbox.md, and list them in your final reply with a one-line reason each. If two entries contradict each other, treat both as unsure and say what conflicts. Entries have no dates, so do not guess which one is newer.

7. **Do not rewrite or delete global/inbox.md.** Only remove single lines with memory_str_replace, as described above. Never use memory_write or memory_delete on it: that could break the pending proposals. The user reviews the inbox and deletes it.

## Final reply

When you have finished, reply to the user with these sections:

1. **Saved:** the global files you created or changed.
2. **Dropped:** the entries you removed, and why.
3. **Proposals:** for each proposal, its id, the project, whether the project is new, and the preview that memory_propose returned.
4. **Left in the inbox:** the entries labelled "unsure", each with its reason. (Lines covered by proposals also stay until the user applies them.)

End with these instructions for the user, word for word, with the real ids filled in:

> Project facts are not saved yet. For each proposal, run `/memory-apply <id>` to save it or `/memory-reject <id>` to discard it. `/memory-pending` lists the proposals that are still waiting.
