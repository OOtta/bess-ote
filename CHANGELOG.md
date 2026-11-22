# Changelog

## 0.1.0 – unreleased

First version of **bess-ote** (BESS OTE kalkulačka).

- `bess-ote plan`: optimal charge / discharge / wait schedule of a battery from the next
  quarter-hour over all OTE day-ahead prices published at that moment (live, or `--at` a past
  moment with the publication rule); `--next` prints only the next action; text, `--json`,
  `-o` CSV/JSON.
- `bess-ote backtest`: rolling simulation on historical prices – at every quarter-hour only
  prices published by then are known, the same optimiser as `plan` decides and the first
  action is executed; totals, cycles, day/month breakdown, perfect-foresight benchmark,
  CSV/JSON export. Re-optimises only when new prices are published (decisions identical to
  `plan --at t`, asserted by tests).
- Exact dynamic programming over a fine SoC grid (numpy): AC power limits, separate or
  round-trip efficiency, min/max SoC, wear cost per kWh discharged, terminal handling
  (`value` / `zero` / `min-soc`); no simultaneous charging and discharging.
- Tariffs: purchase and sale as spot × multiplier + surcharge with separate coefficients for
  negative prices (`as-is` / `clamp-spot` / `clamp-total`), distribution VT/NT and other per-kWh
  fees on purchased energy only, NT windows for weekdays/weekends/holidays, separate VAT for
  purchase and sale. All defaults neutral.
- OTE publication times: 12:57 (delivery until 2025-09-30), ~13:05 (from 2025-10-01, 15-minute
  market); `--publish-time` override.
- OTE prices (hourly CZK until 2025-09-30, 15-minute EUR from 2025-10-01 converted with the
  ČNB rate of the delivery day) and ČNB rates with a persistent per-day cache (from
  egd-api-client); TOML config; Windows/Termux-safe output; Python 3.11–3.14.
