"""PV plant: physics, optimiser with free PV energy, rolling backtest with forecasts settled on
actuals, data sources (synthetic data only – no real location or measurements)."""

import json
import math
import re
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import numpy as np
import pytest
import responses

from bess_ote.backtest import PvSetup, run_backtest
from bess_ote.battery import DT_H, Battery
from bess_ote.cli import main
from bess_ote.optimizer import Terminal, pv_flows, rollout, solve
from bess_ote.planner import TerminalConfig, plan
from bess_ote.pv import Irradiance, PvArray, PvError, PvSystem, plane_of_array, sun_position
from bess_ote.pvdata import (
    CHMI_URL,
    SINGLE_RUNS_URL,
    Chmi,
    _parse_series,
    latest_run,
    previous_run,
    runs_between,
)
from bess_ote.tariff import Distribution, SpotFormula, Tariffs
from bess_ote.timeutil import QUARTER, local_midnight_utc
from conftest import FakeOte, add_cnb, add_ote, synthetic_book

B = Battery(10, 5, soc_min_pct=10, soc_max_pct=95)
T = Tariffs(buy=SpotFormula(1.0, 400), sell=SpotFormula(1.0, -300),
            distribution=Distribution(2.0), buy_vat_percent=21)  # fmt: skip
KWP = 6.0


# -- synthetic PV ------------------------------------------------------------------------------
def truth_pv(start: date, days: int) -> dict[datetime, float]:
    """Bell-shaped days (UTC 4–18 h) with day-to-day and quarter-to-quarter clouds."""
    out = {}
    t, end = local_midnight_utc(start), local_midnight_utc(start + timedelta(days=days))
    while t < end:
        h = t.hour + t.minute / 60 + 0.125
        sun = max(0.0, math.sin((h - 4) / 14 * math.pi)) if 4 <= h <= 18 else 0.0
        cloud = 0.55 + 0.45 * math.sin(t.toordinal() * 1.3) * math.cos(h * 2.1)
        out[t] = KWP * DT_H * 0.85 * sun * max(0.05, cloud)
        t += QUARTER
    return out


class FakeForecast:
    """Runs every 3 h known 2 h after their start; each run errs differently."""

    def __init__(self, truth: dict[datetime, float]) -> None:
        self.truth = truth

    def at(self, at: datetime, starts: list[datetime]) -> tuple[list[float], int]:
        run = int(((at - timedelta(hours=2)).timestamp()) // (3 * 3600))
        vals = [self.truth.get(t, 0.0) * (1 + 0.45 * math.sin(run * 1.7 + t.timestamp() / 5e3))
                for t in starts]  # fmt: skip
        return vals, run


class ZeroForecast:
    def at(self, at: datetime, starts: list[datetime]) -> tuple[list[float], int]:
        return [0.0] * len(starts), 0


def _setup(replan: str, days: int = 9) -> tuple:
    book = synthetic_book(date(2026, 5, 31), days)
    truth = truth_pv(date(2026, 5, 31), days)
    return book, truth, PvSetup(FakeForecast(truth), truth, KWP, replan)


# -- physics -------------------------------------------------------------------------------------
def test_sun_position_and_plane_of_array():
    cz, az = sun_position(datetime(2026, 6, 21, 11, 2, tzinfo=UTC), 50.08, 14.42)
    assert 90 - math.degrees(math.acos(cz)) == pytest.approx(63.4, abs=0.2)  # solar noon
    assert abs(az) < 1.5
    cz, az = sun_position(datetime(2026, 6, 21, 6, 0, tzinfo=UTC), 50.08, 14.42)
    assert -110 < az < -70  # morning sun in the east
    south, north = PvArray(0, 35, 1), PvArray(180, 35, 1)
    noon = datetime(2026, 3, 20, 11, 0, tzinfo=UTC)
    cz, az = sun_position(noon, 50.0, 15.0)
    irr = Irradiance(500, 100)
    assert plane_of_array(irr, cz, az, south, noon) > 500 > plane_of_array(irr, cz, az, north, noon)
    flat = plane_of_array(irr, cz, az, PvArray(0, 0, 1), noon)
    assert flat == pytest.approx(500, rel=1e-9)  # horizontal module = global irradiance


def test_quarter_energy_scales_with_kwp_losses_and_temperature():
    t = datetime(2026, 6, 21, 10, 55, tzinfo=UTC)
    one = PvSystem(50, 15, (PvArray(0, 0, 1),), losses_pct=0)
    # 1000 W/m² flat at 25 °C module temperature: exactly 1 kW → 0.25 kWh
    t_air = 25 - 1000 * 25 / 800
    assert one.quarter_kwh(t, Irradiance(1000, 1000, t_air)) == pytest.approx(0.25)
    two = PvSystem(50, 15, (PvArray(0, 0, 1), PvArray(0, 0, 2)), losses_pct=14)
    assert two.quarter_kwh(t, Irradiance(1000, 1000, t_air)) == pytest.approx(0.25 * 3 * 0.86)
    hot = one.quarter_kwh(t, Irradiance(1000, 1000, t_air + 10))
    assert hot == pytest.approx(0.25 * 0.96)  # −0.4 %/K
    assert one.quarter_kwh(datetime(2026, 6, 21, 22, 0, tzinfo=UTC), Irradiance(50)) == 0.0


def test_pv_array_parsing():
    assert PvArray.parse("-90, 20, 3.5") == PvArray(-90, 20, 3.5)
    for bad in ("0,35", "a,b,c", "0,95,5", "0,35,0"):
        with pytest.raises(PvError):
            PvArray.parse(bad)


# -- optimiser ----------------------------------------------------------------------------------
def test_pv_flows_use_free_energy_best():
    b = Battery(10, 4, charge_eff=1.0, discharge_eff=1.0, inverter_kw=4)  # 1 kWh / quarter
    # charging 0.6 kWh with 1 kWh PV: PV covers it, 0.4 sold at a positive price
    assert [float(x) for x in pv_flows(b, 0.6, 1.0, 3.0, 2.0)] == pytest.approx([0, 0, 0.6, 0.4, 0])
    # negative sale price: the rest is curtailed
    assert [float(x) for x in pv_flows(b, 0.6, 1.0, 3.0, -0.5)] == pytest.approx(
        [0, 0, 0.6, 0, 0.4]
    )
    # discharge 0.8 + PV 0.5 with a 1 kWh/quarter inverter: only 0.2 PV fits
    assert [float(x) for x in pv_flows(b, -0.8, 0.5, 3.0, 2.0)] == pytest.approx(
        [0, 0.8, 0, 0.2, 0.3]
    )
    # PV shortfall bought from the grid
    assert [float(x) for x in pv_flows(b, 0.9, 0.3, 3.0, 2.0)] == pytest.approx([0.6, 0, 0.3, 0, 0])
    # buying pays (negative buy price): grid first, PV only above the inverter limit
    assert [float(x) for x in pv_flows(b, 1.0, 0.5, -1.0, -2.0)] == pytest.approx(
        [1.0, 0, 0, 0, 0.5]
    )


def _lp_pv_profit(b, buy, sell, pv, e0):
    """Continuous relaxation with PV (charge from grid/PV, PV export/curtail, shared limit)."""
    linprog = pytest.importorskip("scipy.optimize").linprog
    n = len(buy)
    # variables: g (grid→bat AC), c (PV→bat), d (bat→grid AC), x (PV→grid)
    nv = 4 * n
    obj = np.concatenate([buy, np.zeros(n), -(sell - b.cycle_cost_czk_kwh), -sell])
    tri = np.tril(np.ones((n, n)))
    soc = np.hstack([tri * b.charge_eff, tri * b.charge_eff, -tri / b.discharge_eff,
                     np.zeros((n, n))])  # fmt: skip
    eye, z = np.eye(n), np.zeros((n, n))
    lq = b.inverter_q_kwh
    rows = [soc, -soc, np.hstack([eye, eye, z, z]), np.hstack([z, eye, z, eye]),
            np.hstack([z, z, eye, eye]), np.hstack([eye, z, z, z])]  # fmt: skip
    rhs = [np.full(n, b.e_max - e0), np.full(n, e0 - b.e_min), np.full(n, b.charge_kw * DT_H),
           pv, np.full(n, lq), np.full(n, lq)]  # fmt: skip
    res = linprog(obj, A_ub=np.vstack(rows), b_ub=np.concatenate(rhs),
                  bounds=[(0, None)] * nv, method="highs")  # fmt: skip
    assert res.status == 0
    return -res.fun


@pytest.mark.parametrize("inverter", [None, 4.0])
def test_dp_with_pv_matches_continuous_lp(inverter):
    rnd = np.random.default_rng(3)
    n = 96 * 2
    h = np.arange(n) % 96 / 4
    sell = 1.5 + np.sin((h - 6) / 24 * 2 * np.pi) * 1.4 + rnd.normal(0, 0.3, n)
    buy = sell + 1.5
    pv = np.clip(np.sin((h - 5) / 14 * np.pi), 0, None) * 1.5 * DT_H * 4 * rnd.uniform(0.3, 1, n)
    b = Battery(10, 5, soc_min_pct=10, cycle_cost_czk_kwh=0.05,
                inverter_kw=inverter or 5.0)  # fmt: skip
    lp = _lp_pv_profit(b, buy, sell, pv, b.e_min)
    steps = rollout(solve(b, buy, sell, Terminal("zero"), pv=pv), b.e_min)
    dp = sum(s.cash_czk - s.wear_czk for s in steps)
    assert dp <= lp + 1e-6
    assert dp >= lp * 0.995
    for s, p in zip(steps, pv, strict=True):  # PV fully accounted for, never both ways
        assert s.pv_battery_kwh + s.pv_grid_kwh + s.pv_curtailed_kwh == pytest.approx(p)
        assert not (s.delta_kwh < 0 and s.pv_battery_kwh > 0)


def test_zero_pv_is_bit_identical_to_no_pv():
    book = synthetic_book(date(2026, 5, 31), 8)
    args = (book, date(2026, 6, 1), date(2026, 6, 6), T, B, TerminalConfig(), 2.0)
    base = run_backtest(*args)
    for bat in (B, replace(B, inverter_kw=5.0)):  # default inverter with PV = battery power
        z = run_backtest(*args[:4], bat, *args[5:], pv=PvSetup(ZeroForecast(), {}, KWP))
        assert z.solves == base.solves
        for a, c in zip(base.intervals, z.intervals, strict=True):
            assert a.step == c.step

        def strip(d, drop=("pv_",)):
            return {k: v for k, v in d.items() if not k.startswith(drop)}

        assert strip(z.totals.as_dict(10)) == strip(base.totals.as_dict(10))
        assert strip(z.foresight.as_dict(10)) == strip(base.foresight.as_dict(10))


@pytest.mark.parametrize("replan", ["forecast", "quarter"])
def test_pv_backtest_decision_equals_plan_at_t(replan):
    book, truth, setup = _setup(replan)
    bat = replace(B, inverter_kw=5.0)
    res = run_backtest(book, date(2026, 6, 1), date(2026, 6, 7), T, bat, TerminalConfig(), 2.0,
                       pv=setup)  # fmt: skip
    for i in range(0, len(res.intervals), 7):
        iv = res.intervals[i]
        soc = res.initial_soc_kwh if i == 0 else res.intervals[i - 1].step.soc_kwh
        if replan == "forecast":
            fc = setup.forecast

            def pvf(starts, at=iv.start, fc=fc):
                return fc.at(at, starts)[0]
        else:  # the forecast vector the backtest used at t (incl. the nowcast)
            ev_pv = iv.pv_forecast_kwh

            def pvf(starts, at=iv.start, first=ev_pv):
                vals = setup.forecast.at(at, starts)[0]
                return [first, *vals[1:]] if replan == "quarter" else vals

        p = plan(book, iv.start, soc, T, bat, TerminalConfig(), pv=pvf)
        if replan == "forecast":
            assert p.next_step.delta_kwh == iv.step.delta_kwh or truth[iv.start] != 0, i
            planned = p.solution.choice[0, p.solution.state_index(soc)]
            assert p.problem.pv[0] == iv.pv_forecast_kwh
            assert p.solution.deltas[planned] * bat.step == pytest.approx(p.next_step.delta_kwh)


def test_pv_backtest_bounds_balance_and_modes():
    book, truth, _ = _setup("forecast")
    bat = replace(B, inverter_kw=5.0)
    runs = {}
    for mode in ("publication", "forecast", "quarter"):
        setup = PvSetup(FakeForecast(truth), truth, KWP, mode)
        res = run_backtest(book, date(2026, 6, 1), date(2026, 6, 7), T, bat, TerminalConfig(),
                           2.0, pv=setup)  # fmt: skip
        runs[mode] = res
        tot, f = res.totals, res.foresight
        assert tot.pv_kwh == pytest.approx(sum(truth[iv.start] for iv in res.intervals))
        assert tot.pv_battery_kwh + tot.pv_grid_kwh + tot.pv_curtailed_kwh == pytest.approx(
            tot.pv_kwh
        )
        tv = res.terminal_value_czk_kwh
        rolling = tot.profit_czk + (res.end_soc_kwh - res.initial_soc_kwh) * tv
        best = f.profit_czk + (res.foresight_end_soc_kwh - res.initial_soc_kwh) * tv
        assert rolling <= best + 1e-6  # perfect foresight (prices + actual PV) is an upper bound
        assert f.pv_kwh == pytest.approx(tot.pv_kwh)
        rows = res.breakdown("hour")
        assert sum(r["pv_kwh"] for r in rows) == pytest.approx(tot.pv_kwh, abs=1e-4)
        assert sum(r["profit_czk"] for r in rows) == pytest.approx(tot.profit_czk, abs=1e-3)
        assert res.pv["events"] == res.solves
    assert runs["publication"].solves < runs["forecast"].solves < runs["quarter"].solves
    assert runs["publication"].solves == 8  # only price publications, as without PV
    no_pv = run_backtest(book, date(2026, 6, 1), date(2026, 6, 7), T, bat, TerminalConfig(), 2.0)
    assert runs["forecast"].totals.profit_czk > no_pv.totals.profit_czk  # free energy helps


def test_pv_parallel_identical_to_serial(monkeypatch):
    monkeypatch.setattr("bess_ote.backtest.MIN_UNITS_PER_WORKER", 1)
    book, _truth, setup = _setup("quarter", days=5)
    args = (book, date(2026, 6, 1), date(2026, 6, 3), T, replace(B, inverter_kw=4.0),
            TerminalConfig(), 3.0)  # fmt: skip
    s = run_backtest(*args, pv=setup, jobs=1)
    p = run_backtest(*args, pv=setup, jobs=3)
    assert p.workers > 1
    assert [iv.step for iv in s.intervals] == [iv.step for iv in p.intervals]
    assert s.totals == p.totals and s.foresight == p.foresight


def test_pv_foresight_never_decreases_with_capacity_on_a_shared_grid():
    """With PV the optimum depends on how exactly partial PV surplus fits the SoC grid, so
    capacity monotonicity is exact for batteries sharing the grid step (all above
    400 × the full-power quarter / K, and pairs with the same power-of-two refinement)."""
    rnd = np.random.default_rng(8)
    n = 96 * 2
    h = np.arange(n) % 96 / 4
    sell = 1.2 + np.sin((h - 6) / 24 * 2 * np.pi) + rnd.normal(0, 0.4, n)
    buy = sell + 1.8
    pv = np.clip(np.sin((h - 5) / 14 * np.pi), 0, None) * 1.2 * rnd.uniform(0.2, 1, n)
    prev = -math.inf
    for cap in (60, 80, 120, 175, 400):
        b = Battery(cap, 5, soc_min_pct=10, inverter_kw=5)
        assert b.step == Battery(60, 5).step
        steps = rollout(solve(b, buy, sell, Terminal("zero"), pv=pv), b.e_min)
        p = sum(s.cash_czk - s.wear_czk for s in steps)
        assert p >= prev - 1e-9, cap
        prev = p


# -- data sources -------------------------------------------------------------------------------
def test_parse_series_interval_ending_labels():
    doc = {
        "minutely_15": {
            "time": ["2026-06-01T10:15", "2026-06-01T10:30"],
            "shortwave_radiation": [400.0, None],
            "diffuse_radiation": [100.0, None],
            "temperature_2m": [20.0, 21.0],
        }
    }
    q = _parse_series(doc, True)
    assert list(q) == [datetime(2026, 6, 1, 10, 0, tzinfo=UTC)] and q[next(iter(q))].ghi == 400
    h = _parse_series({"hourly": {"time": ["2026-06-01T11:00"], "shortwave_radiation": [300.0]}},
                      False)  # fmt: skip
    assert sorted(h) == [datetime(2026, 6, 1, 10, m, tzinfo=UTC) for m in (0, 15, 30, 45)]


def test_run_schedule_and_model_switch():
    at = datetime(2026, 6, 14, 11, 5, tzinfo=UTC)  # 13:05 CEST, OTE publication
    r = latest_run(at)
    assert (r.model, r.init.hour, r.available) == (
        "icon_d2",
        9,
        datetime(2026, 6, 14, 11, tzinfo=UTC),
    )
    assert previous_run(r).init.hour == 6
    old = latest_run(datetime(2025, 10, 14, 11, 5, tzinfo=UTC))
    assert (old.model, old.init.hour) == ("ecmwf_ifs", 0)  # 06 UTC run known only at 13 UTC
    first = latest_run(datetime(2026, 4, 2, 3, 0, tzinfo=UTC))
    assert first.model == "icon_d2" and previous_run(first).model == "ecmwf_ifs"
    got = runs_between(datetime(2026, 6, 14, 0, tzinfo=UTC), datetime(2026, 6, 14, 12, tzinfo=UTC))
    assert [x.init.hour for x in got] == [0, 3, 6, 9]


def test_chmi_quarter_means_from_10_minute_values(monkeypatch, tmp_path):
    from bess_ote.cache import JsonCache

    c = Chmi(None, JsonCache(None), today=date(2026, 7, 1))
    vals = {}
    t = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)
    for k in range(1, 7):  # labels 10:10 … 11:00 (end of each 10 minutes)
        lab = t + timedelta(minutes=10 * k)
        vals[lab.strftime("%Y-%m-%dT%H:%M:%SZ")] = [100.0 * k, 20.0]
    monkeypatch.setattr(c, "_period", lambda wsi, y, m: vals)
    q = c.irradiance("x", t, t + timedelta(hours=1))
    # 10:00–10:15 = 10 min of 100 + 5 min of 200
    assert q[t].ghi == pytest.approx((100 * 10 + 200 * 5) / 15)
    assert q[t + QUARTER].ghi == pytest.approx((200 * 5 + 300 * 10) / 15)
    assert len(q) == 4


# -- CLI end to end (all HTTP mocked, synthetic data) --------------------------------------------
WSI = "0-20000-0-99999"


def _om_callback(request):
    q = parse_qs(urlparse(request.url).query)
    init = datetime.fromisoformat(q["run"][0]).replace(tzinfo=UTC)
    hours = int(q.get("forecast_hours", ["72"])[0])
    times, ghi, dhi, tmp = [], [], [], []
    for k in range(1, hours + 1):
        end = init + timedelta(hours=k)
        h = end.hour - 0.5
        g = max(0.0, 700 * math.sin((h - 5) / 13 * math.pi)) if 5 <= h <= 18 else 0.0
        g *= 0.8 + 0.2 * math.sin(init.hour + k)
        times.append(end.strftime("%Y-%m-%dT%H:%M"))
        ghi.append(round(g, 1))
        dhi.append(round(g * 0.3, 1))
        tmp.append(15.0)
    body = {"hourly": {"time": times, "shortwave_radiation": ghi, "diffuse_radiation": dhi,
                       "temperature_2m": tmp}}  # fmt: skip
    return 200, {}, json.dumps(body)


def _chmi_month(d0: date, d1: date) -> str:
    rows = []
    t = datetime(d0.year, d0.month, d0.day, tzinfo=UTC)
    while t.date() <= d1:
        t += timedelta(minutes=10)
        h = t.hour + t.minute / 60 - 1 / 12
        g = max(0.0, 650 * math.sin((h - 5) / 13 * math.pi)) if 5 <= h <= 18 else 0.0
        ts = t.strftime("%Y-%m-%dT%H:%M:%SZ")
        rows += [[WSI, "RGLB10", ts, round(g, 1), "", 0.0], [WSI, "T", ts, 14.0, "", 0.0]]
    return json.dumps({"data": {"data": {"header": "STATION,ELEMENT,DT,VAL,FLAG,QUALITY",
                                         "values": rows}}})  # fmt: skip


def _add_pv_mocks(rsps):
    rsps.add_callback(responses.GET, re.compile(re.escape(SINGLE_RUNS_URL) + r".*"),
                      callback=_om_callback)  # fmt: skip
    meta1 = {
        "data": {
            "data": {
                "header": "WSI,GH_ID,FULL_NAME,GEOGR1,GEOGR2,ELEVATION,BEGIN_DATE",
                "values": [
                    [WSI, "X", "Testovací stanice", 15.0, 50.0, 300.0, ""],
                    ["0-1", "Y", "Daleko", 18.0, 49.0, 300.0, ""],
                ],
            }
        }
    }
    meta2 = {
        "data": {
            "data": {
                "header": "OBS_TYPE,WSI,EG_EL_ABBREVIATION,NAME",
                "values": [["10M", WSI, "RGLB10", "Záření"], ["10M", "0-1", "RGLB10", "Záření"]],
            }
        }
    }
    rsps.add(responses.GET, re.compile(re.escape(CHMI_URL) + r"/now/metadata/meta1-.*"), json=meta1)
    rsps.add(responses.GET, re.compile(re.escape(CHMI_URL) + r"/now/metadata/meta2-.*"), json=meta2)
    rsps.add(
        responses.GET,
        f"{CHMI_URL}/historical/data/10min/2025/10m-{WSI}-202510.json",
        body=_chmi_month(date(2025, 10, 1), date(2025, 10, 31)),
    )


def _price(d, i):
    n = 24 if d < date(2025, 10, 1) else 96
    return 2500 + 2000 * math.sin(i / n * 2 * math.pi) + d.day * 10


@pytest.fixture
def pv_http(mocked, tmp_path, monkeypatch):
    monkeypatch.setenv("BESS_OTE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.delenv("BESS_OTE_CONFIG", raising=False)
    add_ote(mocked, FakeOte(_price))
    add_cnb(mocked)
    _add_pv_mocks(mocked)
    return mocked


BAT = ["-c", "10", "-p", "5", "--soc", "20", "-a", "400", "--sell-add", "-300"]
PV = ["--pv-array", "0,35,4", "--pv-array=-90,20,2", "--lat", "50.01", "--lon", "15.02"]


def test_cli_backtest_with_pv_json_and_breakdown(pv_http, capsys, tmp_path):
    argv = ["backtest", *BAT, *PV, "--from", "2025-10-14", "--to", "2025-10-15", "--json",
            "-b", "qh", "--no-progress"]  # fmt: skip
    rc = main(argv)
    out, _err = capsys.readouterr()
    assert rc == 0, out
    doc = json.loads(out)
    s = doc["summary"]
    assert doc["pv"]["system"].startswith("6 kWp") and doc["pv"]["replan"] == "forecast"
    assert "Testovací stanice" in doc["pv"]["actual"] and doc["pv"]["inverter_kw"] == 5.0
    assert s["pv_kwh"] > 5 and s["pv_forecast_kwh"] > 5
    assert s["pv_battery_kwh"] + s["pv_grid_kwh"] + s["pv_curtailed_kwh"] == pytest.approx(
        s["pv_kwh"], abs=1e-3
    )
    rows = doc["breakdown"]["rows"]
    assert len(rows) == 192 and "pv_forecast_kwh" in rows[0]
    assert sum(r["pv_kwh"] for r in rows) == pytest.approx(s["pv_kwh"], abs=1e-3)
    assert doc["perfect_foresight"]["pv_kwh"] == pytest.approx(s["pv_kwh"], abs=1e-3)
    # cached second run; other re-planning modes; text output
    for mode in ("publication", "quarter"):
        rc = main([*argv[:-4], "--replan", mode, "--no-progress"])
        cap = capsys.readouterr()
        assert rc == 0 and f"re-planning: {mode}" in cap.out and "PV → battery" in cap.out
        assert "PV forecasts:" in cap.err and "from cache" in cap.err


def test_cli_plan_at_with_pv_and_config(pv_http, capsys, tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text(
        "[pv]\nlat = 50.01\nlon = 15.02\narrays = [[0, 35, 4], {azimuth = 90, "
        "tilt = 30, kwp = 1.5}]\ninverter_kw = 4\n",
        encoding="utf-8",
    )
    rc = main(["plan", *BAT, "--config", str(cfg), "--at", "2025-10-14 13:20", "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert doc["pv"]["forecast"].startswith("Open-Meteo run ecmwf_ifs 2025-10-14 00")
    assert "inverter AC limit 4 kW" in doc["battery"]
    assert doc["summary"]["pv_kwh"] > 0 and "pv_battery_kwh" in doc["intervals"][0]
    rc = main(["plan", *BAT, "--config", str(cfg), "--at", "2025-10-14 13:20", "--next"])
    nxt = capsys.readouterr().out.splitlines()[1]
    assert rc == 0 and nxt.startswith("NEXT 13:30–13:45") and "→ battery" in nxt, nxt


def test_cli_pv_needs_location(pv_http, capsys):
    rc = main(["backtest", *BAT, "--pv-array", "0,35,5", "--from", "2025-10-14"])
    assert rc == 2 and "location" in capsys.readouterr().err
    rc = main(["backtest", *BAT, "--pv-array", "0,35", "--lat", "50", "--lon", "15",
               "--from", "2025-10-14"])  # fmt: skip
    assert rc == 2 and "AZIMUTH,TILT,KWP" in capsys.readouterr().err


def test_open_meteo_retries_throttling(monkeypatch):
    from bess_ote import pvdata

    class Resp:
        def __init__(self, code):
            self.status_code = code

    codes = iter([429, 503, 200])
    calls, slept = [], []

    class Session:
        def get(self, url, params, timeout):
            calls.append(url)
            return Resp(next(codes))

    monkeypatch.setattr(pvdata, "_sleep", slept.append)
    assert pvdata.get_with_retry(Session(), "u", {}).status_code == 200
    assert len(calls) == 3 and slept == list(pvdata.RETRY_DELAYS_S[:2])
    always = Session()
    always.get = lambda url, params, timeout: Resp(429)
    slept.clear()
    assert pvdata.get_with_retry(always, "u", {}).status_code == 429
    assert slept == list(pvdata.RETRY_DELAYS_S)
