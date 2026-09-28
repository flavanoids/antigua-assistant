# Skill: Calculator & Unit Conversion

**Status:** Active
**Pipeline stage:** LLM bypass — Python computes and phrases the answer, LLM never called

---

## What It Does

Intercepts arithmetic, percentage, tip, unit/temperature-conversion, and
currency questions and answers them from pure Python (`calc.answer()`),
skipping the ~1–2s LLM round-trip.

Arithmetic and conversions are fully offline — they work on the fallback
server too. Currency is the one part that needs the network: daily reference
rates from `open.er-api.com` (no key), cached with a TTL and stale-served on
failure, wired in through `Backend.rates_provider`. Where that provider is
absent (fallback server, `calc.currency.enabled: false`), currency questions
fall through to the LLM unchanged.

---

## How Users Trigger It

**Arithmetic:**
- "What's 12 times 13?" / "100 divided by 4" / "5 plus 8" / "7 minus 12"

**Percentages & tips:**
- "What's 15% of 80?" / "20 percent off 40 dollars"
- "Add 20% to 45"
- "What's a 20% tip on a 47 dollar check?" (defaults to 20% if no rate given)

**Fractions / doubling:**
- "Half of 250" / "three quarters of 200" / "double 1500"

**Unit conversion** (length, mass, volume, cooking, time, speed):
- "How many ml in 2 cups?" / "how many tablespoons in a cup"
- "5 miles in km" / "convert 3 feet to centimeters"
- "How many minutes in 2 hours?"

**Temperature:**
- "350 Fahrenheit in Celsius" / "20 C to F" / "convert 300 kelvin to celsius"

**Currency:**
- "50 dollars in euros" / "how much is 20 pounds in dollars"
- "convert 100 yen to dollars" / "$50 to pesos"

**Does NOT fire on:**
- "How many calories in a banana?" (not a unit — LLM)
- "How many days until Christmas?" (date math — LLM)
- "45 dollars plus 8% tax" (calc bails when a percent it can't place appears)
- "50 dollars in rubles" (currency not in the ~30 covered — LLM)
- Cross-category nonsense like "cups in a mile" (returns nothing → LLM declines)

---

## Detection

`_CALC_ROUTE_RE` in `classify.py` — a whitelist, like the search layers. It is
checked **after** the volume / timer / weather / memory routes, so "add 5
minutes to the timer" and "is it warmer than yesterday" are claimed first. A
regex that is slightly too generous only costs a fall-through: `calc.answer()`
returns `None` on anything it can't actually compute and the turn continues to
the LLM tail.

---

## Response Phrasing

Built in `calc.py` so `clean_for_tts()` speaks it cleanly:

- **No thousands separators** — bare integers only; `num2words` says "one
  thousand", never "one comma zero zero zero".
- **Results rounded** — arithmetic to 2 decimals ("about 3.33"), conversions to
  a spoken-friendly precision, temperature to one decimal.
- **Money spelled in-module** — `_money()` uses `num2words` + the correct
  currency noun and splits cents ("nine dollars and forty cents"), because
  `tts_text._cents_sub` / `currency_sub` hardcode "dollars".
- **Negatives** — "negative 5", not "-5".
- Division by zero → "You can't divide by zero."

The arithmetic evaluator is `ast.parse` + a node whitelist (`+ - * / %`,
unary minus) — **not** `eval` on user text.

---

## Code Location

| What | Where |
|---|---|
| Whole skill | `server/antigua_core/calc.py` |
| Entry point | `calc.answer(transcript, *, rates=None)` |
| Currency + rates cache | `server/antigua_core/calc_currency.py` (`RatesProvider`) |
| Routing regex | `_CALC_ROUTE_RE` (`classify.py`, above the volume block) |
| Route hook | `_handle_calc()` (`pipeline.py`); `_HANDLERS["calc"]` |
| Provider wiring | `rates_provider` (`antigua_server.py`), `Backend.rates_provider` |
| Snapshot + TTS tests | `tests/test_calc.py` (currency via a fake rates stub) |
| Routing fixtures | `tests/fixtures/routing.yaml` (`calc:`) |

---

## Config Knobs

Units and factors are static tables in `calc.py`. Currency (`server.yaml`):

```yaml
calc:
  currency:
    enabled: true
    ttl_seconds: 21600     # ECB publishes once per business day
    base: USD              # table kept warm on boot; others fetched on demand
    timeout_seconds: 4
```

---

## Limitations

- No algebra, word problems, square roots, or date math — those go to the LLM.
- Conversions are single-unit ("2 cups", not "1 lb 4 oz").
- One representative factor per unit (US customary; "ounce" is weight, "fluid
  ounce" is volume — say "fluid ounce" explicitly).
- "double 1500" is spoken "double fifteen hundred" (the year regex in
  `tts_text` claims 1000–2199).
- Currency: ~30 common currencies (`CURRENCY_ALIASES`), daily reference rates
  only (not live/tradeable), single hop. Uncovered pairs fall to the LLM. On a
  cold cache the first currency question waits for the fetch (≤4s) or falls
  through; the boot prewarm normally avoids that.

---

## How to Extend

**Add a currency:** add its spoken aliases → ISO code to
`calc_currency.CURRENCY_ALIASES` and the spoken singular/plural to
`CURRENCY_WORDS` (and `NO_SUBUNIT` if it has no cents). `open.er-api.com`
already returns it.

**Add a unit:** add the canonical name + base-unit factor to the right table
(`_LENGTH` / `_MASS` / `_VOLUME` / `_TIME` / `_SPEED`) and its spoken aliases to
`_ALIAS`. Add a snapshot to `tests/test_calc.py`.
