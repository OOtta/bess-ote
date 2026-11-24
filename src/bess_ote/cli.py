"""Command line: ``bess-ote plan`` and ``bess-ote backtest``."""

from __future__ import annotations

import argparse
import contextlib
import csv
import json
import math
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from time import monotonic
from typing import Any

from bess_ote import __version__
from bess_ote.backtest import REPLAN_MODES, BacktestResult, PvSetup, run_backtest
from bess_ote.battery import DT_H, Battery, BatteryError
from bess_ote.breakdown import FINE, Quarter, fine_rows, normalise
from bess_ote.cache import JsonCache, default_cache_dir
from bess_ote.cnb import CnbRates
from bess_ote.config import Config, ConfigError, load_config
from bess_ote.http import FetchError, make_session
from bess_ote.lowtariff import LowTariffError, LowTariffSchedule
from bess_ote.market import PriceBook, ceil_quarter, default_publish_time
from bess_ote.ote import OteClient
from bess_ote.planner import PlanResult, TerminalConfig, plan
from bess_ote.pv import PvArray, PvError, PvSystem
from bess_ote.pvdata import Chmi, OpenMeteo, PvForecast, Run, runs_between
from bess_ote.tariff import Distribution, SpotFormula, Tariffs
from bess_ote.timeutil import (
    PRAGUE,
    QUARTER,
    local_date,
    local_midnight_utc,
    local_today,
    to_local,
)


class CliError(Exception):
    """User-facing error; printed without a traceback."""


# ------------------------------------------------------------------------------------------
# output state (--json: one JSON document on stdout, nothing on stderr)
# ------------------------------------------------------------------------------------------
class _Run:
    json = False
    warnings: list[str] = []  # noqa: RUF012


_RUN = _Run()


def warn(msg: str) -> None:
    if msg not in _RUN.warnings:
        _RUN.warnings.append(msg)
        if not _RUN.json:
            print(f"warning: {msg}", file=sys.stderr)


def info(msg: str) -> None:
    if not _RUN.json:
        print(msg, file=sys.stderr)


def dump_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def ensure_unicode_stdio() -> None:
    """Switch stdout/stderr to UTF-8 when they cannot encode Czech text (Windows cp1252)."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            "čČřŘ–€→".encode(getattr(stream, "encoding", None) or "ascii")
        except (UnicodeEncodeError, LookupError):
            with contextlib.suppress(ValueError, OSError):
                stream.reconfigure(encoding="utf-8", errors="replace")


# ------------------------------------------------------------------------------------------
# argument parsing
# ------------------------------------------------------------------------------------------
def _hhmm(text: str) -> time:
    try:
        return datetime.strptime(text.strip(), "%H:%M").time()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid time {text!r} (use HH:MM)") from exc


def _breakdown_type(*allowed: str):  # type: ignore[no-untyped-def]
    def parse(text: str) -> str:
        try:
            by = normalise(text)
        except ValueError:
            by = ""
        if by not in allowed:
            raise argparse.ArgumentTypeError(
                f"choose from {', '.join(allowed)} (aliases: d, h/60min, qh/15min/quarter)"
            )
        return by

    return parse


def _jobs(text: str) -> int | None:
    if text.strip().lower() == "auto":
        return None
    try:
        n = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("expected a number ≥ 1 or 'auto'") from None
    if n < 1:
        raise argparse.ArgumentTypeError("expected a number ≥ 1 or 'auto'")
    return n


def common_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    b = p.add_argument_group("battery")
    b.add_argument("-c", "--capacity", type=float, metavar="KWH", help="usable capacity (kWh)")
    b.add_argument("-p", "--power", type=float, metavar="KW", help="max charging power (kW, AC)")
    b.add_argument(
        "--discharge-power",
        type=float,
        metavar="KW",
        help="max discharging power (kW, AC); default = --power",
    )
    b.add_argument(
        "--efficiency",
        type=float,
        metavar="PCT",
        help="round-trip efficiency in %% (default 90; split evenly)",
    )
    b.add_argument(
        "--charge-efficiency",
        type=float,
        metavar="PCT",
        help="grid → battery efficiency in %% (overrides --efficiency)",
    )
    b.add_argument(
        "--discharge-efficiency",
        type=float,
        metavar="PCT",
        help="battery → grid efficiency in %% (overrides --efficiency)",
    )
    b.add_argument("--soc-min", type=float, metavar="PCT", help="minimum SoC %% (default 0)")
    b.add_argument("--soc-max", type=float, metavar="PCT", help="maximum SoC %% (default 100)")
    b.add_argument(
        "--soc",
        type=float,
        metavar="PCT",
        help="current (plan) / initial (backtest) SoC %% (default = --soc-min)",
    )
    b.add_argument(
        "--cycle-cost",
        type=float,
        metavar="CZK_KWH",
        help="wear cost per kWh discharged to the grid (default 0)",
    )
    b.add_argument(
        "--terminal",
        choices=["value", "zero", "min-soc"],
        help="energy left at the horizon end: value it (default), worth nothing, "
        "or require SoC ≥ --terminal-soc",
    )
    b.add_argument(
        "--terminal-price",
        type=float,
        metavar="CZK_KWH",
        help="sale price for leftover energy (default: average sale price of the last known day)",
    )
    b.add_argument(
        "--terminal-soc",
        type=float,
        metavar="PCT",
        help="min. end SoC %% for --terminal min-soc (default = --soc)",
    )
    b.add_argument(
        "--soc-step",
        type=float,
        metavar="KWH",
        help="optimiser SoC resolution (default: full-power quarter / K, at most capacity/400 "
        "– full power stays exact)",
    )

    t = p.add_argument_group("purchase tariff (nákup): spot × multiplier + add")
    t.add_argument("-m", "--buy-multiplier", type=float, metavar="X", help="default 1")
    t.add_argument("-a", "--buy-add", type=float, metavar="CZK_MWH", help="surcharge, default 0")
    t.add_argument(
        "--buy-negative-multiplier",
        type=float,
        metavar="X",
        help="multiplier for negative spot prices (default = --buy-multiplier)",
    )
    t.add_argument(
        "--buy-negative-add",
        type=float,
        metavar="CZK_MWH",
        help="surcharge for negative spot prices (default = --buy-add)",
    )
    t.add_argument(
        "--buy-negative",
        choices=["as-is", "clamp-spot", "clamp-total"],
        help="negative prices: as-is (default), clamp-spot, clamp-total",
    )
    t.add_argument(
        "--buy-vat",
        type=float,
        metavar="PCT",
        help="VAT %% on purchase incl. distribution and monthly fees (default 0 = prices "
        "excl. VAT)",
    )
    t.add_argument(
        "--buy-monthly-fee",
        type=float,
        metavar="CZK",
        help="supplier's fixed fee CZK/month excl. VAT (prorated by days; does not change "
        "decisions), default 0",
    )

    s = p.add_argument_group("sale tariff (výkup): spot × multiplier + add")
    s.add_argument("--sell-multiplier", type=float, metavar="X", help="default 1")
    s.add_argument(
        "--sell-add",
        type=float,
        metavar="CZK_MWH",
        help="surcharge, usually negative = supplier fee (default 0)",
    )
    s.add_argument("--sell-negative-multiplier", type=float, metavar="X")
    s.add_argument("--sell-negative-add", type=float, metavar="CZK_MWH")
    s.add_argument("--sell-negative", choices=["as-is", "clamp-spot", "clamp-total"])
    s.add_argument(
        "--sell-vat",
        type=float,
        metavar="PCT",
        help="VAT %% added to sale revenue (VAT payers; default 0)",
    )

    d = p.add_argument_group("distribution (per-kWh rates charged on purchased energy only)")
    d.add_argument(
        "--distribution-high",
        type=float,
        metavar="CZK_KWH",
        help="VT rate (or the only rate) excl. VAT, default 0",
    )
    d.add_argument(
        "--distribution-low",
        type=float,
        metavar="CZK_KWH",
        help="NT rate excl. VAT (needs --low-tariff-hours)",
    )
    d.add_argument(
        "--distribution-monthly-fee",
        type=float,
        metavar="CZK",
        help="fixed distribution payments CZK/month excl. VAT (breaker, reserved capacity; "
        "prorated by days, follows --buy-vat), default 0",
    )
    d.add_argument(
        "--purchase-fee",
        type=float,
        metavar="CZK_KWH",
        help="other per-kWh charges on purchase excl. VAT (system services, POZE, "
        "tax …), default 0",
    )
    d.add_argument(
        "--low-tariff-hours", metavar="RANGES", help='NT windows, e.g. "1:15-5:15,12:30-16:30"'
    )
    d.add_argument(
        "--low-tariff-hours-weekend",
        metavar="RANGES",
        help="NT windows on Saturday/Sunday (default = --low-tariff-hours)",
    )
    d.add_argument(
        "--holidays",
        choices=["weekday", "weekend"],
        help="public holidays use their weekday (default) or the weekend windows",
    )

    v = p.add_argument_group("PV plant (photovoltaics; location stays in your local config)")
    v.add_argument(
        "--pv-array",
        action="append",
        metavar="AZ,TILT,KWP",
        help="PV array: azimuth ° (0 = south, −90 = east, 90 = west), tilt ° (0 = flat), kWp; "
        "repeat for more arrays, e.g. --pv-array 0,35,5 --pv-array=-90,20,2 "
        "(use = when the value starts with −)",
    )
    v.add_argument("--lat", type=float, metavar="DEG", help="PV location latitude")
    v.add_argument("--lon", type=float, metavar="DEG", help="PV location longitude")
    v.add_argument("--pv-losses", type=float, metavar="PCT", help="system losses %% (default 14)")
    v.add_argument(
        "--inverter-kw",
        type=float,
        metavar="KW",
        help="shared hybrid-inverter AC limit for grid import and export (PV + battery); "
        "default with PV = max(--power, --discharge-power)",
    )

    g = p.add_argument_group("general")
    g.add_argument(
        "--publish-time",
        type=_hhmm,
        metavar="HH:MM",
        help="when OTE prices for D are known on D−1 (default 12:57 for delivery "
        "until 2025-09-30, 13:05 from 2025-10-01)",
    )
    g.add_argument("--config", metavar="FILE", help="TOML config (env BESS_OTE_CONFIG)")
    g.add_argument("--cache-dir", metavar="DIR", help="cache dir (default ~/.cache/bess-ote)")
    g.add_argument("--no-cache", action="store_true", help="do not read or write the cache")
    g.add_argument(
        "--refresh-cache", action="store_true", help="download prices again and update the cache"
    )
    g.add_argument("--json", action="store_true", help="print one JSON document to stdout")
    return p  # fmt: skip


def build_parser() -> argparse.ArgumentParser:
    common = common_parser()
    parser = argparse.ArgumentParser(
        prog="bess-ote",
        description="BESS OTE kalkulačka: when to charge, discharge or wait with a home "
        "battery on OTE day-ahead prices, and a backtest on historical prices.",
        epilog="examples:\n"
        "  bess-ote plan -c 10 -p 5 --soc 40 --next\n"
        "  bess-ote plan -c 10 -p 5 --at '2026-10-14 09:00' -o plan.csv\n"
        "  bess-ote backtest -c 10 -p 5 --from 2026-06 --to 2026-10 -a 400 --sell-add -300 "
        "--distribution-high 2.435 --distribution-low 0.416 "
        '--low-tariff-hours "1:15-5:15,12:30-16:30"',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    pl = sub.add_parser(
        "plan",
        parents=[common],
        help="optimal actions from now over all published prices",
        description="Optimal charge/discharge/wait schedule from the next quarter-hour until "
        "the end of the OTE prices published at that moment (today, and tomorrow after ~13:05).",
    )
    pl.add_argument(
        "--at",
        metavar="TIME",
        help="plan as of this local time ('YYYY-MM-DD HH:MM'); only prices published "
        "by then are used. Default: now, with whatever OTE has published.",
    )
    pl.add_argument("--next", action="store_true", help="show only the next quarter-hour action")
    pl.add_argument("-o", "--export", metavar="FILE", help="write the schedule (.csv or .json)")
    pl.add_argument(
        "-b",
        "--breakdown",
        type=_breakdown_type("hour", "quarter-hour"),
        default=None,
        metavar="BY",
        help="hour = aggregate the table per hour (sums of energy/money, average prices); "
        "quarter-hour = native rows (default). Adds 'breakdown' to JSON",
    )
    pl.add_argument("--breakdown-csv", metavar="FILE", help="write the breakdown rows as CSV")
    pl.set_defaults(func=cmd_plan)

    bt = sub.add_parser(
        "backtest",
        parents=[common],
        help="simulate the planner on historical prices",
        description="Rolling simulation: at every quarter-hour only prices published by then "
        "are known; the same optimiser as `plan` decides, the first action is executed.",
    )
    bt.add_argument(
        "--from",
        dest="start",
        required=True,
        metavar="DATE",
        help="first day (YYYY-MM-DD or YYYY-MM)",
    )
    bt.add_argument(
        "--to",
        dest="end",
        metavar="DATE",
        help="last day incl. (YYYY-MM-DD or YYYY-MM = whole month); default = --from",
    )
    bt.add_argument(
        "-b",
        "--breakdown",
        type=_breakdown_type("month", "day", "hour", "quarter-hour", "none"),
        default="none",
        metavar="BY",
        help="breakdown rows: month, day, hour (h, 60min), quarter-hour (qh, 15min); default "
        "none = summary only",
    )
    bt.add_argument("--breakdown-csv", metavar="FILE", help="write the breakdown rows as CSV")
    bt.add_argument(
        "--no-foresight", action="store_true", help="skip the perfect-foresight benchmark"
    )
    bt.add_argument(
        "--replan",
        choices=REPLAN_MODES,
        help="with PV: re-plan only at OTE publications (publication), also at every new "
        "forecast run (forecast, default) or every quarter-hour using the actual production "
        "(quarter)",
    )
    bt.add_argument(
        "-j",
        "--jobs",
        type=_jobs,
        default=None,
        metavar="N",
        help="parallel processes for the independent optimisations (default auto = CPU "
        "count; 1 = serial). Results are identical for any N",
    )
    bt.add_argument(
        "--no-progress",
        action="store_true",
        help="no progress bar (shown only on a terminal and without --json)",
    )
    bt.add_argument(
        "-o", "--export", metavar="FILE", help="intervals as .csv, or everything as .json"
    )
    bt.set_defaults(func=cmd_backtest)
    return parser  # fmt: skip


# ------------------------------------------------------------------------------------------
# settings
# ------------------------------------------------------------------------------------------
def _pick(args: argparse.Namespace, attr: str, cfg: Config, section: str, key: str,
          default: Any = None) -> Any:  # fmt: skip
    v = getattr(args, attr, None)
    if v is None:
        v = cfg.get(section, key)
    return default if v is None else v


def _num(v: Any, what: str) -> float:
    try:
        return float(v)
    except (TypeError, ValueError) as exc:
        raise CliError(f"{what} must be a number, got {v!r}") from exc


def pv_arrays(args: argparse.Namespace, cfg: Config) -> tuple[PvArray, ...]:
    try:
        if getattr(args, "pv_array", None):
            return tuple(PvArray.parse(a) for a in args.pv_array)
        raw = cfg.get("pv", "arrays") or []
        out = []
        for a in raw:
            if isinstance(a, dict):
                out.append(PvArray(float(a["azimuth"]), float(a["tilt"]), float(a["kwp"])))
            elif isinstance(a, (list, tuple)) and len(a) == 3:
                out.append(PvArray(*map(float, a)))
            else:
                raise PvError(f"[pv] arrays: use [azimuth, tilt, kwp] or tables, got {a!r}")
        return tuple(out)
    except (PvError, KeyError, TypeError, ValueError) as exc:
        raise CliError(f"PV: {exc}") from exc


def build_pv(args: argparse.Namespace, cfg: Config) -> PvSystem | None:
    arrays = pv_arrays(args, cfg)
    if not arrays:
        return None
    lat, lon = _pick(args, "lat", cfg, "pv", "lat"), _pick(args, "lon", cfg, "pv", "lon")
    if lat is None or lon is None:
        raise CliError("PV needs the location: --lat/--lon or lat/lon in the [pv] section of "
                       "your local config file")  # fmt: skip
    try:
        return PvSystem(
            _num(lat, "latitude"),
            _num(lon, "longitude"),
            arrays,
            _num(_pick(args, "pv_losses", cfg, "pv", "losses", 14.0), "PV losses"),
        )
    except PvError as exc:
        raise CliError(f"PV: {exc}") from exc


def build_battery(args: argparse.Namespace, cfg: Config) -> Battery:
    cap = _pick(args, "capacity", cfg, "battery", "capacity_kwh")
    power = _pick(args, "power", cfg, "battery", "charge_kw")
    if cap is None or power is None:
        raise CliError("Battery capacity (-c KWH) and power (-p KW) are required.")
    rt = _num(_pick(args, "efficiency", cfg, "battery", "efficiency", 90.0), "efficiency")
    ce = _pick(args, "charge_efficiency", cfg, "battery", "charge_efficiency")
    de = _pick(args, "discharge_efficiency", cfg, "battery", "discharge_efficiency")
    for name, v in (("efficiency", rt), ("charge efficiency", ce), ("discharge efficiency", de)):
        if v is not None and not 1 <= float(v) <= 100:
            raise CliError(f"{name} is in percent (1–100), got {v}")
    one_way = math.sqrt(rt / 100)
    dp = _pick(args, "discharge_power", cfg, "battery", "discharge_kw")
    step = _pick(args, "soc_step", cfg, "battery", "soc_step_kwh")
    inv = _pick(args, "inverter_kw", cfg, "pv", "inverter_kw")
    if inv is None and pv_arrays(args, cfg):
        inv = max(_num(power, "power"), _num(power if dp is None else dp, "discharge power"))
    try:
        return Battery(
            capacity_kwh=_num(cap, "capacity"),
            charge_kw=_num(power, "power"),
            discharge_kw=None if dp is None else _num(dp, "discharge power"),
            charge_eff=float(ce) / 100 if ce is not None else one_way,
            discharge_eff=float(de) / 100 if de is not None else one_way,
            soc_min_pct=_num(_pick(args, "soc_min", cfg, "battery", "soc_min", 0.0), "soc min"),
            soc_max_pct=_num(_pick(args, "soc_max", cfg, "battery", "soc_max", 100.0), "soc max"),
            cycle_cost_czk_kwh=_num(
                _pick(args, "cycle_cost", cfg, "battery", "cycle_cost_czk_kwh", 0.0), "cycle cost"
            ),
            soc_step_kwh=None if step is None else _num(step, "soc step"),
            inverter_kw=None if inv is None else _num(inv, "inverter limit"),
        )
    except BatteryError as exc:
        raise CliError(str(exc)) from exc


def initial_soc_kwh(args: argparse.Namespace, cfg: Config, b: Battery) -> float:
    pct = _num(_pick(args, "soc", cfg, "battery", "soc", b.soc_min_pct), "soc")
    if not b.soc_min_pct <= pct <= b.soc_max_pct:
        raise CliError(f"--soc {pct:g} % is outside {b.soc_min_pct:g}–{b.soc_max_pct:g} %")
    return b.capacity_kwh * pct / 100


def build_terminal(args: argparse.Namespace, cfg: Config, b: Battery) -> TerminalConfig:
    mode = _pick(args, "terminal", cfg, "battery", "terminal", "value")
    if mode not in ("value", "zero", "min-soc"):
        raise CliError(f"terminal must be value, zero or min-soc, got {mode!r}")
    price = _pick(args, "terminal_price", cfg, "battery", "terminal_price_czk_kwh")
    tsoc = _pick(args, "terminal_soc", cfg, "battery", "terminal_soc")
    if mode == "min-soc" and tsoc is None:
        tsoc = _num(_pick(args, "soc", cfg, "battery", "soc", b.soc_min_pct), "soc")
    return TerminalConfig(
        mode, None if price is None else _num(price, "terminal price"),
        None if tsoc is None else _num(tsoc, "terminal soc"),
    )  # fmt: skip


def _formula(args: argparse.Namespace, cfg: Config, side: str) -> SpotFormula:
    def g(attr: str, key: str, default: Any = None) -> Any:
        return _pick(args, f"{side}_{attr}", cfg, side, key, default)

    neg_m, neg_a = (
        g("negative_multiplier", "negative_multiplier"),
        g("negative_add", "negative_add_czk_mwh"),
    )
    mode = g("negative", "negative", "as-is")
    if mode not in ("as-is", "clamp-spot", "clamp-total"):
        raise CliError(f"[{side}] negative must be as-is, clamp-spot or clamp-total")
    return SpotFormula(
        multiplier=_num(g("multiplier", "multiplier", 1.0), f"{side} multiplier"),
        add_czk_mwh=_num(g("add", "add_czk_mwh", 0.0), f"{side} add"),
        negative=mode,
        negative_multiplier=None if neg_m is None else _num(neg_m, "negative multiplier"),
        negative_add_czk_mwh=None if neg_a is None else _num(neg_a, "negative add"),
    )  # fmt: skip


def build_tariffs(args: argparse.Namespace, cfg: Config) -> Tariffs:
    hours = _pick(args, "low_tariff_hours", cfg, "low_tariff", "hours")
    weekend = _pick(args, "low_tariff_hours_weekend", cfg, "low_tariff", "hours_weekend")
    holidays = _pick(args, "holidays", cfg, "low_tariff", "holidays", "weekday")
    schedule = None
    if hours:
        try:
            schedule = LowTariffSchedule.parse(str(hours), weekend, holidays)
        except LowTariffError as exc:
            raise CliError(f"low-tariff hours: {exc}") from exc
    elif weekend:
        raise CliError("--low-tariff-hours-weekend needs --low-tariff-hours")
    low = _pick(args, "distribution_low", cfg, "distribution", "low_czk_kwh")
    if low is not None and schedule is None:
        raise CliError("--distribution-low needs --low-tariff-hours (when is NT?)")
    dist = Distribution(
        high_czk_kwh=_num(_pick(args, "distribution_high", cfg, "distribution", "high_czk_kwh",
                                0.0), "distribution high"),
        low_czk_kwh=None if low is None else _num(low, "distribution low"),
        schedule=schedule,
        other_czk_kwh=_num(_pick(args, "purchase_fee", cfg, "distribution", "other_czk_kwh",
                                 0.0), "purchase fee"),
    )  # fmt: skip
    return Tariffs(
        buy=_formula(args, cfg, "buy"),
        sell=_formula(args, cfg, "sell"),
        distribution=dist,
        buy_vat_percent=_num(_pick(args, "buy_vat", cfg, "buy", "vat", 0.0), "buy VAT"),
        sell_vat_percent=_num(_pick(args, "sell_vat", cfg, "sell", "vat", 0.0), "sell VAT"),
        buy_monthly_fee=_num(
            _pick(args, "buy_monthly_fee", cfg, "buy", "monthly_fee", 0.0), "buy monthly fee"
        ),
        distribution_monthly_fee=_num(
            _pick(args, "distribution_monthly_fee", cfg, "distribution", "monthly_fee", 0.0),
            "distribution monthly fee",
        ),
    )


def publish_time(args: argparse.Namespace, cfg: Config) -> time | None:
    if args.publish_time is not None:
        return args.publish_time
    v = cfg.get("market", "publish_time")
    if v is None:
        return None
    try:
        return _hhmm(str(v))
    except argparse.ArgumentTypeError as exc:
        raise CliError(f"[market] publish_time: {exc}") from exc


def make_cache(args: argparse.Namespace) -> JsonCache:
    directory = None if args.no_cache else (
        Path(args.cache_dir).expanduser() if args.cache_dir else default_cache_dir()
    )  # fmt: skip
    return JsonCache(directory, warn=warn, refresh=args.refresh_cache)


def ote_client(args: argparse.Namespace) -> OteClient:
    session = make_session()
    cache = make_cache(args)
    return OteClient(session, cache, CnbRates(session, cache))


def prefetch_runs(om: OpenMeteo, runs: list[Run], threads: int = 3) -> None:
    """Download the forecast runs a backtest needs concurrently (cached afterwards)."""
    todo = [r for r in runs if r not in om._mem]
    if not todo:
        return
    with ThreadPoolExecutor(max_workers=threads) as pool:
        list(pool.map(om.run, todo))


def fetch_book(ote: OteClient, start: date, end: date) -> PriceBook:
    points = ote.prices(start, end, "auto")
    st = ote.stats
    info(
        f"OTE prices {start} – {end}: {st.cached_days} day(s) from cache, "
        f"{st.downloaded_days} downloaded ({st.requests} request(s))"
    )
    for d, n in sorted(ote.unpriced.items()):
        warn(f"OTE published no price for {n} period(s) on {d}; the battery waits then.")
    return PriceBook.from_points(points)


def parse_at(text: str) -> datetime:
    text = text.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=PRAGUE).astimezone(UTC)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise CliError(f"invalid --at {text!r} (use 'YYYY-MM-DD HH:MM', local time)") from exc
    return (dt if dt.tzinfo else dt.replace(tzinfo=PRAGUE)).astimezone(UTC)


def parse_day(text: str, *, end: bool = False) -> date:
    try:
        if len(text) == 7:
            y, m = map(int, text.split("-"))
            if end:
                nxt = date(y + (m == 12), m % 12 + 1, 1)
                return nxt - timedelta(days=1)
            return date(y, m, 1)
        return date.fromisoformat(text)
    except ValueError as exc:
        raise CliError(f"invalid date {text!r} (use YYYY-MM-DD or YYYY-MM)") from exc


def fmt_local(dt: datetime) -> str:
    return to_local(dt).strftime("%Y-%m-%d %H:%M %Z")


def _r(v: float, nd: int = 4) -> float | None:
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else round(v, nd)


# ------------------------------------------------------------------------------------------
# plan
# ------------------------------------------------------------------------------------------
def plan_rows(res: PlanResult, b: Battery, tariffs: Tariffs) -> list[dict[str, Any]]:
    rows = []
    prev = res.soc_kwh
    for t, spot, buy, sell, st in zip(res.problem.starts, res.problem.spot, res.problem.buy,
                                      res.problem.sell, res.steps, strict=True):  # fmt: skip
        rows.append({
            "start": to_local(t).isoformat(),
            "spot_czk_mwh": _r(spot, 2),
            "buy_czk_kwh": _r(buy),
            "sell_czk_kwh": _r(sell),
            "action": st.action,
            "power_kw": round(st.power_kw, 3),
            "grid_kwh": round(st.grid_kwh, 4),
            "soc_start_pct": round(b.pct(prev), 2),
            "soc_end_pct": round(b.pct(st.soc_kwh), 2),
            "cash_czk": round(st.cash_czk - st.wear_czk, 4),
            "fees_czk": round(tariffs.fees_for_quarter(t), 4),
        } | (_pv_cols(st) if res.problem.pv is not None else {}))  # fmt: skip
        prev = st.soc_kwh
    return rows


def _pv_cols(st: Any) -> dict[str, float]:
    return {
        "pv_kwh": round(st.pv_kwh, 4), "pv_battery_kwh": round(st.pv_battery_kwh, 4),
        "pv_grid_kwh": round(st.pv_grid_kwh, 4), "pv_curtailed_kwh": round(st.pv_curtailed_kwh, 4),
    }  # fmt: skip


def _summary(steps: list, fees: float, pv: bool = False) -> dict[str, float]:
    bought = sum(s.grid_kwh for s in steps if s.grid_kwh > 0)
    sold = -sum(s.grid_kwh for s in steps if s.grid_kwh < 0)
    cost = -sum(s.cash_czk for s in steps if s.grid_kwh > 0)
    rev = sum(s.cash_czk for s in steps if s.grid_kwh < 0)
    wear = sum(s.wear_czk for s in steps)
    return {
        "bought_kwh": round(bought, 4), "cost_czk": round(cost, 2),
        "sold_kwh": round(sold, 4), "revenue_czk": round(rev, 2),
        "wear_czk": round(wear, 2), "profit_czk": round(rev - cost - wear, 2),
        "fees_czk": round(fees, 2), "net_profit_czk": round(rev - cost - wear - fees, 2),
    } | ({
        "pv_kwh": round(sum(s.pv_kwh for s in steps), 4),
        "pv_battery_kwh": round(sum(s.pv_battery_kwh for s in steps), 4),
        "pv_grid_kwh": round(sum(s.pv_grid_kwh for s in steps), 4),
        "pv_curtailed_kwh": round(sum(s.pv_curtailed_kwh for s in steps), 4),
    } if pv else {})  # fmt: skip


def describe_next(st, start: datetime, b: Battery, soc0: float) -> str:  # type: ignore[no-untyped-def]
    span = f"{to_local(start):%H:%M}–{to_local(start + QUARTER):%H:%M}"
    soc = f"SoC {b.pct(soc0):.1f} % → {b.pct(st.soc_kwh):.1f} %"
    if st.action == "charge":
        return f"{span}  CHARGE {st.power_kw:.2f} kW ({st.grid_kwh:.3f} kWh from grid), {soc}"
    if st.action == "discharge":
        return f"{span}  DISCHARGE {st.power_kw:.2f} kW ({-st.grid_kwh:.3f} kWh to grid), {soc}"
    return f"{span}  WAIT, {soc}"


def cmd_plan(args: argparse.Namespace) -> int:
    cfg = load_config(Path(args.config) if args.config else None)
    for w in cfg.warnings:
        warn(w)
    b = build_battery(args, cfg)
    tariffs = build_tariffs(args, cfg)
    soc = initial_soc_kwh(args, cfg, b)
    tcfg = build_terminal(args, cfg, b)
    pub = publish_time(args, cfg)
    live = args.at is None
    at = datetime.now(UTC) if live else parse_at(args.at)
    start = ceil_quarter(at)
    d0 = local_date(start)
    if not live and d0 >= local_today() + timedelta(days=2):
        raise CliError("--at is too far in the future: no prices can be published yet.")
    book = fetch_book(ote_client(args), d0, d0 + timedelta(days=1))
    system = build_pv(args, cfg)
    pvf, pv_doc = (None, None) if system is None else plan_pv(args, system, at, live)
    res = plan(book, at, soc, tariffs, b, tcfg, publish=pub, use_publication_rule=not live,
               pv=pvf)  # fmt: skip
    if not res.steps:
        raise CliError(f"No OTE prices known for {fmt_local(start)} (as of {fmt_local(at)}).")
    if abs(res.soc_kwh - soc) > 1e-9:
        info(f"SoC rounded to the optimiser grid: {b.pct(res.soc_kwh):.2f} %")
    rows = plan_rows(res, b, tariffs)
    summary = _summary(res.steps, sum(tariffs.fees_for_quarter(t) for t in res.problem.starts),
                       res.problem.pv is not None)  # fmt: skip
    end = res.problem.starts[-1] + QUARTER
    term = res.problem.terminal
    doc: dict[str, Any] = {
        "at": to_local(at).isoformat(timespec="seconds"),
        "start": to_local(start).isoformat(),
        "horizon_end": to_local(end).isoformat(),
        "intervals_in_horizon": len(rows),
        "prices": "live (as published by OTE now)" if live else
        "published by --at (publication rule)",
        "publish_time": (pub.strftime("%H:%M") if pub else "default (12:57 / 13:05)"),
        "battery": b.describe(),
        "tariffs": tariffs.describe(),
        "soc_start_pct": round(b.pct(res.soc_kwh), 2),
        "terminal": {"mode": term.mode, "czk_per_kwh_stored": round(term.czk_per_kwh_stored, 4),
                     "min_soc_pct": round(b.pct(term.min_kwh), 2) if term.mode == "min-soc"
                     else None},
        "next": rows[0],
        "summary": summary | {"soc_end_pct": rows[-1]["soc_end_pct"]},
    }  # fmt: skip
    if pv_doc is not None:
        doc["pv"] = pv_doc
    by = getattr(args, "breakdown", None)
    fine: list[dict[str, Any]] | None = None
    if by:
        fine = fine_rows(plan_quarters(res, tariffs), b, by)
        doc["breakdown"] = {"by": by, "rows": fine}
    if args.breakdown_csv:
        write_csv(Path(args.breakdown_csv), fine if fine is not None else rows)
        info(f"Wrote {args.breakdown_csv}")
    if not args.next:
        doc["intervals"] = rows
    if args.export:
        export_rows(Path(args.export), rows, doc | {"intervals": rows})
        info(f"Wrote {args.export}")
    if args.json:
        print(dump_json(doc | {"warnings": list(_RUN.warnings)}))
        return 0
    print(f"Plan as of {fmt_local(at)}: {fmt_local(start)} – {fmt_local(end)} "
          f"({len(rows)} quarter-hours)")  # fmt: skip
    print(f"NEXT {describe_next(res.steps[0], start, b, res.soc_kwh)}")
    if args.next:
        return 0
    print(f"Battery:      {b.describe()}")
    for k, v in tariffs.describe().items():
        print(f"{k.capitalize() + ':':<13} {v}")
    print(f"Terminal:     {_terminal_text(term, b)}")
    if pv_doc is not None:
        print(f"PV:           {pv_doc['system']}; {pv_doc.get('forecast', '')}")
    print()
    if by == "hour" and fine is not None:
        render_fine(fine, "hour", bool(summary["fees_czk"]))
    else:
        _plan_table(rows, res)
    print()
    print(f"Horizon total: bought {summary['bought_kwh']:.2f} kWh for {summary['cost_czk']:.2f} "
          f"CZK, sold {summary['sold_kwh']:.2f} kWh for {summary['revenue_czk']:.2f} CZK, "
          f"wear {summary['wear_czk']:.2f} CZK → profit {summary['profit_czk']:.2f} CZK; "
          f"SoC at the end {rows[-1]['soc_end_pct']:.1f} %")  # fmt: skip
    if pv_doc is not None:
        print(f"PV forecast {summary['pv_kwh']:.2f} kWh: → battery "
              f"{summary['pv_battery_kwh']:.2f}, → grid {summary['pv_grid_kwh']:.2f}, curtailed "
              f"{summary['pv_curtailed_kwh']:.2f} kWh")  # fmt: skip
    if summary["fees_czk"]:
        print(f"Monthly fees prorated to the horizon: {summary['fees_czk']:.2f} CZK → net "
              f"{summary['net_profit_czk']:.2f} CZK")  # fmt: skip
    return 0


def plan_pv(args: argparse.Namespace, system: PvSystem, at: datetime,
            live: bool) -> tuple[Any, dict[str, Any]]:  # fmt: skip
    """PV forecast function for ``plan``: live forecast now, or the run known at ``--at``."""
    om = OpenMeteo(make_session(), make_cache(args), system.lat, system.lon)
    doc: dict[str, Any] = {"system": system.describe()}
    if live:
        energy = system.energy(om.live())
        doc["forecast"] = "Open-Meteo live forecast (15 min)"

        def live_pv(starts: list[datetime]) -> list[float]:
            return [energy.get(t, 0.0) for t in starts]

        return live_pv, doc
    fc = PvForecast(om, system)

    def hist_pv(starts: list[datetime]) -> list[float]:
        vals, run = fc.at(at, starts)
        doc["forecast"] = f"Open-Meteo run {run.label}" if run else "no forecast run available"
        if fc.uncovered:
            warn(f"PV forecast missing for {fc.uncovered} quarter-hour(s); counted as 0.")
        return vals

    return hist_pv, doc


def plan_quarters(res: PlanResult, tariffs: Tariffs) -> list[Quarter]:
    out, soc = [], res.soc_kwh
    p = res.problem
    for t, spot, buy, sell, st in zip(p.starts, p.spot, p.buy, p.sell, res.steps, strict=True):
        dist = tariffs.buy_distribution_czk_kwh(t, QUARTER)
        out.append(Quarter(t, float(spot), float(buy), float(sell), st, soc,
                           tariffs.fees_for_quarter(t), dist,
                           tariffs.low_tariff_fraction(t, QUARTER),
                           None if p.pv is None else st.pv_kwh))  # fmt: skip
        soc = st.soc_kwh
    return out


def render_fine(rows: list[dict[str, Any]], by: str, fees: bool) -> None:
    """Text table of hour / quarter-hour rows (times local, with CET/CEST)."""
    extra = f" {'fees':>8} {'net':>9}" if fees else ""
    head = "acts" if by == "hour" else ""
    print(f"{by:<21} {head:<4} {'action':<17} {'spot':>8} {'buy':>7} {'dist':>7} {'':<5} "
          f"{'sell':>7} {'bought':>8} {'sold':>8} {'SoC %':>6} {'profit':>9}{extra}")  # fmt: skip
    print(f"{'(local)':<21} {'':<4} {'':<17} {'CZK/MWh':>8} {'CZK/kWh':>7} {'in buy':>7} "
          f"{'':<5} {'CZK/kWh':>7} {'kWh':>8} {'kWh':>8} {'end':>6} {'CZK':>9}")  # fmt: skip

    def num(v: float | None, spec: str) -> str:
        return "–" if v is None else format(v, spec)

    for r in rows:
        t = datetime.fromisoformat(r["period"])
        pat = r["pattern"] if by == "hour" else ""
        extra = f" {r['fees_czk']:>8.3f} {r['net_profit_czk']:>9.3f}" if fees else ""
        print(f"{fmt_local(t):<21} {pat:<4} {r['action']:<17} {num(r['spot_czk_mwh'], '.2f'):>8} "
              f"{num(r['buy_czk_kwh'], '.3f'):>7} {num(r['dist_czk_kwh'], '.3f'):>7} "
              f"{r['tariff']:<5} {num(r['sell_czk_kwh'], '.3f'):>7} "
              f"{r['bought_kwh']:>8.3f} {r['sold_kwh']:>8.3f} {r['soc_end_pct']:>6.1f} "
              f"{r['profit_czk']:>9.3f}{extra}")  # fmt: skip


def _plan_table(rows: list[dict[str, Any]], res: PlanResult) -> None:
    pv = res.problem.pv is not None
    ph = f" {'PV kWh':>7} {'PV→bat':>7} {'PV→grid':>7}" if pv else ""
    print(f"{'time':<20} {'spot':>9} {'buy':>7} {'sell':>7}  {'action':<9} {'kW':>6} "
          f"{'grid kWh':>9} {'SoC %':>6} {'CZK':>8}{ph}")  # fmt: skip
    print(f"{'(local)':<20} {'CZK/MWh':>9} {'CZK/kWh':>7} {'CZK/kWh':>7}")
    for r, t in zip(rows, res.problem.starts, strict=True):
        spot = "–" if r["spot_czk_mwh"] is None else f"{r['spot_czk_mwh']:.2f}"
        buy = "–" if r["buy_czk_kwh"] is None else f"{r['buy_czk_kwh']:.3f}"
        sell = "–" if r["sell_czk_kwh"] is None else f"{r['sell_czk_kwh']:.3f}"
        pc = (f" {r['pv_kwh']:>7.3f} {r['pv_battery_kwh']:>7.3f} {r['pv_grid_kwh']:>7.3f}"
              if pv else "")  # fmt: skip
        print(f"{fmt_local(t):<20} {spot:>9} {buy:>7} {sell:>7}  {r['action']:<9} "
              f"{r['power_kw']:>6.2f} {r['grid_kwh']:>9.3f} {r['soc_end_pct']:>6.1f} "
              f"{r['cash_czk']:>8.2f}{pc}")  # fmt: skip


def _terminal_text(term, b: Battery) -> str:  # type: ignore[no-untyped-def]
    if term.mode == "value":
        return f"leftover energy valued at {term.czk_per_kwh_stored:.3f} CZK per stored kWh"
    if term.mode == "min-soc":
        return f"end SoC ≥ {b.pct(term.min_kwh):.1f} %"
    return "leftover energy worth nothing"


def export_rows(path: Path, rows: list[dict[str, Any]], doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".json":
        path.write_text(dump_json(doc | {"warnings": list(_RUN.warnings)}) + "\n",
                        encoding="utf-8")  # fmt: skip
        return
    write_csv(path, rows)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["start"])
        w.writeheader()
        w.writerows(rows)


# ------------------------------------------------------------------------------------------
# backtest
# ------------------------------------------------------------------------------------------
class ProgressBar:
    """Single-line progress on stderr (``\\r`` redraw, ASCII only – safe on Windows/Termux).

    Shown only when stderr is a terminal; stdout and exported files are never touched.
    The fraction comes from :func:`run_backtest`: DP work done (rolling re-optimisations and
    the perfect-foresight benchmark weighted by their quarter-hours, whether they run one
    after another or in parallel). The final ``done`` event always draws 100 %.
    """

    WIDTH = 24

    def __init__(self, stream: Any) -> None:
        self.stream = stream
        self.t0 = monotonic()
        self.last = -1.0
        self.overall = 0.0  # last drawn fraction (never decreases)
        self.shown = False

    @classmethod
    def create(cls, *, enabled: bool) -> ProgressBar | None:
        stream = sys.stderr
        if not enabled or stream is None or not getattr(stream, "isatty", lambda: False)():
            return None
        return cls(stream)

    def _overall(self, phase: str, fraction: float) -> float:
        return 1.0 if phase == "done" else min(max(fraction, 0.0), 1.0)

    def eta_text(self, overall: float, elapsed: float) -> str:
        if overall >= 1:
            return "ETA 0:00"
        if overall < 0.03 or elapsed < 0.5:  # too early for a meaningful estimate
            return "ETA --:--"
        eta = round(elapsed / overall * (1 - overall))
        return f"ETA {eta // 60}:{eta % 60:02d}"

    def update(self, phase: str, fraction: float, detail: str) -> None:
        overall = max(self.overall, self._overall(phase, fraction))
        now = monotonic()
        if fraction < 1 and phase != "done" and now - self.last < 0.1:
            return  # throttled; phase ends and the final "done" are always drawn
        self.last = now
        self.overall = overall
        filled = int(self.WIDTH * overall)
        bar = "#" * filled + "-" * (self.WIDTH - filled)
        line = (
            f"\r[{bar}] {overall * 100:5.1f} %  {detail:<18} "
            f"{self.eta_text(overall, now - self.t0)}"
        )
        with contextlib.suppress(OSError, ValueError):
            self.stream.write(line)
            self.stream.flush()
            self.shown = True

    def close(self) -> None:
        if self.shown:
            with contextlib.suppress(OSError, ValueError):
                self.stream.write("\r" + " " * 72 + "\r")
                self.stream.flush()


def backtest_rows(res: BacktestResult, tariffs: Tariffs) -> list[dict[str, Any]]:
    b = res.battery
    rows, prev = [], res.initial_soc_kwh
    for iv in res.intervals:
        st = iv.step
        rows.append({
            "start": to_local(iv.start).isoformat(),
            "spot_czk_mwh": _r(iv.spot_czk_mwh, 2),
            "buy_czk_kwh": _r(iv.buy_czk_kwh),
            "sell_czk_kwh": _r(iv.sell_czk_kwh),
            "action": st.action,
            "power_kw": round(st.power_kw, 3),
            "grid_kwh": round(st.grid_kwh, 4),
            "soc_start_pct": round(b.pct(prev), 2),
            "soc_end_pct": round(b.pct(st.soc_kwh), 2),
            "cash_czk": round(st.cash_czk - st.wear_czk, 4),
            "fees_czk": round(tariffs.fees_for_quarter(iv.start), 4),
            "known_until": to_local(iv.horizon_end).isoformat(),
        } | ({"pv_forecast_kwh": _r(iv.pv_forecast_kwh)} | _pv_cols(st)
             if res.pv is not None else {}))  # fmt: skip
        prev = st.soc_kwh
    return rows


def backtest_doc(res: BacktestResult, tariffs: Tariffs, pub: time | None) -> dict[str, Any]:
    b = res.battery
    days = (res.end - res.start).days + 1
    tv = res.terminal_value_czk_kwh
    tot = res.totals.as_dict(b.usable_kwh)
    stored_change = (res.end_soc_kwh - res.initial_soc_kwh) * tv
    doc: dict[str, Any] = {
        "period": {"from": res.start.isoformat(), "to": res.end.isoformat(), "days": days},
        "battery": b.describe(),
        "tariffs": tariffs.describe(),
        "publication": (
            f"prices for day D known at {pub:%H:%M} on D−1" if pub else
            "prices for day D known on D−1 at 12:57 (delivery ≤ 2025-09-30) / 13:05 (from "
            "2025-10-01), used from the next quarter-hour"
        ),
        "optimisations": res.solves,
        "soc_start_pct": round(b.pct(res.initial_soc_kwh), 2),
        "soc_end_pct": round(b.pct(res.end_soc_kwh), 2),
        "summary": tot | {
            "profit_per_day_czk": round(tot["profit_czk"] / days, 2),
            "net_profit_per_day_czk": round(tot["net_profit_czk"] / days, 2),
            "stored_energy_value_change_czk": round(stored_change, 2),
            "profit_incl_stored_energy_czk": round(tot["profit_czk"] + stored_change, 2),
        },
    }  # fmt: skip
    if res.pv is not None:
        doc["pv"] = dict(res.pv)
    if res.foresight is not None:
        f = res.foresight.as_dict(b.usable_kwh)
        f_change = ((res.foresight_end_soc_kwh or 0) - res.initial_soc_kwh) * tv
        f_incl = f["profit_czk"] + f_change
        doc["perfect_foresight"] = f | {
            "soc_end_pct": round(b.pct(res.foresight_end_soc_kwh or 0), 2),
            "profit_incl_stored_energy_czk": round(f_incl, 2),
            "captured_pct": round((tot["profit_czk"] + stored_change) / f_incl * 100, 1)
            if f_incl > 0 else None,
        }  # fmt: skip
    return doc


def cmd_backtest(args: argparse.Namespace) -> int:
    cfg = load_config(Path(args.config) if args.config else None)
    for w in cfg.warnings:
        warn(w)
    b = build_battery(args, cfg)
    tariffs = build_tariffs(args, cfg)
    soc = initial_soc_kwh(args, cfg, b)
    tcfg = build_terminal(args, cfg, b)
    pub = publish_time(args, cfg)
    start = parse_day(args.start)
    end = parse_day(args.end or args.start, end=True)
    if end < start:
        raise CliError("--to is before --from")
    today = local_today()
    if start > today:
        raise CliError("--from is in the future")
    if end >= today:
        warn(f"The period was cut to yesterday ({today - timedelta(days=1)}).")
        end = today - timedelta(days=1)
        if end < start:
            raise CliError("Nothing to backtest before today.")
    # one extra day: on the last day the next day's prices are published at ~13:05
    book = fetch_book(ote_client(args), start, min(end + timedelta(days=1), today))
    system = build_pv(args, cfg)
    replan = _pick(args, "replan", cfg, "pv", "replan", "forecast")
    if replan not in REPLAN_MODES:
        raise CliError(f"replan must be one of {', '.join(REPLAN_MODES)}")
    pvs, pv_info = (None, {}) if system is None else backtest_pv(args, system, start, end,
                                                                 replan)  # fmt: skip
    info(f"Simulating {start} – {end} ...")
    bar = ProgressBar.create(enabled=not (args.json or args.no_progress))
    try:
        res = run_backtest(book, start, end, tariffs, b, tcfg, soc, publish=pub,
                           foresight=not args.no_foresight, jobs=args.jobs,
                           progress=bar.update if bar else None, pv=pvs)  # fmt: skip
    finally:
        if bar:
            bar.close()
    if res.workers > 1:
        info(f"{res.solves} optimisations solved in parallel on {res.workers} processes")
    for w in res.warnings:
        warn(w)
    if not res.intervals:
        raise CliError("No OTE prices for the selected period.")
    doc = backtest_doc(res, tariffs, pub)
    if pvs is not None:
        doc["pv"] = pv_info | doc.get("pv", {})
    if args.breakdown != "none":
        doc["breakdown"] = {"by": args.breakdown, "rows": res.breakdown(args.breakdown)}
    if args.breakdown_csv:
        if args.breakdown == "none":
            raise CliError("--breakdown-csv needs a breakdown (-b month|day|hour|quarter-hour)")
        write_csv(Path(args.breakdown_csv), doc["breakdown"]["rows"])
        info(f"Wrote {args.breakdown_csv}")
    if args.export:
        rows = backtest_rows(res, tariffs)
        export_rows(Path(args.export), rows, doc | {"intervals": rows})
        info(f"Wrote {args.export}")
    if args.json:
        print(dump_json(doc | {"warnings": list(_RUN.warnings)}))
        return 0
    render_backtest(doc)
    return 0


def backtest_pv(args: argparse.Namespace, system: PvSystem, start: date, end: date,
                replan: str) -> tuple[PvSetup, dict[str, Any]]:  # fmt: skip
    """Forecast runs (Open-Meteo) and actual production (nearest ČHMÚ station) for a backtest."""
    session, cache = make_session(), make_cache(args)
    om = OpenMeteo(session, cache, system.lat, system.lon)
    fc = PvForecast(om, system)
    t0, t1 = local_midnight_utc(start), local_midnight_utc(end + timedelta(days=1))
    prefetch_runs(om, runs_between(t0 - timedelta(days=2), t1))
    chmi = Chmi(session, cache)
    st = chmi.nearest(system.lat, system.lon)
    actual = system.energy(chmi.irradiance(st.wsi, t0, t1))
    info(f"PV forecasts: {om.cached} run(s) from cache, {om.downloaded} downloaded; actual: ČHMÚ "
         f"{st.name} ({st.distance_km:.1f} km), {chmi.downloaded} file(s) downloaded")  # fmt: skip
    doc = {
        "system": system.describe(),
        "forecast": "Open-Meteo single runs as issued: ICON-D2 (15 min, every 3 h, known 2 h "
        "after the run) from 2026-04-02, ECMWF IFS (hourly, every 6 h, known after 7 h) before",
        "actual": f"ČHMÚ 10-min global radiation, station {st.name} ({st.distance_km:.1f} km), "
        "transposed to the arrays",
    }
    return PvSetup(fc, actual, system.kwp, replan), doc


def render_backtest(doc: dict[str, Any]) -> None:
    p, s = doc["period"], doc["summary"]
    print(f"Backtest {p['from']} – {p['to']} ({p['days']} days)")
    print(f"Battery:      {doc['battery']}")
    for k, v in doc["tariffs"].items():
        if k != "monthly_fees":
            print(f"{k.capitalize() + ':':<13} {v}")
    print(f"Prices:       {doc['publication']}; {doc['optimisations']} re-optimisations")
    pv = doc.get("pv")
    if pv:
        print(f"PV:           {pv['system']}; re-planning: {pv['replan']}")
        print(f"PV forecast:  {pv['forecast']}")
        print(f"PV actual:    {pv['actual']}")
    print()
    print(f"Bought        {s['bought_kwh']:>12.2f} kWh   cost     {s['cost_czk']:>12.2f} CZK")
    print(f"Sold          {s['sold_kwh']:>12.2f} kWh   revenue  {s['revenue_czk']:>12.2f} CZK")
    if s["wear_czk"]:
        print(f"Wear                                {s['wear_czk']:>12.2f} CZK")
    if s["fees_czk"]:
        print(
            f"Trading profit                      {s['profit_czk']:>12.2f} CZK "
            f"({s['profit_per_day_czk']:.2f} CZK/day, before monthly fees)"
        )
        print(
            f"Monthly fees                        {-s['fees_czk']:>12.2f} CZK "
            f"({doc['tariffs']['monthly_fees'].split(' (')[0]}, prorated by days)"
        )
        print(
            f"NET PROFIT                          {s['net_profit_czk']:>12.2f} CZK "
            f"({s['net_profit_per_day_czk']:.2f} CZK/day)"
        )
    else:
        print(f"PROFIT                              {s['profit_czk']:>12.2f} CZK "
              f"({s['profit_per_day_czk']:.2f} CZK/day)")  # fmt: skip
    print(f"Cycles        {s['cycles']:>12.1f} (equivalent full cycles of usable capacity)")
    if pv:
        bias = "–" if s["pv_forecast_bias_pct"] is None else f"{s['pv_forecast_bias_pct']:+.1f} %"
        mae = "–" if s["pv_forecast_mae_pct"] is None else f"{s['pv_forecast_mae_pct']:.1f} %"
        print(f"PV actual     {s['pv_kwh']:>12.2f} kWh   forecast used {s['pv_forecast_kwh']:.2f} "
              f"kWh (bias {bias}, quarter-hour MAE {mae}"
              + (f", daily MAE {pv['daily_mae_pct']:.1f} %" if pv.get("daily_mae_pct") is not None
                 else "") + ")")  # fmt: skip
        print(f"PV → battery  {s['pv_battery_kwh']:>12.2f} kWh   PV → grid "
              f"{s['pv_grid_kwh']:.2f} kWh (revenue {s['pv_revenue_czk']:.2f} CZK), curtailed "
              f"{s['pv_curtailed_kwh']:.2f} kWh")  # fmt: skip
    print(f"SoC           {doc['soc_start_pct']:.1f} % → {doc['soc_end_pct']:.1f} %"
          f" (stored energy change worth {s['stored_energy_value_change_czk']:.2f} CZK; "
          f"trading profit incl. it {s['profit_incl_stored_energy_czk']:.2f} CZK)")  # fmt: skip
    f = doc.get("perfect_foresight")
    if f:
        cap = f"{f['captured_pct']:.1f} %" if f["captured_pct"] is not None else "–"
        what = "all prices and the actual PV" if pv else "all prices"
        print(f"Perfect foresight ({what} known in advance): trading profit "
              f"{f['profit_incl_stored_energy_czk']:.2f} CZK incl. stored energy, "
              f"{f['cycles']:.1f} cycles; rolling plan captures {cap}")  # fmt: skip
    br = doc.get("breakdown")
    if br and br["by"] in FINE:
        print()
        render_fine(br["rows"], br["by"], bool(s["fees_czk"]))
    elif br:
        print()
        fees = bool(s["fees_czk"])
        extra = f" {'fees':>9} {'net':>10}" if fees else ""
        print(f"{br['by']:<10} {'bought kWh':>11} {'cost':>10} {'sold kWh':>10} "
              f"{'revenue':>10} {'profit':>10}{extra} {'cycles':>7}")  # fmt: skip
        for r in br["rows"]:
            extra = f" {r['fees_czk']:>9.2f} {r['net_profit_czk']:>10.2f}" if fees else ""
            print(f"{r['period']:<10} {r['bought_kwh']:>11.2f} {r['cost_czk']:>10.2f} "
                  f"{r['sold_kwh']:>10.2f} {r['revenue_czk']:>10.2f} {r['profit_czk']:>10.2f}"
                  f"{extra} {r['cycles']:>7.1f}")  # fmt: skip


# ------------------------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ensure_unicode_stdio()
    args = build_parser().parse_args(argv)
    _RUN.json = bool(getattr(args, "json", False))
    _RUN.warnings = []
    try:
        return int(args.func(args))
    except (CliError, ConfigError, FetchError) as exc:
        if _RUN.json:
            print(dump_json({"error": str(exc), "warnings": list(_RUN.warnings)}))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # pragma: no cover
        return 130


__all__ = ["DT_H", "default_publish_time", "main"]
