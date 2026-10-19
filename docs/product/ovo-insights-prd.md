# ovo-insights PRD

**Status:** Draft, 2026-10-13
**Owner:** Stephen Janaway
**Surface:** New repo `~/Development/ovo-insights` (private GitHub). Python service on `127.0.0.1` serving a browser UI; local SQLite database; macOS Keychain for credentials. Talks outbound to OVO/Kaluza's unofficial APIs only.

## Problem

OVO's portal and app show smart-meter usage, but in a fixed way: no year-on-year overlays, no half-hourly heatmaps, no way to re-cost your own usage under a different tariff, and no durable archive you control. The only existing desktop tool ([MikeWilliams-UK/My-Ovo-Data](https://github.com/MikeWilliams-UK/My-Ovo-Data)) is Windows-only WPF, stores the OVO password in plain text in the registry, writes access tokens and raw API responses to a world-writable `C:\ProgramData` folder by default (`DumpData: true`), blocks on async HTTP (`.Result`), calls `Debugger.Break()` on errors, and has no tests.

We want a Mac-first, local-only web app (private repo for now, designed so it can be open-sourced later) that pulls a user's OVO data reliably, keeps it on their machine, and answers the questions the OVO portal can't. It should be secure by default and have tests strong enough that code Claude writes can be trusted without line-by-line human review.

We want to:
1. Archive the last two years of electricity and gas usage (monthly, daily, half-hourly) and meter readings locally, and keep it fresh automatically.
2. Show usage and cost trends, including year-on-year comparisons.
3. Expose time-of-day patterns and base load from the half-hourly data.
4. Re-cost actual half-hourly usage under user-defined tariffs and compare against what OVO actually charged.
5. Show data completeness (missing days and half-hours) so the analysis can be trusted.
6. Export to CSV.

**Success metric:** The owner stops opening the OVO app or portal to answer usage and cost questions. Measured as four consecutive weeks after v1 with no portal visits for that purpose (self-reported in a weekly retro note). No baseline needed; this is a behaviour switch.

## Constraints

- **Freedom:** Free hand. New project, owner picks everything. Private repo for v1; the code is written open-source-ready (no secrets, no real data in git) so it can go public later.
- **Touches:** Real personal data (the user's OVO credentials, account IDs, MPAN/MPRN, meter serials, consumption patterns that reveal occupancy) and a real third-party system (OVO/Kaluza production APIs, accessed unofficially).
- **Data & safety:**
  - All data stays on the user's machine. No telemetry, analytics, crash reporting or third-party CDNs. The only outbound hosts are those in the OVO host allowlist (M3).
  - The password lives only in the OS keychain. Tokens live only in memory. Neither is ever logged, written to disk or returned by any HTTP endpoint.
  - Calls to OVO must be throttled and must back off hard on any 401/403/429 or Cloudflare challenge. The app must never retry-storm a third party's production API on behalf of every user (relevant once it's shared).
  - Fixtures committed to the repo must be synthetic or redacted. No real account IDs, MPxNs, meter serials, names or addresses ever reach git.
  - The README and UI carry an "unofficial, not affiliated with OVO Energy" disclaimer.

## Decisions captured (from scoping conversation)

| Question | Decision |
|---|---|
| Primary goals | Cut bills, compare tariffs, track trends over time, own my data. All four. |
| Success metric | Replaces the OVO portal for the owner (four weeks with no portal visits for usage questions). |
| Fuels | Electricity and gas, both smart meters. The data model supports multiple accounts and supply points, but v1 only needs to be tested against one dual-fuel account. |
| Audience | The owner for v1, in a private repo. Built so anyone with an OVO account could install it if it's open-sourced later. |
| Who writes the code | Claude builds it; quality gates (types, tests, coverage, security scans, CI) must catch mistakes without human line review. |
| Credential storage | macOS Keychain via the `keyring` library (cross-platform backend). Never plaintext on disk. |
| Refresh | Daily background sync while the server runs, plus a manual "Sync now" button. |
| Tariff sources | Manually entered tariffs, plus OVO's returned costs as the "actually paid" baseline. No external tariff APIs in v1. |
| Platforms | macOS officially supported and tested in CI. The code stays portable (no Mac-only APIs); Linux and Windows are best-effort. |
| v1 views | Usage & cost dashboard, half-hourly heatmap with base load, tariff comparison, readings & data-completeness view. |
| Install / run | `uv tool install` from the local checkout or private git URL (no PyPI while private), then `ovo-insights serve` opens the browser. |
| Export | CSV only. No Excel in v1. |
| API client | Our own thin async client with strict schema validation. The C# repo and timmo001/ovoenergy are references only, not dependencies. |
| Backfill depth | Last 2 years on first sync. Resumable, throttled, and never re-fetches complete closed periods. |
| Repo | New **private** GitHub repo at `~/Development/ovo-insights`, with GitHub Actions CI from the first commit. |
| Name | `ovo-insights`, with a prominent "unofficial" disclaimer. |
| Language / runtime | *(implied)* Python 3.13, managed with `uv`. Chosen for the mature `keyring` library (the Node equivalent, keytar, is archived) and strong data tooling. |
| Web framework | *(implied)* FastAPI + Jinja2 server-rendered pages + HTMX for interactivity. Minimal JS reduces attack surface and supply-chain risk. |
| Charts | *(implied)* Apache ECharts, **vendored** into `static/` with a pinned version and SHA-256 recorded; no CDN. |
| HTTP client | *(implied)* `httpx` async, with explicit timeouts on every call. |
| Validation | *(implied)* Pydantic v2 models with `extra="forbid"` on our own models and `extra="ignore"` + logged-unknown-field warnings on OVO responses. |
| Storage | *(implied)* SQLite (stdlib `sqlite3`, WAL mode, `STRICT` tables, foreign keys on), with numbered SQL migrations. Analysis queries run in SQL; no pandas dependency. |
| Money | *(implied)* Stored as integer **milli-pence** (thousandths of a penny, `INTEGER`); `Decimal` in Python; never `float`. *(Corrected in M2: an earlier draft said "tenths of a penny", which is too coarse to sum half-hourly costs.)* |
| Time | *(implied)* All timestamps stored as UTC ISO-8601; displayed in `Europe/London`. DST days have 46 or 50 half-hours, and the app treats that as correct, not as a gap. |
| CSV writer | *(implied)* stdlib `csv` module, UTF-8, RFC 4180 quoting; no extra dependency. |
| Testing | *(implied)* pytest, hypothesis (property tests), respx (HTTP mocking), pytest-cov, Playwright (e2e). Coverage gate 90% lines/branches on `src/` *(illustrative - confirm in implementation)*. |
| Static checks | *(implied)* ruff (incl. `S` bandit rules), mypy `--strict`, pip-audit, Semgrep OSS (CodeQL only if the account has GitHub Code Security for private repos), Dependabot, gitleaks. |
| Local web security model | *(implied)* Bind `127.0.0.1` only; Host-header allowlist (DNS rebinding defence); per-launch random session token; CSRF token + Origin check on every state-changing request; strict CSP; no CORS. |

## Implementation milestones

**Owner test checkpoints** (where the owner can try it, not just CI):
1. **After M3:** `ovo-insights doctor` checks that login and every OVO endpoint work against the real account.
2. **After M5:** `ovo-insights sync` pulls two years of real data from the terminal; spot-check it against the OVO app.
3. **After M7:** `ovo-insights demo` (synthetic data) and `ovo-insights serve` (real data) for the first usable dashboard.

Dependency graph: **M1 → M2 → {M3, M4} → M5**; **M1 → M6**; **{M2, M6} → {M7, M8, M9, M10}** (M9 also needs M7's query layer); **M5 + M7–M10 → M11 → M12**.

### M1 - Repo scaffold and quality gates

Every later milestone lands into a pipeline that already rejects bad code.

- [x] `gh repo create ovo-insights --private`; MIT licence (harmless while private, ready if it goes public); `README.md` with the "Unofficial — not affiliated with or endorsed by OVO Energy. Use at your own risk." disclaimer as its first paragraph
- [x] `uv init --package`, `src/ovo_insights/` layout, Python `>=3.13`, `uv.lock` committed
- [x] Dependencies pinned to exact versions in the lockfile; runtime deps limited to: fastapi, uvicorn, jinja2, httpx, pydantic, keyring, platformdirs, typer (CLI). Any addition needs a one-line justification in the PR. *(Done: only `typer` added so far; the others join in their milestones. Allowlist enforced by `tests/test_dependency_policy.py`.)*
- [x] `ruff` configured (rule sets: E, F, I, B, UP, S, SIM, RUF, DTZ, PT, ASYNC); `ruff format`
- [x] `mypy --strict` over `src/` and `tests/`
- [x] `pytest` + `pytest-cov` with branch coverage, fail-under gate at 90%
- [x] `pre-commit` hooks: ruff, ruff-format, mypy, gitleaks, a check-added-large-files hook
- [x] GitHub Actions `ci.yml` on `macos-latest` and `ubuntu-latest`: lint → types → unit tests → coverage gate → `pip-audit` → build wheel
- [x] GitHub Actions `semgrep.yml` (Semgrep OSS, `p/python` + `p/secrets` rulesets) and Dependabot for `uv` and `github-actions`; add `codeql.yml` only if `gh api repos/{owner}/ovo-insights` shows code scanning is available for this private repo
- [x] Branch protection on `main`: CI required, no direct pushes *(Confirmed 2026-10-13: the GitHub API returns 403 "Upgrade to GitHub Pro". Enforced locally instead: the `no-commit-to-branch` pre-commit hook plus a pre-push hook running the full test suite.)*
- [x] `SECURITY.md` (how to report vulnerabilities privately; GitHub private vulnerability reporting isn't available on private repos, so it's email for now) and `CONTRIBUTING.md` (fixture redaction rules, "no real data in git")
- [x] `docs/adr/0001-stack.md` capturing the stack rows of the decisions table

### M2 - Domain model and storage

Typed core that everything else reads and writes. Depends on M1.

- [x] Paths module: data dir = `platformdirs.user_data_dir("ovo-insights")` (`~/Library/Application Support/ovo-insights` on macOS), overridable with an absolute `OVO_INSIGHTS_DATA_DIR`; created with mode `0700`; DB file `0600` (created with `O_EXCL|O_NOFOLLOW`); refuses symlinks, other owners and looser permissions, printing the `chmod` fix
- [x] Domain types as frozen, slotted dataclasses that validate on construction (and again on every read from the DB): `SupplyPoint`, `HalfHourUsage`, `DailyUsage`, `MonthlyUsage`, `Meter`, `Register`, `Reading`. *Refined:* no separate `Account` type (a validated id string is enough); `DailyUsage` stores kWh, cost and `has_half_hourly` only, because OVO's standing-charge and rate semantics are unconfirmed (open question). Those columns arrive via a migration in M3 once fixtures confirm their meaning. `Tariff`/`TariffBand` move to M9.
- [x] `Fuel` enum (`electricity`, `gas`); parses OVO's `ELECTRICITY`/`ELECTRIC`/`GAS`; anything else raises `UnknownFuelError`, never coerced
- [x] Migration runner (`storage/migrator.py`): `migrations/NNNN_*.sql` numbered 1..N without gaps, `schema_migrations` ledger, each migration plus its ledger row in one `BEGIN IMMEDIATE` transaction, forward-only, SHA-256 checksum and name verified on every start, refuses a DB from a newer app version, rejects transaction-control statements inside migration files
- [x] `0001_initial.sql`: `STRICT, WITHOUT ROWID` tables for accounts, supply_points, usage_half_hourly, usage_daily, usage_monthly, meters, registers, readings. `CHECK` constraints mirror the domain rules (fuel values, `x IS date(x)` dates, canonical UTC timestamps on :00/:30, kWh `>= 0`, integer money, MPxN digits, decimal-text readings). *Refined:* usage is keyed on `(account_id, fuel, …)` because OVO's usage endpoints are per account and fuel, not per supply point. Readings are keyed on `(meter_serial, register_id, read_date)`; the reference repo's `(date, fuel)` key silently overwrote multi-register reads. `sync_runs`/`sync_state` move to an M5 migration and `tariffs`/`tariff_bands` to an M9 migration, so the schema isn't designed ahead of its semantics.
- [x] Repository layer with **upsert-only** writes (`INSERT … ON CONFLICT DO UPDATE`) that join a caller's transaction if one is open (for M5's per-period transactions)
- [x] All SQL parameterised; `tests/test_code_policy.py` walks the AST of `src/` and fails if any `execute*` call gets anything but literal SQL (one allow-listed exception: running migration files). It also fails if any regex using `\d`/`\w`/`\s`/`\b` lacks `re.ASCII`, after M2's tests caught full-width digits passing money validation.
- [x] Money helpers: `parse_pounds`/`parse_pence` → integer milli-pence, rounding half-even to the nearest milli-penny; reject floats, bools, NaN, infinities, exponents, currency symbols, separators, non-ASCII digits, values over £1m and negatives unless `allow_negative=True`; `format_pounds` for display; Hypothesis property tests (round-trip, bounded rounding error, no drift when summing)
- [x] Time helpers: `london_half_hours(date)` returning 46/48/50 UTC slots; strict `format_utc`/`parse_utc`; naive datetimes rejected; Hypothesis property tests across 2015–2035 including DST days and day-to-day tiling
- [x] Unit tests for repositories against a temp DB: round-trips for every entity, idempotent re-upsert (Hypothesis: upserting twice equals once, last write wins), FK and `CHECK` enforcement on raw SQL, corrupted rows failing loudly on read, migration checksum tamper, rename, newer-DB and mid-migration-failure rollback
- [x] CI wheel smoke test also opens a database from the *installed* wheel, proving the SQL migrations ship as package data

### M3 - OVO API client

Thin, defensive async client. Depends on M2 (domain types). Reference flow observed in My-Ovo-Data v1.0.9 (Dec 2025), to be re-confirmed live in this milestone.

- [x] `OvoClient` using a single shared `httpx.AsyncClient` with `timeout=httpx.Timeout(connect=5, read=30)` *(illustrative - confirm in implementation)*, HTTP/1.1, a descriptive `User-Agent: ovo-insights/<version> (+repo URL)`
- [x] Host allowlist enforced in a transport hook: `my.ovoenergy.com`, `smartpaymapi.ovoenergy.com`, `api.eu1.prod.kaluza.com`. Any other host (including via redirect) raises `DisallowedHostError`. `follow_redirects=False`.
- [x] TLS verification always on; no option to disable it
- [x] Auth: `POST my.ovoenergy.com/api/v2/auth/login` → capture `userId` and the `restricted_refresh_token` cookie → `GET /api/v2/auth/token` with that header → access-token JWT
- [x] JWT `exp` read (no signature validation needed; we're the bearer, not the verifier) to schedule refresh 60 s before expiry; refresh-token expiry triggers re-login from Keychain; at most **one** re-login attempt per sync run
- [x] Accounts: Kaluza GraphQL `Bootstrap` query (port `Accounts.query` from the reference repo) → accounts and supply points with fuel and start date
- [x] Usage: `smartpaymapi …/usage/api/monthly/{accountId}?date=YYYY`, `/daily/{accountId}?date=YYYY-MM`, `/half-hourly-local/{accountId}?date=YYYY-MM-DD`
- [x] Readings: Kaluza GraphQL `MeterReads_nextV1` with `includeReads: TOP_VALID_ONLY` (port `Readings.query`)
- [x] GraphQL queries stored as `.graphql` files and sent with `variables` (never string-substituted into the query, unlike the reference repo's `[[customerGuid]]` replace)
- [x] Pydantic response models for every endpoint; `Cost.amount` arrives as a string, so it's parsed via `parse_money`; nulls tolerated where the reference repo hit them (null cost, unexpected readings shape — see its Nov 2024 and Nov 2025 fixes)
- [x] Migration `0002_daily_rates.sql` adds `unit_rate_milli_pence` and `standing_charge_milli_pence` to `usage_daily` (deferred from M2; semantics confirmed on real data)
- [x] Unknown response fields: ignored but counted, and a single WARN log per field name per run (schema-drift early warning)
- [x] Error taxonomy: `AuthFailed`, `Unauthorized`, `RateLimited(retry_after)`, `BlockedByUpstream` (403 / Cloudflare challenge HTML detected), `UpstreamSchemaChanged`, `UpstreamUnavailable` (5xx/timeouts)
- [x] Retry policy: only `UpstreamUnavailable` is retried — 3 attempts *(illustrative)*, exponential backoff with full jitter. `RateLimited` honours `Retry-After`. `BlockedByUpstream` and `AuthFailed` are **never** retried; they abort the sync and open the circuit breaker (M5).
- [x] Client-side rate limiter: max 1 request/second *(illustrative - confirm in implementation)*, shared across all endpoints *(Changed in M5: 1 request every 2 s, doubled on every 429; see M5.)*
- [x] Log redaction filter on the root logger: masks `Authorization`, cookies, the `restricted_refresh_token` header, JWT-shaped strings, the password field and email addresses; unit-tested with hypothesis-generated JWTs
- [x] `ovo-insights capture-fixtures` dev command: hits each endpoint once for the logged-in user and writes responses to a gitignored `fixtures-raw/`; a `redact` step rewrites IDs, MPxNs, serials, names and emails with stable fakes; only redacted output may go in `tests/fixtures/`
- [x] A CI check fails if anything under `tests/fixtures/` matches a real-looking MPAN (13 digits passing the MPAN check digit), a UK postcode or an email not on `example.com`
- [x] Contract tests against **synthetic** fixtures (`tests/fixtures/synthetic/`, shaped from both reference clients); respx-mocked tests for each error-taxonomy path, the token refresh path and the host allowlist (redirect to an off-list host is rejected)
- [x] `ovo-insights doctor` command (**owner checkpoint 1**). It probes both `half-hourly-local` and `half-hourly`, since the reference clients disagree: logs in and calls each endpoint once, printing a pass/fail table — the canary used when users report breakage. Until M4's Keychain storage exists, it prompts for credentials with `getpass` each run.
- [x] *Added:* the token request sends the login cookies both as cookies (timmo001's way) and as the `restricted_refresh_token` header (My-Ovo-Data's way). The HTTP client's cookie jar accepts nothing, so cookies can never be sent to the data hosts.
- [x] *Added:* a login that returns HTTP 200 with `{"code": "Unknown"}` is treated as a failure (timmo001 handles OVO's bad-password response this way).
- [x] *Added:* timestamps without a timezone and half-hourly units other than kWh are rejected rather than guessed. timmo001's mock data suggests gas half-hours may be in m³.
- [x] *Added:* the Bootstrap query asks for no address or personal fields (timmo001's asks for the postal address).
- [x] **Owner step:** first `doctor` run (2026-10-13). Login, accounts and monthly passed. Daily, half-hourly and readings failed on *deliberate* strictness (naive timestamps; 8-decimal readings), which the capture then resolved
- [x] **Owner step:** captured and redacted real responses (`tests/fixtures/captured/`). All 15 identifiers were verified replaced (checked by value comparison without printing them). `tests/ovo/test_captured.py` pins the confirmed facts below
- [x] **Owner step:** re-ran `ovo-insights doctor` (2026-10-13); every check passed. **Owner checkpoint 1 complete.**

**Confirmed against the real account (2026-10-13):**
- Both half-hourly endpoints send **naive UTC** timestamps (`2026-10-11T00:00:00.000`). `half-hourly-local?date=D` returns the UK day D (23:00 UTC the day before → 22:30 UTC); `half-hourly?date=D` returns the UTC day. Values at the same timestamp are identical. The client defaults to `half-hourly-local`. Naive timestamps are accepted only where they are known to be UTC.
- Daily intervals are labelled with naive midnights; they are treated as calendar dates.
- Daily `cost` = kWh × `rates.anytime` (£/kWh), rounded to the penny. `rates.standing` (£/day) is **not** included. Monthly = sum of daily, also excluding standing.
- Gas has no half-hourly data on this account (`hasHhData: false`; empty half-hourly gas block), so the m³ question doesn't arise yet.
- The customer has **two accounts** (left OVO and came back) on the same supply points: 2017-09-26 and 2021-08-13.
- Meter history goes back to 1996 (removed meters, register ids `1` and `01`). One old gas register has no start date (skipped with a warning). Estimated reads carry up to 8 decimal places; the allowed maximum is now 10.
- OVO pads floats (`223.29300000000003`). Money arrives as strings, so the integer milli-pence decision holds.

### M4 - Credentials and secrets

Depends on M1. Can run in parallel with M3.

- [x] `CredentialStore` protocol with `KeyringCredentialStore` (OS keychain) and `MemoryCredentialStore` (tests, and runs without a secure keychain). *Refined:* one keychain item (service `ovo-insights`, account `ovo-login`) holds `{"version": 1, "email", "password"}` as JSON. Load and forget are each one atomic operation, and the email never touches disk either. Corrupt or oversized items are refused with a "logout, then login" fix.
- [x] Setup page collects email and password over the local form → **moved to M6** (needs the web server); it will call the same `CredentialStore.save` *(done in M6)*
- [x] Secure-backend **allowlist** (macOS Keychain, Windows Credential Locker, Secret Service, libsecret, KWallet). Anything else (`fail`, `null`, `keyrings.alt` plaintext, or a chainer containing any of them) is refused. `login` exits before asking for the password; other commands fall back to prompting.
- [x] `ovo-insights login`: prompts with `getpass` (never echoed, never an argument or env var). **Verifies the login with OVO before saving** (`--no-verify` to skip), so a typo isn't stored. `ovo-insights logout` deletes the item and is idempotent.
- [x] `doctor` and `dev capture-fixtures` use the saved login, falling back to a prompt (`--prompt` forces one)
- [x] "Forget my credentials" = `logout` on the CLI (the web button arrives with M6); in-memory tokens are cleared whenever a client closes (M3)
- [x] Tokens held only in an in-memory `TokenState`; never serialised; `Secret` masks `repr` and refuses pickling (done in M3)
- [x] Capture-everything test: a sentinel password goes through `login` → `doctor` → `logout` with DEBUG logging, and appears **only** in the keychain item, never in CLI output or any log record (template contexts are covered when M6 adds them)
- [x] Test-suite guard: every test runs with a keyring backend that fails loudly if touched, so tests can never read or write the real Keychain

### M5 - Sync engine

Fetches, validates and stores data. Resumable and polite. Depends on M2, M3, M4.

- [x] Migration `0003_sync.sql` adds `sync_runs` and `sync_state` (deferred from M2)
- [x] Sync **every** account from Bootstrap, not just the first. Each account's backfill window is clipped to its own active period, because the customer's old and current accounts share supply points. *(Owner has two accounts: 2017 and 2021 starts.)* *Refined:* the active period comes from the supply end dates in each account's meter-reads response (one request per account, which also stores its readings). If OVO gives no end date and a later account starts on the same MPxN, the earlier one is taken to end the day before. Rows OVO returns outside an account's active period are ignored. Accounts are written oldest first, so the current account owns the shared meters; readings from both accounts are kept, since they're keyed by meter. On the owner's data, the two-year cap leaves the 2017 account with only its meter reads to fetch.
- [x] Half-hourly sync fetches by **UK date** via `half-hourly-local`, so each request is exactly one UK day and completeness is 46/48/50 slots. A response containing a slot outside the requested UK day, or the same slot twice, fails validation.
- [ ] Once the clocks go back (2026-10-25), re-capture that day with `capture-fixtures` and add a contract test that it returns 50 slots *(owner step after 25 Oct; the engine's 46/50-slot handling is already tested against synthetic DST days)*
- [x] `sync_runs` row per run: start, end, status (`ok`/`partial`/`failed`/`blocked`), counts per entity, error class (no secrets). *Refined:* a `running` status while in progress; a run left `running` by a crash or `kill` is closed as `partial` with error class `Interrupted` by the next run. Ctrl-C is recorded the same way. Error classes are class names only, checked against `[A-Za-z]{1,64}`, never messages.
- [x] `sync_state` per (supply point, granularity): earliest and latest complete period fetched. *Refined:* one row per **(account, granularity, period)**, a ledger instead of a range. It's keyed per account because the usage endpoints are per account (the same refinement as M2's usage tables), and a range can't represent holes. Each row stores its status (`complete`/`provisional`/`incomplete`/`gap`/`error`), the expected and received counts, attempts, last fetch and a note. Earliest/latest complete is a query over it (`store.coverage`), and `sync` prints it.
- [x] First sync: backfill from `max(supply_start_date, today - 2 years)` to yesterday. A period is one request: a year (monthly), a month (daily) or a UK day (half-hourly). For the owner, that's 3 + 25 + 730 requests, at least 25 minutes at the 2-second pace. Newest data comes first, so an interrupted backfill still has the recent months.
- [x] Incremental sync: re-fetch the last 3 days *(illustrative - confirm in implementation)* of half-hourly and daily data (OVO data lags to end of the previous day and may be revised), the current and previous month for daily, the current year for monthly; skip complete closed periods. *Refined into one rule:* a period stays `provisional` and is re-fetched until a fetch made at least `REVISION_DAYS` (3) days after it ends. Day-to-day, that means the last 3 days, the current month (plus the previous month until the 3rd), and the current year (plus the previous year until 3 January).
- [x] A half-hourly day is "complete" when it has 46/48/50 slots matching `london_half_hours`; incomplete days are re-fetched on later syncs (up to 14 days back *(illustrative)*), then marked as a known gap. *Refined:* slots are expected only for fuels whose daily row says `hasHhData`; otherwise the owner's gas (no half-hourly data) would make every day a gap. A day with no daily row for an active fuel stays incomplete. Monthly and daily periods use the same incomplete → gap rule against the expected (fuel, day or month) rows. A day that is already more than 14 days old when first found incomplete becomes a gap at once, so old holes cost one request, not 14.
- [x] Each period is written in its own transaction, so a crash mid-sync loses at most one period
- [x] Circuit breaker: on `BlockedByUpstream`, `AuthFailed` or 3 consecutive failed runs, auto-sync pauses until the user presses "Sync now"; the UI shows why. *Refined:* derived from `sync_runs` (`store.breaker`), not stored separately, so it can't drift. `Unauthorized` (refused even after logging in again) also opens it. A manual sync always runs, prints why auto-sync was paused, and closes the breaker if it succeeds. The UI and the scheduler that obeys the breaker arrive with M6.
- [x] Single-flight lock: only one sync at a time (manual during scheduled → returns "already running"). An OS file lock on `<data dir>/sync.lock` (`flock`; Windows best-effort). The OS releases it if the process dies, so a stale lock can't block syncing.
- [ ] Daily scheduler: an asyncio task in the server process; runs at a random minute between 06:00–08:00 local *(illustrative)* to avoid every install hitting OVO at once; also runs on startup if the last successful sync is over 24 h old → **moved to M6** (needs the server process; it must check `store.breaker` and use `Trigger.SCHEDULED`)
- [x] Sanity checks before writing: daily kWh is within 10% *(illustrative)* of the sum of that day's half-hours, otherwise a WARN is stored on the day (not rejected); negative consumption rejected. *Refined:* the warning goes in the half-hourly day's `sync_state.note`, and differences under 0.1 kWh are ignored so tiny days don't trip it. Negative consumption was already rejected in M3.
- [x] Progress reported via a small in-memory status object that the UI polls (HTMX every 2 s while running). *(`SyncProgress` exists and the CLI prints from it; the HTMX polling arrives with M6.)*
- [x] `ovo-insights sync` CLI command (**owner checkpoint 2**): runs one sync in the foreground using the Keychain credentials from `ovo-insights login`; prints progress per fuel and granularity, then a summary (periods fetched, rows written, gaps found, final status). Exit code 0 for `ok`, non-zero for `partial`/`failed`/`blocked`, so it's scriptable. Shares the single-flight lock with the server. *Refined:* progress is per account and granularity, because one request returns both fuels. Exit codes: 0 ok, 1 failed, 3 partial, 4 blocked, 5 already running (2 is the CLI's usage error). Output never includes account numbers, MPxNs or serials.
- [x] `ovo-insights sync --dry-run`: logs in and plans the periods it would fetch without writing anything. It makes the login, Bootstrap and meter-reads requests (meter reads give the end dates the plan needs), reads an existing database read-only (`mode=ro`), and never creates one.
- [x] Tests: full backfill against a respx-mocked OVO with fixtures; resume after an injected failure at period N; idempotency (two syncs give an identical DB); circuit breaker opens on a 403 Cloudflare page; DST-day completeness. *(`tests/sync/fake_ovo.py` serves consistent synthetic data for any date. Also covered: the owner-shaped two-account backfill, incomplete → gap, schema errors, interruption, the cross-process lock, the CLI exit codes and Hypothesis properties of the planner.)*
- [x] *Added:* stopping rules. `BlockedByUpstream`, `AuthFailed`, `Unauthorized`, `RateLimited`, `UpstreamUnavailable` (after the client's own retries) and `DisallowedHostError` end the run at once. A response that fails validation marks only that period `error`; it is retried on later runs, after everything else. After 5 such errors in one run *(illustrative)* the run stops, so a changed API costs at most a handful of requests.
- [x] *Added after the first real runs (2026-10-13):* the backfill kept stopping with `RateLimited`. Two causes. (1) Every data request was preceded by a token request: OVO's access tokens appear to be short-lived, and with a flat 60 s refresh margin each token was "due" on arrival (903 requests for 446 periods). The margin is now a quarter of the token's lifetime, capped at 60 s, and each refresh logs the lifetime at INFO (`-v`). (2) OVO answers 429 with `Retry-After: 60`, which the client treated as "stop". Now waits of up to 120 s are honoured and the request retried. Each 429 doubles the gap between requests for the rest of the run (1 s minimum, 30 s maximum), and the default pace is one request every 2 s. Backfill was already newest-first, so stopped runs had always saved the most recent data.
- [x] *Fixed after an owner report (2026-10-14):* OVO publishes a day's half-hours before its daily row, and a day with no daily row was expected to have half-hours for **every** fuel (96 for electricity plus gas, which has none), so it stayed `incomplete` even with all 48 electricity slots stored, and the Sync page's "What's downloaded" stopped a day short. Now, for a fuel with no daily row yet, a full day is expected only if OVO sent slots for it or that account's fuel had half-hourly data on an earlier day (so gas, which never has any, isn't waited for). If nothing can be said to be expected, the day still stays incomplete. The day is `provisional` (revisable for 3 days) instead of `incomplete`, and counted as downloaded.
- [x] *Added:* a fuel that OVO sends half-hours for is expected to have a full day, even when its daily row says `hasHhData: false`. On the real account, 4 such days had all 48 slots and were recorded as "0 expected"; they now count as 48 expected.
- [x] **Owner step:** `ovo-insights sync --dry-run` (758 periods planned), then `ovo-insights sync` to completion (2026-10-13, five runs; the last made 297 requests for 280 periods after the fixes above)
- [x] **Owner step:** spot-checked against the OVO app (2026-10-13): 5 random days, both fuels, kWh and £ (usage cost, excluding standing charge), plus September 2025 and September 2026 monthly totals all match. **Owner checkpoint 2 complete.**

**Confirmed against the real account by the first full sync (2026-10-13):**
- The two-year window held 3 monthly, 25 daily and 730 half-hourly periods for the current account and none for the 2017 one, as planned. 1,156 meter readings came from both accounts.
- Electricity half-hourly data starts on 2025-02-11 (`hasHhData` is false before that). Gas has none. Half-hours stored: 29,038.
- DST days on real data: 2025-03-30 and 2026-03-29 have 46 slots, 2025-10-26 has 50, all complete.
- Known gaps: 4 days with 46 of 48 slots (2025-07-29, 2025-09-18, 2025-09-26, 2026-07-31). Each follows a day whose `hasHhData` is false despite having 48 slots.
- Sanity warnings: 2025-09-19 and 2025-09-20 differ from their half-hour sums by about 0.94 kWh in opposite directions, with the two-day totals matching. The stored daily figures match the OVO app, so OVO's own daily and half-hourly figures disagree on those days; the sync stores both faithfully.


### M6 - Local web server and security hardening

Secure shell for all UI. Depends on M1. Can run in parallel with M2–M5.

- [x] `ovo-insights serve [--port N] [--no-open]`: uvicorn bound to `127.0.0.1` only; a `--host` flag does not exist; binding a non-loopback address is impossible from config. The listening socket is bound to `127.0.0.1` in our code (`server.bind_loopback`) and passed to uvicorn, so uvicorn never sees a host setting. A test asserts `--host` is rejected.
- [x] Port defaults to an OS-assigned free port if unspecified *(or fixed 8765 - confirm in implementation)*. **Decided: OS-assigned.** The launch link is single-use, so a bookmark could never work anyway. Port 8765 was in fact already taken by another local site on the owner's Mac during development.
- [x] Launch token: 32 random bytes (`secrets.token_urlsafe`) generated per launch; the browser is opened at `http://127.0.0.1:PORT/?token=…`; the first request exchanges it for an `HttpOnly; SameSite=Strict; Path=/` session cookie and redirects to strip the token from the URL; every request without a valid cookie gets 403. *Refined:* the token works **once**, so a link left in browser history or the terminal can't be replayed (restart `serve` for a new one). The session cookie holds a separate random id, never the token.
- [x] Host-header middleware: only `127.0.0.1:PORT` and `localhost:PORT` accepted (DNS-rebinding defence). It's a pure ASGI middleware wrapping the whole app, static files included. A repeated Host header is refused.
- [x] CSRF: synchroniser token in a meta tag sent by HTMX as a header on every non-GET; plus `Origin`/`Sec-Fetch-Site` must be same-origin. *Refined:* the token reaches HTMX through a `hx-headers` attribute rather than a meta tag plus script, so there's no inline script. Every state-changing action goes through HTMX, so the token is only accepted in the `X-CSRF-Token` header, never a form field. Forms are parsed by a small urlencoded parser (4 KB cap, single values), which avoids the `python-multipart` dependency.
- [x] Security headers: `Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store` on HTML and data responses. These go on **every** response, including the 400/403 refusals and static files, plus `X-Frame-Options: DENY`, `Cross-Origin-Opener-Policy`, `Cross-Origin-Resource-Policy` and a `Permissions-Policy`. There is no `Server` header.
- [x] No inline scripts or styles (CSP-compatible); HTMX and ECharts vendored in `static/vendor/` with versions and SHA-256 in `static/vendor/MANIFEST.txt`, checked in CI. HTMX 2.0.11 was downloaded with the owner's approval and verified against npm's sha512 integrity before extraction (licence 0BSD, included). HTMX is configured with `allowEval: false`, `allowScriptTags: false`, `selfRequestsOnly: true` and no injected indicator styles. **ECharts arrives with M7**, which is the first milestone that draws charts. Checked in a real browser: no CSP violations in the console.
- [x] Jinja2 autoescape on for all templates; a CI grep fails on the `|safe` filter outside an allowlisted file. *Refined:* `tests/test_code_policy.py` fails on `|safe`, `autoescape false`, inline `<script>`/`<style>`/`style=`, `on…=` handlers, `hx-on` (which needs eval), `javascript:` URLs and external `src`/`href`. There is no allowlist; nothing needs one.
- [x] FastAPI's `/docs`, `/redoc` and `/openapi.json` disabled
- [x] Error handler returns a generic page with a short error ID; full traceback only to the local log, after redaction
- [x] Logging to `<data dir>/logs/ovo-insights.log`, rotating, 5 × 1 MB *(illustrative)*, file mode `0600`; no raw response dumping exists as a feature (deliberately unlike the reference repo's `DumpData`). Rotated files are also created `0600` without following symlinks, and an existing looser file is tightened.
- [x] Pages: Setup (credentials), Dashboard, Half-hourly, Tariffs, Readings & gaps, Sync status, Settings (forget credentials, delete all data). For now the Dashboard shows the last sync and what's downloaded per account (no ids); Half-hourly, Tariffs and Readings are placeholders until M7–M9. Setup checks the login with OVO before saving it (at most one check every 5 s) and never echoes the password.
- [x] "Delete all data" deletes the DB and logs after a typed confirmation ("DELETE"). It refuses while a sync holds the lock and keeps the saved login; the app carries on with a fresh, empty database.
- [x] *Moved from M5:* daily sync scheduler (an asyncio task, random minute 06:00–08:00 local, plus on startup if the last successful sync is over 24 h old). It skips while `store.breaker` is open, runs `SyncEngine` with `Trigger.SCHEDULED` under the same `sync_lock`, and moves the blocking SQLite work off the event loop. A sync runs in its own worker thread, with its own event loop and SQLite connection. "Successful" means a run that saved data (`ok` or `partial`).
- [x] *Moved from M5:* "Sync now" button (`Trigger.MANUAL`, "already running" if the lock is held); the Sync status page polls `SyncProgress` with HTMX every 2 s while running and shows the circuit-breaker reason when auto-sync is paused. Polling stops by itself when the sync finishes. The lock is taken before the button's request returns, so a sync already running in the terminal is reported straight away.
- [x] Tests: requests with a wrong Host → 400; no cookie → 403; POST without CSRF → 403; cross-origin `Origin` → 403; every response carries the CSP; the token is not present in the URL after the first redirect. *(Plus: single-use token, same-origin acceptance rules, a real uvicorn server on a loopback socket, the password never in any page or log, delete-data, the scheduler across a DST change, the vendored-file hashes, and the CI wheel smoke test renders a template and checks the HTMX hash from the installed wheel.)*
- [x] *Added:* FastAPI 0.143 instruments every app with OpenTelemetry by default (`opentelemetry-api` arrived as a FastAPI dependency). Nothing could be exported without the OpenTelemetry SDK, which isn't installed, but every telemetry switch is turned off explicitly, including environment-driven auto-configuration. A test asserts this and that no SDK or exporter is importable.
- [x] *Added:* uvicorn's access log, which would record `/?token=…`, is off, and its logger is disabled outright. A real-server test found the logger could still receive the line when another tool attaches handlers. The `httpx`/`httpcore` loggers are pinned to WARNING everywhere, because at INFO they log every request URL, and OVO's URLs contain account numbers.
- [ ] **Owner step:** run `ovo-insights serve`, open the printed link, check the Dashboard and Sync pages show the real sync history, and press Sync now. (Settings' "Delete all data" really deletes the downloaded data: it was exercised against throwaway data in a real browser, not the owner's.)

**Known limitation (for `docs/security.md`, M11):** browsers don't scope cookies by port. If the user visits another web server on `127.0.0.1` while `serve` is running, that server receives the session cookie and could replay it to this port (`SameSite` doesn't help, because ports don't make sites distinct). That needs a malicious local listener *and* a visit to it. Sessions last only as long as one `serve` process.

### M7 - Usage & cost dashboard and half-hourly heatmap

Depends on M2 and M6.

- [x] Query layer (`analysis/` module, pure functions over SQL results, fully unit-tested): daily/weekly/monthly totals per fuel, cost totals, year-on-year alignment by day-of-year and by ISO week. Accounts are summed per fuel (one household). Money stays integer milli-pence, and a day without a cost is counted rather than treated as £0. "Cost" is OVO's usage cost **plus** the standing charge, i.e. what the customer pays.
- [x] Dashboard: date-range picker (presets: last 30 days, last 12 months, this year, all); per-fuel kWh and £ bar charts; year-on-year line overlay; summary tiles (total kWh, total £, average £/day, change vs same period last year with sign and %). Ranges end yesterday, and bars are by day, week or month depending on the range's length. £ bars stack usage and standing charge. Periods only partly inside the range are drawn faded and labelled, so a short week isn't misread. The year-on-year comparison is like-for-like: only days present in both years count, and the tile says how many. The line uses whole ISO weeks only.
- [x] *Added (owner request):* custom **from/to dates** alongside the presets, on the Dashboard and the Half-hourly page (they share the range bar). Plain GET parameters `from` and `to` (`YYYY-MM-DD`, both required), also accepted by every chart endpoint. `to` is capped at yesterday and `from` raised to the first day with data. Malformed, impossible, backwards or wholly-future dates get 400. The date pickers are bounded to the data.
- [x] Heatmap page: date (x) × half-hour of day (y) for electricity; colour = kWh; tooltip shows the local time and value. On the day the clocks go back, 01:00 and 01:30 happen twice: the cell shows their average, and the tooltip says so.
- [x] Typical-day profile: mean and median kWh per half-hour slot for weekdays vs weekends over the selected range
- [x] Base load: the 10th percentile *(illustrative - confirm in implementation)* of half-hourly electricity between 00:00–05:00 local, shown as watts and as estimated £/year at the current average unit rate. That's the nearest-rank 10th percentile. The "current" rate is the kWh-weighted unit rate over the last 30 days that have one.
- [x] *Added after M12 (owner request):* the base-load time window is chosen on the page, not fixed at 00:00-05:00: a from and a to on the half hour (an end of 00:00 is midnight; an end at or before the start runs past midnight, and the morning part belongs to the evening's night), over the chosen dates. The tile shows the figure for that window, and a chart shows each night's base load (the same lowest-10% rule applied to that night's half-hours). A night with a missing half-hour in the window is a gap in the chart, never estimated. The window survives changing the dates and vice versa (`bl_from`, `bl_to`, both or neither; anything else is a 400); the default is still 00:00-05:00.
- [x] Gas shown at daily granularity on the heatmap page (OVO gas half-hourly may be sparse; render whatever exists)
- [x] `ovo-insights demo` command (**owner checkpoint 3**, moved from M11): generates a synthetic two-year dual-fuel dataset with a realistic shape (seasonal heating, weekday/weekend profiles, an overnight base load, a few deliberate gaps and both DST days) into a throwaway temp data dir, then starts the server against it. It never touches the real data dir or Keychain. Deterministic for a given `--seed`. Also deterministic for a given date. It includes a price rise and a matching sync history. In demo mode, login, syncing and forgetting are switched off and every page carries a banner. The temp folder (created 0700) is deleted on exit, including after Ctrl-C. *(If the process is killed outright it stays behind in the OS temp folder.)*
- [x] Empty and partial states: "no data yet — run a sync" and "N days missing in this range (see Readings & gaps)" banners instead of misleading zeros. Missing means no daily row for a fuel after that fuel's first data, or a half-hourly day recorded as a gap or incomplete.
- [x] Charts receive data via a JSON endpoint per chart; numbers are serialised as strings for money to avoid float drift, then formatted client-side. Endpoints: `/api/charts/{usage,yoy,heatmap,profile,gas-daily}`. kWh is rounded to 3 dp, missing values are `null`, and a bad `range` or `fuel` gets 400.
- [x] Tests: analysis functions with hypothesis (totals invariant to slicing; DST days; empty ranges); Playwright smoke that the dashboard renders charts from a seeded DB. **Playwright moved to M12** (owner's decision), which already plans browser end-to-end tests; it would have meant a ~150 MB browser download now. Instead, the charts were checked in a real browser against `ovo-insights demo`: they render, there are no CSP violations in the console, phone width works with no sideways scrolling, and dark mode works. That check found and fixed a heatmap bug (it was coloured by the wrong value). Server-side, every chart payload and page is tested against the demo dataset, including money adding up to the penny and 46/48 cells on DST days.
- [x] *Added:* ECharts 6.1.0 vendored with the owner's approval. The npm tarball was verified against the registry's sha512 (Apache-2.0; `LICENSE` and `NOTICE` included), and its SHA-256 is pinned in `MANIFEST.txt`. At ~1.1 MB it has a narrow exception from the 500 KB large-file hook. It's loaded only on chart pages. Its one `new Function` call is a JSON fallback for geo maps, which we never use. Tooltips use canvas "rich text" mode, since HTML tooltips would need inline styles that the CSP blocks. The CI wheel smoke test now checks every vendored file against the manifest.
- [x] **Owner step:** ran `ovo-insights demo`, then `ovo-insights serve` on the real data, and compared a month's totals on the dashboard with the OVO app (2026-10-13). **Owner checkpoint 3 complete.**

### M8 - Readings and data completeness

Depends on M2 and M6.

- [x] Readings table per meter/register: date, value, source, lifecycle, type; sortable. *Refined:* a picker chooses one register at a time (the owner has 5 meters and about 1,150 readings), the table is sorted server-side by clicking a column heading (plain links, so no inline script), 100 rows a page. Meters are shown as "Electricity meter 1", never by serial. Active meters come first. The page's range bar also covers meter readings: "All" starts at the oldest reading (1996-era reads included), not at the first smart-data day, and a range ending yesterday includes today's reading, which closes yesterday's window.
- [x] Derived consumption between successive readings, shown alongside the smart-data total for the same window; mismatches over 5% flagged. *Refined:* a window from reading date `a` to `b` covers the days `a` up to but not including `b` (confirmed on the owner's electricity: the differences equal the daily totals to the last decimal). Flagged when derived and smart kWh differ by more than 5% **and** by at least 0.1 kWh, so meter rounding on a tiny window can't flag. A window is compared only when everything is known; otherwise it says why: no smart data (before it started), smart data has gaps (n of N days), reading went down (a rollover or meter swap; never guessed), unsupported unit, or a meter with several registers (not summed). All arithmetic is exact `Decimal`.
- [x] *Departure (gas):* gas is **not** converted with one calorific value. The owner's bill gives the formula (kWh = volume × 1 for metric or 2.83 for imperial × calorific value × 1.02264 ÷ 3.6) and says the calorific value changes daily (37.5 to 43.0), so no single figure exists. First plan was a figure the owner types in; dropped for that reason (and its settings table and migration with it). Instead the formula runs backwards: for each gas window, the **implied calorific value** from the smart-data kWh and the register's volume. A window is flagged when that falls outside the bill's 37.5 to 43.0 range. Gas windows therefore show volume (m³), smart kWh and implied calorific value, not a derived kWh. The register unit labels OVO stores are `SCMH` (metric, factor 1) and `SCFH` (imperial, factor 2.83); both are flow-rate names, so they are trusted only through the bill's own statement and the implied value having to land inside the bill's range. On the owner's data the implied value sits at about 39.4 MJ/m³ across two years, which supports the metric reading.
- [x] Completeness calendar: one cell per day per fuel — complete / partial (n of expected slots) / missing / not yet available (today). *Refined:* plus a fifth state, **no supply** (outside the fuel's supply dates, taken from the supply points across both accounts, end dates inclusive; nothing known means supplied). Plain HTML and CSS squares (a week per column, Monday at the top; title text and `aria-label` give the date and "46 of 48 half-hours"), not an ECharts calendar: it needs no script, and the cells are testable as HTML. Days before a fuel's first stored data aren't drawn (they were never downloaded, which isn't "missing"), and the page says so. Today's cell is added when the range ends yesterday. A partial day says whether it is a known gap or will be retried (from `sync_state`).
- [x] Completeness summary: % half-hour coverage per fuel over the selected range. *Refined:* also shows % of days with a daily figure. Expected half-hours follow the sync engine's rule (46/48/50 for a day OVO flags as having half-hourly data, or when slots arrive anyway). A day with no daily row expects a full day once the fuel has ever had half-hourly data, so whole missing days pull the percentage down; a fuel that never had any (the owner's gas) shows "n/a". Today and days with no supply count towards neither. The days the calendar marks missing or partial are exactly the days `analysis.queries.missing_in` reports (tested against the demo data), so the banners and the page never disagree.
- [x] Tests: completeness calculations across DST changes and supply start/end dates (46/50-slot days complete when whole and partial when one short, Hypothesis over 2015–2035; supply boundaries inclusive; two supply periods; supply ending on a clock-change day), the readings windows (floor, exactly-5% edge, went-down, several registers, gas implied calorific value, telescoping property), the new SQL reads against a real temporary database, and the page, sorting, paging, refusals and chart JSON against the demo data.
- [x] *Added:* `ovo-insights demo` now includes synthetic meters and readings: daily electricity reads and monthly gas reads that agree with the smart data, two removed meters from before it, and deliberate faults that the page should catch (a bad estimated electricity read and the read after it going down, a bad estimated gas read, and the three days missing from the smart data).
- [x] *Added:* a chart on the page (`/api/charts/readings`, the same JSON-per-chart pattern): derived against smart kWh for electricity, implied calorific value against the bill's range for gas, flagged windows in red.
- [ ] **Owner step:** open **Readings & gaps** on the real data and compare a window against the OVO app (see the PR description for what to check).

### M9 - Tariff comparison

Depends on M2, M6 and M7's query layer. **Built in two stages (two PRs):** stage 1 was everything below the page (storage, rules, engine, tests); stage 2 the editor, comparison view, chart and demo tariffs.

Decisions agreed with the owner before stage 1:
- Electricity days with no half-hours (before 2025-02-11, and known gaps): a flat tariff is costed from the daily kWh, a time-of-day tariff only on days with every half-hour. A day counts only if the baseline and **every** column can cost it; otherwise it leaves all of them, and the count and reason are shown.
- Versions of one tariff (same name and fuel, non-overlapping effective dates) make one comparison column, so a mid-range price change is one column. Same-name overlaps are refused.
- The baseline is OVO's usage cost plus its standing charge; a missing cost or standing charge makes the day "not known" and excluded, never zero.
- OVO rounds each day's usage cost to the penny (half-even) and adds the standing charge exact (confirmed: all 1,484 stored daily costs on the real account equal that); the engine does the same.
- Bands are on half-hour boundaries in UK local time; a band applies on the local calendar day of the slot itself, so "Monday 23:30 to 05:30" is two parts of Monday and nothing carries into Tuesday. Limits: unit rate 0 to 500 p/kWh, standing charge 0 to 1,000 p/day, both inc. VAT.
- Gas is one unit rate plus the standing charge, costed from daily kWh. Comparisons are per fuel.

Stage 1 (done):
- [x] Migration `0004_tariffs.sql` adds `tariffs` and `tariff_bands`: integer milli-pence, bands as half-hour slot ranges and a weekday bitmask, `CHECK`s mirroring the rules. Coverage and version overlap are checked in code (`storage/tariffs.py`), inside the write's transaction.
- [x] Validation (`tariffs.py`): `build_tariff` turns raw input into a `Tariff` or raises `TariffError` listing every problem with its field (`name`, `fuel`, `standing`, `effective_from`, `effective_to`, `bands`, `bands[i].start|end|rate|weekdays`); the constructors re-check on every read from the database. A band whose end isn't after its start wraps past midnight and is split in two; `00:00` as an end means midnight. *Refined:* **all seven weekdays** must have all 48 slots covered exactly once (the PRD said "each selected weekday", but a weekday with no rate can't be costed), and the messages name the day and times ("Saturday has no rate for 06:00-07:30"). A gas tariff has a single unit rate.
- [x] Optional effective-from/to dates (inclusive), with the version-overlap rule above.
- [x] Re-costing engine (`analysis/recost.py`): flat tariffs from daily kWh, time-of-day tariffs per half-hour matched by UK wall-clock slot and local weekday (so 46- and 50-slot clock-change days cost correctly), a day's usage rounded to the penny, standing exact. Returns a per-day cost or the reason it can't be costed.
- [x] "Actually paid" baseline: OVO's daily cost plus standing charge (`recost.actual_cost`). *Resolves the open question:* standing is always separate, and always present on the real account.
- [x] Gas tariffs re-cost at daily granularity (single rate + standing).
- [x] Like-for-like selection in the engine (`recost.compare`): totals, difference in £ and %, and a per-month breakdown, over exactly the days every column and the baseline can cost, with each excluded day's reason.
- [x] Tests: hand-calculated golden cases (flat, two-band Economy 7 with a wrapping band, a four-band EV tariff with a weekend rate, the 46-slot and 50-slot clock-change days), property tests (a flat tariff's total is rate × kWh + standing × days, to within half a penny a day for OVO's rounding, and exactly equal to the sum of penny-rounded days; two-band cost equals an independent wall-clock calculation on any date 2015–2035; the weekday comes from the slot's local date), validation and storage tests, and the SQL read of each day's half-hours (`queries.costing_days`).

Stage 2 (done):
- [x] Tariff editor (`/tariffs`): name, fuel, standing charge (p/day), optional effective-from/to dates, and band rows of from, to, p/kWh and weekday boxes; rates **inc. VAT**, stated on the form; field-level errors from `TariffError` shown beside the fields (and gaps/overlaps in plain words at the top), with what was typed kept. *Refined:* a plain HTML form posted with HTMX and the CSRF header, no script. Bands are fixed rows (4 to 10 shown, empty rows skipped) because the form parser takes single values only; the parser's field and size limits are raised for this route only. A wrapped band is split on saving and joined again when editing. Starting points with blank rates (flat, Economy 7, EV overnight, gas) answer the PRD's "ship 2-3 example templates" mitigation. Delete asks first.
- [x] Comparison view: for each fuel with tariffs, a table of usage, standing and total for what OVO charged and each tariff, with the difference in £ and % and "cheaper/dearer"; a stacked bar chart (`/api/charts/tariffs`, money as exact strings); a month-by-month table. Uses the same range bar as the other pages.
- [x] The count of excluded days, and why, shown on the comparison ("2 days left out of every figure so the comparison stays like for like": no daily figure; half-hours incomplete; and so on). When nothing can be costed in the range it says so instead of showing zeros.
- [x] Demo data gains sample tariffs: "OVO standard" (the demo's own rates, as dated versions either side of its price rise, so it matches what was charged to the penny), Economy 7 and EV overnight examples, and a cheaper-gas example.
- [x] Tests: the page, the chart JSON, every refusal, the editor round-trip (every example tariff and a Hypothesis search over rotated bandings come back from the form unchanged), saving, editing, deleting, CSRF, size limits, and the demo.
- [x] **Owner step:** enter the current OVO tariff and compare a full month's re-costed total with what OVO charged (M12 target: within 1%)

### M10 - CSV export

Depends on M2 and M6 (and M7/M9 for summary sheets).

Decisions agreed with the owner before building:
- Usage is **household totals** (accounts added together), as on every page, so an export matches what's on screen.
- Meters appear as "Electricity meter 1", never by serial number (same as the pages).
- A new **Export** page in the menu, with the same range bar as the other pages, lists each file with its row count and a download link, plus one zip of everything. Downloads are read-only `GET` links behind the session cookie, with `no-store` and `attachment` headers.
- A dataset with nothing in the range is still offered, as a header-only file (and is in the zip), so the file set is always the same.

- [x] "Export CSV" for the selected date range, one file per dataset: monthly, daily, half-hourly electricity, half-hourly gas, readings, tariff comparison; offered individually and as a single `.zip` of all of them. *Refined:* monthly and daily are one file each with a `fuel` column; months overlapping the range are included; the readings file includes today's reading when the range ends yesterday (as the Readings page does); the tariff comparison has one row per fuel, tariff and month (including a "What OVO charged" baseline), over the days every tariff can be costed, and only for fuels that have tariffs.
- [x] Streamed (generator-based): rows are produced one at a time and written in chunks, the half-hours straight from a SQL cursor, and the zip is written as a stream too (checked on two years of half-hours: about 40 pieces of under 30 KB, never the whole archive at once). `Content-Disposition` filename `ovo-insights-<dataset>-YYYY-MM-DD_to_YYYY-MM-DD.csv`. The download opens its own read-only connection in the worker thread (`check_same_thread=False`, used one chunk at a time).
- [x] UTF-8 **with** BOM (decided: Excel on a Mac needs it for `£`; Python and pandas read it with `utf-8-sig`), `\r\n` line endings, header row, stdlib `csv` with `QUOTE_MINIMAL`.
- [x] CSV-injection defence: text from OVO or the user (register, unit, meter status, source, lifecycle, type, tariff names) beginning `=`, `+`, `-`, `@`, tab or carriage return is prefixed with `'`; numeric columns are never touched.
- [x] Money and time columns. *Departure:* amounts (`..._gbp`) are pounds to exactly 2 decimal places as planned, but **rates and standing charges are pence to exactly 3 decimal places** (`..._pence`), not 2 dp pounds, because £0.54 would lose a 53.68p standing charge and a unit rate can't be shown in pounds to 2 dp at all. kWh are 3 dp via `Decimal`, never a float. Half-hours carry `utc_time` and `local_time` (UK time with its offset); an unknown value is an empty cell, never 0.
- [x] Column names and units documented in `docs/csv-format.md` as a stable contract with a changelog; a test fails if the documented columns and the columns the code writes disagree.
- [x] Tests: each export parsed back with `csv.DictReader` (headers, row counts equal the database and the page), the formula sentinel (`=HYPERLINK(...)`) and a tariff name with quotes both come back guarded, money round-trips to the penny, the 50-slot and 46-slot days carry the right offsets (the repeated 01:00 hour is +01:00 then +00:00), the zip's files are byte-identical to the single downloads, every refusal (unknown file, bad dates, no session), and Hypothesis properties for the guard, the CSV round trip and kWh rounding.
- [x] **Owner step:** export a month from your real data and open it in Excel or Numbers: it should open cleanly (no garbled characters), and a daily row's `usage_cost_gbp` and `kwh` should match the OVO app for that day.

### M11 - Packaging, docs and release

Depends on M5, M7–M10.

- [x] `pyproject.toml` entry point `ovo-insights = ovo_insights.cli:app`; `ovo-insights --version` *(both already existed from M1 and M3; the release workflow now also checks the built wheel's `--version` equals the project version)*
- [x] README, rewritten user-first: disclaimer, what it does, four screenshots from `ovo-insights demo` (`docs/screenshots/`, under 65 KB each, with the demo banner visible), install, first run, what's in the app, where data lives (data folder, logs, the one Keychain item), how to delete everything (data, login, program, exported files), a security summary, and "if it breaks: run `ovo-insights doctor` and open an issue (redacted output only)". Install was tested into throwaway tool folders: from a local checkout, from `git+ssh://git@github.com/sjanaway/ovo-insights`, `--reinstall` and uninstall. `tests/test_docs.py` fails if the README shows a command or flag that doesn't exist, or if any relative link, image or anchor in the docs is broken. *(The Development section is short and points at CONTRIBUTING.md.)*
- [x] Issue templates (`.github/ISSUE_TEMPLATE/`): bug report and OVO API-change report. Both ask for `ovo-insights doctor` output, warn against passwords, tokens, cookies, account numbers, MPAN/MPRN, meter serials and addresses, and have a required privacy checkbox; blank issues are off and security reports are pointed at SECURITY.md.
- [x] Release workflow (`.github/workflows/release.yml`): pushing a `vX.Y.Z` tag checks the tag equals the project version, takes the release notes from the matching `CHANGELOG.md` section (`scripts/release-notes.sh`, which fails if there isn't one), runs the tests, builds the wheel and sdist with `SHA256SUMS`, smoke-tests the wheel, then a separate job (the only one with write access, tags only) creates the GitHub release with the `gh` CLI. No third-party actions and nothing goes to PyPI. Actions are pinned by SHA, enforced by a test. The same build runs as a dry run (no release) on pull requests that change the workflow, changelog or version, and from the Actions tab. `CHANGELOG.md` added.
- [x] `docs/security.md`: threat model (local malware out of scope; other local users, malicious websites in the same browser, DNS rebinding, supply chain and log leakage in scope, plus not being impolite to OVO), each mitigation with the test that enforces it (a test checks the cited test files exist), and the known limitations stated plainly. SECURITY.md now links to it.
- [ ] **Owner step:** read the README as a stranger would: do the install and first-run steps work for you, and does anything confuse you?

### M12 - Verification

Run before tagging v1.0.0. Each check, how it is run and its evidence are in [docs/verification.md](../verification.md).

- [x] CI green on macOS and Ubuntu: ruff, mypy `--strict`, pytest with coverage gate, pip-audit (no known vulns), Semgrep (no high/critical findings), gitleaks (no findings), vendor-manifest hash check, fixture-PII check *(green on `main` after M11, 2026-10-14; the links are in the verification page)*
- [ ] *Optional, deferred by the owner:* Playwright e2e on macOS against `ovo-insights demo`. The owner decided not to add a browser-automation dependency and a ~150 MB browser download: the app is small and was tested by hand. Page content, chart JSON, forms, downloads, the CSP and every refusal are covered by HTTP-level tests, and each milestone's UI was checked in a real browser (no console or CSP errors), which also covers M7's deferred "dashboard renders charts" check.
- [x] Security smoke (scripted), `tests/web/test_security_smoke.py`, against a real server on a real socket: a request with `Host: evil.example` gets 400; the session cookie is `HttpOnly`/`SameSite=Strict`; cross-site POSTs are refused even with a stolen cookie or the real CSRF token; no CORS headers; the listener is on `127.0.0.1` only (a connection to the machine's other address is refused, and `lsof` shows no wildcard listener). *Changed:* the PRD's "from a second origin page, `fetch(...POST)` fails" is now a one-off check in a real browser (a page on another origin, which is the same *site*, so the strict cookie was still sent): every request was blocked or refused and nothing changed (2026-10-14).
- [ ] Secret-leak smoke: after a real login + full sync, search for the password, the refresh token and the access token across the data dir, the logs and `~/Library/Caches`. *Changed:* tokens live only in memory, so they can't be searched for by value. `ovo-insights dev verify` searches for the password (data folder and caches) and for the *shape* of a login token (data folder only: the caches hold other apps' tokens). It prints file names and counts, never what it finds. **Owner step:** run it after a real login and sync.
- [x] Permissions: data dir `0700`, DB and logs `0600` *(automated in the test suite; `ovo-insights dev verify` also checks the real folder: see the owner step above)*
- [ ] Real-account run (owner's account): `ovo-insights doctor` all pass; full 2-year backfill completes; spot-check 5 random days and 2 months against the OVO app (kWh and £ match to the penny) *(backfill and spot-checks done 2026-10-13, M5; **owner step:** re-run `doctor` just before tagging)*
- [ ] Resilience: kill the process mid-backfill, restart, and confirm it resumes without duplicates or gaps; disconnect the network mid-sync and confirm a `partial` run with a clear UI message. *Automated:* both are tested (a failing request and a dropped connection; the resumed database is identical to an uninterrupted one). *Added:* the Sync page now explains in plain words how the last run stopped ("OVO couldn't be reached…", "Everything it saved is kept…"), not just a class name. **Owner step:** the same with a real process and a real network (commands in the verification page).
- [x] Tariff check: enter the owner's actual current OVO tariff; the re-costed total is within 1% *(illustrative)* of OVO's actual for a full month with complete data *(done by the owner, 2026-10-14: the M9 owner step)*
- [ ] Fresh-machine install: `uv tool install` from the built wheel on a clean macOS user account → `serve` → setup → sync works *(**owner step**; commands in the verification page. The install from a local checkout and over SSH was tested into throwaway folders in M11, and CI smoke-tests the wheel)*

## Out of scope / future additions

- Excel (.xlsx) export - *deliberate; the owner chose CSV only for v1 (CSV opens in Excel/Numbers anyway). Raw SQLite is documented in the README for power users.*
- Public repo, PyPI release, issue templates for outside users - *deferred; future decision once v1 has met the success metric. Before going public: re-run the fixture-PII and gitleaks checks over full git history, and resolve the ToS/trademark flags below.*
- Octopus/other public tariff price feeds (Agile, Go, Tracker) - *deferred; future PRD once manual tariff comparison is in use and the owner wants dynamic-price modelling.*
- Other suppliers (Octopus, EDF, n3rgy/DCC direct access) - *deferred; future PRD when there's demand. The adapter boundary in M3 keeps this possible.*
- Live/real-time data (Glow IHD, CAD, MQTT) - *deliberate; OVO data is day-behind by design and v1 is about history.*
- Solar, battery, export and EV-specific analysis - *deliberate; the owner has no generation. Revisit if contributors need it.*
- Mac `.app` bundle / menu-bar app - *deferred; future PRD if CLI install proves a barrier for non-technical users.*
- Docker image - *deliberate; Keychain isn't available inside containers and it conflicts with the credential decision.*
- Multi-user / LAN access - *deliberate; localhost only is a core security property.*
- Carbon intensity, OVO "carbon" endpoints, direct-debit recommendations - *deferred; low value for the success metric.*
- Windows and Linux official support - *deferred; best-effort in v1. Add CI runners and promote when someone volunteers to maintain them.*
- Raw API response dumping - *deliberate; the reference repo's `DumpData` leaked tokens. `capture-fixtures` with redaction is the only path.*

## Open questions to resolve during implementation

- ~~Does OVO's daily `cost` include the standing charge?~~ **Resolved:** no; `rates.standing` (£/day) is separate, and present on every day of the real account. The M9 "actually paid" baseline is `cost + standing` per day.
- ~~Does `half-hourly-local` return both fuels in one call?~~ **Resolved:** yes, one response with `electricity` and `gas` blocks. *(Original question:)* Does the `half-hourly-local` endpoint return electricity and gas together or need one call per fuel? Confirm in M3; affects the request count and the M5 rate-limit maths.
- *(Parked: this account has no gas half-hourly data. Meter readings are a separate matter: see M8.)* Is gas half-hourly consumption reported in kWh or m³? timmo001's mock data shows `m³`. The client currently rejects non-kWh units; if real data is in m³, add a calorific-value conversion (or store volume separately) before M5.
- ~~Which half-hourly endpoint, and are timestamps zoned?~~ **Resolved:** both work, both naive UTC; `half-hourly-local` is the default.
- Exact token lifetimes (access and refresh) - read from real JWTs in M3 and set refresh margins accordingly. *(M5: the access token appears to last a minute or less; the refresh margin now scales to the lifetime. Confirm the figure from the `-v` log line.)*
- Does the Bootstrap GraphQL query still return supply-point start dates as shown in the reference repo (Dec 2025)? Confirm with `doctor` in M3.
- Which `Retry-After`/challenge signals does OVO's Cloudflare front actually return? Capture one if seen; until then, detect `403` + `text/html` + `cf-ray` header as `BlockedByUpstream`.
- ~~Fixed default port vs OS-assigned~~ **Resolved in M6:** OS-assigned. The single-use launch link makes bookmarks moot.

## Pre-mortem flags

**Technical risks (will the build itself fail?):**
- **OVO changes or blocks the unofficial API.** 2025 community reports show auth-flow changes, 404s on account selection and Cloudflare IP blocks; this is the single biggest risk. Caught by: M3 (schema-drift warnings, error taxonomy, `doctor` canary), M5 (circuit breaker, no retry storms), M11 (API-change issue template). The local archive keeps working when the API doesn't.
- **If it's open-sourced later, many users collectively hammer OVO**, triggering blanket blocks for everyone. Caught by: M3 rate limiter, M5 randomised schedule window and skip-complete-periods, M5 2-year cap.
- **Real data lands in git history via fixtures or screenshots and becomes public if the repo is opened later.** Caught by: M3 redaction tool + fixture-PII CI check, M1 gitleaks, M7 `demo` synthetic data for screenshots.
- **DST and timezone bugs** silently misalign half-hours (the reference repo's `half-hourly-local` naming suggests local timestamps). Caught by: M2 property tests, M5 completeness logic, M12 real-account spot checks on a DST day.
- **Float money drift** makes tariff comparisons disagree with OVO by pennies. Caught by: M2 integer milli-pence decision, M9 golden tests, M12 penny-match spot checks.
- **No secure keyring on some Linux setups** leads to a plaintext fallback. Caught by: M4 backend detection and refusal.
- **Claude-written code passes tests that are themselves weak.** Caught by: hypothesis property tests and golden cases in M2/M7/M9, the coverage gate, the M12 real-account reconciliation. Consider mutation testing (`mutmut`) on `analysis/` and `money` if bugs slip through.
- **Unverified upstream facts** (token lifetimes, cost semantics, endpoint shapes) are carried over from a Dec 2025 reference. Caught by: Open questions + M3 live capture.

**Strategic risks (will it replace the OVO portal even if it ships?):**
- **OVO data lags a day**, so "how much today?" still sends the owner to the OVO app. Accept and state it in the UI; real-time is out of scope.
- **Manual tariff entry is tedious**, so comparisons go unused. Mitigation: ship 2–3 example tariff templates (flat, Economy 7, a generic EV two-band) with rates left blank, in M9.
- **An API breakage stalls auto-sync for weeks** and the owner drifts back to the portal. Mitigation: M5 circuit breaker makes the problem visible immediately; `doctor` speeds up fixing.
- **OVO terms of service** may restrict automated access to customer accounts. `[UNVERIFIED - confirm before any public release]`. The README disclaimer + per-user, own-credentials-only use reduces but does not remove this.
- **Using "OVO" in the name** may draw a trademark request. `[UNVERIFIED - confirm before any public release]`. Low cost to rename later if asked; keep the package name a single constant.

## Research grounding

Checked 2026-10-13 via web search and the GitHub API.

- Reference implementation endpoints and auth flow (login → `restricted_refresh_token` cookie → token endpoint → Bearer JWT; Kaluza GraphQL for accounts/readings; smartpaymapi usage endpoints with `date=YYYY`, `YYYY-MM`, `YYYY-MM-DD`) - source: https://github.com/MikeWilliams-UK/My-Ovo-Data (v1.0.9, last commit 2025-12-23)
- The community Python client timmo001/ovoenergy is actively maintained (dependency bumps October 2026) - source: https://github.com/timmo001/ovoenergy
- 2025 reports of OVO API auth failures, 404s and Cloudflare IP blocks for community integrations - sources: https://discourse.nodered.org/t/ovo-energy-api-get-your-energy-data-from-ovo/87440?page=3, https://forum.ovoenergy.com/general-153/api-unable-to-access-smartpaym-ovoenergy-com-17702
- OVO usage data is available only up to the end of the previous day - source: https://forum.ovoenergy.com/my-smart-home-138/trying-to-connect-home-assistant-to-ovo-api-17690
- `keyring` supports the macOS Keychain on macOS 11+ and has an active 25.x line (25.7.0, Nov 2025) - sources: https://keyring.readthedocs.io/, https://github.com/jaraco/keyring/
- FastAPI is still pre-1.0 (0.13x line in 2026), so minor releases can break; pin exactly - source: https://releasebot.io/updates/tiangolo/fastapi
