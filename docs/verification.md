# Verification before v1.0.0

The M12 checklist, with how each check is run, who ran it, and the evidence. A row marked
**Automated** runs on every change; a row marked **Owner** needs the owner's real account or a
clean Mac, and is left blank until it has been done.

| # | Check | How | Status |
|---|---|---|---|
| 1 | CI green on macOS and Ubuntu | **Automated.** ruff, `mypy --strict`, pytest with the 90% coverage gate, `pip-audit`, wheel build and smoke test, Semgrep, gitleaks, the vendor-manifest hash check and the fixture-privacy check all run in CI on every pull request. | Passing on `main`: [CI](https://github.com/sjanaway/ovo-insights/actions/runs/38534519160), [Semgrep](https://github.com/sjanaway/ovo-insights/actions/runs/38534519003) (2026-10-14, after M11). The release workflow's dry run also passes: [run](https://github.com/sjanaway/ovo-insights/actions/runs/38534038981). |
| 2 | Browser end-to-end tests (Playwright) | **Optional, not done.** The owner chose not to add a browser-automation dependency (and a ~150 MB browser download): the app is small and was tested by hand. Page content, charts' JSON, forms, downloads, the CSP and every refusal are covered by HTTP-level tests, and the layout and chart drawing were checked in a real browser during each milestone (no console or CSP errors). | Deferred by choice. |
| 3 | Security smoke test | **Automated**, against a real server on a real socket: `tests/web/test_security_smoke.py`. A foreign `Host` gets 400; the cookie is `HttpOnly; SameSite=Strict`; cross-site POSTs are refused even with a stolen cookie or the real token; there are no CORS headers; the listener is `127.0.0.1` only (a direct connection to the machine's other address is refused, and `lsof` shows no `*:PORT`). | Passing. |
| 3b | The same, from a second origin, in a real browser | **One-off, by hand**, 2026-10-14, against `ovo-insights demo`: a page served from `http://127.0.0.1:8799` (another origin; the same *site*, so the browser still sent the strict cookie) tried a simple cross-origin form POST to create a tariff, a credentialed POST to `/sync`, a credentialed read of a chart's JSON and a POST with a guessed CSRF header. The browser blocked every response, the tariff list was unchanged (7 before, 7 after, no new tariff), and the session was still valid. | Done. |
| 4 | Secret-leak scan | **Owner.** After a real login and sync, run `ovo-insights dev verify` (see below). | |
| 5 | Permissions: data folder `0700`, database and logs `0600` | **Automated** in `tests/test_paths.py` and `tests/storage/test_db.py`, and checked on the real folder by `ovo-insights dev verify` (row 4). | Automated: passing. Real folder: |
| 6 | Real-account run: `doctor` all pass, full backfill, spot-checks against the OVO app | **Owner.** Done for the first full sync on 2026-10-13: five random days (both fuels, kWh and £) and two months matched (PRD, M5). Re-run `ovo-insights doctor` just before tagging. | Backfill and spot-checks done 2026-10-13. `doctor` re-run: |
| 7 | Resilience | **Automated:** `tests/sync/test_engine.py` kills a sync part-way (a failing request, and a dropped connection) and checks that the run is `partial`, that the next run finishes, and that the database is *identical* to one from a sync that was never interrupted; the Sync page's message is tested in `tests/web/test_sync_web.py`. **Owner:** the same with a real process and a real network, below. | Automated: passing. Owner: |
| 8 | Tariff check | **Owner**, done 2026-10-14 (PRD, M9): the owner's real tariff re-costed a full month; the target was within 1% of what OVO charged. | Done. |
| 9 | Fresh-machine install | **Owner**, below. | |

## Row 4 and 5: the leak scan and permissions

After you have logged in and synced, and used `serve` for a while:

```bash
ovo-insights dev verify
```

It checks that the data folder is `0700` and everything in it `0600` and yours, then searches the
data folder (database and logs) for your saved password and for anything shaped like a login
token, and `~/Library/Caches` for your password. It prints file names and counts, never what it
found, and exits 0 only if everything passed. It reports the data folder first, then searches
the caches, which can take several minutes (it says so, and prints "still searching…" every few
seconds). Press Ctrl-C to skip the caches search: it is then reported as `SKIP` and the run does
not count as a pass. `--no-caches` leaves the caches out. Files over 25 MB in the caches are skipped
(and counted): caches hold other apps' media and binaries, and anything that leaked from here is
small.

Login tokens only exist in memory, so they can't be searched for by value; the data folder is
searched for their *shape* (a JWT, a bearer token, a refresh-token cookie). That would be noisy
in the system caches, where other apps keep their own tokens, so the caches are searched for the
password only.

## Row 7: resilience with a real process and a real network

Use a throwaway data folder, so your real data isn't touched (it needs about two minutes of
requests to OVO, not 25):

```bash
export OVO_INSIGHTS_DATA_DIR=/tmp/ovo-resilience
ovo-insights login          # skip if your login is already saved
ovo-insights sync           # wait about two minutes, then kill it hard, from another terminal:
pkill -9 -f "ovo-insights sync"
```

Then:

```bash
ovo-insights sync --dry-run   # the periods fetched before the kill are not planned again
ovo-insights sync             # resumes; let it run a minute or two, then press Ctrl-C
ovo-insights serve            # the Sync page lists the killed run as "partial / Interrupted"
```

For the network: in `serve`, press **Sync now**, then turn Wi-Fi off after about 30 seconds. Within
a minute the Sync page should say "The last sync … stopped early. OVO couldn't be reached…
Everything it saved is kept, and “Sync now” carries on from where it stopped." Turn Wi-Fi back on
and press **Sync now**: it continues. Finally `unset OVO_INSIGHTS_DATA_DIR` and delete
`/tmp/ovo-resilience` (it is your usage data).

## Row 9: install on a clean Mac user

Build the wheel on your Mac, put it somewhere every user can read, then switch to a new macOS user
(System Settings → Users & Groups, then log in as them):

```bash
uv build && cp dist/ovo_insights-*.whl /Users/Shared/
```

As the new user:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh    # then open a new terminal
uv tool install /Users/Shared/ovo_insights-*.whl
ovo-insights --version
ovo-insights login
ovo-insights doctor
ovo-insights serve          # Setup → Sync now; let it sync for a minute, then stop
```

This is what a stranger with the released wheel would do (the wheel from the GitHub release
stands in for the `git+https` install). Afterwards delete the user and
`/Users/Shared/ovo_insights-*.whl`.
