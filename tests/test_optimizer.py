import itertools
import math

import numpy as np
import pytest

from bess_ote.battery import Battery, BatteryError
from bess_ote.optimizer import Terminal, rollout, solve


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
