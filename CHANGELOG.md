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
- Czech README explains the negative-price modes for purchase and sale
  (`--buy-negative`, `--sell-negative`) with example tables.
