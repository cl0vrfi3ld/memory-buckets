# memory-buckets

![A banner reading "Memory Buckets" with subtext "Big memory, small footprint" flanked on the right by various blue and grey shapes, including one big bucket containing a circle and a square with rounded corners.](assets/memory-buckets-banner-mono.svg)

`memory-buckets` is a memory provider for [Hermes Agent](https://hermes-agent.nousresearch.com/)
modelled on Claude's memory system: one Markdown file per subject, organised into a
`global` bucket and one bucket per [Hermes project](#projects), read on demand.

The store is a plain directory of Markdown files with YAML frontmatter that you can
open directly as an Obsidian vault. Search is hybrid, combining SQLite FTS5 with a
static embedding model that runs in-process in pure stdlib Python without an external
server or GPU.

## Quick start

1. Install the plugin:
   ```sh
   hermes plugins install --no-enable https://git.cl0vr.co/cl0vr/memory-buckets
   ```
   `--no-enable` skips the general-plugin prompt (`memory.provider` is what
   activates a memory provider).
2. Set it as your memory provider in `~/.hermes/config.yaml`
   (`%LOCALAPPDATA%\hermes\config.yaml` on Windows):
   ```yaml
   memory:
     provider: memory-buckets
   ```
3. Start a new session (`/new`) or restart the gateway.
4. Check that it loaded: `hermes memory-buckets status`.

On first use, a plugin-directory install downloads the embedding model (about 130 MB)
in the background. Search falls back to keywords until the download finishes. The pip
and Nix installs bundle the model at build time; see [Install](#install).

## Install

Whichever install method you use, enable the provider by setting
`memory.provider: memory-buckets` in `config.yaml`.

Requirements are Linux, macOS or Windows, Python 3.10+, and a `sqlite3` module compiled
with FTS5 (`hermes memory-buckets diagnose` checks this). On Windows the plugin runs on
Hermes's native install, where `$HERMES_HOME` is `%LOCALAPPDATA%\hermes` and
`config.yaml` lives there too.

### Plugin directory

`hermes plugins install` clones the repo into `$HERMES_HOME/plugins/memory-buckets/`.
You can also clone it there by hand or symlink a local checkout, as long as the
directory is named `memory-buckets`:

```sh
git clone https://git.cl0vr.co/cl0vr/memory-buckets ~/.hermes/plugins/memory-buckets
# or
ln -s ~/src/memory-buckets ~/.hermes/plugins/memory-buckets
```

On Windows, in PowerShell (a junction needs no admin rights, unlike a symlink):

```powershell
git clone https://git.cl0vr.co/cl0vr/memory-buckets "$env:LOCALAPPDATA\hermes\plugins\memory-buckets"
# or
New-Item -ItemType Junction -Path "$env:LOCALAPPDATA\hermes\plugins\memory-buckets" -Target "$HOME\src\memory-buckets"
```

### pip

Install into the Python environment Hermes runs in:

```sh
pip install git+https://git.cl0vr.co/cl0vr/memory-buckets
# or, from a local checkout
pip install .
```

Hermes discovers the package through the `hermes_agent.memory_providers` entry point,
and building the wheel bundles a float16 copy of the embedding model
(see [Embeddings](#embeddings)).

### Nix

`nix/package.nix` builds the plugin as a Python package with the embedding model
bundled. Pass it to Hermes via `extraPythonPackages`, built with Hermes's own Python
interpreter, following
[Distribute for NixOS](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins#distribute-for-nixos)
in the Hermes docs (supported in both the NixOS and Home Manager modules):

```nix
# flake inputs
inputs.memory-buckets = {
  url = "git+https://git.cl0vr.co/cl0vr/memory-buckets";
  # or the GitHub mirror: url = "github:cl0vrfi3ld/memory-buckets";
  flake = false;  # only nix/package.nix is used
};
```

```nix
{ config, inputs, ... }:
let
  memory-buckets = config.services.hermes-agent.package.python.pkgs.callPackage
    "${inputs.memory-buckets}/nix/package.nix" { };
in
{
  services.hermes-agent = {
    extraPythonPackages = [ memory-buckets ];
    settings = {
      memory.provider = "memory-buckets";
      skills.external_dirs = [ "${memory-buckets}/${memory-buckets.skillsPath}" ];  # /sort-imports
    };
  };
}
```

Avoid `extraPlugins` here: Hermes symlinks those entries as
`plugins/nix-managed-<name>` and derives a memory provider's name from its directory,
so the provider would register as `nix-managed-memory-buckets`.

The built package is around 63 MB. Building it runs the test suite, including the
real-model tests.

## How it works

### The store

```
$HERMES_HOME/memory-buckets/
  memories/                        every memory file
    global/     profile.md preferences.md inbox.md  topics/ areas/ people/
    <project>/  profile.md preferences.md            topics/ areas/ people/
  _backup/<version>/               originals kept before a store migration
  .index/memory.sqlite             search and vector cache (safe to delete)
  .models/                         downloaded embedding model, when not bundled
```

Each file covers one subject. Its frontmatter carries `name` (matching the filename
stem), a one-line `description` telling the agent when to read the file, and optional
`aliases` (other names for the subject). The body is plain Markdown bullets. Writes
made through the plugin also record `sources` and a UTC `edited_at` timestamp; edits
you make by hand in Obsidian or an editor are picked up automatically on the next
search or turn.

### Projects

Buckets map directly to Hermes Projects, the named workspaces stored per profile in
`$HERMES_HOME/projects.db`. You create them with `hermes project create`, the desktop
sidebar, or the `desktop_project` tool; `memory-buckets` doesn't keep a separate
project list of its own.

A session belongs to whichever Hermes project owns its working directory (the
innermost project folder containing it). The working directory is resolved on every
tool call, first match winning:

1. the session's workspace override, updated mid-turn when you switch projects with
   `desktop_project` or the desktop sidebar (plain `cd` in the terminal doesn't
   change it);
2. the working directory a multi-session gateway pins for the turn;
3. the `cwd` Hermes passed when starting the session;
4. the agent's working directory (`terminal.cwd`, or wherever you ran `hermes`).

Because the check runs per call, switching a chat into a project takes effect right
away. A session outside every project is unscoped and reads only `global/` by
default.

A project's slug names its bucket under `memories/<slug>/` (with `_` normalised to
`-`). Hermes keeps slugs fixed after creation, so to attach an existing bucket on
disk to a new Hermes project, pass `--slug`:

```sh
hermes project create "Home server" ~/src/home-server --slug home-server
```

`hermes memory-buckets status` lists any buckets on disk that don't have a matching
Hermes project yet.

By default, a project session reads `global/` and its own bucket, and only reads
other projects' buckets when you ask about them. Write access depends on the session:

| Session | `global/` | Own bucket | Other buckets |
|---|---|---|---|
| Unscoped | full | - | add only |
| Project | full | full | add only |
| Read-only (cron, subagents, flushes, `readonly: true`) | none | none | none |

Full access allows all write tools (`memory_write`, `memory_str_replace`,
`memory_append`, `memory_delete`). Add-only access allows appending to an existing
file with `memory_append` or creating a new file with `memory_write` and
`if_version: "new"`, while edits, overwrites, and deletes in another project's bucket
are refused and left to sessions actually working in that project. Setting
`cron_writes: true` lets cron sessions write like a normal session.

"Other buckets" covers every bucket on disk plus every Hermes project, including
projects that have no memory files yet. Facts about a project that has neither go into
`global/topics/<name>.md`.

When a session writes into another project's bucket, the file's `sources` frontmatter
records `<platform>@<project>` (for example `tui@proj-2`, or `tui@*` from an unscoped
TUI session) so you can trace cross-project additions later. Classic CLI and TUI
sessions also show a brief `📝 Saved to <path>` status line on cross-project writes.

### What the agent gets

At the start of a session, the plugin builds a system prompt block (capped at
`snapshot_max_chars`, 12,000 by default) and freezes it for the session so prefix
caching stays warm. If Hermes compresses the conversation in place, the cached
snapshot is cleared and rebuilt from the current files.

The prompt block contains:
- usage and routing instructions for reading and filing facts;
- `global/profile.md` and `global/preferences.md` in full;
- in a project session, a generated project header (the Hermes project's folders, its
  name and description when they add detail, and the bucket's `profile.md` and
  `preferences.md` in full, or a note asking the agent to create `profile.md` once it
  learns about the project);
- a path-and-description index of the remaining files in scope, plus the list of other
  projects the session can add to.

If the prompt block would exceed `snapshot_max_chars`, it trims the file listing
first, then drops `global/preferences.md`, `<project>/preferences.md`, and
`<project>/profile.md` in that order. `global/profile.md` and the project header are
always kept.

The listing is cut by recency, not by name. Project files come first, then global
ones; within each group the most recently edited files are kept and the least recently
edited are the ones summarised as "and N more files". A file's age is its `edited_at`
stamp, or its modification time when it has none (a hand edit, or a date that isn't in
the stamp's format). The lines that survive are still shown in path order. Hand edits
to a file that already has a stamp don't move it, since the stamp wins over the
modification time.

The agent gets seven tools: `memory_list`, `memory_read`, `memory_search`,
`memory_write`, `memory_str_replace`, `memory_append`, and `memory_delete`. Every
write is compare-and-swap against a content hash version; on a conflict, the tool
returns the file's current version and content so the agent can merge and retry.

On each user turn, `prefetch` checks whether any files outside the inlined profiles
and preferences match the message, and injects their paths and descriptions as hints
(never the file bodies). Once a file has been hinted, read, or written, prefetch
holds it back for the next four turns so the agent isn't nagged with the same hint on
every message. Every `nudge_interval` turns (default 10), a short reminder also asks
the agent to save any new durable facts from the conversation.

### Embeddings

Embeddings use
[potion-retrieval-32M](https://huggingface.co/minishlab/potion-retrieval-32M)
(MinishLab, MIT), a Model2Vec static model that distils a transformer into a token
lookup table. Embedding a piece of text is BERT WordPiece tokenisation, a weighted
mean of token rows, and L2 normalisation in pure Python. The safetensors weights are
memory-mapped and shared across processes by the OS page cache, so loading is instant
and encoding ~1,000 characters takes a couple of milliseconds.

| Install | How the model is provided |
|---|---|
| pip / wheel | Bundled at build time. `hatch_build.py` fetches the pinned upstream files, checks their sha256 hashes, converts the weights to float16 (~65 MB), and caches the result in `${XDG_CACHE_HOME:-~/.cache}/memory-buckets` (`%USERPROFILE%\.cache\memory-buckets` on Windows). Set `MEMORY_BUCKETS_BUNDLE_MODEL=0` to skip bundling; editable installs (`uv sync`) never bundle it. |
| Nix | Bundled at build time via `nix/model.nix` with the same float16 conversion. |
| Plugin directory | Downloaded into `$HERMES_HOME/memory-buckets/.models/` in the background on first use, or up front with `hermes memory-buckets fetch-model`. |

To point at an existing local copy, set `embeddings.local_model_dir` (or
`MEMORY_BUCKETS_MODEL_DIR`). `embeddings.auto_download: false` disables background
downloads, and `embeddings.backend: off` disables embeddings completely (leaving FTS5
keyword search only and turning off prefetch hints).

When indexing a file, the store splits the body into heading-aware chunks of up to
300 characters alongside an identity chunk (`bucket`, filename stem, description, and
`aliases`). Body chunks include their section heading path (or the file's stem when
there is no heading) in the embedded text so individual bullets keep their subject
context. Long user messages over 512 tokens are embedded from their first 352 and
last 160 tokens, so a question at the bottom of a pasted build log isn't lost to
truncation.

`memory_search` runs FTS5 BM25 (with section headings weighted 2x and stopwords
filtered) alongside cosine similarity (with a `0.20` relevance floor), fuses file
ranks with Reciprocal Rank Fusion, and scales scores by query-term coverage, best
vector similarity, and `project_boost`.

Prefetch hints use vector similarity (boosted by `project_boost` for the session's
own bucket) and also rescue any file whose exact stem or frontmatter alias appears in
the message. The default `prefetch_min_similarity` threshold is `0.27`; you can
inspect raw similarity scores against your own store with
`hermes memory-buckets hints "<query>"`.

## Importing built-in memory

`global/inbox.md` is a staging file for memory imported from Hermes's built-in
`MEMORY.md` and `USER.md`. Only two things append to it:

- `hermes memory-buckets import`, which copies entries over from `MEMORY.md` and
  `USER.md`;
- the `on_memory_write` bridge, which mirrors built-in memory writes while built-in
  memory is still enabled.

During normal conversations the agent files facts directly into `global/` or the
relevant project bucket, and `memory_append` to `global/inbox.md` is refused. The
agent can still remove sorted entries from the inbox with `memory_str_replace`,
`memory_write`, or `memory_delete`. An empty or deleted inbox is fine; the next
import recreates it if needed, and the startup migration strips any `## ` section
headings that a sort left empty.

To sort the inbox, run `/sort-imports` or ask the agent to sort your imported memory
(it only sorts when asked):

- General facts are filed into `global/` (`profile.md`, `preferences.md`, `people/`,
  `areas/`, or `topics/`).
- Project facts are added directly to that project's bucket (`memory_append` or new
  files only, never overwriting or editing existing project notes). Facts for an
  unrecognised project go into `global/topics/<name>.md`.
- Entries the agent isn't sure how to classify are saved under `global/` and listed in
  its final reply for you to check, along with any stale or duplicate lines it
  dropped.

Back up your store before sorting. `import` and `import-prompt` print the commands to
back it up and to review the result afterwards: `cp -a` and `diff -ru`, or on Windows
the PowerShell `Copy-Item -Recurse` and `git diff --no-index`.

For `/sort-imports` to register as a slash command, Hermes needs the plugin's `skills`
directory in `skills.external_dirs`:

```yaml
skills:
  external_dirs:
    - plugins/memory-buckets/skills   # plugin directory, relative to HERMES_HOME
    # pip: <site-packages>/memory_buckets/skills
    # Nix: see the Nix install example above
```

Without `skills.external_dirs`, asking the agent in prose still works for a
plugin-directory install because the prompt block points it to `skill_view`. For pip
and Nix installs without `skills.external_dirs`, paste the prompt printed by
`hermes memory-buckets import-prompt`. Avoid saving that prompt as a custom skill in
`~/.hermes/skills/`, as a local copy will shadow updates to the bundled skill.

## Reference

### Commands

`hermes memory-buckets` is available whenever `memory.provider` is `memory-buckets`:

```
status                                  store, index, embeddings, Hermes config, unlinked buckets
diagnose [--platform P]                 check why an agent would or wouldn't see the memory tools
lint                                    check paths, frontmatter, duplicate stems, and [[links]]
migrate [--dry-run]                     migrate a store from an older version (also runs on startup)
reindex [--embed]                       rebuild the SQLite search cache
search QUERY [--limit N] [--prefix P]   search the store (--json for raw output)
hints QUERY [--limit N] [--threshold T] show raw prefetch similarities to tune prefetch_min_similarity
fetch-model [--force]                   download the built-in embedding model now
import [--from HERMES_HOME] [--dry-run] append built-in MEMORY.md/USER.md entries to global/inbox.md
import-prompt                           print the prompt for sorting global/inbox.md
```

### Configuration

All settings are optional and live under `plugins.memory-buckets` in `config.yaml`:

| Key | Default | Effect |
|---|---|---|
| `readonly` | `false` | make every session read-only |
| `cron_writes` | `false` | allow cron sessions to write (cron, subagent, and flush contexts are otherwise read-only) |
| `nudge_interval` | `10` | user turns between reminders to file durable facts (`0` disables reminders) |
| `snapshot_max_chars` | `12000` | character cap on the system prompt block |
| `prefetch_min_similarity` | `<model default>` | minimum cosine similarity for prefetch hints (`0.27` for the built-in model; see [Embeddings](#embeddings)) |
| `prefetch_max_hints` | `4` | maximum prefetch hints per turn |
| `search_limit` | `8` | default `memory_search` result count |
| `project_boost` | `1.3` | score multiplier for the session's own bucket in search and prefetch hints |
| `store_path` | `$HERMES_HOME/memory-buckets` | store directory (custom paths outside `$HERMES_HOME` are also included in `hermes backup`) |
| `embeddings.backend` | `local` | `local` (built-in model) or `off` (keyword search only) |
| `embeddings.auto_download` | `true` | download the model in the background when not bundled |
| `embeddings.local_model_dir` | | load the model from this directory instead |

Environment variable overrides: `MEMORY_BUCKETS_STORE`,
`MEMORY_BUCKETS_EMBEDDINGS_BACKEND`, `MEMORY_BUCKETS_EMBEDDINGS_AUTO_DOWNLOAD`, and
`MEMORY_BUCKETS_MODEL_DIR`. Setting `MEMORY_BUCKETS_TRACE=1` logs every provider hook
call.

## Troubleshooting

If the agent doesn't see the `memory_*` tools after you set `memory.provider`, start a
new session (`/new`) or restart the gateway: Hermes resolves the memory provider when
constructing an agent, so sessions that were already running won't pick it up. If a
fresh session still lacks the tools, run `hermes memory-buckets diagnose` to check
provider loading and per-platform toolsets.

If tools are missing on one specific platform (for example Telegram or Discord), make
sure the `memory` toolset is enabled for that platform in `platform_toolsets` and not
listed in `agent.disabled_toolsets`.

`hermes plugins doctor memory-buckets` reporting `0 tool(s), 0 hook(s)` is normal:
`plugins doctor` only counts general-plugin registrations, whereas memory provider
tools are registered through Hermes's memory manager. Use
`hermes memory-buckets status` instead, which should show 7 memory tools.

## Migrating

### From an older version

Writable sessions automatically migrate the store on startup (read-only sessions skip
migration; if you run with `readonly: true`, run `hermes memory-buckets migrate`
manually). Before modifying or removing any file in the store, `migrate` saves the
original under `_backup/<version>/`. You can preview pending changes with
`hermes memory-buckets migrate --dry-run`.

The SQLite cache at `.index/memory.sqlite` also rebuilds itself automatically whenever
the index schema changes (most recently schema v4 in 0.3.0).

For stores created on 0.1.0 or earlier:
- Leftover `<project>/index.md` files (replaced in 0.1.0 by the generated project
  header) are renamed to `<project>/profile.md`, or appended under
  `## Moved from index.md` if `profile.md` already exists.
- Leftover `_pending/` proposal directories from 0.1.0 are backed up to
  `_backup/0.2.0/_pending/` and removed; their unconfirmed lines remain in
  `global/inbox.md` for `/sort-imports` to file.
- `write_policy: confined` was removed in 0.2.0 (project sessions now write `global/`
  and can add to other projects' buckets); if still present in `config.yaml`, it logs
  a warning and is ignored.

See [CHANGELOG.md](CHANGELOG.md) for full release notes.

### From built-in memory

1. Install the plugin and set `memory.provider: memory-buckets`, leaving built-in
   memory enabled for the moment so any writes still mirror into `global/inbox.md`.
2. Run `hermes memory-buckets import` (pass `--from <HERMES_HOME>` to import from
   another profile or instance).
3. Back up the store with the command it prints, then start a new chat and
   [sort the imported memory](#importing-built-in-memory).
4. Check the result with the review command it printed (`diff -ru`, or
   `git diff --no-index` on Windows).
5. Disable built-in memory in `config.yaml` (`memory.memory_enabled: false` and
   `memory.user_profile_enabled: false`).

## Development

Inside the Nix devshell (`direnv allow` or `nix develop`):
- `sync` creates or updates `.venv` with `uv sync`;
- `check` runs the test suite (`uv run pytest tests/`);
- `check-windows` runs it on Windows Python under Wine (`tests/wine.sh`, x86_64 Linux;
  the first run fetches Wine and a Windows CPython into `~/.cache/memory-buckets/wine`);
- `lint` runs `ruff check .` and `ruff format --check .`;
- `run` executes a command inside the project venv (`uv run`).

Two environment variables enable extra test coverage:
- `HERMES_PYTHON=<path to Hermes's python>` runs the Hermes integration tests against
  a temporary `HERMES_HOME`, testing both plugin-directory and pip entry-point loads.
- `MEMORY_BUCKETS_MODEL_DIR=<path to potion-retrieval-32M>` enables the real-model
  embedding and retrieval tests.

To run the synthetic retrieval and prefetch benchmark added in 0.3.0:

```sh
MEMORY_BUCKETS_MODEL_DIR=<model-dir> uv run python -m tests.bench_retrieval
```

`nix flake check` builds the package with the float16 model bundled and runs the test
suite inside the Nix sandbox, including the real-model tests and token-for-token
parity against Hugging Face `tokenizers`. `tests/smoke.sh` runs a manual end-to-end
chat test against a throwaway profile (never `~/.hermes`); it's a bash script, so it
runs on Linux and macOS only.

Windows runs in CI: `.github/workflows/windows.yml` on the GitHub mirror runs the test
suite on Windows x64 (Python 3.10 and 3.14) and arm64 (3.14) with the real model, and
the Hermes integration tests against the Hermes release pinned in `HERMES_REF`. Bump
that pin when Hermes releases. Hermes refuses to build as a wheel, so the job installs
an editable checkout.
