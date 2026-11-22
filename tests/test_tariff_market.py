from datetime import UTC, date, datetime, time

import pytest

from bess_ote.lowtariff import LowTariffSchedule
from bess_ote.market import (
    PriceBook,
    ceil_quarter,
    publication_utc,
)
from bess_ote.models import PricePoint
from bess_ote.tariff import Distribution, SpotFormula, Tariffs
from bess_ote.timeutil import HOUR, PRAGUE, QUARTER, local_midnight_utc
from conftest import synthetic_book


def local(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=PRAGUE).astimezone(UTC)


def test_spot_formula_negative_semantics():
    f = SpotFormula(1.1, 400, negative_multiplier=1.0, negative_add_czk_mwh=0)
    assert f.czk_mwh(1000) == pytest.approx(1500)
    assert f.czk_mwh(-500) == pytest.approx(-500)
    assert SpotFormula(1, 400, negative="clamp-spot").czk_mwh(-500) == 400
    assert SpotFormula(1, 400, negative="clamp-total").czk_mwh(-900) == 0
    assert "negative spot" in f.describe()


def test_buy_includes_distribution_by_nt_window_and_vat_sell_without():
    sched = LowTariffSchedule.parse("1:15-5:15,12:30-16:30", "2:45-6:45,13:00-17:00", "weekend")
    t = Tariffs(
        buy=SpotFormula(1, 400),
        sell=SpotFormula(1, -300),
        distribution=Distribution(2.435, 0.416, sched, other_czk_kwh=0.5),
        buy_vat_percent=21,
    )
    wd_nt = local(2026, 6, 10, 1, 15)  # Wednesday, NT
    wd_vt = local(2026, 6, 10, 1, 0)
    we_nt = local(2026, 6, 13, 2, 45)  # Saturday, weekend NT window
    assert t.buy_czk_kwh(2000, wd_nt, QUARTER) == pytest.approx((2.4 + 0.416 + 0.5) * 1.21)
    assert t.buy_czk_kwh(2000, wd_vt, QUARTER) == pytest.approx((2.4 + 2.435 + 0.5) * 1.21)
    assert t.buy_czk_kwh(2000, we_nt, QUARTER) == pytest.approx((2.4 + 0.416 + 0.5) * 1.21)
    assert t.sell_czk_kwh(2000) == pytest.approx(1.7)  # no VAT unless --sell-vat
    assert "on purchase only" in t.describe()["distribution"]


def test_publication_times_change_with_15min_market():
    assert publication_utc(date(2025, 9, 30)) == local(2025, 9, 29, 12, 57)
    assert publication_utc(date(2025, 10, 1)) == local(2025, 9, 30, 13, 5)
    assert publication_utc(date(2026, 6, 2), time(14, 20)) == local(2026, 6, 1, 14, 20)


def test_horizon_includes_tomorrow_only_after_publication():
    book = synthetic_book(date(2026, 6, 1), 3)
    end1 = local_midnight_utc(date(2026, 6, 2))
    end2 = local_midnight_utc(date(2026, 6, 3))
    h = book.horizon(local(2026, 6, 1, 13), local(2026, 6, 1, 13))
    assert h[-1] + QUARTER == end1  # 13:05 not reached yet
    h = book.horizon(local(2026, 6, 1, 13, 15), local(2026, 6, 1, 13, 15))
    assert h[-1] + QUARTER == end2
    old = synthetic_book(date(2025, 6, 1), 2, hourly=True)
    h = old.horizon(local(2025, 6, 1, 13), local(2025, 6, 1, 13))  # 12:57 rule
    assert h[-1] + QUARTER == local_midnight_utc(date(2025, 6, 3))


def test_hourly_prices_fill_quarters_and_dst_days():
    book = synthetic_book(date(2025, 3, 30), 1, hourly=True)
    assert len(book.spot) == 92 and book.resolution[date(2025, 3, 30)] == "PT60M"
    book = synthetic_book(date(2025, 10, 26), 1)
    assert len(book.spot) == 100 and book.resolution[date(2025, 10, 26)] == "PT15M"
    t0 = local_midnight_utc(date(2024, 1, 1))
    b = PriceBook.from_points([PricePoint(t0, HOUR, 100.0)])
    assert [b.spot[t0 + i * QUARTER] for i in range(4)] == [100.0] * 4
    assert sum(1 for v in b.spot.values() if v != v) == 92  # rest of the day: NaN


def test_ceil_quarter():
    assert ceil_quarter(local(2026, 11, 22, 10, 56)) == local(2026, 11, 22, 11)
    assert ceil_quarter(local(2026, 11, 22, 11, 0)) == local(2026, 11, 22, 11)
