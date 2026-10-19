# Changelog

Notable changes to memory-buckets, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). Until 1.0, a minor version can
change the store's layout; the plugin migrates the store itself (see
[Migrating](README.md#migrating)).

## [Unreleased]

### Added

- **Drift detection for the Hermes internals the plugin depends on.** The Hermes
  integration probe now imports and calls the private code behind the desktop workspace
  override, the gateway per-turn cwd, the TUI status line and project lookup, and fails
  naming whichever moved. Before, those paths fell back silently (an ignored workspace,
  no status line, an unscoped session) and a Hermes refactor looked like behaviour drift.
  A new weekly CI job runs the integration tests on Linux against Hermes `main`, apart
  from the job pinned to a Hermes release.
- **Setup support for `hermes memory setup`.** The provider reports its twelve
  non-secret settings through `get_config_schema()` (types, defaults and the minimums
  the code already enforces) and writes them to `plugins.memory-buckets` through
  Hermes's own config writer in `save_config()`, so unrelated config.yaml keys survive.
  The README quick start and install steps now use `hermes config set` instead of a
  hand-edited config.yaml snippet.

- **Pre-compression nudge.** Facts mentioned since the last save and never filed were
  lost when Hermes compressed the conversation. The provider now implements
  `on_pre_compress`: it sets the filing reminder for the next turn, even if
  `nudge_interval` hasn't come round, and returns a line for the summary prompt asking
  it to keep durable facts that haven't been saved. No model calls, and the transcript
  isn't read. Read-only sessions and `nudge_interval: 0` skip both.

- **Threat scanning for memory.** Built-in memory already runs Hermes's prompt-injection
  patterns over every write and over what it loads into the prompt; memory-buckets did
  neither, and `global/profile.md` and `global/preferences.md` go into every system
  prompt. It now uses the same scanner (`tools.threat_patterns`, `strict` scope):
  - `memory_write`, `memory_append` and `memory_str_replace` refuse content that
    matches, with a new `threat_detected` error that lists the pattern IDs in
    `patterns` and does not repeat the text. The whole file is scanned, frontmatter
    description included. `memory_delete` is never blocked, so a bad file can be removed.
  - A file inlined into the system prompt that matches (a hand edit, a synced file) is
    replaced there by `[BLOCKED: <path> matched <pattern ids>; fix or delete it]`, and a
    matching description in the file listing is replaced the same way. A warning is
    logged. The file on disk is untouched and `memory_read` still returns it.
  - Without the scanner (a bare Python, or a Hermes that doesn't ship it) nothing is
    scanned and memory behaves as before. A scanner that raises is logged once and
    treated the same way.

- `hermes memory-buckets status` reports what the agent can't see or edit:
  `ignored` counts files outside the path rules (spaces, capitals, underscores,
  nesting below `topics/`, stray top-level files) and symlinks, and `frontmatter`
  counts files whose frontmatter the tools reject. Both point at `lint`, which
  lists them. Neither changes the exit code.

- The README says which files are read, and what the naming rules are.

- `tests/test_bench_retrieval.py` guards the benchmark: a paraphrase that shares a word
  with its target, a negative that names a file, or a filler store that fits in the
  prompt fails a test. With the real model it also holds floors for each split and
  fails if the paraphrase split reaches 1.0.

- **Recoverable history.** Overwrites, `memory_str_replace` edits and deletes save
  the previous bytes to `.history/<path>/` before the change is applied, so a bad
  write no longer loses a file for good. `hermes memory-buckets history PATH` lists
  the recorded versions and `hermes memory-buckets restore PATH [VERSION]` writes
  one back through the normal compare-and-swap path, so a restore is itself
  recorded and a concurrent change is refused. Appends and creations record
  nothing. Retention keeps the newest `history_keep` versions per file (default 20)
  and drops anything older than `history_days` (default 90); `history_keep: 0`
  turns history off. A history write that fails is logged and never breaks the
  memory write. Hand edits outside the plugin are not recorded (README: History).

- **`/tidy-memory` skill.** A bundled skill that finds duplicate facts, contradictions,
  stale time words ("currently", "this semester"), finished one-off tasks, files over
  about 8 KB and vague descriptions. It proposes the fixes as a numbered list, waits for
  you to say which to apply, and applies only those. It never touches `global/inbox.md`
  or another project's files. It registers like `/sort-imports`, with no code change.
  See [Tidying memory](README.md#tidying-memory).

### Fixed

- **Closing the session no longer closes the index under a running `queue_prefetch`.**
  Hermes drains its background executor only up to a timeout, so a prefetch could still be
  reading SQLite when `shutdown()` closed the connection, which can crash the interpreter.
  `shutdown()` now waits for prefetches in flight (within the same 10 seconds it gives the
  embedding worker) and leaves the index open, with a warning, if one outlasts that. A
  `queue_prefetch` that starts after `shutdown()` does nothing.

- `lint` in the devshell passes. The ruff rules are pinned in `pyproject.toml` (`E4`, `E7`,
  `E9`, `F`), so a newer ruff no longer adds hundreds of findings, and the four existing
  ones in the tests are fixed. The `ruff format --check` step is gone from `lint`: the
  tree was never formatted that way and 34 files would change.

- The tests ignore `MEMORY_BUCKETS_*` variables set in the shell (`MEMORY_BUCKETS_STORE`
  used to fail 25 of them). `tests/conftest.py` clears them for every test and keeps
  `MEMORY_BUCKETS_MODEL_DIR`, `MEMORY_BUCKETS_MODEL_F32` and
  `MEMORY_BUCKETS_REFERENCE_TOKENIZER`, which switch on the real-model tests.

- A pending filing reminder now carries over when Hermes switches to a new session id
  without a reset (compression that rotates the id), instead of being dropped with the
  old session's state. `/new` still clears it.

- **The prompt block no longer goes stale when a session changes project.** Scope
  follows the session's project on every tool call, but the frozen prompt block kept
  describing the project the session started in: it told the agent to save under the
  old bucket (which the tools then refused to edit) and left the new project's profile
  and filing rules out of context. `prefetch` now compares the session's project with
  the one it last described and, on a difference, sends a one-off note: the routing
  rules for the new project, its `profile.md` and `preferences.md` in full, and a line
  saying the old project is now add-only, and that the new one is no longer an "other
  project" as the block lists it. It fires once per move (also when leaving a
  project or returning to the first one), the files it inlines are left out of
  `prefetch` hints, and the frozen block is untouched, so prefix caching still works.
  `Snapshot` records the bucket it was built for (`Snapshot.project`). A resumed chat
  has no snapshot to compare with, so it gets no note (documented in the README).

- A whole-line `#` comment in the frontmatter no longer makes the file fail to
  parse (so the tools refuse to edit it). It is ignored when reading and kept in
  place when the file is rewritten.

- A `global/profile.md` too big to inline no longer floods the system prompt block:
  an always-kept file that doesn't fit under `snapshot_max_chars` is cut to its head,
  ending with a line that says how much was cut and points at `memory_read` for the
  rest (#17).

- `memory_read` returns at most 48 KiB per file. `MAX_FILE_BYTES` was checked on
  writes only, so a hand-edited or synced file over the cap came back whole and could
  take most of the context window in one call. Larger files now come back in pages,
  with `truncated`, `total_bytes` and `next_offset`, and an optional `offset`
  argument reads the next page (#17).

- Frontmatter aliases with a comma, such as `Smith, John`, are stored in the search
  index as a JSON array instead of `", "`-joined text, so they stay one alias and
  prefetch stops rescuing the file on `John` alone. The index schema version is now
  5: an existing cache is dropped and rebuilt automatically (#17).

- `hermes memory-buckets status` warns about files over the 48 KiB cap, which
  `lint` already reported: no memory tool can rewrite one as it stands.

### Changed

- When the prompt block's file listing doesn't fit under `snapshot_max_chars`, it now
  keeps the most recently edited files instead of the ones that sort first by path.
  Project files still come before global ones, and the lines that remain are shown in
  path order. A file's age is its `edited_at` stamp, falling back to its modification
  time. No store or index changes.

- **`tests/bench_retrieval.py` no longer saturates.** Every metric was 1.0 on its
  23-chunk store, because every query reused its target file's words, so it could
  neither catch a regression nor tell two `prefetch_min_similarity` values apart. It
  now reports three splits separately:
  - exact: the existing queries, now against a larger store (12/13 search top-1 where
    it was 13/13);
  - paraphrase: 16 queries that share no content word with their target;
  - negative: 13 greetings, acknowledgements and off-topic requests, plus the two
    existing no-match hint queries, scored as hint false positives.

  A second, large store adds 400 filler files so the prompt's file listing truncates;
  there only queries whose target the prompt does not show are scored. Each store
  prints a sweep of `prefetch_min_similarity` against hint recall and false positives.
  `run_benchmark` now returns a nested result per store and split instead of a flat
  dict. The numbers are in the README's new
  [Retrieval quality](README.md#retrieval-quality) section.

### Fixed

- **Long queries keep their keyword leg.** The FTS half of hybrid search only
  ever saw the first 32 tokens of a query, so a key term past that point (a
  question at the end of a pasted log, say) matched nothing: a long message was
  effectively vector-only. `_active_tokens` now samples the first and last 32
  tokens, order-preserving, before stopword filtering, mirroring the embedder's
  own head/tail pre-truncation. Query-side only; nothing indexed changes.

## [0.4.0] - 2026-10-16

Windows support. No store or index changes: upgrading needs no migration.

### Added

- **Windows support.** The plugin runs on Hermes's native Windows install:
  - The store's write lock uses `msvcrt.locking` where `fcntl` doesn't exist.
  - Files are written in binary mode, so `\n` stays `\n` and a write's version
    matches the bytes on disk.
  - Memory paths found on disk always use `/`, so `topics/`, `areas/` and
    `people/` files show up in `list`, `search`, the index and `lint`.
  - Every text read and write names UTF-8 instead of the cp1252 default. The
    built-in memory import splits `MEMORY.md` on `§` correctly as a result.
  - Replacing or deleting a memory file waits up to a second for another program
    holding it open (an editor, a virus scanner, another session), then fails with
    `locked`.
  - `hermes memory-buckets reindex` reports an index that another process has
    open instead of crashing.
  - Junctions count as symlinks: the store refuses them and `lint` reports them.
  - `import` and `import-prompt` print PowerShell backup and review commands
    (`Copy-Item -Recurse`, `git diff --no-index`) instead of `cp -a` and `diff -ru`.
- **Windows CI** on the GitHub mirror: the test suite on x64 (Python 3.10 and 3.14)
  and arm64 (3.14) with the real embedding model, and the Hermes integration test
  against a pinned Hermes release.
- `tests/wine.sh` (devshell: `check-windows`) runs the test suite on Windows
  Python under Wine.

### Fixed

- On Python 3.10 and 3.11, the bundled embedding model (float16, in pip and Nix
  installs) failed to load, because `memoryview` only casts to float16 from 3.12.
  Search quietly fell back to keywords only. Float16 weights now decode with
  `struct` on those versions, and the float16 converter uses `struct` too (its
  output is byte-identical).
- An index connection that failed to open (a corrupt `memory.sqlite`) was left
  open instead of closed. Harmless on Linux; on Windows it kept the rebuild from
  deleting the broken file.
- Rebuilding the index deletes `-wal` and `-shm` before the database, so an
  interrupted rebuild can't leave a stale WAL beside a fresh database.

## [0.3.0] - 2026-10-15

Retrieval and prefetch matching rework. The index schema is now v4; existing
`.index/memory.sqlite` caches rebuild automatically on startup.

### Changed

- **Finer chunks (index schema v4).** `CHUNK_CHARS` drops from 700 to 300, so a
  flat bullet note packs two or three facts per chunk instead of averaging
  eight to twelve unrelated bullets into one vector. Mean pooling a long mixed
  chunk divides a single fact's signal by roughly the square root of the number
  of bullets, which is why specific questions about a `profile.md`-style file
  used to miss the similarity threshold entirely.
- **Cleaner and contextualised chunk embeddings.** A chunk's vector no longer
  carries the raw `global/topics/foo.md` path or the `Read when working on...`
  description boilerplate:
  - Chunk 0 (the file's identity) embeds the stripped identity: bucket, stem,
    de-boilerplated description and aliases.
  - A body chunk under a heading keeps `heading\ntext`.
  - A body chunk with no heading is prefixed with its file's stem
    (`hetzner-box\n- ...`), except for the generic `profile`, `preferences`
    and `inbox` stems.
  Measured on `potion-retrieval-32M`, two unrelated topic files previously sat
  at `0.2994` cosine on their identity chunks, right on the `0.30` prefetch
  threshold; stripping the path and boilerplate drops that to `0.1177` and widens
  the true-positive margin by up to `+0.07`.
- **File-level rank fusion in hybrid search.** RRF ranks now count distinct
  files rather than raw chunk positions, so a file split into many chunks can
  no longer push another file several ranks down the curve.
- **Query-term coverage and similarity calibration on top of RRF.**
  - A file's keyword score is scaled by how much of the query's active tokens
    its matched chunks actually cover (`0.5 + 0.5 * coverage`), so a four-of-four
    keyword match beats a one-of-four stray `OR` match by a wide margin instead
    of by `1/61` against `1/62`.
  - A file's vector score is scaled by `0.75 + 0.5 * best similarity`, so a
    strong semantic match outranks a match scraping the relevance floor at an
    adjacent rank.
  - Section headings carry double weight in `bm25`.
  - Bare single-digit tokens are dropped from multi-word queries, so `clo-gpt-2`
    no longer matches on `2` alone.
- **Prefetch hints: project boost and exact stem or alias rescue.** `hints()`
  takes the session's `project` and `project_boost` (forwarded from `prefetch`)
  and, separately, rescues a file whose exact stem or frontmatter alias is named
  in the message. A topic named inside a noisy paste now surfaces even when the
  weak embedding misses it, and mentioning a topic's name in a turn about
  something else no longer produces a false-positive hint.
- **Head plus tail query windowing.** A user message longer than 512 tokens is
  embedded from its first 352 and last 160 tokens with duplicate token IDs
  removed, instead of a strict head truncation. A pasted build log with the
  actual question at the bottom no longer drowns that question out: measured
  cosine against the right file went from `0.0957` to `0.5306` on a
  180-line log followed by one question.

### Added

- **`tests/bench_retrieval.py`**: a synthetic retrieval and hint benchmark over
  a fictional corpus, reporting `search_top1`, `search_mrr`, `hint_top1`,
  `hint_fp_rate`, and embed and search latency, so a retrieval change can be
  checked with numbers rather than by feel.

### Performance

- **`StaticModel.encode` is 6.5x faster** on a 100-chunk batch: token IDs are
  deduplicated with `Counter`, unpacked token rows and WordPiece word splits are
  cached, and the vector sum uses `map(sum, zip(*rows))`.
- **Cosine scans are about 3x faster** via `math.sumprod` (with a
  `sum(map(float.__mul__, ...))` fallback on older Pythons), which also speeds
  up `dot`.

### Fixed

- A query whose only overlap with a file was a number or a bare stopword could
  outrank a genuine multi-keyword match.
- Prefetch could hint a topic the user had merely mentioned in passing, because
  a stem or alias match had no path into the hint list.

## [0.2.1] - 2026-10-14

### Added

- **`backup_paths` on the provider,** so `hermes backup` includes a custom
  `store_path` outside `HERMES_HOME`.
- **A staleness rule in the prompt's Saving section:** when a fact changes or is
  no longer true, the agent is told to update or remove it rather than append a
  contradicting bullet.

### Changed

- **Per-session prefetch hint cooldown.** Once a file is hinted by prefetch or
  read/written in a session, prefetch suppresses it for the next four turns and
  allows re-hinting five turns later. In-place context compression clears the
  cooldown state.
- **Hybrid search relevance floor and FTS stopword filtering.**
  - Vector search ignores zero-norm queries and chunks below `0.20` cosine
    similarity, so unrelated queries can return no matches instead of padding
    results up to `limit`.
  - Keyword search strips common English stopwords before building the `OR`
    query (falling back to all tokens when a query is all stopwords) and checks
    scope permissions before capping at 200 candidates.
  - When a file's identity chunk (`path` and `description`) wins, the result
    uses the best body chunk for `heading` and `snippet` instead of repeating
    the metadata.
- **Chunking and heading embeddings (index schema v2).**
  - Section headings are included when embedding chunk text, and nested
    headings without immediate body lines keep their parent path
    (`Parent > Child`).
  - Lines inside fenced code blocks (` ``` ` or `~~~`) are never parsed as
    Markdown headings or top-level bullets.
  - Existing `.index/memory.sqlite` caches rebuild automatically on startup.
- **Tool guardrails and schema size.**
  - `memory_write` strips a duplicate leading frontmatter block if the agent
    passes `memory_read` output back as `body`.
  - `memory_str_replace` deletes the full line (including its newline) when
    `new` is empty and `old` matches a single line without `\n`, normalises
    trailing newlines, and points to `memory_write` when `old` matches
    frontmatter instead of body text.
  - Cross-project `memory_write` conflicts with `if_version: "new"` direct the
    agent to `memory_append` instead of a versioned overwrite that would be
    refused as `out_of_scope`.
  - The full path-format help stays on `memory_write` while `memory_read`,
    `memory_str_replace`, `memory_append` and `memory_delete` use a shorter
    path description.
- **`plugin.yaml` uses Manifest v2** (`manifest_version: 2`, `api_version: 1`,
  `author`, `homepage` and `tags`).

### Fixed

- One-shot prefetch and background embedding threads no longer accumulate open
  SQLite connections in `Index._conns`, and opening a thread connection no
  longer runs a `BEGIN IMMEDIATE` schema write transaction when the schema is
  already current.
- `queue_prefetch` and `_after_write` share the embedding lock so they never
  embed the same backlog concurrently.
- Entry-point installs (pip and Nix) no longer call `register_skill`, which
  listed a descriptionless `memory_buckets:sort-imports` entry in the system
  prompt that Hermes pruned on `skill_view`. Plugin-directory installs now pass
  the parsed `description` and `frontmatter` from `SKILL.md`.
- Resumed sessions (where Hermes reuses the persisted system prompt without
  calling `system_prompt_block`) still exclude `global/profile.md`,
  `global/preferences.md` and the project's `profile.md` and `preferences.md`
  from prefetch hints.
- In-place context compression (`on_session_switch` on the current session id)
  clears the cached prompt snapshot so the rebuilt prompt reflects current
  files.
- `projects.session_cwd` checks the live terminal session cwd first, so
  switching projects mid-turn with `desktop_project` or `cd` takes effect
  immediately.
- `provider.initialize` expands `~` in `store_path`, matching the CLI.
- One-shot CLI commands (`status`, `search`, `reindex`, `hints`) no longer
  spawn a background model download thread or create `<store>/.models/` when
  the built-in model is not yet downloaded.
- `Store.projects` ignores symlinked directories, and `Store.delete` removes
  empty parent and bucket directories when the last file in a project is
  deleted.
- `frontmatter` preserves multi-quoted scalars (`"a" and "b"`) and handles
  escaped quotes (`\"`) inside inline lists.
- `migrate --dry-run` validates tidied inbox content the same way `migrate`
  does, and `migrate` only prints `originals kept in _backup` when a backup was
  written.
- `lint` checks slash wikilinks (`[[bucket/topic]]`) against valid store files
  rather than raw filesystem existence.
- `diagnose` and `import-prompt` only say the agent loads `sort-imports`
  automatically when installed as a plugin directory or configured in
  `skills.external_dirs`.

## [0.2.0] - 2026-10-12

Upgrading from 0.1.0 needs nothing from you: the first writable session
migrates the store. It backs up and removes `_pending/`, and keeps every
file it changes or removes under `_backup/`. `hermes memory-buckets migrate
--dry-run` shows what it will do. **If your config sets `write_policy:
confined`, upgrading widens what sessions may write.** Project sessions now
write `global/`, and every writable session can add to other projects'
buckets. The setting is ignored, with a warning in the log.

### Added

- **Provenance on cross-project writes.** A write into another project's
  bucket adds `<platform>@<project>` to the file's `sources`, or
  `<platform>@*` from an unscoped session: `tui@proj-2`, `tui@*`. Nothing
  undoes these writes yet; the tag is there to trace them.
- **A status line on cross-project writes:** `📝 Saved to <path>`. The
  classic CLI and the terminal TUI show it. Elsewhere it isn't shown, and the
  agent isn't asked to tell you.
- `status` and `lint` report a leftover `_pending/` until migration removes
  it.

### Changed

- **The agent files every fact itself.** Nothing waits for you to apply it.
  - Any writable session, unscoped ones included, can add to another
    project's bucket: `memory_append` to an existing file, or `memory_write`
    with `if_version: "new"` to create one. Editing, overwriting and deleting
    there stay with that project's own sessions, and the refusal names both
    ways to add.
  - Another project is a bucket on disk or any Hermes project, including
    ones with no memory yet. A fact about a project with neither goes to
    `global/topics/<name>.md`.
  - A fact the agent can't place goes under `global/`, not the inbox.
- **`global/inbox.md` only holds imported memory:** entries from
  `hermes memory-buckets import` and the mirror of built-in memory writes.
  `memory_append` to it is refused. Its description is now "Imported memory
  waiting to be sorted", and migration updates it. Existing
  `added in conversation` sections stay and are sorted with the rest. The
  agent can still clear it with `memory_str_replace`, `memory_write` or
  `memory_delete`; a deleted inbox comes back with the next import.
- **Migration removes empty inbox headings.** A `## ` heading with no
  non-blank lines before the next heading or the end of the file goes,
  whenever the migration runs — so each run after a sort tidies what the
  last sort emptied. A section holding only a code fence counts as content.
  Removal loses nothing, so a headings-only run keeps no backup (only a
  description change is backed up, as before), and `status` and `lint`
  don't report empty headings: the next writable session clears them.
- **The prompt block** follows suit: the rules for other projects' buckets
  replace the inbox and proposal steps, and the inbox note only mentions
  imported memory.
- **The `sort-inbox` skill is now `sort-imports`,** and its slash command is
  `/sort-imports`. It writes project facts straight into their buckets
  instead of proposing them. There's no alias; `skills.external_dirs` needs
  no change.
- **`hermes memory-buckets sort-prompt` is now `import-prompt`,** with no
  alias.

### Removed

- **Proposals:** the `memory_propose` tool (eight tools become seven), the
  `/memory-pending`, `/memory-apply` and `/memory-reject` slash commands, and
  the CLI's `pending`, `apply` and `reject`. The inbox and proposal notices
  go with them.
- **`<store>/_pending/`.** Migration copies each proposal to
  `_backup/0.2.0/_pending/` and removes the folder. It applies none of them:
  you never confirmed them, and their lines are still in the inbox, so the
  next `/sort-imports` files them.
- **`write_policy`.** Project sessions always write `global/`. A leftover
  `confined` is ignored with a logged warning (see above).

### Fixed

- With `embeddings.backend: off`, prefetch logged a traceback and returned
  nothing on every turn. It now runs keyword-only, with no hints.
- A path or project id ending in a newline passed validation, so
  `memory_write` could create a file literally named `x.md\n`. Both are now
  refused.
- The built-in tokeniser turned a word-final capital sigma (Σ) into a final
  sigma (ς), where the reference tokeniser gives σ. Greek text now embeds
  the same as the reference.

## [0.1.0] - 2026-10-01

Upgrading from 0.0.1 needs nothing from you: the first writable session
migrates the store, and keeps every file it changes under
`_backup/0.1.0/`. `hermes memory-buckets migrate --dry-run` shows what it
will do.

### Added

- **Store migrations.** They run when a writable session starts, or on
  demand with `hermes memory-buckets migrate [--dry-run]`. Originals are
  backed up under `_backup/<version>/`, and a file that can't be read or
  parsed is skipped and reported while the rest still migrate. `status` and
  `lint` report anything still waiting.
- **A generated project header** in the prompt: the Hermes project's name,
  description and folders, followed by the bucket's `profile.md` and
  `preferences.md` in full.
- **A line about the project profile** in that header. It says when the
  profile doesn't exist yet or is empty, and says how to fill it. When its
  frontmatter is missing or can't be parsed, the agent is told to ask you to
  fix it; nothing repairs it for you.
- **Proposals from any session.** A fact about another project goes into
  `global/inbox.md` as `<project>: <fact>` and is proposed straight away with
  `memory_propose`. Nothing is saved until you `/memory-apply` it.
- **Proposals into Hermes projects that have no memory yet.** They don't
  need `new_project` or a profile. Applying one checks that Hermes still has
  the project.
- **Notices** when the agent adds to the inbox or makes a proposal, with the
  proposal's summary and the commands to apply or reject it. The classic CLI
  and the terminal TUI show them as status lines. Elsewhere (the desktop
  app, gateways, and quiet, one-shot or muted turns), the tool result asks
  the agent to tell you.
- **Inbox appends under `write_policy: confined`.** A confined project
  session can append to `global/inbox.md`, and still can't write anything
  else in `global/`.
- This changelog.

### Changed

- **The prompt block is rewritten:** shorter, plainer instructions, with
  routing rules that save each fact once, in one place. A fact that's partly
  general and partly about the project is split; one for another project
  goes through the inbox and a proposal; one the agent can't place goes to
  the inbox. The block lists the projects it can propose into.
- **`global/inbox.md` is a standing file** rather than a migration leftover
  you delete after sorting:
  - `memory_append` creates it, and files entries under a dated
    `## added in conversation <date> (<platform>)` heading.
  - An append fills in missing frontmatter, so an inbox you edited by hand
    keeps working. Frontmatter that can't be parsed is refused, not
    rewritten.
  - The plugin owns its description, and migration replaces any other one.
  - `status` and `lint` report how many entries are waiting, and say nothing
    when it's empty.
- **Trimming the prompt to `snapshot_max_chars`:** over the cap, a project
  session drops the global preferences first, then the project's own
  preferences, then the project profile. The global profile is never
  dropped.
- **The `sort-inbox` skill** leaves lines that are already in a pending
  proposal alone, and treats Hermes projects with no files yet as existing
  projects.
- `memory_propose` is no longer described as a sorting-only tool, and its
  summary is shown to you.

### Removed

- **`<project>/index.md`.** It was never maintained, and the project header
  is now generated instead. Migration turns a leftover one into the
  project's `profile.md`, or appends its body unchanged to an existing
  profile under `## Moved from index.md`.

### Fixed

- The prompt's file listing assumed about 40 characters a line and was cut
  far below the cap. It now fills the space up to the cap.
- Trimming no longer drops a file when everything fits, or when dropping it
  would make the prompt longer.
- A project profile with an empty body is no longer reported as missing,
  which had led the agent into a failing `memory_write`.
- `lint` only sends you to `migrate` for files that `migrate` handles.
- A `## ` line inside a fenced code block no longer counts as an inbox
  section.

## [0.0.1] - 2026-09-30

Initial release.

[0.4.0]: https://git.cl0vr.co/cl0vr/memory-buckets/compare/v0.3.0...v0.4.0
[0.3.0]: https://git.cl0vr.co/cl0vr/memory-buckets/compare/v0.2.1...v0.3.0
[0.2.1]: https://git.cl0vr.co/cl0vr/memory-buckets/compare/v0.2.0...v0.2.1
[0.2.0]: https://git.cl0vr.co/cl0vr/memory-buckets/compare/v0.1.0...v0.2.0
[0.1.0]: https://git.cl0vr.co/cl0vr/memory-buckets/compare/v0.0.1...v0.1.0
[0.0.1]: https://git.cl0vr.co/cl0vr/memory-buckets/src/tag/v0.0.1
