"""Backtest = rolling plan: identical decisions to `plan --at t`, no look-ahead."""

import random
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from itertools import pairwise

import pytest

from bess_ote.backtest import run_backtest
from bess_ote.battery import Battery
from bess_ote.lowtariff import LowTariffSchedule
from bess_ote.planner import TerminalConfig, plan
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


def test_progress_callback_reaches_100_percent():
    book = synthetic_book(date(2026, 6, 1), 4)
    seen = []
    run_backtest(book, date(2026, 6, 1), date(2026, 6, 3), T, B, TerminalConfig(), 1.0,
                 progress=lambda ph, f, det: seen.append((ph, f, det)))  # fmt: skip
    sim = [s for s in seen if s[0] == "simulate"]
    assert sim[0][1] == 0 and sim[-1][1] == 1.0 and sim[-1][2] == "3/3 days"
    assert all(a[1] <= b[1] for a, b in pairwise(sim))
    assert any(s[0] == "foresight" for s in seen)
