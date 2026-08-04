"""Settings for the weather Kalshi paper bot."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


# Settlement stations used by Kalshi KXHIGH* markets (approx coords for Open-Meteo).
CITIES: dict[str, dict] = {
    "NYC": {
        "series": "KXHIGHNY",
        "lat": 40.7789,
        "lon": -73.9692,
        "timezone": "America/New_York",
        "station": "Central Park",
    },
    "CHI": {
        "series": "KXHIGHCHI",
        "lat": 41.7868,
        "lon": -87.7522,
        "timezone": "America/Chicago",
        "station": "Chicago Midway",
    },
    "MIA": {
        "series": "KXHIGHMIA",
        "lat": 25.7959,
        "lon": -80.2870,
        "timezone": "America/New_York",
        "station": "Miami Intl",
    },
    "LAX": {
        "series": "KXHIGHLAX",
        "lat": 33.9425,
        "lon": -118.4081,
        "timezone": "America/Los_Angeles",
        "station": "LAX",
    },
}


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


@dataclass(frozen=True)
class Settings:
    paper_bankroll: float
    stake_notional: float
    min_edge: float
    loop_interval_seconds: int
    auto_bet: bool
    city: str
    kalshi_series: str
    target_date: str
    forecast_provider: str
    kalshi_base: str

    @property
    def city_meta(self) -> dict:
        if self.city not in CITIES:
            known = ", ".join(sorted(CITIES))
            raise ValueError(f"Unknown CITY={self.city!r}. Known: {known}")
        return CITIES[self.city]


def load_settings() -> Settings:
    city = os.getenv("CITY", "NYC").strip().upper()
    meta = CITIES.get(city, CITIES["NYC"])
    series = os.getenv("KALSHI_SERIES", meta["series"]).strip().upper()
    return Settings(
        paper_bankroll=_float("PAPER_BANKROLL", 1000.0),
        stake_notional=_float("STAKE_NOTIONAL", 10.0),
        min_edge=_float("MIN_EDGE", 0.08),
        loop_interval_seconds=_int("LOOP_INTERVAL_SECONDS", 60),
        auto_bet=_bool("AUTO_BET", True),
        city=city,
        kalshi_series=series,
        target_date=os.getenv("TARGET_DATE", "tomorrow").strip().lower(),
        forecast_provider=os.getenv("FORECAST_PROVIDER", "open_meteo_ensemble").strip(),
        kalshi_base=os.getenv(
            "KALSHI_BASE",
            "https://api.elections.kalshi.com/trade-api/v2",
        ).rstrip("/"),
    )
