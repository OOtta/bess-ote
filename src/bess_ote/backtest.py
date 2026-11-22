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
import threading
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, BrokenExecutor, wait
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

import numpy as np

from bess_ote.battery import Battery
from bess_ote.market import PriceBook
from bess_ote.optimizer import (
    Solution,
    Step,
    Terminal,
    _grid,
    rollout,
    solve,
    solve_choice,
    step_at,
)
from bess_ote.parallel import MIN_UNITS_PER_WORKER, make_process_pool, resolve_jobs
from bess_ote.planner import Problem, TerminalConfig, terminal_for
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
    fees_czk: float = 0.0  # prorated monthly fees (incl. VAT)

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
        """Trading profit (before monthly fees)."""
        return self.revenue_czk - self.cost_czk - self.wear_czk

    @property
    def net_profit_czk(self) -> float:
        return self.profit_czk - self.fees_czk

    def as_dict(self, usable_kwh: float) -> dict[str, float]:
        return {
            "bought_kwh": round(self.bought_kwh, 4),
            "cost_czk": round(self.cost_czk, 2),
            "sold_kwh": round(self.sold_kwh, 4),
            "revenue_czk": round(self.revenue_czk, 2),
            "wear_czk": round(self.wear_czk, 2),
            "profit_czk": round(self.profit_czk, 2),
            "fees_czk": round(self.fees_czk, 2),
            "net_profit_czk": round(self.net_profit_czk, 2),
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
    fees_by_day: dict[date, float] = field(default_factory=dict)
    workers: int = 1  # worker processes used for the DP solves (1 = serial)

    def breakdown(self, by: str) -> list[dict]:
        groups: dict[str, Totals] = defaultdict(Totals)

        def key(d: date) -> str:
            return d.isoformat() if by == "day" else f"{d.year:04d}-{d.month:02d}"

        for iv in self.intervals:
            groups[key(to_local(iv.start).date())].add(iv)
        for d, fee in self.fees_by_day.items():
            groups[key(d)].fees_czk += fee
        return [
            {"period": k} | t.as_dict(self.battery.usable_kwh) for k, t in sorted(groups.items())
        ]


@dataclass
class _Event:
    """One re-optimisation: problem from ``t`` to the end of the prices known at ``t``."""

    prob: Problem
    keep: int = 0  # decisions actually executed (intervals until the next event)
    choice: np.ndarray | None = None


class _PeriodPrices:
    """Spot / buy / sell for every quarter of the book, computed once (same formulas as
    :func:`build_problem`), so each re-optimisation is just a slice."""

    def __init__(self, book: PriceBook, tariffs: Tariffs) -> None:
        self.starts = sorted(book.spot)
        self.index = {t: i for i, t in enumerate(self.starts)}
        self.spot = np.array([book.spot[t] for t in self.starts], dtype=float)
        self.buy = np.array(
            [tariffs.buy_czk_kwh(s, t, QUARTER) if not math.isnan(s) else math.nan
             for s, t in zip(self.spot, self.starts, strict=True)],
            dtype=float,
        )  # fmt: skip
        self.sell = np.array(
            [tariffs.sell_czk_kwh(s) if not math.isnan(s) else math.nan for s in self.spot],
            dtype=float,
        )

    def problem(self, starts: list[datetime], terminal: Terminal) -> Problem:
        i = self.index[starts[0]]
        j = i + len(starts)
        assert self.starts[i:j] == starts, "horizon is not contiguous in the price book"
        return Problem(starts, self.spot[i:j], self.buy[i:j], self.sell[i:j], terminal)


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
    progress: Callable[[str, float, str], None] | None = None,
    jobs: int | None = 1,
) -> BacktestResult:
    """Rolling backtest.

    The DP backward pass of each re-optimisation depends only on the prices known at that
    publication, never on the state of charge, so all of them (and the perfect-foresight
    benchmark) are independent and may run in parallel (``jobs`` > 1 or None = auto). The
    state of charge is then carried through the stored decision tables sequentially. Every
    solve is deterministic, so the result is bit-for-bit the same for any ``jobs``.

    ``progress(phase, fraction 0..1, detail)``: phase ``simulate`` with the overall fraction
    of DP work done (rolling solves and the benchmark weighted by their number of
    quarter-hours), then a final ``done`` with 1.0, always emitted on success.
    """
    if tcfg.mode == "min-soc" and tcfg.min_soc_pct is None:
        tcfg = TerminalConfig("min-soc", None, battery.pct(initial_soc_kwh))
    t0 = local_midnight_utc(start)
    t_end = local_midnight_utc(end + timedelta(days=1))
    n_days = (end - start).days + 1
    report = progress or (lambda *_: None)
    report("simulate", 0.0, f"0/{n_days} days")

    # 1) schedule – which quarter is decided by which re-optimisation (independent of SoC)
    plan_q: list[tuple[datetime, int, int]] = []  # (t, event index or -1 = skipped, offset)
    ev_spans: list[tuple[datetime, datetime]] = []  # (first start, last start)
    t = t0
    while t < t_end:
        last = book.horizon_end(t, t, publish)
        if last is None:
            plan_q.append((t, -1, 0))
        else:
            if not ev_spans or ev_spans[-1][1] != last or t > last:
                ev_spans.append((t, last))
            e_t = ev_spans[-1][0]
            plan_q.append((t, len(ev_spans) - 1, (t - e_t) // QUARTER))
        t += QUARTER

    # 2) problems (prices sliced from one precomputed table, terminal value per last day)
    prices = _PeriodPrices(book, tariffs)
    by_day = book.values_by_day()
    events: list[_Event] = []
    for first, last in ev_spans:
        n = (last - first) // QUARTER + 1
        starts = [first + i * QUARTER for i in range(n)]
        term = terminal_for(book, starts, tariffs, battery, tcfg, initial_soc_kwh, by_day)
        events.append(_Event(prices.problem(starts, term)))
    for _, e, off in plan_q:
        if e >= 0:
            events[e].keep = max(events[e].keep, off + 1)
    intervals_starts = [t for t, e, _ in plan_q if e >= 0]
    terminal_value = events[-1].prob.terminal.czk_per_kwh_stored if events else 0.0

    fprob: Problem | None = None
    fterminal: Terminal | None = None
    warnings: list[str] = []
    if foresight and intervals_starts:
        if all(b - a == QUARTER for a, b in itertools.pairwise(intervals_starts)):
            fterminal = terminal_for(book, intervals_starts, tariffs, battery, tcfg,
                                     initial_soc_kwh, by_day)  # fmt: skip
            if fterminal.mode == "value":  # same valuation as the report → a true upper bound
                fterminal = Terminal("value", czk_per_kwh_stored=terminal_value)
            fprob = prices.problem(intervals_starts, fterminal)
        else:
            warnings.append("Perfect-foresight benchmark skipped: the period has gaps.")

    # 3) DP backward passes – serial or in parallel
    fsol, workers = _solve_all(battery, events, fprob, resolve_jobs(jobs), report, n_days)

    # 4) forward pass: carry the state of charge through the decision tables (sequential)
    levels, k0, deltas = _grid(battery)
    sols = [
        Solution(battery, levels, deltas, ev.choice, np.empty(0), ev.prob.buy, ev.prob.sell, k0)
        for ev in events
    ]  # fmt: skip
    state = sols[0].state_index(initial_soc_kwh) if sols else None
    snapped_initial = float(levels[state]) if state is not None else initial_soc_kwh
    intervals: list[Interval] = []
    totals = Totals()
    skipped = 0
    for t, e, off in plan_q:
        if e < 0:
            skipped += 1
            continue
        prob = events[e].prob
        st, state = step_at(sols[e], off, state)
        iv = Interval(t, float(prob.spot[off]), float(prob.buy[off]), float(prob.sell[off]),
                      st, prob.starts[-1] + QUARTER)  # fmt: skip
        intervals.append(iv)
        totals.add(iv)
    if skipped:
        warnings.insert(
            0, f"{skipped} quarter-hour(s) without known OTE prices were skipped (battery idle)."
        )
    end_soc = intervals[-1].step.soc_kwh if intervals else snapped_initial
    res = BacktestResult(start, end, battery, snapped_initial, intervals, totals, end_soc,
                         len(events), skipped_intervals=skipped, warnings=warnings)  # fmt: skip
    res.terminal_value_czk_kwh = terminal_value
    res.workers = workers
    d = start
    while d <= end:
        res.fees_by_day[d] = tariffs.fees_for_day(d)
        d += timedelta(days=1)
    res.totals.fees_czk = sum(res.fees_by_day.values())
    if fsol is not None:
        ft = Totals()
        steps = rollout(fsol, snapped_initial)
        for i, s in enumerate(steps):
            ft.add(Interval(intervals_starts[i], math.nan, 0, 0, s, intervals_starts[-1]))
        ft.fees_czk = res.totals.fees_czk
        res.foresight = ft
        res.foresight_end_soc_kwh = steps[-1].soc_kwh if steps else snapped_initial
    report("done", 1.0, f"{n_days}/{n_days} days")
    return res


def _solve_all(
    battery: Battery,
    events: list[_Event],
    fprob: Problem | None,
    jobs: int,
    report: Callable[[str, float, str], None],
    n_days: int,
) -> tuple[Solution | None, int]:
    """Fill ``ev.choice`` for every event; returns (perfect-foresight solution, workers)."""
    rolling_units = sum(len(ev.prob.starts) for ev in events)
    f_units = len(fprob.starts) if fprob is not None else 0
    total = max(1, rolling_units + f_units)
    keep_total = max(1, sum(ev.keep for ev in events))
    state = {"rolling": 0, "keep": 0, "f": 0.0}
    lock = threading.Lock()

    def emit() -> None:
        with lock:
            done = state["rolling"] + state["f"] * f_units
            all_rolling = state["rolling"] >= rolling_units
            days = min(n_days, int(n_days * state["keep"] / keep_total))
        detail = "perfect foresight" if all_rolling and f_units else f"{days}/{n_days} days"
        report("simulate", min(done / total, 1.0), detail)

    def finished(ev: _Event) -> None:
        with lock:
            state["rolling"] += len(ev.prob.starts)
            state["keep"] += ev.keep

    def solve_foresight() -> Solution | None:
        if fprob is None:
            return None

        def cb(f: float) -> None:
            with lock:
                state["f"] = f

        sol = solve(battery, fprob.buy, fprob.sell, fprob.terminal, cb)
        with lock:
            state["f"] = 1.0
        return sol

    def serial(todo: list[_Event]) -> None:
        for ev in todo:
            p = ev.prob
            ev.choice = solve_choice(battery, p.buy, p.sell, p.terminal, ev.keep)
            finished(ev)
            emit()

    workers = min(jobs - (1 if fprob is not None else 0), len(events),
                  rolling_units // MIN_UNITS_PER_WORKER)  # fmt: skip
    pool = make_process_pool(workers) if jobs > 1 and workers >= 1 else None
    if pool is None:
        serial(events)
        return (_threaded(solve_foresight, emit) if fprob is not None else None), 1

    # parallel: rolling solves in worker processes (largest first), benchmark on a thread here
    fres: dict[str, Any] = {}
    fthread = threading.Thread(target=_capture, args=(solve_foresight, fres), daemon=True)
    try:
        with pool:
            order = sorted(events, key=lambda ev: -len(ev.prob.starts))
            futs = {
                pool.submit(solve_choice, battery, ev.prob.buy, ev.prob.sell, ev.prob.terminal,
                            ev.keep): ev
                for ev in order
            }  # fmt: skip
            if fprob is not None:
                fthread.start()  # after submit: fork-based pools have started all workers
            pending = set(futs)
            while pending:
                done, pending = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
                for fut in done:
                    ev = futs[fut]
                    ev.choice = fut.result()
                    finished(ev)
                emit()
    except (BrokenExecutor, OSError, PermissionError):
        serial([ev for ev in events if ev.choice is None])  # deterministic → same result
        workers = 1
    if fprob is None:
        return None, workers
    if fthread.ident is None:  # pool failed before the benchmark was started
        return _threaded(solve_foresight, emit), workers
    while fthread.is_alive():
        fthread.join(0.1)
        emit()
    emit()
    return _result(fres), workers


def _threaded(fn: Callable[[], Solution | None], emit: Callable[[], None]) -> Solution | None:
    """Run ``fn`` on a helper thread while this thread reports progress every 0.1 s."""
    out: dict[str, Any] = {}
    th = threading.Thread(target=_capture, args=(fn, out), daemon=True)
    th.start()
    while th.is_alive():
        th.join(0.1)
        emit()
    emit()
    return _result(out)


def _capture(fn: Callable[[], Solution | None], out: dict[str, Any]) -> None:
    try:
        out["sol"] = fn()
    except BaseException as exc:  # re-raised in the calling thread by _result
        out["error"] = exc


def _result(out: dict[str, Any]) -> Solution | None:
    if "error" in out:
        raise out["error"]
    return out.get("sol")
