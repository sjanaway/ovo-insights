# Security policy

## Reporting a vulnerability

Please **do not** open an issue. Report it privately through GitHub instead: the
repository's **Security** tab, then **Report a vulnerability**
([direct link](https://github.com/sjanaway/ovo-insights/security/advisories/new)). If you
can't use that, email the maintainer at the address on their GitHub profile.

Include what you found, how to reproduce it, and its impact. **Never include real OVO
credentials, tokens, account numbers or meter data** in a report; use redacted values.

You should get an acknowledgement within 7 days.

## Scope

In scope: anything that could expose a user's OVO password, tokens or energy data; let a
website or another local user drive the local server; or make the app misbehave towards
OVO's APIs (for example, retry storms).

Out of scope: malware already running as the same user on the machine, and OVO's own
services.

## Security model (summary)

The full threat model is in [docs/security.md](docs/security.md). In short:

- The OVO password is stored only in the OS keychain; tokens only in memory.
- The web server binds to `127.0.0.1`, checks the `Host` header, and requires a
  per-launch session token and a CSRF token.
- No raw API responses or secrets are ever written to disk or logs.
- Dependencies are locked, audited (`pip-audit`) and scanned (Semgrep, gitleaks) in CI.
