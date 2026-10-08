# Skill: Drive Times & Traffic

**Status:** Active
**Pipeline stage:** LLM bypass. Python resolves the place, routes the trip, and phrases the answer.

---

## What It Does

Answers "how long to get there" by car, always from home, and says when
traffic is worse than usual and why. It also gives a traffic report with no
destination ("how's traffic", "any wrecks on 610"). Nothing uses Google.

| Piece | Source | Key? |
|---|---|---|
| Home + favorites | `server/config/places.yaml` (git-ignored, chmod 600) | — |
| Finding places | Photon (`photon.komoot.io`, OpenStreetMap) | No |
| Route + ETA | TomTom Routing with live traffic, if `TOMTOM_API_KEY` is in `server/config/mcp.env`; otherwise OSRM (`router.project-osrm.org`, no traffic) | TomTom: free developer key |
| Live traffic | Houston TranStar: the JSON its public traffic map loads (`transtar.py`) | No |

TranStar's documented XML feeds (`/datafeed/getdatafeed.aspx`) return 403
until TranStar grants access. The map layers used here are public:
- freeway AVI speeds (`speed_segments.js`)
- City of Houston Bluetooth arterial speeds (`coh_bt_segments.js`)
- freeway incidents (`incidents_json.js`)
- HPD street crashes (`coh_hpd_incidents_json.js`)

All four refresh once a minute and are cached for 60 s.

## How Users Trigger It

- "How long to get to Lowe's on Ella?" / "How long to drive to HEB Bunker Hill?"
- "How long to Niko Niko's?" → "There are two Niko Niko's near you: one on
  Montrose Boulevard, and one on West Sam Houston Parkway North in CityCentre.
  Which one?" → "The Montrose one" / "the one by the beltway" / "the closer one"
- "How far is Mom's house?" / "What's my ETA to work?" / "How long is the drive to Galveston?"
- "How's traffic to work?" / "Is there any traffic getting to the airport?"
- "How's traffic?" / "Is there traffic on I-10?" / "Any wrecks on 610?"

## Picking the Place

`commute.Resolver.resolve()`:
1. **Favorites first.** The match ignores "my", "'s" and "house", so "my mom's
   house", "mom's" and "mom" all match a favorite named *Mom's house*, plus any
   `aliases`.
2. **Street address.** A destination starting with a number is geocoded.
3. **Name + qualifier.** The qualifier can follow "on", "in", "by" or "near",
   or just trail the name ("HEB Bunker Hill"). The name is searched within
   `search_radius_km` (35 km) of home. The qualifier picks a location by any
   of these:
   - its street, district or city text
   - a same-brand side listing on that street (the H-E-B's gas station is on
     Bunker Hill Rd, though the store's address is Katy Fwy)
   - the qualifier street or area lying within 1.2 km
4. **No qualifier.** The nearest location wins if the next one is at least 1.4×
   as far away. Otherwise Antigua asks, offering up to 3 options. The answer is
   taken without the wake word (`expects_reply`). If the answer isn't
   understood, Antigua re-asks once.
5. **Towns and cities** ("Galveston", "Katy") at any distance.

If nothing matches, the turn falls through to the LLM.

## Is the Traffic Unusual?

`commute.combine()`:
- **TomTom routes:** the ETA is TomTom's live time. "Unusual" comes from
  TranStar when it has live speeds for ≥30% of the route (it's the more
  trusted source). Otherwise it comes from TomTom's live time minus its
  historical time for this hour.
- **OSRM routes:** OSRM's time, plus TranStar's freeway delay over free flow,
  plus the street segments' time over usual.

A trip is unusual when it's at least `unusual_min_minutes` (5) **and**
`unusual_pct` (20%) slower than usual. The reply then names the slowest
TranStar segment and the worst incident on the route. A lane-blocking accident
is mentioned even when traffic is otherwise normal.

## Privacy

- `places.yaml` is git-ignored. Antigua warns in the log if the file isn't
  chmod 600. `deploy_fallback.sh` copies it to the backup box with mode 600,
  and `publish_public.sh` never sees it (`git archive`).
- Replies name favorites, never their addresses. The log names a favorite but
  never its coordinates. Nothing about favorites goes to the LLM.
- An address is geocoded once. Only its coordinates are cached, in
  `data/commute_places.json` (chmod 600), keyed by hash. Give `lat`/`lon` to
  skip the lookup.
- Home and favorites are sent to the routers rounded to ~100 m. Place-search
  bias uses a ~10 km-rounded point.
- Errors are logged by exception type only, because the request URLs carry
  search text and the TomTom key.
- Antigua's ports answer only over the WireGuard mesh, so LAN/IoT devices
  can't query it (`network/README.md`).

## Files

- `server/antigua_core/intents/commute.py`: `parse_commute_request()`
- `server/antigua_core/commute.py`: places, search, resolver, routing, replies
- `server/antigua_core/transtar.py`: TranStar feeds and route matching
- `server/config/places.example.yaml`: the template
- `tests/test_commute.py`: offline suite
