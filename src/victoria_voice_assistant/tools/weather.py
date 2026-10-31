from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from typing import Any, Callable, Protocol

import httpx

from victoria_voice_assistant.config.schema import WeatherConfig
from victoria_voice_assistant.tools.outcome import ToolOutcome

logger = logging.getLogger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
REQUEST_TIMEOUT_S = 4.0
RAIN_HOURS = 6

# WMO weather interpretation codes, as Open-Meteo documents them.
WMO_CODES = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "cloudy",
    45: "foggy", 48: "foggy",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "rain showers", 81: "rain showers", 82: "heavy rain showers",
    85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with hail",
}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


class WeatherError(RuntimeError):
    pass


class WeatherProvider(Protocol):
    async def geocode(self, name: str) -> dict[str, Any] | None: ...

    async def forecast(self, lat: float, lon: float, units: str) -> dict[str, Any]: ...


class OpenMeteoProvider:
    """Open-Meteo forecast and geocoding APIs (no API key)."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def _get(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        client = self._client or httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S)
        try:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise WeatherError(str(exc) or type(exc).__name__) from exc
        finally:
            if self._client is None:
                await client.aclose()

    async def geocode(self, name: str) -> dict[str, Any] | None:
        data = await self._get(GEOCODING_URL, {"name": name, "count": 1, "language": "en", "format": "json"})
        results = data.get("results") or []
        if not results:
            return None
        place = results[0]
        label = place["name"]
        if place.get("admin1") and place.get("admin1") != label:
            label = f"{label}, {place['admin1']}"
        return {"name": label, "lat": place["latitude"], "lon": place["longitude"]}

    async def forecast(self, lat: float, lon: float, units: str) -> dict[str, Any]:
        params: dict[str, Any] = {
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,precipitation",
            "hourly": "temperature_2m,precipitation_probability,weather_code",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "timezone": "auto",
            "forecast_days": 7,
        }
        if units == "imperial":
            params.update(temperature_unit="fahrenheit", wind_speed_unit="mph", precipitation_unit="inch")
        return await self._get(FORECAST_URL, params)


class WeatherService:
    """Answers weather questions with only the fields the question needs, cached per location."""

    def __init__(
        self,
        settings: WeatherConfig | None,
        provider: WeatherProvider | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.provider = provider or OpenMeteoProvider()
        self.clock = clock
        self._forecasts: dict[tuple[float, float], tuple[float, dict[str, Any]]] = {}
        self._places: dict[str, tuple[float, dict[str, Any] | None]] = {}

    def update_settings(self, settings: WeatherConfig | None) -> None:
        if settings != self.settings:
            self._forecasts.clear()
        self.settings = settings

    def _fresh(self, stored_at: float) -> bool:
        ttl = self.settings.cache_ttl_s if self.settings else 600
        return self.clock() - stored_at < ttl

    async def _place(self, location: str | None) -> dict[str, Any] | None:
        home = self.settings.home if self.settings else None
        if not location or location.strip().lower() in ("home", "here", "outside", "local", "today"):
            if home is None:
                return None
            return {"name": home.name, "lat": home.lat, "lon": home.lon, "home": True}
        key = location.strip().lower()
        cached = self._places.get(key)
        if cached and self._fresh(cached[0]):
            return cached[1]
        place = await self.provider.geocode(location.strip())
        self._places[key] = (self.clock(), place)
        return place

    async def _forecast(self, place: dict[str, Any]) -> dict[str, Any]:
        key = (round(place["lat"], 3), round(place["lon"], 3))
        cached = self._forecasts.get(key)
        if cached and self._fresh(cached[0]):
            logger.info("Weather cache hit", extra={"event": "weather_cache_hit"})
            return cached[1]
        units = self.settings.units if self.settings else "imperial"
        data = await self.provider.forecast(place["lat"], place["lon"], units)
        self._forecasts[key] = (self.clock(), data)
        return data

    async def handle(self, arguments: dict[str, Any]) -> ToolOutcome:
        location = arguments.get("location") or None
        when = str(arguments.get("when") or "now")
        try:
            place = await self._place(location)
            if place is None:
                if location:
                    return ToolOutcome(result={"error": "unknown place"}, speak=f"I couldn't find {location}.")
                return ToolOutcome(
                    result={"error": "home location not configured"},
                    speak="I don't know where home is yet. Set weather.home in tools.yaml.",
                    error=True,
                )
            data = await self._forecast(place)
        except WeatherError as exc:
            logger.warning("Weather lookup failed", extra={"event": "weather_failed", "error": str(exc)})
            return ToolOutcome(
                result={"error": "weather service unavailable"},
                speak="I couldn't get the weather right now.",
                error=True,
            )
        return ToolOutcome(result=shape_forecast(data, place, when, self.settings.units if self.settings else "imperial"))


def _local_now(data: dict[str, Any]) -> datetime:
    current = data.get("current") or {}
    if current.get("time"):
        return datetime.fromisoformat(current["time"])
    return datetime.now()


def _day(data: dict[str, Any], day: date) -> dict[str, Any] | None:
    daily = data.get("daily") or {}
    for index, value in enumerate(daily.get("time") or []):
        if date.fromisoformat(value) == day:
            return {
                "date": value,
                "day": WEEKDAYS[day.weekday()].title(),
                "conditions": WMO_CODES.get(daily["weather_code"][index], "unknown"),
                "high": round(daily["temperature_2m_max"][index]),
                "low": round(daily["temperature_2m_min"][index]),
                "chance_of_rain_pct": daily["precipitation_probability_max"][index],
            }
    return None


def _hours(data: dict[str, Any], start: datetime, end: datetime) -> list[dict[str, Any]]:
    hourly = data.get("hourly") or {}
    hours = []
    for index, value in enumerate(hourly.get("time") or []):
        moment = datetime.fromisoformat(value)
        if start <= moment < end:
            hours.append(
                {
                    "time": moment.strftime("%-I %p").lower(),
                    "temp": round(hourly["temperature_2m"][index]),
                    "chance_of_rain_pct": hourly["precipitation_probability"][index],
                    "conditions": WMO_CODES.get(hourly["weather_code"][index], "unknown"),
                }
            )
    return hours


def _rain_summary(hours: list[dict[str, Any]]) -> dict[str, Any]:
    likely = [hour for hour in hours if (hour["chance_of_rain_pct"] or 0) >= 40]
    peak = max((hour["chance_of_rain_pct"] or 0 for hour in hours), default=0)
    return {
        "max_chance_of_rain_pct": peak,
        "rain_likely_from": likely[0]["time"] if likely else None,
    }


def shape_forecast(data: dict[str, Any], place: dict[str, Any], when: str, units: str) -> dict[str, Any]:
    """Cut the forecast down to what the question needs, so the model's reply call stays short."""
    now = _local_now(data)
    today = now.date()
    when_key = when.strip().lower()
    shaped: dict[str, Any] = {
        "place": place["name"],
        "units": "°F, mph" if units == "imperial" else "°C, km/h",
        "local_time": now.strftime("%A %-I:%M %p"),
    }

    if when_key in ("now", "current", "currently", "right now", "today", "", "next few hours", "later", "rain"):
        current = data.get("current") or {}
        shaped["now"] = {
            "temp": round(current.get("temperature_2m", 0)),
            "feels_like": round(current.get("apparent_temperature", 0)),
            "conditions": WMO_CODES.get(current.get("weather_code"), "unknown"),
            "wind": round(current.get("wind_speed_10m", 0)),
        }
        shaped["today"] = _day(data, today)
        shaped["next_hours"] = _rain_summary(_hours(data, now, now + timedelta(hours=RAIN_HOURS)))
        return shaped

    if when_key in ("tonight", "this evening", "overnight"):
        start = datetime.combine(today, datetime.min.time()).replace(hour=18)
        hours = _hours(data, max(now, start), start + timedelta(hours=12))
        shaped["tonight"] = {
            "low": min((hour["temp"] for hour in hours), default=None),
            "conditions": hours[len(hours) // 2]["conditions"] if hours else "unknown",
            **_rain_summary(hours),
        }
        return shaped

    if when_key in ("weekend", "this weekend"):
        saturday = today + timedelta(days=(5 - today.weekday()) % 7)
        shaped["weekend"] = [day for day in (_day(data, saturday), _day(data, saturday + timedelta(days=1))) if day]
        return shaped

    if when_key in ("week", "this week", "next few days"):
        shaped["days"] = [day for offset in range(7) if (day := _day(data, today + timedelta(days=offset)))]
        return shaped

    target: date | None = None
    if when_key == "tomorrow":
        target = today + timedelta(days=1)
    else:
        for index, weekday in enumerate(WEEKDAYS):
            if weekday in when_key:
                target = today + timedelta(days=(index - today.weekday()) % 7)
                break
    if target is None:
        try:
            target = date.fromisoformat(when_key)
        except ValueError:
            target = today
    day = _day(data, target)
    shaped["day"] = day if day else {"error": "outside the 7-day forecast"}
    return shaped
