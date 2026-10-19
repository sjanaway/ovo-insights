# Security model

ovo-insights runs on your Mac, holds your OVO login and years of how you use energy, and
talks to OVO's private APIs. This page says what it defends against, how, and what it does
not defend against. To report a problem, see [SECURITY.md](../SECURITY.md).

Every mitigation below is checked by a test, named at the end of its section, so a change that
weakens one fails the build. The checks run before a release, and `ovo-insights dev verify` for
your own machine, are in [verification.md](verification.md).

## What's protected

| Asset | Where it lives |
|---|---|
| Your OVO email and password | The macOS Keychain only (one item: service `ovo-insights`). Never a file, a log or a web page. |
| OVO login tokens | Memory only, while a sync or `doctor` runs. |
| Your downloaded usage, readings and tariffs | A SQLite database in the data folder (`~/Library/Application Support/ovo-insights/`), readable by you only (folder `0700`, files `0600`). |
| The logs | The data folder's `logs/`, `0600`, with secrets masked. |

## In scope

### 1. Other users on the same Mac

They must not be able to read your data, or drive the web UI.

- The data folder is created `0700` and the database and logs `0600`. The app refuses a folder
  or file that is a symlink, owned by someone else, or has looser permissions, and prints the
  `chmod` that fixes it. (`tests/test_paths.py`, `tests/storage/test_db.py`)
- The web UI listens on `127.0.0.1` only. There is no `--host` option and no way to configure
  another address; the socket is bound in our code and handed to the server.
  (`tests/web/test_server.py`)
- Even on the loopback address, every request needs a session cookie that only the browser
  you launched from has (see 2).

### 2. Malicious websites in the same browser

A page you have open must not be able to read your data or press buttons in the app.

- **Per-launch token, single use.** `serve` prints a link with a random 32-byte token. The first
  request swaps it for a session cookie and redirects to a URL without the token. The token
  works once, so a link left in browser history or the terminal can't be replayed.
- **Cookie:** `HttpOnly; SameSite=Strict; Path=/`. Other sites can't read it, and the browser
  doesn't send it on cross-site requests. Every request without a valid session gets 403.
- **CSRF:** anything that changes state needs the session's CSRF token in a header (set by the
  page, never by a form field) **and** a same-origin `Origin` (or `Sec-Fetch-Site`), else 403.
- **No cross-origin reads:** there is no CORS, no WebSocket, no `/docs` or `/openapi.json`.
- **Strict Content-Security-Policy** on every response: `default-src 'self'`, no inline script
  or style, no `eval`, `frame-ancestors 'none'`, `form-action 'self'`, plus `nosniff`,
  `no-referrer` and `Cache-Control: no-store`.
- **Templates:** autoescaped; no `|safe`; no inline `<script>`, `<style>`, `style=`,
  event-handler attributes, `hx-on` or `javascript:` URLs. A policy test fails on any of them.
- **Downloads** (CSV export) are read-only `GET`s behind the same session cookie. In CSV files,
  text from outside that starts like a formula gets a leading `'`.

(`tests/web/test_security.py`, `tests/test_code_policy.py`, `tests/web/test_export.py`)

### 3. DNS rebinding

A hostile site could point its own name at `127.0.0.1` and then talk to the app as if it were
the same site.

- The `Host` header must be exactly `127.0.0.1:PORT` or `localhost:PORT`; anything else, or a
  repeated `Host` header, is refused with 400, including for static files.
- The session cookie and CSRF token would also be missing from such a request.

(`tests/web/test_security.py`)

### 4. Supply chain

- **Runtime dependencies are an allowlist** (`tests/test_dependency_policy.py`); a new one needs
  a stated reason in the pull request.
- **Locked and audited:** `uv.lock` is committed and CI installs with `--locked`; `pip-audit` runs
  on every change, plus Semgrep, gitleaks (secrets) and weekly Dependabot updates.
- **GitHub Actions are pinned by commit SHA**, so a moved tag can't change what runs.
  (`tests/test_docs.py`)
- **Vendored scripts** (HTMX, ECharts) are pinned by version and SHA-256 in
  `static/vendor/MANIFEST.txt`; a test and the CI wheel check fail if a file changes. They load
  from the app itself, never from a CDN.
- **Telemetry is off:** FastAPI's built-in OpenTelemetry hooks are disabled and no exporter is
  installed; there is no analytics, crash reporting or update check.
- **Outbound connections** go only to OVO's three hosts (`my.ovoenergy.com`,
  `smartpaymapi.ovoenergy.com`, `api.eu1.prod.kaluza.com`). Any other host, including through a
  redirect, is refused; TLS verification can't be turned off; redirects aren't followed.
  (`tests/ovo/test_auth_redact_transport.py`, `tests/ovo/test_client.py`)

### 5. Leaks through logs, errors and files

- **Log redaction:** a filter on the root logger masks `Authorization` headers, cookies, the
  refresh-token header, JWT-shaped strings, passwords and email addresses.
  (`tests/ovo/test_auth_redact_transport.py`)
- **Request URLs are not logged**, because they contain account numbers: the HTTP libraries'
  loggers are pinned to WARNING, and the web server's access log (which would record the launch
  token) is off.
- **A sentinel password** is run through login, `doctor` and logout with debug logging on and
  appears only in the Keychain item, never in output, logs or any page.
  (`tests/test_credentials.py`, `tests/web/test_sync_web.py`)
- **Errors:** the web UI shows a short error ID and nothing else; the traceback goes to the
  (redacted) local log. Sync records error *class names* only, never messages.
- **No raw API dumps exist as a feature.** The `dev capture-fixtures` tool writes to a
  gitignored folder, and a redaction step plus a CI check keep real account numbers, meter
  serials, postcodes and emails out of the repository (`tests/test_fixture_privacy.py`).
- **Pages never show identifiers:** meters are "Electricity meter 1", accounts are numbered, and
  exported CSVs carry no serials.
- **The database** is only written through parameterised SQL; a policy test fails on any query
  built from strings (`tests/test_code_policy.py`).

### 6. Being impolite to OVO

OVO's APIs are private and unofficial. A bug (or many users) hammering them would get you, or
everyone, blocked.

- One request every two seconds at most, doubling when OVO says to slow down; waits of up to
  two minutes are honoured.
- Only transient failures are retried. A block, a refused login or a Cloudflare challenge
  stops the sync and pauses automatic syncing until you press Sync now.
- The daily sync runs at a random minute between 06:00 and 08:00, and one sync at a time is
  enforced with a file lock.

(`tests/sync/`, `tests/web/test_scheduler.py`)

## Out of scope, and known limitations

- **Malware already running as you.** It can read your data folder and ask the Keychain for
  your password the way this app does. Nothing here defends against that.
- **Your OVO account's own security** and OVO's services.
- **Files you export.** A CSV in your Downloads folder is outside the app's protection, and it
  shows when you're home. Delete it when you're done.
- **Browsers don't scope cookies by port.** If you visit another web server on `127.0.0.1`
  while `serve` is running, that server also receives the session cookie and could replay it to
  this app. It needs a malicious local listener *and* a visit to it, and a session only lasts as
  long as one `serve` process. Stop `serve` when you're not using it.
- **A password typed into the web Setup page** passes through the page and server memory
  on its way to the Keychain. It is checked with OVO first, never written to a file, and never
  echoed back. `ovo-insights login` in a terminal avoids the browser entirely.
- **Backups.** `ovo-insights backup` writes a `0600` copy into a `0700` folder (it refuses a folder
  that others can read, or one inside the data folder), and never overwrites or deletes unless
  asked. But a backup is a second copy of your data wherever you keep it: a cloud-synced folder
  uploads it, and one on an unencrypted drive can be read by anyone with the drive.
- **Disk encryption** is up to you (FileVault). The database itself is not encrypted.
- **Linux and Windows** are best-effort: the Keychain checks refuse insecure backends rather
  than store your password in a file, but these platforms aren't tested in CI beyond Ubuntu.
