# Changelog

## 0.1.0 – unreleased

First version of **bess-ote** (BESS OTE kalkulačka).

- **Solar PV (FVE)** on the same hybrid inverter, in `plan` and `backtest`:
  - `--pv-array AZ,TILT,KWP` (repeatable; azimuth 0 = south, −90 = east; write
    `--pv-array=-90,20,2` for a negative azimuth), `--lat/--lon` (CLI or local config only),
    `--pv-losses` (default 14 %), `--inverter-kw` (shared AC limit for grid import and export
    of PV + battery; default with PV = max of charge/discharge power). Config section `[pv]`.
  - PV energy is free: the DP chooses per quarter-hour between charging the battery (DC, not
    counted against the inverter limit), selling at the sell formula and curtailing (e.g.
    negative sell price); grid charging and discharging stay available. Zero PV / no PV is
    bit-identical to before (tested), DP vs a continuous LP bound tested.
  - PV model: NOAA sun position, isotropic-sky transposition with albedo 0.2, Erbs diffuse
    split for measured GHI, NOCT cell temperature (−0.4 %/K), system losses.
  - Forecasts (keyless, cached): Open-Meteo 15-minute forecast for live `plan`; Open-Meteo
    Single Runs as issued for `plan --at` and backtests – ICON-D2 (every 3 h, available 2 h
    after the run) from 2026-04-02, ECMWF IFS (every 6 h, +7 h) before; gaps filled from older
    runs. Throttling (HTTP 429) and 5xx are retried with back-off, at most 3 download threads.
  - Actuals: ČHMÚ open data, 10-minute global radiation and temperature of the nearest
    station, averaged to quarter-hours and transposed to each array; the backtest always
    settles on actual production (shortfall reduces charging, surplus is sold, stored or
    curtailed), no bias correction.
  - `backtest --replan publication|forecast|quarter`: re-plan only at OTE publications, also at
    each new forecast run (default), or every quarter-hour with a nowcast from actual
    production; only information available at the decision time is used and the decision at
    `t` equals `plan --at t` (tested); serial and parallel results identical.
  - Outputs: PV actual vs forecast (bias, quarter-hour and daily MAE), PV → battery / grid /
    curtailed, PV export revenue; breakdown, CSV and JSON columns `pv_forecast_kwh`,
    `pv_kwh`, `pv_battery_kwh`, `pv_grid_kwh`, `pv_curtailed_kwh`, JSON object `pv`; perfect
    foresight knows prices and actual PV.
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
- Monthly fees: `--buy-monthly-fee` and `--distribution-monthly-fee` (config `[buy]` /
  `[distribution]` `monthly_fee`), CZK/month excl. VAT, subject to `--buy-vat`. Fixed costs –
  decisions are unchanged; prorated per day (fee / days in the month, partial months by days)
  and spread over the day's quarter-hours. Backtest prints trading profit, monthly fees and net
  profit, with `fees` / `net` columns in the breakdown and `fees_czk` / `net_profit_czk` in
  CSV/JSON; `plan` shows the fees for the horizon and the net result.
- Backtest progress bar on stderr (percent, days processed, ETA; plain `\r`, ASCII, no
  dependency) – only when stderr is a terminal and without `--json`; `--no-progress` turns it
  off. With `--no-foresight` the rolling simulation fills the whole bar (60 / 40 split only
  when the benchmark runs); the bar always ends at 100 % (also when the benchmark is skipped
  because of gaps) and the ETA appears only once it is meaningful.
- Faster backtest, bit-for-bit identical results (tests compare every interval with the
  previous serial loop; live June–October 2026 CSV/JSON byte-identical):
  - `-j/--jobs N` (default auto = `os.cpu_count()`, 1 = serial): the DP backward passes of
    all re-optimisations depend only on the prices known at each publication, not on the
    state of charge, so they run in a process pool; the perfect-foresight DP runs on a
    thread in the main process; the state of charge is then carried through the stored
    decision tables sequentially. Processes rather than threads (threads scaled ~2× because
    of the GIL); falls back to serial where a process pool is unavailable (Termux) or for
    short periods. `python -m bess_ote` is guarded for spawn-based platforms (Windows).
  - Serial speedups: publication times memoised and only the horizon end computed per
    quarter (was ~40 % of the runtime), terminal day averages from a per-day index, buy/sell
    prices computed once per period, DP loop with preallocated float64 buffers.
  - Live example (153 days, warm cache, 8 CPUs): 5.18 s → 2.45 s serial, 1.25 s auto;
    `--no-foresight` 4.13 s → 0.54 s.
  - Progress bar fraction = DP work done (rolling solves and the benchmark weighted by
    quarter-hours), correct for serial and parallel runs.
- Fix: profit could fall with growing battery capacity. The SoC grid step was capacity / 400
  regardless of power, so full power was rounded down to whole steps – at 175 kWh / 5 kW the
  step was 0.4375 kWh and charging ran at 3.7 kW (100 kWh: 4.2 kW, 125 kWh: 3.95 kW).
  New default step: a fraction of one full-power quarter-hour, or capacity / 400, whichever is
  lower (`charge_q / (K × 2^j)`, K ≥ 9 from power and efficiencies so full-power charging –
  and with the default 90 % round trip also discharging – is exact; nested grids so a bigger
  battery can do everything a smaller one can); grid anchored at min SoC. Perfect-foresight
  profit is now monotone in capacity (tests over a dense capacity sweep, and within 0.5 % of
  the continuous LP optimum – scipy as a test-only dependency). Live June–October 2026,
  10 kWh / 5 kW: trading profit 3,319.54 → 3,326.27 CZK (exact 5 kW instead of 4.95/4.93 kW);
  175 kWh: perfect foresight 5,713.86 → 6,732.01 CZK. Slower for small batteries (finer
  grid; 10 kWh `--jobs auto` 1.25 s → 2.36 s), `--soc-step` overrides. Long perfect-foresight
  runs on big batteries keep value-function checkpoints instead of the full decision table
  (bit-identical, bounded memory); decision tables are int8 when possible.
- Breakdown rows show the distribution part of the purchase price: `dist_czk_kwh` (VT/NT
  distribution + other per-kWh items incl. purchase VAT; text column `dist`) and `tariff`
  (`NT` / `VT` / `VT/NT`), so the step of the buy price at the NT window edges
  ((VT − NT) × (1 + VAT)) is visible. Regression test recomputes every quarter's buy price by
  hand (ČNB rate, negative-price surcharge, VT/NT by local time incl. a holiday, VAT). Prices
  and decisions are unchanged.
- Hour and quarter-hour breakdowns: `backtest -b hour|quarter-hour` (aliases `h`, `60min`,
  `qh`, `15min`, `quarter`; also `d` for day) and `plan -b hour|quarter-hour`; rows with local
  time, actions (`pattern` per quarter: C/D/-/.), average prices, energy, SoC, cost, revenue,
  profit, fees and net (sums – totals equal the summary; prices = simple averages, i.e. the
  hourly price until 2025-09-30); DST days give 23 / 25 hourly rows (92 / 100 quarters);
  quarter-hours without prices kept as `no price` rows. `--breakdown-csv FILE` writes the
  breakdown rows as CSV (backtest and plan). JSON gets a `breakdown` object.
- **Changed default:** `backtest -b` now defaults to `none` (summary only); the monthly table
  that used to be printed by default needs `-b month`. `plan` prints a breakdown only with
  `-b` as well.
- Czech README explains the negative-price modes for purchase and sale
  (`--buy-negative`, `--sell-negative`) with example tables.
