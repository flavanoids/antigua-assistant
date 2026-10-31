#!/usr/bin/env python3
"""Image search ranking for the display collage: re-ranking SearXNG's
round-robin rows, dropping what would look broken on a tile, cleaning
titles, and pinning local queries to the home city.

No network. The rows mimic SearXNG's image JSON (engine, title, url,
img_src, thumbnail_src, resolution, score).

Run: python3 tests/test_image_search.py   (also works under pytest)
"""

import sys
from pathlib import Path

import _isolated  # noqa: E402,F401  — temp data dir; must precede antigua_core
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from antigua_core import settings  # noqa: E402

settings.configure({})

from antigua_core import image_search  # noqa: E402
from antigua_core.image_search import clean_title, localize, rank  # noqa: E402


def row(engine, title, page, img, res="1200x800", score=1.0, thumb=None):
    return {"engine": engine, "title": title, "url": page, "img_src": img,
            "thumbnail_src": thumb or img + "?thumb", "resolution": res,
            "score": score, "content": ""}


# SearXNG's order: one row per engine, round-robin.
ROUND_ROBIN = [
    row("google cse images", "The Highline | Things To Do in New York, NY",
        "https://www.nycgo.com/attractions/the-high-line", "https://img.example/a1.jpg"),
    row("wikicommons.images", "File:Highland_Park_Michigan_1937.jpg",
        "https://commons.wikimedia.org/wiki/File:Highland.jpg",
        "https://upload.wikimedia.org/x/Highland_Park_Michigan_1937.jpg", "3072 x 2304"),
    row("duckduckgo images", "The High Line elevated park - Stock Photo - Alamy",
        "https://www.alamy.com/the-high-line-park", "https://c8.alamy.com/hl.jpg"),
    row("pinterest", "High Line Park walk",
        "https://example-blog.com/high-line-walk", "https://i.pinimg.com/originals/aa/bb/hl2.jpg",
        "683x1024"),
    row("google cse images", "High Line - Wikipedia",
        "https://en.wikipedia.org/wiki/High_Line", "https://upload.wikimedia.org/x/High_Line.jpg",
        "2272x1704", score=0.5),
    row("duckduckgo images", "The High Line 🌿 at sunset ...",
        "https://www.nycgo.com/high-line-sunset", "https://img.example/a2.jpg", score=0.5),
]


def test_ranks_relevance_over_engine_order():
    items = rank(ROUND_ROBIN, "the high line", count=6)
    titles = [i["title"] for i in items]
    assert titles[:2] == ["The Highline", "High Line"], titles
    # The off-topic Commons hit and the watermarked stock photo sink to the bottom.
    assert titles.index("Highland Park Michigan 1937") >= len(titles) - 2, titles
    assert "The High Line elevated park" not in titles[:3], titles
    assert [i["id"] for i in items] == list("ABCDEF")[:len(items)]


def test_items_carry_shape_and_cleaned_metadata():
    items = {i["title"]: i for i in rank(ROUND_ROBIN, "the high line")}
    wiki = items["High Line"]
    assert wiki["source"] == "Wikipedia"
    assert wiki["aspect_ratio"] == round(2272 / 1704, 3)
    assert items["High Line Park walk"]["aspect_ratio"] == round(683 / 1024, 3)
    assert items["The High Line at sunset"]["thumb_url"].endswith("?thumb")


def test_drops_tiny_banners_svgs_and_duplicates():
    rows = [
        row("google cse images", "High Line map", "https://a.example/map", "https://a.example/m.svg"),
        row("google cse images", "High Line icon", "https://a.example/i", "https://a.example/i.png", "120x120"),
        row("google cse images", "High Line banner", "https://a.example/b", "https://a.example/b.jpg", "3000x400"),
        row("pinterest", "High Line", "https://b.example/1", "https://i.pinimg.com/originals/aa/bb/same.jpg"),
        row("pinterest", "High Line again", "https://c.example/2", "https://i.pinimg.com/736x/aa/bb/same.jpg"),
        row("google cse images", "High Line", "https://d.example/ok", "https://d.example/ok.jpg"),
    ]
    titles = [i["title"] for i in rank(rows, "high line")]
    assert "High Line map" not in titles and "High Line icon" not in titles
    assert "High Line banner" not in titles
    assert sum(t.startswith("High Line") for t in titles) == 2, titles


def test_caps_two_per_site():
    rows = [row("google cse images", f"High Line view {n}", f"https://one.example/{n}",
                f"https://one.example/{n}.jpg") for n in range(5)]
    rows.append(row("pinterest", "High Line other", "https://two.example/x", "https://two.example/x.jpg"))
    sources = [i["source"] for i in rank(rows, "high line")]
    assert sources.count("one.example") == 2, sources
    assert "two.example" in sources


def test_clean_title():
    cases = [
        ("Loews Theatres - Union Square Mall : r/nyc", "reddit.com", "Loews Theatres - Union Square Mall"),
        ("Fuji Apple vs Honeycrisp - Eat Like No One Else", "eatlikenoone.com", "Fuji Apple vs Honeycrisp"),
        ("Difference between: Honeycrisp and Fuji - Example.com", "example.com",
         "Difference between: Honeycrisp and Fuji"),
        ("The Met - All You SHOULD Know Before Going (2026 Reviews)", "tripadvisor.com", "The Met"),
        ("Sneak peek inside: The Post nears completion (PHOTOS ...", "news.example", "Sneak peek inside: The Post nears completion"),
        ("Honeycrisp vs Gala: Which Apple? 🍎 | TikTok", "tiktok.com", "Honeycrisp vs Gala: Which Apple?"),
        ("File:Brooklyn_Bridge_1937.jpg", "commons.wikimedia.org", "Brooklyn Bridge 1937"),
        ("Spider-Man - Marvel", "marvel.com", "Spider-Man"),
        ("Union Square Mall History - Historic NYC - HAIF ➰ The ...", "nycarch.example",
         "Union Square Mall History - Historic NYC - HAIF"),
        ("", "x.example", "fallback"),
    ]
    for raw, host, want in cases:
        got = clean_title(raw, "fallback", host)
        assert got == want, (raw, got, want)
    assert len(clean_title("word " * 40)) <= 80


def test_localize_pins_local_queries_to_home_city():
    settings.SEARCH_HOME_CITY = "New York"
    try:
        assert localize("the post in downtown") == "the post in downtown New York"
        assert localize("coffee shops near me") == "coffee shops in New York"
        assert localize("downtown New York skyline") == "downtown New York skyline"
        assert localize("fuji apples vs honeycrisp") == "fuji apples vs honeycrisp"
        settings.SEARCH_HOME_CITY = ""
        assert localize("the post in downtown") == "the post in downtown"
    finally:
        settings.SEARCH_HOME_CITY = ""


def test_home_city_breaks_ties():
    settings.SEARCH_HOME_CITY = "New York"
    try:
        rows = [
            row("google cse images", "Union Station mall in the 90s", "https://far.example/x",
                "https://far.example/x.jpg"),
            row("google cse images", "Union Station mall in the 90s, New York", "https://near.example/y",
                "https://near.example/y.jpg"),
        ]
        assert rank(rows, "union station mall in the 90's")[0]["source"] == "near.example"
    finally:
        settings.SEARCH_HOME_CITY = ""


def test_source_names():
    assert image_search.source_name("en.wikipedia.org") == "Wikipedia"
    assert image_search.source_name("i.pinimg.com") == "Pinterest"
    assert image_search.source_name("nycgo.com") == "nycgo.com"


def test_split_comparison():
    split = image_search.split_comparison
    assert split("fuji apples vs honeycrisp apples") == ("fuji apples", "honeycrisp apples")
    assert split("fuji vs. honeycrisp apples") == ("fuji apples", "honeycrisp apples")
    assert split("cats versus dogs") == ("cats", "dogs")
    assert split("the met compared to the moma") == ("the met", "the moma")
    assert split("the high line") is None
    assert split("black or white cats") is None
    assert split("vs dogs") is None
    assert image_search.label("fuji apples") == "Fuji apples"


def test_rank_letters_from_an_offset_and_skips_excluded():
    rows = [row("google cse images", f"High Line {n}", f"https://s{n}.example/p",
                f"https://img.example/{n}.jpg") for n in range(4)]
    first = rank(rows, "high line", count=2)
    seen = {image_search.image_key(i["url"]) for i in first}
    second = rank(rows, "high line", count=3, first=2, exclude=seen)
    assert [i["id"] for i in second] == ["C", "D"]
    assert not {i["url"] for i in first} & {i["url"] for i in second}


def run():
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"[ok] {name}")
    print("PASS — image search ranking")


if __name__ == "__main__":
    run()
