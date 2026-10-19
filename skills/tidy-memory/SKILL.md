---
name: tidy-memory
description: Tidy the memory files. Find duplicate, conflicting and stale facts, then propose fixes. Ask the user which fixes to apply before changing anything.
---
# Tidy the memory

## Ask first

You ask before you change anything.
First you read. Then you propose. Then you stop and wait.
You change a file only after the user has answered your proposal.

Until then, do not call memory_write, memory_str_replace, memory_append or memory_delete.
This holds even if the user says "just fix it" in the same message. Propose first. Their answer to your list is the approval.
"Tidy my memory" is the request to start. It is not an approval.

Run this skill only when the user asks for it.

## Goal

Memory only grows. The same fact ends up in two files. Some lines stop being true. Some files get too long. Some descriptions no longer say when to read the file.

Find these problems. Propose one fix for each. Apply only the fixes the user approves.

## Rules

- Reading is always fine. Writing needs the user's answer to your proposal.
- Work only on global/ and the current project's files. memory_list shows these by default.
- Never touch global/inbox.md. Do not read it, change it or propose changes to it. /sort-imports handles it.
- Never touch another project's files. Do not edit them. Do not add a line to them. Do not create files in them. The tools may allow it. You must not.
- Never add a fact the files do not already hold. You may remove, reword, move or split.
- When two facts conflict, do not pick one. Ask the user.
- When you are not sure about a line, still propose it. Say what you are unsure about.
- Do not guess dates. A line has no date. The edited_at value in a file's frontmatter is for the whole file, so use it as a hint only.

## What to look for

Look for six kinds of problem. If a problem fits two kinds, use the earlier one.

1. **Duplicate.** The same fact is in two files, or twice in one file. A different wording still counts.
   - Keep it in the best place. A fact that is true outside any project belongs in global/.
   - Propose removing the other copy.
   - Example: "Prefers uv for Python" is in global/profile.md and in global/preferences.md. Keep the one in preferences.md.

2. **Conflict.** Two facts cannot both be true.
   - Do not decide which one is right.
   - Name both lines and both files. Ask the user which is right.
   - Example: one file says "Lives in Bristol". Another says "Lives in Leeds".

3. **Stale wording.** The line uses a time word that was only true when it was written. Look for "currently", "this semester", "this year", "next week", "right now", "at the moment" and "soon".
   - If the fact is probably still true, propose the same line without the time word. "Currently learning OCaml" becomes "Learning OCaml".
   - If the line is about a time that has probably passed, propose removing it. Example: "Dentist next week".

4. **Finished one-off task.** The line is about an errand that is done, or a plan with a date that has gone.
   - Propose removing it. Example: "Needs to renew passport by March".
   - If you cannot tell whether it is done, still propose it. Say what you are unsure about.

5. **File too big.** memory_list shows each file's size in bytes. A file of 8000 bytes or more is too big.
   - Propose splitting it by subject into two or more files.
   - Name each new file. Say which lines or headings go into it. Do not list every line.

6. **Vague description.** The description does not say when to read the file. Examples: "Notes", "Stuff about work", or no description.
   - Propose a new one-line description that starts with "Read when".
   - Skip profile.md and preferences.md files. The prompt always shows them in full.

## Steps

Follow these steps in order.

1. **Read.** Call memory_list. Do not set include_projects.
   - If the user named a folder or a file, look only at that.
   - If the user named another project, tell them you cannot change those files. Stop.
   - Leave out global/inbox.md.
   - Call memory_read on the files in scope. It takes up to 20 paths per call.

2. **Find.** Go through every file you read.
   - Note each problem and its kind.
   - Copy each line exactly. You need the exact text later.
   - If you find nothing, tell the user the memory looks tidy. Stop. Change nothing.

3. **Propose.** Reply with one numbered list. Do not call a writing tool in this turn.
   - Group the items by file. Put the file path above each group.
   - Number the items 1, 2, 3 across the whole list, so the user can say "apply 2 and 5".
   - Write each item as one or two short lines. Give the kind, quote the line, and say what you would do.
   - Put an item that involves two files under the first file it names.
   - If an item adds a line to another file, say so in the item.
   - Write a conflict as a question the user can answer by number.
   - Show at most 15 items. If there are more, say so and offer a second round.
   - End with this question: "Which of these should I apply? Say all, none, or the numbers."
   - Then stop. Do not call any tool. Wait for the user.

   Example of two items:

       global/profile.md
       1. Duplicate. "- Prefers uv for Python" is also in global/preferences.md. Remove it from here.
       2. Stale wording. "- Currently learning OCaml". Reword to "- Learning OCaml".

4. **Read the answer.** Apply only what the user approved.
   - "None" means stop. Change nothing.
   - "All" means every item except the conflicts. A conflict needs a choice.
   - A numbered answer means those items only.
   - If the user changes an item, do what they said.
   - If the answer is unclear, ask again. Do not guess.
   - If the user picks a side in a conflict, remove or reword the lines on the other side. Keep any fact in those lines that is not in dispute.
   - If the user does not pick a side, leave the conflict alone. Report it at the end.

5. **Apply.** Do the approved items one at a time.
   - Before you edit a file, call memory_read on it. Use that version as if_version.
   - **Remove a line.** Call memory_str_replace. Set old to the whole line, with no newline. Set new to an empty string.
   - **Reword a line.** Call memory_str_replace. Set old to the whole old line plus a newline. Set new to the whole new line plus a newline.
   - **Keep detail.** If the copy you remove holds detail the kept copy lacks, call memory_append on the kept file first. Then remove the copy.
   - **Change a description.** Call memory_write with the same path and the new description. Set body to the file's text after the closing "---" of the frontmatter, unchanged. Leave aliases out. The tool keeps the file's existing aliases and other frontmatter.
   - **Rewrite a file.** Use this when many lines change, or when memory_str_replace will not match. Call memory_write with the file's current description. Set body to the text after the closing "---", with your changes made. Leave aliases out. The tool keeps the file's existing aliases and other frontmatter.
   - **Split a file.** First create each new file with memory_write and if_version "new". Give each one a description that starts with "Read when". Only after that, remove the moved lines from the old file. Never remove a line before it is saved in its new file.
   - **Delete a file.** Only when the user approved it and every line is already saved elsewhere. Call memory_delete.

6. **Handle errors.** Read the error code.
   - conflict: the file changed since you read it. The error holds its current content. Redo the edit on that text. If the line is gone, skip the item and report it.
   - no_match: call memory_read and copy the line exactly. Try once more. If it fails again, skip the item and report it.
   - ambiguous_match: the text is in the file more than once. Rewrite the file instead. If that fails too, skip the item and report it.
   - out_of_scope or read_only: skip the item and report it. Do not try another path or another tool.
   - Any other error: skip the item and report it.

7. **Check.** Call memory_read on each file you changed. Check that the approved lines are gone or reworded, and that nothing else changed.

## Final reply

When you have finished, reply with these sections.

1. **Changed:** each file you changed, and what you did to it.
2. **Left alone:** the items the user did not approve.
3. **Check these:** items you skipped because of an error, conflicts still waiting for an answer, and lines you were not sure about. Give a one-line reason for each.
