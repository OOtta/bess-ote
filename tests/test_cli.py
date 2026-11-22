import csv
import io
import json
import math
import re
import sys
from datetime import date

import pytest

from bess_ote.cli import main
from conftest import FakeOte, add_cnb, add_ote

BAT = ["-c", "10", "-p", "5", "--soc", "20"]
TAR = ["-a", "400", "--sell-add", "-300", "--distribution-high", "2.435", "--distribution-low",
       "0.416", "--low-tariff-hours", "1:15-5:15,12:30-16:30"]  # fmt: skip


def price(d, i):
    n = 24 if d < date(2025, 10, 1) else 96
    return 2500 + 2000 * math.sin(i / n * 2 * math.pi) + d.day * 10


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("BESS_OTE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.delenv("BESS_OTE_CONFIG", raising=False)


@pytest.fixture
def ote(mocked):
    fake = FakeOte(price)
    add_ote(mocked, fake)
    add_cnb(mocked)
    return fake


def run(argv, capsys):
    rc = main(argv)
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def test_plan_next_respects_publication_time(ote, capsys):
    argv = ["plan", *BAT, *TAR, "--at", "2025-06-10 12:50", "--next", "--json"]
    rc, out, err = run(argv, capsys)
    assert rc == 0 and err == ""
    doc = json.loads(out)
    assert doc["start"] == "2025-06-10T13:00:00+02:00"
    assert doc["horizon_end"] == "2025-06-11T00:00:00+02:00"  # 11 June published at 12:57
    assert "intervals" not in doc and doc["next"]["action"] in {"charge", "discharge", "wait"}
    assert list(doc)[-1] == "warnings"
    rc, out, _ = run(["plan", *BAT, "--at", "2025-06-10 12:57", "--json"], capsys)
    doc = json.loads(out)
    assert doc["horizon_end"] == "2025-06-12T00:00:00+02:00"
    assert len(doc["intervals"]) == doc["intervals_in_horizon"] == 44 + 96


def test_plan_text_table_and_csv_export(ote, tmp_path, capsys):
    out_csv = tmp_path / "plan.csv"
    argv = ["plan", *BAT, *TAR, "--at", "2025-10-14 09:00", "-o", str(out_csv)]
    rc, out, err = run(argv, capsys)
    assert rc == 0
    assert out.startswith("Plan as of 2025-10-14 09:00 CEST")
    assert "NEXT 09:00–09:15" in out and "Horizon total" in out
    assert "VT 2.435 / NT 0.416 CZK/kWh" in out and "on purchase only" in out
    assert "OTE prices" in err
    rows = list(csv.DictReader(out_csv.open(encoding="utf-8")))
    assert len(rows) == 60  # 09:00 – 24:00, 15 October not yet published
    assert {r["action"] for r in rows} <= {"charge", "discharge", "wait"}


def test_plan_no_prices_error_json(mocked, capsys):
    add_ote(mocked, FakeOte(price, last_day=date(2025, 6, 1)))
    rc, out, _ = run(["plan", *BAT, "--at", "2025-06-10 10:00", "--json"], capsys)
    assert rc == 2 and "No OTE prices" in json.loads(out)["error"]


def test_backtest_json_and_exports(ote, tmp_path, capsys):
    argv = ["backtest", *BAT, *TAR, "--from", "2025-06-01", "--to", "2025-06-03", "-b", "day"]
    rc, out, err = run([*argv, "--json"], capsys)
    assert rc == 0 and err == ""
    doc = json.loads(out)
    assert doc["period"]["days"] == 3 and doc["optimisations"] == 4
    s = doc["summary"]
    assert s["profit_czk"] == pytest.approx(s["revenue_czk"] - s["cost_czk"] - s["wear_czk"],
                                            abs=0.02)  # fmt: skip
    assert len(doc["breakdown"]["rows"]) == 3
    f = doc["perfect_foresight"]
    assert f["profit_incl_stored_energy_czk"] >= s["profit_incl_stored_energy_czk"] - 1e-6
    assert (
        ote.calls[0][1].isoformat() == "2025-06-01" and ote.calls[0][2].isoformat() == "2025-06-04"
    )
    csv_path, json_path = tmp_path / "bt.csv", tmp_path / "bt.json"
    assert main([*argv, "-o", str(csv_path)]) == 0
    assert main([*argv, "-o", str(json_path)]) == 0
    capsys.readouterr()
    assert len(csv_path.read_text(encoding="utf-8").splitlines()) == 1 + 3 * 96
    assert len(json.loads(json_path.read_text(encoding="utf-8"))["intervals"]) == 3 * 96


class FakeTty(io.StringIO):
    def isatty(self):
        return True


def test_monthly_fees_cli_backtest_and_plan(ote, tmp_path, capsys):
    fees = ["--buy-vat", "21", "--buy-monthly-fee", "147", "--distribution-monthly-fee", "257"]
    argv = ["backtest", *BAT, *TAR, "--from", "2025-06-29", "--to", "2025-07-02"]
    rc, out, _ = run([*argv, *fees, "--json"], capsys)
    doc = json.loads(out)
    s = doc["summary"]
    m = 404 * 1.21
    assert s["fees_czk"] == pytest.approx(m * 2 / 30 + m * 2 / 31, abs=0.01)
    assert s["net_profit_czk"] == pytest.approx(s["profit_czk"] - s["fees_czk"], abs=0.02)
    assert "monthly_fees" in doc["tariffs"]
    rc, plain, _ = run([*argv, "--buy-vat", "21", "--json"], capsys)
    assert json.loads(plain)["summary"]["profit_czk"] == s["profit_czk"]
    assert json.loads(plain)["summary"]["fees_czk"] == 0
    rc, out, _ = run([*argv, *fees, "--no-progress"], capsys)
    assert "Trading profit" in out and "Monthly fees" in out and "NET PROFIT" in out
    assert " fees " in out and " net " in out  # month breakdown columns
    csv_path = tmp_path / "bt.csv"
    assert main([*argv, *fees, "-o", str(csv_path)]) == 0
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    assert sum(float(r["fees_czk"]) for r in rows) == pytest.approx(s["fees_czk"], abs=0.01)
    capsys.readouterr()
    rc, out, _ = run(["plan", *BAT, *TAR, *fees, "--at", "2025-06-10 12:00", "--json"], capsys)
    assert rc == 0
    ps = json.loads(out)["summary"]
    assert ps["fees_czk"] == pytest.approx(m / 30 / 2, abs=0.01)  # 12:00–24:00 = half a day
    assert ps["net_profit_czk"] == pytest.approx(ps["profit_czk"] - ps["fees_czk"], abs=0.02)
    _, out, _ = run(["plan", *BAT, *TAR, *fees, "--at", "2025-06-10 12:00"], capsys)
    assert "Monthly fees prorated to the horizon" in out


def test_backtest_progress_bar_only_on_tty(ote, tmp_path, monkeypatch, capsys):
    argv = ["backtest", *BAT, "--from", "2025-06-01", "--to", "2025-06-02"]
    main([*argv, "--json"])  # warm the cache
    capsys.readouterr()
    tty = FakeTty()
    monkeypatch.setattr(sys, "stderr", tty)
    csv_path = tmp_path / "bt.csv"
    assert main([*argv, "-o", str(csv_path)]) == 0
    out = capsys.readouterr().out
    bar = tty.getvalue()
    assert "\r[" in bar and "100.0 %" in bar and "2/2 days" in bar and "ETA" in bar
    assert "\r" + " " * 72 + "\rWrote" in bar  # line cleared before further messages
    assert "\r" not in out and "#" not in out
    assert "\r" not in csv_path.read_text(encoding="utf-8")
    for extra in (["--json"], ["--no-progress"]):
        tty.seek(0)
        tty.truncate()
        assert main([*argv, *extra]) == 0
        assert "\r[" not in tty.getvalue()
    capsys.readouterr()
    monkeypatch.setattr(sys, "stderr", io.StringIO())  # not a terminal → no bar
    assert main(argv) == 0
    assert "\r[" not in sys.stderr.getvalue()


def _bar_percents(text):
    return [float(m) for m in re.findall(r"\] +([0-9.]+) %", text)]


@pytest.mark.parametrize("foresight", [True, False])
def test_progress_bar_ends_at_100_in_both_modes(ote, monkeypatch, capsys, foresight):
    argv = ["backtest", *BAT, "--from", "2025-06-01", "--to", "2025-06-03"]
    if not foresight:
        argv.append("--no-foresight")
    main([*argv, "--json"])  # warm the cache
    capsys.readouterr()
    tty = FakeTty()
    monkeypatch.setattr(sys, "stderr", tty)
    assert main(argv) == 0
    bar = tty.getvalue()
    pct = _bar_percents(bar)
    assert pct[-1] == 100.0  # final reported fraction == 1.0
    assert pct == sorted(pct)  # never goes backwards
    assert ("perfect foresight" in bar) is foresight
    last = bar.split("\r[")[-1]
    assert "ETA 0:00" in last
    if not foresight:
        assert "3/3 days" in last  # simulation alone fills the bar, no stop at 60 %
        assert "60.0 %" not in bar.replace("100.0 %", "")


def test_progress_bar_mapping_and_eta():
    from bess_ote.cli import ProgressBar

    with_f, without = ProgressBar(FakeTty(), True), ProgressBar(FakeTty(), False)
    assert with_f._overall("simulate", 1.0) == pytest.approx(0.6)
    assert with_f._overall("foresight", 0.5) == pytest.approx(0.8)
    assert without._overall("simulate", 1.0) == 1.0
    assert without._overall("simulate", 0.25) == 0.25
    assert with_f._overall("done", 0.0) == without._overall("done", 0.0) == 1.0
    assert without.eta_text(0.01, 5.0) == "ETA --:--"  # too early
    assert without.eta_text(0.5, 0.1) == "ETA --:--"
    assert without.eta_text(0.5, 30.0) == "ETA 0:30"
    assert without.eta_text(0.25, 60.0) == "ETA 3:00"
    assert without.eta_text(1.0, 60.0) == "ETA 0:00"
    # throttled updates never skip the final 100 %
    without.update("simulate", 0.1, "1/3 days")
    without.update("simulate", 0.5, "2/3 days")
    without.update("done", 1.0, "3/3 days")
    assert without.overall == 1.0 and "100.0 %" in without.stream.getvalue()


def test_backtest_text_15min_era(ote, capsys):
    rc, out, err = run(["backtest", *BAT, "--from", "2025-10-01", "--to", "2025-10-02"], capsys)
    assert rc == 0, err
    assert "PROFIT" in out and "Perfect foresight" in out and "2025-10" in out


def test_second_run_uses_cache(ote, capsys):
    argv = ["backtest", *BAT, "--from", "2025-06-01", "--to", "2025-06-02", "--json"]
    main(argv)
    n = len(ote.calls)
    main(argv)
    capsys.readouterr()
    assert len(ote.calls) == n


@pytest.mark.parametrize(
    ("argv", "msg"),
    [
        (["plan", "--at", "2025-06-10 10:00"], "capacity"),
        (["plan", *BAT, "--distribution-low", "0.4", "--at", "2025-06-10"], "low-tariff"),
        (["plan", *BAT, "--efficiency", "0.9", "--at", "2025-06-10"], "percent"),
        (["plan", "-c", "10", "-p", "5", "--soc-min", "20", "--soc", "10"], "outside"),
        (["backtest", *BAT, "--from", "2025-06-05", "--to", "2025-06-01"], "before"),
        (["plan", *BAT, "--at", "yesterday"], "invalid --at"),
    ],
)
def test_errors(argv, msg, capsys):
    rc, _, err = run(argv, capsys)
    assert rc == 2 and msg in err


def test_config_file(ote, tmp_path, capsys):
    cfg = tmp_path / "c.toml"
    cfg.write_text(
        "[battery]\ncapacity_kwh = 10\ncharge_kw = 5\nsoc = 20\nefficiency = 85\n"
        "[buy]\nadd_czk_mwh = 400\nvat = 21\n[sell]\nadd_czk_mwh = -300\n"
        '[distribution]\nhigh_czk_kwh = 2.435\n[market]\npublish_time = "14:20"\n'
        "[nonsense]\nx = 1\n",
        encoding="utf-8",
    )
    rc, out, _ = run(["plan", "--config", str(cfg), "--at", "2025-06-10 14:00", "--json"], capsys)
    doc = json.loads(out)
    assert rc == 0 and doc["horizon_end"] == "2025-06-11T00:00:00+02:00"  # 14:20 not reached
    assert "VAT 21 %" in doc["tariffs"]["buy"] and "85.0 %" in doc["battery"]
    assert any("unknown section [nonsense]" in w for w in doc["warnings"])


def test_cp1252_stdout_gets_utf8(monkeypatch):
    from bess_ote.cli import ensure_unicode_stdio

    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", stream)
    ensure_unicode_stdio()
    print("Plán → čtvrthodina")
    stream.flush()
    assert "→".encode() in raw.getvalue()


def test_example_config_is_valid():
    from pathlib import Path

    from bess_ote.config import load_config

    cfg = load_config(Path(__file__).parents[1] / "examples" / "config.example.toml")
    assert cfg.warnings == [] and cfg.get("battery", "capacity_kwh") == 10
