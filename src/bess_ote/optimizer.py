"""Optimal charge / discharge / wait schedule for known prices.

Dynamic programming over a fine grid of stored energy (exact on that grid):

* state  = stored energy ``E`` (kWh) on a grid ``k × step`` within [min SoC, max SoC];
* action = change of stored energy ``ΔE = j × step`` in one quarter-hour, limited by the
  AC power: ``ΔE ≤ P_charge × 0.25 h × η_charge`` and ``−ΔE × η_discharge ≤ P_discharge × 0.25 h``;
  one signed action per interval, so charging and discharging at the same time is impossible;
* cost   = ``buy × ΔE / η_charge`` when charging, ``−(sell − wear) × |ΔE| × η_discharge``
  when discharging (all in CZK/kWh on the grid side);
* terminal value of the energy left at the end of the horizon (see :class:`Terminal`).

With a PV plant (``pv`` = forecast kWh per quarter-hour) the action stays the change of stored
energy; for each action the PV energy is used in the most profitable way (see
:func:`pv_flows`): it is free, so it charges the battery first, the rest is sold when the sale
price is positive and curtailed otherwise; a shared inverter AC limit
(:attr:`Battery.inverter_kw`) caps grid import and export. Without PV and without an inverter
limit the original cost formula is used unchanged (bit-identical results).

The backward recursion ``V_t(E) = min_ΔE cost_t(ΔE) + V_{t+1}(E + ΔE)`` only uses intervals
``t … end``; therefore the decision at interval ``t`` is identical whether the problem is
solved from ``t`` or from any earlier start with the same horizon end. The backtest relies on
this to reuse one solution per information update while giving exactly the decisions a
fresh ``plan`` at that moment would give (asserted by the tests).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Literal

import numpy as np

from bess_ote.battery import DT_H, Battery

TerminalMode = Literal["value", "zero", "min-soc"]
_BIG = 1e12  # infeasible
_SHORTFALL_PENALTY = 1e6  # CZK per kWh missing at the end in min-soc mode


@dataclass(frozen=True, slots=True)
class Terminal:
    """How energy left in the battery at the horizon end is valued.

    * ``value``   – each stored kWh is worth ``czk_per_kwh_stored`` (typically the average
      sale price of the last known day × discharge efficiency − wear);
    * ``zero``    – leftover energy is worth nothing (the battery tends to empty itself);
    * ``min-soc`` – the end SoC must be at least ``min_kwh`` (penalty otherwise).
    """

    mode: TerminalMode = "value"
    czk_per_kwh_stored: float = 0.0
    min_kwh: float = 0.0


@dataclass
class Solution:
    """Backward DP result for intervals ``0 … n-1`` of a horizon."""

    battery: Battery
    levels: np.ndarray  # kWh of each state
    deltas: np.ndarray  # stored-energy change (grid steps) of each action
    choice: np.ndarray  # (n, S) best action index per interval and state
    value0: np.ndarray  # (S,) cost-to-go from interval 0 (negative = profit)
    buy: np.ndarray
    sell: np.ndarray
    k0: int = 0  # grid index of levels[0]
    meta: dict = field(default_factory=dict)
    pv: np.ndarray | None = None  # PV kWh per interval the decisions were based on

    @property
    def n(self) -> int:
        return len(self.choice)

    def state_index(self, kwh: float) -> int:
        """Nearest grid state for a stored energy (clamped to the allowed SoC range)."""
        k = round((kwh - float(self.levels[0])) / self.battery.step)
        return int(min(max(k, 0), len(self.levels) - 1))


@dataclass(frozen=True, slots=True)
class Step:
    """One executed quarter-hour."""

    index: int
    delta_kwh: float  # stored energy change
    grid_kwh: float  # + bought from the grid, − sold to the grid
    cash_czk: float  # − cost of purchase, + sale revenue (wear excluded)
    wear_czk: float
    soc_kwh: float  # after the interval
    pv_kwh: float = 0.0  # PV energy available (settled: actual)
    pv_battery_kwh: float = 0.0  # PV → battery (before charging losses)
    pv_grid_kwh: float = 0.0  # PV sold to the grid (part of −grid_kwh)
    pv_curtailed_kwh: float = 0.0

    @property
    def action(self) -> str:
        return "charge" if self.delta_kwh > 0 else "discharge" if self.delta_kwh < 0 else "wait"

    @property
    def power_kw(self) -> float:
        return abs(self.grid_kwh) / DT_H


def _grid(b: Battery) -> tuple[np.ndarray, int, np.ndarray]:
    g = b.step
    # anchored at min SoC: min SoC exact, usable steps = floor(usable / g) (monotone in size)
    n = math.floor(b.usable_kwh / g + 1e-9)
    levels = b.e_min + np.arange(n + 1) * g
    k_min = 0
    up = math.floor(b.charge_q_kwh / g + 1e-9)
    down = math.floor(b.discharge_q_kwh / g + 1e-9)
    order = sorted(range(-down, up + 1), key=lambda j: (abs(j), -j))  # prefer small actions
    return levels, k_min, np.array(order, dtype=np.int64)


def pv_flows(b: Battery, e, pv, buy, sell):  # type: ignore[no-untyped-def]
    """Energy flows of a quarter-hour with stored-energy change ``e`` (kWh) and ``pv`` kWh of PV
    (numpy arrays or scalars, broadcast). Returns ``(grid_in, out, pv_battery, pv_grid,
    curtailed)``: grid → battery (AC), battery → grid, PV → battery, PV → grid, PV curtailed.

    PV energy costs nothing, so it covers charging first – unless buying from the grid pays
    (buy < 0), then the grid goes first up to the inverter limit. PV left over is sold when
    the sale price is positive, within the inverter limit left after the battery discharge,
    otherwise curtailed. Feasibility (``grid_in``/``out`` ≤ limit) is checked by the caller.
    """
    lq = b.inverter_q_kwh
    need = np.where(e > 0, e / b.charge_eff, 0.0)
    out = np.where(e < 0, -e * b.discharge_eff, 0.0)
    with np.errstate(invalid="ignore"):
        prefer_grid = buy < 0
        sellable = sell > 0
    want = np.where(prefer_grid, np.maximum(need - lq, 0.0), need)
    pv_batt = np.minimum(pv, want)
    grid_in = need - pv_batt
    rest = pv - pv_batt
    pv_grid = np.where(sellable, np.minimum(rest, np.maximum(lq - out, 0.0)), 0.0)
    return grid_in, out, pv_batt, pv_grid, rest - pv_grid


def _pv_costs(b: Battery, buy: np.ndarray, sell: np.ndarray, deltas: np.ndarray,
              pv: np.ndarray) -> np.ndarray:  # fmt: skip
    e = (deltas * b.step)[None, :]
    bu, se = buy[:, None], sell[:, None]
    gi, out, _, pg, _ = pv_flows(b, e, pv[:, None], bu, se)
    lq = b.inverter_q_kwh + 1e-9
    with np.errstate(invalid="ignore"):
        cost = bu * gi - (se - b.cycle_cost_czk_kwh) * out - se * pg
    wait = deltas == 0
    cost[:, wait] = np.where(np.isnan(cost[:, wait]), 0.0, cost[:, wait])
    return np.where(np.isnan(cost) | (gi > lq) | (out > lq), _BIG, cost)


def action_costs(b: Battery, buy: np.ndarray, sell: np.ndarray, deltas: np.ndarray,
                 pv: np.ndarray | None = None) -> np.ndarray:  # fmt: skip
    """(n, A) cost of each action; NaN prices (no market price) allow only waiting."""
    if pv is not None or b.inverter_kw is not None:
        pv_arr = np.zeros(len(buy)) if pv is None else np.asarray(pv, dtype=np.float64)
        return _pv_costs(b, buy, sell, deltas, pv_arr)
    e = deltas * b.step
    charge = np.where(e > 0, e / b.charge_eff, 0.0)
    out = np.where(e < 0, -e * b.discharge_eff, 0.0)
    with np.errstate(invalid="ignore"):
        cost = (
            buy[:, None] * charge[None, :] - (sell[:, None] - b.cycle_cost_czk_kwh) * out[None, :]
        )
    cost[:, deltas == 0] = 0.0
    return np.where(np.isnan(cost), _BIG, cost)


def _setup(b: Battery, buy: np.ndarray, sell: np.ndarray, terminal: Terminal,
           pv: np.ndarray | None = None):  # fmt: skip
    buy = np.asarray(buy, dtype=np.float64)
    sell = np.asarray(sell, dtype=np.float64)
    levels, k0, deltas = _grid(b)
    s = len(levels)
    if terminal.mode == "value":
        v = -levels * terminal.czk_per_kwh_stored
    elif terminal.mode == "min-soc":
        v = np.maximum(0.0, terminal.min_kwh - levels - 1e-9) * _SHORTFALL_PENALTY
    else:
        v = np.zeros(s)
    target = np.arange(s)[:, None] + deltas[None, :]  # transition table (state, action)
    valid = (target >= 0) & (target < s)
    target = np.where(valid, target, s)  # index s = padded infeasible state
    costs = action_costs(b, buy, sell, deltas, pv)
    return buy, sell, levels, k0, deltas, v, target, costs


def _choice_dtype(actions: int) -> type:
    return np.int8 if actions <= 127 else np.int16 if actions < 32000 else np.int32


def _backward(
    v: np.ndarray,
    target: np.ndarray,
    costs: np.ndarray,
    t_hi: int,
    t_lo: int,
    store: np.ndarray | None = None,
    store_from: int = 0,
    tick: Callable[[int], None] | None = None,
    checkpoints: dict[int, np.ndarray] | None = None,
    every: int = 0,
) -> np.ndarray:
    """``V_t`` for t = t_hi−1 … t_lo from ``v`` = ``V_{t_hi}``; decisions of t ≥ store_from
    go to ``store[t − store_from]``. Fixed float64 operation order → bit-reproducible."""
    s, a = target.shape
    padded = np.empty(s + 1, dtype=np.float64)  # preallocated work buffers
    padded[s] = _BIG
    total = np.empty((s, a), dtype=np.float64)
    flat = total.reshape(-1)
    row_base = np.arange(s) * a
    store_to = store_from + (len(store) if store is not None else 0)
    for t in range(t_hi - 1, t_lo - 1, -1):
        if checkpoints is not None and (t + 1) % every == 0 and t + 1 != t_hi:
            checkpoints[t + 1] = v  # V_{t+1}: start of a recomputable segment
        padded[:s] = v
        np.take(padded, target, out=total)
        total += costs[t]
        best = total.argmin(axis=1)  # first minimum = smallest action on ties
        if store_from <= t < store_to:
            store[t - store_from] = best
        v = flat.take(row_base + best)
        if tick is not None and t % 512 == 0:
            tick(t)
    return v


def solve(
    b: Battery,
    buy: np.ndarray,
    sell: np.ndarray,
    terminal: Terminal,
    progress: Callable[[float], None] | None = None,
    keep: int | None = None,
    pv: np.ndarray | None = None,
) -> Solution:
    """Backward DP over the whole horizon (``buy``/``sell`` in CZK/kWh per quarter-hour).

    ``keep`` stores the decisions of the first ``keep`` intervals only (the backtest executes
    just those); the recursion itself always runs over the whole horizon. All arithmetic is
    float64 and in a fixed order, so the result does not depend on the thread / process it
    runs in.
    """
    buy, sell, levels, k0, deltas, v, target, costs = _setup(b, buy, sell, terminal, pv)
    n = len(buy)
    keep = n if keep is None else max(0, min(keep, n))
    choice = np.empty((keep, len(levels)), dtype=_choice_dtype(len(deltas)))
    tick = None if progress is None else (lambda t: progress((n - t) / n))
    v = _backward(v, target, costs, n, 0, choice, 0, tick)
    pv_arr = None if pv is None else np.asarray(pv, dtype=np.float64)
    return Solution(b, levels, deltas, choice, v, buy, sell, k0, pv=pv_arr)


#: decision-table entries kept in memory at once (int8 → bytes); above this the
#: perfect-foresight rollout recomputes segments from stored value-function checkpoints
MAX_TABLE_ENTRIES = 64_000_000


def solve_rollout(
    b: Battery,
    buy: np.ndarray,
    sell: np.ndarray,
    terminal: Terminal,
    soc_kwh: float,
    progress: Callable[[float], None] | None = None,
    max_entries: int | None = None,
    pv: np.ndarray | None = None,
) -> list[Step]:
    """``rollout(solve(...), soc_kwh)`` with bounded memory – bit-identical results.

    Long horizons on big batteries would need a huge decision table (14 700 quarters ×
    3 600 states for 5 months of a 175 kWh battery). Instead the value function is saved
    every ``C`` intervals during the backward pass, and the forward pass recomputes each
    segment's decisions from its checkpoint (same operations, same numbers): ≈ 2× the DP
    work, a fraction of the memory.
    """
    limit = MAX_TABLE_ENTRIES if max_entries is None else max_entries
    buy, sell, levels, k0, deltas, v_end, target, costs = _setup(b, buy, sell, terminal, pv)
    n, s = len(buy), len(levels)
    if n * s <= limit:
        return rollout(solve(b, buy, sell, terminal, progress, pv=pv), soc_kwh)
    pv_arr = None if pv is None else np.asarray(pv, dtype=np.float64)
    seg = max(1, min(n, limit // (2 * s)))
    checkpoints: dict[int, np.ndarray] = {n: v_end}
    tick1 = None if progress is None else (lambda t: progress((n - t) / n / 2))
    _backward(v_end, target, costs, n, 0, tick=tick1, checkpoints=checkpoints, every=seg)
    sol = Solution(b, levels, deltas, np.empty((0, s), np.int8), np.empty(0), buy, sell, k0)
    state = sol.state_index(soc_kwh)
    bounds = sorted(checkpoints)  # segment ends: seg, 2·seg, …, n
    steps: list[Step] = []
    lo = 0
    table = np.empty((seg, s), dtype=_choice_dtype(len(deltas)))
    for hi in bounds:
        _backward(checkpoints[hi], target, costs, hi, lo, table, lo)
        part = Solution(b, levels, deltas, table[: hi - lo], np.empty(0), buy[lo:hi],
                        sell[lo:hi], k0,
                        pv=None if pv_arr is None else pv_arr[lo:hi])  # fmt: skip
        for t in range(hi - lo):
            st, state = step_at(part, t, state)
            steps.append(replace(st, index=lo + t))
        if progress is not None:
            progress(0.5 + hi / n / 2)
        lo = hi
    return steps


def solve_choice(
    b: Battery,
    buy: np.ndarray,
    sell: np.ndarray,
    terminal: Terminal,
    keep: int | None,
    pv: np.ndarray | None = None,
) -> np.ndarray:
    """Worker entry point (picklable): only the decision table of the first ``keep`` rows."""
    return solve(b, buy, sell, terminal, keep=keep, pv=pv).choice


def step_at(sol: Solution, t: int, state: int,
            pv_actual: float | None = None) -> tuple[Step, int]:  # fmt: skip
    """Execute interval ``t`` from grid state ``state``; returns the step and the next state.

    ``pv_actual`` settles the quarter on the actual PV production instead of the one the
    decision was based on: the planned battery action is kept (the grid covers a PV shortfall,
    extra PV is sold or curtailed as in :func:`pv_flows`); charging is reduced only when the
    shortfall would exceed the inverter limit."""
    b = sol.battery
    a = int(sol.choice[t, state])
    j = int(sol.deltas[a])
    if sol.pv is not None or pv_actual is not None or b.inverter_kw is not None:
        return _settle(sol, t, state, j, pv_actual)
    nxt = state + j
    delta = j * b.step
    if delta > 0:
        grid = delta / b.charge_eff
        cash = -sol.buy[t] * grid
        wear = 0.0
    elif delta < 0:
        out = -delta * b.discharge_eff
        grid = -out
        cash = sol.sell[t] * out
        wear = b.cycle_cost_czk_kwh * out
    else:
        grid = cash = wear = 0.0
    return Step(t, delta, grid, float(cash), float(wear), float(sol.levels[nxt])), nxt


#: how a quarter is settled on the actual PV when it differs from the forecast:
#: ``follow`` – grid import is capped at the planned import, so a PV shortfall charges less
#: (like a hybrid inverter charging from surplus), and PV that cannot be sold at a positive
#: price charges the battery for free when there is room; ``setpoint`` – the planned battery
#: power is kept and the grid covers any shortfall
SETTLEMENT = "follow"


def _settle(sol: Solution, t: int, state: int, j: int,
            pv_actual: float | None) -> tuple[Step, int]:  # fmt: skip
    b = sol.battery
    if pv_actual is not None:
        pv = float(pv_actual)
    else:
        pv = float(sol.pv[t]) if sol.pv is not None else 0.0
    buy, sell = float(sol.buy[t]), float(sol.sell[t])
    lq = b.inverter_q_kwh + 1e-9
    if pv_actual is not None and sol.pv is not None and SETTLEMENT == "follow" and j >= 0:
        gi_plan = float(pv_flows(b, j * b.step, float(sol.pv[t]), buy, sell)[0])
        cap = math.floor((pv + gi_plan) * b.charge_eff / b.step + 1e-9)
        j = min(j, cap)
        if not sell > 0:  # unsellable PV: store it for free
            j = max(j, min(cap, int(sol.deltas.max()), len(sol.levels) - 1 - state))
    while True:
        gi, out, pb, pg, cu = (float(x) for x in pv_flows(b, j * b.step, pv, buy, sell))
        if j <= 0 or gi <= lq:
            break
        j -= 1  # PV shortfall beyond the inverter limit: charge less
    nxt = state + j
    cash = sell * (out + pg) - buy * gi
    wear = b.cycle_cost_czk_kwh * out
    return Step(t, j * b.step, gi - out - pg, cash, wear, float(sol.levels[nxt]), pv, pb, pg,
                cu), nxt  # fmt: skip


def rollout(sol: Solution, soc_kwh: float, start: int = 0) -> list[Step]:
    """Follow the optimal policy from interval ``start`` with the given stored energy."""
    state = sol.state_index(soc_kwh)
    steps = []
    for t in range(start, sol.n):
        st, state = step_at(sol, t, state)
        steps.append(st)
    return steps
