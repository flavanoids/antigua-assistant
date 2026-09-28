"""Static team directory for the sports skill: every NFL / MLB / NBA / MLS
team, keyed by (league, team_id), with the spoken aliases resolve_team() in
sports.py matches against.

`team_id` is always a string — it's a URL path segment, never arithmetic.
MLB/NFL/NBA use ESPN's lowercase abbreviation ("hou"); MLS uses ESPN's
numeric team id as a string ("6077") since MLS abbreviations aren't unique
path segments on ESPN's API the way the other three leagues' are.

Regenerate the base table with `server/scripts/build_sports_teams.py` (hits
ESPN's live /teams endpoint per league) if a franchise relocates, renames, or
MLS expands — then hand-merge any colloquial EXTRA_ALIASES additions below
back in; don't hand-edit `team_id`/`league`/`display` from memory.

Two nicknames are shared by two leagues in this table: "Giants" (NFL New York
Giants vs MLB San Francisco Giants) and "Cardinals" (NFL Arizona Cardinals vs
MLB St. Louis Cardinals). COLLISION_DEFAULTS below picks a fixed answer when
the transcript doesn't name a sport — see sports.py::resolve_team().
"""

from __future__ import annotations

TEAMS: dict[tuple[str, str], dict] = {
    # -- MLB (30 teams) --------------------------------------------------
    ("mlb", "ari"): {"display": "Arizona Diamondbacks", "aliases": ["arizona diamondbacks", "d-backs", "dbacks", "diamondbacks"]},
    ("mlb", "ath"): {"display": "Athletics", "aliases": ["a's", "as", "athletics", "oakland athletics"]},
    ("mlb", "atl"): {"display": "Atlanta Braves", "aliases": ["atlanta braves", "braves"]},
    ("mlb", "bal"): {"display": "Baltimore Orioles", "aliases": ["baltimore orioles", "orioles"]},
    ("mlb", "bos"): {"display": "Boston Red Sox", "aliases": ["boston red sox", "red sox"]},
    ("mlb", "chc"): {"display": "Chicago Cubs", "aliases": ["chicago cubs", "cubs"]},
    ("mlb", "chw"): {"display": "Chicago White Sox", "aliases": ["chicago white sox", "white sox"]},
    ("mlb", "cin"): {"display": "Cincinnati Reds", "aliases": ["cincinnati reds", "reds"]},
    ("mlb", "cle"): {"display": "Cleveland Guardians", "aliases": ["cleveland guardians", "guardians"]},
    ("mlb", "col"): {"display": "Colorado Rockies", "aliases": ["colorado rockies", "rockies"]},
    ("mlb", "det"): {"display": "Detroit Tigers", "aliases": ["detroit tigers", "tigers"]},
    ("mlb", "hou"): {"display": "Houston Astros", "aliases": ["astros", "houston astros"]},
    ("mlb", "kc"): {"display": "Kansas City Royals", "aliases": ["kansas city royals", "royals"]},
    ("mlb", "laa"): {"display": "Los Angeles Angels", "aliases": ["angels", "los angeles angels"]},
    ("mlb", "lad"): {"display": "Los Angeles Dodgers", "aliases": ["dodgers", "los angeles dodgers"]},
    ("mlb", "mia"): {"display": "Miami Marlins", "aliases": ["marlins", "miami marlins"]},
    ("mlb", "mil"): {"display": "Milwaukee Brewers", "aliases": ["brewers", "milwaukee brewers"]},
    ("mlb", "min"): {"display": "Minnesota Twins", "aliases": ["minnesota twins", "twins"]},
    ("mlb", "nym"): {"display": "New York Mets", "aliases": ["mets", "new york mets"]},
    ("mlb", "nyy"): {"display": "New York Yankees", "aliases": ["new york yankees", "yankees"]},
    ("mlb", "phi"): {"display": "Philadelphia Phillies", "aliases": ["philadelphia phillies", "phillies"]},
    ("mlb", "pit"): {"display": "Pittsburgh Pirates", "aliases": ["pirates", "pittsburgh pirates"]},
    ("mlb", "sd"): {"display": "San Diego Padres", "aliases": ["padres", "san diego padres"]},
    ("mlb", "sf"): {"display": "San Francisco Giants", "aliases": ["giants", "san francisco giants"]},
    ("mlb", "sea"): {"display": "Seattle Mariners", "aliases": ["mariners", "seattle mariners"]},
    ("mlb", "stl"): {"display": "St. Louis Cardinals", "aliases": ["cardinals", "st. louis cardinals"]},
    ("mlb", "tb"): {"display": "Tampa Bay Rays", "aliases": ["rays", "tampa bay rays"]},
    ("mlb", "tex"): {"display": "Texas Rangers", "aliases": ["rangers", "texas rangers"]},
    ("mlb", "tor"): {"display": "Toronto Blue Jays", "aliases": ["blue jays", "toronto blue jays"]},
    ("mlb", "wsh"): {"display": "Washington Nationals", "aliases": ["nationals", "washington nationals"]},
    # -- NFL (32 teams) --------------------------------------------------
    ("nfl", "ari"): {"display": "Arizona Cardinals", "aliases": ["arizona cardinals", "cardinals"]},
    ("nfl", "atl"): {"display": "Atlanta Falcons", "aliases": ["atlanta falcons", "falcons"]},
    ("nfl", "bal"): {"display": "Baltimore Ravens", "aliases": ["baltimore ravens", "ravens"]},
    ("nfl", "buf"): {"display": "Buffalo Bills", "aliases": ["bills", "buffalo bills"]},
    ("nfl", "car"): {"display": "Carolina Panthers", "aliases": ["carolina panthers", "panthers"]},
    ("nfl", "chi"): {"display": "Chicago Bears", "aliases": ["bears", "chicago bears"]},
    ("nfl", "cin"): {"display": "Cincinnati Bengals", "aliases": ["bengals", "cincinnati bengals"]},
    ("nfl", "cle"): {"display": "Cleveland Browns", "aliases": ["browns", "cleveland browns"]},
    ("nfl", "dal"): {"display": "Dallas Cowboys", "aliases": ["cowboys", "dallas cowboys"]},
    ("nfl", "den"): {"display": "Denver Broncos", "aliases": ["broncos", "denver broncos"]},
    ("nfl", "det"): {"display": "Detroit Lions", "aliases": ["detroit lions", "lions"]},
    ("nfl", "gb"): {"display": "Green Bay Packers", "aliases": ["green bay packers", "packers"]},
    ("nfl", "hou"): {"display": "Houston Texans", "aliases": ["houston texans", "texans"]},
    ("nfl", "ind"): {"display": "Indianapolis Colts", "aliases": ["colts", "indianapolis colts"]},
    ("nfl", "jax"): {"display": "Jacksonville Jaguars", "aliases": ["jacksonville jaguars", "jaguars"]},
    ("nfl", "kc"): {"display": "Kansas City Chiefs", "aliases": ["chiefs", "kansas city chiefs"]},
    ("nfl", "lv"): {"display": "Las Vegas Raiders", "aliases": ["las vegas raiders", "raiders"]},
    ("nfl", "lac"): {"display": "Los Angeles Chargers", "aliases": ["chargers", "los angeles chargers"]},
    ("nfl", "lar"): {"display": "Los Angeles Rams", "aliases": ["los angeles rams", "rams"]},
    ("nfl", "mia"): {"display": "Miami Dolphins", "aliases": ["dolphins", "miami dolphins"]},
    ("nfl", "min"): {"display": "Minnesota Vikings", "aliases": ["minnesota vikings", "vikings"]},
    ("nfl", "ne"): {"display": "New England Patriots", "aliases": ["new england patriots", "patriots"]},
    ("nfl", "no"): {"display": "New Orleans Saints", "aliases": ["new orleans saints", "saints"]},
    ("nfl", "nyg"): {"display": "New York Giants", "aliases": ["giants", "new york giants"]},
    ("nfl", "nyj"): {"display": "New York Jets", "aliases": ["jets", "new york jets"]},
    ("nfl", "phi"): {"display": "Philadelphia Eagles", "aliases": ["eagles", "philadelphia eagles"]},
    ("nfl", "pit"): {"display": "Pittsburgh Steelers", "aliases": ["pittsburgh steelers", "steelers"]},
    ("nfl", "sf"): {"display": "San Francisco 49ers", "aliases": ["49ers", "niners", "san francisco 49ers"]},
    ("nfl", "sea"): {"display": "Seattle Seahawks", "aliases": ["seahawks", "seattle seahawks"]},
    ("nfl", "tb"): {"display": "Tampa Bay Buccaneers", "aliases": ["buccaneers", "tampa bay buccaneers"]},
    ("nfl", "ten"): {"display": "Tennessee Titans", "aliases": ["tennessee titans", "titans"]},
    ("nfl", "wsh"): {"display": "Washington Commanders", "aliases": ["commanders", "washington commanders"]},
    # -- NBA (30 teams) --------------------------------------------------
    ("nba", "atl"): {"display": "Atlanta Hawks", "aliases": ["atlanta hawks", "hawks"]},
    ("nba", "bos"): {"display": "Boston Celtics", "aliases": ["boston celtics", "celtics"]},
    ("nba", "bkn"): {"display": "Brooklyn Nets", "aliases": ["brooklyn nets", "nets"]},
    ("nba", "cha"): {"display": "Charlotte Hornets", "aliases": ["charlotte hornets", "hornets"]},
    ("nba", "chi"): {"display": "Chicago Bulls", "aliases": ["bulls", "chicago bulls"]},
    ("nba", "cle"): {"display": "Cleveland Cavaliers", "aliases": ["cavaliers", "cleveland cavaliers"]},
    ("nba", "dal"): {"display": "Dallas Mavericks", "aliases": ["dallas mavericks", "mavericks"]},
    ("nba", "den"): {"display": "Denver Nuggets", "aliases": ["denver nuggets", "nuggets"]},
    ("nba", "det"): {"display": "Detroit Pistons", "aliases": ["detroit pistons", "pistons"]},
    ("nba", "gs"): {"display": "Golden State Warriors", "aliases": ["golden state warriors", "warriors"]},
    ("nba", "hou"): {"display": "Houston Rockets", "aliases": ["houston rockets", "rockets"]},
    ("nba", "ind"): {"display": "Indiana Pacers", "aliases": ["indiana pacers", "pacers"]},
    ("nba", "lac"): {"display": "LA Clippers", "aliases": ["clippers", "la clippers"]},
    ("nba", "lal"): {"display": "Los Angeles Lakers", "aliases": ["lakers", "los angeles lakers"]},
    ("nba", "mem"): {"display": "Memphis Grizzlies", "aliases": ["grizzlies", "memphis grizzlies"]},
    ("nba", "mia"): {"display": "Miami Heat", "aliases": ["heat", "miami heat"]},
    ("nba", "mil"): {"display": "Milwaukee Bucks", "aliases": ["bucks", "milwaukee bucks"]},
    ("nba", "min"): {"display": "Minnesota Timberwolves", "aliases": ["minnesota timberwolves", "timberwolves"]},
    ("nba", "no"): {"display": "New Orleans Pelicans", "aliases": ["new orleans pelicans", "pelicans"]},
    ("nba", "ny"): {"display": "New York Knicks", "aliases": ["knicks", "new york knicks"]},
    ("nba", "okc"): {"display": "Oklahoma City Thunder", "aliases": ["oklahoma city thunder", "thunder"]},
    ("nba", "orl"): {"display": "Orlando Magic", "aliases": ["magic", "orlando magic"]},
    ("nba", "phi"): {"display": "Philadelphia 76ers", "aliases": ["76ers", "philadelphia 76ers"]},
    ("nba", "phx"): {"display": "Phoenix Suns", "aliases": ["phoenix suns", "suns"]},
    ("nba", "por"): {"display": "Portland Trail Blazers", "aliases": ["portland trail blazers", "trail blazers"]},
    ("nba", "sac"): {"display": "Sacramento Kings", "aliases": ["kings", "sacramento kings"]},
    ("nba", "sa"): {"display": "San Antonio Spurs", "aliases": ["san antonio spurs", "spurs"]},
    ("nba", "tor"): {"display": "Toronto Raptors", "aliases": ["raptors", "toronto raptors"]},
    ("nba", "utah"): {"display": "Utah Jazz", "aliases": ["jazz", "utah jazz"]},
    ("nba", "wsh"): {"display": "Washington Wizards", "aliases": ["washington wizards", "wizards"]},
    # -- MLS (30 teams) --------------------------------------------------
    ("mls", "18418"): {"display": "Atlanta United FC", "aliases": ["atlanta", "atlanta united fc"]},
    ("mls", "20906"): {"display": "Austin FC", "aliases": ["austin", "austin fc"]},
    ("mls", "9720"): {"display": "CF Montréal", "aliases": ["cf montréal", "impact", "montreal"]},
    ("mls", "21300"): {"display": "Charlotte FC", "aliases": ["charlotte", "charlotte fc"]},
    ("mls", "182"): {"display": "Chicago Fire FC", "aliases": ["chicago fire fc", "fire"]},
    ("mls", "184"): {"display": "Colorado Rapids", "aliases": ["colorado rapids", "rapids"]},
    ("mls", "183"): {"display": "Columbus Crew", "aliases": ["columbus crew", "crew"]},
    ("mls", "193"): {"display": "D.C. United", "aliases": ["d.c. united", "dc united", "united"]},
    ("mls", "18267"): {"display": "FC Cincinnati", "aliases": ["cincinnati", "fc cincinnati"]},
    ("mls", "185"): {"display": "FC Dallas", "aliases": ["fc dallas"]},
    ("mls", "6077"): {"display": "Houston Dynamo FC", "aliases": ["dynamo", "houston dynamo fc"]},
    ("mls", "20232"): {"display": "Inter Miami CF", "aliases": ["inter miami cf", "miami"]},
    ("mls", "187"): {"display": "LA Galaxy", "aliases": ["galaxy", "la galaxy"]},
    ("mls", "18966"): {"display": "LAFC", "aliases": ["lafc"]},
    ("mls", "17362"): {"display": "Minnesota United FC", "aliases": ["minnesota", "minnesota united", "minnesota united fc"]},
    ("mls", "18986"): {"display": "Nashville SC", "aliases": ["nashville", "nashville sc"]},
    ("mls", "189"): {"display": "New England Revolution", "aliases": ["new england revolution", "revolution", "revs"]},
    ("mls", "17606"): {"display": "New York City FC", "aliases": ["new york city", "new york city fc", "nyc fc", "nycfc"]},
    ("mls", "12011"): {"display": "Orlando City SC", "aliases": ["orlando", "orlando city sc"]},
    ("mls", "10739"): {"display": "Philadelphia Union", "aliases": ["philadelphia union", "union"]},
    ("mls", "9723"): {"display": "Portland Timbers", "aliases": ["portland timbers", "timbers"]},
    ("mls", "4771"): {"display": "Real Salt Lake", "aliases": ["real salt lake"]},
    ("mls", "190"): {"display": "Red Bull New York", "aliases": ["ny red bulls", "red bull new york", "red bull ny", "red bulls"]},
    ("mls", "22529"): {"display": "San Diego FC", "aliases": ["san diego", "san diego fc"]},
    ("mls", "191"): {"display": "San Jose Earthquakes", "aliases": ["earthquakes", "quakes", "san jose earthquakes"]},
    ("mls", "9726"): {"display": "Seattle Sounders FC", "aliases": ["seattle sounders fc", "sounders"]},
    ("mls", "186"): {"display": "Sporting Kansas City", "aliases": ["sporting", "sporting kansas city", "sporting kc"]},
    ("mls", "21812"): {"display": "St. Louis CITY SC", "aliases": ["st louis city", "st. louis", "st. louis city", "st. louis city sc"]},
    ("mls", "7318"): {"display": "Toronto FC", "aliases": ["toronto fc"]},
    ("mls", "9727"): {"display": "Vancouver Whitecaps", "aliases": ["vancouver whitecaps", "whitecaps"]},
}

# A sport/league word in the transcript that disambiguates a shared nickname.
QUALIFIER_WORDS = {
    "nfl": {"nfl", "football"},
    "mlb": {"mlb", "baseball"},
    "nba": {"nba", "basketball"},
    "mls": {"mls", "soccer"},
}

# Fixed default when an alias is shared by 2+ leagues and no qualifier word
# is present in the transcript (see sports_teams.py docstring above).
COLLISION_DEFAULTS = {
    "giants": "nfl",
    "cardinals": "nfl",
}
