"""Fetch daily high temperature distributions from Open-Meteo ensemble."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import requests


@dataclass(frozen=True)
class DailyHighForecast:
    target: date
    members_f: tuple[float, ...]
    source: str

    @property
    def mean_f(self) -> float:
        return sum(self.members_f) / len(self.members_f)

    @property
    def min_f(self) -> float:
        return min(self.members_f)

    @property
    def max_f(self) -> float:
        return max(self.members_f)


def fetch_ensemble_high(
    lat: float,
    lon: float,
    timezone: str,
    target: date,
    timeout: float = 20.0,
) -> DailyHighForecast:
    """Return ensemble daily max temps (°F) for `target` near lat/lon."""
    url = "https://ensemble-api.open-meteo.com/v1/ensemble"
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max",
        "temperature_unit": "fahrenheit",
        "timezone": timezone,
        "forecast_days": 8,
    }
    resp = requests.get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    payload = resp.json()
    daily = payload.get("daily") or {}
    times = daily.get("time") or []
    target_s = target.isoformat()
    if target_s not in times:
        raise ValueError(
            f"Open-Meteo ensemble has no daily high for {target_s}. "
            f"Available: {times}"
        )
    idx = times.index(target_s)

    members: list[float] = []
    # Control run + member01..N
    for key, series in daily.items():
        if not key.startswith("temperature_2m_max"):
            continue
        val = series[idx]
        if val is None:
            continue
        members.append(float(val))

    if len(members) < 5:
        raise ValueError(f"Too few ensemble members for {target_s}: {len(members)}")

    return DailyHighForecast(
        target=target,
        members_f=tuple(members),
        source="open_meteo_ensemble",
    )
