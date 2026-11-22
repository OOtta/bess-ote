"""Plan: optimal action for the next quarter-hour(s) from the prices known at a moment."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time

import numpy as np

from bess_ote.battery import Battery
from bess_ote.market import PriceBook
from bess_ote.optimizer import Solution, Step, Terminal, TerminalMode, rollout, solve
from bess_ote.tariff import Tariffs
from bess_ote.timeutil import QUARTER, local_date


@dataclass(frozen=True, slots=True)
class TerminalConfig:
    """User settings for the end-of-horizon value of stored energy.

    ``value``: ``price_czk_kwh`` (sale price for the leftover energy) or, when None, the
    average sale price of the **last day of the horizon**; a stored kWh is worth
    ``max(0, price) × η_discharge − wear × η_discharge``. The last day is the same for every
    moment between two price publications, so the value does not depend on the moment.
    """

    mode: TerminalMode = "value"
    price_czk_kwh: float | None = None
    min_soc_pct: float | None = None


@dataclass
class Problem:
    starts: list[datetime]
    spot: np.ndarray
    buy: np.ndarray
    sell: np.ndarray
    terminal: Terminal


def build_problem(
    book: PriceBook,
    start: datetime,
    at: datetime | None,
    tariffs: Tariffs,
    battery: Battery,
    tcfg: TerminalConfig,
    *,
    publish: time | None = None,
    initial_soc_kwh: float | None = None,
    horizon: list[datetime] | None = None,
) -> Problem:
    starts = horizon if horizon is not None else book.horizon(start, at, publish)
    spot = np.array([book.spot[t] for t in starts], dtype=float)
    buy = np.array(
        [tariffs.buy_czk_kwh(s, t, QUARTER) if not math.isnan(s) else math.nan
         for s, t in zip(spot, starts, strict=True)],
        dtype=float,
    )  # fmt: skip
    sell = np.array(
        [tariffs.sell_czk_kwh(s) if not math.isnan(s) else math.nan for s in spot], dtype=float
    )
    return Problem(starts, spot, buy, sell, terminal_for(book, starts, tariffs, battery, tcfg,
                                                         initial_soc_kwh))  # fmt: skip


def terminal_for(
    book: PriceBook,
    starts: list[datetime],
    tariffs: Tariffs,
    battery: Battery,
    tcfg: TerminalConfig,
    initial_soc_kwh: float | None,
    by_day: dict[date, list[float]] | None = None,
) -> Terminal:
    """``by_day`` = precomputed :meth:`PriceBook.values_by_day` (same values, same order)."""
    if tcfg.mode == "zero" or not starts:
        return Terminal("zero")
    if tcfg.mode == "min-soc":
        pct = tcfg.min_soc_pct
        kwh = battery.capacity_kwh * pct / 100 if pct is not None else (initial_soc_kwh or 0.0)
        return Terminal("min-soc", min_kwh=kwh)
    price = tcfg.price_czk_kwh
    if price is None:
        last = local_date(starts[-1])
        if by_day is not None:
            vals = [tariffs.sell_czk_kwh(v) for v in by_day.get(last, [])]
        else:
            vals = [
                tariffs.sell_czk_kwh(v)
                for t, v in book.spot.items()
                if local_date(t) == last and not math.isnan(v)
            ]
        price = sum(vals) / len(vals) if vals else 0.0
    per_stored = (max(0.0, price) - battery.cycle_cost_czk_kwh) * battery.discharge_eff
    return Terminal("value", czk_per_kwh_stored=max(0.0, per_stored))


@dataclass
class PlanResult:
    at: datetime
    start: datetime
    problem: Problem
    solution: Solution
    steps: list[Step]
    soc_kwh: float  # snapped initial stored energy

    @property
    def next_step(self) -> Step | None:
        return self.steps[0] if self.steps else None


def plan(
    book: PriceBook,
    at: datetime,
    soc_kwh: float,
    tariffs: Tariffs,
    battery: Battery,
    tcfg: TerminalConfig,
    *,
    publish: time | None = None,
    use_publication_rule: bool = True,
) -> PlanResult:
    """Optimal schedule from the first quarter-hour boundary at/after ``at`` until the end of
    the prices known at ``at`` (publication rule) – or all prices in ``book`` when
    ``use_publication_rule`` is False (live data: the book holds exactly what OTE published).
    """
    from bess_ote.market import ceil_quarter

    start = ceil_quarter(at)
    prob = build_problem(
        book, start, at if use_publication_rule else None, tariffs, battery, tcfg,
        publish=publish, initial_soc_kwh=soc_kwh,
    )  # fmt: skip
    sol = solve(battery, prob.buy, prob.sell, prob.terminal)
    state = sol.state_index(soc_kwh)
    snapped = float(sol.levels[state])
    return PlanResult(at, start, prob, sol, rollout(sol, snapped), snapped)
