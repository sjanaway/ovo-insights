-- 0001: accounts, supply points, usage at three granularities, meters, registers, readings.
--
-- Conventions:
--   * Every table is STRICT. Constraints repeat the domain invariants so that a bug or a
--     hand-edited database can't store something the domain layer would reject.
--   * Dates are 'YYYY-MM-DD' (UK local calendar). Instants are 'YYYY-MM-DDTHH:MM:SSZ' (UTC).
--     `x IS date(x)` rejects both bad formats and impossible dates such as 2025-02-30.
--   * Money is INTEGER milli-pence. kWh is REAL (exactness matters for money, not energy).
--   * Sync bookkeeping and tariffs arrive in later migrations (M5, M9).

CREATE TABLE accounts (
    account_id TEXT PRIMARY KEY NOT NULL CHECK (length(account_id) BETWEEN 1 AND 64)
) STRICT, WITHOUT ROWID;

CREATE TABLE supply_points (
    account_id TEXT NOT NULL REFERENCES accounts (account_id) ON DELETE CASCADE,
    fuel TEXT NOT NULL CHECK (fuel IN ('electricity', 'gas')),
    mpxn TEXT NOT NULL CHECK (length(mpxn) BETWEEN 6 AND 13 AND mpxn NOT GLOB '*[^0-9]*'),
    start_date TEXT NOT NULL CHECK (start_date IS date(start_date)),
    end_date TEXT CHECK (end_date IS NULL OR (end_date IS date(end_date) AND end_date >= start_date)),
    PRIMARY KEY (account_id, fuel, mpxn)
) STRICT, WITHOUT ROWID;

CREATE TABLE usage_half_hourly (
    account_id TEXT NOT NULL REFERENCES accounts (account_id) ON DELETE CASCADE,
    fuel TEXT NOT NULL CHECK (fuel IN ('electricity', 'gas')),
    interval_start_utc TEXT NOT NULL CHECK (
        interval_start_utc IS strftime('%Y-%m-%dT%H:%M:%SZ', interval_start_utc)
        AND strftime('%M:%S', interval_start_utc) IN ('00:00', '30:00')
    ),
    kwh REAL NOT NULL CHECK (kwh >= 0),
    fetched_at_utc TEXT NOT NULL CHECK (fetched_at_utc IS strftime('%Y-%m-%dT%H:%M:%SZ', fetched_at_utc)),
    PRIMARY KEY (account_id, fuel, interval_start_utc)
) STRICT, WITHOUT ROWID;

CREATE TABLE usage_daily (
    account_id TEXT NOT NULL REFERENCES accounts (account_id) ON DELETE CASCADE,
    fuel TEXT NOT NULL CHECK (fuel IN ('electricity', 'gas')),
    day TEXT NOT NULL CHECK (day IS date(day)),
    kwh REAL NOT NULL CHECK (kwh >= 0),
    cost_milli_pence INTEGER,
    has_half_hourly INTEGER NOT NULL CHECK (has_half_hourly IN (0, 1)),
    fetched_at_utc TEXT NOT NULL CHECK (fetched_at_utc IS strftime('%Y-%m-%dT%H:%M:%SZ', fetched_at_utc)),
    PRIMARY KEY (account_id, fuel, day)
) STRICT, WITHOUT ROWID;

CREATE TABLE usage_monthly (
    account_id TEXT NOT NULL REFERENCES accounts (account_id) ON DELETE CASCADE,
    fuel TEXT NOT NULL CHECK (fuel IN ('electricity', 'gas')),
    month TEXT NOT NULL CHECK (month IS date(month) AND substr(month, 9, 2) = '01'),
    kwh REAL NOT NULL CHECK (kwh >= 0),
    cost_milli_pence INTEGER,
    mpxn TEXT CHECK (mpxn IS NULL OR (length(mpxn) BETWEEN 6 AND 13 AND mpxn NOT GLOB '*[^0-9]*')),
    fetched_at_utc TEXT NOT NULL CHECK (fetched_at_utc IS strftime('%Y-%m-%dT%H:%M:%SZ', fetched_at_utc)),
    PRIMARY KEY (account_id, fuel, month)
) STRICT, WITHOUT ROWID;

CREATE TABLE meters (
    meter_serial TEXT PRIMARY KEY NOT NULL CHECK (length(meter_serial) BETWEEN 1 AND 64),
    account_id TEXT NOT NULL REFERENCES accounts (account_id) ON DELETE CASCADE,
    fuel TEXT NOT NULL CHECK (fuel IN ('electricity', 'gas')),
    meter_type TEXT,
    status TEXT
) STRICT, WITHOUT ROWID;

-- Gas meters have a single register, stored with register_id = ''.
CREATE TABLE registers (
    meter_serial TEXT NOT NULL REFERENCES meters (meter_serial) ON DELETE CASCADE,
    register_id TEXT NOT NULL CHECK (length(register_id) <= 64),
    start_date TEXT NOT NULL CHECK (start_date IS date(start_date)),
    end_date TEXT CHECK (end_date IS NULL OR (end_date IS date(end_date) AND end_date >= start_date)),
    timing_category TEXT,
    unit TEXT,
    digits INTEGER CHECK (digits IS NULL OR digits BETWEEN 1 AND 20),
    PRIMARY KEY (meter_serial, register_id, start_date)
) STRICT, WITHOUT ROWID;

-- Keyed on meter + register + date: My-Ovo-Data keyed readings on (date, fuel) and silently
-- overwrote multi-register reads made on the same day.
CREATE TABLE readings (
    meter_serial TEXT NOT NULL REFERENCES meters (meter_serial) ON DELETE CASCADE,
    register_id TEXT NOT NULL CHECK (length(register_id) <= 64),
    read_date TEXT NOT NULL CHECK (read_date IS date(read_date)),
    -- Kept as exact decimal text; meter registers are counters, not measurements to round.
    value TEXT NOT NULL CHECK (
        value GLOB '[0-9]*' AND value NOT GLOB '*[^0-9.]*'
        AND length(value) - length(replace(value, '.', '')) <= 1
        AND value NOT GLOB '*.'
    ),
    source TEXT,
    lifecycle TEXT,
    read_type TEXT,
    PRIMARY KEY (meter_serial, register_id, read_date)
) STRICT, WITHOUT ROWID;

CREATE INDEX idx_meters_account ON meters (account_id);
