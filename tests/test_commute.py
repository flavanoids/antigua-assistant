#!/usr/bin/env python3
"""Commute skill: parsing, picking the right store, "which one?", TranStar
route matching, and the spoken replies. No network: Photon, the router and
TranStar are fakes fed from canned data.

Run: python3 tests/test_commute.py   (also works under pytest)
"""

import logging
import os
import sys
import tempfile
from pathlib import Path

import _isolated  # noqa: E402,F401  — temp data dir; must precede antigua_core
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from antigua_core import settings  # noqa: E402

_tmp = Path(tempfile.mkdtemp())
PLACES = _tmp / "places.yaml"
PLACES.write_text("""
home: {lat: 39.83, lon: -105.47}
favorites:
  - name: Mom's house
    aliases: [mom, mom and dad's]
    lat: 39.81234
    lon: -105.41678
  - name: work
    aliases: [the office]
    lat: 39.76
    lon: -105.37
""")
os.chmod(PLACES, 0o600)
settings.configure({"commute": {"places_file": str(PLACES)}, "search": {"home_city": "Houston"}})

from antigua_core import commute as C  # noqa: E402
from antigua_core import transtar as T  # noqa: E402
from antigua_core.intents.commute import parse_commute_request as parse  # noqa: E402
from antigua_core.tts_text import clean_for_tts  # noqa: E402

FAILS = []


def check(name, got, want):
    if got != want:
        FAILS.append(f"{name}: got {got!r}, want {want!r}")


# ── Parsing ──────────────────────────────────────────────────────────────────

PARSE = {
    "How long to get to Lowe's on Ella": ("Lowe's on Ella", "eta"),
    "how long to drive to HEB Bunker Hill": ("HEB Bunker Hill", "eta"),
    "How long to Niko Niko's?": ("Niko Niko's", "eta"),
    "how long does it take to get to mom's house": ("mom's house", "eta"),
    "how long will it take me to get over to the Galleria right now": ("the Galleria", "eta"),
    "how long of a drive is it to Galveston": ("Galveston", "eta"),
    "how long is the drive to work": ("work", "eta"),
    "what's my ETA to the office": ("the office", "eta"),
    "what's the drive time to Costco": ("Costco", "eta"),
    "how far is Niko Niko's from here": ("Niko Niko's", "distance"),
    "how far away is mom's": ("mom's", "distance"),
    "how far is it to Katy": ("Katy", "distance"),
    "how's traffic to work": ("work", "traffic"),
    "what's the traffic like on the way to the airport": ("the airport", "traffic"),
    "is there any traffic getting to H-E-B": ("H-E-B", "traffic"),
    "What's traffic look like going to Memorial City Mall": ("Memorial City Mall", "traffic"),
    "Is traffic bad heading the The Galleria right now?": ("the Galleria", "traffic"),
    "is it bad going to the Galleria": ("the Galleria", "traffic"),
    "How long to The POST": ("The POST", "eta"),
}
NOT_TRIPS = [
    "how long to boil an egg", "how long to bake chicken thighs", "how long to cook rice",
    "how long is left on the pasta timer", "how long until my gym alarm",
    "how long till christmas", "how far is the moon", "how long to get rid of a cold",
    "how far is it from Houston to Dallas", "how long would it take to walk to the park",
    "how long to charge my phone", "what's the weather", "how long on the second one",
]

PARSE.update({
    "how's traffic": ("", "area"),
    "how bad is traffic right now": ("", "area"),
    "is there traffic on I-10 right now": ("I-10", "area"),
    "any wrecks on 610": ("610", "area"),
    "how's the traffic on 290 this morning": ("290", "area"),
    "what's traffic looking like": ("", "area"),
    "is traffic bad on 290": ("290", "area"),
})
for q, want in PARSE.items():
    r = parse(q)
    check(f"parse {q!r}", (r.dest, r.kind) if r else None, want)
for q in NOT_TRIPS:
    check(f"not a trip {q!r}", parse(q), None)


# ── Picking the place ────────────────────────────────────────────────────────

def P(name, lat, lon, street="", district="", value="", key="shop", city="Houston"):
    return C.Place(name, lat, lon, kind="poi" if key != "place" else "place", street=street,
                   district=district, city=city, osm_value=value)


NIKO = [P("Niko Niko's", 39.7619, -105.3925, "Montrose Boulevard", "Montrose", "restaurant"),
        P("Niko Niko's", 39.7840, -105.5600, "West Sam Houston Parkway North", "CityCentre", "restaurant"),
        P("Niko Niko's", 40.1600, -105.4600, "Lake Front Circle", "Town Center", "restaurant")]
LOWES = [P("Lowe's", 39.8103, -105.4279, "North Loop West", "Houston Heights"),
         P("Lowe's", 39.7820, -105.5300, "Katy Freeway", "Memorial City"),
         P("Lowe's", 39.6900, -105.4700, "Beechnut Street", "Braeswood")]
HEB = [P("H-E-B", 39.8090, -105.4100, "North Shepherd Drive", "Houston Heights", "supermarket"),
       P("H-E-B", 39.7840, -105.5310, "Katy Freeway", "Memorial City", "supermarket"),
       P("H-E-B", 39.8150, -105.5150, "Kempwood Drive", "Spring Branch North", "supermarket"),
       P("H-E-B Fuel", 39.7855, -105.5323, "Bunker Hill Road", "Memorial City", "fuel", "amenity"),
       P("H-E-B Pharmacy", 39.8090, -105.4100, "North Shepherd Drive", "Houston Heights", "pharmacy", "amenity")]
STREETS = {"ella": [P("Ella Boulevard", 39.8064, -105.4293, key="highway"),
                    P("Ella Boulevard", 39.8279, -105.4296, key="highway")]}
def L(name, lat, lon, value, street="", district="", key="amenity"):
    p = P(name, lat, lon, street, district, value, key)
    if key == "place":
        p.kind = "place"
    return p


MORE = {
    "post": [L("The Post", 39.79, -105.36, "biergarten", "North Main Street", "Houston Heights")],
    "posthouston": [L("POST Houston", 39.766, -105.364, "arts_centre", "Franklin Street", "Downtown")],
    "hobbyairport": [L("Red Carpet Inn Hobby Airport", 39.70, -105.35, "motel", "Brays Bayou Greenway"),
                     L("William P. Hobby Airport", 39.65, -105.28, "aerodrome", "Monroe Road")],
    "hobbyairporthouston": [],
    "minutemaidpark": [L("Daikin Park", 39.757, -105.355, "stadium", "Crawford Street", "Downtown")],
    "minutemaidparkhouston": [],
    "medicalcenter": [L("Medical Center RV Resort", 39.69, -105.40, "caravan_site"),
                      L("Courtyard Houston Medical Center", 39.70, -105.40, "hotel", "Main Street")],
    "medicalcenterhouston": [L("Texas Medical Center", 39.707, -105.398, "quarter", key="place")],
    "galleria": [], "galleriahouston": [L("The Galleria", 39.738, -105.464, "mall", "Westheimer Road", "Uptown"),
                                        L("The Houston Galleria", 39.7381, -105.4632, "mall", "Westheimer Road")],
    "cheesecakefactory": [L("The Cheesecake Factory", 39.74, -105.46, "restaurant", "Westheimer Road", "Uptown"),
                          L("The Cheesecake Factory", 39.78, -105.54, "restaurant", "Memorial City Way")],
}


for lst in STREETS.values():
    for s in lst:
        s.kind = "other"


class FakePhoton:
    def __init__(self):
        self.queries = []

    def search(self, q, near, *, local=True, limit=15):
        self.queries.append(q)
        n = C._norm(q)
        if n == "nikonikos":
            return NIKO
        if n == "lowes":
            return LOWES
        if n == "heb":
            return HEB
        if n == "hebbunkerhill":
            return [HEB[3]]
        if n == "galveston":
            return [P("Galveston", 39.30, -104.80, key="place")] if not local else [
                P("Galveston Bay/Harbor", 39.70, -105.30, value="harbour")]
        return MORE.get(n, []) if n in MORE else STREETS.get(n, [])

    def geocode(self, address, near=None):
        return None


places = C.Places(PLACES)
resolver = C.Resolver(FakePhoton(), places)


def resolved(dest):
    status, val = resolver.resolve(dest)
    if status == "found":
        return status, val.label
    if status == "ask":
        return status, [p.street for p in val[1]]
    return status, val


check("Niko asks", resolved("Niko Niko's"),
      ("ask", ["West Sam Houston Parkway North", "Montrose Boulevard"]))
check("Niko in Montrose", resolved("Niko Niko's in Montrose"),
      ("found", "the Niko Niko's on Montrose Boulevard"))
check("Niko Montrose (no 'in')", resolved("Niko Niko's Montrose"),
      ("found", "the Niko Niko's on Montrose Boulevard"))
check("Lowe's nearest", resolved("Lowe's"), ("found", "the Lowe's on North Loop West in Houston Heights"))
check("Lowe's on Ella", resolved("Lowe's on Ella"), ("found", "the Lowe's on North Loop West in Houston Heights"))
check("HEB Bunker Hill", resolved("HEB Bunker Hill"), ("found", "the H-E-B on Katy Freeway in Memorial City"))
check("H-E-B on Kempwood", resolved("the H-E-B on Kempwood"),
      ("found", "the H-E-B on Kempwood Drive in Spring Branch North"))
check("favorite", resolved("my mom's house"), ("found", "Mom's house"))
check("favorite alias", resolved("mom and dad's"), ("found", "Mom's house"))
check("favorite bare", resolved("Mom"), ("found", "Mom's house"))
check("work alias", resolved("the office"), ("found", "work"))
check("home", resolved("home"), ("home", None))
check("city", resolved("Galveston"), ("found", "Galveston"))
check("nothing", resolved("Zzyzx Bakery"), ("none", None))
check("same name, different places: ask", resolved("The POST"), ("ask", ["North Main Street", "Franklin Street"]))
check("airport by its short name", resolved("Hobby Airport"), ("found", "William P. Hobby Airport"))
check("renamed landmark", resolved("Minute Maid Park"), ("found", "Daikin Park"))
check("district over RV resort", resolved("the medical center"), ("found", "Texas Medical Center"))
check("one mall, two names", resolved("the Galleria"), ("found", "the Galleria"))
check("town over a harbor", resolved("Galveston"), ("found", "Galveston"))
check("The-names", resolved("the Cheesecake Factory"), ("ask", ["Memorial City Way", "Westheimer Road"]))
_, (_, cf) = resolver.resolve("the Cheesecake Factory")
check("The-names ask", C.ask_text("The Cheesecake Factory", cf).split(":")[0],
      "There are two Cheesecake Factories near you")
check("The-names label", cf[1].label, "the Cheesecake Factory on Westheimer Road in Uptown")
_, (_, post) = resolver.resolve("The POST")
check("different-names ask", C.ask_text(None, post),
      "Do you mean The Post on North Main Street in Houston Heights or POST Houston on Franklin Street in Downtown?")
check("answer by kind", C.pick_answer("the biergarten", post, resolver.aliases), 0)
check("answer by name", C.pick_answer("POST Houston", post, resolver.aliases), 1)
status, tgt = resolver.resolve("Lowe's on Beechnut")
check("qualifier hit", tgt.label, "the Lowe's on Beechnut Street in Braeswood")
status, tgt = resolver.resolve("Lowe's on Zzyzx")
check("qualifier miss: closest + note", (tgt.label, tgt.note),
      ("the Lowe's on North Loop West in Houston Heights",
       "I couldn't find a Lowe's on Zzyzx, so here's the closest one."))
check("Lowe's on Ella resolves without asking Photon about Boulevard",
      any("boulevard" in q.lower() for q in resolver.photon.queries), False)

_, (_, niko_opts) = resolver.resolve("Niko Niko's")
A = resolver.aliases
for answer, want in {
    "the Montrose one": 1, "Montrose": 1, "the one by the beltway": 0,
    "the one near BW8 and I-10": 0, "CityCentre": 0, "city center": 0,
    "the closer one": 0, "the second one": 1, "never mind": "cancel",
    "the one on I-10": None, "what's the weather like tomorrow in Austin Texas please": None,
}.items():
    check(f"answer {answer!r}", C.pick_answer(answer, niko_opts, A), want)

check("ask text", C.ask_text("Niko Niko's", niko_opts),
      "There are two Niko Niko's near you: one on West Sam Houston Parkway North in CityCentre, "
      "and one on Montrose Boulevard. Which one?")
_, (_, heb_opts) = resolver.resolve("H-E-B")
check("ask among close ones only", C.ask_text("H-E-B", heb_opts),
      "There are two H-E-Bs near you: one on Kempwood Drive in Spring Branch North, and one on "
      "North Shepherd Drive in Houston Heights. Which one?")
check("ask three", C.ask_text("H-E-B", HEB[:3]).startswith("There are a few H-E-Bs near you: on "), True)


# ── TranStar ─────────────────────────────────────────────────────────────────

# A route due east along lat 39.78 from -105.50 to -105.40 (~8.5 km). Coordinates
# throughout are Houston's shifted +10/-10 degrees, so no real place is in here.
ROUTE = [(39.78, -105.50 + i * 0.001) for i in range(101)]
FREEWAY_JS = """
SpeedSegments[0] = new SpeedSegment("39.7801 39.7801","-105.49 -105.45","0.004 0.004","0.001 0.001","EB",22,"8 minutes 0 seconds","2.40","ML","1","2","IH-10 Katy Eastbound from Antoine to Silber","150.00","300");
SpeedSegments[1] = new SpeedSegment("39.7798 39.7798","-105.45 -105.49","0.004 0.004","0.001 0.001","WB",20,"9 minutes 0 seconds","2.40","ML","2","1","IH-10 Katy Westbound from Silber to Antoine","80.00","400");
SpeedSegments[2] = new SpeedSegment("39.7801 39.7801","-105.45 -105.41","0.004 0.004","0.001 0.001","EB",-1,"Not Available","2.40","ML","2","3","IH-10 Katy Eastbound from Silber to TC Jester","-100.50","-218");
SpeedSegments[3] = new SpeedSegment("39.7801 39.7801","-105.45 -105.41","0.004 0.004","0.001 0.001","EB",50,"3 minutes 0 seconds","2.40","ML","9","8","IH-10 Katy Managed Lanes Eastbound from Silber to TC Jester","0","20");
SpeedSegments[4] = new SpeedSegment("39.90 39.90","-105.49 -105.45","0.004 0.004","0.001 0.001","EB",60,"2 minutes 0 seconds","2.40","ML","5","6","US-290 Eastbound far away","90.00","100");
"""
STREET_JS = ('btSegs[0]=new btSeg("39.78 39.78","-105.44 -105.42",".0023 .0023",".001 .001","EB","15","300",'
             '"11th from Shepherd to Yale","ML","10/2/2026 6:18:45 PM","1.2","#FF0000","n","50");'
             'btSegs[1]=new btSeg("39.78 39.78","-105.44 -105.42",".0023 .0023",".001 .001","EB","30","100",'
             '"11th from Shepherd to Yale (hist)","ML","10/2/2026 6:18:45 PM","1.2","#FF0000","y","50");')
segs = T.parse_freeway(FREEWAY_JS) + T.parse_streets(STREET_JS)
check("freeway parse skips N/A + managed lanes", [s.name for s in segs if s.kind == "freeway"],
      ["IH-10 Katy Eastbound from Antoine to Silber", "IH-10 Katy Westbound from Silber to Antoine",
       "US-290 Eastbound far away"])
check("street parse skips historical fill", len([s for s in segs if s.kind == "street"]), 1)
check("travel time", segs[0].travel_s, 480)
check("excess vs usual", round(segs[0].excess_s), 288)   # 480 s at 150% over usual -> 192 usual

incs = T.parse_incidents('{"incidents": ['
                         '{"id":"1","location":"IH-10 Katy Eastbound At Silber Rd","dir":"Eastbound",'
                         '"desc":"Accident","lanes":"Left Lane","status":"Verified","lat":"39.7802","lng":"-105.452"},'
                         '{"id":"2","location":"IH-10 Katy Westbound At Silber Rd","dir":"Westbound",'
                         '"desc":"Stall","lanes":"Right Shoulder","status":"Verified","lat":"39.7797","lng":"-105.452"},'
                         '{"id":"3","location":"IH-10 Katy Eastbound At Antoine","dir":"Eastbound",'
                         '"desc":"Accident","lanes":"","status":"Cleared","lat":"39.78","lng":"-105.49"}]}')
check("cleared incidents dropped", len(incs), 2)
rt = T.match_route(ROUTE, segs, incs)
check("on-route segments", sorted(s.name for s, _ in rt.segments),
      ["11th from Shepherd to Yale", "IH-10 Katy Eastbound from Antoine to Silber"])
check("incident direction", [i.location for i in rt.incidents], ["IH-10 Katy Eastbound At Silber Rd"])
check("spoken incident", T.spoken_incident(rt.incidents[0]),
      "an accident on I-10 Katy eastbound at Silber Road, blocking lanes")
check("spoken 610", T.spoken_road("IH-610 West Loop Southbound At N Post Oak Rd"),
      "the 610 West Loop Southbound at North Post Oak Road")

from datetime import datetime  # noqa: E402
hpd = T.parse_hpd('{"incidents": [{"location":"4759 W FUQUA ST @ 14699 BUXLEY ST","desc":"Major Accident",'
                  '"time":"Today at 6:13 PM","lat":"39.6","lng":"-105.4","display":"True"},'
                  '{"location":"1 MAIN ST","desc":"Minor Accident","time":"Today at 3:00 PM",'
                  '"lat":"39.6","lng":"-105.4","display":"True"}]}', now=datetime(2026, 10, 2, 18, 30))
check("hpd keeps recent only", len(hpd), 1)
check("hpd spoken", T.spoken_incident(hpd[0]), "a major accident on West Fuqua Street at Buxley Street")


# ── Trips and replies ────────────────────────────────────────────────────────

def trip_for(source, seconds, usual=None, traffic=None, meters=9600):
    return C.combine(C.Route(seconds, meters, ROUTE, usual, source), traffic)


normal = trip_for("tomtom", 900, 880, T.RouteTraffic([], [], 0))
check("tomtom normal", C.phrase_trip(normal, "the Lowe's on North Loop West", "eta"),
      "It's about 15 minutes to the Lowe's on North Loop West. Traffic looks normal.")
slow = trip_for("tomtom", 1500, 1400, rt)    # TranStar covers the route: its excess wins
check("transtar trusted", round(slow.excess_s), round(rt.excess_s))
check("unusual phrasing", C.phrase_trip(slow, "work", "eta"),
      "It's about 25 minutes to work. That's about 6 minutes more than usual. Traffic's slow on "
      "I-10 Katy eastbound from Antoine to Silber and there's an accident on I-10 Katy eastbound "
      "at Silber Road, blocking lanes.")
tt_only = trip_for("tomtom", 2400, 1500, T.RouteTraffic([], [], 0))
check("tomtom excess without transtar", round(tt_only.excess_s), 900)
check("traffic kind", C.phrase_trip(tt_only, "the airport", "traffic").split(". ")[0],
      "Traffic to the airport looks heavier than usual")
osrm = trip_for("osrm", 600, None, None)
check("osrm no traffic", C.phrase_trip(osrm, "Mom's house", "distance"),
      "Mom's house is about 6 miles away, about 10 minutes by car. That's without live traffic.")
inc_only = trip_for("osrm", 600, None, T.RouteTraffic([], rt.incidents, 0))
check("incident without speeds", C.phrase_trip(inc_only, "work", "eta"),
      "It's about 10 minutes to work. I don't have live speeds for that route, but there's an "
      "accident on I-10 Katy eastbound at Silber Road, blocking lanes.")
osrm_ts = trip_for("osrm", 600, None, rt)
check("osrm + transtar delay", round(osrm_ts.seconds), round(600 + rt.freeway_delay_s
                                                            + sum(s.excess_s * c for s, c in rt.segments
                                                                  if s.kind == "street")))
check("minutes", [C.spoken_minutes(s) for s in (20, 61, 3600, 3720, 4500, 7500)],
      ["a minute", "a minute", "an hour", "an hour", "an hour and 15 minutes", "2 hours and 5 minutes"])

for text in (C.phrase_trip(slow, "work", "eta"), C.ask_text("Niko Niko's", niko_opts)):
    check(f"tts-clean {text[:30]!r}", bool(clean_for_tts(text).strip()), True)


# ── "How's traffic" ──────────────────────────────────────────────────────────

class FakeTranStar:
    def __init__(self, segs, incs):
        self.data = (segs, incs)

    def fetch_all(self):
        return self.data

    def along(self, pts):
        return T.match_route(pts, *self.data)


for road, want in {
    "": "I-10 Katy eastbound from Antoine to Silber is running about 5 minutes slower than usual "
        "and there's an accident on I-10 Katy eastbound at Silber Road, blocking lanes.",
    "I-10": "I-10 Katy eastbound from Antoine to Silber is running about 5 minutes slower than usual "
            "and there's an accident on I-10 Katy eastbound at Silber Road, blocking lanes.",
    "the beltway": "I don't see the beltway in the traffic data.",
    "11th": "Traffic on 11th looks normal.",   # 100 s over usual: under the 2-minute bar
}.items():
    pv = C.CommuteProvider(places=C.Places(PLACES), photon=FakePhoton(), router=object(),
                           transtar=FakeTranStar(segs, incs))
    pv.places._home = C.Place("home", 39.79, -105.46, kind="home")
    pv.places._reload = lambda: None
    check(f"area {road!r}", pv.area_report(road).text, want)
check("road pattern beltway", bool(C.road_pattern("the beltway").search("West Sam Houston Tollway Southbound")), True)
check("road pattern number", bool(C.road_pattern("interstate 10").search("IH-10 Katy Eastbound")), True)
check("road pattern no partial number", bool(C.road_pattern("I-10").search("IH-610 West Loop")), False)


# ── Privacy ──────────────────────────────────────────────────────────────────

class FakeRouter:
    def __init__(self):
        self.sent = []

    def route(self, a, b):
        self.sent.append((C._send_point(a), C._send_point(b)))
        return C.Route(700, 5000, ROUTE, 690, "tomtom")


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


cap = Capture()
logging.getLogger("antigua_core").addHandler(cap)
logging.getLogger("antigua_core").setLevel(logging.INFO)
router = FakeRouter()
provider = C.CommuteProvider(places=places, photon=FakePhoton(), router=router,
                             transtar=type("NoTraffic", (), {"along": lambda self, pts: None})())
reply = provider.answer(parse("how long to get to mom's house"))
check("favorite reply names only", reply.text, "It's about 12 minutes to Mom's house. Traffic looks normal.")
check("favorite sent rounded", router.sent[-1][1], (39.812, -105.417))
check("home sent rounded", router.sent[-1][0], (39.83, -105.47))
check("no coordinates logged", [ln for ln in cap.lines if "39.81" in ln or "95.41" in ln], [])

ask = provider.answer(parse("how long to Niko Niko's"))
check("ask pending", bool(ask.pending), True)
check("choose", provider.choose(ask.pending, "the Montrose one").text.split(".")[0],
      "It's about 12 minutes to the Niko Niko's on Montrose Boulevard")
again = provider.choose(ask.pending, "the one on I-10")
check("re-ask once", (again.text.startswith("Sorry, which one? The one on"), bool(again.pending)), (True, True))
check("then give up", provider.choose(again.pending, "the one on I-10"), None)
check("cancel", provider.choose(ask.pending, "never mind").text, "Okay.")


if FAILS:
    print(f"FAIL ({len(FAILS)}):")
    for f in FAILS:
        print("  " + f)
    sys.exit(1)
print("ok: commute")
