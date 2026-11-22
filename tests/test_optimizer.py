import itertools
import math

import numpy as np
import pytest

from bess_ote.battery import DT_H, Battery, BatteryError
from bess_ote.optimizer import Terminal, _grid, rollout, solve, solve_rollout


def bat(**kw):
    return Battery(**({"capacity_kwh": 1.0, "charge_kw": 4.0, "soc_step_kwh": 0.25} | kw))


def test_charge_low_discharge_high_no_simultaneous():
    b = bat(charge_eff=1.0, discharge_eff=1.0)
    buy = np.array([1.0, 5.0, 1.0, 5.0])
    sell = buy - 0.5
    steps = rollout(solve(b, buy, sell, Terminal("zero")), 0.0)
    assert [s.action for s in steps] == ["charge", "discharge", "charge", "discharge"]
    assert sum(s.cash_czk for s in steps) == pytest.approx(2 * (4.5 - 1.0))
    for s in steps:  # one signed action per interval
        assert (s.grid_kwh > 0) == (s.delta_kwh > 0)


def test_power_and_capacity_limits_and_efficiency():
    b = Battery(10, 2, charge_eff=0.9, discharge_eff=0.9, soc_step_kwh=0.05)
    buy = np.array([1.0] * 30 + [10.0] * 30)
    steps = rollout(solve(b, buy, buy, Terminal("zero")), 0.0)
    assert max(s.grid_kwh for s in steps) <= 2 * 0.25 + 1e-9  # 2 kW for 15 min
    assert max(s.soc_kwh for s in steps) <= 10 + 1e-9
    stored = max(s.soc_kwh for s in steps)
    bought = sum(s.grid_kwh for s in steps if s.grid_kwh > 0)
    sold = -sum(s.grid_kwh for s in steps if s.grid_kwh < 0)
    assert stored == pytest.approx(bought * 0.9, abs=1e-6)
    assert sold == pytest.approx(stored * 0.9, abs=1e-6)
    assert steps[-1].soc_kwh == pytest.approx(0.0)


def test_spread_below_losses_means_wait():
    b = bat(charge_eff=0.9, discharge_eff=0.9)
    buy = np.array([1.0, 1.15])  # 1.15 × 0.81 < 1 → not worth it
    steps = rollout(solve(b, buy, buy, Terminal("zero")), 0.0)
    assert {s.action for s in steps} == {"wait"}


def test_negative_buy_price_charges_and_zero_terminal_dumps():
    b = bat(charge_eff=1.0, discharge_eff=1.0)
    buy = np.array([-2.0, 0.5])
    sell = np.array([-3.0, 0.1])
    steps = rollout(solve(b, buy, sell, Terminal("zero")), 0.0)
    assert steps[0].action == "charge" and steps[1].action == "discharge"


def test_terminal_value_keeps_energy_and_min_soc():
    b = bat(charge_eff=1.0, discharge_eff=1.0)
    buy = np.array([1.0, 1.0])
    sell = np.array([0.5, 0.5])
    keep = rollout(solve(b, buy, sell, Terminal("value", czk_per_kwh_stored=0.8)), 1.0)
    assert {s.action for s in keep} == {"wait"}
    dump = rollout(solve(b, buy, sell, Terminal("zero")), 1.0)
    assert dump[-1].soc_kwh == pytest.approx(0.0)  # equal prices: when is a tie
    fill = rollout(solve(b, buy, sell, Terminal("min-soc", min_kwh=1.0)), 0.0)
    assert fill[-1].soc_kwh == pytest.approx(1.0)


def test_nan_price_forces_wait():
    b = bat(charge_eff=1.0, discharge_eff=1.0)
    buy = np.array([1.0, math.nan, 9.0])
    steps = rollout(solve(b, buy, buy, Terminal("zero")), 0.0)
    assert steps[1].action == "wait" and steps[0].action == "charge"


def test_wear_cost_blocks_small_spreads():
    b = bat(charge_eff=1.0, discharge_eff=1.0, cycle_cost_czk_kwh=1.0)
    steps = rollout(solve(b, np.array([1.0, 1.8]), np.array([1.0, 1.8]), Terminal("zero")), 0.0)
    assert {s.action for s in steps} == {"wait"}


def test_dp_equals_brute_force():
    """Exhaustive search over all action sequences on the same grid."""
    rng = np.random.default_rng(3)
    b = Battery(1.0, 2.0, charge_eff=0.95, discharge_eff=0.9, soc_step_kwh=0.25)
    for _ in range(5):
        buy = rng.uniform(-1, 5, 5)
        sell = buy - rng.uniform(0, 1, 5)
        sol = solve(b, buy, sell, Terminal("value", czk_per_kwh_stored=1.0))
        dp = sum(s.cash_czk for s in rollout(sol, 0.5)) + rollout(sol, 0.5)[-1].soc_kwh * 1.0
        best = -math.inf
        for seq in itertools.product(range(-2, 3), repeat=5):  # ±0.5 kWh per step max
            e, cash, ok = 0.5, 0.0, True
            for t, j in enumerate(seq):
                d = j * 0.25
                if d > 0.5 * 0.95 + 1e-9 or -d * 0.9 > 0.5 + 1e-9 or not 0 <= e + d <= 1:
                    ok = False
                    break
                cash += -buy[t] * d / 0.95 if d > 0 else sell[t] * (-d) * 0.9
                e += d
            if ok:
                best = max(best, cash + e)
        assert dp == pytest.approx(best)


@pytest.mark.parametrize(
    "kw",
    [
        {"capacity_kwh": 0, "charge_kw": 1},
        {"capacity_kwh": 1, "charge_kw": 0},
        {"capacity_kwh": 1, "charge_kw": 1, "charge_eff": 1.2},
        {"capacity_kwh": 1, "charge_kw": 1, "soc_min_pct": 50, "soc_max_pct": 40},
        {"capacity_kwh": 1, "charge_kw": 1, "soc_step_kwh": 0.9},
    ],
)
def test_battery_validation(kw):
    with pytest.raises(BatteryError):
        Battery(**kw)


# --- SoC grid tied to power: exact full power, monotone in capacity, close to the LP optimum


def _prices(n=192, seed=5):
    rng = np.random.default_rng(seed)
    x = np.arange(n)
    spot = 2500 + 2000 * np.sin(x / 96 * 2 * np.pi) + rng.normal(0, 700, n)
    buy = (spot + 400) / 1000 * 1.21 + np.where((x % 96) < 24, 0.5, 2.4)
    return buy, (spot - 300) / 1000


def _profit(b, buy, sell, e0, terminal=None):
    steps = solve_rollout(b, buy, sell, terminal or Terminal("zero"), e0)
    return sum(s.cash_czk - s.wear_czk for s in steps), steps


@pytest.mark.parametrize(
    ("cap", "pc", "pd", "ce", "de"),
    [(1, 5, 5, 0.95, 0.95), (10, 5, 5, 0.9487, 0.9487), (175, 5, 5, 0.9487, 0.9487),
     (30, 3, 4.5, 0.97, 0.92), (13.5, 11, 11, 0.96, 0.96), (500, 2, 6, 1.0, 1.0)],
)  # fmt: skip
def test_auto_step_full_power_exactly_representable(cap, pc, pd, ce, de):
    b = Battery(cap, pc, discharge_kw=pd, charge_eff=ce, discharge_eff=de)
    g = b.step
    assert g <= cap / 400 + 1e-12
    up = b.charge_q_kwh / g
    assert abs(up - round(up)) < 1e-9 and round(up) >= 9  # full charge = whole steps
    down = b.discharge_q_kwh / g
    # discharge: exact when possible (symmetric efficiencies), else < 0.5 % of its power lost
    assert (down - math.floor(down + 1e-9)) / down <= 0.005

    levels, _, deltas = _grid(b)
    assert levels[0] == b.e_min and levels[-1] <= b.e_max + 1e-9
    assert deltas.max() * g / ce / DT_H == pytest.approx(pc)  # 5 kW stays 5 kW (was 3.7)


@pytest.mark.parametrize("cap", [2, 10, 25, 175])
def test_default_efficiency_both_directions_exact(cap):
    b = Battery(cap, 5)  # round trip 90 %: discharge/charge quantum = 10/9 → K divisible by 9
    for q in (b.charge_q_kwh, b.discharge_q_kwh):
        assert abs(q / b.step - round(q / b.step)) < 1e-6


def test_step_depends_on_power_only_for_large_batteries():
    steps = {Battery(c, 5).step for c in (60, 100, 175, 1000)}
    assert len(steps) == 1


def test_foresight_profit_never_decreases_with_capacity():
    buy, sell = _prices()
    prev = -math.inf
    for cap in np.arange(1.0, 60.5, 0.5):  # both regimes: capacity- and power-limited step
        b = Battery(float(cap), 5, soc_min_pct=10)
        p, _ = _profit(b, buy, sell, b.e_min)
        assert p >= prev - 1e-9, cap
        prev = p
    for tv in (0.0, 1.5):  # also with stored energy valued at the end
        prev = -math.inf
        for cap in (5, 20, 40, 80, 120):
            b = Battery(cap, 5, soc_min_pct=10)
            p, steps = _profit(b, buy, sell, b.e_min, Terminal("value", tv))
            p += (steps[-1].soc_kwh - b.e_min) * tv
            assert p >= prev - 1e-9
            prev = p


def _lp_profit(b, buy, sell, e0):
    linprog = pytest.importorskip("scipy.optimize").linprog
    n = len(buy)
    obj = np.concatenate([buy, -(sell - b.cycle_cost_czk_kwh)])
    tri = np.tril(np.ones((n, n)))
    a = np.hstack([tri * b.charge_eff, -tri / b.discharge_eff])
    res = linprog(
        obj,
        A_ub=np.vstack([a, -a]),
        b_ub=np.concatenate([np.full(n, b.e_max - e0), np.full(n, e0 - b.e_min)]),
        bounds=[(0, b.charge_kw * DT_H)] * n + [(0, b.p_dis * DT_H)] * n,
        method="highs",
    )
    assert res.status == 0
    return -res.fun


@pytest.mark.parametrize("cap", [1, 3, 10, 25, 60])
def test_dp_matches_continuous_lp_optimum(cap):
    """LP = continuous relaxation (any power, simultaneous charge/discharge allowed) = an upper
    bound; the DP on the power-tied grid must be within 0.5 % of it."""
    buy, sell = _prices(n=96 * 3, seed=11)
    b = Battery(cap, 5, soc_min_pct=10, cycle_cost_czk_kwh=0.1)
    lp = _lp_profit(b, buy, sell, b.e_min)
    dp, _ = _profit(b, buy, sell, b.e_min)
    assert dp <= lp + 1e-6
    assert dp >= lp * 0.995


def test_checkpointed_rollout_is_bit_identical():
    buy, sell = _prices(n=500, seed=2)
    b = Battery(40, 5, soc_min_pct=10)
    term = Terminal("value", 1.2)
    direct = rollout(solve(b, buy, sell, term), 4.0)
    for limit in (len(_grid(b)[0]) * 7, len(_grid(b)[0]) * 64):  # many / few segments
        assert solve_rollout(b, buy, sell, term, 4.0, max_entries=limit) == direct
