-- 0004: user-defined tariffs for re-costing usage (M9; deferred from M2).
--
-- A tariff is a name, a fuel, a daily standing charge and bands of unit rates. Money is
-- INTEGER milli-pence (rates per kWh, standing charge per day), always inc. VAT as entered.
-- A band covers [start_slot, end_slot) half-hour slots of the UK wall-clock day (slot 0 is
-- 00:00, slot 47 is 23:30, end 48 means midnight) on the weekdays in `weekdays` (bit 0 =
-- Monday ... bit 6 = Sunday). A band never wraps past midnight: the app splits a band such
-- as 23:30-05:30 into two before storing it. That every weekday's 48 slots are covered
-- exactly once is checked in code (storage/tariffs.py), not here.
-- Tariffs with the same name and fuel are versions of one tariff and must not overlap in
-- time (their effective dates); that is checked in code too, inside one transaction.

CREATE TABLE tariffs (
    tariff_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 64),
    fuel TEXT NOT NULL CHECK (fuel IN ('electricity', 'gas')),
    standing_charge_milli_pence INTEGER NOT NULL
        CHECK (standing_charge_milli_pence BETWEEN 0 AND 1000000),
    effective_from TEXT CHECK (effective_from IS NULL OR effective_from IS date(effective_from)),
    effective_to TEXT CHECK (
        effective_to IS NULL
        OR (effective_to IS date(effective_to)
            AND (effective_from IS NULL OR effective_to >= effective_from))
    ),
    created_at_utc TEXT NOT NULL CHECK (created_at_utc IS strftime('%Y-%m-%dT%H:%M:%SZ', created_at_utc)),
    updated_at_utc TEXT NOT NULL CHECK (updated_at_utc IS strftime('%Y-%m-%dT%H:%M:%SZ', updated_at_utc))
) STRICT;

CREATE TABLE tariff_bands (
    tariff_id INTEGER NOT NULL REFERENCES tariffs (tariff_id) ON DELETE CASCADE,
    band_no INTEGER NOT NULL CHECK (band_no >= 0),
    start_slot INTEGER NOT NULL CHECK (start_slot BETWEEN 0 AND 47),
    end_slot INTEGER NOT NULL CHECK (end_slot BETWEEN 1 AND 48),
    unit_rate_milli_pence INTEGER NOT NULL CHECK (unit_rate_milli_pence BETWEEN 0 AND 500000),
    weekdays INTEGER NOT NULL CHECK (weekdays BETWEEN 1 AND 127),
    PRIMARY KEY (tariff_id, band_no),
    CHECK (end_slot > start_slot)
) STRICT, WITHOUT ROWID;

CREATE INDEX idx_tariffs_fuel ON tariffs (fuel, name);
