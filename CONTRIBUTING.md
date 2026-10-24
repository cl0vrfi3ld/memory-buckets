# Contributing

Thanks for looking. This document covers how to set up a development environment, the
repository's house rules, and the conventions for issues, commits, and pull requests.
How the plugin works is in the [README](README.md).

## Where things live

- Issues and pull requests: <https://git.cl0vr.co/cl0vr/memory-buckets>.
- The GitHub repository (<https://github.com/cl0vrfi3ld/memory-buckets>) is a read-only
  mirror. Its Actions workflows (`.github/workflows/test.yml`,
  `.github/workflows/windows.yml`, and `.github/workflows/hermes-main.yml`) run the
  Linux, macOS, and Windows test jobs on branches pushed to `git.cl0vr.co`.
- Security reports: do not post vulnerability details in a public issue. Open a short
  issue asking to report a security problem and the maintainer will arrange a private
  channel.

## Set up

Inside the Nix devshell (`direnv allow` or `nix develop`):

- `sync` creates or updates `.venv` with `uv sync`.
- `check` runs the test suite (`uv run pytest tests/`).
- `check-windows` runs the test suite on Windows Python under Wine (`tests/wine.sh`,
  x86_64 Linux).
- `lint` runs `ruff check .`.
- `run` executes a command inside the project venv (`uv run`).

Without Nix, use Python 3.10 or newer (with `sqlite3` compiled with FTS5) and `uv`:

```sh
uv sync
uv run pytest tests/
uvx ruff check .
```

Keep your checkout directory named `memory-buckets` (or another name that is not a valid
Python identifier). The repository root contains `__init__.py` (the Hermes
plugin-directory shim), so a checkout directory named `repo` or `src` causes
multiprocessing-spawn tests to import test modules under that package name and fail in
child processes.

Optional environment variables (`HERMES_PYTHON` for Hermes integration tests and
`MEMORY_BUCKETS_MODEL_DIR` for real-model embedding tests), the retrieval benchmark, and
the CI layout are covered under [Development](README.md#development) in the README.

## House rules

- **Standard library only.** `pyproject.toml` sets `dependencies = []` on purpose. Test
  and build tools belong in `[dependency-groups].dev` only. A runtime dependency needs a
  strong reason and a maintainer's approval before you write it.
- **Python 3.10 is the floor.** `requires-python = ">=3.10"` and Ruff's
  `target-version = "py310"` enforce this, and the Windows CI job exercises Python 3.10.
  Do not use newer syntax or standard library additions without a 3.10 fallback.
- **Tests come with the change.** Every bug fix includes a test that fails on `main`
  without the fix. Every new behaviour includes a test that pins it. Wire hand
  reproductions and new tests with the fixtures in `tests/helpers.py` (`StoreCase`,
  `FakeEmbedder`, `fake_hermes`, `run_hermes_cli`). `tests/conftest.py` clears every
  `MEMORY_BUCKETS_*` variable from your shell (apart from the real-model opt-ins
  `MEMORY_BUCKETS_MODEL_DIR`, `MEMORY_BUCKETS_MODEL_F32`, and
  `MEMORY_BUCKETS_REFERENCE_TOKENIZER`) and sets
  `MEMORY_BUCKETS_EMBEDDINGS_AUTO_DOWNLOAD=0`, so test runs stay hermetic and never
  touch `~/.hermes`.
- **Comments say why.** The code shows what it does. Write a comment when the reason is
  not obvious: a Windows file-locking or ACL quirk, a case that bit someone, or a limit
  that looks arbitrary.
- **British spelling and ASCII only.** Use British spelling (`normalise`, `behaviour`,
  `recognised`, `colour`) in code, comments, documentation, commit messages, and pull
  requests. Keep added lines ASCII only: no em dashes, en dashes, smart quotes, or
  emoji.
- **Lint is `ruff check .`, nothing more.** The rules (`E4`, `E7`, `E9`, `F`,
  `line-length = 140`) are pinned in `pyproject.toml` so every Ruff version checks the
  same thing. The codebase is not auto-formatted; do not run `ruff format` or reformat
  files you are not otherwise changing.
- **Windows counts.** Memory paths use `/`, files are written atomically in binary mode,
  text I/O names `encoding="utf-8"`, and POSIX permission bits (`0700` and `0600`) are
  gated on `store.private_modes()`. If you touch path handling, file locking, or
  permissions, run `check-windows` or check the Windows job on the GitHub mirror.
- **Maintainer-owned files.** `README.md` and `CHANGELOG.md` belong to the maintainer
  (`@cl0vr`) and are updated at release time. Do not edit them in feature or bug-fix
  pull requests unless the maintainer explicitly asks for that on the thread.

## Issues

Search open and closed issues and pull requests before filing a new one. When opening an
issue on <https://git.cl0vr.co/cl0vr/memory-buckets/issues>, pick the matching template
from `.github/ISSUE_TEMPLATE/`:

- **Bug report** (`.github/ISSUE_TEMPLATE/bug-report.md`): name the commit or version
  checked, give a minimal reproduction (commands, inputs, expected versus actual
  behaviour), outline a proposed fix if known, and list checkable "Done when" criteria.
- **Feature request** (`.github/ISSUE_TEMPLATE/feature-request.md`): state the problem
  or gap on `main`, describe the proposed behaviour and what is out of scope, list any
  prerequisite issues under "Depends on", and give checkable "Done when" criteria.

## Commits and pull requests

### Scope and branches

- **One concern per pull request.** Keep each pull request focused on a single issue, or
  one self-contained part of a multi-part issue. If an issue covers two independent
  changes, split them into separate branches and pull requests.
- **Branch naming.** Branch from `main` as `<user>/issue-<N>-<short-slug>` (or
  `fix/<short-slug>`, `feat/<short-slug>`).
- **Clean commit history.** Fold review or lint fixups into the relevant commit before
  opening the pull request rather than leaving "fix lint" or "drop README edits" commits
  at the tip. Do not force-push a branch that is already under review without maintainer
  approval.

### Commit messages and pull request titles

Commit messages and pull request titles follow
[Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <summary>
```

**Types:**

| Type | When to use |
|---|---|
| `feat` | New user-facing or plugin capability |
| `fix` | Bug fix |
| `test` | Adding or updating tests or benchmarks only |
| `docs` | Documentation or templates only |
| `refactor` | Code restructuring with no behaviour change |
| `perf` | Performance or memory improvement |
| `ci` | CI workflow changes |
| `build` | Build system, packaging (`pyproject.toml`, `hatch_build.py`, `flake.nix`, `nix/`) |
| `chore` | Maintenance and housekeeping |

**Scopes:**

Use the module or subsystem touched: `store`, `index`, `snapshot`, `tools`, `provider`,
`config`, `cli`, `frontmatter`, `embeddings`, `static_model`, `scopes`, `credentials`,
`migrate`, `inbox`, `projects`, `skills`, `hermes`, `security`, or `nix`. Omit `(scope)`
only when a change spans the whole repository.

**Summary and body rules:**

- Use lowercase imperative mood after the colon:
  `feat(tools): page results at the 48 KiB cap`, not `Paged` or `Pages`.
- Keep the subject line under 72 characters with no trailing full stop.
- Wrap commit body lines at 72 characters and explain why the change was made.
- Do not append `(#issue)` to a single-PR title, as Forgejo appends `(#PR)` on squash
  merge. Multi-part pull request titles may include `(#N, part 1 of M)` when
  disambiguating a split series.

### Linking issues and filling in the pull request body

Follow `.github/PULL_REQUEST_TEMPLATE.md` when opening a pull request against `main`:

1. **Issue reference:** Put `Fixes #N` (or `Closes #N`) at the top when merging the pull
   request completes the issue. When an issue is split across several pull requests, use
   `Part of #N (1/M). Refs #N.` instead so merging one part does not close the parent
   issue before the rest land.
2. **What changed:** Bullet the files and symbols touched and the reason for each change.
3. **Done when:** Map each acceptance criterion from the issue to the test or check that
   verifies it.
4. **Verification:** Record the commands you ran (`uv run pytest tests/`,
   `uvx ruff check .`), the pass and skip counts on `main` versus your branch,
   confirmation that new tests fail on bare `main` before the change, and anything you
   could not run locally (such as Windows or the real Hermes process).
5. **Notes (optional):** Call out non-obvious judgement calls, trade-offs, or follow-up
   items deliberately left out of scope.
