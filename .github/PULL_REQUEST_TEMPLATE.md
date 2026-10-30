<!-- If this PR addresses an issue, add "Fixes #N" at the top (or "Part of #N (1/M). Refs #N." when split across multiple PRs so the parent issue stays open until every part lands). Omit if there is no linked issue. -->
<!-- Keep one concern per PR. Do not modify CHANGELOG.md unless asked by the maintainer on the thread; README.md may be modified if it is a critical facet of the PR. -->

## What changed

-

## Issue criteria

<!-- Required only when this PR lines up with an existing issue: map each "Done when" item from the issue to the test or check that verifies it. Remove this section if there is no linked issue. -->

-

## Verification

<!-- State what you actually ran (commands, Python version, pass/skip counts on main vs this branch) and confirm that new tests fail on bare main before the change. Note anything you could not verify locally. -->

- `uv run pytest tests/`:
- `uvx ruff check .`:

## Notes

<!-- Optional: judgement calls, trade-offs, or out-of-scope observations. Remove if unused. -->
