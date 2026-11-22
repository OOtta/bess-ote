"""Purchase (nákup) and sale (výkup) prices derived from the OTE spot price.

All prices handled here are per kWh in CZK. Nothing is assumed: every coefficient defaults to
a neutral value (multiplier 1, surcharge 0, distribution 0, VAT 0) and the effective formula is
printed with every result.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from bess_ote.lowtariff import LowTariffSchedule

NegativeMode = Literal["as-is", "clamp-spot", "clamp-total"]


@dataclass(frozen=True, slots=True)
class SpotFormula:
    """Energy price per MWh = spot × multiplier + add_czk_mwh (same semantics as egd-api-client).

    For a **negative spot price** (strictly below 0) ``negative_multiplier`` /
    ``negative_add_czk_mwh`` apply; each ``None`` falls back to the normal coefficient.
    ``negative``: ``as-is`` (formula unchanged), ``clamp-spot`` (negative spot replaced by 0
    before the coefficients) or ``clamp-total`` (result floored at 0).
    """

    multiplier: float = 1.0
    add_czk_mwh: float = 0.0
    negative: NegativeMode = "as-is"
    negative_multiplier: float | None = None
    negative_add_czk_mwh: float | None = None

    def coefficients(self, spot_czk_mwh: float) -> tuple[float, float]:
        if spot_czk_mwh < 0:
            m = self.multiplier if self.negative_multiplier is None else self.negative_multiplier
            a = self.add_czk_mwh if self.negative_add_czk_mwh is None else self.negative_add_czk_mwh
            return m, a
        return self.multiplier, self.add_czk_mwh

    def czk_mwh(self, spot_czk_mwh: float) -> float:
        mult, add = self.coefficients(spot_czk_mwh)
        spot = 0.0 if self.negative == "clamp-spot" and spot_czk_mwh < 0 else spot_czk_mwh
        price = spot * mult + add
        if self.negative == "clamp-total" and price < 0:
            price = 0.0
        return price

    @staticmethod
    def _formula(mult: float, add: float) -> str:
        text = "spot" if mult == 1.0 else f"spot × {mult:g}"
        if add:
            text += f" {'+' if add >= 0 else '−'} {abs(add):g} CZK/MWh"
        return text

    def describe(self) -> str:
        text = self._formula(self.multiplier, self.add_czk_mwh)
        if self.negative_multiplier is not None or self.negative_add_czk_mwh is not None:
            text += f" (negative spot: {self._formula(*self.coefficients(-1.0))})"
        if self.negative != "as-is":
            text += f", negative prices: {self.negative}"
        return text


@dataclass(frozen=True, slots=True)
class Distribution:
    """Per-kWh charges paid on **purchased** energy only.

    ``high_czk_kwh`` (VT) applies outside and ``low_czk_kwh`` (NT) inside the low-tariff
    windows of ``schedule``; without a schedule the VT rate applies all the time.
    ``other_czk_kwh`` = further per-kWh items you pay on purchase (e.g. system services, POZE,
    electricity tax) – excl. VAT, like the distribution rates.
    """

    high_czk_kwh: float = 0.0
    low_czk_kwh: float | None = None
    schedule: LowTariffSchedule | None = None
    other_czk_kwh: float = 0.0

    def czk_kwh(self, start_utc: datetime, length: timedelta) -> float:
        rate = self.high_czk_kwh
        if self.schedule is not None and self.low_czk_kwh is not None:
            f = self.schedule.low_fraction(start_utc, length)
            rate = (1 - f) * self.high_czk_kwh + f * self.low_czk_kwh
        return rate + self.other_czk_kwh

    def describe(self) -> str:
        if not (self.high_czk_kwh or self.low_czk_kwh or self.other_czk_kwh):
            return "none"
        if self.schedule is not None and self.low_czk_kwh is not None:
            text = (
                f"VT {self.high_czk_kwh:g} / NT {self.low_czk_kwh:g} CZK/kWh "
                f"({self.schedule.describe()})"
            )
        else:
            text = f"{self.high_czk_kwh:g} CZK/kWh"
        if self.other_czk_kwh:
            text += f", other {self.other_czk_kwh:g} CZK/kWh"
        return text


@dataclass(frozen=True, slots=True)
class Tariffs:
    """Everything that turns a spot price into what one kWh costs / earns."""

    buy: SpotFormula = SpotFormula()
    sell: SpotFormula = SpotFormula()
    distribution: Distribution = Distribution()
    buy_vat_percent: float = 0.0
    sell_vat_percent: float = 0.0

    def buy_czk_kwh(self, spot_czk_mwh: float, start_utc: datetime, length: timedelta) -> float:
        """Price of 1 kWh taken from the grid: (energy + distribution + other) × (1 + VAT)."""
        net = self.buy.czk_mwh(spot_czk_mwh) / 1000 + self.distribution.czk_kwh(start_utc, length)
        return net * (1 + self.buy_vat_percent / 100)

    def sell_czk_kwh(self, spot_czk_mwh: float) -> float:
        """Revenue for 1 kWh fed into the grid (× (1 + sale VAT) for VAT payers)."""
        return self.sell.czk_mwh(spot_czk_mwh) / 1000 * (1 + self.sell_vat_percent / 100)

    def describe(self) -> dict[str, str]:
        vat_b = f", VAT {self.buy_vat_percent:g} %" if self.buy_vat_percent else ", excl. VAT"
        vat_s = f", VAT {self.sell_vat_percent:g} %" if self.sell_vat_percent else ", excl. VAT"
        return {
            "buy": self.buy.describe() + vat_b,
            "sell": self.sell.describe() + vat_s,
            "distribution": self.distribution.describe() + (" (on purchase only)"),
        }
