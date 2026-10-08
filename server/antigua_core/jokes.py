"""Antigua's joke bank: hand-picked jokes, told without repeats.

The 4B model can follow a comic structure but rarely lands a punchline, so a
plain "tell me a joke" comes from here instead. The house style is dry:
deadpan observations about everyday things (printers, socks, Houston in
August), anti-jokes, and a few flatly delivered puns. Never about AI, and
never at anyone's expense. Every joke has to work heard aloud, so no puns
that need spelling.

Jokes are dealt like a shuffled deck: none comes back until the whole bank has
been told, and the told list is saved in the data dir so a restart doesn't
reshuffle the deck. "A joke about cats" picks from the jokes tagged for it;
a subject the bank doesn't cover returns None and the LLM writes one.
"""

import json
import logging
import random
import re
import threading
from pathlib import Path

from . import settings

log = logging.getLogger("antigua_core")

# (joke, tags). Tags are singular, lowercase; a subject matches on any word.
JOKES: list[tuple[str, tuple[str, ...]]] = [
    # ── Around the house ──
    ("A fitted sheet can't be folded. It can only be put somewhere and never spoken of again.",
     ("laundry", "sheet", "bed", "chore", "home", "folding")),
    ("Socks go into the dryer as pairs and come out as individuals.",
     ("sock", "laundry", "dryer", "chore", "home")),
    ("Laundry is the only chore that's never technically finished. You're always wearing the next load.",
     ("laundry", "chore", "clothes", "home")),
    ("Every junk drawer has one key. Nobody knows what it opens. Nobody is going to find out.",
     ("junk", "drawer", "key", "home", "organizing")),
    ("Every house has a drawer of cables for devices that no longer exist. It's a museum with no visitors.",
     ("cable", "drawer", "junk", "tech", "home", "charger")),
    ("Flat-pack furniture always comes with two extra screws. Either the factory is generous, or something in the house is about to fall down.",
     ("ikea", "furniture", "home", "diy", "screw", "assembly")),
    ("Tupperware lids and their containers are never in the same place at the same time. Scientists have stopped asking why.",
     ("tupperware", "kitchen", "leftover", "container")),
    ("Batteries come in packs of eight. Every remote takes three.",
     ("battery", "remote", "tv", "home")),
    ("Smoke detector batteries only die at three in the morning. It has never been explained, and it never will be.",
     ("smoke", "detector", "battery", "night", "sleep", "home")),
    ("The microwave has thirty buttons. Everyone uses one of them.",
     ("microwave", "kitchen", "appliance", "home")),
    ("The printer says it's out of cyan. You're printing in black and white. It isn't a negotiation.",
     ("printer", "office", "work", "computer", "tech")),
    ("A paper jam has never once been where the printer says it is.",
     ("printer", "office", "work", "computer", "tech")),
    ("Every phone charger in the house is either too short or in another room.",
     ("phone", "charger", "cable", "home", "tech")),
    ("Stepping on a Lego is one of the few things in life that's exactly as bad as everyone says.",
     ("lego", "toy", "kid", "home", "foot")),
    ("A houseplant is a pet that dies very slowly and doesn't hold it against you.",
     ("plant", "houseplant", "garden", "gardening", "pet", "home")),
    ("Leaf blowers move leaves to a different part of the same yard, very loudly.",
     ("leaf", "yard", "lawn", "fall", "autumn", "noise", "neighbor")),
    ("The robot vacuum has fully mapped the house. Mostly the places where it gets stuck.",
     ("robot", "vacuum", "cleaning", "chore", "home")),

    # ── Food ──
    ("An avocado is ripe for about forty-five minutes, and it happens while you're asleep.",
     ("avocado", "food", "grocery", "fruit", "guacamole")),
    ("Recipes say season to taste. That isn't a measurement. That's a philosophy.",
     ("recipe", "cooking", "cook", "food")),
    ("Prep time, ten minutes, assumes the onion is already chopped and you have four arms.",
     ("recipe", "cooking", "cook", "food", "dinner", "onion")),
    ("Charcuterie is a plate of lunch meat that went to college.",
     ("charcuterie", "food", "snack", "cheese", "party")),
    ("Cauliflower has been rice, pizza crust, and chicken wings. At some point someone should ask cauliflower what it wants.",
     ("cauliflower", "vegetable", "diet", "healthy", "food", "pizza")),
    ("Pumpkin spice contains no pumpkin. It's the spice you would put on a pumpkin, if you had one.",
     ("pumpkin", "spice", "fall", "autumn", "coffee", "october", "season")),
    ("Fun-size candy bars are not more fun. They are smaller. That is the entire difference.",
     ("halloween", "candy", "chocolate", "sweets", "snack", "october")),
    ("Halloween candy bought in September is just candy.",
     ("halloween", "candy", "october", "september", "holiday", "sweets", "snack")),
    ("Coffee doesn't make anyone a morning person. It makes them a person, in the morning.",
     ("coffee", "morning", "sleep", "tired")),
    ("Leftovers last three days, or one smell, whichever comes first.",
     ("leftover", "fridge", "food", "kitchen")),
    ("A bag of chips is mostly air. Technically it's a balloon with a snack in it.",
     ("chips", "chip", "snack", "food", "grocery")),
    ("Microwave popcorn has two settings: not done, and on fire.",
     ("popcorn", "microwave", "snack", "movie", "food")),
    ("A salad is a meal you plan to eat right up until you see the tacos.",
     ("salad", "taco", "diet", "healthy", "food", "lunch")),
    ("Restaurants put the menu on a QR code now. So it's a menu, but you need a phone, and the phone is at two percent.",
     ("restaurant", "menu", "phone", "food", "tech")),
    ("Tipping screens have started showing up at self-checkout. Presumably for the machine.",
     ("tip", "tipping", "checkout", "money", "shopping", "store")),
    ("In Texas, mild salsa is still technically a warning.",
     ("salsa", "spicy", "texas", "food", "mexican", "taco")),

    # ── Phones, screens, work ──
    ("The front-facing camera opening by accident is the most honest photo ever taken.",
     ("phone", "camera", "selfie", "photo", "tech")),
    ("The weekly screen time report doesn't say anything. It doesn't have to.",
     ("phone", "screen", "tech")),
    ("Nobody calls anymore. When the phone rings, it's either an emergency or the car's extended warranty.",
     ("phone", "call", "spam", "car")),
    ("Voicemail is a podcast nobody subscribed to.",
     ("phone", "voicemail", "podcast", "call")),
    ("Every app wants to send notifications. Even the flashlight.",
     ("phone", "app", "notification", "tech")),
    ("Autocorrect is confident in a way most things are not.",
     ("autocorrect", "phone", "text", "texting", "tech")),
    ("A group chat with forty people will produce four hundred messages and zero decisions.",
     ("group", "chat", "text", "texting", "friend", "family", "trip")),
    ("Passwords now need a capital letter, a number, a symbol, and a little piece of your soul.",
     ("password", "computer", "internet", "tech")),
    ("A free trial is just a subscription that hasn't been noticed yet.",
     ("subscription", "streaming", "money", "trial")),
    ("Picking something to stream takes forty-five minutes. Then it's The Office again.",
     ("streaming", "tv", "netflix", "movie", "show", "watch")),
    ("The terms and conditions are the most agreed-to document in history, and nobody has read them.",
     ("terms", "internet", "app", "tech", "contract")),
    ("Wi-Fi is the only utility that goes out exactly when it's being used.",
     ("wifi", "internet", "router", "tech")),
    ("Turning it off and on again has fixed more problems than every manual ever written.",
     ("computer", "tech", "router", "wifi", "manual")),
    ("Reply all has never been pressed on purpose.",
     ("email", "work", "office")),
    ("A quick call is never quick, and is rarely a call. It's a meeting in a disguise.",
     ("meeting", "call", "work", "office")),
    ("Every meeting that should have been an email eventually becomes an email anyway, summarizing the meeting.",
     ("meeting", "email", "work", "office")),
    ("A standing desk is a regular desk you sit at and feel slightly guilty near.",
     ("desk", "work", "office", "home", "fitness")),
    ("Working from home means the commute is twelve feet, and it's still possible to be late.",
     ("work", "remote", "home", "commute", "office")),

    # ── Shopping, money ──
    ("Target is where you go in for paper towels and come out with a lamp.",
     ("target", "shopping", "store", "money")),
    ("Costco is the only place where spending three hundred dollars feels like winning.",
     ("costco", "shopping", "store", "grocery", "money")),
    ("Self-checkout has never accepted a bag on the first try. Unexpected item in the bagging area. The item is the bag.",
     ("self", "checkout", "grocery", "shopping", "store", "bag")),
    ("A garlic press online has four thousand reviews calling it life changing. It presses garlic.",
     ("review", "shopping", "amazon", "online", "kitchen", "garlic")),
    ("Shipping is free if you spend thirty more dollars. That's how free works now.",
     ("shipping", "shopping", "amazon", "online", "money")),

    # ── Health, getting older ──
    ("Injuries in your twenties: skiing accident. Injuries in your forties: slept wrong.",
     ("age", "aging", "old", "sleep", "health", "back", "body", "birthday")),
    ("Past forty, sitting down comes with a sound effect.",
     ("age", "aging", "old", "body", "health", "birthday")),
    ("The dentist asks if you floss. You say yes. Everyone moves on.",
     ("dentist", "floss", "teeth", "health", "doctor")),
    ("A smartwatch will congratulate you for standing up. It's the most supportive thing in the house.",
     ("watch", "smartwatch", "fitness", "exercise", "health", "tech")),
    ("A gym membership is a monthly donation to a building you think about a lot.",
     ("gym", "fitness", "exercise", "workout", "money", "resolution")),
    ("Nothing ages a song faster than hearing it on the classic rock station.",
     ("music", "song", "radio", "age", "aging", "old")),

    # ── Houston, Texas, getting around ──
    ("Houston has two seasons: summer, and one Tuesday in January.",
     ("houston", "texas", "weather", "summer", "winter", "season", "hot", "heat")),
    ("Houston in August doesn't have weather. It has a condition.",
     ("houston", "texas", "weather", "summer", "august", "hot", "heat", "humidity")),
    ("The electric bill this summer was high enough that the power company sent a thank-you card.",
     ("electric", "bill", "money", "summer", "heat", "hot", "houston", "texas", "ac")),
    ("Three things are certain: death, taxes, and construction on I forty-five.",
     ("houston", "texas", "traffic", "construction", "highway", "commute", "driving", "drive", "tax", "car")),
    ("The Katy Freeway is twenty-six lanes wide in places, and it is still full.",
     ("houston", "texas", "traffic", "highway", "katy", "freeway", "commute", "driving", "drive", "car")),
    ("Going to Buc-ee's for gas is like going to Disney World for the parking.",
     ("bucees", "texas", "gas", "road", "trip", "car", "driving", "drive")),
    ("The GPS says turn left in five hundred feet, as if anyone knows where five hundred feet is.",
     ("gps", "map", "driving", "drive", "car", "directions")),
    ("A road trip is sitting in a car for six hours so you can sit somewhere else.",
     ("road", "trip", "car", "driving", "drive", "travel", "vacation")),
    ("Airport security takes your water bottle, then sells you the same water for six dollars on the other side.",
     ("airport", "security", "flight", "flying", "travel", "trip", "plane", "water")),

    # ── Pets ──
    ("Dogs see you leave for two minutes and act like you came back from war. Cats see you come back from war and ask about dinner.",
     ("dog", "cat", "pet", "animal")),
    ("Cats knock things off tables to check that gravity still works. So far, it does.",
     ("cat", "pet", "animal", "gravity", "science")),
    ("A cat will always sit on the one piece of paper you need. It's not a coincidence. It's a skill.",
     ("cat", "pet", "animal", "paper", "work")),
    ("A dog will bark at the mail carrier every day for ten years. The mail carrier has never taken anything.",
     ("dog", "pet", "animal", "mail")),

    # ── Calendar ──
    ("The Sunday scaries are just Monday sending a calendar invite you can't decline.",
     ("sunday", "monday", "work", "weekend", "week", "calendar")),
    ("Seven alarms, five minutes apart, is not a plan. It's a negotiation.",
     ("alarm", "snooze", "morning", "sleep", "wake", "clock")),
    ("Daylight saving time is when the entire country agrees to be wrong about what time it is.",
     ("daylight", "time", "clock", "november", "march", "sleep")),
    ("Nobody knows what day it is between Christmas and New Year's. It's just one long day with leftovers.",
     ("christmas", "holiday", "new", "year", "december", "leftover", "calendar")),
    ("A New Year's resolution lasts about as long as the confetti.",
     ("new", "year", "resolution", "january", "holiday", "gym")),

    # ── Anti-jokes, deadpan puns, a few nerdy ones ──
    ("Why did the chicken cross the road? It lived on the other side. It was not a big deal.",
     ("chicken", "anti", "classic", "animal", "road")),
    ("A horse walks into a bar. Several people get up and leave, sensing potential danger.",
     ("horse", "anti", "bar", "animal")),
    ("What's red and bad for your teeth? A brick.",
     ("anti", "teeth", "dentist", "brick")),
    ("Knock knock. Who's there? The delivery driver. They've already left. The package is in the rain.",
     ("knock", "anti", "delivery", "package", "amazon", "rain", "shopping")),
    ("What's the best thing about Switzerland? Not sure, but the flag is a big plus.",
     ("dad", "pun", "switzerland", "country", "flag", "geography")),
    ("A man's wife asked him to stop impersonating a flamingo. He had to put his foot down.",
     ("dad", "pun", "flamingo", "bird", "animal", "marriage", "wife")),
    ("Fear of elevators is very common. Most people take steps to avoid them.",
     ("dad", "pun", "elevator", "stairs", "fear")),
    ("Someone asked the librarian for books about paranoia. She whispered, they're right behind you.",
     ("dad", "pun", "library", "book", "reading", "librarian")),
    ("The person who invented the knock-knock joke won the no-bell prize.",
     ("dad", "pun", "knock", "nobel", "prize", "science")),
    ("There are some good chemistry jokes, but all the good ones argon.",
     ("dad", "pun", "science", "chemistry", "nerd")),
    ("A photon checks into a hotel. The clerk asks if it has any luggage. It says no, it's traveling light.",
     ("pun", "science", "physics", "nerd", "hotel", "travel", "light")),
    ("Schrödinger's cat walks into a bar. And doesn't.",
     ("science", "physics", "nerd", "cat", "bar")),
    ("A database query walks into a bar, goes up to two tables, and asks, mind if I join you?",
     ("programming", "programmer", "code", "coding", "computer", "nerd", "tech", "database", "bar")),
    ("Programmers prefer dark mode because light attracts bugs.",
     ("programming", "programmer", "code", "coding", "computer", "nerd", "tech", "bug")),
    ("There's a joke about UDP, but you might not get it.",
     ("programming", "programmer", "computer", "nerd", "tech", "network", "internet")),
]

_KEEP_AFTER_RESHUFFLE = 25   # the most recent ones stay out of a fresh deck
_STOP = {"a", "an", "the", "my", "your", "our", "me", "us", "some", "and", "or", "of", "that", "this", "is", "about"}
_lock = threading.Lock()


def _state_path() -> Path:
    return settings.DATA_DIR / "jokes_told.json"


def _load_told() -> list[str]:
    try:
        return list(json.loads(_state_path().read_text()))
    except (OSError, ValueError):
        return []


def _save_told(told: list[str]) -> None:
    try:
        p = _state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(told))
    except OSError:
        log.warning("Couldn't save told jokes", exc_info=True)


def _words(subject: str) -> set[str]:
    words = set()
    for w in re.findall(r"[a-z]+", subject.lower().replace("-", "").replace("'", "")):
        if w in _STOP:
            continue
        words.add(w)
        if len(w) > 3 and w.endswith("s"):
            words.add(w[:-1])   # cats -> cat, tacos -> taco
    return words


def pick(subject: str | None = None, rng: random.Random | None = None) -> str | None:
    """A joke she hasn't told lately, or None when nothing in the bank fits
    the subject (or every fitting joke is still fresh in memory)."""
    rng = rng or random
    if subject:
        wanted = _words(subject)
        if {"dad", "pun"} & wanted and len(wanted - {"dad", "pun", "joke"}) == 0:
            wanted = {"dad", "pun"}   # "a dad joke", "a pun": the dad/pun pile
        pool = [j for j, tags in JOKES if wanted & set(tags)] if wanted else [j for j, _ in JOKES]
    else:
        pool = [j for j, _ in JOKES]
    if not pool:
        return None
    with _lock:
        told = _load_told()
        fresh = [j for j in pool if j not in told]
        if not fresh:
            if subject:
                return None   # every one about that has been told; the LLM writes one
            told = told[-_KEEP_AFTER_RESHUFFLE:]   # deck's done: reshuffle, minus the latest
            fresh = [j for j in pool if j not in told]
        joke = rng.choice(fresh)
        told.append(joke)
        _save_told(told)
    return joke
