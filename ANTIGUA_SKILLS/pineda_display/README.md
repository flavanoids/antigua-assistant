# Skill: PinedaDisplay

**Status:** Active on the primary. The fallback has no `pineda:` url and says it can't.
**Pipeline stage:** Pre-LLM (route `pineda`); "tell me more about this quote" continues into the LLM tail
**LLM involved:** Only for "tell me more about this quote"

---

## What It Does

Drives [PinedaDisplay Web](https://github.com/flavanoids/PinedaDisplay), the
dashboard on the airplaypi kiosk screen. pineda-web runs on noegpu01 next to
Antigua (`:8090`), so this is plain HTTP to localhost; the restart and reboot
endpoints reach airplaypi over SSH themselves.

| Ask | Does | pineda-web call |
|---|---|---|
| "change the theme to ocean" / "switch to the groovy theme" | Sets the kiosk profile's theme (saved) | `PATCH /api/profiles/{profile}` |
| "change the theme" / "random theme" / "surprise me with a new theme" | A random day theme, never the current one | same |
| "what themes are there" / "what theme is this" | Lists / names them | `GET /api/themes`, `GET /api/profiles` |
| "restart the display" / "refresh the screen" | Restarts cage + Chromium on airplaypi | `POST /api/control/restart-display` |
| "reboot the pi" / "restart airplaypi" | Asks "Say yes to reboot", then reboots airplaypi 10 s later | `POST /api/control/restart-system` |
| "who said this quote" | "That's Maya Angelou, in 1978." | `GET /api/now-showing` |
| "read the quote" | The quote and who said it | same |
| "tell me more about this quote" / "what does the quote mean" | Searches author + quote, then the LLM explains, grounded in both | same, then SearXNG |
| "say the Spanish phrase" / "what's the word of the day" | Spanish voice, then the meaning in English, then Spanish again | same |
| "when was this photo taken" | "June 14th, 2019, around 7 in the evening." | same |
| (a recipe opens, is rescaled or swapped) | The recipe full screen for 120 s | `PUT /api/takeover` |
| "can you show the recipe again" / "show me the ingredients" | The open recipe, or the last one shown | same |
| (a step is read: "next", "back", "step 4") | Moves the step ladder there and lights the ingredients it uses, if the card's up | `PATCH /api/takeover` |
| (an ingredient comes up: the checklist, "how much salt", a substitute) | Lights that line; skips are struck through, swaps show "→ coconut milk" | same |
| "stop the display" / "have the display go back" / "hide the recipe" | Takes it down ("already back to normal" if nothing's up) | `DELETE /api/takeover` |

Running timers also show on the display: every set, cancel, extension or
fire pushes the whole list to `PUT /api/timers/sync/antigua`, and the
moon-climate mini card turns 2×2 with the soonest in one quadrant ("pasta
4:12 +1"). Alarms and reminders aren't mirrored.

**The recipe card.** When the recipes skill opens a recipe, the kiosk shows
it full screen in the profile theme's colours and fonts: title, source,
servings and time over the ingredients (grouped as on the page, at the
session's scale) beside a preview of step 1. Once the steps start it turns
into a ladder: the step being read as large as it fits, the one before and
the one after smaller and dimmed, beside a narrow rail of ingredients. It
comes down after `recipe_seconds` (120), on "stop the display", or when the
recipe ends ("stop the recipe").

What's being read is lit, and moves with `PATCH /api/takeover`, which only
restyles a card that's still up and doesn't add time, so a dismissed or
timed-out card stays down; "show" is the word that brings it back, as it
stands. Lit, in order (`_session_card`): the lines this turn's answer was
about ("how much salt", "what's the third ingredient", one turn only), the
line being checked one at a time or offered a substitute for, then the
lines the current step uses. Those come from `recipe_subs.step_ingredients`:
whole names first ("garam masala"), then a name's last word ("the yogurt",
not a vague one like "the juices"), a cut's first word ("the chicken" for
chicken thighs), "the sauce ingredients" for a group, "the remaining
ingredients"; never the dish's own name ("chicken tikka masala"); a name
listed twice goes to the copy not used yet, then the one used last.
Checklist answers so far are quieter; skipped lines are struck through, and
swapped ones show what's used instead. The last card is kept in `data/pineda_recipe_card.json`, so "show the
recipe again" works after the recipe ends and across restarts. "Show me the
recipe for lasagna" is a new recipe, not this; "go back" stays the previous
step; "close the recipe" ends the recipe. These phrases are checked ahead of
the recipe session (`_handle_recipe_display`).

---

## Details worth knowing

- **Theme: the profile, not the server.** pineda-web resolves a screen's
  theme as `profile.theme or settings.theme`. The kiosk's profile has its own
  theme, so `POST /api/themes/apply` would change nothing visible. Night twins
  (`ocean_night` …) aren't offered by name; each light theme switches to its
  twin at sundown by itself.
- **"This photo" / "this quote".** The slideshow and the quote rotation run in
  the browser, so the cards report every swap to `POST /api/now-showing`.
  With `pineda.device` unset Antigua takes the most recent report from any
  screen, which is the kiosk unless a phone has the dashboard open. The
  kiosk gets a new device id each time its browser cache is cleared, so
  pinning one isn't worth it.
- **Photo dates are the camera's clock.** `taken_local` is EXIF
  `DateTimeOriginal`, with no time zone. About 18 of the photos (screenshots,
  edited images) have none: "That one doesn't have a date."
- **Reboot waits for its own reply.** airplaypi plays Antigua's replies, so
  the reboot call waits `reboot_delay_seconds` (10) for "Rebooting airplaypi"
  to finish. Music and Antigua's voice are gone for about a minute.
- **Routing.** The route sits before `tv` (whose input regex would take
  "switch to the ocean theme"). "Theme" never matches with play / put on /
  listen, or as "the theme of / from / for / song", so "play the Jeopardy
  theme" stays music and "what's the theme of Macbeth" stays the LLM. A quote
  or phrase needs "the / this / that" in front, so "give me a quote" and
  "what does the phrase break a leg mean" still reach the LLM.

---

## Code

| File | What |
|---|---|
| `server/antigua_core/intents/pineda.py` | `parse_pineda_request()`, `parse_recipe_display()`: regex only, no network |
| `server/antigua_core/pineda.py` | `PinedaClient`, `TimerMirror`, `recipe_card()` / `RecipeDisplay`, theme matching, reply formatting, WAV joining |
| `server/antigua_core/pipeline.py` | `_handle_pineda`, `_pineda_theme`, `_pineda_phrase`, `_handle_pending_reboot`, `_handle_recipe_display`, the mirror in `_recipe_reply` |
| `server/antigua_server.py` | Builds the client from `pineda:` and hooks `TimerManager(on_change=…)` |
| `tests/test_pineda.py` | Parser, replies, timer mirror, and the route against a fake pineda-web |

## Config

```yaml
pineda:
  url: http://127.0.0.1:8090
  profile: default
  # device: <id>
  reboot_delay_seconds: 10
  recipe_seconds: 120     # the recipe card comes down by itself after this
```
