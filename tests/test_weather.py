"""Contract test for the weather lane (argus/tools/weather.py).

Stubs Open-Meteo's geocoding + forecast HTTP so the full shape is exercised offline.
    python tests/test_weather.py
"""
from __future__ import annotations
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from argus.tools import weather


class _Resp:
    def __init__(self, payload): self._p = payload
    def json(self): return self._p
    def raise_for_status(self): pass


GEO = {"results": [{"name": "Atlanta", "admin1": "Georgia",
                    "latitude": 33.749, "longitude": -84.388}]}

FCAST = {
    "current": {"temperature_2m": 81.4, "apparent_temperature": 86.0,
                "relative_humidity_2m": 64, "weather_code": 2,
                "wind_speed_10m": 5.6, "wind_direction_10m": 180, "is_day": 1},
    "daily": {"time": ["2026-06-20", "2026-06-21", "2026-06-22"],
              "weather_code": [3, 95, 0],
              "temperature_2m_max": [88.1, 84.0, 90.2],
              "temperature_2m_min": [67.0, 66.4, 68.9],
              "precipitation_probability_max": [10, 70, 0]},
}


def _fake_get(url, params=None, timeout=None):
    if "geocoding" in url:
        return _Resp(GEO)
    return _Resp(FCAST)


def main() -> None:
    weather.httpx.get = _fake_get  # type: ignore[assignment]

    # Default location skips geocoding (hard-coded Dahlonega coords).
    cur = weather.current()
    assert cur["location"] == "Dahlonega", cur
    assert cur["temp"] == 81 and cur["feels_like"] == 86, cur
    assert cur["condition"] == "Partly cloudy" and cur["emoji"] == "⛅", cur
    assert cur["humidity"] == 64 and cur["wind_mph"] == 6, cur

    # Named location goes through geocoding.
    fc = weather.forecast("Atlanta, GA", days=3)
    assert fc["location"] == "Atlanta, Georgia", fc
    assert len(fc["days"]) == 3, fc
    d1 = fc["days"][1]
    assert d1["condition"] == "Thunderstorm" and d1["precip_pct"] == 70, d1
    assert d1["high"] == 84 and d1["low"] == 66, d1

    # days clamps to 1..7.
    assert len(weather.forecast("x", days=99)["days"]) <= 7

    # Tool lane wiring.
    names = {t.name for t in weather.tools()}
    assert names == {"weather_current", "weather_forecast"}, names

    print("test_weather: OK")


if __name__ == "__main__":
    main()
