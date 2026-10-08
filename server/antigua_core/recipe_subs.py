"""Ingredient matching and common substitutions for the recipe skill.

The table is curated kitchen knowledge, not generated: each entry lists
alternatives in the order worth trying, as (short name, how to use it).
Keys are matched against an ingredient's core name, longest key first, so
"brown sugar" wins over "sugar".

"How" can depend on the dish: a dict keyed by context — "cook", "bake", or
"creamed" (baked, and this ingredient is beaten with the sugar) — falls back
creamed → bake → cook. None there means the swap doesn't work in that kind
of recipe; _NOT_HERE says why.
"""

import re

SUBSTITUTES = {
    "buttermilk": [("milk and lemon juice", "1 cup of milk with 1 tablespoon of lemon juice or vinegar stirred in, left for 5 minutes, for each cup"),
                   ("plain yogurt", "the same amount of plain yogurt thinned with a little milk")],
    "sour cream": [("plain Greek yogurt", "the same amount of plain Greek yogurt"),
                   ("plain yogurt", "the same amount of plain yogurt"),
                   ("cream cheese", "cream cheese thinned with a little milk")],
    "greek yogurt": [("sour cream", "the same amount of sour cream"),
                     ("plain yogurt", "plain yogurt, strained if you have time")],
    "yogurt": [("sour cream", "the same amount of sour cream"),
               ("buttermilk", "buttermilk, a little less of it")],
    "heavy cream": [("milk and butter", "three quarters of a cup of milk plus a quarter cup of melted butter for each cup"),
                    ("half and half", "the same amount of half and half, though it won't whip")],
    "whipping cream": [("milk and butter", "three quarters of a cup of milk plus a quarter cup of melted butter for each cup")],
    "half and half": [("milk and cream", "half milk and half heavy cream"),
                      ("milk and butter", "a cup of milk with a tablespoon of melted butter for each cup")],
    "evaporated milk": [("regular milk", "the same amount of whole milk, for a slightly thinner result")],
    "milk": [("water and butter", "the same amount of water plus a tablespoon of butter per cup"),
             ("plant milk", "the same amount of an unsweetened plant milk like oat or almond")],
    "cream cheese": [("mascarpone", "the same amount of mascarpone"),
                     ("ricotta", "the same amount of ricotta, blended smooth")],
    "mascarpone": [("cream cheese", "the same amount of cream cheese, softened")],
    "ricotta": [("cottage cheese", "the same amount of cottage cheese, blended smooth")],
    "parmesan": [("pecorino", "the same amount of pecorino romano"),
                 ("any hard cheese", "any hard, salty cheese, grated")],
    # Fats, 2026-10-06. ¾ oil for butter, never where the butter is creamed
    # with sugar (oil holds no air): Bob's Red Mill, "Can I Substitute Oil for
    # Butter?"; California Olive Ranch, "How To Substitute Olive Oil For Butter
    # In Baking". Coconut oil 1:1, solid when it's creamed.
    "butter": [("oil", {"cook": "three quarters as much oil",
                        "bake": "three quarters as much oil; it'll come out a little denser and moister",
                        "creamed": None}),
               ("coconut oil", {"cook": "the same amount of coconut oil",
                                "creamed": "the same amount of solid coconut oil at room temperature, beaten with the sugar like butter"}),
               ("margarine", "the same amount of margarine")],
    "egg": [("flax egg", "a flax egg for each one: 1 tablespoon of ground flaxseed mixed with 3 tablespoons of water, left for 5 minutes"),
            ("applesauce", "a quarter cup of unsweetened applesauce per egg, in sweet baking")],
    "brown sugar": [("white sugar and molasses", "1 cup of white sugar plus 1 tablespoon of molasses for each cup"),
                    ("white sugar", "the same amount of white sugar, with a slightly different flavor")],
    "powdered sugar": [("blended sugar", "granulated sugar blended until fine, with a teaspoon of cornstarch per cup")],
    "sugar": [("honey", "three quarters as much honey, and a little less liquid elsewhere"),
              ("brown sugar", "the same amount of brown sugar")],
    "honey": [("maple syrup", "the same amount of maple syrup"),
              ("sugar", "a quarter more sugar plus a splash of water")],
    "maple syrup": [("honey", "the same amount of honey")],
    "molasses": [("dark brown sugar", "three quarters of a cup of dark brown sugar per cup"),
                 ("honey", "the same amount of honey")],
    "corn syrup": [("honey", "the same amount of honey")],
    "cake flour": [("flour and cornstarch", "1 cup of all-purpose flour minus 2 tablespoons, plus 2 tablespoons of cornstarch, for each cup")],
    "self-rising flour": [("flour, baking powder and salt", "1 cup of all-purpose flour plus 1 and a half teaspoons of baking powder and a quarter teaspoon of salt for each cup")],
    "bread flour": [("all-purpose flour", "the same amount of all-purpose flour; the bread will be a little softer")],
    "baking powder": [("baking soda and cream of tartar", "a quarter teaspoon of baking soda plus half a teaspoon of cream of tartar for each teaspoon")],
    "baking soda": [("baking powder", "three times as much baking powder")],
    "cornstarch": [("flour", "twice as much all-purpose flour")],
    "breadcrumb": [("crushed crackers", "the same amount of crushed crackers"),
                   ("oats", "the same amount of quick oats")],
    "graham cracker": [("vanilla wafers", "the same amount of crushed vanilla wafers or digestive biscuits")],
    "lemon juice": [("lime juice", "the same amount of lime juice"),
                    ("vinegar", "half as much white vinegar")],
    "lime juice": [("lemon juice", "the same amount of lemon juice")],
    "lemon zest": [("lemon juice", "half a teaspoon of lemon juice per teaspoon of zest"),
                   ("lemon extract", "a few drops of lemon extract")],
    "vinegar": [("lemon juice", "the same amount of lemon juice")],
    "white wine": [("broth and vinegar", "the same amount of chicken or vegetable broth with a splash of white wine vinegar")],
    "red wine": [("broth and vinegar", "the same amount of beef broth with a splash of red wine vinegar")],
    "chicken broth": [("vegetable broth", "the same amount of vegetable broth"),
                      ("bouillon", "a bouillon cube dissolved in a cup of hot water for each cup")],
    "chicken stock": [("chicken broth", "the same amount of chicken broth"),
                      ("bouillon", "a bouillon cube dissolved in a cup of hot water for each cup")],
    "vegetable broth": [("chicken broth", "the same amount of chicken broth"),
                        ("bouillon", "a bouillon cube dissolved in a cup of hot water for each cup")],
    "beef broth": [("bouillon", "a beef bouillon cube dissolved in a cup of hot water for each cup"),
                   ("chicken broth", "chicken broth with a splash of soy sauce")],
    "vanilla extract": [("maple syrup", "the same amount of maple syrup"),
                        ("almond extract", "half as much almond extract")],
    "garlic": [("garlic powder", "an eighth of a teaspoon of garlic powder per clove")],
    "onion": [("onion powder", "1 tablespoon of onion powder per medium onion"),
              ("shallots", "3 or 4 shallots per onion")],
    "shallot": [("onion", "a small onion for every 3 shallots")],
    "tomato paste": [("tomato sauce", "3 times as much tomato sauce, cooked down a little")],
    "tomato sauce": [("tomato paste and water", "half tomato paste, half water")],
    "soy sauce": [("worcestershire", "the same amount of Worcestershire sauce with a pinch of salt")],
    "worcestershire": [("soy sauce", "the same amount of soy sauce with a squeeze of lemon")],
    "dijon mustard": [("yellow mustard", "the same amount of yellow mustard")],
    "mayonnaise": [("greek yogurt", "the same amount of plain Greek yogurt")],
    "vegetable oil": [("oil", "the same amount of canola or any mild oil; olive oil adds a little flavor"),
                      ("melted butter", "the same amount of melted butter")],
    "olive oil": [("oil", "the same amount of another oil")],
    "coconut oil": [("butter", "the same amount of butter"),
                    ("oil", "the same amount of another oil")],
    "oil": [("oil", "the same amount of another oil"),
            ("melted butter", "the same amount of melted butter")],
    "shortening": [("butter", "a quarter more butter than the shortening"),
                   ("coconut oil", "the same amount of solid coconut oil")],
    "margarine": [("butter", "the same amount of butter")],
    "semi-sweet chocolate": [("cocoa, sugar and butter", "1 tablespoon of cocoa powder, 1 tablespoon of sugar and 1 teaspoon of butter for each ounce")],
    "cocoa powder": [("unsweetened chocolate", "1 ounce of unsweetened chocolate for every 3 tablespoons, with a tablespoon less butter")],
    "egg noodle": [("any pasta", "the same weight of any short pasta")],
    "pasta": [("any other pasta", "any other pasta shape of about the same size")],
    "rice": [("quinoa", "the same amount of quinoa, cooked the same way")],
    "celery": [("fennel", "the same amount of fennel"), ("more onion", "a little more onion or carrot")],
    "carrot": [("parsnip", "the same amount of parsnip or sweet potato")],
    "green onion": [("chives", "the same amount of chives"), ("onion", "a little finely chopped onion")],
    "cilantro": [("parsley", "the same amount of parsley")],
    "parsley": [("cilantro", "the same amount of cilantro"), ("dried parsley", "a third as much dried parsley")],
    "basil": [("dried basil", "a third as much dried basil"), ("oregano", "a little oregano")],
    "thyme": [("dried thyme", "a third as much dried thyme"), ("oregano", "the same amount of oregano")],
    "rosemary": [("dried rosemary", "a third as much dried rosemary"), ("thyme", "the same amount of thyme")],
    "oregano": [("basil", "the same amount of basil"), ("thyme", "the same amount of thyme")],
    "dill": [("dried dill", "a third as much dried dill")],
    "ginger": [("ground ginger", "a quarter teaspoon of ground ginger per tablespoon of fresh")],
    "cinnamon": [("allspice or nutmeg", "a quarter as much allspice or nutmeg")],
    "nutmeg": [("cinnamon", "the same amount of cinnamon"), ("allspice", "the same amount of allspice")],
    "allspice": [("cinnamon, nutmeg and cloves", "half cinnamon, a quarter nutmeg, a quarter cloves")],
    "pumpkin pie spice": [("cinnamon, ginger and nutmeg", "half cinnamon, a quarter ginger, a quarter nutmeg, pinch of cloves")],
    "cumin": [("chili powder", "half as much chili powder"), ("coriander", "the same amount of ground coriander")],
    "chili powder": [("paprika and cayenne", "the same amount of paprika with a pinch of cayenne and cumin")],
    "paprika": [("chili powder", "half as much chili powder")],
    "cayenne": [("red pepper flakes", "the same amount of red pepper flakes"), ("hot sauce", "a few dashes of hot sauce")],
    "red pepper flake": [("cayenne", "a pinch of cayenne")],
    "bay leaf": [("thyme", "a pinch of dried thyme")],
    "italian seasoning": [("basil, oregano and thyme", "equal parts dried basil, oregano and thyme")],
    "salt": [("soy sauce", "a splash of soy sauce, in savory dishes")],
}

# Why a swap the table knows doesn't work in this kind of recipe.
_NOT_HERE = {
    "creamed": "this recipe beats the {name} with the sugar, and {x} can't hold the air "
               "that makes it rise, so it would come out dense",
}

# The same thing by another name: "how much broth" when it says chicken stock.
_ALIASES = [
    ("broth", "stock"), ("scallion", "green onion"), ("spring onion", "green onion"),
    ("coriander leaves", "cilantro"), ("confectioners sugar", "powdered sugar"),
    ("icing sugar", "powdered sugar"), ("heavy whipping cream", "heavy cream"),
    ("whipping cream", "heavy cream"), ("double cream", "heavy cream"),
    ("garbanzo bean", "chickpea"), ("aubergine", "eggplant"), ("courgette", "zucchini"),
    ("prawn", "shrimp"), ("corn starch", "cornstarch"), ("cornflour", "cornstarch"),
    ("bicarbonate of soda", "baking soda"), ("bicarb", "baking soda"),
    ("caster sugar", "superfine sugar"), ("plain flour", "all-purpose flour"),
]
# Oils that stand in for "oil" in the table; coconut oil is its own entry.
_OIL_FAMILY = re.compile(r"\b(?:olive|vegetable|canola|avocado|sunflower|safflower|grapeseed|peanut|"
                         r"corn|neutral|light|mild|cooking|rapeseed)\s+oil\b|^oil$", re.IGNORECASE)
# Words in a table short name that don't name the thing ("plain Greek yogurt").
_LOOSE = {"plain", "any", "regular", "other", "mild", "neutral", "unsweetened", "melted", "more", "a", "little"}

# Words that describe an ingredient rather than name it.
_DESCRIPTORS = set("""
a an the of or to for into in with about plus
cup cups tablespoon tablespoons teaspoon teaspoons ounce ounces pound pounds
gram grams kilogram kilograms milliliter milliliters liter liters quart quarts
pint pints gallon gallons package packages can cans jar jars bottle bottles box
boxes bag bags stick sticks block blocks clove cloves pinch dash handful bunch
bunches sprig sprigs slice slices piece pieces head heads stalk stalks rib ribs
large small medium big whole fresh freshly dried ground chopped finely roughly
coarsely minced diced sliced grated shredded melted softened cold warm hot
room temperature packed divided plus extra more optional taste serving
unsalted salted granulated pure full fat full-fat low-fat nonfat light dark
boneless skinless raw cooked peeled seeded trimmed beaten lightly fine sea
kosher low-sodium reduced-sodium extra virgin extra-virgin good quality store-bought
homemade choice your favorite
""".split())
_OPTIONAL_RE = re.compile(r"\b(?:optional|for\s+garnish|garnish|to\s+taste|for\s+serving|to\s+serve|"
                          r"for\s+topping|toppings?|if\s+(?:desired|you\s+like))\b", re.IGNORECASE)
_STRUCTURAL_RE = re.compile(r"\b(?:flour|eggs?|egg\s+yolks?|butter|sugar|baking\s+(?:powder|soda)|yeast|"
                            r"oil|milk|cream\s+cheese|gelatin|cornstarch)\b", re.IGNORECASE)
_BASE_RE = re.compile(r"\b(?:broth|stock|water|pasta|noodles?|rice|tortillas?|dough|crust)\b", re.IGNORECASE)
_FLAVOR_RE = re.compile(r"\b(?:salt|pepper|cinnamon|nutmeg|paprika|cumin|oregano|basil|thyme|parsley|"
                        r"cilantro|rosemary|bay\s+lea(?:f|ves)|chili\s+(?:powder|flakes)|cayenne|dill|sage|"
                        r"zest|vanilla|extract|garlic|onions?|shallots?|seasoning|spice|herbs?|"
                        r"celery|carrots?|peas|corn|mushrooms?|scallions?|green\s+onions?|chives|"
                        r"lemon\s+juice|lime\s+juice|nuts?|walnuts?|pecans?|almonds?|raisins?|"
                        r"chocolate\s+chips?|sprinkles|berries|fruit|sauce)\b", re.IGNORECASE)


def _stem(word: str) -> str:
    word = word.lower()
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("oes", "ches", "shes", "xes")):
        return word[:-2]
    if word.endswith("ves") and len(word) > 4:
        return word[:-3] + "f"
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


def words(text: str) -> list:
    return [_stem(w.strip("'")) for w in re.findall(r"[a-z][a-z'-]*", (text or "").lower())]


def _alias_forms(item: str) -> list:
    """`item` with each known alias swapped in, both ways ("broth" -> "stock")."""
    low, out = " ".join(words(item)), []
    for a, b in _ALIASES:
        for src, dst in ((a, b), (b, a)):
            k = " ".join(words(src))
            if re.search(rf"(?:^|\s){re.escape(k)}(?:\s|$)", low):
                out.append(re.sub(rf"(?:^|(?<=\s)){re.escape(k)}(?=\s|$)", " ".join(words(dst)), low))
    return out


def core_name(line: str) -> str:
    """'2 (8 ounce) packages cream cheese, softened' -> 'cream cheese';
    '1 pound boneless, skinless chicken thighs' -> 'chicken thighs';
    '5 ounces egg noodles or pasta of choice' -> 'egg noodles'."""
    text = re.sub(r"\([^)]*\)", " ", line)
    text = re.split(r"\bfor\s+the\b|\bdivided\b|\bto\s+taste\b", text, maxsplit=1)[0]
    for part in text.split(","):
        part = re.split(r"\s+or\s+", part, maxsplit=1)[0]
        part = re.sub(r"[\d/.]+", " ", part)
        kept = [w for w in re.findall(r"[A-Za-z][A-Za-z'-]*", part) if w.lower() not in _DESCRIPTORS]
        if kept:
            return " ".join(kept).lower()
    return line.strip().lower()


def match_ingredient(item: str, ingredients: list) -> list:
    """Indexes of the ingredient lines that the spoken item names, best first.
    'eggs' matches '4 large eggs' and '3 large egg yolks'; the line whose core
    name is closest wins. 'broth' finds 'chicken stock' (_ALIASES)."""
    hits = _match(item, ingredients)
    for form in ([] if hits else _alias_forms(item)):
        hits = _match(form, ingredients)
        if hits:
            break
    return hits


def _match(item: str, ingredients: list) -> list:
    want = set(words(item)) - _DESCRIPTORS - {"and"}
    if not want:
        return []
    hits = []
    for i, line in enumerate(ingredients):
        name = set(words(core_name(line)))
        have = set(words(line))
        if want <= have:
            extra = len(name - want)
            hits.append((0 if want <= name else 1, extra, i))
        elif len(want) > 1 and len(want & name) >= max(1, len(want) - 1) and want & name:
            hits.append((2, len(name - want), i))
    hits.sort()
    return [i for *_, i in hits]


def _entry(name: str) -> list:
    for form in [name] + _alias_forms(name):
        stems = " ".join(words(form))
        for key in sorted(SUBSTITUTES, key=len, reverse=True):
            k = " ".join(words(key))
            if re.search(rf"(?:^|\s){re.escape(k)}(?:\s|$)", stems):
                return SUBSTITUTES[key]
    return []


def _how(how, context: str):
    """The table's advice for this kind of recipe; None = doesn't work here."""
    if isinstance(how, str):
        return how
    for c in {"creamed": ("creamed", "bake", "cook"), "bake": ("bake", "cook")}.get(context, ("cook",)):
        if c in how:
            return how[c]
    return None


def substitutes_for(name: str, context: str = "cook") -> list:
    """[(short, how)] for an ingredient's core name, or [] — only the ones
    that work in this kind of recipe (see the module docstring)."""
    return [(short, h) for short, how in _entry(name) if (h := _how(how, context))]


def _names_alt(x: str, short: str) -> int:
    """How well the spoken `x` names the table's `short` (0 = not at all).
    'olive oil' names 'oil'; 'greek yogurt' names 'plain Greek yogurt'."""
    if short == "oil" and _OIL_FAMILY.search(x.strip()):
        return 1
    xs = set(words(x)) - _DESCRIPTORS - _LOOSE
    ss = set(words(short)) - _DESCRIPTORS - _LOOSE
    if not xs or not ss or not (xs <= ss or ss <= xs):
        return 0
    return 10 - len(xs ^ ss)


def swap_verdict(name: str, x: str, context: str = "cook"):
    """Is `x` okay in place of `name`, by the table? ("yes", how),
    ("no", why) or (None, None) when the table doesn't list x for it."""
    best, best_score = None, 0
    for short, how in _entry(name):
        score = _names_alt(x, short)
        if score > best_score:
            best, best_score = (short, how), score
    if not best:
        return None, None
    h = _how(best[1], context)
    if h:
        return "yes", h
    why = _NOT_HERE.get(context, "it doesn't work in this kind of recipe")
    return "no", why.format(name=name, x=x)


def general_swap_answer(name: str, x: str) -> str | None:
    """"Can I use X instead of Y?" with no recipe open: the table's answer
    for both kinds of recipe, or None when it doesn't list X for Y."""
    best, best_score = None, 0
    for short, how in _entry(name):
        score = _names_alt(x, short)
        if score > best_score:
            best, best_score = how, score
    if best is None:
        return None
    if isinstance(best, str):
        return f"Yes. Instead of {name}, use {best}."
    cook, bake = _how(best, "cook"), _how(best, "bake")
    oil = lambda h: re.sub(r"\bas much oil\b", f"as much {x}", h)  # noqa: E731
    parts = [f"In cooking, use {oil(cook)}." if cook else "Not in cooking."]
    if bake and bake != cook:
        parts.append(f"In baking, use {oil(bake)}.")
    if "creamed" in best and best["creamed"] is None:
        parts.append(f"But not where the {name} is beaten with the sugar, like most cookies and cakes.")
    elif best.get("creamed"):
        parts.append(f"Where it's beaten with the sugar, use {best['creamed']}.")
    return " ".join(parts)


def table_swap(x: str, y: str) -> str | None:
    """general_swap_answer either way round ("swap butter for olive oil")."""
    return general_swap_answer(y, x) or general_swap_answer(x, y)


def is_creamed(item: str, steps: list) -> bool:
    """Is `item` (say "butter") beaten with the sugar in these steps?"""
    w = re.escape(item.split()[-1].rstrip("s"))
    pat = (rf"(?<!heavy\s)(?<!sour\s)(?<!whipping\s)(?<!ice\s)\b(?:cream|beat)(?:ed|ing|s)?\b[^.;]{{0,60}}?"
           rf"(?:\b{w}s?\b[^.;]{{0,50}}?\bsugars?\b|\bsugars?\b[^.;]{{0,50}}?\b{w}s?\b)")
    return bool(re.search(pat, " ".join(steps), re.IGNORECASE))


def is_optional(line: str) -> bool:
    return bool(_OPTIONAL_RE.search(line))


def is_essential(line: str, dish: str, title: str, baked: bool) -> bool:
    """Can the dish be made without this line? Optional or flavoring lines can
    be left out; what the dish is named after, the base (broth, pasta,
    dough) and, when it's baked, what holds it together cannot."""
    if is_optional(line):
        return False
    name = words(core_name(line))
    named = " ".join(words(f"{dish} {title}"))
    for w in name:
        if len(w) >= 4 and w not in _DESCRIPTORS and w in named:
            return True
    if _BASE_RE.search(line):
        return True
    if baked and _STRUCTURAL_RE.search(core_name(line)):
        return True
    if _FLAVOR_RE.search(core_name(line)):
        return False
    return True


# ── which lines a step uses (the kiosk lights them while it's read) ─────────

# Words that name the kind of thing, not the thing: "the juices from the pan"
# isn't the lemon juice. They count only as part of the whole name.
_VAGUE_HEADS = {"juice", "zest", "powder", "seed", "extract", "paste", "leaf", "flake",
                "water", "mixture", "ingredient", "sauce"}
# A name ending in a cut goes by its first word too: "the chicken" for
# "chicken thighs". Any other first word ("beef" in "beef broth") doesn't.
_CUTS = {"thigh", "breast", "fillet", "leg", "wing", "drumstick", "piece", "strip", "chunk",
         "cube", "tenderloin", "chop", "loin", "shoulder", "rib"}
_NOT_NAMES = {"lb", "oz", "inch", "-inch", "piece", "batch"}
# One spelling per thing, both sides: "red chilli powder" is the "chili powder".
_SPELLINGS = {"chilli": "chili", "chile": "chili", "chilie": "chili", "yoghurt": "yogurt",
              "capsicum": "pepper", "aubergine": "eggplant", "courgette": "zucchini"}


def _spelled(ws: list) -> list:
    return [_SPELLINGS.get(w, w) for w in ws]


_REMAINING_RE = re.compile(r"\b(?:remaining|rest\s+of\s+the)\s+ingredients?\b", re.I)
_ALL_RE = re.compile(r"\ball\s+(?:of\s+)?(?:the\s+|your\s+)?ingredients\b", re.I)


def _names(line: str) -> list:
    """The name(s) a line goes by in a step, as stemmed words: '2 cups white
    sugar' -> [['white', 'sugar']]; 'salt and pepper to taste' -> both."""
    name = core_name(line)
    parts = re.split(r"\s+and\s+|\s*&\s*", name)
    out = []
    for part in parts:
        ws = _spelled([w for w in words(part) if w.lstrip("-") not in _NOT_NAMES])
        if ws:
            out.append(ws)
    return out


def step_ingredients(steps: list, ingredients: list, groups=(), title: str = "") -> list:
    """For each step, the indexes of the ingredient lines it uses, in line
    order. A line's whole name ('garam masala'), or how it ends, claims
    those words first; then its last word ('the yogurt'), unless that only
    names a kind of thing ('juice') and more than one line could be meant;
    then, for a cut, its first word ('the chicken' for 'chicken thighs').
    "The sauce ingredients" is the whole group, "the remaining ingredients"
    every line no earlier step used, "all the ingredients" all of them. The
    dish's own name ("serve the chicken tikka masala") names no line. When a
    name is listed twice (salt in the marinade and the sauce), a line no
    earlier step used wins, then the one used most recently, then the group
    the step otherwise draws from."""
    names = [_names(line) for line in ingredients]
    heads = [ws[-1] for line_names in names for ws in line_names]
    dish = [w for w in _spelled(words(re.sub(r"\([^)]*\)", " ", title))) if w != "recipe"]
    group_of = {}
    labels = []
    for name, a, b in groups:
        label = re.sub(r"^(?:for\s+(?:the\s+)?)", "", name, flags=re.I).strip()
        labels.append((words(label), a, b))
        for i in range(a, b + 1):
            group_of[i] = (a, b)
    used, out = {}, []  # line -> the last step that used it
    for n_step, step in enumerate(steps):
        text = _spelled(words(step))
        taken = [False] * len(text)
        found = {}  # line -> strength (0 whole name, 1 last word, 2 other word)

        def at(seq, text=text):
            n = len(seq)
            return [k for k in range(len(text) - n + 1) if text[k:k + n] == seq]

        for k0 in range(len(dish) - 1):
            for k in at(dish[k0:]):
                for j in range(k, k + len(dish) - k0):
                    taken[j] = True

        for i, line_names in enumerate(names):
            # The whole name, or how it ends ("yogurt sauce" for "lemon
            # yogurt sauce"); never one vague word on its own.
            for ws in (n[k:] for n in line_names for k in range(max(1, len(n) - 1))):
                if len(ws) == 1 and ws[0] in _VAGUE_HEADS:
                    continue
                for k in at(ws):
                    found[i] = 0
                    for j in range(k, k + len(ws)):
                        taken[j] = True
        free = {w for w, t in zip(text, taken) if not t}
        # A vague word still names the one line it could ("blend the
        # juice"), but not "the juices from the pan".
        loose = {w for k, w in enumerate(text) if not taken[k]
                 and text[k + 1:k + 2] != ["from"] and heads.count(w) == 1}
        for i, line_names in enumerate(names):
            if i in found:
                continue
            for ws in line_names:
                if ws[-1] in free and (ws[-1] not in _VAGUE_HEADS or ws[-1] in loose):
                    found[i] = 1
                elif len(ws) > 1 and ws[-1] in _CUTS and ws[0] in free:
                    found.setdefault(i, 2)
        if _ALL_RE.search(step):
            found.update({i: 0 for i in range(len(ingredients)) if i not in found})
        elif _REMAINING_RE.search(step):
            found.update({i: 0 for i in range(len(ingredients)) if i not in used})
        for label, a, b in labels:
            if label and at(label + ["ingredient"]):
                found.update({i: 0 for i in range(a, b + 1) if i not in found})
        # One line per name listed twice.
        by_name = {}
        for i in found:
            by_name.setdefault(" ".join(names[i][0]) if names[i] else str(i), []).append(i)
        keep = set()
        for same in by_name.values():
            if len(same) == 1 or not group_of:
                keep.update(same)
                continue
            others = [g for j, g in group_of.items() if j in found and j not in same]
            fresh = [i for i in same if i not in used] or same
            keep.add(max(fresh, key=lambda i: (used.get(i, -1),
                                               sum(g == group_of.get(i) for g in others), -i)))
        lines = sorted(keep)
        used.update({i: n_step for i in lines})
        out.append(lines)
    return out
