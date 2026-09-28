# Skill: Weather

**Status:** Active
**Pipeline stage:** Deterministic route (`weather`) + LLM context injection

---

## What It Does

Answers weather questions with deterministic, spoken sentences — no LLM in the
loop, so an answer can't hallucinate, ramble, or stall. Covers any location and
any timeframe the household actually asks about.

**Data source:** [Open-Meteo](https://open-meteo.com) — free, no API key.
- `api.open-meteo.com/v1/forecast` — current, 48h hourly (temperature +
  `precipitation_probability` + weather code), 7-day daily, `past_days=1` for
  "warmer than yesterday".
- `geocoding-api.open-meteo.com/v1/search` — city → lat/lon/timezone/country.
- `api.weather.gov` (NWS) — active alerts, US only. Unchanged from before.

wttr.in is no longer queried for spoken answers; `WeatherCache` is kept only as a
fallback for the legacy LLM-prompt path and the offline fallback server.

---

## Three resolvers

`server/antigua_core/weather.py` turns a transcript into an answer through three
orthogonal steps:

| Resolver | Question | Examples |
|---|---|---|
| `classify_weather_intent()` | **what** | rain, umbrella, jacket/"is it cold", temperature, general, sunset, sunrise, wind, "warmer than yesterday" |
| `resolve_timeframe()` | **when** | now, "soon" (next 3h), "later today", this morning/afternoon/evening, tonight, tomorrow(+part), "this Thursday", "this weekend", "next weekend", "this week", "the next few days", "in 3 days", beyond-horizon (months/seasons) |
| `extract_location_query()` + `GeocodeCache` | **where** | home, "in Denver", "Portland, Maine", "Hawaii" (→ Honolulu), "the whole country of Guatemala" (→ "Across Guatemala") |

Each `phrase_*()` function is pure: `(Forecast, Timeframe, now) -> str`. One
sentence, direct answer first, ≤25 words. All snapshot-tested in
`tests/test_weather.py`, including through `clean_for_tts()`.

---

## How Users Trigger It

Routing lives in `classify()` (`server/antigua_core/classify.py`):

- `_WEATHER_SIMPLE_RE` — early, anchored forms ("what's the weather", "will it
  rain", "what's the forecast"). Routes to `weather` for **any** location.
- `_WEATHER_ROUTE_RE` — checked late (after timers/memory/lists so "remind me to
  grab an umbrella" stays a memory). Broad question-shaped phrasings: "do I need
  a jacket", "is it chilly out", "chance of rain", "when's sunset", "colder than
  yesterday".

A weather-ish turn the regexes miss is logged as `weather_intent_miss:` in
`pipeline.dispatch_text()` — grep the logs and widen `_WEATHER_ROUTE_RE` toward
how the household actually phrases things.

### Examples

| She says | Antigua says |
|---|---|
| "is it going to rain today" | Yes, today has about a 70 percent chance of rain. |
| "will it rain this afternoon" | Yes — the best chance this afternoon is around noon, about 65 percent. |
| "is it going to rain later" | Dry right now, but rain's likely later, around 5 PM, about 75 percent. |
| "any rain this weekend" | It looks like Saturday is the wet one, around 80 percent. The other days look drier. |
| "do I need an umbrella" | Yes, I'd take one — rain's likely today, around 75 percent. |
| "is it cold out" | Yes, bundle up — it's cold right now, around 40. |
| "what's the weather" | It's 64 and foggy right now, heading for a high near 80. |
| "what's the forecast for Saturday" | Saturday: cloudy, high near 90, low around 70. |
| "what's the weather next Tuesday" | Early signs point to Tuesday: partly cloudy, high near 85, low around 70. |
| "is it warmer than yesterday" | About 13 degrees warmer than yesterday, near 85. |
| "weather in Denver" | In Denver, it's 58 and drizzly right now, heading for a high near 70. |
| "weather in Hawaii" | In Honolulu, it's 82 and sunny right now, heading for a high near 86. |
| "weather in Guatemala" | Across Guatemala, it's 70 and drizzly right now, heading for a high near 80. |
| "weather in Springfield" | Do you mean Springfield, Missouri or Springfield, Illinois? |
| "what's the weather in December" | I can only see about a week ahead — want the forecast through Friday? |

---

## Confidence-graded language

`_hedge(days_out)` — the forecast's certainty shapes the wording:

| Days out | Lead-in |
|---|---|
| 0–3 | flat statement |
| 4–5 | "it looks like …" |
| 6–7 | "early signs point to …" |

---

## Locations

- **Home** — `weather.home_lat` / `home_lon` / `home_tz` in `server.yaml`
  (defaults to the NWS lat/lon).
- **Named city** — geocoded once, then cached in `data/geocode_cache.json`.
  Repeat questions are instant.
- **US state** — a built-in state→city table (`_US_STATES`); Open-Meteo
  geocoding has no reliable US-state entries.
- **Region / country** — the centroid is used and the answer says "Across
  <name>," so the scope is honest.
- **Ambiguous name** (Springfield, Portland) — if two candidates have comparable
  population in different states, Antigua asks which one. Otherwise it takes the
  top hit and names the state so she can correct it.
- **Corrections stick** — "no, Portland Maine" is remembered in
  `geocode_cache.json` under `corrections` and wins next time.
- **Timezone** — "this afternoon" / "tomorrow" / "Thursday" anchor to the
  *target location's* local date, not the home clock.
- **No interruption** — geocoding + forecast run within a ~4s budget
  (`weather.timeout_seconds`); a cold/unreachable city gets one clean sentence,
  never a hang.

---

## Degradation

| Situation | Response |
|---|---|
| No data at all | "I can't reach the weather service right now. Try again in a few minutes." |
| Stale data (> 4× TTL) | answer + " (my data's a little old right now)" |
| Hour-level data missing that far out | falls back to the whole-day summary |
| Day past the 7-day horizon | "I can only see about a week ahead — want the forecast through <last day>?" |

The `"Hmm, I'm not quite sure"` LLM-empty fallback is unreachable for a
recognized weather intent.

---

## Proactive severe-weather alerts

`_severe_alert_watch()` in `antigua_server.py` polls the NWS alert cache. A new
**Severe or Extreme** alert for the home point is spoken unprompted via
`antigua/play` ("Heads up — <headline>."). Minor/Moderate advisories stay
silent; each alert fires at most once per process. Toggle with
`weather.severe_alerts` (default on).

---

## LLM context injection

On weather-relevant turns (`wants_weather_context()`),
`weather.context_for_prompt()` injects a compact block scoped to the location
and timeframe the turn is about — not the whole 7-day dump. `weather_claim_allowed()`
still drops invented weather sentences on turns where nothing was injected.

---

## Config (`server/config/server.yaml`)

```yaml
weather:
  ttl_seconds: 900
  nws_lat: 40.713
  nws_lon: -74.006
  alerts_ttl_seconds: 600
  home_lat: 40.713          # Open-Meteo home point (defaults to nws_lat/lon)
  home_lon: -74.006
  home_tz: "America/New_York"
  timeout_seconds: 4
  severe_alerts: true
  prewarm_cities: []        # ["Denver", "Portland, Maine"] — kept warm on boot
  phrasing:
    rain_likely_pct: 50
    rain_slight_pct: 20
    jacket_below_f: 58
    notable_wind_mph: 20
```

---

## Code Map

| What | Where |
|---|---|
| Whole skill | `server/antigua_core/weather.py` |
| Forecast fetch + per-location cache | `WeatherProvider` |
| Geocoding + corrections + US-state table | `GeocodeCache` |
| Timeframe parsing | `resolve_timeframe()` (shares `resolve_day_offset()` with the alarm parser in `classify.py`) |
| Intent detection | `classify_weather_intent()` (weather.py) / `_WEATHER_ROUTE_RE` (classify.py) |
| Spoken phrasing | `phrase_*()` |
| Entry point | `weather.answer(transcript, provider)` |
| LLM prompt block | `weather.context_for_prompt()` |
| Route handler | `_handle_weather()` (`pipeline.py`) |
| Provider instance | `weather_provider` (`antigua_server.py`) |
| Severe-alert thread | `_severe_alert_watch()` (`antigua_server.py`) |
| Snapshot tests | `tests/test_weather.py` |
| Routing fixtures | `tests/fixtures/routing.yaml` (`weather:`) |

---

## Limitations

- Forecast horizon is 7 days; months and seasons return the "about a week
  ahead" message.
- NWS alerts are US-only; non-US locations get no alert data (and Antigua does
  not claim "no alerts" when it can't check).
- "Will it snow?" in a place that never snows is answered as a rain question
  ("mostly dry") rather than "no snow expected" — the phraser sees the forecast,
  not the noun she used.
- Region/country answers use a single centroid point, not a national roundup.
- Open-Meteo has no SLA, but it is fast and stable; on failure the skill serves
  stale data or the "can't reach it" line.
