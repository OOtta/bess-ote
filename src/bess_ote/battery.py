"""Battery (storage) parameters."""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache

DT_H = 0.25  # decision interval: one quarter-hour


class BatteryError(ValueError):
    """Inconsistent battery parameters."""


@dataclass(frozen=True, slots=True)
class Battery:
    """Usable storage with AC-side power limits and one-way efficiencies.

    * ``capacity_kwh`` – usable energy capacity (100 % SoC).
    * ``charge_kw`` / ``discharge_kw`` – max power on the grid (AC) side.
    * ``charge_eff`` / ``discharge_eff`` – grid→storage and storage→grid efficiency
      (round trip = product).
    * ``soc_min_pct`` / ``soc_max_pct`` – allowed state of charge.
    * ``cycle_cost_czk_kwh`` – wear cost per kWh delivered to the grid (default 0).
    * ``soc_step_kwh`` – resolution of the optimiser's SoC grid (default: see :func:`auto_step`).
    """

    capacity_kwh: float
    charge_kw: float
    discharge_kw: float | None = None
    charge_eff: float = math.sqrt(0.9)
    discharge_eff: float = math.sqrt(0.9)
    soc_min_pct: float = 0.0
    soc_max_pct: float = 100.0
    cycle_cost_czk_kwh: float = 0.0
    soc_step_kwh: float | None = None

    def __post_init__(self) -> None:
        if self.capacity_kwh <= 0:
            raise BatteryError("capacity must be > 0 kWh")
        if self.charge_kw <= 0 or (self.discharge_kw is not None and self.discharge_kw <= 0):
            raise BatteryError("charge/discharge power must be > 0 kW")
        for name in ("charge_eff", "discharge_eff"):
            v = getattr(self, name)
            if not 0 < v <= 1:
                raise BatteryError(f"{name.replace('_', ' ')} must be in (0, 1]")
        if not 0 <= self.soc_min_pct < self.soc_max_pct <= 100:
            raise BatteryError("need 0 ≤ min SoC < max SoC ≤ 100 %")
        if self.cycle_cost_czk_kwh < 0:
            raise BatteryError("cycle cost must be ≥ 0")
        if self.soc_step_kwh is not None and not 0 < self.soc_step_kwh <= self.capacity_kwh / 4:
            raise BatteryError("SoC step must be > 0 and ≤ capacity / 4")

    @property
    def p_dis(self) -> float:
        return self.charge_kw if self.discharge_kw is None else self.discharge_kw

    @property
    def round_trip(self) -> float:
        return self.charge_eff * self.discharge_eff

    @property
    def charge_q_kwh(self) -> float:
        """Stored energy gained in one quarter-hour at full charging power."""
        return self.charge_kw * DT_H * self.charge_eff

    @property
    def discharge_q_kwh(self) -> float:
        """Stored energy spent in one quarter-hour at full discharging power."""
        return self.p_dis * DT_H / self.discharge_eff

    @property
    def step(self) -> float:
        if self.soc_step_kwh:
            return self.soc_step_kwh
        return auto_step(self.capacity_kwh, self.charge_q_kwh, self.discharge_q_kwh)

    @property
    def e_min(self) -> float:
        return self.capacity_kwh * self.soc_min_pct / 100

    @property
    def e_max(self) -> float:
        return self.capacity_kwh * self.soc_max_pct / 100

    @property
    def usable_kwh(self) -> float:
        return self.e_max - self.e_min

    def pct(self, kwh: float) -> float:
        return kwh / self.capacity_kwh * 100

    def describe(self) -> str:
        pd = f"/{self.p_dis:g}" if self.p_dis != self.charge_kw else ""
        text = (
            f"{self.capacity_kwh:g} kWh, {self.charge_kw:g}{pd} kW, round trip "
            f"{self.round_trip * 100:.1f} % (charge {self.charge_eff * 100:.1f} %, discharge "
            f"{self.discharge_eff * 100:.1f} %), SoC {self.soc_min_pct:g}–{self.soc_max_pct:g} %"
        )
        if self.cycle_cost_czk_kwh:
            text += f", wear {self.cycle_cost_czk_kwh:g} CZK/kWh discharged"
        return text


#: full-power quarter-hour = at least this many grid steps. 9 makes both directions exact for
#: the default round trip of 90 % (discharge quantum = 10/9 × charge quantum); a finer
#: minimum gave the same profit in tests against the LP optimum, only slower (work ∝ K²)
MIN_STEPS_PER_QUARTER = 9
#: … and the step never exceeds capacity / this (fine SoC resolution for small batteries)
CAPACITY_DIVISOR = 400
#: accepted loss of full discharge power from rounding it down to whole steps
MAX_DISCHARGE_LOSS = 0.005


@lru_cache(maxsize=256)
def base_steps(charge_q: float, discharge_q: float) -> int:
    """Steps per full-power charging quarter for a power-limited grid (depends on power and
    efficiency only): the first K ≥ :data:`MIN_STEPS_PER_QUARTER` with full-power
    discharging also a whole number of steps, else the first losing ≤ 0.5 % of its power."""
    ks = range(MIN_STEPS_PER_QUARTER, 8 * MIN_STEPS_PER_QUARTER + 1)
    ratio = discharge_q / charge_q

    def loss(k: int) -> float:
        r = ratio * k
        return (r - math.floor(r + 1e-9)) / r if r >= 1 else 1.0

    for k in ks:
        if loss(k) <= 1e-6:
            return k
    for k in ks:
        if loss(k) <= MAX_DISCHARGE_LOSS:
            return k
    return min(ks, key=loss)


@lru_cache(maxsize=256)
def auto_step(capacity_kwh: float, charge_q: float, discharge_q: float) -> float:
    """Default SoC grid step: a fraction of the stored energy of one full-power quarter-hour,
    or capacity / 400, whichever is lower.

    ``step = charge_q / (K × 2^j)`` with K = :func:`base_steps` and the smallest j that keeps
    the step ≤ capacity / 400. Full-power charging is a whole number of steps (never rounded
    down; previously the step was capacity / 400 and 5 kW became 3.7 kW at 175 kWh), and so is
    discharging for the default efficiencies. The grids are nested: a bigger battery's step
    divides a smaller one's, so every schedule of a smaller battery is also available to a
    bigger one and the optimum cannot fall with capacity.
    """
    k = base_steps(charge_q, discharge_q)
    m = 1
    while charge_q / (k * m) > capacity_kwh / CAPACITY_DIVISOR * (1 + 1e-12):
        m *= 2
    return charge_q / (k * m)
