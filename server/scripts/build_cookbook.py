#!/usr/bin/env python3
"""Fill the offline cookbook (data/cookbook/) from config/cookbook_dishes.yaml.

Each dish is searched the way a spoken "how do I make <dish>" is, and only
popular recipes are kept (recipe.COOKBOOK_MIN_RATING / _MIN_RATINGS, up to
COOKBOOK_KEEP per dish). Dishes already in the cookbook are skipped unless
--refresh. One dish at a time with a pause between, to be polite to the
recipe sites.

    venv/bin/python3 server/scripts/build_cookbook.py            # the whole list
    venv/bin/python3 server/scripts/build_cookbook.py --only "pad thai" --refresh
    venv/bin/python3 server/scripts/build_cookbook.py --list     # what's in it
    venv/bin/python3 server/scripts/build_cookbook.py --urls found.json

--urls skips SearXNG: the recipe pages for each dish were found some other
way (2026-10-06: a long SearXNG run got its engines suspended for the whole
household). Pages are fetched one at a time, at most one request per site
every --site-gap seconds.
"""

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import yaml

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))

from antigua_core import settings  # noqa: E402
from antigua_core.recipe import Cookbook, RecipeFinder, dish_key, rank  # noqa: E402

DISHES = SERVER / "config" / "cookbook_dishes.yaml"


def load_dishes(path=DISHES) -> list:
    """[(dish, [aliases], category)], first mention of each dish wins."""
    out, seen = [], set()
    for category, items in (yaml.safe_load(path.read_text()) or {}).items():
        for item in items or []:
            dish, _, rest = str(item).partition("|")
            dish = dish.strip()
            key = dish_key(dish)
            if not key or key in seen:
                continue
            seen.add(key)
            out.append((dish, [a.strip() for a in rest.split(",") if a.strip()], category))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--only", action="append", help="just this dish (repeatable)")
    ap.add_argument("--refresh", action="store_true", help="search dishes already in the cookbook too")
    ap.add_argument("--pause", type=float, default=3.0, help="seconds between dishes")
    ap.add_argument("--retry-wait", type=float, default=60.0,
                    help="seconds to wait before retrying a dish whose search found no recipe pages")
    ap.add_argument("--list", action="store_true", help="print the cookbook and exit")
    ap.add_argument("--urls", type=Path, help='JSON {"dish": [recipe page urls]}: fetch these, no search')
    ap.add_argument("--site-gap", type=float, default=5.0, help="seconds between requests to one site")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    cfg_path = settings.local_or_example(os.environ.get("ANTIGUA_CONFIG") or SERVER / "config" / "server.yaml")
    settings.configure(yaml.safe_load(Path(cfg_path).read_text()))
    book = Cookbook()

    if args.list:
        files = sorted((settings.DATA_DIR / "cookbook").glob("*.json"))
        for f in files:
            d = json.loads(f.read_text())
            tops = ", ".join(f"{r['source']} {r['rating']:.1f}/{r['rating_count']}" for r in d["recipes"])
            print(f"{d['dish']:32} {tops}")
        print(f"{len(files)} dishes")
        return 0

    if args.urls:
        return from_urls(args, book)
    if not settings.SEARCH_ENABLED:
        print("search is off in server.yaml; the cookbook is filled from the web")
        return 1
    finder = RecipeFinder(settings.RECIPE_CACHE_DAYS, cookbook=book)
    dishes = load_dishes()
    if args.only:
        want = {dish_key(d) for d in args.only}
        dishes = [d for d in dishes if dish_key(d[0]) in want] or [(d, [], "extra") for d in args.only]

    kept, missed, dry = 0, [], 0
    for n, (dish, aliases, category) in enumerate(dishes, 1):
        if book.has(dish) and not args.refresh:
            book.put(dish, [], aliases)          # aliases may have changed
            continue
        # A long run gets the search engines throttled, and they answer with
        # unrelated pages for a while (2026-10-06: "pumpkin pie recipe" ->
        # SAP Concur forums). No recipe page at all means wait and try again.
        got = []
        for attempt in range(2):
            try:
                got = finder.search(dish, enough=settings.RECIPE_CANDIDATES, fresh=args.refresh)
            except Exception as e:               # one bad dish doesn't stop the run
                logging.warning("%s: search failed: %s", dish, e)
            if got or attempt:
                break
            logging.info("%s: no recipe pages; retrying in %.0fs", dish, args.retry_wait)
            time.sleep(args.retry_wait)
        # Three dishes in a row with no recipe page at all: the engines are
        # suspended (SearXNG "too many requests"/CAPTCHA), and pressing on only
        # keeps them suspended — for the household's searches too. Stop.
        dry = 0 if got else dry + 1
        if dry >= 3:
            print(f"\nstopped at {dish!r}: 3 dishes in a row found no recipe pages, so the search "
                  "engines are probably throttled. Run it again later; finished dishes are skipped.")
            break
        count = book.put(dish, got, aliases)
        best = got[0] if got else None
        if count:
            kept += 1
            logging.info("[%d/%d] %-28s kept %d (%s)", n, len(dishes), dish, count, category)
        else:
            missed.append(dish)
            why = (f"best was {best.rating:.1f} from {best.rating_count}" if best else "no recipe pages")
            logging.info("[%d/%d] %-28s none popular enough: %s", n, len(dishes), dish, why)
        time.sleep(args.pause)

    print(f"\nadded or refreshed {kept} dishes; {len(missed)} without a popular recipe:")
    for d in missed:
        print(f"  {d}")
    return 0


def from_urls(args, book) -> int:
    """Fetch given pages per dish, slowly, and keep the popular recipes."""
    finder = RecipeFinder(settings.RECIPE_CACHE_DAYS, cookbook=book)
    aliases = {dish_key(d): a for d, a, _ in load_dishes()}
    last_hit = {}                                    # site -> time of our last request
    kept, missed = 0, []
    for dish, urls in json.loads(args.urls.read_text()).items():
        if book.has(dish) and not args.refresh:
            continue
        got = []
        for url in dict.fromkeys(urls):              # each page once, in the given order
            host = (urlparse(url).hostname or "").removeprefix("www.")
            wait = last_hit.get(host, 0) + args.site_gap - time.time()
            if wait > 0:
                time.sleep(wait)
            recipe = finder._fetch_one(url)
            last_hit[host] = time.time()
            if recipe:
                got.append(recipe)
        count = book.put(dish, rank(got, dish), aliases.get(dish_key(dish), []))
        if count:
            kept += 1
            logging.info("%-28s kept %d of %d pages", dish, count, len(urls))
        else:
            missed.append(dish)
            best = max(got, key=lambda r: (r.rating_count >= 50, r.rating), default=None)
            why = f"best was {best.rating:.1f} from {best.rating_count}" if best else "no recipe data on those pages"
            logging.info("%-28s none popular enough: %s", dish, why)
        time.sleep(args.pause)
    print(f"\nadded {kept} dishes; {len(missed)} without a popular recipe:")
    for d in missed:
        print(f"  {d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
