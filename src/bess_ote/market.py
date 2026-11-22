"""OTE day-ahead prices as a quarter-hour series, and when they become known.

Publication of the day-ahead (DT / SDAC) results for delivery day ``D`` happens on ``D − 1``
(local time, Europe/Prague):

* delivery days up to 2025-09-30 (60-minute MTU): final results at **12:57**;
* delivery days from 2025-10-01 (15-minute MTU): final results at about **13:05**
  (calculation extended from 17 to 30 minutes; SDAC preliminary results never before 12:55,
  final publication "~13:05" with no fixed time, latest 14:20 in a delay situation).

Sources: OTE webinar "Zavedení 15min. produktů na DT" (2025-03, process times 12:57 → 13:02
planned), OTE seminar 2025 (12:57 → 13:05*), SDAC Fallback Manual v1.0 applicable from
2025-09-30 (normal day: preliminary 12:55, final ~13:05, decoupling deadline 14:20).
Decisions are taken at quarter-hour boundaries, so with the defaults the next day is first
used at 13:00 (until 2025-09-30) or 13:15 (from 2025-10-01). ``--publish-time`` overrides it.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta

from bess_ote.models import PricePoint
from bess_ote.timeutil import PRAGUE, QUARTER, local_date, local_midnight_utc

QUARTER_HOUR_MARKET_START = date(2025, 10, 1)
PUBLISH_60MIN = time(12, 57)
PUBLISH_15MIN = time(13, 5)


def default_publish_time(day: date) -> time:
    return PUBLISH_15MIN if day >= QUARTER_HOUR_MARKET_START else PUBLISH_60MIN


def publication_utc(day: date, override: time | None = None) -> datetime:
    """Moment when prices for delivery day ``day`` are known (local time on ``day − 1``)."""
    t = override or default_publish_time(day)
    prev = day - timedelta(days=1)
    return datetime.combine(prev, t, tzinfo=PRAGUE).astimezone(UTC)


def ceil_quarter(dt: datetime) -> datetime:
    """First quarter-hour boundary at or after ``dt`` (UTC; Prague offsets are whole hours)."""
    dt = dt.astimezone(UTC)
    base = dt.replace(minute=0, second=0, microsecond=0)
    q = math.ceil((dt - base) / QUARTER)
    return base + q * QUARTER


@dataclass
class PriceBook:
    """Spot prices (CZK/MWh) per quarter-hour start (UTC); hourly prices fill 4 quarters.

    ``resolution`` maps each delivery day to ``PT60M`` / ``PT15M``. A quarter without a market
    price (OTE emergency state) is stored as NaN.
    """

    spot: dict[datetime, float] = field(default_factory=dict)
    resolution: dict[date, str] = field(default_factory=dict)
    _pub: dict[tuple[date, time | None], datetime] = field(
        default_factory=dict, repr=False, compare=False
    )  # memoised publication_utc (a pure function of day and override)

    def publication(self, day: date, publish: time | None = None) -> datetime:
        key = (day, publish)
        pub = self._pub.get(key)
        if pub is None:
            pub = self._pub[key] = publication_utc(day, publish)
        return pub

    @classmethod
    def from_points(cls, points: Iterable[PricePoint]) -> PriceBook:
        book = cls()
        for p in points:
            n = round(p.length / QUARTER)
            for i in range(n):
                book.spot[p.start + i * QUARTER] = p.czk_mwh
            book.resolution[local_date(p.start)] = "PT60M" if n == 4 else "PT15M"
        # complete days: fill missing quarters (no price published) with NaN
        for d in list(book.resolution):
            t, end = local_midnight_utc(d), local_midnight_utc(d + timedelta(days=1))
            while t < end:
                book.spot.setdefault(t, math.nan)
                t += QUARTER
        return book

    @property
    def days(self) -> list[date]:
        return sorted(self.resolution)

    def known_days(self, at: datetime | None, publish: time | None = None) -> set[date]:
        """Days whose prices are in the book and published by ``at`` (None = all)."""
        if at is None:
            return set(self.resolution)
        return {d for d in self.resolution if self.publication(d, publish) <= at}

    def _known(self, d: date, at: datetime | None, publish: time | None) -> bool:
        return d in self.resolution and (at is None or self.publication(d, publish) <= at)

    def horizon_end(
        self, start: datetime, at: datetime | None, publish: time | None = None
    ) -> datetime | None:
        """Last quarter start of :meth:`horizon` (None if empty) without building the list."""
        d = local_date(start)
        if not self._known(d, at, publish):
            return None
        while self._known(d + timedelta(days=1), at, publish):
            d += timedelta(days=1)
        return local_midnight_utc(d + timedelta(days=1)) - QUARTER

    def values_by_day(self) -> dict[date, list[float]]:
        """Non-NaN spot prices grouped by local day, in the book's iteration order."""
        out: dict[date, list[float]] = {}
        for t, v in self.spot.items():
            if not math.isnan(v):
                out.setdefault(local_date(t), []).append(v)
        return out

    def horizon(
        self, start: datetime, at: datetime | None, publish: time | None = None
    ) -> list[datetime]:
        """Quarter starts from ``start`` to the end of the last *contiguously* known day."""
        known = self.known_days(at, publish)
        out = []
        d = local_date(start)
        t = start
        while d in known:
            end = local_midnight_utc(d + timedelta(days=1))
            while t < end:
                out.append(t)
                t += QUARTER
            d += timedelta(days=1)
        return out
