-- 0003: sync bookkeeping (deferred from M2 until M5 defined its semantics).
--
-- sync_runs: one row per sync attempt. 'running' rows left behind by a crash are closed as
-- 'partial' (error_class 'Interrupted') by the next run. Error classes are exception class
-- names only, never messages, so nothing from OVO or the login can end up here.
--
-- sync_state: one row per fetched period, keyed per account (OVO's usage endpoints are per
-- account, as in 0001), so holes in the middle of a range are represented exactly.
--   complete     data matched expectations and was fetched after the revision window: final
--   provisional  data matched, but OVO may still revise it: re-fetched on later syncs
--   incomplete   data missing: re-fetched on later syncs until the gap horizon
--   gap          still incomplete after the gap horizon: final, a known gap
--   error        the response failed validation: re-fetched on later syncs

CREATE TABLE sync_runs (
    run_id INTEGER PRIMARY KEY,
    trigger TEXT NOT NULL CHECK (trigger IN ('cli', 'manual', 'scheduled')),
    started_at_utc TEXT NOT NULL CHECK (started_at_utc IS strftime('%Y-%m-%dT%H:%M:%SZ', started_at_utc)),
    finished_at_utc TEXT CHECK (
        finished_at_utc IS NULL
        OR (finished_at_utc IS strftime('%Y-%m-%dT%H:%M:%SZ', finished_at_utc)
            AND finished_at_utc >= started_at_utc)
    ),
    status TEXT NOT NULL CHECK (status IN ('running', 'ok', 'partial', 'failed', 'blocked')),
    error_class TEXT CHECK (
        error_class IS NULL
        OR (length(error_class) BETWEEN 1 AND 64 AND error_class NOT GLOB '*[^A-Za-z]*')
    ),
    requests INTEGER NOT NULL DEFAULT 0 CHECK (requests >= 0),
    periods_fetched INTEGER NOT NULL DEFAULT 0 CHECK (periods_fetched >= 0),
    gaps_found INTEGER NOT NULL DEFAULT 0 CHECK (gaps_found >= 0),
    warnings INTEGER NOT NULL DEFAULT 0 CHECK (warnings >= 0),
    half_hours_written INTEGER NOT NULL DEFAULT 0 CHECK (half_hours_written >= 0),
    days_written INTEGER NOT NULL DEFAULT 0 CHECK (days_written >= 0),
    months_written INTEGER NOT NULL DEFAULT 0 CHECK (months_written >= 0),
    readings_written INTEGER NOT NULL DEFAULT 0 CHECK (readings_written >= 0),
    CHECK ((status = 'running') = (finished_at_utc IS NULL)),
    CHECK (status NOT IN ('running', 'ok') OR error_class IS NULL)
) STRICT;

CREATE TABLE sync_state (
    account_id TEXT NOT NULL REFERENCES accounts (account_id) ON DELETE CASCADE,
    granularity TEXT NOT NULL CHECK (granularity IN ('monthly', 'daily', 'half_hourly')),
    -- First day of the period: 1 January for monthly (one request per year), the 1st of the
    -- month for daily (one request per month), the UK day itself for half-hourly.
    period_start TEXT NOT NULL CHECK (
        period_start IS date(period_start)
        AND (granularity != 'monthly' OR substr(period_start, 6, 5) = '01-01')
        AND (granularity != 'daily' OR substr(period_start, 9, 2) = '01')
    ),
    status TEXT NOT NULL CHECK (status IN ('complete', 'provisional', 'incomplete', 'gap', 'error')),
    expected INTEGER NOT NULL CHECK (expected >= 0),
    received INTEGER NOT NULL CHECK (received >= 0),
    attempts INTEGER NOT NULL CHECK (attempts >= 1),
    last_fetched_at_utc TEXT NOT NULL CHECK (last_fetched_at_utc IS strftime('%Y-%m-%dT%H:%M:%SZ', last_fetched_at_utc)),
    last_run_id INTEGER REFERENCES sync_runs (run_id) ON DELETE SET NULL,
    note TEXT CHECK (note IS NULL OR length(note) <= 200),
    PRIMARY KEY (account_id, granularity, period_start)
) STRICT, WITHOUT ROWID;

CREATE INDEX idx_sync_runs_started ON sync_runs (started_at_utc);
