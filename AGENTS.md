# AGENTS.md

Hermes memory-provider plugin: one Markdown file per subject under a bucket, hybrid
retrieval (SQLite FTS5 keyword + Model2Vec static embeddings, pure Python), shipped as a
Hermes plugin directory and a pip entry point. Repo root doubles as the plugin directory
(`plugin.yaml`, `__init__.py` shim, `skills/`); `pyproject.toml` forces the wheel build to
copy those in. Runtime dependencies are `[]` by policy - stdlib only, ever. Python 3.11 is
the floor.

## Dev environment

- Nix devshell via `nix develop` (Python 3.14, uv, ruff; sets
  `UV_PROJECT_ENVIRONMENT`/`UV_PYTHON_PREFERENCE`/`LD_LIBRARY_PATH`).
- Devshell commands: `sync` (uv sync), `check` (pytest), `check-windows` (tests/wine.sh,
  x86_64 Linux only), `lint` (ruff check .), `run` (uv run ...).
- Without Nix: Python >= 3.11 with sqlite3 FTS5, then `uv sync`, `uv run pytest tests/`,
  `uvx ruff check .`.

## Build & test

- `check` / `uv run pytest tests/` - full suite, hermetic by default.
- `HERMES_PYTHON=<hermes-agent-env>/bin/python MEMORY_BUCKETS_MODEL_DIR=<potion-retrieval-32M
  dir> uv run pytest tests/` - adds Hermes integration tests and real-model tests.
- `MEMORY_BUCKETS_MODEL_DIR=<model-dir> uv run python -m tests.bench_retrieval` -
  retrieval benchmark.
- `nix flake check` - builds the wheel with the bundled model and runs the suite in the
  sandbox, including tokenizer parity against Hugging Face `tokenizers`.
- CI lives on the GitHub mirror (`.github/workflows/`): test.yml (Linux/macOS),
  windows.yml (x64 3.11/3.14, arm64 3.14, HERMES_REF pin), hermes-main.yml (weekly
  integration run against Hermes main).

## Module roles

- `provider.py`: Hermes lifecycle hooks (register, save/compress nudges). `tools.py`: the
  seven `memory_*` tools. `snapshot.py`: system-prompt block and prefetch hints.
- `store.py`: Markdown files, CAS writes, flock, permissions. `index.py`: SQLite FTS5
  cache, chunking, schema versions. `embeddings.py`/`static_model.py`: Model2Vec
  potion-retrieval-32M inference in pure Python.
- `inbox.py` (import-only inbox), `projects.py` (Hermes projects.db lookup),
  `migrate.py`, `config.py`, `scopes.py`, `credentials.py`, `frontmatter.py`,
  `security` surface (threat scanning) spans credentials/permissions code.

## Conventions

- Conventional Commits: `<type>(<scope>): lowercase imperative summary`; branches
  `<type>/<short-slug>` from `main`. Scopes: store, index, snapshot, tools, provider,
  cli, config, security, nix.
- One concern per pull request. Fold fixups into the relevant commit; no
  force-push under review.
- Comments explain why (Windows locking/ACL quirks, cases that bit someone), not what.
- Added lines are ASCII only: no em/en dashes, smart quotes, or emoji, anywhere
  including commits and docs. British spelling in prose (behaviour, licence).
- Lint is `ruff check .` with pinned rules (E4/E7/E9/F, line-length 140). Do not run
  `ruff format`; the codebase keeps wide lines and is not auto-formatted.
- Tests come with the change: fixes need a test that fails on main without the fix;
  use fixtures in `tests/helpers.py` (`StoreCase`, `FakeEmbedder`, `fake_hermes`,
  `run_hermes_cli`).

## Pitfalls

- Never read or copy the live store at `~/.hermes/memory-buckets/`; tests use temp or
  synthetic stores only, and Ivy runs anything against the real store herself.
- Never put real facts about the maintainer (identity, courses, hardware, projects)
  into test files or benchmarks - use fictional personas.
- Keep the checkout directory name not-a-valid-Python-identifier (e.g. `memory-buckets`,
  not `repo` or `src`): the root `__init__.py` shim makes multiprocessing-spawn tests
  import test modules under the directory name.
- `tests/conftest.py` clears every shell `MEMORY_BUCKETS_*` variable except
  `MEMORY_BUCKETS_MODEL_DIR`, `MEMORY_BUCKETS_MODEL_F32`, and
  `MEMORY_BUCKETS_REFERENCE_TOKENIZER`, and sets auto-download off, so day-to-day
  config cannot break the suite.
- `CHANGELOG.md` is maintainer-updated at release; leave it alone unless asked.
  `README.md` stays untouched unless the change is a critical facet.
- `memory_buckets/model/` is produced by the build hook (`hatch_build.py`), never a
  source file; don't hand-edit or commit it. `MEMORY_BUCKETS_BUNDLE_MODEL=0` skips it.
- Windows is a first-class target: files written atomically in binary mode, text I/O
  names `encoding="utf-8"`, POSIX bits gated on `store.private_modes()`. Touching
  paths, locking, or permissions means running `check-windows`.
