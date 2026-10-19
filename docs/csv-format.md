# CSV export format

The **Export** page offers one CSV file per dataset for the dates you choose, or all of them in
one `.zip`. This is a stable contract: a column's name, position, unit or meaning changes only
with an entry in the changelog at the end, and a test fails if the columns listed here and the
columns the app writes ever disagree.

## All files

- UTF-8 with a byte-order mark (so Excel on a Mac shows `£` and accents correctly). Python and
  pandas read it with `encoding="utf-8-sig"`.
- `\r\n` line endings, a header row, and standard quoting (fields with commas, quotes or line
  breaks are quoted).
- File names are `ovo-insights-<dataset>-YYYY-MM-DD_to_YYYY-MM-DD.csv`; the zip is
  `ovo-insights-export-YYYY-MM-DD_to_YYYY-MM-DD.zip`.
- Dates are UK calendar dates, `YYYY-MM-DD`.
- **Amounts** (`..._gbp`) are pounds to exactly 2 decimal places. **Rates and standing charges**
  (`..._pence`) are pence to exactly 3 decimal places, which is exact. **kWh** are 3 decimal
  places. Nothing is rounded through a floating-point number.
- An unknown value is an empty cell, never `0`.
- **Usage is household totals**: your accounts are added together, as on every page. Meters are
  "Electricity meter 1" and so on; serial numbers are never exported.
- **Spreadsheet formulas:** text that comes from OVO or from you (register, unit, meter status,
  source, lifecycle, type, tariff names) and begins with `=`, `+`, `-`, `@`, a tab or a carriage
  return has a `'` put in front of it, so a spreadsheet cannot run it as a formula. Numbers are
  never changed.
- A dataset with nothing in the range is still a file, with just the header.
- A downloaded file is yours. It is saved to your Downloads folder, outside this app's
  protection, and it shows how you use energy.

## `monthly`

OVO's monthly figures; months that overlap the range.

| Column | Meaning |
|---|---|
| `month` | First day of the month. |
| `fuel` | `electricity` or `gas`. |
| `kwh` | Energy used in the month. |
| `usage_cost_gbp` | OVO's usage cost for the month, £. It excludes the standing charge. |

## `daily`

| Column | Meaning |
|---|---|
| `date` | The UK day. |
| `fuel` | `electricity` or `gas`. |
| `kwh` | Energy used that day. |
| `usage_cost_gbp` | OVO's usage cost for the day (kWh × unit rate, to the penny), £. |
| `standing_charge_pence` | The day's standing charge, pence. |
| `unit_rate_pence` | The unit rate, pence per kWh. |
| `has_half_hourly` | `true` if OVO says it has half-hourly data for the day. |

## `half-hourly-electricity` and `half-hourly-gas`

One row per half-hour. Clock-change days have 46 or 50 rows, which is correct.

| Column | Meaning |
|---|---|
| `utc_time` | Start of the half-hour in UTC, `YYYY-MM-DDTHH:MM:SSZ`. |
| `local_time` | The same instant in UK time with its offset, such as `2026-03-29T02:00:00+01:00`. |
| `kwh` | Energy used in the half-hour. |

## `readings`

Every stored meter reading in the range, including today's when the range ends yesterday.

| Column | Meaning |
|---|---|
| `fuel` | `electricity` or `gas`. |
| `meter` | `Electricity meter 1` and so on (numbered, active meters first). |
| `register` | The register id as OVO gives it (`1`, `01`; empty for gas). |
| `unit` | The register's unit as OVO gives it (`KWH`, `SCMH`, `SCFH`). |
| `meter_status` | `Active`, `Removed` and so on, as OVO gives it. |
| `date` | The date of the reading. |
| `value` | The register value, exactly as stored (up to 10 decimal places). |
| `source` | Where the reading came from, as OVO gives it. |
| `lifecycle` | OVO's lifecycle label. |
| `type` | `ACTUAL`, `ESTIMATE` and so on. |

## `tariff-comparison`

Your tariffs against what OVO charged, over the days **every** tariff and OVO's own figures can
be costed (the Tariffs page shows the days left out). One row per fuel, tariff and month; the
`What OVO charged` rows are the baseline. Only fuels that have a tariff appear, and nothing is
written if no day could be costed.

| Column | Meaning |
|---|---|
| `fuel` | `electricity` or `gas`. |
| `tariff` | The tariff's name, or `What OVO charged`. |
| `month` | First day of the month. |
| `days_compared` | Days of that month included. |
| `usage_gbp` | Usage cost, £. |
| `standing_gbp` | Standing charges, £. |
| `total_gbp` | Usage plus standing, rounded once from the exact figure, so it can differ by a penny from adding the two columns above. |

## Changelog

- First release: the six datasets above.
