"""Persistent per-delivery-day cache of OTE prices and ČNB rates (mocked OTE/ČNB, no network)."""

import json
import sys
from datetime import date
from pathlib import Path

import pytest
import requests

from bess_ote.cache import JsonCache, default_cache_dir
from bess_ote.cnb import CnbRates
from bess_ote.ote import CACHE_FORMAT, OteClient, expected_periods
from conftest import FakeOte, add_cnb, add_ote

TODAY = date(2025, 12, 1)


def client(cache, today=TODAY):
    s = requests.Session()
    return OteClient(s, cache, CnbRates(s, cache, today=today), today=today)


def month_doc(cache, mk):
    return json.loads((cache.directory / "ote" / f"{mk}.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("BESS_OTE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))


@pytest.mark.parametrize(
    ("day", "n"),
    [
        (date(2025, 3, 30), 23),  # spring DST, hourly era
        (date(2024, 10, 27), 25),  # autumn DST, hourly era
        (date(2025, 9, 30), 24),
        (date(2025, 10, 1), 96),  # first 15-minute day
        (date(2025, 10, 26), 100),  # autumn DST, 15-minute era
        (date(2026, 3, 29), 92),  # spring DST, 15-minute era
    ],
)
def test_expected_periods_dst(day, n):
    assert expected_periods(day) == n


def test_second_run_makes_no_request_and_format_is_versioned(mocked, cache):
    fake = FakeOte(lambda d, i: float(i))
    add_ote(mocked, fake)
    add_cnb(mocked)
    first = client(cache)
    p1 = first.prices(date(2025, 9, 29), date(2025, 10, 2))
    assert first.stats.downloaded_days == 4 and first.stats.requests == 2
    calls = len(fake.calls)

    second = client(cache)
    p2 = second.prices(date(2025, 9, 29), date(2025, 10, 2))
    assert len(fake.calls) == calls  # zero OTE requests
    assert second.stats.as_dict() == {
        "cached_days": 4,
        "downloaded_days": 0,
        "requests": 0,
        "not_cached_days": 0,
    }
    assert p1 == p2
    sep, octo = month_doc(cache, "2025-09"), month_doc(cache, "2025-10")
    assert sep["format"] == octo["format"] == CACHE_FORMAT
    day = sep["days"]["2025-09-30"]
    assert (day["resolution"], day["currency"], len(day["rows"])) == ("PT60M", "CZK", 24)
    day = octo["days"]["2025-10-01"]
    assert (day["resolution"], day["currency"], len(day["rows"])) == ("PT15M", "EUR", 96)
    assert set(day["rows"][0]) == {"i", "eur", "eur_h"}  # raw source values, not CZK


def test_only_missing_days_are_fetched(mocked, cache):
    fake = FakeOte(lambda d, i: 1.0)
    add_ote(mocked, fake)
    client(cache).prices(date(2025, 3, 10), date(2025, 3, 12))
    fake.calls.clear()
    c = client(cache)
    c.prices(date(2025, 3, 8), date(2025, 3, 14))
    assert fake.calls == [
        ("GetDamPriceE", date(2025, 3, 8), date(2025, 3, 9)),
        ("GetDamPriceE", date(2025, 3, 13), date(2025, 3, 14)),
    ]
    assert (c.stats.cached_days, c.stats.downloaded_days) == (3, 4)


def test_today_and_future_days_are_never_cached(mocked, cache):
    fake = FakeOte(lambda d, i: 1.0)  # OTE already publishes tomorrow's complete prices
    add_ote(mocked, fake)
    add_cnb(mocked)
    today = date(2025, 10, 10)
    c = client(cache, today)
    c.prices(date(2025, 10, 9), date(2025, 10, 11))
    assert sorted(month_doc(cache, "2025-10")["days"]) == ["2025-10-09"]
    assert c.stats.not_cached_days == 2
    fake.calls.clear()
    client(cache, today).prices(date(2025, 10, 9), date(2025, 10, 11))
    assert fake.calls == [("GetDamPricePeriodE", date(2025, 10, 10), date(2025, 10, 11))]


def test_wrong_period_count_is_not_cached(mocked, cache, monkeypatch):
    import conftest

    # OTE returns 24 hours for the 23-hour DST day → not trusted, not cached
    monkeypatch.setattr(conftest, "periods_in_day", lambda d, minutes: 24 if minutes == 60 else 96)
    add_ote(mocked, FakeOte(lambda d, i: 1.0))
    c = client(cache)
    c.prices(date(2025, 3, 29), date(2025, 3, 30))
    assert sorted(month_doc(cache, "2025-03")["days"]) == ["2025-03-29"]


def test_corrupted_file_is_refetched_with_warning(mocked, tmp_path):
    warnings = []
    cache = JsonCache(tmp_path / "c", warn=warnings.append)
    fake = FakeOte(lambda d, i: 2.0)
    add_ote(mocked, fake)
    client(cache).prices(date(2025, 3, 1), date(2025, 3, 2))
    path = cache.directory / "ote" / "2025-03.json"
    path.write_text('{"format": 2, "days": {', encoding="utf-8")
    fake.calls.clear()
    c = client(cache)
    assert len(c.prices(date(2025, 3, 1), date(2025, 3, 2))) == 48
    assert len(fake.calls) == 1
    assert len(warnings) == 1 and "2025-03.json" in warnings[0]
    assert len(month_doc(cache, "2025-03")["days"]) == 2  # rewritten


def test_invalid_entry_is_dropped_and_refetched(mocked, cache):
    fake = FakeOte(lambda d, i: 3.0)
    add_ote(mocked, fake)
    client(cache).prices(date(2025, 3, 1), date(2025, 3, 2))
    doc = month_doc(cache, "2025-03")
    doc["days"]["2025-03-02"]["rows"].pop()  # truncated day
    doc["days"]["2025-03-01"]["resolution"] = "PT15M"  # wrong regime
    (cache.directory / "ote" / "2025-03.json").write_text(json.dumps(doc), encoding="utf-8")
    fake.calls.clear()
    client(cache).prices(date(2025, 3, 1), date(2025, 3, 2))
    assert fake.calls == [("GetDamPriceE", date(2025, 3, 1), date(2025, 3, 2))]


def test_legacy_unversioned_file_is_migrated(mocked, cache):
    fake = FakeOte(lambda d, i: 4.0)
    add_ote(mocked, fake)
    legacy = {
        "2025-03-01": [{"i": i, "czk": 4.0} for i in range(1, 25)],
        "2025-03-02": [{"i": i, "czk": 4.0} for i in range(1, 24)],  # incomplete → dropped
    }
    (cache.directory / "ote").mkdir(parents=True)
    (cache.directory / "ote" / "2025-03.json").write_text(json.dumps(legacy), encoding="utf-8")
    c = client(cache)
    c.prices(date(2025, 3, 1), date(2025, 3, 2))
    assert fake.calls == [("GetDamPriceE", date(2025, 3, 2), date(2025, 3, 2))]
    doc = month_doc(cache, "2025-03")
    assert doc["format"] == CACHE_FORMAT and sorted(doc["days"]) == ["2025-03-01", "2025-03-02"]


def test_refresh_ignores_cache_but_rewrites_it(mocked, tmp_path):
    fake = FakeOte(lambda d, i: 5.0)
    add_ote(mocked, fake)
    client(JsonCache(tmp_path / "c")).prices(date(2025, 3, 1), date(2025, 3, 1))
    fake.calls.clear()
    c = client(JsonCache(tmp_path / "c", refresh=True))
    c.prices(date(2025, 3, 1), date(2025, 3, 1))
    assert len(fake.calls) == 1 and c.stats.cached_days == 0
    assert (tmp_path / "c" / "ote" / "2025-03.json").exists()


def test_disabled_cache_writes_nothing(mocked, tmp_path):
    add_ote(mocked, FakeOte(lambda d, i: 5.0))
    client(JsonCache(None)).prices(date(2025, 3, 1), date(2025, 3, 1))
    assert not any(tmp_path.iterdir())


def test_atomic_write_leaves_no_temp_files(cache):
    cache.set("ote", "2025-01", {"format": 2, "days": {}})
    cache.set("ote", "2025-01", {"format": 2, "days": {"x": 1}})
    assert [p.name for p in (cache.directory / "ote").iterdir()] == ["2025-01.json"]


def test_cnb_v2_format_past_year_immutable_and_weekend_no_refetch(mocked, cache):
    add_cnb(mocked)
    today = date(2025, 10, 6)  # Monday
    CnbRates(requests.Session(), cache, today=today).eur_czk(date(2025, 10, 4))
    doc = json.loads((cache.directory / "cnb" / "2025.json").read_text(encoding="utf-8"))
    assert doc["format"] == 2 and doc["fetched_on"] == "2025-10-06" and doc["rates"]
    n = len(mocked.calls)
    # Saturday after the last cached fixing, cache fetched after it → no refetch
    doc["rates"] = {k: v for k, v in doc["rates"].items() if k <= "2025-10-03"}
    (cache.directory / "cnb" / "2025.json").write_text(json.dumps(doc), encoding="utf-8")
    r = CnbRates(requests.Session(), cache, today=today)
    r.eur_czk(date(2025, 10, 5))
    assert len(mocked.calls) == n and r.stats.cached_years == 1
    # a day after the fetch date → refetch once
    r = CnbRates(requests.Session(), cache, today=date(2025, 10, 20))
    r.eur_czk(date(2025, 10, 15))
    assert len(mocked.calls) == n + 1 and r.stats.downloaded_years == 1
    # fetched after the year ended → complete, never refetched
    r = CnbRates(requests.Session(), cache, today=date(2026, 1, 5))
    r._load_year(2025)
    r = CnbRates(requests.Session(), cache, today=date(2026, 1, 5))
    r.eur_czk(date(2025, 12, 31))
    assert len(mocked.calls) == n + 2


def test_cnb_corrupted_file_warns_and_refetches(mocked, tmp_path):
    warnings = []
    cache = JsonCache(tmp_path / "c", warn=warnings.append)
    add_cnb(mocked)
    (cache.directory / "cnb").mkdir(parents=True)
    (cache.directory / "cnb" / "2025.json").write_text('{"format": 2, "rates": "x"}', "utf-8")
    CnbRates(requests.Session(), cache, today=TODAY).eur_czk(date(2025, 10, 1))
    assert len(warnings) == 1 and "ČNB" in warnings[0]
    assert len(mocked.calls) == 1


@pytest.mark.parametrize(
    ("platform", "env", "expected"),
    [
        ("linux", {"XDG_CACHE_HOME": "/x/cache"}, "/x/cache/bess-ote"),
        ("linux", {}, "~/.cache/bess-ote"),
        ("win32", {"LOCALAPPDATA": "/w/Local"}, "/w/Local/bess-ote"),
        ("linux", {"BESS_OTE_CACHE_DIR": "/own", "XDG_CACHE_HOME": "/x"}, "/own"),
    ],
)
def test_default_cache_dir(monkeypatch, tmp_path, platform, env, expected):
    for v in ("BESS_OTE_CACHE_DIR", "XDG_CACHE_HOME", "LOCALAPPDATA"):
        monkeypatch.delenv(v, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    # Termux: home = /data/data/com.termux/files/home
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(sys, "platform", platform)
    want = expected.replace("~", tmp_path.as_posix())
    assert default_cache_dir().as_posix() == want
