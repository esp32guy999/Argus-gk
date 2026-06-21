"""Weather lane — current conditions + multi-day forecast via Open-Meteo.

No API key, no config: Open-Meteo is free and keyless, so this lane is always on
(like web/notes). Defaults to Dahlonega, GA; any location string is geocoded via
Open-Meteo's geocoding endpoint. US-friendly units (°F, mph, inches).

The module-level `current()` / `forecast()` are shared by the chat tools AND the
Forge weather widget's HTTP endpoints. WMO weather codes are mapped to a short
label + emoji here so both surfaces render the same vocabulary.
"""
from __future__ import annotations

import httpx
from pydantic_ai.exceptions import ModelRetry

from ..registry import Tool

DEFAULT_LOCATION = "Dahlonega, GA"
# Hard-coded fallback so the widget still works if geocoding is rate-limited/down.
DEFAULT_COORDS = (34.5337, -83.9846, "Dahlonega")

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# WMO weather interpretation codes -> (label, emoji). Grouped to the buckets that
# actually matter at a glance; see open-meteo.com/en/docs for the full table.
WMO: dict[int, tuple[str, str]] = {
    0: ("Clear", "☀️"),
    1: ("Mainly clear", "🌤️"), 2: ("Partly cloudy", "⛅"), 3: ("Overcast", "☁️"),
    45: ("Fog", "🌫️"), 48: ("Rime fog", "🌫️"),
    51: ("Light drizzle", "🌦️"), 53: ("Drizzle", "🌦️"), 55: ("Heavy drizzle", "🌧️"),
    56: ("Freezing drizzle", "🌧️"), 57: ("Freezing drizzle", "🌧️"),
    61: ("Light rain", "🌦️"), 63: ("Rain", "🌧️"), 65: ("Heavy rain", "🌧️"),
    66: ("Freezing rain", "🌧️"), 67: ("Freezing rain", "🌧️"),
    71: ("Light snow", "🌨️"), 73: ("Snow", "🌨️"), 75: ("Heavy snow", "❄️"),
    77: ("Snow grains", "🌨️"),
    80: ("Light showers", "🌦️"), 81: ("Showers", "🌧️"), 82: ("Violent showers", "⛈️"),
    85: ("Snow showers", "🌨️"), 86: ("Snow showers", "🌨️"),
    95: ("Thunderstorm", "⛈️"), 96: ("Thunderstorm + hail", "⛈️"), 99: ("Severe thunderstorm", "⛈️"),
}


def _describe(code: int | None) -> tuple[str, str]:
    return WMO.get(int(code), ("Unknown", "❔")) if code is not None else ("Unknown", "❔")


def _geocode(location: str) -> tuple[float, float, str]:
    """Resolve a place name to (lat, lon, display_name). Falls back to Dahlonega."""
    loc = (location or "").strip()
    if not loc or loc.lower().startswith("dahlonega"):
        return DEFAULT_COORDS
    try:
        r = httpx.get(GEOCODE_URL, params={"name": loc, "count": 1, "language": "en",
                                           "format": "json"}, timeout=15)
        results = (r.json() or {}).get("results") or []
    except httpx.RequestError as e:
        raise ModelRetry(f"weather: geocoding failed: {e}")
    if not results:
        raise ModelRetry(f"weather: couldn't find a place called '{loc}'. Try 'City, ST'.")
    top = results[0]
    name = ", ".join(p for p in (top.get("name"), top.get("admin1")) if p)
    return float(top["latitude"]), float(top["longitude"]), name or loc


def _fetch(lat: float, lon: float, *, days: int = 0) -> dict:
    params = {
        "latitude": lat, "longitude": lon,
        "current": ("temperature_2m,relative_humidity_2m,apparent_temperature,"
                    "weather_code,wind_speed_10m,wind_direction_10m,is_day"),
        "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
        "precipitation_unit": "inch", "timezone": "auto",
    }
    if days:
        params["daily"] = ("weather_code,temperature_2m_max,temperature_2m_min,"
                           "precipitation_probability_max")
        params["forecast_days"] = days
    try:
        r = httpx.get(FORECAST_URL, params=params, timeout=15)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as e:
        raise ModelRetry(f"weather: forecast request failed: {e}")


def current(location: str = DEFAULT_LOCATION) -> dict:
    """Current conditions for a location (default Dahlonega, GA)."""
    lat, lon, name = _geocode(location)
    cur = _fetch(lat, lon).get("current") or {}
    label, emoji = _describe(cur.get("weather_code"))
    return {
        "location": name,
        "temp": round(cur.get("temperature_2m", 0)),
        "feels_like": round(cur.get("apparent_temperature", 0)),
        "humidity": cur.get("relative_humidity_2m"),
        "wind_mph": round(cur.get("wind_speed_10m", 0)),
        "condition": label,
        "emoji": emoji,
        "is_day": bool(cur.get("is_day", 1)),
        "units": "F",
    }


def forecast(location: str = DEFAULT_LOCATION, days: int = 5) -> dict:
    """Current conditions + a `days`-day daily forecast (default 5, max 7)."""
    days = max(1, min(int(days), 7))
    lat, lon, name = _geocode(location)
    data = _fetch(lat, lon, days=days)
    cur = data.get("current") or {}
    daily = data.get("daily") or {}
    cur_label, cur_emoji = _describe(cur.get("weather_code"))
    out_days = []
    for i, date in enumerate(daily.get("time", [])):
        label, emoji = _describe(daily.get("weather_code", [None])[i])
        out_days.append({
            "date": date,
            "high": round(daily["temperature_2m_max"][i]),
            "low": round(daily["temperature_2m_min"][i]),
            "precip_pct": daily.get("precipitation_probability_max", [None] * (i + 1))[i],
            "condition": label, "emoji": emoji,
        })
    return {
        "location": name,
        "current": {
            "temp": round(cur.get("temperature_2m", 0)),
            "feels_like": round(cur.get("apparent_temperature", 0)),
            "humidity": cur.get("relative_humidity_2m"),
            "wind_mph": round(cur.get("wind_speed_10m", 0)),
            "condition": cur_label, "emoji": cur_emoji,
            "is_day": bool(cur.get("is_day", 1)),
        },
        "days": out_days,
        "units": "F",
    }


def tools() -> list[Tool]:
    return [
        Tool(
            name="weather_current",
            description=("Get current weather (temperature, feels-like, humidity, wind, "
                         "conditions) for a place. Defaults to Dahlonega, GA if no "
                         "location is given. Units are °F / mph."),
            tags=["weather", "forecast", "temperature", "utility", "home", "dahlonega"],
            func=current,
            provider="weather",
            example={"location": "Dahlonega, GA"},
        ),
        Tool(
            name="weather_forecast",
            description=("Get a multi-day weather forecast (daily high/low, conditions, "
                         "precipitation chance) plus current conditions for a place. "
                         "Defaults to Dahlonega, GA. `days` is 1–7 (default 5). °F / mph."),
            tags=["weather", "forecast", "temperature", "utility", "home", "dahlonega"],
            func=forecast,
            provider="weather",
            example={"location": "Dahlonega, GA", "days": 5},
        ),
    ]
