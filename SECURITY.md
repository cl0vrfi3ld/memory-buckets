# Security

This page says what memory-buckets trusts, what it checks and what it does not.
For where your data goes and how to sync or back it up, see
[Privacy and data handling](README.md#privacy-and-data-handling) and
[Syncing and backups](README.md#syncing-and-backups).

## Reporting a vulnerability

Please do not put details in a public issue.

1. Open an issue at <https://git.cl0vr.co/cl0vr/memory-buckets/issues> that says only
   that you have a security report. Leave out the details and any exploit.
2. The maintainer will reply there and arrange a private way to send it.

## What the plugin trusts

The plugin hands text from the store to the model. The model treats it as instructions
or as facts. So the store is closer to a configuration file for your agent than to a
folder of notes you can paste anything into.

| Text | How the model gets it | Treated as |
|---|---|---|
| Body of `profile.md` and `preferences.md` (in `global/` and in the session's project) | Inlined in full in the system prompt | System prompt |
| Description line of every file in scope | Listed in the system prompt, and offered again as a hint on later turns | System prompt |
| Body of any other file | Only when the model calls `memory_read`; the best-matching passage also comes back from `memory_search` | A tool result |

Text in the first two rows is scanned for prompt injection each time the prompt is
built. Text in the third row is not scanned. See
[What each check does and does not do](#what-each-check-does-and-does-not-do).

## Who can write

- **You**, with any editor, Obsidian or a sync tool. The plugin does not stop you, record
  who wrote what, or scan the file when it lands. It scans prompt-bound text later, when
  the prompt is built.
- **The agent**, through the seven `memory_*` tools. What it may do depends on the
  session; the table is in the README under
  [Projects](README.md#projects).
  - Full: `global/`, plus the session's own project bucket when it has one.
  - Add only: every other project's bucket. It can append to an existing file or create a
    new one, but not edit or delete. These writes record `<platform>@<project>` in the
    file's `sources` frontmatter, and the classic CLI and the TUI show a
    `Saved to <path>` status line.
  - None: cron, subagent and flush sessions, and every session when `readonly: true`.
    Setting `cron_writes: true` lets cron sessions write.
- **Any other program running as your user.** The store is not encrypted. File modes keep
  other accounts out (see below), and that is all.

Reading is not limited the same way. A session lists, searches and inlines `global/` and
its own project by default, but `memory_read` will read any memory path it is given,
including another project's.

## What each check does and does not do

| Check | What it does | What it does not do |
|---|---|---|
| **Injection scan** (`memory_buckets/_compat.py`, `tools.py`, `snapshot.py`, `provider.py`) | Runs Hermes's own threat patterns (`tools.threat_patterns`, `strict` scope) over the whole file a write would save, description included, and refuses with `threat_detected` and the pattern IDs. When building the prompt, replaces profile and preferences bodies, listing descriptions and hint descriptions that match with a `[BLOCKED: ...]` line. A prompt-bound file longer than the scanner reads (65,536 characters on current Hermes) is blocked whole. Built-in memory writes that match are not copied into `global/inbox.md`. | The patterns are a list of regular expressions that live in Hermes, not here, so they change when Hermes does. A rephrased or translated instruction passes. It does not scan `memory_read`, `memory_search` or `memory_list` output, `restore`, or `hermes memory-buckets import`. If Hermes has no scanner (an older Hermes, or the plugin running outside Hermes) scanning is off, and `hermes memory-buckets status` does not say so. |
| **Credential refusal** (`credentials.py`, `tools.py`) | Refuses a write with `secret_detected` when the saved file contains an AWS access key ID, an `sk-` API key, a GitHub or GitLab token, a Slack token, a PEM private key block, or `password`, `secret`, `token` or `api_key` followed by `:` or `=` and a value that looks like a credential (one word of six or more characters with a letter and a digit, not a path, URL, `$VAR`, placeholder or code call). The error names the kind of credential and never repeats the text. A credential in built-in memory is not copied into the inbox. This check does not need Hermes. | It is a pattern list. It misses other key formats (Stripe, Google, bearer tokens, connection strings with a password), a password written in a sentence, and a value of only letters, only digits or one containing `/`. It also refuses some harmless text, such as `token = lexer2` or `password: min-12chars`. It does not scan files already on disk, hand edits or synced files, `restore` or `import`. |
| **Private modes** (`store.py`, `migrate.py`) | Creates the store directories as `0700` and files as `0600`, including the search index and its journal files. Every writable session start removes group and other access from anything older that is looser. `hermes memory-buckets status` warns about loose paths. | Does nothing on Windows, where access follows the ACLs inherited from your profile folder. It is not encryption, and it does not stop root or a program running as you. It does not follow files out of the store: a copy, a sync target or a backup gets whatever mode the copying tool gives it. A store you shared with a group on purpose loses that access the next time a writable session starts. |
| **History** (`store.py`, `cli.py`) | Before `memory_write` replaces a file, `memory_str_replace` edits one or `memory_delete` removes one, the previous bytes are saved under `.history/`. Keeps the newest 20 versions of each file. Each time a file records a new version, entries older than 90 days are dropped too. `hermes memory-buckets history` lists them and `restore` writes one back. | Hand edits are not recorded, so the text before an editor or sync overwrite is gone. Appends and new files record nothing. Deleting a memory leaves a copy in `.history/` that nothing ages out, because pruning only runs when the same file records another version. `restore` is not scanned. History is per machine unless you sync `.history/` yourself. |
| **Scopes** (`scopes.py`, `tools.py`) | Limits what a session may write, as above. | Does not limit what it may read by path. |

## What is not covered

- **A wrong fact.** A page the agent read can persuade it to save something false, such as
  where you bank. No scan can tell. The `edited_at` and `sources` lines and `.history/`
  help you notice and undo it afterwards.
- **Instructions the patterns do not match.** See the table.
- **Anything that arrives by hand or by sync.** It gets no write-time check. Only the
  injection scan of prompt-bound text, each time the prompt is built, applies to it.
- **A credential that is already in a file.** The tools refuse to save one. They do not
  find ones you wrote yourself, and the history keeps whatever was there before.
- **Two machines writing synced copies at once.** The version check and the write lock only
  see this machine's files, so both writes succeed and the sync tool decides which
  survives. See [Syncing and backups](README.md#syncing-and-backups).
- **Your model provider.** Text the model reads from the store is sent to it. See
  [Privacy and data handling](README.md#privacy-and-data-handling).
- **Hermes itself.** The plugin relies on Hermes for the threat patterns, the project list
  and the tool loop.
