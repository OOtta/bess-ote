"""PV plant model: irradiance → energy per quarter-hour.

* sun position: NOAA low-accuracy formulas (error < 0.1° – plenty for energy estimates);
* horizontal → plane of array: isotropic sky, ground albedo 0.2 (same model Open-Meteo uses
  for its tilted irradiance); measured global irradiance without the diffuse part (ČHMÚ) is
  split with the Erbs decomposition;
* module temperature: NOCT model ``T_cell = T_air + G × (NOCT − 20) / 800`` with NOCT 45 °C and
  a power temperature coefficient of −0.4 %/K;
* ``losses`` (default 14 %, as PVGIS) cover inverter, cables, mismatch, soiling.

Azimuth in degrees as in PVGIS / Open-Meteo: 0 = south, −90 = east, 90 = west, ±180 = north;
tilt 0 = horizontal, 90 = vertical.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from bess_ote.battery import DT_H

ALBEDO = 0.2
NOCT_C = 45.0
TEMP_COEFF = -0.004  # per K
MIN_COS_ZENITH = 0.065  # below ≈ 86° zenith the beam is treated as diffuse
SOLAR_CONSTANT = 1361.0


class PvError(ValueError):
    """Invalid PV definition."""


@dataclass(frozen=True, slots=True)
class PvArray:
    azimuth: float  # degrees, 0 = south
    tilt: float  # degrees
    kwp: float

    def __post_init__(self) -> None:
        if not -180 <= self.azimuth <= 360:
            raise PvError(f"azimuth {self.azimuth:g}° out of range (0 = south, −90 = east)")
        if not 0 <= self.tilt <= 90:
            raise PvError(f"tilt {self.tilt:g}° out of range 0–90")
        if self.kwp <= 0:
            raise PvError("kWp must be > 0")

    @classmethod
    def parse(cls, text: str) -> PvArray:
        """``"AZIMUTH,TILT,KWP"`` e.g. ``"0,35,5"`` (south, 35°, 5 kWp)."""
        parts = [p.strip() for p in text.replace(";", ",").split(",")]
        if len(parts) != 3:
            raise PvError(f"PV array {text!r}: use AZIMUTH,TILT,KWP (e.g. 0,35,5)")
        try:
            az, tilt, kwp = map(float, parts)
        except ValueError as exc:
            raise PvError(f"PV array {text!r}: numbers expected") from exc
        return cls(az, tilt, kwp)

    def describe(self) -> str:
        return f"{self.kwp:g} kWp, azimuth {self.azimuth:g}°, tilt {self.tilt:g}°"


@dataclass(frozen=True, slots=True)
class Irradiance:
    """Mean irradiance over a quarter-hour (W/m²) and air temperature (°C)."""

    ghi: float
    dhi: float | None = None  # None = unknown (decomposed with Erbs)
    temp_c: float | None = None


def sun_position(t: datetime, lat: float, lon: float) -> tuple[float, float]:
    """(cos zenith, azimuth in degrees with 0 = south, −90 = east) at UTC time ``t``."""
    t = t.astimezone(UTC)
    doy = t.timetuple().tm_yday
    hour = t.hour + t.minute / 60 + t.second / 3600
    g = 2 * math.pi / 365 * (doy - 1 + (hour - 12) / 24)
    eot = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                    - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))  # fmt: skip
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g)
            - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
            - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))  # fmt: skip
    solar_min = hour * 60 + eot + 4 * lon
    ha = math.radians(solar_min / 4 - 180)
    phi = math.radians(lat)
    cz = math.sin(phi) * math.sin(decl) + math.cos(phi) * math.cos(decl) * math.cos(ha)
    cz = max(-1.0, min(1.0, cz))
    sz = math.sqrt(max(0.0, 1 - cz * cz))
    if sz < 1e-9:
        return cz, 0.0
    # azimuth from south, positive towards west
    sin_az = math.cos(decl) * math.sin(ha) / sz
    cos_az = (cz * math.sin(phi) - math.sin(decl)) / (sz * math.cos(phi))
    return cz, math.degrees(math.atan2(sin_az, cos_az))


def extraterrestrial(t: datetime) -> float:
    doy = t.timetuple().tm_yday
    return SOLAR_CONSTANT * (1 + 0.033 * math.cos(2 * math.pi * doy / 365))


def erbs_diffuse(ghi: float, cos_z: float, t: datetime) -> float:
    """Diffuse horizontal irradiance from the global one (Erbs et al. 1982)."""
    if ghi <= 0 or cos_z <= MIN_COS_ZENITH:
        return max(ghi, 0.0)
    kt = min(ghi / (extraterrestrial(t) * cos_z), 1.0)
    if kt <= 0.22:
        kd = 1 - 0.09 * kt
    elif kt <= 0.8:
        kd = 0.9511 - 0.1604 * kt + 4.388 * kt**2 - 16.638 * kt**3 + 12.336 * kt**4
    else:
        kd = 0.165
    return ghi * kd


def plane_of_array(irr: Irradiance, cos_z: float, sun_az: float, arr: PvArray,
                   t: datetime) -> float:  # fmt: skip
    """Irradiance on the module plane (W/m²), isotropic sky."""
    ghi = max(irr.ghi, 0.0)
    if ghi <= 0:
        return 0.0
    dhi = irr.dhi if irr.dhi is not None else erbs_diffuse(ghi, cos_z, t)
    dhi = min(max(dhi, 0.0), ghi)
    tilt = math.radians(arr.tilt)
    beam = 0.0
    if cos_z > MIN_COS_ZENITH:
        dni = (ghi - dhi) / cos_z
        cos_inc = cos_z * math.cos(tilt) + math.sqrt(max(0.0, 1 - cos_z * cos_z)) * math.sin(
            tilt
        ) * math.cos(math.radians(sun_az - arr.azimuth))
        beam = dni * max(cos_inc, 0.0)
    else:
        dhi = ghi
    sky = dhi * (1 + math.cos(tilt)) / 2
    ground = ghi * ALBEDO * (1 - math.cos(tilt)) / 2
    return beam + sky + ground


@dataclass(frozen=True, slots=True)
class PvSystem:
    """One or more PV arrays at a location (``lat``/``lon`` stay in your local settings)."""

    lat: float
    lon: float
    arrays: tuple[PvArray, ...]
    losses_pct: float = 14.0

    def __post_init__(self) -> None:
        if not self.arrays:
            raise PvError("at least one PV array is needed")
        if not -90 <= self.lat <= 90 or not -180 <= self.lon <= 180:
            raise PvError("latitude/longitude out of range")
        if not 0 <= self.losses_pct < 100:
            raise PvError("PV losses must be in 0–100 %")

    @property
    def kwp(self) -> float:
        return sum(a.kwp for a in self.arrays)

    def quarter_kwh(self, start: datetime, irr: Irradiance) -> float:
        """Energy (kWh, after losses) of the quarter-hour starting at ``start``."""
        if irr.ghi is None or irr.ghi <= 0:
            return 0.0
        mid = start + timedelta(minutes=7.5)
        cz, az = sun_position(mid, self.lat, self.lon)
        if cz <= 0:
            return 0.0
        total = 0.0
        temp = 20.0 if irr.temp_c is None else irr.temp_c
        for arr in self.arrays:
            g = plane_of_array(irr, cz, az, arr, mid)
            t_cell = temp + g * (NOCT_C - 20) / 800
            total += arr.kwp * g / 1000 * max(0.0, 1 + TEMP_COEFF * (t_cell - 25))
        return max(0.0, total * (1 - self.losses_pct / 100) * DT_H)

    def energy(self, irr: dict[datetime, Irradiance]) -> dict[datetime, float]:
        return {t: self.quarter_kwh(t, v) for t, v in irr.items()}

    def describe(self) -> str:
        arrays = "; ".join(a.describe() for a in self.arrays)
        return f"{self.kwp:g} kWp ({arrays}), losses {self.losses_pct:g} %"
