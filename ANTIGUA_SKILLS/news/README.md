# Skill: News Headlines

**Status:** Active  
**Pipeline stage:** Context injection with token override (LLM gets headlines as `extra_context`)

---

## What It Does

Detects news requests, fetches cached headlines from configured RSS feeds, and injects them as extra context for the LLM. The LLM reads up to 5 headlines and summarizes them naturally, attributing each to its source. Also supports Google News topic searches for on-demand topic filtering.

---

## How Users Trigger It

- "What's in the news?"
- "Give me the headlines"
- "What's the latest?"
- "Al Jazeera headlines"
- "Reuters headlines on Ukraine"
- "News about the Middle East"
- "What's happening in Texas?"

---

## Detection

Three-layer detection:

```python
is_news_request(transcript)      # bool — general news intent
extract_news_source(transcript)  # "aljazeera" | "reuters" | "bbc" | "guardian" | None
extract_news_topic(transcript)   # "Ukraine" | "Middle East" | None
```

Patterns: `_NEWS_INTENT_RE`, `_NEWS_SOURCE_RE`, `_NEWS_TOPIC_RE`, `_NEWS_LOCATION_RE`  
All in `server/antigua_server.py`.

---

## Response

LLM-generated with injected headlines. When a news request is detected:

1. `news_cache.format_for_prompt(source_filter, topic_filter)` builds a numbered headline list
2. That string is passed to `ask_llm()` as `extra_context` (replaces the default weather context)
3. `max_tokens` is overridden to `news.max_tokens_news` (default 150) to allow longer responses

```python
news_context = news_cache.format_for_prompt(source_filter, topic_filter)
llm_result = ask_llm(transcript, extra_context=news_context, max_tokens_override=news_tokens)
```

---

## Code Location

| What | Where |
|---|---|
| Cache class | `NewsCache` (~line 184, `server/antigua_server.py`) |
| RSS fetch | `NewsCache._fetch(name, url)` |
| Google News topic fetch | `NewsCache._fetch_google_news_topic(topic)` |
| Format for prompt | `NewsCache.format_for_prompt(source_filter, topic_filter)` |
| Intent detection | `is_news_request()` (~line 532) |
| Source extraction | `extract_news_source()` (~line 536) |
| Topic extraction | `extract_news_topic()` (~line 550) |
| Pipeline hook | `run_pipeline()` — "News request" block (~line 1168) |
| Cache instance | `news_cache = NewsCache()` (module-level) |
| Cache pre-warm | `main()` — threads started at boot to pre-fetch each source |

---

## Config Knobs (`server/config/server.yaml`)

```yaml
news:
  ttl_seconds: 1200         # Cache refresh interval (default: 20 min)
  max_headlines: 5          # Headlines fetched per source before topic filtering
  max_tokens_news: 150      # LLM token budget for news responses (vs. default 65)
  sources:
    - name: aljazeera
      url: https://www.aljazeera.com/xml/rss/all.xml
    - name: bbc
      url: https://feeds.bbci.co.uk/news/world/middle_east/rss.xml
    - name: guardian
      url: https://www.theguardian.com/world/middleeast/rss
```

**To add a news source:** append a `name`/`url` entry under `sources`. The name is used for source filtering by voice ("Reuters headlines") and must match `_NEWS_SOURCE_RE` labels or be added there.

---

## MQTT Events

None specific to news.

---

## Limitations

- Topic filtering uses keyword matching against titles and summaries — not semantic search. "News about climate" works; niche phrasing may miss articles.
- Google News topic search is fetched live (not cached), so topic queries add ~0.5-1s latency.
- RSS feeds may embed HTML or navigation fragments in summaries. `NewsCache._fetch()` strips common ones but may miss edge cases.
- No opinion or analysis — the LLM is instructed to read headlines as-is.

---

## How to Extend

**Add a new RSS source:** Add to `news.sources` in `server.yaml`. If users should be able to ask for it by name ("BBC headlines"), also add the name to `_NEWS_SOURCE_RE` and the label map in `NewsCache.format_for_prompt()`.

**Change how many headlines the LLM reads:** Adjust `max_headlines` in config (fetched per source) or the `[:5]` slice in `NewsCache.format_for_prompt()`.

**Add a new intent phrase:** Add to `_NEWS_INTENT_RE` regex in `antigua_server.py`.

**Separate news into its own module:** `NewsCache`, its regexes, and detection functions are self-contained — they can be moved to `server/skills/news.py` and imported without changing the pipeline hook in `run_pipeline()`.
