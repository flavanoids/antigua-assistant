"""Currency conversion for the calc skill.

The one part of calc that needs the network: daily reference rates from the
Frankfurter API (European Central Bank data, no key). Cached with a TTL and
stale-served on failure, the same shape as weather.WeatherProvider.

Wired into calc.answer() through Backend.rates_provider; absent on the offline
fallback server, where a currency question falls through to the LLM unchanged.
"""

import json
import logging
import threading
import time
import urllib.request

from . import settings

log = logging.getLogger("antigua_core")

# open.er-api.com — the free, no-key tier of exchangerate-api.com. Daily rates,
# ~160 currencies, `rates` always includes the base at 1.0.
_LATEST_URL = "https://open.er-api.com/v6/latest"

# Spoken / written names → ISO 4217. Frankfurter covers ~30 currencies; this is
# the subset a household is plausibly going to ask about.
CURRENCY_ALIASES = {
    "$": "USD", "dollar": "USD", "dollars": "USD", "usd": "USD",
    "us dollar": "USD", "us dollars": "USD", "bucks": "USD", "buck": "USD",
    "€": "EUR", "euro": "EUR", "euros": "EUR", "eur": "EUR",
    "£": "GBP", "pound": "GBP", "pounds": "GBP", "gbp": "GBP",
    "quid": "GBP", "pound sterling": "GBP",
    "¥": "JPY", "yen": "JPY", "jpy": "JPY",
    "peso": "MXN", "pesos": "MXN", "mxn": "MXN", "mexican peso": "MXN",
    "mexican pesos": "MXN",
    "canadian dollar": "CAD", "canadian dollars": "CAD", "cad": "CAD",
    "loonie": "CAD",
    "australian dollar": "AUD", "australian dollars": "AUD", "aud": "AUD",
    "franc": "CHF", "francs": "CHF", "swiss franc": "CHF", "swiss francs": "CHF",
    "chf": "CHF",
    "yuan": "CNY", "renminbi": "CNY", "rmb": "CNY", "cny": "CNY",
    "rupee": "INR", "rupees": "INR", "inr": "INR", "indian rupee": "INR",
    "indian rupees": "INR",
    "won": "KRW", "krw": "KRW", "korean won": "KRW",
    "real": "BRL", "reais": "BRL", "brl": "BRL", "brazilian real": "BRL",
    "krona": "SEK", "kronor": "SEK", "sek": "SEK", "swedish krona": "SEK",
    "zloty": "PLN", "pln": "PLN",
    "rand": "ZAR", "zar": "ZAR", "south african rand": "ZAR",
    "hong kong dollar": "HKD", "hong kong dollars": "HKD", "hkd": "HKD",
    "new zealand dollar": "NZD", "new zealand dollars": "NZD", "nzd": "NZD",
    "shekel": "ILS", "shekels": "ILS", "ils": "ILS",
    "lira": "TRY", "try": "TRY", "turkish lira": "TRY",
}

# Spoken singular / plural for the answer.
CURRENCY_WORDS = {
    "USD": ("dollar", "dollars"), "EUR": ("euro", "euros"),
    "GBP": ("pound", "pounds"), "JPY": ("yen", "yen"),
    "MXN": ("peso", "pesos"), "CAD": ("Canadian dollar", "Canadian dollars"),
    "AUD": ("Australian dollar", "Australian dollars"),
    "CHF": ("Swiss franc", "Swiss francs"), "CNY": ("yuan", "yuan"),
    "INR": ("rupee", "rupees"), "KRW": ("won", "won"),
    "BRL": ("real", "reais"), "SEK": ("krona", "kronor"),
    "PLN": ("zloty", "zloty"), "ZAR": ("rand", "rand"),
    "HKD": ("Hong Kong dollar", "Hong Kong dollars"),
    "NZD": ("New Zealand dollar", "New Zealand dollars"),
    "ILS": ("shekel", "shekels"), "TRY": ("lira", "lira"),
}

# Whole-unit currencies — cents phrasing doesn't apply.
NO_SUBUNIT = {"JPY", "KRW"}


def canonical_currency(name: str):
    n = " ".join(name.strip().lower().split())
    if n in CURRENCY_ALIASES:
        return CURRENCY_ALIASES[n]
    if n.endswith("s") and n[:-1] in CURRENCY_ALIASES:
        return CURRENCY_ALIASES[n[:-1]]
    return None


class RatesProvider:
    """Cached ECB reference rates. get_rate(src, dst) -> (rate, stale) | None."""

    _FAIL_TTL = 120

    def __init__(self):
        self._cache: dict[str, tuple[dict, float]] = {}   # base -> (rates, at)
        self._lock = threading.Lock()

    @property
    def _ttl(self) -> int:
        return settings.CALC_CURRENCY_TTL

    def get_rate(self, src: str, dst: str):
        if src == dst:
            return (1.0, False)
        table = self._table(src)
        if table is None or dst not in table[0]:
            return None
        rates, at = table
        stale = time.time() - at >= self._ttl * 4
        return (rates[dst], stale)

    def prewarm(self):
        threading.Thread(target=self._table,
                         args=(settings.CALC_CURRENCY_BASE,), daemon=True).start()

    def _table(self, base: str):
        now = time.time()
        with self._lock:
            hit = self._cache.get(base)
        if hit and now - hit[1] < self._ttl:
            return hit
        if hit:
            threading.Thread(target=self._refresh, args=(base,), daemon=True).start()
            return hit  # serve stale while refreshing
        return self._refresh(base)

    def _refresh(self, base: str):
        try:
            url = f"{_LATEST_URL}/{base}"
            req = urllib.request.Request(url, headers={"User-Agent": "antigua/1.0"})
            with urllib.request.urlopen(req, timeout=settings.CALC_CURRENCY_TIMEOUT) as r:
                raw = json.loads(r.read())
            if raw.get("result") not in (None, "success"):
                raise ValueError(f"provider said {raw.get('result')!r}")
            rates = {k: float(v) for k, v in raw["rates"].items()}
            rates[base] = 1.0
            entry = (rates, time.time())
            with self._lock:
                self._cache[base] = entry
            log.info("fx rates fetched: base %s, %d currencies", base, len(rates))
            return entry
        except Exception as e:  # noqa: BLE001
            log.warning("fx rate fetch failed (base %s): %s", base, e)
            with self._lock:
                if base in self._cache:
                    return self._cache[base]
                self._cache[base] = ({}, time.time() - self._ttl + self._FAIL_TTL)
            return None
