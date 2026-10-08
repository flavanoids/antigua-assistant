"""The cooking session: one recipe, followed step by step, by voice.

Household-wide rather than per conversation: the mics start a new
conversation id after every follow-up window, and a bake can run for hours
between "Antigua, next" and the one after. Saved to disk on every change so
a restart or deploy mid-recipe doesn't lose the place.

handle() decides whether a turn belongs to the session and answers it. It
only claims a bare "yes", "no" or "next" while the session is actually
waiting for one; anything else goes on to the other skills. Every word
spoken about the recipe comes from the recipe itself (recipe.py) or the
substitution table (recipe_subs.py). The exception is a free-form question
("can I use a hand mixer?"): the LLM answers it, but only from
question_context() below, and every number it says is checked against the
recipe afterwards (grounding.recipe_claims_ok).
"""

import copy
import json
import logging
import re
import time
from dataclasses import dataclass
from threading import RLock

from . import settings
from .intents.recipe import bare_items, parse_cook_command
from .recipe import (Recipe, fmt_qty, listed, say_minutes, scale_line, scale_step_text, step_parts,
                     spoken_title)
from .intents.recipe import looks_like_food
from .recipe_subs import (core_name, is_creamed, is_essential, match_ingredient, substitutes_for,
                          swap_verdict)

log = logging.getLogger("antigua_core")

# Steps take minutes and the mic's answer window is only seconds, so the
# prompt names the wake word: "next" alone works only right after a reply.
_PROMPTS = (
    "When you're ready, say {wake}, next.",
    "Say {wake}, next, when you're ready for more.",
    "Just say {wake}, next, when you're ready.",
)


_COUNT_WORDS = {2: "two", 3: "three", 4: "four", 5: "five", 6: "six"}


def _wake() -> str:
    return (settings.WAKE_WORDS or ["Antigua"])[0].title()


def _say_next(lead: str = "") -> str:
    return f"{lead}Say {_wake()}, next, when you're ready."
_COOK_VERB_RE = re.compile(r"\b(?:bake|baking|cook|cooking|simmer|boil|roast|fry|grill|broil|"
                           r"chill|refrigerate|rest|rise|marinate|microwave|steam|braise|sear|"
                           r"let\s+(?:it\s+)?(?:sit|stand|cool|rest))\w*", re.IGNORECASE)
_DURATION_RE = re.compile(
    r"(?P<h>\d+)\s*(?:hours?|hrs?)(?:\s*(?:and\s*)?(?P<hm>\d+)\s*(?:minutes?|mins?))?"
    r"|(?P<m>\d+)(?:\s*(?:to|-|–|or)\s*\d+)?\s*(?:minutes?|mins?)",
    re.IGNORECASE)
_TEMP_IN_RE = re.compile(r"\b\d{2,3}\s*(?:degrees|°)(?:\s*[FC]\b|\s+Fahrenheit)?", re.IGNORECASE)
_HEAT_RE = re.compile(r"\b(?:over\s+)?(?:low|medium[- ]low|medium|medium[- ]high|high)\s+heat\b", re.IGNORECASE)
# Time and temperature questions that belong to the timers or the weather.
_NOT_RECIPE_Q_RE = re.compile(r"\b(?:timers?|alarms?|reminders?|left|remaining|until|outside|weather|"
                              r"today|tomorrow|tonight|thermostat|house|room|drive|flight|song|game|movie|"
                              r"laundry|dishes)\b",
                              re.IGNORECASE)
_OVEN_Q_RE = re.compile(r"\b(?:oven|bake|baking|roast)\b", re.IGNORECASE)
# Doneness cues for "how do I know when it's done?" — a step sentence with
# one of these answers it. No bare "when" or "set": nearly every step has
# one, and they'd match a question the recipe doesn't answer.
_DONE_CUE_RE = re.compile(
    r"\buntil\b|\bgolden\b|\bbrown(?:ed|ing)?\b|\bjiggl\w*|\bbubbl\w*|\btender\b|"
    r"\btoothpick\b|\bthicken\w*|\bpuffed\b|\bopaque\b|\bno\s+longer\b|"
    r"\bsprings?\s+back\b|\bpulls?\s+away\b|\bcooked\s+through\b|"
    r"\bfirm\s+to\s+the\s+touch\b|\bsets?\s+up\b|\b(?:is|has|it'?s)\s+set\b",
    re.IGNORECASE)
_Q_TOPIC = {
    r"\boven|bake|baking\b": r"\b(?:bake|oven|baking)\b",
    r"\bchill|fridge|refrigerat": r"\b(?:chill|refrigerat|fridge)\w*",
    r"\bsimmer|boil|stove|pot\b": r"\b(?:simmer|boil)\w*",
    r"\bcool\b": r"\bcool\w*",
    r"\brest|rise|sit\b": r"\b(?:rest|rise|sit)\w*",
    r"\bmix|beat|whisk|stir\b": r"\b(?:mix|beat|whisk|stir)\w*",
    r"\bfry|sear|brown|cook\b": r"\b(?:fry|sear|brown|cook)\w*",
}


def _join(items: list) -> str:
    items = [i for i in items if i]
    if len(items) <= 2:
        return " and ".join(items)
    return ", ".join(items[:-1]) + ", and " + items[-1]


def _a(noun: str) -> str:
    return ("an " if re.match(r"[aeiou]", noun, re.I) else "a ") + noun


def _sentences(text: str) -> list:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def _duration_minutes(m) -> int:
    if m.group("h"):
        return int(m.group("h")) * 60 + int(m.group("hm") or 0)
    return int(m.group("m"))


def timer_minutes(step: str) -> int | None:
    """The cooking time in a step worth a timer: the first duration that goes
    with a cooking verb ("bake for 1 hour and 10 minutes"), 6 minutes or more:
    a few minutes of stirring is watched, not timed."""
    for sent in _sentences(step):
        if not _COOK_VERB_RE.search(sent):
            continue
        m = _DURATION_RE.search(sent)
        if m:
            mins = _duration_minutes(m)
            return mins if 6 <= mins <= 12 * 60 else None
    return None


@dataclass
class Reply:
    text: str
    timer_minutes: int = 0        # set a timer for the dish
    timer_label: str = ""
    shop_item: str = ""           # add to the shopping list
    fetch: str = ""               # find this dish (a switch the user confirmed)
    ended: bool = False


class CookSession:
    def __init__(self, dish: str, recipes: list, now: float | None = None):
        self.dish = dish
        self.recipes = recipes            # [Recipe], ranked; idx is the one in use
        self.idx = 0
        self.stage = "ingredients"        # ingredients | steps | finished
        self.awaiting = ""                # the yes/no question just asked, if any
        self.asked_at = 0.0
        self.step = -1
        self.part = 0                     # which part of a long step (step_parts)
        self.missing = []                 # items still to work through
        self.sub = None                   # {"i": line, "alts": [...], "n": alt index}
        self.swaps = {}                   # line index -> short substitute
        self.skipped = []                 # line indexes left out
        self.shop = ""                    # item offered for the shopping list
        self.pending_dish = ""
        self.check_i = -1                 # one-at-a-time ingredient checklist
        self.asked = []                   # lines this turn's answer was about (the kiosk lights them)
        self.timer = 0
        self.prompt_n = 0
        self.scale = 1.0                  # ingredient amounts x this (§2, scaling)
        self.target_servings = 0          # the "for 4" a scale came from, if any
        self.scale_notes = []             # rounding warnings, spoken once
        self.pending_servings = 0          # "for 4 people" riding a switch
        self.at = now or time.time()

    # ── persistence ──────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["recipes"] = [r.to_dict() for r in self.recipes]
        d["swaps"] = {str(k): v for k, v in self.swaps.items()}
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "CookSession":
        s = cls(d["dish"], [Recipe.from_dict(r) for r in d["recipes"]])
        for k, v in d.items():
            if k not in ("dish", "recipes"):
                setattr(s, k, v)
        s.swaps = {int(k): v for k, v in d.get("swaps", {}).items()}
        return s

    # ── helpers ──────────────────────────────────────────────────────────

    @property
    def recipe(self) -> Recipe:
        return self.recipes[self.idx]

    @property
    def name(self) -> str:
        return spoken_title(self.recipe.title)

    def _ask(self, question: str) -> None:
        self.awaiting, self.asked_at = question, time.time()

    def _waiting(self) -> str:
        if self.awaiting and time.time() - self.asked_at < settings.RECIPE_ANSWER_TTL:
            return self.awaiting
        return ""

    def _baked(self) -> bool:
        return bool(re.search(r"\b(?:bake|oven)\b", " ".join(self.recipe.steps), re.I))

    def _context(self, i: int | None = None) -> str:
        """For the substitution table: cook, bake, or creamed (line i is
        beaten with the sugar, so oil won't do for it)."""
        if not self._baked():
            return "cook"
        if i is not None and is_creamed(core_name(self.recipe.ingredients[i]), self.recipe.steps):
            return "creamed"
        return "bake"

    def _line_name(self, i: int) -> str:
        return core_name(self.recipe.ingredients[i])

    # ── scaling (§2 of docs/recipe_questions_and_scaling_plan.md) ────────

    def line(self, i: int) -> str:
        """Ingredient line i at the session's size."""
        r = self.recipe
        if self.scale == 1 or i >= len(r.amounts):
            return r.ingredients[i]
        text, _note = scale_line(r.ingredients[i], r.amounts[i], self.scale)
        return text

    def _scaled_servings(self) -> str:
        r = self.recipe
        if r.yield_unit:
            n = int(r.yield_n * self.scale + 0.5)
            word = {"loaf": "loaves"}.get(r.yield_unit, r.yield_unit + "s")
            return f"makes {n} {r.yield_unit if n == 1 else word}"
        if not r.yield_n:
            return r.servings
        n = int(r.yield_n * self.scale + 0.5)
        if r.yield_hi:
            hi = int(r.yield_hi * self.scale + 0.5)
            return f"serves {n}" if n == hi else f"serves {n} to {hi}"
        return f"serves {n}"

    def _set_scale(self, factor: float, servings: int) -> None:
        self.scale, self.target_servings = factor, servings
        notes = []
        for i, a in enumerate(self.recipe.amounts):
            _line, note = scale_line(self.recipe.ingredients[i], a, factor)
            if note and note not in notes:
                notes.append(note)
        self.scale_notes = [f"eggs don't divide evenly: {n}" for n in notes]

    def set_servings(self, n: int) -> str:
        """Scale for `n` at the moment the recipe starts: "" or what to say
        when it can't be done."""
        r = self.recipe
        if r.yield_unit:
            return f"This recipe makes one {r.yield_unit}. I can double or halve it."
        if not r.yield_n:
            return "This recipe doesn't say how many it serves. I can double or halve it."
        f = n / r.yield_n
        if not 0.125 <= f <= 8:
            return (f"That's a big change; this recipe {r.servings}. "
                    "I'll read it as written.")
        self._set_scale(f, n)
        return ""

    def _factor_phrase(self, f: float) -> str:
        for k, v in ((0.125, "an eighth"), (0.25, "a quarter"), (1 / 3, "a third"),
                     (0.5, "half"), (2 / 3, "two thirds"), (1, "the original"),
                     (1.5, "one and a half times"), (2, "double"), (3, "triple"),
                     (4, "four times"), (6, "six times"), (8, "eight times")):
            if abs(f - k) < 1e-9:
                return v
        return f"{fmt_qty(f)} times"

    def _pans(self) -> bool:
        return bool(self.recipe.equipment) or re.search(
            r"\b(?:pan|dish|tin|skillet|pot)\b", " ".join(self.recipe.steps), re.I)

    def _apply_scale(self, factor: float, servings: int) -> Reply:
        r = self.recipe
        if factor != 1 and not any(a.get("qty") is not None for a in r.amounts):
            return Reply("This recipe doesn't list amounts I can scale.")
        self._set_scale(factor, servings)
        notes, self.scale_notes = self.scale_notes, []
        if factor == 1:
            return Reply("Back to the original amounts.")
        parts = []
        if servings:
            of = "the recipe" if factor >= 1 else "of the recipe"
            parts.append(f"Okay, scaled for {servings}, {self._factor_phrase(factor)} {of}.")
        else:
            done = {2: "I've doubled", 3: "I've tripled", 4: "I've quadrupled",
                    0.5: "I've halved", 0.25: "I've quartered"}
            say = done.get(round(factor, 6)) or f"I've made {self._factor_phrase(factor)}"
            parts.append(f"{say} the ingredients.")
        parts.append("Cooking times and pan size stay the same, so keep an eye on it.")
        if factor >= 1.5 and self._pans():
            parts.append("You may need a bigger pan.")
        elif factor <= 0.67 and self._pans():
            parts.append("You may need a smaller pan.")
        if notes:
            parts.append(" ".join(n.capitalize() + "." for n in notes))
        self._ask("scale_read")
        return Reply(" ".join(parts) + " Want me to read the new amounts?")

    def _cmd_scale(self, arg: dict) -> Reply:
        r = self.recipe
        if arg.get("ask"):
            if self.scale != 1 and r.yield_n:
                return Reply(f"It {self._scaled_servings()}, scaled from "
                             f"{re.sub(r'^(?:serves|makes) ', '', r.servings)}.")
            return Reply(f"It {r.servings}.")
        if "factor" in arg:
            f = arg["factor"]
            if not 0.125 <= f <= 8:
                return Reply("That's a big change; I'd find a recipe sized for that instead.")
            return self._apply_scale(f, 0)
        n = arg["servings"]
        if r.yield_unit:
            return Reply(f"This recipe makes one {r.yield_unit}. I can double or halve it.")
        if not r.yield_n:
            return Reply("This recipe doesn't say how many it serves. I can double or halve it.")
        f = n / r.yield_n
        if f == 1:
            return Reply(f"It already {r.servings}.")
        if not 0.125 <= f <= 8:
            return Reply("That's a big change; I'd find a recipe sized for that instead.")
        return self._apply_scale(f, n)

    def _read_new_amounts(self) -> Reply:
        self.awaiting = ""
        if self.stage == "ingredients":
            self._ask("have_all")
            return Reply(f"Here are the new amounts: {self.ingredient_list()} "
                         "Do you have everything?")
        return Reply(f"Here are the new amounts: {self.ingredient_list()}")

    def _carry_scale(self) -> None:
        """Decision 3: a servings request follows to the next recipe; a
        plain factor resets to the new recipe's own size."""
        r = self.recipe
        if self.target_servings and r.yield_n and not r.yield_unit:
            f = self.target_servings / r.yield_n
            if 0.125 <= f <= 8:
                self._set_scale(f, self.target_servings)
                return
        self._set_scale(1.0, 0)

    # ── what gets said ───────────────────────────────────────────────────

    def ingredient_list(self) -> str:
        r = self.recipe
        if r.groups:
            parts = []
            for name, a, b in r.groups:
                label = re.sub(r"^(?:for\s+(?:the\s+)?)", "", name, flags=re.I).strip().lower()
                items = [listed(self.line(i)) for i in range(a, b + 1)]
                parts.append((f"For the {label}: " if label else "") + self._chunked(items))
            return " ".join(parts)
        return self._chunked([listed(self.line(i)) for i in range(len(r.ingredients))])

    @staticmethod
    def _chunked(items: list) -> str:
        """Five at a time, so a long list has somewhere to breathe."""
        if len(items) <= 6:
            return _join(items) + "."
        chunks = [items[i:i + 5] for i in range(0, len(items), 5)]
        out = [", ".join(c) + "." for c in chunks[:-1]]
        out.append(_join(chunks[-1]) + ".")
        return " ".join(out)

    def _howto_intro(self, lead: str) -> str:
        """A household guide (howto.py): what it is, the caution line once,
        then the supplies, same flow as a recipe's ingredients."""
        r = self.recipe
        what = r.title.strip()
        if re.match(r"how\s+to\b", what, re.I):
            what = what.lower()
        parts = [lead + (f"Here's {what}" if what.lower().startswith("how to") else f"Here's a guide: {what}")
                 + (f", from {r.source}." if r.source else ".")]
        parts.append(f"It's {len(r.steps)} steps.")
        if r.caution:
            parts.append(r.caution)
        self.stage, self.step = "ingredients", -1
        self.missing, self.sub, self.swaps, self.skipped, self.shop = [], None, {}, [], ""
        if not r.ingredients:
            self._ask("ready")
            return " ".join(parts + ["Ready for step one?"])
        parts.append(f"You'll need: {self.ingredient_list()}")
        parts.append("Do you have everything?")
        self._ask("have_all")
        return " ".join(parts)

    def intro(self, lead: str = "") -> str:
        r = self.recipe
        if r.kind == "howto":
            return self._howto_intro(lead)
        parts = [lead + f"I found a recipe for {self.name} from {r.source}."]
        about = []
        if r.servings:
            if self.scale != 1 and (r.yield_n or r.yield_unit):
                about.append(f"It {self._scaled_servings()}, scaled from "
                             f"{re.sub(r'^(?:serves|makes) ', '', r.servings)}")
            else:
                about.append(f"It {r.servings}")
        if r.total_min:
            about.append(("and takes" if about else "It takes") + f" about {say_minutes(r.total_min)}")
        if about:
            parts.append(" ".join(about) + ".")
        if r.wait:
            parts.append(f"Heads up: it needs to {r.wait}.")
        if self.scale != 1:
            parts.append("Cooking times and pan size are from the original recipe, "
                         "so keep an eye on it.")
        if self.scale_notes:
            parts.append("Heads up: " + "; ".join(self.scale_notes) + ".")
            self.scale_notes = []
        parts.append(f"You'll need {len(r.ingredients)} ingredients. {self.ingredient_list()}")
        if r.equipment:
            parts.append(f"You'll also need {_join([_a(e) for e in r.equipment])}.")
        parts.append("Do you have everything?")
        self.stage, self.step = "ingredients", -1
        self.missing, self.sub, self.swaps, self.skipped, self.shop = [], None, {}, [], ""
        self._ask("have_all")
        return " ".join(parts)

    def _notes(self, text: str) -> str:
        notes = []
        for i, short in self.swaps.items():
            name = self._line_name(i)
            if re.search(rf"\b{re.escape(name.split()[-1])}", text, re.I):
                notes.append(f"You're using {short} instead of the {name}.")
        for i in self.skipped:
            name = self._line_name(i)
            if re.search(rf"\b{re.escape(name.split()[-1])}", text, re.I):
                notes.append(f"You're leaving out the {name}.")
        return (" " + " ".join(notes)) if notes else ""

    def _parts(self, i: int) -> list:
        return step_parts(scale_step_text(self.recipe.steps[i], self.recipe.amounts, self.scale))

    def _at_end(self) -> bool:
        return (self.step == len(self.recipe.steps) - 1
                and self.part >= len(self._parts(self.step)) - 1)

    def _say_step(self, i: int, part: int = 0) -> Reply:
        """Step i, or one part of it when it's long: "Step 2 of 6, in three
        parts. First: ..." then "Then: ..." on next. The wake-word prompt
        only comes at the end of the whole step."""
        steps = self.recipe.steps
        i = max(0, min(i, len(steps) - 1))
        parts = self._parts(i)
        part = max(0, min(part, len(parts) - 1))
        self.step, self.part, self.stage = i, part, "steps"
        n = len(steps)
        last = i == n - 1
        last_part = part == len(parts) - 1
        if part == 0:
            head = f"Step {i + 1} of {n}, the last one" if last else f"Step {i + 1} of {n}"
            head += f", in {_COUNT_WORDS.get(len(parts), len(parts))} parts. First:" if len(parts) > 1 else "."
        else:
            head = "Then:"
        text = parts[part]
        body = f"{head} {text}{self._notes(text)}"
        mins = timer_minutes(text)
        self.awaiting = ""
        if mins:
            self.timer = mins
            self._ask("timer")
            tail = f" Want me to set a timer for {say_minutes(mins)}?"
            if last and last_part:
                tail = " That's the last step." + tail
        elif not last_part:
            tail = ""
        elif last:
            self.stage = "finished"
            # Many recipes end their last step with "Enjoy!" already.
            tail = ("" if re.search(r"\benjoy\b", text, re.I)
                    else f" That's the last step. Enjoy your {self.name}!")
        else:
            tail = " " + _PROMPTS[self.prompt_n % len(_PROMPTS)].format(wake=_wake())
            self.prompt_n += 1
        return Reply(body + tail)

    def _whole_step(self) -> Reply:
        i = max(self.step, 0)
        text = scale_step_text(self.recipe.steps[i], self.recipe.amounts, self.scale)
        self.part = len(step_parts(text)) - 1      # next goes on to the next step
        return Reply(f"Step {i + 1}: {text}{self._notes(text)}")

    def _check_item(self, lead: str) -> Reply:
        ing = self.recipe.ingredients
        if self.check_i < len(ing):
            self._ask("checklist")
            return Reply(f"{lead}{listed(self.line(self.check_i))}.")
        self.check_i = -1
        if not self.missing:
            return self._ready_prompt("That's everything.")
        return self._next_missing("")

    def _ready_prompt(self, lead: str = "") -> Reply:
        self.missing, self.sub = [], None
        self._ask("ready")
        return Reply((lead + " " if lead else "") + "Ready for step one?")

    # ── missing ingredients ──────────────────────────────────────────────

    def _lines_for(self, whole: str, items: list) -> tuple:
        """Match the whole phrase first ("half and half"), then each item."""
        hits = match_ingredient(whole, self.recipe.ingredients)
        if hits:
            return [hits[0]], []
        found, unknown = [], []
        for item in items:
            hits = [h for h in match_ingredient(item, self.recipe.ingredients) if h not in found]
            if hits:
                # "eggs" can mean "4 large eggs" and "3 large egg yolks" both.
                same = [h for h in hits if set(core_name(self.recipe.ingredients[h]).split())
                        >= set(core_name(self.recipe.ingredients[hits[0]]).split())]
                found.extend(same or hits[:1])
            else:
                unknown.append(item)
        return found, unknown

    def _missing(self, whole: str, items: list) -> Reply:
        found, unknown = self._lines_for(whole, items)
        lead = ""
        if unknown:
            lead = f"{_join(unknown).capitalize()} {'is' if len(unknown) == 1 else 'are'}n't in this recipe, so you're fine there. "
        self.missing.extend(i for i in found if i not in self.missing)
        return self._next_missing(lead)

    def _next_missing(self, lead: str = "") -> Reply:
        if self.recipe.kind == "howto" and self.missing:
            # No kitchen substitutions for a drywall saw: say what's needed.
            names = _join([self._line_name(i) for i in self.missing])
            self.missing = []
            self._ask("anything_else")
            return Reply(f"{lead}You'll want the {names} before you start; a hardware store "
                         "will have it. Anything else missing?")
        while self.missing:
            i = self.missing.pop(0)
            name = self._line_name(i)
            alts = substitutes_for(name, self._context(i))
            if alts:
                self.sub = {"i": i, "alts": alts, "n": 0}
                self._ask("have_sub")
                return Reply(f"{lead}Instead of the {name}, you can use {alts[0][1]}. Do you have that?")
            reply = self._no_substitute(i, lead)
            if reply is not None:
                return reply
            lead = f"{lead}You can leave out the {name}; it'll just taste a little different. "
            self.skipped.append(i)
        self._ask("anything_else")
        return Reply(f"{lead}Anything else missing?")

    def _no_substitute(self, i: int, lead: str) -> Reply | None:
        r = self.recipe
        if not is_essential(r.ingredients[i], self.dish, r.title, self._baked()):
            return None
        name = self._line_name(i)
        self.shop = name
        self._ask("shopping")
        return Reply(f"{lead}The {name} is hard to do without in this recipe. "
                     "Want me to add it to the shopping list?")

    def _sub_declined(self) -> Reply:
        sub = self.sub
        sub["n"] += 1
        name = self._line_name(sub["i"])
        if sub["n"] < len(sub["alts"]):
            self._ask("have_sub")
            return Reply(f"Or you can use {sub['alts'][sub['n']][1]}. Do you have that?")
        self.sub = None
        reply = self._no_substitute(sub["i"], "I don't have another substitute for that. ")
        if reply is not None:
            return reply
        self.skipped.append(sub["i"])
        return self._next_missing(f"Then leave out the {name}; it'll just taste a little different. ")

    # ── questions ────────────────────────────────────────────────────────

    def _skip_question(self, item: str) -> Reply | None:
        """"Can I skip the vanilla?" — the optional/essential rule as an
        answer. Nothing is recorded: it's a question, not a decision, so a
        later "I don't have it" still works the same."""
        hits = match_ingredient(item, self.recipe.ingredients)
        if not hits:
            return None          # not an ingredient here; the LLM hook answers
        i = hits[0]
        name = self._line_name(i)
        if i in self.skipped:
            return Reply(f"You're already leaving out the {name}.")
        if i in self.swaps:
            return Reply(f"You're using {self.swaps[i]} instead of the {name}.")
        if is_essential(self.recipe.ingredients[i], self.dish, self.recipe.title, self._baked()):
            verb = "is" if name.endswith("ss") or not name.endswith("s") else "are"
            return Reply(f"The {name} {verb} hard to do without in this recipe.")
        return Reply(f"You can leave out the {name}; it'll just taste a little different.")

    def _swap_question(self, x: str, y: str) -> Reply | None:
        """"Can I use olive oil instead of butter?" — answered from the
        table, never the web or the model. A yes is recorded, so the steps
        say "you're using olive oil instead of the butter"."""
        ing = self.recipe.ingredients
        hy, hx = match_ingredient(y, ing), match_ingredient(x, ing)
        if not hy and hx:           # "swap olive oil for butter": the recipe's one is replaced
            x, y, hy = y, x, hx
        if not hy:
            food = lambda w: looks_like_food(w) or bool(substitutes_for(w))  # noqa: E731
            if food(y) and food(x):
                return Reply(f"This recipe doesn't use {y}.")
            return None
        i = hy[0]
        name, core, ctx = self._line_name(i), core_name(ing[i]), self._context(i)
        if self.swaps.get(i, "").lower() == x.lower():
            return Reply(f"You're already using {x} instead of the {name}.")
        verdict, said = swap_verdict(core, x, ctx)
        alts = substitutes_for(core, ctx)
        if verdict == "yes":
            self.swaps[i] = x
            if i in self.skipped:
                self.skipped.remove(i)
            said = re.sub(r"\bas much oil\b", f"as much {x}", said)
            return Reply(f"Yes. Instead of the {name}, use {said}. I'll remind you in the steps.")
        if verdict == "no":
            more = f" What works here is {alts[0][1]}." if alts else ""
            return Reply(f"Not in this one: {said}.{more}")
        if alts:
            return Reply(f"I don't have a tested swap of {x} for the {name}. "
                         f"What I know works here is {alts[0][1]}.")
        if not is_essential(ing[i], self.dish, self.recipe.title, self._baked()):
            return Reply(f"I don't have a tested substitute for the {name}, but you can leave it out; "
                         "it'll just taste a little different.")
        return Reply(f"I don't have a tested substitute for the {name}.")

    def _keep(self, item: str) -> Reply | None:
        """"Keep the butter" after a swap: undo it."""
        i = next((h for h in match_ingredient(item, self.recipe.ingredients) if h in self.swaps), None)
        if i is None:
            return None
        was = self.swaps.pop(i)
        return Reply(f"Okay, the {self._line_name(i)} it is, not the {was}.")

    def how_much(self, item: str) -> str | None:
        r = self.recipe
        hits = match_ingredient(item, r.ingredients)
        if not hits:
            return None
        head = core_name(r.ingredients[hits[0]])
        lines = [h for h in hits if core_name(r.ingredients[h]) == head] or hits[:1]
        self.asked = lines
        said = []
        for h in lines:
            text = self.line(h)
            group = next((g[0] for g in r.groups if g[1] <= h <= g[2]), "")
            label = re.sub(r"^(?:for\s+(?:the\s+)?)", "", group, flags=re.I).strip().lower()
            if len(lines) > 1 and label:
                text += f" for the {label}"
            if h in self.swaps:
                text += f", and you're using {self.swaps[h]} instead"
            elif h in self.skipped:
                text += ", but you're leaving it out"
            said.append(text)
        return _join(said)[0].upper() + _join(said)[1:] + "."

    def _search_steps(self, question: str, pattern: re.Pattern) -> str | None:
        """The sentence that answers a time or temperature question: the
        current step first, then the ones before it, then the ones after."""
        steps = self.recipe.steps
        cur = max(self.step, 0)
        order = [cur] + list(range(cur - 1, -1, -1)) + list(range(cur + 1, len(steps)))
        topic = next((t for q, t in _Q_TOPIC.items() if re.search(q, question, re.I)), None)
        for want_topic in ((True, False) if topic else (False,)):
            for i in order:
                for sent in _sentences(steps[i]):
                    if not pattern.search(sent):
                        continue
                    if want_topic and not re.search(topic, sent, re.I):
                        continue
                    where = "" if i == self.step else f" That's in step {i + 1}."
                    return sent + where
        return None

    def how_long(self, question: str) -> str:
        found = self._search_steps(question, _DURATION_RE)
        if found:
            return found
        if self.recipe.total_min and re.search(r"\b(?:whole|total|all|altogether|recipe|take)\b", question, re.I):
            return f"The whole recipe takes about {say_minutes(self.recipe.total_min)}."
        return "The recipe doesn't say."

    def temperature(self, question: str) -> str:
        pattern = _TEMP_IN_RE if _OVEN_Q_RE.search(question) else re.compile(
            f"{_TEMP_IN_RE.pattern}|{_HEAT_RE.pattern}", re.I)
        return self._search_steps(question, pattern) or "The recipe doesn't say."

    def question_context(self) -> str:
        """The whole recipe as it's being cooked, given to the LLM behind a
        free-form question. The question itself is deliberately not part of
        this context: a number the model echoes back from it ("can I bake it
        at 300?") is exactly what recipe_claims_ok has to catch."""
        r = self.recipe
        lines = ([f"Guide being followed: {r.title} from {r.source}."] if r.kind == "howto" else
                 [f"Recipe being cooked: {r.title} from {r.source} ({self._scaled_servings()})."])
        ings = []
        for i in range(len(r.ingredients)):
            text = self.line(i)
            if i in self.swaps:
                text += f" (using {self.swaps[i]} instead)"
            elif i in self.skipped:
                text += " (left out)"
            ings.append(f"- {text}")
        lines.append("Ingredients:\n" + "\n".join(ings))
        if r.equipment:
            lines.append("Equipment: " + ", ".join(r.equipment) + ".")
        steps = [f"{i + 1}. {scale_step_text(s, r.amounts, self.scale)}"
                 for i, s in enumerate(r.steps)]
        lines.append("Steps:\n" + "\n".join(steps))
        if self.stage == "finished":
            where = "The cook has finished the last step."
        elif self.step < 0:
            where = "The cook hasn't started the steps yet."
        else:
            where = f"The cook is on step {self.step + 1} of {len(r.steps)}."
        if self.skipped:
            where += (" Left out: " + ", ".join(self._line_name(i) for i in self.skipped) + ".")
        lines.append(where)
        if self.scale != 1:
            lines.append(f"Amounts are {self._factor_phrase(self.scale)} "
                         f"{'of the' if self.scale < 1 else 'the'} original; "
                         "times, temperatures and pan sizes are the original.")
        lines.append(
            "Answer the cook's question in a few short spoken sentences, using "
            "ONLY the recipe above plus general kitchen technique. Never state "
            "an amount, time, temperature, pan size or ingredient that is not "
            "written in the recipe. If the recipe doesn't say, say \"The recipe "
            "doesn't say\" and, if useful, a single general tip without numbers. "
            "No lists, no headings.")
        return "\n".join(lines)

    # ── the turn ─────────────────────────────────────────────────────────

    def handle(self, transcript: str) -> Reply | None:
        self.asked = []
        cmd = parse_cook_command(transcript)
        c, arg = cmd if cmd else (None, None)
        waiting = self._waiting()
        cooking = self.stage in ("steps", "finished")

        # Said plainly about the recipe: always this session's.
        if c == "end":
            if self.recipe.kind == "howto":
                return Reply("Okay, I've closed the guide.", ended=True)
            return Reply(f"Okay, I've closed the {self.name} recipe.", ended=True)
        if c == "scale":
            return self._cmd_scale(arg)
        if c == "another":
            return self.another(arg["filter"], arg["without"])
        if c == "start_over" and cooking:
            return self._say_step(0)
        if c == "goto":
            n, move = arg
            steps = self.recipe.steps
            i = len(steps) - 1 if n == -1 else n - 1
            if not 0 <= i < len(steps):
                return Reply(f"This recipe has {len(steps)} steps.")
            if move or not cooking:
                return self._say_step(i)
            text = scale_step_text(steps[i], self.recipe.amounts, self.scale)
            return Reply(f"Step {i + 1}: {text}{self._notes(steps[i])}")
        if c == "status" and cooking:
            n = len(self.recipe.steps)
            left = n - 1 - self.step
            if self.stage == "finished" or left <= 0:
                return Reply(f"You're on the last step of {n}.")
            return Reply(f"You're on step {self.step + 1} of {n}, so {left} more to go.")
        if c == "ingredients":
            return Reply(f"For the {self.name}: {self.ingredient_list()}")
        if c == "ingredient_n":
            ing = self.recipe.ingredients
            i = len(ing) - 1 if arg == -1 else arg - 1
            if 0 <= i < len(ing):
                self.asked = [i]
                ln = self.line(i)
                return Reply(ln[0].upper() + ln[1:] + ".")
        if c == "how_much":
            said = self.how_much(arg)
            if said:
                return Reply(said)
            # "How much vanilla?" when there's none; not "how many people live in Texas".
            if (waiting or cooking) and len(arg.split()) <= 3 and (
                    looks_like_food(arg) or substitutes_for(arg)):
                return Reply(f"This recipe doesn't use {arg}.")
            return None
        if c == "sub_q":
            hits = match_ingredient(arg, self.recipe.ingredients)
            alts = (substitutes_for(core_name(self.recipe.ingredients[hits[0]]), self._context(hits[0])) if hits
                    else substitutes_for(arg, "bake" if self._baked() else "cook"))
            if alts:
                return Reply(f"Instead of {arg}, you can use {alts[0][1]}."
                             + (f" Or {alts[1][1]}." if len(alts) > 1 else ""))
            return None      # the general substitution search takes it
        if c == "how_long" and cooking and not _NOT_RECIPE_Q_RE.search(arg):
            return Reply(self.how_long(arg))
        if c == "temp" and (cooking or waiting) and not _NOT_RECIPE_Q_RE.search(arg):
            return Reply(self.temperature(arg))
        if c == "swap_q":
            return self._swap_question(*arg)
        if c == "keep":
            return self._keep(arg)
        if c == "whole_step" and cooking:
            return self._whole_step()
        if c == "skip_q":
            return self._skip_question(arg)
        if c == "looks_q" and cooking and not _NOT_RECIPE_Q_RE.search(arg):
            found = self._search_steps(arg, _DONE_CUE_RE)
            if found:
                return Reply(found)

        if c == "checklist" and self.stage == "ingredients":
            self.check_i, self.missing = 0, []
            return self._check_item("Okay, one at a time. Say got it, or that you don't have it. ")

        # Answers to the question just asked.
        if waiting == "checklist":
            if c in ("yes", "next"):
                self.check_i += 1
                return self._check_item("")
            if c in ("no", "missing"):
                self.missing.append(self.check_i)
                self.check_i += 1
                return self._check_item("")
            if c == "repeat":
                return self._check_item("")
        if waiting == "switch":
            if c == "yes" or c == "next":
                return Reply("", fetch=self.pending_dish)
            if c in ("no", "cancel"):
                self.awaiting = ""
                return Reply(f"Okay, staying with the {self.name}. You're on step {self.step + 1}.")
        if waiting == "scale_read":
            if c in ("yes", "next"):
                return self._read_new_amounts()
            if c in ("no", "cancel"):
                self.awaiting = ""
                if self.stage == "ingredients":
                    self._ask("have_all")
                    return Reply("Okay. Do you have everything?")
                return Reply("Okay.")
        if waiting in ("have_all", "anything_else", "what_missing"):
            if c == "yes" and (arg or waiting == "have_all"):
                return self._ready_prompt("Great.")
            if c == "yes":
                self._ask("what_missing")
                return Reply("What else are you missing?")
            if c == "no" and waiting == "have_all":
                self._ask("what_missing")
                return Reply("What are you missing?")
            if c in ("no", "next") or (c == "cancel" and waiting != "have_all"):
                return self._ready_prompt("Okay.")
            if c == "missing":
                return self._missing(*arg)
            if c == "repeat":
                return Reply(f"{self.ingredient_list()} Do you have everything?")
            if c is None and waiting == "what_missing":
                whole, items = bare_items(transcript)
                if items and self._lines_for(whole, items)[0]:
                    return self._missing(whole, items)
        if waiting == "have_sub":
            if c == "yes":
                sub = self.sub
                short = sub["alts"][sub["n"]][0]
                self.swaps[sub["i"]] = short
                self.sub = None
                return self._next_missing(f"Great, {short} it is. ")
            if c in ("no", "missing"):
                return self._sub_declined()
        if waiting == "shopping":
            if c == "yes":
                item, self.shop = self.shop, ""
                self._ask("switch_or_go")
                return Reply(f"Added {item} to your shopping list. Want a different recipe instead, "
                             "or keep going anyway?", shop_item=item)
            if c == "no":
                self._ask("switch_or_go")
                return Reply("Okay. Want a different recipe instead, or keep going anyway?")
        if waiting == "switch_or_go":
            if c in ("yes", "next") or re.search(r"\b(?:keep|go(?:ing)?\s+(?:on|anyway|ahead)|continue|anyway)\b",
                                                 transcript, re.I):
                return self._next_missing()
            if c == "no" or re.search(r"\bdifferent\b", transcript, re.I):
                return self.another(None, None)
        if waiting == "ready":
            if c in ("yes", "next", "start_over"):
                return self._say_step(0)
            if c in ("no", "cancel"):
                self.awaiting = ""
                return Reply(f"Okay. Just say {_wake()}, I'm ready, when you are.")
        if waiting == "timer":
            if c == "yes":
                mins, self.timer = self.timer, 0
                self.awaiting = ""
                if self.stage == "finished" or self._at_end():
                    self.stage = "finished"
                    return Reply(f"Timer set. That was the last step. Enjoy your {self.name}!",
                                 timer_minutes=mins, timer_label=self.dish)
                return Reply(_say_next("Timer set. "),
                             timer_minutes=mins, timer_label=self.dish)
            if c == "no":
                self.awaiting = ""
                if self._at_end():
                    self.stage = "finished"
                    return Reply(f"Okay. Enjoy your {self.name}!")
                return Reply(_say_next("Okay. "))

        # Moving through the steps.
        if self.stage == "ingredients" and not waiting and c == "next" and re.search(r"\bready\b", transcript, re.I):
            return self._say_step(0)
        if cooking:
            if c == "next":
                if self.part < len(self._parts(self.step)) - 1:
                    return self._say_step(self.step, self.part + 1)
                if self.step >= len(self.recipe.steps) - 1:
                    self.stage = "finished"
                    return Reply(f"That was the last step. Enjoy your {self.name}!")
                return self._say_step(self.step + 1)
            if c == "back":
                if self.part > 0:
                    return self._say_step(self.step, self.part - 1)
                if self.step <= 0:
                    return Reply(f"You're on the first step. {self.recipe.steps[0]}")
                return self._say_step(self.step - 1)
            if c == "repeat":
                return self._say_step(self.step, self.part)
            # The mic takes plain speech after each step now, so "okay" or
            # "not yet" to "say next when you're ready" lands here. Neither
            # moves on; "I'm ready" and "next" do.
            if c == "yes" and not waiting:
                return Reply("Okay.")
            if c == "no" and not waiting:
                return Reply("Okay, take your time.")
        elif c == "repeat" and self.stage == "ingredients":
            return Reply(f"{self.ingredient_list()} Do you have everything?")
        if c == "cancel" and self.stage == "ingredients" and waiting:
            return Reply("Okay, never mind the recipe.", ended=True)
        return None

    def another(self, filt: str | None, without: str | None) -> Reply:
        cur = self.recipe
        for j in range(self.idx + 1, len(self.recipes)):
            r = self.recipes[j]
            if filt == "simpler" and r.size() >= cur.size():
                continue
            if filt == "quicker" and not (r.total_min and (not cur.total_min or r.total_min < cur.total_min)):
                continue
            if without and match_ingredient(without, r.ingredients):
                continue
            self.idx = j
            self._carry_scale()
            return Reply(self.intro("Okay, here's another one. "))
        what = {"simpler": " that's simpler", "quicker": " that's quicker"}.get(filt, "")
        if without:
            what = f" without {without}"
        self._ask(self.awaiting or "have_all")
        return Reply(f"That's all the {self.dish} recipes I found{what}. "
                     f"I'll stay with the {self.name} from {cur.source}.")


# ── The one session in the house ─────────────────────────────────────────────

_lock = RLock()
_session: CookSession | None = None
_loaded = False


def _path():
    return settings.DATA_DIR / "recipe_session.json"


def current() -> CookSession | None:
    global _session, _loaded
    with _lock:
        if not _loaded:
            _loaded = True
            try:
                _session = CookSession.from_dict(json.loads(_path().read_text()))
            except (OSError, ValueError, KeyError, TypeError):
                _session = None
        if _session and time.time() - _session.at > settings.RECIPE_SESSION_TTL:
            log.info("Recipe session for %r expired", _session.dish)
            clear()
        return _session


def save(session: CookSession | None) -> None:
    global _session, _loaded
    with _lock:
        _session, _loaded = session, True
        try:
            if session is None:
                _path().unlink(missing_ok=True)
            else:
                session.at = time.time()
                _path().parent.mkdir(parents=True, exist_ok=True)
                _path().write_text(json.dumps(session.to_dict()))
        except OSError as e:
            log.warning("Recipe session save failed: %s", e)


def clear() -> None:
    save(None)


def start(dish: str, recipes: list, servings: int = 0) -> tuple:
    """A new session on the best of `recipes`; returns (session, intro).
    `servings` is the "for 4 people" of the request, or the target carried
    over from the recipe before it (Decision 3)."""
    prev = current()
    if not servings and prev:
        servings = prev.target_servings
    s = CookSession(dish, recipes)
    note = s.set_servings(servings) if servings else ""
    intro = s.intro() + (f" {note}" if note else "")
    save(s)
    return s, intro


def likely_next() -> list:
    """What "next" would say now, and "yes" when a question is waiting,
    worked out on a copy so the session doesn't move. The pipeline
    synthesizes these ahead so the reply is already in the TTS cache."""
    with _lock:
        s = current()
        if s is None:
            return []
        s = copy.deepcopy(s)
    texts = []
    for said in ("next", "yes") if s.awaiting else ("next",):
        reply = copy.deepcopy(s).handle(said)
        if reply and not (reply.ended or reply.fetch) and reply.text not in texts:
            texts.append(reply.text)
    return texts


def handle(transcript: str) -> Reply | None:
    with _lock:
        s = current()
        if s is None:
            return None
        reply = s.handle(transcript)
        if reply is None:
            return None
        if reply.ended:
            clear()
        else:
            save(s)
        return reply
