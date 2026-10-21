"""Weekday "good morning": spoken once, when the first trip out the back
door of the morning (letting the dogs out) ends with the trip back in.

The back door's contact sensor (Home Assistant, pushed through
security.HomeAssistant.watch) is the trigger. A week of its history showed
the pattern every weekday: an opening between 5:50 and 7:20, then a second
one 1 to 3 minutes later. The first is going out, so nothing is said; she
speaks a few seconds after the door shuts behind the second, when you're
back inside. Openings within `bounce_seconds` of the first are the same
trip (the sensor reported a bounce 2 s after one close), and if nobody
comes back within `back_within_minutes` the morning is given up on, so a
mid-morning door doesn't get a "good morning".

Times come from HA's last_changed, not the clock, so the 2 s poller and the
push reporting the same change agree.

The affirmations come from published lists, reworded to be said to you
rather than by you; each line names its source key in SOURCES.
"""

import json
import logging
import random
import threading
from dataclasses import dataclass
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Callable

from . import household_memory, settings

log = logging.getLogger("antigua")

SOURCES = {
    "ggg": "https://www.goodgoodgood.co/articles/positive-morning-affirmations",
    "ctl": "https://www.crisistextline.org/blog/2024/01/08/100-positive-affirmations-for-better-self-care/",
    "mc": "https://www.marriage.com/advice/relationship/words-of-affirmation-for-him/",
}

AFFIRMATIONS = [
    ("You're capable of handling whatever today brings.", "ggg"),
    ("You have everything you need to take on the day.", "ggg"),
    ("You're strong and resilient.", "ggg"),
    ("You bring value to every new day.", "ggg"),
    ("You're a creative problem-solver.", "ggg"),
    ("You're ready to face the day with courage and grace.", "ggg"),
    ("You're a force for good in the world.", "ggg"),
    ("You make today a good day just by being here.", "ggg"),
    ("You're capable of achieving great things.", "ggg"),
    ("Your thoughts are powerful and focused.", "ggg"),
    ("You're filled with determination and drive.", "ggg"),
    ("You're a source of inspiration to others.", "ggg"),
    ("You're resilient and adaptable.", "ggg"),
    ("You're a lifelong learner, always growing.", "ggg"),
    ("You're a source of strength and support for the people around you.", "ggg"),
    ("You're grounded, present, and ready for today.", "ggg"),
    ("You're patient, understanding, and compassionate.", "ggg"),
    ("You make the best of every situation.", "ggg"),
    ("You're an unstoppable force of determination and perseverance.", "ggg"),
    ("You're passionate about your goals, and it shows.", "ggg"),
    ("You're open to every possibility this day holds.", "ggg"),
    ("You deserve a day filled with joy.", "ggg"),
    ("Today is going to be an amazing day.", "ggg"),
    ("You're a unique and valuable person.", "ggg"),
    ("You're a problem-solver who finds solutions to challenges.", "ggg"),
    ("You should be proud of how far you've come.", "ctl"),
    ("You get better every single day.", "ctl"),
    ("You're worthy of love and respect.", "ctl"),
    ("You're valued, and you're needed.", "ctl"),
    ("Your future is bright.", "ctl"),
    ("You can make a difference today.", "ctl"),
    ("You're building your future, one day at a time.", "ctl"),
    ("All your problems have solutions.", "ctl"),
    ("Doing your best is enough.", "ctl"),
    ("You have the courage and confidence to do the right thing.", "ctl"),
    ("Your mind is clear and your focus is steady.", "ctl"),
    ("You radiate positive energy in everything you do.", "ctl"),
    ("You're well prepared for whatever comes up today.", "ctl"),
    ("The people you work with value and appreciate you.", "ctl"),
    ("Today, you're a leader.", "ctl"),
    ("You can get through anything.", "ctl"),
    ("You're a valuable part of your community.", "ctl"),
    ("You belong here, and you're more than good enough.", "ctl"),
    ("Your challenges help you grow.", "ctl"),
    ("It's okay to take things one step at a time.", "ctl"),
    ("You've made it this far, and you're not stopping now.", "ctl"),
    ("You're in control of your day.", "ctl"),
    ("You believe in yourself and your abilities, and you should.", "ctl"),
    ("Your hard work inspires the people around you.", "mc"),
    ("Your energy is contagious.", "mc"),
    ("You're a wonderful family man.", "mc"),
    ("Your work ethic earns real respect.", "mc"),
    ("You're thoughtful in ways people notice.", "mc"),
    ("Your decision-making is something to be proud of.", "mc"),
    ("You're talented and skilled.", "mc"),
    ("Your go-getter attitude is something to admire.", "mc"),
    ("You never take the people you love for granted.", "mc"),
    ("You're caring, and it shows.", "mc"),
    ("You're a rock for the people who count on you.", "mc"),
    ("You're a great listener.", "mc"),
]

_KEEP_AFTER_RESHUFFLE = 10      # a new deck doesn't open with last week's lines


def _state_path() -> Path:
    return settings.DATA_DIR / "morning_said.json"


def _load() -> dict:
    try:
        d = json.loads(_state_path().read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(d: dict) -> None:
    try:
        _state_path().parent.mkdir(parents=True, exist_ok=True)
        _state_path().write_text(json.dumps(d))
    except OSError:
        log.warning("Morning: couldn't save state", exc_info=True)


def greeted_on() -> str | None:
    """The last day greeted (ISO), so a restart mid-morning doesn't greet twice."""
    return _load().get("greeted")


def pick(rng: random.Random | None = None, day: date | None = None) -> str:
    """An affirmation not said since the deck was last reshuffled; records
    `day` as greeted."""
    rng = rng or random
    lines = [a for a, _ in AFFIRMATIONS]
    d = _load()
    said = list(d.get("said") or [])
    fresh = [a for a in lines if a not in said]
    if not fresh:
        said = said[-_KEEP_AFTER_RESHUFFLE:]
        fresh = [a for a in lines if a not in said]
    line = rng.choice(fresh)
    said.append(line)
    _save({"said": said, "greeted": (day or date.today()).isoformat()})
    return line


def _clock(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


@dataclass
class Config:
    door: str                         # HA binary_sensor; "on" = open
    name: str = ""
    window: str = "05:00-09:00"       # the trip out has to start in here
    weekdays_only: bool = True
    delay_seconds: float = 12         # after the door shuts behind you
    bounce_seconds: float = 30        # openings this close to the first are the same trip
    back_within_minutes: float = 20

    @classmethod
    def from_dict(cls, d: dict) -> "Config":
        keys = cls.__dataclass_fields__
        return cls(**{k: v for k, v in d.items() if k in keys})


def _changed(state: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(state["last_changed"]).astimezone()
    except (KeyError, TypeError, ValueError):
        return None


class MorningGreeting:
    """Fed every pushed or polled state of the door; speaks at most once a day."""

    def __init__(self, cfg: Config, announce: Callable[[str], None],
                 timer: Callable[..., threading.Timer] = threading.Timer):
        self.cfg = cfg
        self.announce = announce
        self._timer_cls = timer
        self._lock = threading.Lock()
        self._last: tuple[str, datetime] | None = None   # (state, last_changed) seen
        self._day: date | None = None
        self._out: datetime | None = None     # first opening this morning
        self._back = False                    # the trip back in has started
        self._done = False
        self._timer: threading.Timer | None = None

    def _new_day(self, d: date) -> None:
        if self._day != d:
            self._day, self._out, self._back = d, None, False
            self._done = greeted_on() == d.isoformat()
            self._cancel()

    def _cancel(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _in_window(self, t: datetime) -> bool:
        if self.cfg.weekdays_only and t.weekday() >= 5:
            return False
        start, end = (_clock(x) for x in self.cfg.window.split("-"))
        return start <= t.time() < end

    def on_state(self, state: dict) -> None:
        s, t = state.get("state"), _changed(state)
        if s not in ("on", "off") or t is None:
            return
        with self._lock:
            if self._last == (s, t):
                return                       # the push and the poller saw the same change
            self._last = (s, t)
            self._new_day(t.date())
            if self._done:
                return
            if s == "on":
                self._opened(t)
            elif self._back:
                self._cancel()               # wait again from this close
                delay = max(0.0, self.cfg.delay_seconds
                            - (datetime.now().astimezone() - t).total_seconds())
                self._timer = self._timer_cls(delay, self._speak)
                self._timer.daemon = True
                self._timer.start()

    def _opened(self, t: datetime) -> None:
        if self._out is None:
            if self._in_window(t):
                self._out = t
                log.info("Morning: back door out at %s", t.strftime("%H:%M:%S"))
            return
        gap = (t - self._out).total_seconds()
        if gap < self.cfg.bounce_seconds:
            return
        if gap > self.cfg.back_within_minutes * 60:
            log.info("Morning: no trip back in within %g min; not today",
                     self.cfg.back_within_minutes)
            self._done = True
            return
        self._back = True
        self._cancel()                       # still coming in: wait for the door to shut

    def _speak(self) -> None:
        with self._lock:
            if self._done:
                return
            self._done = True
            self._timer = None
            day = self._day
        name = household_memory.store().call_name(self.cfg.name) or self.cfg.name
        greeting = f"Good morning, {name}." if name else "Good morning."
        text = f"{greeting} {pick(day=day)}"
        log.info("Morning: %r", text)
        try:
            self.announce(text)
        except Exception:
            log.exception("Morning: announcement failed")
