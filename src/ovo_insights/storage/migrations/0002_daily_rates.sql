-- 0002: per-day unit rate and standing charge (deferred from M2 until real data confirmed
-- their meaning). OVO's daily `cost` is kWh x unit rate only; the standing charge is extra.
-- Both are INTEGER milli-pence: per kWh and per day respectively.

ALTER TABLE usage_daily ADD COLUMN unit_rate_milli_pence INTEGER
    CHECK (unit_rate_milli_pence IS NULL OR unit_rate_milli_pence >= 0);
ALTER TABLE usage_daily ADD COLUMN standing_charge_milli_pence INTEGER
    CHECK (standing_charge_milli_pence IS NULL OR standing_charge_milli_pence >= 0);
