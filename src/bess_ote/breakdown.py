"""Hour / quarter-hour breakdown of a schedule (backtest and plan).

Rows are grouped by the local clock: an hour is the four quarter-hours of one UTC hour
(Prague offsets are whole hours), so on the day the clocks go back the repeated 02:00 hour
appears twice (02:00 CEST and 02:00 CET – 25 rows, 100 quarter-hours) and on the day they go
forward there are 23 rows (92 quarter-hours). Energy and money are sums; prices are simple
averages of the quarter-hours with a market price (until 2025-09-30 OTE prices are hourly, so
an hour's four quarter-hours carry the same price and the average equals the hourly price;
from 2025-10-01 it is the mean of the four 15-minute prices – the same way OTE derives its
hourly index). Quarter-hours without a price (skipped in the backtest) are kept as idle rows
so the monthly-fee shares – and therefore the totals – match the summary.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from bess_ote.battery import DT_H, Battery
from bess_ote.optimizer import Step
from bess_ote.timeutil import QUARTER, to_local

FINE = ("hour", "quarter-hour")
ALIASES = {
    "month": "month", "m": "month",
    "day": "day", "d": "day",
    "hour": "hour", "h": "hour", "60min": "hour", "hourly": "hour",
    "quarter-hour": "quarter-hour", "quarter": "quarter-hour", "qh": "quarter-hour",
    "15min": "quarter-hour", "15m": "quarter-hour",
    "none": "none",
}  # fmt: skip
#: decimals of energy / money in fine rows – sums of thousands of rows stay exact to 0.01
ND = 6
LETTER = {"charge": "C", "discharge": "D", "wait": "-", None: "."}


def normalise(by: str) -> str:
    """Canonical breakdown name for an option value or alias (ValueError if unknown)."""
    key = by.strip().lower()
    if key not in ALIASES:
        raise ValueError(by)
    return ALIASES[key]


@dataclass(frozen=True, slots=True)
class Quarter:
    """One quarter-hour of a schedule; ``step`` None = no price (battery idle)."""

    start: datetime  # UTC
    spot_czk_mwh: float
    buy_czk_kwh: float
    sell_czk_kwh: float
    step: Step | None
    soc_before_kwh: float
    fee_czk: float = 0.0
    #: distribution + other per-kWh items incl. purchase VAT (part of ``buy_czk_kwh``)
    dist_czk_kwh: float = math.nan
    #: share of the quarter in NT; None = no VT/NT schedule
    low_tariff: float | None = None


def _mean(values: list[float]) -> float | None:
    vals = [v for v in values if not math.isnan(v)]
    return sum(vals) / len(vals) if vals else None


def _round(v: float | None, nd: int) -> float | None:
    return None if v is None else round(v, nd)


def _tariff_label(qs: list[Quarter]) -> str:
    fr = [q.low_tariff for q in qs if q.low_tariff is not None]
    if not fr:
        return ""
    if all(f >= 1 for f in fr):
        return "NT"
    if all(f <= 0 for f in fr):
        return "VT"
    return "VT/NT"


def fine_rows(quarters: Iterable[Quarter], battery: Battery, by: str) -> list[dict]:
    """Rows per local hour or quarter-hour (``by`` = ``hour`` / ``quarter-hour``)."""
    groups: dict[datetime, list[Quarter]] = {}
    for q in quarters:
        key = q.start.replace(minute=0) if by == "hour" else q.start
        groups.setdefault(key, []).append(q)
    rows = []
    for key in sorted(groups):
        qs = groups[key]
        steps = [q.step for q in qs if q.step is not None]
        bought = sum(s.grid_kwh for s in steps if s.grid_kwh > 0)
        sold = -sum(s.grid_kwh for s in steps if s.grid_kwh < 0)
        cost = -sum(s.cash_czk for s in steps if s.grid_kwh > 0)
        revenue = sum(s.cash_czk for s in steps if s.grid_kwh < 0)
        wear = sum(s.wear_czk for s in steps)
        fees = sum(q.fee_czk for q in qs)
        grid = bought - sold
        pattern = "".join(LETTER[q.step.action if q.step else None] for q in qs)
        if not steps:
            action = "no price"
        elif all(c == pattern[0] for c in pattern):
            action = qs[0].step.action  # type: ignore[union-attr]
        else:
            action = "charge" if grid > 0 else "discharge" if grid < 0 else "wait"
            action = f"mixed ({action})" if ("C" in pattern and "D" in pattern) else action
        last = qs[-1]
        soc_end = last.step.soc_kwh if last.step is not None else last.soc_before_kwh
        hours = len(qs) * DT_H
        rows.append({
            "period": to_local(key).isoformat(timespec="minutes"),
            "end": to_local(qs[-1].start + QUARTER).isoformat(timespec="minutes"),
            "quarters": len(qs),
            "action": action,
            "pattern": pattern,  # C charge, D discharge, - wait, . no price
            "spot_czk_mwh": _round(_mean([q.spot_czk_mwh for q in qs]), 2),
            "buy_czk_kwh": _round(_mean([q.buy_czk_kwh for q in qs]), 4),
            "sell_czk_kwh": _round(_mean([q.sell_czk_kwh for q in qs]), 4),
            # buy = (energy + distribution) × (1 + VAT); dist = the distribution part incl. VAT
            "dist_czk_kwh": _round(
                _mean([q.dist_czk_kwh for q in qs if not math.isnan(q.buy_czk_kwh)]), 4
            ),
            "tariff": _tariff_label(qs),  # NT / VT / VT/NT (mixed hour), "" = no schedule
            "power_kw": round(abs(grid) / hours, 3) if hours else 0.0,  # average net grid
            "grid_kwh": round(grid, ND),
            "soc_start_pct": round(battery.pct(qs[0].soc_before_kwh), 2),
            "soc_end_pct": round(battery.pct(soc_end), 2),
            "bought_kwh": round(bought, ND),
            "cost_czk": round(cost, ND),
            "sold_kwh": round(sold, ND),
            "revenue_czk": round(revenue, ND),
            "wear_czk": round(wear, ND),
            "profit_czk": round(revenue - cost - wear, ND),
            "fees_czk": round(fees, ND),
            "net_profit_czk": round(revenue - cost - wear - fees, ND),
        })  # fmt: skip
    return rows
