"""LLM search router, shared by both servers.

The regex layers in classify are a whitelist and always will be: "rotten
tomatoes rating for the new Dune movie" and "how do I install a Moen 1225
cartridge" are exactly the shapes they can't enumerate. When nothing matches,
ask the model itself. Measured on qwen3.5:4b: 16/17 correct, ~0.5s.
"""

import logging
import re
from datetime import datetime

import requests

log = logging.getLogger("antigua_core")

_ROUTER_PROMPT = """You route voice-assistant questions. Today is {today}.
Reply with ONE line only.

If answering needs current, local, or highly specific information from the web \
(prices, ratings, reviews, schedules, results, news, store hours, availability, \
recent events), reply:
SEARCH: <a short keyword web query>

Also reply SEARCH whenever the question names a specific product, model number, \
part number, brand, movie, show, team, or company — even for how-to questions. \
"How do I install a Moen 1225 cartridge" is SEARCH.

Otherwise (chit-chat, general knowledge, math, definitions, cooking basics, \
history, opinions, anything about this house), reply:
NONE

Query rules: plain keywords only. No quotation marks. Do not add a year unless \
the user said one. Keep the user's own words for names, brands and part numbers.

Question: {q}"""


def llm_route_search(transcript: str, *, ollama_host, model, timeout,
                     keep_alive="15m", num_ctx=8192, enabled=True):
    """Ask the LLM whether this needs the web. Returns a query, or None.

    Runs only after every skill and every regex layer has declined, so the
    ~0.5s it costs never lands on a question something faster could answer.
    """
    if not enabled or len(transcript.split()) < 3:
        return None
    prompt = _ROUTER_PROMPT.format(
        today=datetime.now().strftime("%A, %B %-d, %Y"), q=transcript.strip()
    )
    try:
        resp = requests.post(
            f"{ollama_host}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                # Without this the model spends its whole budget on reasoning
                # tokens and returns an empty response.
                "think": False,
                # Must match the chat calls, or Ollama reloads the model.
                "options": {"temperature": 0, "num_predict": 32, "num_ctx": num_ctx},
                "keep_alive": keep_alive,
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        lines = (resp.json().get("response") or "").strip().splitlines()
    except Exception as e:
        log.warning("Search router failed (answering without search): %s", e)
        return None

    line = lines[0].strip() if lines else ""
    if not line.upper().startswith("SEARCH") or ":" not in line:
        return None
    # Drop quotes entirely: the model reaches for phrase search on its own and
    # a wrong phrase ("Dune 2024") returns nothing at all rather than something
    # close. Keywords degrade gracefully; phrases don't. Same for the operators
    # and placeholders it invents when the transcript is garbled — one query
    # came back as "We team name + 20-26 FIFA World Cup winner".
    query = re.sub(r"[\"'+*|]", " ", line.split(":", 1)[1])
    query = re.sub(r"\b(?:team|player|movie|product)\s+name\b", " ", query, flags=re.I)
    query = re.sub(r"<[^>]*>", " ", query)
    return " ".join(query.split()) or None
