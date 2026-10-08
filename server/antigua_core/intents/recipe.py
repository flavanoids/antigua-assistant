"""Recipe requests, and everything said while cooking one.

"How do I make cheesecake", "recipe for chicken noodle soup", "let's bake
banana bread" start a recipe (recipe.py finds a real one online). Once a
cooking session is on, parse_cook_command() reads the short things said
over the stove: "next", "go back", "what was step 3", "how much salt?",
"I don't have sour cream", "another recipe".

Pure regex, no state: recipe_session.py decides which of these a turn is
allowed to claim at its current stage.
"""

import re

# ── Starting a recipe ────────────────────────────────────────────────────────

# Wording that is about cooking whatever follows: no food check needed.
_COOK_RE = re.compile(
    r"^(?:(?:can|could|will)\s+you\s+)?(?:please\s+)?(?:"
    r"(?:give|find|get|read|tell)\s+(?:me|us)\s+(?:an?\s+|the\s+|your\s+)?(?:good\s+|easy\s+|simple\s+)?recipe\s+(?:for|to\s+make)|"
    r"(?:find|look\s+up|search\s+for|pull\s+up)\s+(?:me\s+)?(?:an?\s+|the\s+)?(?:good\s+|easy\s+)?recipe\s+(?:for|to\s+make)|"
    r"(?:what(?:'s|\s+is)\s+)?(?:an?\s+|the\s+)?(?:good\s+|easy\s+|simple\s+)?recipe\s+for|"
    r"how\s+(?:do|would|should|can|could)\s+(?:i|you|we|one)\s+(?:cook|bake|roast)|"
    r"how\s+to\s+(?:cook|bake|roast)|"
    r"(?:let'?s|let\s+us|i\s+want\s+to|i'?d\s+like\s+to|i'?m\s+going\s+to|we'?re\s+going\s+to|"
    r"i'?m\s+gonna|we'?re\s+gonna|help\s+me|teach\s+me\s+(?:how\s+)?to|walk\s+me\s+through\s+how\s+to)\s+(?:cook|bake|roast)"
    r")\s+(?P<dish>.+)$",
    re.IGNORECASE,
)
# "X recipe" / "a recipe" at the end: "cheesecake recipe please".
_TRAILING_RECIPE_RE = re.compile(
    r"^(?:(?:give\s+me|find\s+me|read\s+me|i\s+(?:want|need)|i'?d\s+like|get\s+me)\s+)?"
    r"(?:an?\s+|the\s+|your\s+)?(?P<dish>.+?)\s+recipe$",
    re.IGNORECASE,
)
# "make" is not always cooking ("how do I make money"): the dish must look like food.
_MAKE_RE = re.compile(
    r"^(?:(?:can|could|will)\s+you\s+)?(?:"
    r"how\s+(?:do|would|should|can|could)\s+(?:i|you|we|one)\s+(?:make|prepare|fix)|"
    r"how\s+to\s+(?:make|prepare)|"
    r"(?:what\s+do\s+i\s+need|what\s+(?:do\s+i|should\s+i)\s+use)\s+to\s+make|"
    r"(?:let'?s|let\s+us|i\s+want\s+to|i'?d\s+like\s+to|i'?m\s+going\s+to|we'?re\s+going\s+to|"
    r"i'?m\s+gonna|we'?re\s+gonna|help\s+me|teach\s+me\s+(?:how\s+)?to|"
    r"walk\s+me\s+through\s+(?:how\s+to\s+)?)\s*(?:make|making|prepare|fix|whip\s+up)"
    r")\s+(?P<dish>.+)$",
    re.IGNORECASE,
)

# Words that say "this is food". Singular forms; a trailing s is dropped
# before the lookup. Suffixes catch compounds ("cheesecake", "shortbread").
_FOOD_WORDS = set("""
chicken beef pork steak lamb turkey ham bacon sausage meatball meatloaf burger
fish salmon tuna shrimp crab lobster tilapia cod scallop clam mussel
egg omelet omelette frittata quiche pancake waffle crepe french toast
soup stew chili curry broth chowder gumbo bisque ramen pho stir fry
pasta spaghetti lasagna noodle macaroni mac fettuccine alfredo carbonara ravioli
penne gnocchi risotto rice paella pilaf biryani casserole pot pie
taco burrito enchilada tamale quesadilla fajita nacho salsa guacamole mole
pozole tostada chilaquile empanada pupusa elote arepa
pizza sandwich wrap salad dressing sauce gravy marinade dip hummus
bread loaf biscuit muffin scone roll bun bagel cornbread focaccia tortilla
cake cupcake cheesecake brownie cookie pie tart cobbler crumble crisp pudding
custard flan fudge frosting icing candy caramel toffee donut doughnut
cinnamon banana apple pumpkin chocolate vanilla lemon strawberry blueberry
potato fries mashed vegetable veggie broccoli cauliflower carrot spinach
bean lentil chickpea tofu mushroom zucchini squash eggplant corn pepper onion
smoothie shake lemonade tea coffee latte cocktail margarita sangria punch
jam jelly pickle granola oatmeal porridge cereal
dumpling potsticker sushi teriyaki fried roast roasted grilled baked
dinner lunch breakfast dessert appetizer snack meal dish side
""".split())
_FOOD_SUFFIX_RE = re.compile(r"(?:cake|bread|pie|soup|stew|cookies?|muffins?|salad|sauce|"
                             r"burgers?|fries|noodles?|tacos?|balls?)$", re.IGNORECASE)
_DISH_CLEAN_RE = re.compile(
    r"^(?:(?:a|an|the|some|my|our|your|me|us|good|great|nice|easy|simple|quick|homemade|"
    r"basic|classic|traditional|best|really|recipe\s+for)\s+)+", re.IGNORECASE)
_DISH_TAIL_RE = re.compile(
    r"\s+(?:please|for\s+(?:me|us|dinner|lunch|breakfast|tonight|today)|tonight|today|"
    r"at\s+home|from\s+scratch|step\s+by\s+step|real\s+quick|again|antigua"
    r"|for\s+(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|"
    r"fifteen|twenty|thirty)(?:\s*(?:people|persons?|servings?|guests?|plates?|portions?))?"
    r")$", re.IGNORECASE)


def _norm(text: str) -> str:
    text = re.sub(r"[?!.¿¡\"]", "", text or "").replace("’", "'")
    text = re.sub(r"\s*,\s*", ", ", text)
    return re.sub(r"\s+", " ", text).strip(" ,")


def _clean_dish(dish: str) -> str:
    dish = dish.strip(" ,")
    for _ in range(3):
        before = dish
        dish = _DISH_TAIL_RE.sub("", dish).strip(" ,")
        dish = _DISH_CLEAN_RE.sub("", dish).strip(" ,")
        if dish == before:
            break
    return dish


def looks_like_food(dish: str) -> bool:
    for w in re.findall(r"[a-z]+", dish.lower()):
        if w in _FOOD_WORDS or w.rstrip("s") in _FOOD_WORDS or (
                w.endswith("es") and w[:-2] in _FOOD_WORDS):
            return True
        if _FOOD_SUFFIX_RE.search(w):
            return True
    return False


def parse_recipe_request(transcript: str) -> str | None:
    """The dish asked for, or None when this isn't a recipe request."""
    text = _norm(transcript)
    text = re.sub(r"^(?:hey|ok(?:ay)?|so|um|uh|alright|antigua)[, ]+", "", text, flags=re.I)
    text = re.sub(r"[, ]+(?:please|antigua|thanks|thank\s+you)$", "", text, flags=re.I)
    m = _COOK_RE.match(text)
    if m:
        dish = _clean_dish(m.group("dish"))
        return dish or None
    m = _MAKE_RE.match(text)
    if m:
        dish = _clean_dish(m.group("dish"))
        return dish if dish and looks_like_food(dish) else None
    m = _TRAILING_RECIPE_RE.match(text)
    if m:
        dish = _clean_dish(m.group("dish"))
        if dish and len(dish.split()) <= 6 and not re.search(
                r"\b(?:another|different|other|new|next|simpler|easier|this|that)\b", dish, re.I):
            return dish
    return None


_SERVINGS_REQ_RE = re.compile(
    r"\bfor\s+(?P<n>\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fifteen|twenty|thirty)\s*"
    r"(?:people|persons?|servings?|guests?|plates?|portions?)?\s*$", re.IGNORECASE)


def requested_servings(transcript: str) -> int:
    """The "for 4 people" at the end of a recipe request: the recipe starts
    scaled to that many servings. 0 when the request says no size."""
    m = _SERVINGS_REQ_RE.search(_norm(transcript))
    return _number(m.group("n")) or 0 if m else 0


# ── While cooking ────────────────────────────────────────────────────────────

_NUMS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "first": 1, "second": 2, "third": 3, "fourth": 4,
    "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
    "eleventh": 11, "twelfth": 12, "to": 2, "too": 2, "for": 4, "won": 1,
    "last": -1, "final": -1,
}


def _number(word: str) -> int | None:
    word = word.lower()
    if word.isdigit():
        return int(word)
    m = re.match(r"(\d+)(?:st|nd|rd|th)$", word)
    if m:
        return int(m.group(1))
    return _NUMS.get(word)


_LEAD_RE = re.compile(r"^(?:(?:ok(?:ay)?|alright|all\s+right|um+|uh+|so|and|hey|well|oh|"
                      r"cool|great|good|perfect|awesome|antigua)[, ]+)+", re.IGNORECASE)
_TAIL_RE = re.compile(r"(?:[, ]+(?:please|antigua|thanks|thank\s+you))+$", re.IGNORECASE)

_ENDING = r"(?:\s+(?:please|now))?$"
_NEXT_RE = re.compile(
    r"^(?:(?:done|got\s+it|yes|yeah|yep|finished|ok(?:ay)?)[, ]+)*(?:"
    r"next(?:\s+(?:step|one|part|thing|instruction))?|"
    r"(?:go|move)\s+(?:on|ahead)(?:\s+to\s+the\s+next\s+(?:step|one))?|continue|keep\s+going|"
    r"what(?:'s|\s+is|\s+do\s+(?:i|we)\s+do)\s+next|what(?:'s|\s+is)\s+the\s+next\s+(?:step|one)|"
    r"what\s+now|then\s+what|and\s+then|"
    r"(?:(?:i'?m|we'?re|i\s+am|we\s+are)\s+)?ready(?:\s+for\s+(?:the\s+)?next(?:\s+(?:step|one))?)?|"
    r"(?:(?:i'?m|we'?re|i\s+am|we\s+are|all)\s+)?(?:done|finished)(?:\s+with\s+(?:that|this|it)(?:\s+(?:step|one))?)?|"
    r"got\s+it|did\s+it|let'?s\s+(?:go|keep\s+going|continue)|"
    r"(?:tell|give|read)\s+me\s+the\s+next\s+(?:step|one)|(?:go\s+to\s+)?(?:the\s+)?next\s+step"
    r")" + _ENDING, re.IGNORECASE)
_BACK_RE = re.compile(
    r"^(?:(?:go|step|move)\s+back(?:\s+(?:a|one)(?:\s+step)?)?|back(?:\s+up)?(?:\s+(?:a|one)\s+step)?|"
    r"previous(?:\s+step)?|(?:go\s+to\s+|read\s+(?:me\s+)?|repeat\s+|what\s+was\s+)?(?:the\s+)?(?:previous|last)\s+step|"
    r"one\s+step\s+back|undo)" + _ENDING, re.IGNORECASE)
_REPEAT_RE = re.compile(
    r"^(?:repeat(?:\s+(?:that|it|this|the\s+step|this\s+step|that\s+step|yourself|the\s+last\s+part))?(?:\s+again)?|"
    r"(?:can|could|would)\s+you\s+(?:repeat|say|read)\s+(?:that|it|this)(?:\s+again)?|"
    r"(?:say|read)\s+(?:that|it|this|the\s+step)\s+again|"
    r"what\s+(?:was\s+that|did\s+you\s+(?:just\s+)?say|was\s+the\s+step)|what|huh|pardon(?:\s+me)?|sorry|"
    r"come\s+again|one\s+more\s+time|again|"
    r"what\s+(?:was|is)\s+(?:this|the\s+current|the)\s+step|what\s+was\s+i\s+(?:doing|supposed\s+to\s+do)|"
    r"what\s+am\s+i\s+(?:doing|supposed\s+to\s+(?:be\s+)?do(?:ing)?)|where\s+(?:was\s+i|were\s+we))" + _ENDING,
    re.IGNORECASE)
_STATUS_RE = re.compile(
    r"^(?:what\s+step\s+(?:am\s+i|are\s+we)\s+on|where\s+(?:am\s+i|are\s+we)|"
    r"how\s+many\s+(?:more\s+)?steps(?:\s+(?:are\s+)?(?:left|remaining|to\s+go|are\s+there|(?:does\s+it|is\s+it)\s+have|in\s+total|total))?|"
    r"how\s+much\s+(?:more\s+)?(?:is\s+)?(?:left|to\s+go)|are\s+we\s+(?:almost\s+)?(?:done|there)|"
    r"how\s+far\s+(?:along\s+)?(?:am\s+i|are\s+we)|am\s+i\s+almost\s+done)" + _ENDING, re.IGNORECASE)
_GOTO_RE = re.compile(
    r"^(?:(?P<move>(?:go|skip|jump|move|take\s+me|bring\s+me|get)(?:\s+back)?\s+to|back\s+to|start\s+(?:at|from|with))\s+|"
    r"(?P<read>what\s+(?:was|is|were)|what's|read(?:\s+me)?|repeat|tell\s+me|say|remind\s+me\s+(?:of|what)|"
    r"what\s+did\s+(?:step)?)\s+)?"
    r"(?:the\s+)?(?:step\s+(?:number\s+)?(?P<n>\w+)|(?P<ord>\w+)\s+step)(?:\s+(?:again|say|was|is))*" + _ENDING,
    re.IGNORECASE)
_START_OVER_RE = re.compile(
    r"^(?:start\s+(?:over|again|from\s+the\s+(?:top|beginning|start))|(?:go\s+)?(?:back\s+)?to\s+the\s+(?:top|beginning|start)|"
    r"restart(?:\s+the\s+(?:recipe|steps))?|from\s+the\s+(?:top|beginning))" + _ENDING, re.IGNORECASE)
_INGREDIENTS_RE = re.compile(
    r"^(?:(?:read|say|list|tell\s+me|give\s+me|go\s+over|repeat|what\s+(?:are|were))\s+(?:me\s+|us\s+)?"
    r"(?:the\s+|all\s+the\s+|all\s+of\s+the\s+)?ingredients(?:\s+again|\s+one\s+more\s+time)?|"
    r"what\s+(?:do|did)\s+(?:i|we)\s+need(?:\s+again)?|what(?:'s|\s+is|\s+goes)\s+in\s+(?:it|this|the\s+recipe)|"
    r"(?:read|say)\s+(?:them|those|the\s+list)\s+(?:again|one\s+more\s+time|slower))" + _ENDING, re.IGNORECASE)
_CHECKLIST_RE = re.compile(
    r"^(?:(?:can\s+you\s+|let'?s\s+)?(?:go|read(?:\s+them)?|do\s+it|go\s+through\s+them|check\s+them))?\s*"
    r"(?:one\s+(?:at\s+a\s+time|by\s+one)|slower|slowly|one\s+ingredient\s+at\s+a\s+time)$", re.IGNORECASE)
_INGREDIENT_N_RE = re.compile(
    r"^what\s+(?:was|is|were)\s+the\s+(?P<n>\w+)\s+(?:one|ingredient|thing|item)(?:\s+again)?$", re.IGNORECASE)
_END_RE = re.compile(
    r"^(?:(?:stop|cancel|end|quit|exit|close|forget|drop)\s+(?:the\s+|this\s+|that\s+|my\s+)?(?:recipe|cooking|baking)(?:\s+mode)?|"
    r"(?:(?:i'?m|we'?re|i\s+am|we\s+are)\s+)?(?:all\s+)?(?:done|finished|through)\s+(?:cooking|baking|with\s+(?:the|this|that)\s+recipe)|"
    r"no\s+more\s+(?:recipe|cooking)|never\s*mind\s+(?:the|this|that)\s+recipe|(?:i|we)\s+don'?t\s+want\s+to\s+(?:cook|bake|make)\s+(?:it|this|that)(?:\s+anymore)?)"
    + _ENDING, re.IGNORECASE)
# Not "stop": that silences an alarm or the music.
_CANCEL_RE = re.compile(r"^(?:never\s*mind|forget\s+it|cancel)$", re.IGNORECASE)
_ANOTHER_RE = re.compile(
    r"^(?:(?:no[, ]+)?(?:give\s+me|find(?:\s+me)?|try|read(?:\s+me)?|how\s+about|what\s+about|"
    r"(?:have|got)\s+you\s+got|do\s+you\s+have|is\s+there|i\s+(?:want|need)|i'?d\s+(?:like|prefer)|"
    r"can\s+(?:i|we|you)\s+(?:get|have|try|find)|let'?s\s+(?:try|do|hear|go\s+with)|(?:i'?d\s+)?rather\s+have)\s+)?"
    r"(?:(?:an?|some)\s+)?(?:(?P<kind>another|different|other|new|simpler|easier|quicker|faster|shorter)"
    r"(?:\s+(?:one|recipe|version|option|way))|another|next\s+recipe|"
    r"something\s+(?P<kind2>else|simpler|easier|quicker|faster|different))"
    r"(?:\s+(?:instead|please))?"
    r"(?:\s+(?:without|with\s+no|that\s+doesn'?t\s+(?:have|use|need))\s+(?P<without>.+?))?" + _ENDING,
    re.IGNORECASE)
_WITHOUT_RE = re.compile(
    r"^(?:(?:is\s+there\s+|do\s+you\s+have\s+|find\s+me\s+|give\s+me\s+|i\s+(?:want|need)\s+)?(?:an?\s+|one\s+)?)?"
    r"(?:(?:recipe|one|version)\s+)?(?:without|with\s+no|that\s+doesn'?t\s+(?:have|use|need))\s+(?P<without>.+)$",
    re.IGNORECASE)
_DISLIKE_RE = re.compile(
    r"^(?:i\s+don'?t\s+(?:like|want)\s+(?:that|this|the\s+sound\s+of\s+(?:that|this))(?:\s+(?:one|recipe))?|"
    r"(?:that|this)\s+(?:one\s+)?(?:sounds|seems|is)\s+(?:too\s+)?(?:hard|complicated|difficult|like\s+a\s+lot|long|much)(?:\s+work)?|"
    r"too\s+(?:many\s+ingredients|complicated|hard|much\s+work|long)|not\s+(?:that|this)\s+one|skip\s+(?:that|this)\s+(?:one|recipe))"
    + _ENDING, re.IGNORECASE)
_HOW_MUCH_RE = re.compile(
    r"^(?:(?:wait|now|remind\s+me)[, ]+)*(?:how\s+(?:much|many)|what\s+amount\s+of|what\s+quantity\s+of)\s+"
    r"(?:of\s+)?(?:the\s+)?(?P<item>.+?)"
    r"(?:\s+(?:do|did|should|shall|was|am|are|is|were|does|will|would|goes|go|to)\b.*|\s+again|\s+in\s+(?:it|this|there|total))?$",
    re.IGNORECASE)
_HOW_LONG_RE = re.compile(
    r"^(?:(?:wait|now|remind\s+me)[, ]+)*(?:how\s+long|how\s+many\s+(?:minutes|hours|mins)|"
    r"for\s+how\s+long|when\s+(?:is|will)\s+it\s+(?:be\s+)?done|when\s+do\s+i\s+take\s+it\s+out)\b(?P<q>.*)$",
    re.IGNORECASE)
_TEMP_RE = re.compile(
    r"\b(?:what\s+(?:temperature|temp|degrees?|heat)|how\s+hot|what(?:'s|\s+is|\s+do\s+i\s+set|\s+should)\s+(?:the\s+)?oven|"
    r"oven\s+(?:temperature|temp)|(?:set|preheat)\s+(?:the\s+)?oven\s+to\s+what|preheat\s+to\s+what|"
    r"what\s+(?:do\s+i\s+)?(?:preheat|set)\s+(?:the\s+)?oven|at\s+what\s+temperature|"
    r"what\s+heat|high\s+or\s+low\s+heat|medium\s+heat\s+or)\b",
    re.IGNORECASE)
_YES_RE = re.compile(
    r"^(?:yes|yeah|yea|yep|yup|ya|sure|of\s+course|correct|right|absolutely|definitely|ok(?:ay)?|"
    r"sounds\s+good|that\s+works|perfect|great|please(?:\s+do)?|do\s+it|go\s+ahead|let'?s\s+(?:go|do\s+it|start|begin)|"
    r"(?:i|we)\s+(?:do|have\s+(?:it|that|those|them|some|everything|all\s+of\s+(?:it|them)|it\s+all)|got\s+(?:it|that|those|them|some|everything))|"
    r"(?:i'?ve|we'?ve)\s+got\s+(?:it|that|those|them|some|everything|it\s+all)|got\s+(?:it|everything|them|all\s+of\s+(?:it|them))|"
    r"all\s+set|all\s+good|i'?m\s+(?:good|all\s+set|set)|everything'?s\s+here)"
    r"(?:[, ]+(?:please|i\s+do|i\s+have(?:\s+(?:it|that|everything|them))?|i'?ve\s+got\s+(?:it|everything|them)|"
    r"we\s+do|thanks|thank\s+you|let'?s\s+go|go\s+ahead|do\s+it|sure|i'?m\s+ready|ready|everything|all\s+of\s+it))*$",
    re.IGNORECASE)
_ALL_RE = re.compile(r"\b(?:everything|all\s+of\s+(?:it|them)|it\s+all|all\s+set|all\s+good|"
                     r"i'?m\s+good|that'?s\s+(?:it|all|everything))\b", re.IGNORECASE)
_NO_RE = re.compile(
    r"^(?:no|nope|nah|no\s+thanks?|no\s+thank\s+you|not\s+really|not\s+quite|not\s+everything|not\s+yet|"
    r"negative|i\s+don'?t(?:\s+think\s+so)?|we\s+don'?t|not\s+(?:right\s+)?now|don'?t|(?:i'?m|we'?re)\s+not(?:\s+ready)?(?:\s+yet)?|"
    r"(?:no[, ]+)?(?:that'?s|that\s+is)\s+(?:it|all|everything)|nothing(?:\s+else)?|none|i'?m\s+good|nope\s+that'?s\s+it|"
    r"(?:i|we)\s+don'?t\s+have\s+(?:it|that|those|them|any|either|that\s+either|those\s+either|it\s+either|any\s+of\s+(?:it|them|those))|"
    r"(?:i'?m|we'?re)\s+(?:out\s+of|missing)\s+(?:it|that|those|them)(?:\s+too)?|no\s+i\s+don'?t)(?:\s+(?:either|too|really|sorry))*$",
    re.IGNORECASE)
_NO_LEAD_RE = re.compile(r"^(?:no|nope|nah|not\s+(?:quite|everything|really)|actually)[, ]+", re.IGNORECASE)
_MISSING_RE = re.compile(
    r"^(?:(?:i|we)\s+(?:don'?t|do\s+not|didn'?t)\s+have\s+(?:any\s+)?|(?:i'?m|i\s+am|we'?re|we\s+are)\s+(?:missing|out\s+of|all\s+out\s+of)\s+|"
    r"(?:i|we)\s+(?:ran|run|have\s+run)\s+out\s+of\s+|(?:i|we)\s+(?:have|got)\s+no\s+|(?:there'?s|there\s+is|there\s+are)\s+no\s+|"
    r"(?:i\s+)?(?:can'?t|couldn'?t)\s+find\s+(?:the\s+|any\s+)?|missing\s+(?:the\s+)?|(?:i\s+)?(?:need|lack)\s+|no\s+)"
    r"(?P<items>.+?)(?:\s+(?:either|too|though|right\s+now|at\s+the\s+moment|at\s+home|in\s+the\s+house))*$",
    re.IGNORECASE)
_PRONOUN_RE = re.compile(r"^(?:it|that|those|them|this|these|any|one|either|anything|some|any\s+of\s+(?:it|them|those))$",
                         re.IGNORECASE)
_SUB_Q_RE = re.compile(
    r"^(?:what\s+(?:can|could|should|do)\s+i\s+(?:use|substitute|put|swap)(?:\s+in)?\s+(?:instead\s+of|for|in\s+place\s+of)|"
    r"(?:what(?:'s|\s+is)\s+)?(?:a|the)\s+(?:good\s+)?(?:substitute|replacement|alternative|sub)\s+(?:for|to)|"
    r"(?:can|could)\s+(?:i|you)\s+(?:substitute|replace|swap)|instead\s+of)\s+(?:the\s+)?(?P<item>.+?)"
    r"(?:\s+(?:in\s+(?:this|the\s+recipe|it)|with\s+.+))?$",
    re.IGNORECASE)
# "Can I use olive oil instead of butter?" — a proposed swap, both halves kept:
# (proposed X, replaces Y). People mix up the order of "swap"/"substitute", so
# CookSession settles the direction by which one the recipe actually uses.
_RECIPE_REF = r"(?:\s+(?:in\s+(?:this|the\s+recipe|it|here)|here|for\s+(?:this|the\s+recipe)))?"
_INSTEAD = r"(?:instead\s+of|in\s+place\s+of|rather\s+than|as\s+a\s+(?:substitute|replacement|sub)\s+for|to\s+replace|for)"
_SWAP_Q_RES = (
    # use X instead of Y / make it with X instead of Y
    re.compile(r"^(?:(?:can|could|should|may|would)\s+(?:i|we)\s+|is\s+it\s+(?:ok(?:ay)?|fine|alright)\s+(?:if\s+i|to)\s+)"
                r"(?:use|try|put\s+in|add|go\s+with|(?:make|do)\s+(?:it|this|them)\s+with)\s+(?:some\s+)?(?P<x>.+?)\s+"
                + _INSTEAD + r"\s+(?:the\s+)?(?P<y>.+?)" + _RECIPE_REF + "$", re.I),
    # substitute X for Y
    re.compile(r"^(?:can|could|should)\s+(?:i|we|you)\s+(?:substitute|sub)\s+(?:in\s+)?(?P<x>.+?)\s+for\s+(?:the\s+)?(?P<y>.+?)"
                + _RECIPE_REF + "$", re.I),
    # replace / swap / switch Y with X
    re.compile(r"^(?:can|could|should)\s+(?:i|we|you)\s+(?:replace|swap(?:\s+out)?|switch(?:\s+out)?|trade)\s+(?:the\s+)?"
                r"(?P<y>.+?)\s+(?:with|for)\s+(?:some\s+)?(?P<x>.+?)" + _RECIPE_REF + "$", re.I),
    # would X work instead of Y / is X okay for Y
    re.compile(r"^(?:would|will|does|do|is|are|can)\s+(?P<x>.+?)\s+(?:work|be\s+(?:ok(?:ay)?|fine|alright|good)|ok(?:ay)?|fine|alright|do)\s+"
                + _INSTEAD + r"\s+(?:the\s+)?(?P<y>.+?)" + _RECIPE_REF + "$", re.I),
    # "what about olive oil instead of butter" / "olive oil instead of butter"
    re.compile(r"^(?:what\s+about|how\s+about)?\s*(?P<x>(?!what\b|can\b|could\b|should\b)[a-z][a-z' -]*?)\s+"
                r"(?:instead\s+of|in\s+place\s+of)\s+(?:the\s+)?(?P<y>.+?)" + _RECIPE_REF + "$", re.I),
)


def parse_swap_question(transcript: str):
    """(x, y) for "can I use X instead of Y" and its variants, or None."""
    text = _TAIL_RE.sub("", _LEAD_RE.sub("", _norm(transcript).lower())).strip(" ,")
    for rx in _SWAP_Q_RES:
        m = rx.match(text)
        if m:
            x, y = m.group("x").strip(" ,"), m.group("y").strip(" ,")
            if (x and y and x != y and len(x.split()) <= 4 and len(y.split()) <= 4
                    and not re.fullmatch(r"(?:this|that|it|them|now|the\s+recipe|here)", y)):
                return x, y
    return None


# "Keep the butter" / "no, I'll use the butter after all" — undoes a swap.
_KEEP_RE = re.compile(
    r"^(?:no|actually|never\s*mind)?[, ]*(?:(?:i'?ll|i\s+will|let'?s|i\s+want\s+to|i'?d\s+rather)\s+)?"
    r"(?:keep|stick\s+with|go\s+back\s+to|use)\s+the\s+(?P<item>[a-z][a-z' -]*?)(?:\s+after\s+all|\s+instead)?" + _ENDING,
    re.IGNORECASE)
_WHOLE_STEP_RE = re.compile(
    r"^(?:(?:read|say|give|tell|repeat)\s+(?:me\s+)?)?(?:the\s+)?(?:whole|entire|full|complete)\s+(?:step|thing)"
    r"(?:\s+again)?" + _ENDING +
    r"|^(?:read|say|repeat)\s+(?:me\s+)?(?:all\s+of\s+(?:it|the\s+step)|(?:the\s+)?(?:step|it)\s+all)"
    r"(?:\s+again)?" + _ENDING,
    re.IGNORECASE)

# "Can I skip the vanilla?" / "do I really need the eggs?" — answered by the
# optional/essential rule, not a substitution.
_SKIP_Q_RE = re.compile(
    r"^(?:can|could)\s+(?:i|we)\s+(?:"
    r"(?:just\s+)?(?:skip|omit)|leave\s+out|do\s+without"
    r")\s+(?:the\s+|any\s+|some\s+)?(?P<item>.+?)"
    r"(?:\s+(?:altogether|completely|today|right\s+now|if\s+i\s+don'?t\s+have\s+(?:it|any|some)))?$"
    r"|^(?:can|could)\s+(?:i|we)\s+leave\s+(?:the\s+|any\s+|some\s+)?(?P<item2>.+?)\s+out"
    r"(?:\s+(?:if\s+i\s+don'?t\s+have\s+(?:it|any|some)|altogether))?$"
    r"|^(?:do|does)\s+(?:i|we)\s+(?:really\s+|actually\s+|absolutely\s+)?"
    r"(?:need|have\s+to\s+(?:have|use)|need\s+to\s+use)\s+(?:the\s+|any\s+|some\s+)?(?P<item3>.+?)"
    r"(?:\s+(?:for\s+this(?:\s+recipe)?|in\s+(?:here|this)|at\s+all|though|today))?$"
    r"|^(?:is|are)\s+(?:the\s+|my\s+)?(?P<item4>.+?)\s+(?:really\s+|absolutely\s+)?"
    r"(?:necessary|needed|essential|optional)(?:\s+(?:here|for\s+this(?:\s+recipe)?|in\s+this))?$"
    r"|^(?:can|could)\s+(?:i|we)\s+(?:make|do|bake)\s+(?:this|that|it|the\s+recipe)\s+"
    r"(?:without|with\s+no)\s+(?:the\s+|any\s+|some\s+)?(?P<item5>.+?)$",
    re.IGNORECASE)
# "How do I know when it's done?" / "what's it supposed to look like?" —
# doneness, answered from the step's own cues before the LLM.
_LOOKS_Q_RE = re.compile(
    r"\bhow\s+(?:do|can|will)\s+(?:i|we|you)\s+(?:know|tell)\b(?:\s+(?:when|if|whether))?[^.]{0,40}?"
    r"\b(?:done|ready|finished|cooked|set|golden|browned)\b"
    r"|\b(?:is|are)\s+(?:it|this|that|they)\s+(?:all\s+|completely\s+|fully\s+)?"
    r"(?:done|ready|finished|cooked|set)\b"
    r"|\bwhat(?:'s|\s+is)\s+(?:it|this|that)\s+supposed\s+to\s+(?:look|be|feel)\s+like\b"
    r"|\bwhat\s+(?:should|does)\s+(?:it|this|that)\s+(?:look|be|feel)\s+like\b"
    r"|\b(?:is|are)\s+(?:it|this|that|they)\s+supposed\s+to\s+(?:look|be|feel|jiggle|wobble)\b",
    re.IGNORECASE)
# Scaling servings (docs/recipe_questions_and_scaling_plan.md §2.1).
_NW = r"\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fifteen|twenty|thirty"
_SCALE_SERVE_RE = re.compile(
    r"^(?:(?:can|could)\s+you\s+)?(?:please\s+)?(?:make|do|scale|adjust|set|size)\s+"
    r"(?:it|this|that|the\s+recipe|everything|them)?\s*(?:for|to)?\s*(?P<n>" + _NW + r")\s*"
    r"(?:people|persons?|servings?|guests?|plates?|portions?|folks?)?$"
    r"|(?:i'?m|i\s+am|we'?re|we\s+are)?\s*(?:just\s+)?(?:cooking|baking|making|feeding)\s+"
    r"(?:it\s+|this\s+|that\s+)?(?:for\s+)?(?P<n2>" + _NW + r")\s*"
    r"(?:people|persons?|servings?|guests?|plates?|portions?|folks?)?$"
    r"|^scale\s+(?:it\s+|the\s+recipe\s+|this\s+)?to\s+(?P<n3>" + _NW + r")"
    r"(?:\s*(?:servings?|people))?$", re.IGNORECASE)
_SCALE_FACTOR_RE = re.compile(
    r"^(?:(?:double|triple|quadruple)(?:\s+(?:it|that|this|the\s+recipe|everything|the\s+amounts?))?|"
    r"twice\s+as\s+much|"
    r"(?:halve|half)(?:\s+(?:it|that|this|the\s+recipe|everything))?|"
    r"make\s+(?:it\s+)?half(?:\s+(?:the\s+recipe|it|that|this))?|cut\s+(?:it|this)\s+in\s+half|"
    r"(?:make|do|scale)\s+(?:it|this|the\s+recipe)\s+one\s+and\s+a\s+half\s+times?|"
    r"one\s+and\s+a\s+half\s+times)$", re.IGNORECASE)
_SCALE_UNDO_RE = re.compile(
    r"^(?:back\s+to\s+(?:the\s+)?original(?:\s+(?:amounts?|recipe|size))?|normal\s+(?:amounts?|size)|"
    r"undo\s+(?:the\s+)?scal(?:e|ing)|original\s+amounts?|stop\s+scaling)$", re.IGNORECASE)
_SERVINGS_ASK_RE = re.compile(
    r"^how\s+many\s+(?:(?:does|did)\s+(?:it|this|the\s+recipe)\s+(?:serve|feed|make)|"
    r"(?:servings?|people)\s+(?:does|do|can)\s+(?:it|this|the\s+recipe)\s+(?:serve|feed)|"
    r"servings?\b)", re.IGNORECASE)


def _scale_factor_value(text: str) -> float | None:
    """Which multiple "double it" / "half the recipe" means."""
    t = text.lower()
    if "one and a half" in t:
        return 1.5
    if "quadruple" in t or "four times" in t:
        return 4.0
    if "triple" in t or "three times" in t:
        return 3.0
    if "double" in t or "twice" in t:
        return 2.0
    if "quarter" in t:
        return 0.25
    if "half" in t or "halve" in t:
        return 0.5
    return None


def _items(text: str) -> list:
    """'sour cream and eggs' -> ['sour cream', 'eggs']. The caller tries the
    whole phrase first, so 'half and half' still matches."""
    text = re.sub(r"^(?:any|the|some|a|an)\s+", "", text.strip(" ,"), flags=re.I)
    parts = re.split(r"\s*,\s*(?:and\s+|or\s+)?|\s+and\s+|\s+or\s+|\s+&\s+|\s+plus\s+", text, flags=re.I)
    out = []
    for p in parts:
        p = re.sub(r"^(?:any|the|some|a|an|no)\s+", "", p.strip(" ,"), flags=re.I)
        if p:
            out.append(p)
    return out


def parse_cook_command(transcript: str):
    """(command, arg) for something said while cooking, or None.

    Commands: next, back, repeat, status, goto (arg (n, move)), start_over,
    checklist, ingredients, ingredient_n (arg n), end, cancel, another (arg dict),
    how_much (arg item), how_long (arg question), temp, sub_q (arg item),
    swap_q (arg (x, y): "can I use X instead of Y"), keep (arg item), whole_step,
    skip_q (arg item), looks_q (arg question),
    scale (arg {"servings": n} / {"factor": f} / {"ask": True}),
    yes (arg True when it says "everything"), no, missing (arg (whole, items)).
    """
    text = _norm(transcript).lower()
    text = _TAIL_RE.sub("", _LEAD_RE.sub("", text)).strip(" ,")
    if not text:
        return None
    if _END_RE.match(text):
        return ("end", None)
    m = _ANOTHER_RE.match(text)
    if m and (m.group("kind") or m.group("kind2")):
        kind = (m.group("kind") or m.group("kind2") or "").lower()
        f = {"simpler": "simpler", "easier": "simpler", "shorter": "simpler",
             "quicker": "quicker", "faster": "quicker"}.get(kind)
        return ("another", {"filter": f, "without": (m.group("without") or "").strip() or None})
    m = _WITHOUT_RE.match(text)
    if m:
        return ("another", {"filter": None, "without": m.group("without").strip()})
    if _DISLIKE_RE.match(text):
        return ("another", {"filter": None, "without": None})
    m = _SCALE_SERVE_RE.match(text)
    if m:
        n = _number(m.group("n") or m.group("n2") or m.group("n3") or "")
        if n and n > 0:
            return ("scale", {"servings": n})
    if _SCALE_FACTOR_RE.match(text):
        f = _scale_factor_value(text)
        if f:
            return ("scale", {"factor": f})
    if _SCALE_UNDO_RE.match(text):
        return ("scale", {"factor": 1.0})
    if _SERVINGS_ASK_RE.match(text):
        return ("scale", {"ask": True})
    if _START_OVER_RE.match(text):
        return ("start_over", None)
    if _STATUS_RE.match(text):
        return ("status", None)
    if _CHECKLIST_RE.match(text):
        return ("checklist", None)
    if _INGREDIENTS_RE.match(text):
        return ("ingredients", None)
    m = _INGREDIENT_N_RE.match(text)
    if m and _number(m.group("n")):
        return ("ingredient_n", _number(m.group("n")))
    if _BACK_RE.match(text):
        return ("back", None)
    m = _GOTO_RE.match(text)
    if m:
        n = _number(m.group("n") or m.group("ord") or "")
        if n is not None and not (m.group("ord") and m.group("ord").lower() in ("next", "this", "current")):
            return ("goto", (n, not m.group("read")))
    if _NEXT_RE.match(text):
        return ("next", None)
    if _REPEAT_RE.match(text):
        return ("repeat", None)
    if _WHOLE_STEP_RE.match(text):
        return ("whole_step", None)
    if _CANCEL_RE.match(text):
        return ("cancel", None)
    pair = parse_swap_question(text)
    if pair:
        return ("swap_q", pair)
    m = _KEEP_RE.match(text)
    if m:
        return ("keep", m.group("item").strip())
    m = _SUB_Q_RE.match(text)
    if m:
        return ("sub_q", m.group("item").strip())
    m = _SKIP_Q_RE.match(text)
    if m:
        item = next((g for g in m.groups() if g), "")
        return ("skip_q", item.strip(" ,"))
    if _TEMP_RE.search(text):
        return ("temp", text)
    m = _HOW_LONG_RE.match(text)
    if m:
        return ("how_long", text)
    if _LOOKS_Q_RE.search(text):
        return ("looks_q", text)
    m = _HOW_MUCH_RE.match(text)
    if m:
        item = re.sub(r"^(?:the|of)\s+", "", m.group("item").strip())
        if item and not re.match(r"^(?:is|was|more|left|longer|time|steps?)$", item):
            return ("how_much", item)
    if _YES_RE.match(text):
        return ("yes", bool(_ALL_RE.search(text)))
    if _NO_RE.match(text):
        return ("no", None)
    rest = _NO_LEAD_RE.sub("", text)
    m = _MISSING_RE.match(rest)
    if m:
        whole = re.sub(r"^(?:any|the|some|a|an)\s+", "", m.group("items").strip(" ,"), flags=re.I)
        if _PRONOUN_RE.match(whole):
            return ("no", None)
        return ("missing", (whole, _items(whole)))
    if rest != text:
        # "no, the eggs" — a bare list after a no.
        return ("missing", (rest, _items(rest)))
    return None


def bare_items(transcript: str) -> tuple:
    """An answer to "what are you missing?" that is just the things: "eggs and butter"."""
    text = _TAIL_RE.sub("", _LEAD_RE.sub("", _norm(transcript).lower())).strip(" ,")
    text = re.sub(r"^(?:just|only|i\s+think|probably|maybe)\s+", "", text)
    return text, _items(text)
