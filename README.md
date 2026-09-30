# memory-buckets

A Hermes Agent memory provider that stores memory the way Claude does. It keeps
one Markdown file per subject, in categories, in buckets: a global one, and one
per [Hermes project](#projects). Files are read on demand and searched with FTS5
plus built-in embeddings. Its commands are `hermes memory-buckets <command>`.

Stdlib-only Python: no runtime dependencies, ever.

## Store layout

```
$HERMES_HOME/memory-buckets/        plain directory (not a git repo)
  memories/                        every memory file; also opens as an Obsidian vault
    global/     profile.md preferences.md inbox.md  topics/ areas/ people/
    <project>/  index.md profile.md preferences.md   topics/ areas/ people/   (the Hermes project's slug)
  _pending/<id>.json               project facts from inbox sorting, waiting for you to apply
  .index/memory.sqlite             search cache; safe to delete
```

Every file has frontmatter with `name` (the file's stem) and a one-line
`description` saying when to read it. Every write the plugin makes also sets
`edited_at` (UTC, e.g. `2026-09-27T14:03:12Z`); edits made by hand, in Obsidian
or an editor, don't. The body is plain bullets.

## Install

Pick one. Hermes finds it either way, and you activate it with
`memory.provider: memory-buckets`.

- **Plugin directory:** this repo is the plugin directory. Clone or link it to
  `$HERMES_HOME/plugins/memory-buckets/` (the directory name must be
  `memory-buckets`):
  ```sh
  ln -s ~/Projects/hermes-memory-buckets ~/.hermes/plugins/memory-buckets
  ```
  The code is in `memory_buckets/`; the root `__init__.py`, `cli.py` and
  `plugin.yaml` are what Hermes looks for in a plugin directory.
- **pip** (into the Python environment Hermes runs in):
  ```sh
  pip install .
  ```
  Hermes discovers it through the `hermes_agent.memory_providers` entry point.
  Building the wheel bundles the embedding model (see [Embeddings](#embeddings)).
- **Nix** (Hermes's NixOS or Home Manager module): see [Nix](#nix).

Then configure Hermes (none of the install methods do this for you):

```yaml
memory:
  provider: memory-buckets
  # once your built-in memory is imported and sorted:
  # memory_enabled: false
  # user_profile_enabled: false
plugins:
  memory-buckets:
    embeddings:                # optional; without it, search is keyword-only
      base_url: http://homelab:11434/v1
      model: <embedding model>
    # nudge_interval: 10       # user turns between "file what you've learnt" reminders; 0 = off
    # write_policy: shared     # or confined: project sessions can't write global files
    # cron_writes: false
```

**After setting `memory.provider`, start a new session (`/new`) or restart the
gateway.** Hermes picks the provider when it builds an agent, so a chat whose
agent existed before the change has no memory tools at all, and nothing in the
chat says why. This is the usual reason an agent "doesn't know" the memory
tools. If a new session still can't see them, run `hermes memory-buckets diagnose`.

`hermes plugins doctor memory-buckets` reports `0 tool(s), 0 hook(s)`. That's
expected: the doctor counts general-plugin registrations, but a memory
provider's tools arrive through Hermes's memory manager when an agent starts.
To see them, set the provider, then run `hermes memory-buckets status`: it
should report 8 memory tools. If a chat can't see them, `hermes memory-buckets diagnose`
checks each platform's toolsets the way Hermes does.

An API key for the endpoint goes in `MEMORY_BUCKETS_EMBEDDINGS_API_KEY`, for
example in Hermes's `.env`. Keep the `memory` toolset enabled on every
platform: Hermes withholds provider tools and the prompt block without it.

## Nix

`nix/package.nix` builds the plugin as a Python package. Add it to Hermes with
`extraPythonPackages` (Hermes's NixOS and Home Manager modules both have it),
built with Hermes's own interpreter, as
[Distribute for NixOS](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins#distribute-for-nixos)
describes:

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
      # for /sort-inbox (see Sorting the inbox)
      skills.external_dirs = [ "${memory-buckets}/${memory-buckets.skillsPath}" ];
    };
  };
}
```

with the flake as an input:

```nix
inputs.memory-buckets = {
  url = "github:<owner>/hermes-memory-buckets";
  flake = false;  # only nix/package.nix is used
};
```

Use `extraPythonPackages`, not `extraPlugins`: `extraPlugins` links a directory
in as `plugins/nix-managed-<name>`, and Hermes names a memory provider after its
directory, so it would be `nix-managed-memory-buckets`. As with any pip install,
the `memory-buckets:sort-inbox` skill can't be loaded by name; the
`external_dirs` line is what makes `/sort-inbox` work.

The package bundles the embedding model (`nix/model.nix`: the pinned upstream
files, converted to float16), so nothing downloads at runtime. The package is
about 63 MB. Building it runs the tests, including the real-model ones.

## Embeddings

**Built in, nothing to configure.** The plugin ships with
[potion-retrieval-32M](https://huggingface.co/minishlab/potion-retrieval-32M)
(MinishLab, MIT). It's a Model2Vec *static* model: a transformer distilled into
a lookup table, so embedding text is tokenise → average → normalise, in plain
stdlib Python. There's no server, no dependency and no GPU. Loading is instant,
because the 63 MB of weights are memory-mapped and shared by every process.
It embeds about 1,000 characters in 5 ms.

Where the model comes from:

- **Wheels** (`pip install .`, `uv build`) bundle it at `memory_buckets/model/`.
  A build hook (`hatch_build.py`) fetches the pinned files from Hugging Face,
  checks their sha256, and converts them to float16. The result is cached in
  `~/.cache/memory-buckets` (or `$XDG_CACHE_HOME`), so each machine fetches it
  once. The wheel is about 60 MB. `MEMORY_BUCKETS_BUNDLE_MODEL=0` builds
  without it; editable installs (`uv sync`) never bundle it.
- **The Nix package** bundles the same files (`nix/model.nix`).
- **The plugin directory** (a checkout) has none. On first use the plugin
  downloads the pinned, hash-checked model into
  `$HERMES_HOME/memory-buckets/.models/` in the background, and search is
  keyword-only until it arrives. `hermes memory-buckets fetch-model` does the
  same download up front, and `embeddings.auto_download: false` turns the
  background download off.

`embeddings.local_model_dir` points at a copy elsewhere.

The prefetch threshold defaults to 0.27 for this model. That's measured: relevant
notes scored a median of 0.37, and unrelated ones stayed under 0.15 for 95% of
cases. `hermes memory-buckets hints "<query>"` shows the real scores for your store.

### A different model or an embeddings server

A static model is good at synonyms and paraphrase, but less subtle than a
transformer. If you'd rather use a transformer model, point the plugin at any
OpenAI-compatible `/v1/embeddings` endpoint, and it takes over automatically:

```yaml
plugins:
  memory-buckets:
    embeddings:
      base_url: http://homelab:11434/v1      # e.g. Ollama with nomic-embed-text
      model: nomic-embed-text
      query_prefix: "search_query: "         # for models that expect task prefixes
      document_prefix: "search_document: "
      # backend: auto | local | http | off   (auto = http when base_url is set)
```

On NixOS, Ollama is `services.ollama = { enable = true; loadModels = [ "nomic-embed-text" ]; };`.
Remote calls time out after 2 seconds and back off for 60 after a failure, so a
sleeping server never blocks a chat. Changing the backend, model or document
prefix re-embeds the store automatically.

## Projects

Project buckets come from Hermes's own Projects: named workspaces
with one or more folders, kept per profile in `$HERMES_HOME/projects.db`. Make
them with `hermes project create`, the desktop sidebar, or the agent's
`desktop_project` tool. The plugin has no project settings of its own.

A session is in the project that owns its working directory, the same test
Hermes uses (the innermost project folder containing it). The working
directory is the session's workspace as a gateway pins it for the turn, else
the `cwd` Hermes starts the session with, else the agent's working directory
(`terminal.cwd`, or wherever you ran `hermes`). It's checked on every tool call,
so moving a chat into another project applies straight away. A session outside
every project is **unscoped**: it reads and writes `global/` only.

The project's slug names its bucket, `memories/<slug>/` (an `_` in the slug
becomes `-`). Slugs never change once a project exists. To give an existing
bucket a project, create one with that slug:

```sh
hermes project create "Home server" ~/src/home-server --slug home-server
```

A project session reads `global/` and its own bucket by default, and writes
both (`write_policy: shared`) or only its bucket (`confined`). Other buckets
are read only when asked for. `hermes memory-buckets status` lists buckets that
no Hermes project uses yet; no session is scoped to those.

## What the agent gets

- A frozen per-session prompt block: usage rules, `global/profile.md` and
  `global/preferences.md` in full, the project's `index.md`, and a listing of
  the other files. Capped at 12,000 characters (`snapshot_max_chars`).
- Eight tools: `memory_list`, `memory_read`, `memory_search`, `memory_write`,
  `memory_str_replace`, `memory_append`, `memory_delete`, and `memory_propose`
  (stages project facts from the inbox; see below). Every write is a
  compare-and-swap, and a conflict returns the current content so the agent can
  merge and retry.
- The `memory-buckets:sort-inbox` skill, and the `/memory-pending`,
  `/memory-apply` and `/memory-reject` slash commands.
- Prefetch hints (paths only, never content), from the built-in embedding
  model, plus a periodic reminder to file durable facts.

## `hermes memory-buckets`

Available while `memory.provider` is `memory-buckets`.

```
hermes memory-buckets <command>
  status [--offline]           store, index, embeddings, Hermes config, and which buckets have a project
  diagnose [--platform P]      why an agent would or wouldn't see the memory tools (inside Hermes only)
  lint                         bad paths, frontmatter, duplicate names, broken [[links]]
  reindex [--embed]            rebuild the search cache
  search QUERY [--prefix P]    search the whole store
  hints QUERY [--threshold T]  raw similarities, to tune prefetch_min_similarity for your model
  fetch-model                  download the built-in embedding model now (otherwise automatic)
  import [--from HERMES_HOME]  append built-in MEMORY.md/USER.md entries to global/inbox.md
  sort-prompt                  print the prompt that asks the agent to sort the inbox
  pending [ID]                 list proposals from inbox sorting, or show one
  apply ID                     commit one proposal (asks for confirmation; needs a terminal)
  reject ID                    discard one proposal
```

Config comes from `config.yaml` (`plugins.memory-buckets`). `MEMORY_BUCKETS_*`
environment variables override it: `_EMBEDDINGS_BASE_URL`, `_EMBEDDINGS_MODEL`,
`_EMBEDDINGS_BACKEND`, `_EMBEDDINGS_AUTO_DOWNLOAD`, `_MODEL_DIR`, and `_STORE`
(the store directory).

## Sorting the inbox

`global/inbox.md` collects memory waiting to be sorted: `hermes memory-buckets import`
output, built-in memory writes mirrored while built-in memory is still on, and
project facts parked from chats outside a project.

**Start it** with `/sort-inbox`, or just ask the agent to sort your memory
inbox. `/sort-inbox` needs one line of Hermes config, because Hermes only makes
slash commands from skills in its skill directories, never from a plugin's
bundled skills:

```yaml
skills:
  external_dirs:
    - plugins/memory-buckets/skills          # plugin directory (relative to HERMES_HOME)
    # pip install: <site-packages>/memory_buckets/skills
```

Without it, asking still works: the prompt block tells the agent to load the
`memory-buckets:sort-inbox` skill with `skill_view`.

**What the agent does:**

- files **general** facts into `global/` itself;
- **proposes** project facts with `memory_propose`, one proposal per project,
  and proposes a new project (`<id>/profile.md` plus its facts) when no
  existing one fits. Nothing under `memories/` changes yet. A new bucket is
  only used once a Hermes project has its slug; `/memory-apply` reminds you.

**You commit each proposal yourself, one at a time:**

```
/memory-pending            list proposals (/memory-pending <id> shows one)
/memory-apply <id>         write its files and remove its lines from the inbox
/memory-reject <id>        discard it; its lines stay in the inbox
```

The agent can't send slash commands, so only you can apply. From a terminal,
`hermes memory-buckets pending / apply / reject` do the same; `apply` asks for
confirmation and refuses to run without an interactive terminal. A proposal
that's gone stale (say the project was created in the meantime) fails whole,
without writing anything.

**Don't save the sort prompt as a skill of your own.** Hermes can turn a pasted
prompt into a local skill (`~/.hermes/skills/...`), and that copy goes stale
when the plugin's instructions change. Use `/sort-inbox` or ask.

**pip installs without `external_dirs`:** Hermes can't serve a pip-installed
memory provider's bundled skills (it namespaces them by the package directory,
`memory_buckets`, then prunes them as not belonging to the active provider).
Add the `external_dirs` line above, or paste the output of
`hermes memory-buckets sort-prompt`; the prompt block says to ask you for it.

## Migrating from built-in memory

1. Install, set `memory.provider: memory-buckets`, and leave built-in memory on
   for now. Its writes are mirrored into `global/inbox.md`.
2. `hermes memory-buckets import` (and `--from <old HERMES_HOME>` for another instance).
   It ends by printing the backup command and the sorting prompt.
   `hermes memory-buckets sort-prompt` prints them again.
3. Copy the store somewhere safe, then in a new chat that isn't in a project,
   ask the agent to sort your memory inbox (see above), and apply or reject
   each project proposal.
4. Review with `diff -ru <copy> <store>`, then delete `global/inbox.md`.
5. Turn built-in memory off.

## Migrating from better-memory

This plugin was `better-memory`, in the Better Hermes Client repo. Everything
was renamed, and session bindings (`_bindings/`, `bhc-memory bind`) gave way to
[Hermes projects](#projects). To move an existing install:

1. Move the store: `mv ~/.hermes/better-memory ~/.hermes/memory-buckets`. The
   old `_bindings/` directory inside it is no longer read; delete it if you like.
2. Swap the plugin: remove `~/.hermes/plugins/better-memory` and install this
   one as above.
3. In `config.yaml`: `memory.provider: memory-buckets`; rename
   `plugins.better-memory` to `plugins.memory-buckets`; point
   `skills.external_dirs` at `plugins/memory-buckets/skills`. Rename any
   `BETTER_MEMORY_*` variables in `.env` to `MEMORY_BUCKETS_*`.
4. For each project bucket, create a Hermes project with its name as the slug
   (see [Projects](#projects)). `hermes memory-buckets status` lists the ones
   still missing.
5. Start a new session, or restart the gateway.

## Development

The tests use `unittest` and run under pytest. In the devshell, `sync` then
`check` runs them all. Set `HERMES_PYTHON` to Hermes's interpreter to include
integration tests that drive real Hermes code against a throwaway
`HERMES_HOME`, loading the plugin as a directory and as a pip install.
`nix flake check` builds the package with the bundled model and runs the tests
in the build sandbox, including the real-model tests and, in the `tokenizer`
check, token-for-token parity with Hugging Face's tokenizers. `tests/smoke.sh` is a manual end-to-end chat test. It needs a test config you
prepare, and it refuses anything under `~/.hermes`.

Design: `02 Architecture/Memory Provider.md` and `05 Plan/MEM-1 Plan.md` in the
Better Hermes Client design vault.
