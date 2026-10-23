# Contributing

Thanks for looking. This page has the commands and the house rules. How the plugin works
is in the [README](README.md), and security reports go through [SECURITY.md](SECURITY.md),
not the public issue tracker.

## Where things live

- Issues and pull requests: <https://git.cl0vr.co/cl0vr/memory-buckets>.
- The GitHub repository is a mirror. Its Actions run the Linux, macOS and Windows test
  jobs on branches pushed to the main repository.

## Set up

Inside the Nix devshell (`direnv allow` or `nix develop`):

- `sync` creates or updates `.venv` with `uv sync`.
- `check` runs the test suite (`uv run pytest tests/`).
- `check-windows` runs it on Windows Python under Wine (`tests/wine.sh`, x86_64 Linux).
- `lint` runs `ruff check .`.
- `run` executes a command inside the project venv (`uv run`).

The extra test coverage that needs `HERMES_PYTHON` or `MEMORY_BUCKETS_MODEL_DIR`, and how
the benchmark and the Windows CI work, are under
[Development](README.md#development) in the README.

## House rules

- **Standard library only.** `pyproject.toml` has `dependencies = []` on purpose. Test and
  build tools can be dev dependencies. A runtime dependency needs a good reason and a
  maintainer's yes before you write it.
- **Python 3.10 is the floor.** `requires-python` and ruff's `target-version` say so, and
  the Windows job runs 3.10. Do not use newer syntax or standard library features
  without a fallback.
- **Tests come with the change.** A fix has a test that fails without it. A new behaviour
  has a test that pins it. `tests/conftest.py` clears every `MEMORY_BUCKETS_*` variable
  from your shell and turns the model download off, so a test run does not depend on your
  setup.
- **Comments say why.** The code shows what it does. Write a comment when the reason is
  not obvious: a Windows quirk, a case that bit someone, a limit that looks arbitrary.
- **British spelling**, in code, comments, docs and commit messages: "normalise",
  "behaviour", "colour".
- **Lint is `ruff check`, nothing more.** The rules are pinned in `pyproject.toml` so every
  ruff version checks the same thing. The code is not auto-formatted, so do not run a
  formatter over files you are not otherwise changing.
- **Windows counts.** Paths found on disk use `/`, files are written in binary mode and
  text I/O names UTF-8. If you touch file or lock code, run `check-windows` or read the
  Windows job on the mirror.

## Sending a change

1. Branch from `main`.
2. Run `check` and `lint`.
3. Write the commit message in the style of `git log`: `type(scope): summary`, for example
   `fix(snapshot): cut an always-kept file that cannot fit under snapshot_max_chars`.
4. Open the pull request against `main`. Say what changed, why, and what you ran to check it.
