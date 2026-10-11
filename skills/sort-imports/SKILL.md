---
name: sort-imports
description: Sort the imported memory in global/inbox.md. File each fact where it belongs (global/ or a project bucket). Then remove its line from the inbox.
---
# Sort the imported memory

## Goal

global/inbox.md holds imported memory that nobody has sorted yet.

It can contain:
- entries imported from the old built-in memory (under USER.md and MEMORY.md headings)
- entries under an "added in conversation" heading (left by an older version of this plugin)

Entries under a USER.md heading are about the user.
Entries under a MEMORY.md heading are notes you wrote earlier.
An entry written as "<project>: <fact>" is a project fact for that project.

Each bullet point is one entry.
Move every entry into the right memory file. Drop an entry only if it is not worth keeping.

## The two kinds of entry

Decide which kind each entry is before you do anything with it.

- **General fact** (still true outside any single project)
  Examples: the user's name, their preferred tone, a friend's birthday, a hobby.
  Save it under global/.
- **Project fact** (only matters for one project)
  Examples: how an app is deployed, the stack a repository uses. Also a decision made for one project.
  Save it in that project's bucket (<project>/).

If an entry has a general part and a project part, split it.
Save each part where it belongs.

## Rules for project facts

- Known projects are the ones in memory_list. Your memory instructions also list known projects under "Facts for another project". That list counts even with no files yet.
- To add to an existing file, use memory_append.
- To create a file, use memory_write with if_version "new" and a description.
- Add facts only. Do not edit or delete files in a project's bucket during this task.
- This holds even when the session is in that project.
- When the project is not known, use global/topics/<name>.md (name it after the project).

## Steps

Follow these steps in order.

1. **Read.** Call memory_read with ["global/inbox.md"]. Then call memory_list with include_projects set to true. Note which files exist. A project id is the first part of a path (home-server in home-server/profile.md).

2. **Label each entry.** Choose one of:
   - general fact
   - project fact (and which project)
   - drop (stale, already saved, or not worth keeping)

   If you cannot place an entry, treat it as a general fact. It goes under global/.

3. **Save the general facts.** Choose the file for each fact:
   - who the user is: global/profile.md
   - how the user wants you to work: global/preferences.md
   - a person in the user's life: global/people/<name>.md (one file per person)
   - an ongoing activity with no end date (a job, a course, a hobby): global/areas/<name>.md
   - any other durable subject: global/topics/<subject>.md (one subject per file)

   Then:
   - If the file exists, read it first.
   - Add only facts it does not already have, with memory_append.
   - Correct outdated facts in global/ files with memory_str_replace.
   - If the file does not exist, create it with memory_write (if_version "new").
   - Give the file a description that says when to read it.
   - Write each fact as a short bullet point, one fact per line.

4. **Save the project facts.** Group them by project. Then, for each project:
   - If it is a known project (see the rules above), read its files first. Add only facts they do not already have.
   - If it is not a known project, save the facts in global/topics/<name>.md instead.
   - Choose the file for each fact in a project:
     - what the project is (purpose, technology, status, where things are): <project>/profile.md
     - how the user wants you to work on it: <project>/preferences.md
     - a person's role in the project: <project>/people/<name>.md
     - an ongoing part of the work: <project>/areas/<name>.md
     - anything else durable (a decision, a component, a procedure): <project>/topics/<subject>.md

5. **Remove each filed line from the inbox.** Do this straight after you save it.
   - Call memory_str_replace on global/inbox.md.
   - Set old to the bullet line, exactly as it appears.
   - Set new to an empty string.
   - Leave the headings in place.

6. **Drop entries.** Remove entries labelled "drop" the same way. Drop an entry only if it is stale, already saved, or clearly not useful.

7. **Check for conflicts.** If two entries contradict each other, save both under global/. Say what conflicts in your reply. Entries have no dates, so do not guess which one is newer.

8. **Leave the inbox file alone.** Only remove single lines with memory_str_replace. Never use memory_write or memory_delete on it. Never use memory_append on it (the inbox only holds imported memory). The inbox stays when it is empty, because the next import adds to it.

## Final reply

When you have finished, reply to the user with these sections:

1. **Saved:** the files you created or changed, global/ and project buckets.
2. **Dropped:** the entries you removed, and why.
3. **Check these:** entries you saved under global/ because you could not place them, each with a one-line reason. Include any conflicts.
