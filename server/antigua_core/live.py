"""Live data behind intents/live.py, each from a free source with no key:
stock and index prices from Yahoo Finance's chart API, crypto from
CoinGecko, US air quality from Open-Meteo, pollen from pollen.com.
Each answer is a sentence, or None when the source didn't answer (the
caller says so rather than guessing)."""

import logging
import re
import time
from datetime import datetime

import requests

from . import settings

log = logging.getLogger("antigua_core")

_UA = {"User-Agent": "Mozilla/5.0 (Antigua home assistant)"}
_cache: dict[str, tuple[float, object]] = {}


def _get(url: str, ttl: float, **kw):
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    try:
        r = requests.get(url, timeout=settings.LIVE_TIMEOUT, headers={**_UA, **kw.pop("headers", {})}, **kw)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        log.warning("Live: %s: %s", url.split("?")[0], e)
        return None
    _cache[url] = (time.time(), data)
    return data


def _money(x: float) -> str:
    if x >= 1000:
        return f"{x:,.0f} dollars"
    if x >= 1:
        dollars, cents = int(x), round((x - int(x)) * 100)
        if cents == 100:
            dollars, cents = dollars + 1, 0
        return f"{dollars} dollars" + (f" and {cents} cents" if cents else "")
    return f"{x:.4f}".rstrip("0") + " dollars"


def _change(pct: float | None) -> str:
    if pct is None:
        return ""
    if abs(pct) < 0.05:
        return ", about flat"
    return f", {'up' if pct > 0 else 'down'} {abs(pct):.1f} percent"


# ── markets ──

def _symbol(name: str) -> tuple[str, str] | None:
    data = _get(f"https://query1.finance.yahoo.com/v1/finance/search?q={requests.utils.quote(name)}"
                "&quotesCount=5&newsCount=0", 86400)
    for q in (data or {}).get("quotes") or []:
        if q.get("quoteType") in ("EQUITY", "ETF") and q.get("symbol"):
            return q["symbol"], q.get("shortname") or q.get("longname") or q["symbol"]
    return None


def _short(company: str) -> str:
    """"Apple Inc." → "Apple", "Exxon Mobil Corporation" → "Exxon Mobil"."""
    name, prev = company.strip(), None
    while name != prev:
        prev, name = name, re.sub(r"[,.]?\s+(?:Inc|Corp|Corporation|Company|Co|Holdings?|Ltd|plc|N\.?V|"
                                  r"S\.?A|Group|Class\s+[A-C])\.?$", "", name, flags=re.I)
    return name or company


def market_answer(kind: str, key: str) -> str | None:
    if kind == "crypto":
        data = _get(f"https://api.coingecko.com/api/v3/simple/price?ids={key}"
                    "&vs_currencies=usd&include_24hr_change=true", 60)
        row = (data or {}).get(key)
        if not row or row.get("usd") is None:
            return None
        name = "XRP" if key == "ripple" else key.capitalize()
        return f"{name} is at {_money(row['usd'])}{_change(row.get('usd_24h_change'))} in the last day."
    if kind == "stock":
        hit = _symbol(key)
        if not hit:
            return f"I couldn't find a stock called {key}."
        symbol, company = hit
        label = f"{_short(company)} stock"
    else:
        symbol = key
        label = {"^GSPC": "The S&P 500", "^DJI": "The Dow", "^IXIC": "The Nasdaq"}[key]
    data = _get(f"https://query1.finance.yahoo.com/v8/finance/chart/{requests.utils.quote(symbol)}"
                "?range=1d&interval=1d", 60)
    meta = (((data or {}).get("chart") or {}).get("result") or [{}])[0].get("meta") or {}
    price, prev = meta.get("regularMarketPrice"), meta.get("chartPreviousClose")
    if price is None:
        return None
    pct = (price - prev) / prev * 100 if prev else None
    traded = meta.get("regularMarketTime")
    live = traded and time.time() - traded < 20 * 60
    amount = _money(price) if kind == "stock" else f"{price:,.0f}"
    if kind == "stock":
        label = label[:1].upper() + label[1:]
    verb = "is at" if live else "closed at"
    if not live and traded and datetime.fromtimestamp(traded).date() != datetime.now().date():
        verb = f"closed {datetime.fromtimestamp(traded):%A} at"
    return f"{label} {verb} {amount}{_change(pct)}."


# ── air quality and pollen ──

_AQI = [(50, "good"), (100, "moderate"), (150, "unhealthy for sensitive groups"),
        (200, "unhealthy"), (300, "very unhealthy"), (10**6, "hazardous")]
_POLLEN = [(2.4, "low"), (4.8, "low to medium"), (7.2, "medium"), (9.6, "medium to high"),
           (12.1, "high")]


def air_answer() -> str | None:
    data = _get("https://air-quality-api.open-meteo.com/v1/air-quality"
                f"?latitude={settings.WEATHER_HOME_LAT}&longitude={settings.WEATHER_HOME_LON}"
                "&current=us_aqi&hourly=us_aqi&forecast_days=1&timezone=auto", 900)
    cur = ((data or {}).get("current") or {}).get("us_aqi")
    if cur is None:
        return None
    word = next(w for top, w in _AQI if cur <= top)
    later = [v for v in ((data.get("hourly") or {}).get("us_aqi") or []) if v is not None]
    peak = max(later) if later else cur
    tail = ""
    if peak > 100 >= cur:
        tail = f" It's forecast to reach {next(w for top, w in _AQI if peak <= top)} later today."
    advice = " Sensitive groups should take it easy outside." if 100 < cur <= 150 else (
        " It's a good day to limit time outside." if cur > 150 else "")
    return f"The air quality is {word}, with an index of {round(cur)}.{advice}{tail}"


def pollen_answer() -> str | None:
    zip_code = str(settings.DEFAULT_WEATHER_LOCATION or "")
    if not re.fullmatch(r"\d{5}", zip_code):
        return None
    url = f"https://www.pollen.com/api/forecast/current/pollen/{zip_code}"
    data = _get(url, 3 * 3600, headers={"Referer": f"https://www.pollen.com/forecast/current/pollen/{zip_code}"})
    periods = ((data or {}).get("Location") or {}).get("periods") or []
    today = next((p for p in periods if p.get("Type") == "Today"), None)
    if not today or today.get("Index") is None:
        return None
    idx = float(today["Index"])
    word = next(w for top, w in _POLLEN if idx <= top)
    names = [t.get("Name", "").lower() for t in today.get("Triggers") or [] if t.get("Name")][:3]
    from_ = ""
    if names:
        names = [n if n != "grasses" else "grass" for n in names]
        joined = names[0] if len(names) == 1 else ", ".join(names[:-1]) + f" and {names[-1]}"
        from_ = f", mostly {joined}"
    tomorrow = next((p for p in periods if p.get("Type") == "Tomorrow"), None)
    tail = ""
    if tomorrow and tomorrow.get("Index") is not None:
        w2 = next(w for top, w in _POLLEN if float(tomorrow["Index"]) <= top)
        if w2 != word:
            tail = f" Tomorrow looks {w2}."
    return f"Pollen is {word} today{from_}.{tail}"
