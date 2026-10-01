# Changelog

Notable changes to memory-buckets, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). Until 1.0, a minor version can
change the store's layout; the plugin migrates the store itself (see
[Migrating](README.md#migrating)).

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

[0.1.0]: https://git.cl0vr.co/cl0vr/memory-buckets/compare/v0.0.1...v0.1.0
[0.0.1]: https://git.cl0vr.co/cl0vr/memory-buckets/src/tag/v0.0.1
