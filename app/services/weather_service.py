import os, httpx, datetime as dt
from functools import lru_cache

_API      = "https://api.openweathermap.org"
_KEY      = os.getenv("OPENWEATHER_API_KEY")
_UNITS    = "metric"  # °C
_LANG     = "es"

@lru_cache(maxsize=128)
def _geocode(city: str):
    url = f"{_API}/geo/1.0/direct"
    params = {"q": city, "limit": 1, "appid": _KEY}
    r = httpx.get(url, params=params, timeout=8)
    r.raise_for_status()
    data = r.json()
    if not data:
        raise ValueError("Ciudad no encontrada")
    return data[0]["lat"], data[0]["lon"]

def get_forecast(city: str, date: dt.date) -> dict:
    lat, lon = _geocode(city)
    url = f"{_API}/data/2.5/forecast"
    params = {"lat": lat, "lon": lon, "appid": _KEY,
              "units": _UNITS, "lang": _LANG}
    r = httpx.get(url, params=params, timeout=8)
    r.raise_for_status()
    forecast = r.json()["list"]

    # Elegir el slot más cercano a las 12:00 local de la fecha pedida
    target_ts = dt.datetime.combine(date, dt.time(12))
    best = min(
        forecast,
        key=lambda f: abs(dt.datetime.fromtimestamp(f["dt"]) - target_ts)
    )
    weather = best["weather"][0]
    temp    = best["main"]["temp"]
    return {
        "city": city.title(),
        "date": date.strftime("%d/%m/%Y"),
        "condition": weather["description"],   # “cielo claro”
        "temp": round(temp)
    }
