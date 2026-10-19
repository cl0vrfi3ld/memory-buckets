---
name: tidy-memory
description: Tidy the memory files. Find duplicate, contradictory and stale facts, then propose fixes. Ask the user which fixes to apply before changing anything.
---
# Tidy the memory

This skill finds problems in your memory files and proposes fixes. It asks before it changes anything. You change a file only after the user has told you which fixes to apply.

Run it only when the user asks. Do not start it on your own.

## Goal

Memory only grows. Over time the same fact ends up in two files. Some lines stop being true. Some files get too long. Some descriptions no longer say when to read the file.

Find these problems. Propose one fix for each. Apply only the fixes the user approves.

## Rules

- Reading is always fine. Writing needs approval. Do not call memory_write, memory_str_replace, memory_append or memory_delete until the user has answered your proposal.
- Leave global/inbox.md alone, even if it holds duplicates.
- Work only on global/ and the current project's files. These are the files memory_list shows by default.
- Do not edit another project's files, even if you can read them.
- Do not add a fact that the files do not already hold. You may only remove, reword, move or split.
- When two facts conflict, do not pick one. Ask the user.
- When you are not sure a line is stale, still propose it, and say what you are unsure about. The user decides.
- Use today's date only if you know it. Do not guess it.

## What to look for

Look for six kinds of problem.

1. **Duplicate.** The same fact is in two files, or twice in one file. A fact worded differently still counts.
   - Keep it in the one best place. A fact that is true outside a project belongs under global/.
   - Propose removing the other copy.
   - Example. "Prefers uv for Python" is in global/profile.md and in global/preferences.md. Keep it in preferences.md and remove it from profile.md.

2. **Conflict.** Two facts cannot both be true.
   - Do not decide which one is right.
   - Propose asking the user, and name both lines and both files.
   - Say what you would do for each answer. If a line also holds a fact that is not in dispute, say you would reword it, not remove it.
   - Example. One file says "Lives in Bristol". Another says "Lives in Leeds".

3. **Stale wording.** A line uses time words that were only true when it was written. Look for "currently", "this semester", "this year", "next week", "right now", "at the moment" and "soon".
   - memory_read may show an edited_at date in the file's frontmatter. It is the date of the last edit to the whole file, not to one line. Use it as a hint only.
   - If the fact is probably still true, propose the same line without the time word. "Currently learning OCaml" becomes "Learning OCaml".
   - If the line is about a time that has probably passed, propose removing it. Example. "Dentist next week".

4. **Finished one-off task.** A line about an errand or task that is done, or a plan with a date that has gone.
   - Propose removing it. Example. "Needs to renew passport by March".
   - If you cannot tell whether it is done, still propose removing it. Say what you are unsure about.

5. **File too big.** memory_list shows each file's size in bytes. A file of 8000 bytes or more is too big.
   - Propose splitting it by subject into two or more new files.
   - Name each new file and say which headings or lines go into it. Do not list every line.
   - If every line moves, also propose deleting the old file.
   - Example. global/topics/work.md is 11000 bytes. Split it into global/topics/work-deploys.md and global/topics/work-people.md.

6. **Vague description.** The description does not say when to read the file. Examples are "Notes", "Stuff about work" and an empty description.
   - Propose a new one-line description that starts with "Read when".
   - Do not propose a new description for a profile.md or preferences.md file. The prompt always shows them in full.
   - Example. "Notes" becomes "Read when working on the home server's backups".

If a problem fits two kinds, use the first kind in this list.

## Steps

Follow these steps in order.

1. **Read.** Call memory_list. Do not set include_projects.
   - If the user named a folder or a file, only look at that. Example. "Tidy my global notes" means the prefix global/.
   - If the user named another project's files, tell them you cannot change those. Stop.
   - Skip global/inbox.md.
   - Call memory_read on the files in scope, up to 20 paths per call.

2. **Find.** Go through every file you read. Note each problem and which of the six kinds it is.
   - Copy the exact line. You will need it again.
   - For a duplicate, check that the kept copy really says the same thing.
   - If you find nothing, tell the user the memory looks tidy. Stop here. Change nothing.

3. **Propose.** Reply with one numbered list. Do not call a writing tool in this turn.
   - Group the items by file. Put the file path above each group.
   - Put an item that involves two files under the first file it names.
   - If an item adds a line to another file, say so in the item.
   - Number the items 1, 2, 3 across the whole list, not again in each group, so the user can say "apply 2 and 5".
   - Write each item as one or two short lines. Say the kind, quote the line, and say what you would do. A split may be longer.
   - Put conflicts in the list too. Write them as a question the user can answer by number.
   - Show at most 15 items. If there are more, say so, and offer a second round.
   - End with this question. "Which of these should I apply? Say all, none, or the numbers."
   - Then stop and wait.

   Example of two items.

   ```
   global/profile.md
   1. Duplicate. "- Prefers uv for Python" is also in global/preferences.md. Remove it from here.
   2. Stale. "- Currently learning OCaml". Reword to "- Learning OCaml".
   ```

4. **Wait for the answer.** Apply only what the user approved.
   - "All" means every item except the conflicts. A conflict needs a choice.
   - If the user picked a side for a conflict, the lines on the other side become ordinary items. Remove a line that holds only the losing fact. Reword a line that also holds another fact, and keep that fact. Example. "Lives in Leeds" wins, so "Name is Sam Carter; lives in Bristol" becomes "Name is Sam Carter."
   - If the user did not pick a side, leave the conflict alone. Do not ask again now. Report it in your final reply.
   - If an item only makes sense after an unanswered conflict, leave it alone too.
   - "None" means stop. Change nothing.
   - If the answer is unclear, ask again. Do not guess.
   - If the user changes an item, do what they said.

5. **Apply.** Do the approved items one at a time.
   - Before you edit a file, call memory_read on it. Use the version you just read as if_version.
   - **Remove a line.** Call memory_str_replace. Set old to the whole bullet line, with no newline. Set new to an empty string.
   - If the tool says ambiguous_match, the line is in the file twice. Pick a copy that has a line below it. Put that copy and the line below it in old. Put only the line below it in new. Then old occurs once.
   - **Reword a line.** Call memory_str_replace. Set old to the whole old line. Set new to the whole new line, starting with "- ".
   - If the tool says ambiguous_match, the old line may sit inside a longer line. Example. "- Likes tea" is the start of "- Likes tea with oat milk." Add a newline to the end of old and to the end of new. Then old only matches the whole line.
   - If it still says ambiguous_match, the line is in the file twice. Rewrite the file instead.
   - **Keep detail.** If the copy you remove has detail the kept copy lacks, call memory_append on the kept file first. Then remove the copy.
   - **Change a description.** Call memory_write with the same path and the new description. Set body to the text after the closing "---" of the frontmatter, unchanged. Set if_version to the version you just read. Do not pass aliases.
   - **Split a file.** First create each new file with memory_write and if_version "new". Give it a description that says when to read it. Only after that, remove the moved lines from the old file. Never remove a line before it is saved in its new file.
   - **Rewrite the file.** Use this when many lines change, or when one line will not match once. Use memory_write instead of memory_str_replace. Pass the file's current description. Set body to the text after the closing "---", with your changes made. Set if_version to the version you just read. Do not pass aliases.
   - **Delete a file.** Only when the user approved it and every line is already saved elsewhere. Call memory_delete with the version you just read.

6. **Handle errors.** Read the error code.
   - conflict. The file changed since you read it. Read the current content from the error. Redo the edit on that text. If the line is gone, skip the item and report it.
   - no_match. Call memory_read and copy the line exactly. Try once more. If it fails again, skip the item and report it.
   - ambiguous_match. Use the fix under Remove a line or Reword a line. If it still fails, rewrite the file. If that fails too, skip the item and report it.
   - out_of_scope or read_only. Skip the item and report it. Do not try another path or another tool to get round it.

7. **Check.** Call memory_read on each file you changed. Check that the approved lines are gone or reworded, and that nothing else changed.

## Final reply

When you have finished, reply to the user with these sections.

1. **Changed:** each file you changed, and what you did to it.
2. **Left alone:** the items the user did not approve, and anything you saw but did not touch. Name global/inbox.md here if it had problems.
3. **Check these:** items you skipped because of an error, conflicts still waiting for an answer, and lines you were not sure about. Give a one-line reason for each.
