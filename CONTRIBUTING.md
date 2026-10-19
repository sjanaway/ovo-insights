# Contributing

## Ground rules

1. **No real data in git, ever.** That means no real OVO account IDs, MPANs/MPRNs, meter
   serial numbers, names, addresses, emails, tokens or passwords: not in fixtures,
   screenshots, logs, issues or commit messages.
2. **Every change goes through a branch and CI.** Commits to `main` are blocked locally.
3. **Tests come with the change.** The coverage gate (90% lines and branches) is a floor,
   not a target. Prefer property-based tests (Hypothesis) for anything involving money,
   time or ranges.
4. **No new runtime dependencies without a reason.** Add it to the allowlist in
   `tests/test_dependency_policy.py` with a one-line justification in the same PR.

## Setup

```bash
uv sync
uv run pre-commit install
```

This installs a pre-commit hook (lint, format, types, secret scan) and a pre-push hook
(full test suite).

## Test fixtures from real API responses

Raw captures from your own account go in `fixtures-raw/`, which is gitignored. Never copy
them into `tests/fixtures/` by hand. `ovo-insights dev capture-fixtures` writes the raw
captures, then writes safe copies to `tests/fixtures/captured/`: identifiers are replaced
with fakes, every kWh, cost, rate and meter reading with a made-up value, and account and
meter dates are moved, so your usage pattern and move-in dates stay private. After a fresh
capture, update the expected values in `tests/ovo/test_captured.py`.
`tests/test_fixture_privacy.py` fails if anything that looks like a real identifier gets
into `tests/fixtures/`.

## Code style

- `ruff` for lint and format, `mypy --strict` for types: both must be clean.
- Money is integer milli-pence or `Decimal`, never `float`.
- Timestamps are timezone-aware; stored in UTC, displayed in `Europe/London`.
- Fail loudly on unexpected input from OVO; never silently coerce.

## Releasing

1. Move the "Unreleased" notes in `CHANGELOG.md` under a new `## [X.Y.Z] - YYYY-MM-DD` heading.
2. Set `version = "X.Y.Z"` in `pyproject.toml` (and run `uv lock`).
3. Merge that through a pull request. The release workflow does a dry run on it.
4. Tag the merge commit and push the tag: `git tag vX.Y.Z && git push origin vX.Y.Z`.

The workflow checks the tag matches the version, builds the wheel and sdist, and creates a
GitHub release with the changelog section as its notes. Nothing is published to PyPI (the
`Private :: Do Not Upload` classifier makes PyPI reject an accidental upload).
