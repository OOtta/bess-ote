"""Optional TOML configuration (CLI flags override it)."""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ENV_CONFIG = "BESS_OTE_CONFIG"

KNOWN: dict[str, set[str]] = {
    "battery": {
        "capacity_kwh", "charge_kw", "discharge_kw", "efficiency", "charge_efficiency",
        "discharge_efficiency", "soc_min", "soc_max", "soc", "cycle_cost_czk_kwh",
        "soc_step_kwh", "terminal", "terminal_price_czk_kwh", "terminal_soc",
    },
    "buy": {
        "multiplier", "add_czk_mwh", "negative_multiplier", "negative_add_czk_mwh",
        "negative", "vat",
    },
    "sell": {
        "multiplier", "add_czk_mwh", "negative_multiplier", "negative_add_czk_mwh",
        "negative", "vat",
    },
    "distribution": {"high_czk_kwh", "low_czk_kwh", "other_czk_kwh"},
    "low_tariff": {"hours", "hours_weekend", "holidays"},
    "market": {"publish_time"},
}  # fmt: skip


class ConfigError(ValueError):
    """Invalid configuration file."""


def default_config_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "bess-ote" / "config.toml"


@dataclass
class Config:
    path: Path | None = None
    sections: dict[str, dict[str, Any]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def get(self, section: str, key: str) -> Any:
        return self.sections.get(section, {}).get(key)


def load_config(path: Path | None = None) -> Config:
    explicit = path is not None or bool(os.environ.get(ENV_CONFIG))
    path = path or (Path(os.environ[ENV_CONFIG]) if os.environ.get(ENV_CONFIG) else None)
    path = (path or default_config_path()).expanduser()
    if not path.exists():
        if explicit:
            raise ConfigError(f"Config file not found: {path}")
        return Config()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    cfg = Config(path=path)
    for section, value in data.items():
        if section not in KNOWN:
            cfg.warnings.append(f"{path}: unknown section [{section}]")
            continue
        if not isinstance(value, dict):
            raise ConfigError(f"{path}: [{section}] must be a table")
        unknown = set(value) - KNOWN[section]
        if unknown:
            cfg.warnings.append(
                f"{path}: unknown keys in [{section}]: {', '.join(sorted(unknown))}"
            )
        cfg.sections[section] = {k: v for k, v in value.items() if k in KNOWN[section]}
    return cfg
