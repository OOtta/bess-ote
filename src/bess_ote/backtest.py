"""Backtest: rolling decisions with only the prices published at each moment.

At every quarter-hour boundary ``t`` of the period the information set is the set of delivery
days published by ``t`` (see :mod:`bess_ote.market`). The same optimiser as ``plan`` computes
the optimal schedule over that horizon, only the first interval is executed and the state of
charge is carried on. Because the DP decision at ``t`` depends only on the intervals
``t … horizon end``, the solution is recomputed only when the horizon end changes (a new day
was published) and otherwise reused – the decisions are bit-for-bit those of ``plan --at t``.

The perfect-foresight benchmark solves the whole period at once with all prices known.
"""

from __future__ import annotations

import itertools
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from bess_ote.battery import Battery
from bess_ote.market import PriceBook
from bess_ote.optimizer import Solution, Step, Terminal, rollout, solve, step_at
from bess_ote.planner import Problem, TerminalConfig, build_problem
from bess_ote.tariff import Tariffs
from bess_ote.timeutil import QUARTER, local_midnight_utc, to_local


@dataclass
class Interval:
    start: datetime
    spot_czk_mwh: float
    buy_czk_kwh: float
    sell_czk_kwh: float
    step: Step
    horizon_end: datetime


@dataclass
class Totals:
    bought_kwh: float = 0.0
    cost_czk: float = 0.0
    sold_kwh: float = 0.0
    revenue_czk: float = 0.0
    wear_czk: float = 0.0
    discharged_kwh: float = 0.0  # from storage (before discharge losses)
    intervals: int = 0

    def add(self, iv: Interval) -> None:
        st = iv.step
        self.intervals += 1
        if st.grid_kwh > 0:
            self.bought_kwh += st.grid_kwh
            self.cost_czk += -st.cash_czk
        elif st.grid_kwh < 0:
            self.sold_kwh += -st.grid_kwh
            self.revenue_czk += st.cash_czk
            self.discharged_kwh += -st.delta_kwh
        self.wear_czk += st.wear_czk

    @property
    def profit_czk(self) -> float:
        return self.revenue_czk - self.cost_czk - self.wear_czk

    def as_dict(self, usable_kwh: float) -> dict[str, float]:
        return {
            "bought_kwh": round(self.bought_kwh, 4),
            "cost_czk": round(self.cost_czk, 2),
            "sold_kwh": round(self.sold_kwh, 4),
            "revenue_czk": round(self.revenue_czk, 2),
            "wear_czk": round(self.wear_czk, 2),
            "profit_czk": round(self.profit_czk, 2),
            "cycles": round(self.discharged_kwh / usable_kwh, 3) if usable_kwh else 0.0,
        }


@dataclass
class BacktestResult:
    start: date
    end: date
    battery: Battery
    initial_soc_kwh: float
    intervals: list[Interval]
    totals: Totals
    end_soc_kwh: float
    solves: int
    foresight: Totals | None = None
    foresight_end_soc_kwh: float | None = None
    terminal_value_czk_kwh: float = 0.0
    skipped_intervals: int = 0
    warnings: list[str] = field(default_factory=list)

    def breakdown(self, by: str) -> list[dict]:
        groups: dict[str, Totals] = defaultdict(Totals)
        for iv in self.intervals:
            d = to_local(iv.start).date()
            key = d.isoformat() if by == "day" else f"{d.year:04d}-{d.month:02d}"
            groups[key].add(iv)
        return [
            {"period": k} | t.as_dict(self.battery.usable_kwh) for k, t in sorted(groups.items())
        ]


def run_backtest(
    book: PriceBook,
    start: date,
    end: date,
    tariffs: Tariffs,
    battery: Battery,
    tcfg: TerminalConfig,
    initial_soc_kwh: float,
    *,
    publish: time | None = None,
    foresight: bool = True,
) -> BacktestResult:
    if tcfg.mode == "min-soc" and tcfg.min_soc_pct is None:
        tcfg = TerminalConfig("min-soc", None, battery.pct(initial_soc_kwh))
    t = local_midnight_utc(start)
    t_end = local_midnight_utc(end + timedelta(days=1))
    sol: Solution | None = None
    prob: Problem | None = None
    prob_idx: dict[datetime, int] = {}
    state: int | None = None
    intervals: list[Interval] = []
    totals = Totals()
    solves = 0
    skipped = 0
    warnings: list[str] = []
    snapped_initial = None
    while t < t_end:
        horizon = book.horizon(t, t, publish)
        if not horizon:
            skipped += 1
            t += QUARTER
            continue
        if prob is None or sol is None or prob.starts[-1] != horizon[-1] or t not in prob_idx:
            prob = build_problem(
                book, t, t, tariffs, battery, tcfg, publish=publish, horizon=horizon,
                initial_soc_kwh=initial_soc_kwh,
            )  # fmt: skip
            sol = solve(battery, prob.buy, prob.sell, prob.terminal)
            solves += 1
            prob_idx = {s: i for i, s in enumerate(prob.starts)}
            if state is None:
                state = sol.state_index(initial_soc_kwh)
                snapped_initial = float(sol.levels[state])
        offset = prob_idx[t]
        assert state is not None
        st, state = step_at(sol, offset, state)
        iv = Interval(t, float(prob.spot[offset]), float(prob.buy[offset]),
                      float(prob.sell[offset]), st, prob.starts[-1] + QUARTER)  # fmt: skip
        intervals.append(iv)
        totals.add(iv)
        t += QUARTER
    if skipped:
        warnings.append(
            f"{skipped} quarter-hour(s) without known OTE prices were skipped (battery idle)."
        )
    if snapped_initial is None:
        snapped_initial = initial_soc_kwh
    end_soc = intervals[-1].step.soc_kwh if intervals else snapped_initial
    res = BacktestResult(start, end, battery, snapped_initial, intervals, totals, end_soc,
                         solves, skipped_intervals=skipped, warnings=warnings)  # fmt: skip
    if prob is not None:
        res.terminal_value_czk_kwh = prob.terminal.czk_per_kwh_stored
    if foresight and intervals:
        all_starts = [iv.start for iv in intervals]
        if all(b - a == QUARTER for a, b in itertools.pairwise(all_starts)):
            fprob = build_problem(
                book, all_starts[0], None, tariffs, battery, tcfg, horizon=all_starts,
                initial_soc_kwh=initial_soc_kwh,
            )  # fmt: skip
            terminal = fprob.terminal
            if terminal.mode == "value":  # same valuation as the report → a true upper bound
                terminal = Terminal("value", czk_per_kwh_stored=res.terminal_value_czk_kwh)
            fsol = solve(battery, fprob.buy, fprob.sell, terminal)
            ft = Totals()
            steps = rollout(fsol, snapped_initial)
            for i, s in enumerate(steps):
                ft.add(Interval(all_starts[i], math.nan, 0, 0, s, all_starts[-1]))
            res.foresight = ft
            res.foresight_end_soc_kwh = steps[-1].soc_kwh if steps else snapped_initial
        else:
            warnings.append("Perfect-foresight benchmark skipped: the period has gaps.")
    return res
