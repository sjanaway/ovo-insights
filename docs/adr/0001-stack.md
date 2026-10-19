# ADR 0001: Technology stack

**Status:** Accepted, 2026-10-13
**Context:** [PRD](../product/ovo-insights-prd.md), "Decisions captured". The app must be
local-only, secure by default, Mac-first but portable, and reliable enough that code written
by an AI agent can be trusted on the strength of its tests and checks.

## Decision

| Concern | Choice | Why |
|---|---|---|
| Language | Python 3.13+ (developed on 3.14), managed with `uv` | Mature `keyring` library for the OS keychain (Node's keytar is archived); strong data tooling; `uv.lock` gives reproducible installs. |
| Web | FastAPI + Jinja2 (autoescaped) + HTMX | Server-rendered pages keep JavaScript, and therefore XSS and supply-chain surface, to a minimum. |
| Charts | Apache ECharts, vendored with a pinned SHA-256 | Works offline; no CDN; compatible with a strict CSP. |
| HTTP client | `httpx` (async) | Explicit timeouts, transport hooks for a host allowlist. |
| Validation | Pydantic v2 | Strict parsing of OVO's undocumented responses; schema-drift detection. |
| Storage | SQLite (stdlib), WAL, `STRICT` tables, numbered migrations | Transactional and robust, with zero extra dependencies; analysis is done in SQL. |
| Money | Integer milli-pence; `Decimal` in Python | No float drift in tariff comparisons. |
| Time | UTC storage, `Europe/London` display | DST days have 46 or 50 half-hours; treated as valid, not gaps. |
| Export | CSV via stdlib `csv` | The owner chose CSV; no extra dependency. |
| CLI | Typer | `ovo-insights serve`, `doctor`, `demo`. |
| Quality gates | ruff (incl. bandit `S` rules), mypy `--strict`, pytest + Hypothesis, 90% branch coverage, pip-audit, Semgrep, gitleaks, Dependabot | Each catches a different class of mistake; CI runs all of them on macOS and Ubuntu. |
| Repo | Private GitHub repo; `Private :: Do Not Upload` classifier | Prevents accidental PyPI publication. |

## Consequences

- Runtime dependencies are restricted to an allowlist enforced by
  `tests/test_dependency_policy.py`.
- CodeQL is not used while the repo is private (it needs a paid GitHub plan); Semgrep
  covers static analysis instead.
- Server-side branch protection is not available on a private repo with the current
  GitHub plan (confirmed: the API returns 403, "Upgrade to GitHub Pro"). The pre-push hook (full test suite) and the `no-commit-to-branch` hook
  stand in for it locally.
