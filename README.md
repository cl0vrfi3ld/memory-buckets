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
      skills.external_dirs = [ "${memory-buckets}/${memory-buckets.skillsPath}" ];  # /sort-inbox
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
    <project>/  index.md profile.md preferences.md   topics/ areas/ people/
  _pending/<id>.json               staged project facts, waiting for you to apply
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
effect immediately. A session outside every project is **unscoped** and reads
and writes `global/` only.

The project's slug names its bucket, `memories/<slug>/`. An `_` in the slug
becomes `-`. Hermes never changes a slug, so the mapping is stable. To attach
an existing bucket to a project, create the project with that slug:

```sh
hermes project create "Home server" ~/src/home-server --slug home-server
```

A project session reads `global/` and its own bucket by default. It writes to
both of them (`write_policy: shared`), or only to its own bucket (`confined`).
It reads other buckets only when asked. `hermes memory-buckets status` lists
buckets that no Hermes project uses yet; no session is scoped to those.

### What the agent gets

- **A prompt block, frozen per session.** It holds the usage rules,
  `global/profile.md` and `global/preferences.md` in full, the project's
  `index.md`, and a listing of every other file. It's capped at 12,000
  characters.
- **Eight tools:**
  - `memory_list`, `memory_read` and `memory_search`
  - `memory_write`, `memory_str_replace`, `memory_append` and `memory_delete`
  - `memory_propose`, which stages project facts during
    [inbox sorting](#sorting-the-inbox)

  Every write is a compare-and-swap. On a conflict the tool returns the
  file's current content, so the agent can merge and retry.
- **Prefetch hints** each turn. These are the paths of relevant files, never
  their content.
- **A periodic reminder** to file durable facts.
- **The `sort-inbox` skill** and the `/memory-pending`, `/memory-apply` and
  `/memory-reject` slash commands.

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

## Sorting the inbox

`global/inbox.md` collects memory that's waiting to be sorted:

- entries from `hermes memory-buckets import`;
- built-in memory writes, mirrored there while built-in memory is still on;
- project facts parked by chats outside a project.

**To start**, run `/sort-inbox` or ask the agent to sort your memory inbox.

- The agent files **general** facts into `global/` itself.
- It **proposes** project facts with `memory_propose`, one proposal per
  project. When no existing project fits, it proposes a new one: a
  `<id>/profile.md` plus the facts. Nothing under `memories/` changes yet.

**You apply each proposal yourself:**

```
/memory-pending            list proposals (/memory-pending <id> shows one)
/memory-apply <id>         write its files and remove its lines from the inbox
/memory-reject <id>        discard it; its lines stay in the inbox
```

The agent can't send slash commands, so only you can apply a proposal.
`hermes memory-buckets pending`, `apply` and `reject` do the same from a
terminal; `apply` asks for confirmation. A proposal that's gone stale fails
whole, without writing anything. A new bucket is used only once a Hermes
project has its slug, and `/memory-apply` reminds you of that.

**`/sort-inbox` needs one line of config.** Hermes only makes slash commands
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
There, either add the line or paste the output of
`hermes memory-buckets sort-prompt`.

Don't save the sort prompt as a skill of your own. That copy goes stale when
the plugin's instructions change.

## Reference

### Commands

`hermes memory-buckets` is available while `memory.provider` is `memory-buckets`.

```
status                       store, index, embeddings, Hermes config, buckets without a project
diagnose [--platform P]      why an agent would or wouldn't see the memory tools
lint                         bad paths, frontmatter, duplicate names, broken [[links]]
reindex [--embed]            rebuild the search cache
search QUERY [--prefix P]    search the whole store (--limit N, --json)
hints QUERY [--threshold T]  raw similarities, to tune prefetch_min_similarity
fetch-model [--force]        download the embedding model now
import [--from HERMES_HOME]  append built-in MEMORY.md/USER.md entries to global/inbox.md
sort-prompt                  print the inbox-sorting prompt
pending [ID]                 list proposals, or show one
apply ID                     apply one proposal (asks for confirmation; needs a terminal)
reject ID                    discard one proposal
```

### Configuration

Every key is optional, and all of them go under `plugins.memory-buckets` in
`config.yaml`.

| Key | Default | Effect |
|---|---|---|
| `write_policy` | `shared` | `confined`: project sessions write only their own bucket |
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
status` should report 8 memory tools.

## Migrating

### From built-in memory

1. Install the plugin and set `memory.provider: memory-buckets`, but leave
   built-in memory on for now. Its writes are mirrored into `global/inbox.md`.
2. Run `hermes memory-buckets import`. Add `--from <HERMES_HOME>` to import
   another instance's memory. It prints a backup command and the sorting
   prompt; `sort-prompt` prints them again.
3. Back up the store. Then, in a new chat outside any project,
   [sort the inbox](#sorting-the-inbox) and apply or reject each proposal.
4. Review with `diff -ru <backup> <store>`, then delete `global/inbox.md`.
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
