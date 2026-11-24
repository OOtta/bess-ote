"""PV data: forecasts as they were issued (Open-Meteo) and measured irradiance (ČHMÚ).

* **Forecasts** – Open-Meteo Single Runs API (``&run=`` = model initialisation, UTC): DWD
  ICON-D2 (15-minute, every 3 h, archived from 2026-04-02) and before that ECMWF IFS HRES 9 km
  (hourly, every 6 h, archived from 2024-03-14). A run counts as known
  :data:`AVAILABILITY` after its initialisation (measured: ICON-D2 ≈ 1 h 25 min, IFS ≈ 6 h
  10 min, so 2 h / 7 h are conservative). ``plan`` without ``--at`` uses the live forecast
  (Open-Meteo forecast API, 15-minute).
* **Actuals** – ČHMÚ open data, 10-minute global radiation (``RGLB10``, W/m², time stamp = end
  of the 10 minutes, UTC) and air temperature of the nearest station that measures radiation.

Everything is cached (namespace ``pv``) except the live forecast. Open-Meteo is free for
non-commercial use (CC BY 4.0), ČHMÚ open data CC BY 4.0.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import requests

from bess_ote.cache import JsonCache
from bess_ote.http import DEFAULT_TIMEOUT, FetchError
from bess_ote.pv import Irradiance, PvSystem
from bess_ote.timeutil import QUARTER

SINGLE_RUNS_URL = "https://single-runs-api.open-meteo.com/v1/forecast"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
CHMI_URL = "https://opendata.chmi.cz/meteorology/climate"
ICON_D2_FROM = datetime(2026, 4, 2, tzinfo=UTC)
FORMAT = 1


@dataclass(frozen=True, slots=True)
class ModelSpec:
    name: str
    every_h: int
    delay: timedelta
    minutely: bool
    hours: int  # forecast length requested


MODELS = {
    "icon_d2": ModelSpec("icon_d2", 3, timedelta(hours=2), True, 48),
    "ecmwf_ifs": ModelSpec("ecmwf_ifs", 6, timedelta(hours=7), False, 72),
}
#: older runs used to fill quarters the newest run does not cover
RETRY_DELAYS_S = (2, 5, 10, 20, 40, 60)  # Open-Meteo throttles bursts with HTTP 429
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
_sleep = time.sleep  # patched in tests


def get_with_retry(
    session: requests.Session, url: str, params: dict[str, Any]
) -> requests.Response:
    """GET, retrying throttling (429) and transient server errors with a back-off."""
    for delay in (*RETRY_DELAYS_S, None):
        resp = session.get(url, params=params, timeout=DEFAULT_TIMEOUT)
        if resp.status_code not in RETRY_STATUS or delay is None:
            return resp
        _sleep(delay)
    raise AssertionError("unreachable")


FALLBACK_RUNS = 4


@dataclass(frozen=True, slots=True, order=True)
class Run:
    available: datetime
    init: datetime
    model: str

    @property
    def label(self) -> str:
        return f"{self.model} {self.init:%Y-%m-%d %H} UTC"


def _floor(t: datetime, hours: int) -> datetime:
    t = t.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    return t.replace(hour=t.hour - t.hour % hours)


def model_for(init: datetime) -> ModelSpec:
    return MODELS["icon_d2"] if init >= ICON_D2_FROM else MODELS["ecmwf_ifs"]


def latest_run(at: datetime) -> Run:
    """Newest run known at ``at``."""
    for spec in (MODELS["icon_d2"], MODELS["ecmwf_ifs"]):
        init = _floor(at - spec.delay, spec.every_h)
        if model_for(init) is spec:
            return Run(init + spec.delay, init, spec.name)
    raise AssertionError("unreachable")


def previous_run(run: Run) -> Run:
    spec = MODELS[run.model]
    init = run.init - timedelta(hours=spec.every_h)
    spec = model_for(init)
    init = _floor(init, spec.every_h)
    return Run(init + spec.delay, init, spec.name)


def runs_between(a: datetime, b: datetime) -> list[Run]:
    """Runs that become known in (a, b], oldest first."""
    out: list[Run] = []
    r = latest_run(b)
    while r.available > a:
        out.append(r)
        r = previous_run(r)
    return out[::-1]


def _parse_series(doc: dict[str, Any], minutely: bool) -> dict[datetime, Irradiance]:
    """Open-Meteo radiation = mean over the interval ENDING at the time stamp (verified: the
    hourly value equals the mean of the 15-minute values labelled −45 … 0 min)."""
    block = doc.get("minutely_15" if minutely else "hourly") or {}
    times = block.get("time") or []
    ghi = block.get("shortwave_radiation") or []
    dhi = block.get("diffuse_radiation") or []
    tmp = block.get("temperature_2m") or []
    out: dict[datetime, Irradiance] = {}
    for i, ts in enumerate(times):
        g = ghi[i] if i < len(ghi) else None
        if g is None:
            continue
        end = datetime.fromisoformat(ts).replace(tzinfo=UTC)
        irr = Irradiance(float(g), None if i >= len(dhi) or dhi[i] is None else float(dhi[i]),
                         None if i >= len(tmp) or tmp[i] is None else float(tmp[i]))  # fmt: skip
        n = 1 if minutely else 4
        for k in range(1, n + 1):
            out[end - k * QUARTER] = irr
    return out


class OpenMeteo:
    def __init__(self, session: requests.Session, cache: JsonCache, lat: float, lon: float,
                 now: datetime | None = None) -> None:  # fmt: skip
        self.session, self.cache, self.lat, self.lon = session, cache, lat, lon
        self.now = now or datetime.now(UTC)
        self.downloaded = 0
        self.cached = 0
        self._mem: dict[Run, dict[datetime, Irradiance] | None] = {}

    def _key(self, run: Run) -> str:
        return f"{run.model}-{run.init:%Y%m%dT%H}-{self.lat:.3f}-{self.lon:.3f}"

    def run(self, run: Run) -> dict[datetime, Irradiance] | None:
        """Irradiance per quarter-hour start of one run; None if Open-Meteo has no such run."""
        if run in self._mem:
            return self._mem[run]
        spec = MODELS[run.model]
        key = self._key(run)
        doc = self.cache.get("pv", key)
        if isinstance(doc, dict) and doc.get("format") == FORMAT:
            self.cached += 1
        else:
            doc = self._download(run, spec)
            self.downloaded += 1
            final = doc.get("missing") is not True or run.init < self.now - timedelta(days=2)
            if final:
                self.cache.set("pv", key, doc)
        res = None if doc.get("missing") else _parse_series(doc["data"], spec.minutely)
        self._mem[run] = res
        return res

    def _download(self, run: Run, spec: ModelSpec) -> dict[str, Any]:
        variables = "shortwave_radiation,diffuse_radiation,temperature_2m"
        params: dict[str, Any] = {
            "latitude": self.lat, "longitude": self.lon, "models": spec.name,
            "run": f"{run.init:%Y-%m-%dT%H:%M}",
        }  # fmt: skip
        if spec.minutely:
            params |= {"minutely_15": variables, "forecast_minutely_15": spec.hours * 4 + 4}
        else:
            params |= {"hourly": variables, "forecast_hours": spec.hours}
        try:
            resp = get_with_retry(self.session, SINGLE_RUNS_URL, params)
        except requests.RequestException as exc:
            raise FetchError(f"Cannot download the PV forecast {run.label}: {exc}") from exc
        if resp.status_code == 400 and "not available" in resp.text:
            return {"format": FORMAT, "missing": True}
        if resp.status_code != 200:
            raise FetchError(f"Open-Meteo returned HTTP {resp.status_code} for {run.label}: "
                             f"{resp.text[:200]}")  # fmt: skip
        data = resp.json()
        keep = "minutely_15" if spec.minutely else "hourly"
        return {"format": FORMAT, "data": {keep: data.get(keep, {})}}

    def live(self) -> dict[datetime, Irradiance]:
        params = {
            "latitude": self.lat, "longitude": self.lon,
            "minutely_15": "shortwave_radiation,diffuse_radiation,temperature_2m",
            "forecast_days": 3,
        }  # fmt: skip
        try:
            resp = get_with_retry(self.session, FORECAST_URL, params)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise FetchError(f"Cannot download the live PV forecast: {exc}") from exc
        self.downloaded += 1
        return _parse_series(resp.json(), True)


class PvForecast:
    """PV energy per quarter-hour as known at a moment (newest run, gaps from older runs)."""

    def __init__(self, om: OpenMeteo, system: PvSystem,
                 warn: Callable[[str], None] | None = None) -> None:  # fmt: skip
        self.om, self.system, self._warn = om, system, warn
        self._energy: dict[Run, dict[datetime, float] | None] = {}
        self.missing_runs: set[Run] = set()
        self.uncovered = 0

    def energy(self, run: Run) -> dict[datetime, float] | None:
        if run not in self._energy:
            irr = self.om.run(run)
            if irr is None:
                self.missing_runs.add(run)
            self._energy[run] = None if irr is None else self.system.energy(irr)
        return self._energy[run]

    def at(self, at: datetime, starts: list[datetime]) -> tuple[list[float], Run | None]:
        """Forecast for ``starts`` known at ``at`` and the newest run actually used."""
        need = dict.fromkeys(starts)
        run, used = latest_run(at), None
        for _ in range(FALLBACK_RUNS + 1):
            e = self.energy(run)
            if e is not None:
                used = used or run
                for t in starts:
                    if need[t] is None and t in e:
                        need[t] = e[t]
                if all(v is not None for v in need.values()):
                    break
            run = previous_run(run)
        missing = sum(v is None for v in need.values())
        self.uncovered += missing
        return [0.0 if v is None else v for v in need.values()], used


# ------------------------------------------------------------------------------------------
# ČHMÚ actuals
# ------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Station:
    wsi: str
    name: str
    lat: float
    lon: float
    distance_km: float


def _distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    c = math.sin(p1) * math.sin(p2) + math.cos(p1) * math.cos(p2) * math.cos(dl)
    return 6371.0 * math.acos(max(-1.0, min(1.0, c)))


class Chmi:
    """10-minute global radiation and temperature from ČHMÚ open data."""

    def __init__(self, session: requests.Session, cache: JsonCache,
                 today: date | None = None) -> None:  # fmt: skip
        self.session, self.cache = session, cache
        self.today = today or datetime.now(UTC).date()
        self.downloaded = 0
        self.cached = 0

    def _json(self, path: str) -> Any | None:
        try:
            resp = self.session.get(f"{CHMI_URL}/{path}", timeout=DEFAULT_TIMEOUT * 2)
        except requests.RequestException as exc:
            raise FetchError(f"Cannot download ČHMÚ data {path}: {exc}") from exc
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise FetchError(f"ČHMÚ returned HTTP {resp.status_code} for {path}")
        self.downloaded += 1
        return resp.json()

    def stations(self) -> list[tuple[str, str, float, float]]:
        """Stations measuring 10-minute global radiation: (WSI, name, lat, lon)."""
        doc = self.cache.get("pv", "chmi-stations")
        if (isinstance(doc, dict) and doc.get("format") == FORMAT
                and doc.get("fetched", "") >= str(self.today - timedelta(days=60))):  # fmt: skip
            return [tuple(s) for s in doc["stations"]]  # type: ignore[misc]
        for back in range(4):
            d = self.today - timedelta(days=back)
            m1 = self._json(f"now/metadata/meta1-{d:%Y%m%d}.json")
            m2 = self._json(f"now/metadata/meta2-{d:%Y%m%d}.json")
            if m1 and m2:
                break
        else:
            raise FetchError("ČHMÚ station metadata not found")
        coords = {v[0]: (v[2], float(v[4]), float(v[3])) for v in m1["data"]["data"]["values"]}
        wsis = {v[1] for v in m2["data"]["data"]["values"] if v[0] == "10M" and v[2] == "RGLB10"}
        out = sorted((w, *coords[w]) for w in wsis if w in coords)
        self.cache.set(
            "pv", "chmi-stations", {"format": FORMAT, "fetched": str(self.today), "stations": out}
        )
        return out  # type: ignore[return-value]

    def nearest(self, lat: float, lon: float) -> Station:
        best = min(self.stations(), key=lambda s: _distance_km(lat, lon, s[2], s[3]))
        return Station(best[0], best[1], best[2], best[3],
                       _distance_km(lat, lon, best[2], best[3]))  # fmt: skip

    @staticmethod
    def _reduce(doc: Any) -> dict[str, list[float | None]]:
        out: dict[str, list[float | None]] = {}
        for row in doc["data"]["data"]["values"]:
            el = row[1]
            if el not in ("RGLB10", "T"):
                continue
            v = out.setdefault(row[2], [None, None])
            v[0 if el == "RGLB10" else 1] = None if row[3] is None else float(row[3])
        return out

    def _period(self, wsi: str, y: int, m: int) -> dict[str, list[float | None]]:
        key = f"chmi-{wsi}-{y:04d}{m:02d}"
        doc = self.cache.get("pv", key)
        if isinstance(doc, dict) and doc.get("format") == FORMAT:
            self.cached += 1
            return doc["values"]
        last = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
        complete = last < self.today - timedelta(days=1)
        name = f"10m-{wsi}-{y:04d}{m:02d}.json"
        paths = [f"historical/data/10min/{y}/{name}", f"recent/data/10min/{m:02d}/{name}"]
        if y == self.today.year:
            paths.reverse()
        values: dict[str, list[float | None]] = {}
        for p in paths:
            got = self._json(p)
            if got is not None:
                values = self._reduce(got)
                break
        else:  # current (or just finished) month: daily files
            d = date(y, m, 1)
            while d <= min(last, self.today):
                day = f"10m-{wsi}-{d:%Y%m%d}.json"
                got = self._json(f"recent/data/10min/{day}") or self._json(f"now/data/{day}")
                if got is not None:
                    values |= self._reduce(got)
                d += timedelta(days=1)
        if complete and values:
            self.cache.set("pv", key, {"format": FORMAT, "values": values})
        return values

    def irradiance(self, wsi: str, start: datetime, end: datetime) -> dict[datetime, Irradiance]:
        """Quarter-hour means for quarter starts in [start, end) – a quarter needs ≥ 10 of its
        15 minutes measured; missing quarters are left out."""
        raw: dict[datetime, tuple[float | None, float | None]] = {}
        y, m = start.astimezone(UTC).year, start.astimezone(UTC).month
        while (y, m) <= (end.astimezone(UTC).year, end.astimezone(UTC).month):
            for ts, (g, tc) in self._period(wsi, y, m).items():
                raw[datetime.fromisoformat(ts.replace("Z", "+00:00"))] = (g, tc)
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        out: dict[datetime, Irradiance] = {}
        t = start
        while t < end:
            # 10-minute values cover (label − 10 min, label]; a quarter overlaps three of them
            acc = w = 0.0
            temps = []
            base = t.replace(minute=t.minute - t.minute % 10)
            for k in range(3):
                lab = base + timedelta(minutes=10 * (k + 1))
                lo, hi = max(t, lab - timedelta(minutes=10)), min(t + QUARTER, lab)
                minutes = (hi - lo).total_seconds() / 60
                if minutes <= 0:
                    continue
                g, tc = raw.get(lab, (None, None))
                if g is not None:
                    acc += max(g, 0.0) * minutes
                    w += minutes
                if tc is not None:
                    temps.append(tc)
            if w >= 10:
                out[t] = Irradiance(acc / w, None, sum(temps) / len(temps) if temps else None)
            t += QUARTER
        return out
