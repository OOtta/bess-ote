"""Backtest = rolling plan: identical decisions to `plan --at t`, no look-ahead."""

import random
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from itertools import pairwise

import pytest

from bess_ote.backtest import run_backtest
from bess_ote.battery import Battery
from bess_ote.lowtariff import LowTariffSchedule
from bess_ote.optimizer import solve, step_at
from bess_ote.planner import TerminalConfig, build_problem, plan
from bess_ote.tariff import Distribution, SpotFormula, Tariffs
from bess_ote.timeutil import PRAGUE, QUARTER, local_midnight_utc
from conftest import synthetic_book

B = Battery(10, 5, soc_min_pct=10, soc_max_pct=95)
T = Tariffs(
    buy=SpotFormula(1.0, 400),
    sell=SpotFormula(1.0, -300),
    distribution=Distribution(
        2.435, 0.416, LowTariffSchedule.parse("1:15-5:15,12:30-16:30", "2:45-6:45,13:00-17:00")
    ),
)


@pytest.mark.parametrize("tcfg", [TerminalConfig(), TerminalConfig("min-soc", None, 30)])
@pytest.mark.parametrize("pub", [None, time(14, 20)])
def test_backtest_decision_equals_plan_at_t(tcfg, pub):
    book = synthetic_book(date(2026, 5, 31), 9)
    res = run_backtest(book, date(2026, 6, 1), date(2026, 6, 7), T, B, tcfg, 4.0, publish=pub)
    assert len(res.intervals) == 7 * 96
    assert res.solves == 8  # one per published day (+ the start), not one per quarter-hour
    rnd = random.Random(5)
    picks = rnd.sample(range(len(res.intervals)), 40)
    picks += [i for i, iv in enumerate(res.intervals) if iv.start.astimezone(PRAGUE).hour == 13]
    for i in picks:
        iv = res.intervals[i]
        soc_before = res.initial_soc_kwh if i == 0 else res.intervals[i - 1].step.soc_kwh
        at = iv.start - timedelta(minutes=rnd.choice([0, 1, 7, 14]))  # any moment before t
        p = plan(book, at, soc_before, T, B, tcfg, publish=pub)
        assert p.start == iv.start
        # same action, energy, cash and SoC (index is relative to each horizon)
        assert replace(p.next_step, index=0) == replace(iv.step, index=0), i
        assert p.problem.starts[-1] + QUARTER == iv.horizon_end


def test_no_look_ahead_future_prices_do_not_change_past_decisions():
    book = synthetic_book(date(2026, 6, 1), 6)
    base = run_backtest(book, date(2026, 6, 1), date(2026, 6, 4), T, B, TerminalConfig(), 1.0)
    # change prices of 4 June: decisions before their publication (3 June 13:05) are unchanged
    t4 = local_midnight_utc(date(2026, 6, 4))
    for t in list(book.spot):
        if t >= t4:
            book.spot[t] = book.spot[t] * 3 + 1000
    other = run_backtest(book, date(2026, 6, 1), date(2026, 6, 4), T, B, TerminalConfig(), 1.0)
    pub = datetime(2026, 6, 3, 13, 15, tzinfo=PRAGUE).astimezone(UTC)
    for a, b in zip(base.intervals, other.intervals, strict=True):
        if a.start < pub:
            assert replace(a.step, index=0) == replace(b.step, index=0)
    assert any(
        a.step.delta_kwh != b.step.delta_kwh
        for a, b in zip(base.intervals, other.intervals, strict=True)
    )


def test_totals_and_perfect_foresight_bound():
    book = synthetic_book(date(2026, 6, 1), 15)
    res = run_backtest(book, date(2026, 6, 1), date(2026, 6, 14), T, B, TerminalConfig(), 1.0)
    cash = sum(iv.step.cash_czk - iv.step.wear_czk for iv in res.intervals)
    assert res.totals.profit_czk == pytest.approx(cash)
    tv = res.terminal_value_czk_kwh
    rolling = res.totals.profit_czk + (res.end_soc_kwh - res.initial_soc_kwh) * tv
    best = res.foresight.profit_czk + (res.foresight_end_soc_kwh - res.initial_soc_kwh) * tv
    assert best >= rolling - 1e-6
    assert res.totals.profit_czk > 0
    rows = res.breakdown("day")
    assert len(rows) == 14
    assert sum(r["profit_czk"] for r in rows) == pytest.approx(res.totals.profit_czk, abs=0.1)
    assert res.breakdown("month")[0]["period"] == "2026-06"


def test_hourly_era_and_gaps():
    book = synthetic_book(date(2025, 9, 28), 6, hourly=True)
    a, b = local_midnight_utc(date(2025, 9, 30)), local_midnight_utc(date(2025, 10, 1))
    for t in [t for t in book.spot if a <= t < b]:
        del book.spot[t]
    del book.resolution[date(2025, 9, 30)]
    res = run_backtest(book, date(2025, 9, 28), date(2025, 10, 1), T, B, TerminalConfig(), 1.0)
    assert res.skipped_intervals == 96  # 30 Sep unknown → idle
    assert any("skipped" in w for w in res.warnings)
    assert res.foresight is None


def test_monthly_fees_prorated_by_days_and_do_not_change_decisions():
    book = synthetic_book(date(2026, 6, 25), 10)
    vat = replace(T, buy_vat_percent=21)
    fees = replace(vat, buy_monthly_fee=147, distribution_monthly_fee=257)
    tv = TerminalConfig()
    plain = run_backtest(book, date(2026, 6, 25), date(2026, 7, 3), vat, B, tv, 1.0)
    res = run_backtest(book, date(2026, 6, 25), date(2026, 7, 3), fees, B, tv, 1.0)
    # fixed costs: identical decisions, trading profit unchanged
    assert [iv.step for iv in res.intervals] == [iv.step for iv in plain.intervals]
    assert res.totals.profit_czk == pytest.approx(plain.totals.profit_czk)
    monthly = 404 * 1.21
    june, july = monthly * 6 / 30, monthly * 3 / 31  # 25–30 June, 1–3 July
    assert res.totals.fees_czk == pytest.approx(june + july)
    assert res.totals.net_profit_czk == pytest.approx(res.totals.profit_czk - june - july)
    assert plain.totals.fees_czk == 0
    rows = res.breakdown("month")
    assert [r["period"] for r in rows] == ["2026-06", "2026-07"]
    assert rows[0]["fees_czk"] == pytest.approx(june, abs=0.01)
    assert rows[1]["fees_czk"] == pytest.approx(july, abs=0.01)
    for r in rows:
        assert r["net_profit_czk"] == pytest.approx(r["profit_czk"] - r["fees_czk"], abs=0.02)
    days = res.breakdown("day")
    assert sum(r["fees_czk"] for r in days) == pytest.approx(june + july, abs=0.05)
    # per-quarter share sums to the day share (incl. a 92-quarter DST day)
    d = date(2026, 3, 29)
    q = [local_midnight_utc(d) + i * QUARTER for i in range(92)]
    assert sum(fees.fees_for_quarter(t) for t in q) == pytest.approx(fees.fees_for_day(d))
    assert fees.fees_for_day(d) == pytest.approx(monthly / 31)


@pytest.mark.parametrize("jobs", [1, 3])
@pytest.mark.parametrize("foresight", [True, False])
def test_progress_callback_reaches_100_percent(foresight, jobs, monkeypatch):
    monkeypatch.setattr("bess_ote.backtest.MIN_UNITS_PER_WORKER", 1)  # force a pool
    book = synthetic_book(date(2026, 6, 1), 4)
    seen = []
    res = run_backtest(book, date(2026, 6, 1), date(2026, 6, 3), T, B, TerminalConfig(), 1.0,
                       foresight=foresight, jobs=jobs,
                       progress=lambda ph, f, det: seen.append((ph, f, det)))  # fmt: skip
    assert res.workers == (1 if jobs == 1 else 2 if foresight else 3)
    sim = [s for s in seen if s[0] == "simulate"]
    assert sim[0][1] == 0 and sim[-1][1] == pytest.approx(1.0)
    assert all(a[1] <= b[1] for a, b in pairwise(sim))
    if not foresight:
        assert all(s[2] != "perfect foresight" for s in seen)
    assert seen[-1] == ("done", 1.0, "3/3 days")  # final reported fraction == 1.0


def test_progress_done_when_foresight_skipped_for_gaps():
    book = synthetic_book(date(2025, 9, 28), 6, hourly=True)
    a, b = local_midnight_utc(date(2025, 9, 30)), local_midnight_utc(date(2025, 10, 1))
    for t in [t for t in book.spot if a <= t < b]:
        del book.spot[t]
    del book.resolution[date(2025, 9, 30)]
    seen = []
    res = run_backtest(book, date(2025, 9, 28), date(2025, 10, 1), T, B, TerminalConfig(), 1.0,
                       progress=lambda ph, f, det: seen.append((ph, f)))  # fmt: skip
    assert res.foresight is None and seen[-1] == ("done", 1.0)


def test_single_day_progress():
    book = synthetic_book(date(2026, 6, 1), 2)
    seen = []
    run_backtest(book, date(2026, 6, 1), date(2026, 6, 1), T, B, TerminalConfig(), 1.0,
                 progress=lambda ph, f, det: seen.append((ph, f)))  # fmt: skip
    assert seen[0] == ("simulate", 0.0) and seen[-1] == ("done", 1.0)


def reference_backtest(book, start, end, tariffs, battery, tcfg, soc, publish=None):
    """The previous (pre-parallel) serial loop, kept verbatim as the accuracy reference."""
    t = local_midnight_utc(start)
    t_end = local_midnight_utc(end + timedelta(days=1))
    prob = sol = state = None
    prob_idx = {}
    out = []
    while t < t_end:
        horizon = book.horizon(t, t, publish)
        if not horizon:
            t += QUARTER
            continue
        if prob is None or prob.starts[-1] != horizon[-1] or t not in prob_idx:
            prob = build_problem(book, t, t, tariffs, battery, tcfg, publish=publish,
                                 horizon=horizon, initial_soc_kwh=soc)  # fmt: skip
            sol = solve(battery, prob.buy, prob.sell, prob.terminal)
            prob_idx = {s: i for i, s in enumerate(prob.starts)}
            if state is None:
                state = sol.state_index(soc)
        off = prob_idx[t]
        st, state = step_at(sol, off, state)
        out.append((t, float(prob.spot[off]), float(prob.buy[off]), float(prob.sell[off]),
                    replace(st, index=0), prob.starts[-1] + QUARTER))  # fmt: skip
        t += QUARTER
    return out


def _flat(res):
    return [(iv.start, iv.spot_czk_mwh, iv.buy_czk_kwh, iv.sell_czk_kwh,
             replace(iv.step, index=0), iv.horizon_end) for iv in res.intervals]  # fmt: skip


B2 = Battery(7, 3, discharge_kw=4, soc_min_pct=5, soc_max_pct=90, cycle_cost_czk_kwh=0.3)


@pytest.mark.parametrize(
    ("battery", "tcfg", "pub", "hourly"),
    [
        (B, TerminalConfig(), None, False),
        (B, TerminalConfig("min-soc", None, 40), time(14, 20), False),
        (B2, TerminalConfig("zero"), None, True),
        (B2, TerminalConfig("value", 2.5, None), time(11, 0), False),
    ],
)
def test_parallel_is_bit_identical_to_serial_and_previous_version(
    battery, tcfg, pub, hourly, monkeypatch
):
    monkeypatch.setattr("bess_ote.backtest.MIN_UNITS_PER_WORKER", 1)  # force the pool
    first = date(2025, 9, 20) if hourly else date(2026, 6, 1)
    book = synthetic_book(first, 22, hourly=hourly, seed=7)
    args = (book, first + timedelta(days=1), first + timedelta(days=19), T, battery, tcfg, 3.0)
    serial = run_backtest(*args, publish=pub, jobs=1)
    par = run_backtest(*args, publish=pub, jobs=4)
    assert serial.workers == 1 and par.workers == 3
    assert _flat(par) == _flat(serial)  # every interval, every float, bit for bit
    assert par.totals == serial.totals and par.foresight == serial.foresight
    assert par.end_soc_kwh == serial.end_soc_kwh and par.solves == serial.solves
    assert par.foresight_end_soc_kwh == serial.foresight_end_soc_kwh
    assert _flat(serial) == reference_backtest(*args, publish=pub)


def test_parallel_with_gaps_and_auto_jobs(monkeypatch):
    monkeypatch.setattr("bess_ote.backtest.MIN_UNITS_PER_WORKER", 1)
    book = synthetic_book(date(2026, 6, 1), 12, seed=3)
    a, b = local_midnight_utc(date(2026, 6, 5)), local_midnight_utc(date(2026, 6, 6))
    for t in [t for t in book.spot if a <= t < b]:
        del book.spot[t]
    del book.resolution[date(2026, 6, 5)]
    args = (book, date(2026, 6, 1), date(2026, 6, 10), T, B, TerminalConfig(), 1.0)
    serial = run_backtest(*args, jobs=1)
    auto = run_backtest(*args, jobs=None)
    assert _flat(auto) == _flat(serial) == reference_backtest(*args)
    assert auto.skipped_intervals == serial.skipped_intervals == 96
    assert auto.warnings == serial.warnings and auto.foresight is None


def test_pool_unavailable_falls_back_to_serial(monkeypatch):
    monkeypatch.setattr("bess_ote.backtest.MIN_UNITS_PER_WORKER", 1)
    monkeypatch.setattr("bess_ote.backtest.make_process_pool", lambda n: None)  # e.g. Termux
    book = synthetic_book(date(2026, 6, 1), 6)
    args = (book, date(2026, 6, 1), date(2026, 6, 5), T, B, TerminalConfig(), 1.0)
    res = run_backtest(*args, jobs=8)
    assert res.workers == 1 and _flat(res) == reference_backtest(*args)


def test_horizon_end_matches_horizon():
    book = synthetic_book(date(2026, 6, 1), 5)
    t = local_midnight_utc(date(2026, 5, 31))
    while t < local_midnight_utc(date(2026, 6, 7)):
        for pub in (None, time(14, 20)):
            h = book.horizon(t, t, pub)
            assert book.horizon_end(t, t, pub) == (h[-1] if h else None)
        t += QUARTER


def test_spawn_start_method_like_windows(monkeypatch):
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor

    monkeypatch.setattr("bess_ote.backtest.MIN_UNITS_PER_WORKER", 1)
    ctx = multiprocessing.get_context("spawn")
    monkeypatch.setattr(
        "bess_ote.backtest.make_process_pool",
        lambda n: ProcessPoolExecutor(max_workers=n, mp_context=ctx),
    )
    book = synthetic_book(date(2026, 6, 1), 6)
    args = (book, date(2026, 6, 1), date(2026, 6, 5), T, B, TerminalConfig(), 1.0)
    res = run_backtest(*args, jobs=3)
    assert res.workers == 2 and _flat(res) == reference_backtest(*args)


def test_fine_breakdown_keeps_skipped_quarters_and_fees():
    book = synthetic_book(date(2026, 6, 1), 6, seed=4)
    a, b = local_midnight_utc(date(2026, 6, 3)), local_midnight_utc(date(2026, 6, 4))
    for t in [t for t in book.spot if a <= t < b]:
        del book.spot[t]
    del book.resolution[date(2026, 6, 3)]
    fees = replace(T, buy_monthly_fee=300, buy_vat_percent=21)
    res = run_backtest(book, date(2026, 6, 1), date(2026, 6, 4), fees, B, TerminalConfig(), 1.0)
    for by, n in (("hour", 96), ("quarter-hour", 384)):
        rows = res.breakdown(by)
        assert len(rows) == n
        idle = [r for r in rows if r["action"] == "no price"]
        assert len(idle) == n // 4 and all(r["spot_czk_mwh"] is None for r in idle)
        assert all(r["pattern"] in ("....", ".") for r in idle)
        assert sum(r["fees_czk"] for r in rows) == pytest.approx(res.totals.fees_czk, abs=1e-3)
        assert sum(r["profit_czk"] for r in rows) == pytest.approx(res.totals.profit_czk, abs=0.01)
