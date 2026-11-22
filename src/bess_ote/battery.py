"""Battery (storage) parameters."""

from __future__ import annotations

import math
from dataclasses import dataclass

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
    * ``soc_step_kwh`` – resolution of the optimiser's SoC grid (default capacity / 400).
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
    def step(self) -> float:
        return self.soc_step_kwh or self.capacity_kwh / 400

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
