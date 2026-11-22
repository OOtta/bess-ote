"""Small shared data types."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True, slots=True)
class PricePoint:
    """Day-ahead market price for one delivery period."""

    start: datetime  # UTC
    length: timedelta
    czk_mwh: float
    eur_mwh: float | None = None
    eur_czk: float | None = None  # exchange rate used (None when OTE published CZK directly)

    @property
    def end(self) -> datetime:
        return self.start + self.length
