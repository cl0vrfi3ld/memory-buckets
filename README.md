# memory-buckets

A memory provider for [Hermes Agent](https://hermes-agent.nousresearch.com/)
that keeps memory the way Claude does: one Markdown "bucket" per subject, read on
demand. There's a `global` bucket and one per
[Hermes project](#projects).

- **Plain files.** The store is a directory of Markdown files with frontmatter.
  It also opens as an Obsidian vault.
- **Hybrid search.** SQLite FTS5 plus a built-in embedding model that runs
  in-process. There's no server and no GPU.
- **Projects from Hermes.** A session's bucket follows the Hermes project it's
  working in.

## Quick start

1. Install it as a Hermes plugin:
   ```sh
   hermes plugins install --no-enable https://git.cl0vr.co/cl0vr/memory-buckets
   ```
   `--no-enable` skips a prompt you don't need, because `memory.provider` is
   what activates a memory provider.
2. Make it the memory provider in `~/.hermes/config.yaml`:
   ```yaml
   memory:
     provider: memory-buckets
   ```
3. Start a new session (`/new`) or restart the gateway.
4. Check it: `hermes memory-buckets status`.

On first use the plugin downloads its embedding model (about 130 MB) in the
background. Search is keyword-only until the model arrives. Other ways to
install, some of which bundle the model, are under [Install](#install).

## Install

Hermes finds the plugin whichever way you install it. In every case you still
set `memory.provider: memory-buckets` yourself.

It needs Linux or macOS, Python 3.10 or later, and a `sqlite3` built with FTS5
(`hermes memory-buckets diagnose` tells you if it isn't). Windows isn't
supported: the store locks with `fcntl`, which Windows doesn't have.

### Plugin directory

`hermes plugins install`, as in the quick start, clones it into
`$HERMES_HOME/plugins/memory-buckets/`. You can also clone it there yourself,
or link a checkout you work on. The directory must be named `memory-buckets`:

```sh
git clone https://git.cl0vr.co/cl0vr/memory-buckets ~/.hermes/plugins/memory-buckets
# or
ln -s ~/src/memory-buckets ~/.hermes/plugins/memory-buckets
```

### pip

Install into the Python environment Hermes runs in:

```sh
pip install git+https://git.cl0vr.co/cl0vr/memory-buckets
# or, from a checkout
pip install .
```

Hermes finds it through the `hermes_agent.memory_providers` entry point.
Building the wheel bundles the embedding model (see [Embeddings](#embeddings)).

### Nix

`nix/package.nix` builds the plugin as a Python package, with the model
bundled. Give it to Hermes through `extraPythonPackages`, built with Hermes's
own interpreter. That's the route in the Hermes docs'
[Distribute for NixOS](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins#distribute-for-nixos).
The option exists in both the NixOS and the Home Manager module.

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

Don't use `extraPlugins`. It links the plugin in as
`plugins/nix-managed-<name>`, and Hermes names a memory provider after its
directory, so the provider would be called `nix-managed-memory-buckets`.

The package is about 63 MB. Building it runs the test suite, including the
real-model tests.

## How it works

### The store

```
$HERMES_HOME/memory-buckets/
  memories/                        every memory file
    global/     profile.md preferences.md inbox.md  topics/ areas/ people/
    <project>/  profile.md preferences.md            topics/ areas/ people/
  _backup/<version>/               originals a migration changed or removed
  .index/memory.sqlite             search cache; safe to delete
  .models/                         the embedding model, if it was downloaded rather than bundled
```

Each file covers one subject. Its frontmatter has `name` (the file's stem) and
a one-line `description` that says when to read it. The body is plain bullets.
Every write the plugin makes also stamps `edited_at` (UTC). Edits you make by
hand don't.

### Projects

Buckets map to Hermes's own Projects: named workspaces with one or more
folders, kept per profile in `$HERMES_HOME/projects.db`. You create them with
`hermes project create`, the desktop sidebar, or the agent's `desktop_project`
tool. The plugin has no project settings of its own.

A session belongs to the project that owns its working directory. That's the
same test Hermes uses: the innermost project folder containing it. The working
directory is, first found wins:

1. the workspace a gateway pins for the turn;
2. the `cwd` Hermes starts the session with;
3. the agent's working directory (`terminal.cwd`, or wherever you ran `hermes`).

It's checked on every tool call, so moving a chat into another project takes
effect immediately. A session outside every project is **unscoped**, and
`global/` is the only bucket it reads by default.

The project's slug names its bucket, `memories/<slug>/`. An `_` in the slug
becomes `-`. Hermes never changes a slug, so the mapping is stable. To attach
an existing bucket to a project, create the project with that slug:

```sh
hermes project create "Home server" ~/src/home-server --slug home-server
```

A project session reads `global/` and its own bucket by default. It reads
other buckets only when asked. What a session may write:

| Session | `global/` | Own bucket | Other buckets |
|---|---|---|---|
| Unscoped | full | — | add only |
| Project | full | full | add only |
| Read-only (cron, subagents, flushes, `readonly: true`) | none | none | none |

**Full** is every write tool. **Add only** is `memory_append` to an existing
file and `memory_write` with `if_version: "new"` to create one. Editing,
overwriting and deleting another project's files are refused, and the error
names both ways to add. Those stay with that project's own
sessions. A cron job under `cron_writes: true` writes like any other session.

"Other buckets" are the buckets on disk plus every Hermes project, including
ones with no memory yet. A write anywhere else is refused; the agent is told
to save a fact about any other project in `global/topics/<name>.md`.

A write into another project's bucket records where it came from in the
file's `sources` frontmatter, as `<platform>@<project>`. `tui@ex-luna` is a
TUI session in the `ex-luna` project; `tui@*` is an unscoped TUI session (`*`
can't be a project slug). Writes inside a session's own scope record only the
platform. Nothing undoes a cross-project write; the tag is there so you can
find what another session added.

`hermes memory-buckets status` lists buckets that no Hermes project uses yet;
no session is scoped to those.

### What the agent gets

- **A prompt block, frozen per session.** It holds the usage rules,
  `global/profile.md` and `global/preferences.md` in full, and, in a project
  session, the project index. Then come a listing of every other file and
  the other projects it can add to. It's capped at 12,000 characters.
- **The project index** is built when the session starts, never stored. It
  shows the Hermes project's folders, plus its name and description when
  they add something. Then come the bucket's `profile.md` and
  `preferences.md` in full. A bucket with no profile yet gets a line telling
  the agent to create one.
- **Seven tools:**
  - `memory_list`, `memory_read` and `memory_search`
  - `memory_write`, `memory_str_replace`, `memory_append` and `memory_delete`

  Every write is a compare-and-swap. On a conflict the tool returns the
  file's current content, so the agent can merge and retry.
- **A status line** when the agent writes into another project's bucket:
  `📝 Saved to <path>`. The classic CLI and the terminal TUI show it. The
  desktop app, gateways (Telegram, Discord and so on) and quiet or one-shot
  runs show nothing, and the agent isn't asked to tell you instead.
- **Prefetch hints** each turn. These are the paths of relevant files, never
  their content.
- **A periodic reminder** to file durable facts.
- **The `sort-imports` skill,** which sorts imported memory (see
  [Importing built-in memory](#importing-built-in-memory)). The plugin has no
  slash commands of its own; `/sort-imports` comes from the skill.

### Embeddings

The embedding model is
[potion-retrieval-32M](https://huggingface.co/minishlab/potion-retrieval-32M)
(MinishLab, MIT). It's a Model2Vec static model: a transformer distilled into
a lookup table. Embedding a text means tokenising, averaging and normalising,
all in plain stdlib Python. The weights are memory-mapped and shared by every
process, so loading is instant, and it embeds about 1,000 characters in 5 ms.
It's the only model the plugin supports.

Where the model comes from:

| Install | Model |
|---|---|
| pip / wheel | Bundled at build time. The build hook `hatch_build.py` fetches the pinned files, checks their sha256 and converts them to float16. It caches the result in `~/.cache/memory-buckets` (or `$XDG_CACHE_HOME`). `MEMORY_BUCKETS_BUNDLE_MODEL=0` skips it; editable installs never bundle it. |
| Nix | Bundled at build time (`nix/model.nix`), with the same files. |
| Plugin directory (including `hermes plugins install`) | Downloaded into `$HERMES_HOME/memory-buckets/.models/` in the background on first use. `hermes memory-buckets fetch-model` downloads it up front. |

`embeddings.local_model_dir` uses a copy of the model from elsewhere.
`embeddings.auto_download: false` stops the background download.
`embeddings.backend: off` turns embeddings off, which leaves keyword search
only and no prefetch hints.

The prefetch threshold defaults to 0.27, a measured value. Relevant notes
scored a median of 0.37, and unrelated ones stayed under 0.15 for 95% of
cases. `hermes memory-buckets hints "<query>"` shows the real scores for your
store.

## Importing built-in memory

`global/inbox.md` holds imported memory that's waiting to be sorted. It stays
when it's empty, and you can edit it by hand: adding an entry fills in
missing frontmatter. Two things add to it:

- `hermes memory-buckets import`, which copies in built-in memory's
  `MEMORY.md` and `USER.md` entries;
- built-in memory writes, mirrored there while built-in memory is still on.

Nothing else does. The agent files what it learns in conversation itself, and
`memory_append` to the inbox is refused. An inbox from 0.1.0 may still have
`added in conversation` sections; they're sorted like the rest.

**To sort it**, run `/sort-imports` or ask the agent to sort your imported
memory. It's sorted only when you ask.

- **General** facts go into `global/`.
- **Project** facts are added straight to their project's bucket. Existing
  files there are added to, never edited. Nothing waits for you to apply it.
- A fact for a project with no bucket and no Hermes project goes into
  `global/topics/<name>.md`.
- A fact the agent can't place goes into `global/`, and its reply lists those
  for you to check.
- Each line is removed from the inbox once its fact is filed. Stale and
  already-saved entries are dropped, and the reply says which.

Back up the store first. `import` and `import-prompt` print the command, and
a `diff` to review the result with.

**`/sort-imports` needs one line of config.** Hermes only makes slash commands
from skills in its skill directories, never from a plugin's bundled skills:

```yaml
skills:
  external_dirs:
    - plugins/memory-buckets/skills   # plugin directory, relative to HERMES_HOME
    # pip: <site-packages>/memory_buckets/skills
    # Nix: see the Nix install above
```

Without it, asking still works for a plugin-directory install: the prompt block
tells the agent to load the skill with `skill_view`. pip and Nix installs can't
do that, because Hermes prunes the bundled skills of a pip-installed provider.
There, either add the line or paste the prompt that
`hermes memory-buckets import-prompt` prints.

Don't save the sort prompt as a skill of your own. That copy goes stale when
the plugin's instructions change.

## Reference

### Commands

`hermes memory-buckets` is available while `memory.provider` is `memory-buckets`.

```
status                       store, index, embeddings, Hermes config, buckets without a project
diagnose [--platform P]      why an agent would or wouldn't see the memory tools
lint                         bad paths, frontmatter, duplicate names, broken [[links]]
migrate [--dry-run]          update a store written by an older version (also runs on startup)
reindex [--embed]            rebuild the search cache
search QUERY [--prefix P]    search the whole store (--limit N, --json)
hints QUERY [--threshold T]  raw similarities, to tune prefetch_min_similarity
fetch-model [--force]        download the embedding model now
import [--from HERMES_HOME]  append built-in MEMORY.md/USER.md entries to global/inbox.md
import-prompt                print the prompt that sorts the imported memory
```

### Configuration

Every key is optional, and all of them go under `plugins.memory-buckets` in
`config.yaml`.

| Key | Default | Effect |
|---|---|---|
| `readonly` | `false` | no session writes memory |
| `cron_writes` | `false` | let cron jobs write (cron, subagent and flush contexts are otherwise read-only) |
| `nudge_interval` | `10` | user turns between reminders to file durable facts; `0` turns them off |
| `snapshot_max_chars` | `12000` | cap on the prompt block |
| `prefetch_min_similarity` | `0.27` | how close a file must be to be hinted |
| `prefetch_max_hints` | `4` | hints per turn |
| `search_limit` | `8` | `memory_search` results when the agent doesn't ask for a number |
| `project_boost` | `1.3` | search weight for the session's own bucket |
| `store_path` | `$HERMES_HOME/memory-buckets` | where the store lives |
| `embeddings.backend` | `local` | `off`: keyword search only |
| `embeddings.auto_download` | `true` | download the model in the background when it isn't bundled |
| `embeddings.local_model_dir` | | use the model in this directory |

These environment variables override the config: `MEMORY_BUCKETS_STORE`,
`MEMORY_BUCKETS_EMBEDDINGS_BACKEND`, `MEMORY_BUCKETS_EMBEDDINGS_AUTO_DOWNLOAD`
and `MEMORY_BUCKETS_MODEL_DIR`. `MEMORY_BUCKETS_TRACE=1` logs every hook call
Hermes makes.

## Troubleshooting

**The agent doesn't know the memory tools.** Hermes picks the memory provider
when it builds an agent. A chat that existed before you set `memory.provider`
has no memory tools, and nothing in the chat says why. Start a new session
(`/new`) or restart the gateway. If a new session still can't see the tools,
run `hermes memory-buckets diagnose`. It checks each platform's toolsets the
way Hermes does.

**The tools are withheld on one platform.** Keep the `memory` toolset enabled
on every platform. Without it, Hermes withholds provider tools and the prompt
block.

**`hermes plugins doctor memory-buckets` says `0 tool(s), 0 hook(s)`.** That's
expected. The doctor counts general-plugin registrations, but a memory
provider's tools arrive through Hermes's memory manager. `hermes memory-buckets
status` should report 7 memory tools.

## Migrating

### From an older version

The plugin updates the store itself when a session starts. Read-only
sessions (cron, subagents, flushes, and every session under `readonly: true`)
leave it for the next writable one; with `readonly: true`, run `migrate`
yourself. Before it changes or removes a file, it keeps the original under
`_backup/<version>/`. `hermes memory-buckets migrate --dry-run` shows what it
would do, `migrate` runs it, and `status` reports anything still waiting. A
file it can't read or parse is left alone and reported; the rest still migrate.

- **0.1.0:** `<project>/index.md` is gone, because the project index is now
  generated. A leftover `index.md` becomes the project's `profile.md`. If the
  project already has one, the body of `index.md` is appended to it as it is,
  under `## Moved from index.md`, so check for repeated lines afterwards. If
  either file can't be parsed, or the profile would go over the size cap, the
  migration leaves both files alone and says so. The plugin owns the inbox's
  description, so any other description becomes the current one (the original
  is kept in the backup).
- **0.2.0:** proposals are gone, and so is `_pending/`. Each proposal in it is
  copied to `_backup/0.2.0/_pending/`, then the folder is removed. None is
  applied: you never confirmed them, and their lines are still in the inbox,
  so the next `/sort-imports` files them. A proposal whose copy fails stays
  where it is, with `_pending/`, and `migrate` says so. The inbox's
  description becomes "Imported memory waiting to be sorted", and its lines
  stay as they are.

  `write_policy` is gone too. **If your config sets `write_policy: confined`,
  upgrading widens what sessions may write:** project sessions now write
  `global/`, and every writable session can add to other projects' buckets
  (see [Projects](#projects)). The setting is ignored, with a warning in the
  log; delete it.

### From built-in memory

1. Install the plugin and set `memory.provider: memory-buckets`, but leave
   built-in memory on for now. Its writes are mirrored into `global/inbox.md`.
2. Run `hermes memory-buckets import`. Add `--from <HERMES_HOME>` to import
   another instance's memory. It prints a backup command and the sorting
   prompt; `import-prompt` prints them again.
3. Back up the store. Then, in a new chat outside any project,
   [sort the imported memory](#importing-built-in-memory).
4. Review with `diff -ru <backup> <store>`.
5. Turn built-in memory off (`memory.memory_enabled: false`,
   `memory.user_profile_enabled: false`).

## Development

In the devshell (`direnv allow` or `nix develop`), `sync` creates the venv with
the devshell's Python and `check` runs the tests. They're `unittest` tests,
run under pytest. Two variables turn on more of them:

- `HERMES_PYTHON=<Hermes's interpreter>` adds integration tests that drive real
  Hermes code against a throwaway `HERMES_HOME`, loading the plugin both as a
  directory and as a pip install.
- `MEMORY_BUCKETS_MODEL_DIR=<a copy of the model>` adds the real-model tests.

`nix flake check` builds the package with the model bundled and runs the
tests in the build sandbox. That includes the real-model tests and, in the
`tokenizer` check, token-for-token parity with Hugging Face's tokenizers.

`tests/smoke.sh` is a manual end-to-end chat test on a throwaway profile. It
needs a test config you prepare, and it refuses anything under `~/.hermes`.
