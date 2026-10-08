# Antigua — Skills & Capabilities

What Antigua can do and how to ask for it. The wake word is "Alexa". For how
each skill works inside, see [ANTIGUA_SKILLS/](ANTIGUA_SKILLS/README.md).

---

## Weather

**What she knows**: Current conditions, hourly precipitation, and a 7-day forecast for anywhere — home by default. Deterministic spoken answers (no LLM). Source: Open-Meteo, refreshed every 15 minutes. US severe-weather alerts (NWS) for home are announced unprompted.

**How to ask**:
- "What's the weather?" / "How hot will it get today?"
- "Is it going to rain this afternoon?" / "Any rain this weekend?"
- "Do I need an umbrella?" / "Is it cold out?"
- "What's the forecast for Thursday?" / "...next few days?"
- "Is it warmer than yesterday?" / "When's sunset?"
- "What's the weather in Denver?" / "...in Hawaii?"

**Limits**: 7 days out (no months or seasons). Alerts are US only. Region/country questions use one representative point.

---

## Timers, Alarms & Reminders

**What she does**: Parses natural language, confirms deterministically (no LLM), and fires a spoken announcement when time is up. Three distinct kinds — timers report time remaining, alarms report a clock time, reminders carry an action phrase she reads back.

**How to ask**:
- "Set a timer for 5 minutes" / "for an hour and a half" / "for two and a half minutes"
- "Set a pasta timer for 10 minutes" / "a 20 minute timer for banana bread"
- "Wake me up at 7" / "at 6:30 tomorrow" / "at 8 on Friday"
- "Set an alarm for 6:45 every weekday" / "every morning at 7" / "every Monday and Thursday at 6"
- "Remind me to move the laundry in 40 minutes" / "remind me to call the dentist at 3 tomorrow"
- "Don't let me forget to take the trash out tonight" / "remind me to take my pills every day at 8"
- "How much time is left?" / "How long on the pasta timer?" / "When's my alarm?" / "What are my reminders?"
- "Add 5 minutes to the timer" / "Reset the timer" / "Cancel my 7 AM alarm" / "Cancel the dentist reminder" / "Cancel everything"
- "Snooze" / "Snooze for 10 minutes" / "Snooze 10 more minutes" (after something rings)
- "Set a timer for 10" (minutes) / "Set a timer" → "For how long?" / "Set an alarm" → "For what time?"
- "Set an alarm for half past six" / "quarter to seven" / "noon" / "6 AM on weekdays" / "Monday through Friday"
- "Take 2 minutes off the timer" / "Add five more minutes" / "Pause the timer" / "Resume the timer"
- "Change my alarm to 7:30" / "Push my alarm back 15 minutes" / "Move tomorrow's alarm to 8" (just that day)
- "Skip tomorrow's alarm" / "Turn off my alarm for tomorrow" (a repeating alarm sits that day out)
- "Remind me to stretch every hour" / "every 30 minutes"
- "Is my alarm set?" / "What time is my alarm?" / "Do I have any alarms tomorrow?"
- "Stop" / "Turn it off" / "I'm up" while something is ringing

**Behavior**:
- Fires: alarm sound + TTS voice ("Your pasta timer is done." / "Good morning, your alarm is going off." / "Reminder: move the laundry.")
- A reminder with no time ("remind me to buy milk") can't be set — she answers from the LLM instead.
- Bare daypart words default: morning 8 AM, afternoon 2 PM, evening 7 PM, night/tonight 9 PM.
- If you give a day but no time ("remind me to call the dentist tomorrow"), she asks "What time tomorrow?" and sets it from your answer.
- Rings (announcement + a 5s bell; a soft chime for reminders) every 30s until you say the wake word or "stop", for up to 5 minutes. The bell stays short because the mic sits next to the speaker: the wake word is heard in the quiet between rings. `alarm_repeat_seconds` / `alarm_max_seconds` / `alarm_ring_seconds` in `satellite.yaml`
- "Cancel my alarm" with several set asks which one
- Recurring alarms reschedule themselves after each fire
- Multiple timers/alarms supported; survive a server restart (alarm day-labels refresh on reload), and one that came due during a restart still rings if it's under 10 minutes late

---

## Calculator & Conversions

**What she does**: Arithmetic, percentages, tips, and unit/temperature conversions — computed and spoken from pure Python, no LLM. Currency conversion too, using daily reference rates (cached 6 hours).

**How to ask**:
- "What's 12 times 13?" / "100 divided by 4" / "what's 7 minus 12"
- "What's 15% of 80?" / "20 percent off 40 dollars" / "add 20% to 45"
- "What's a 20% tip on a 47 dollar check?" (defaults to 20%)
- "Half of 250" / "three quarters of 200" / "double 1500"
- "How many ml in 2 cups?" / "how many tablespoons in a cup"
- "5 miles in km" / "convert 3 feet to centimeters" / "how many minutes in 2 hours"
- "350 Fahrenheit in Celsius" / "20 C to F"
- "50 dollars in euros" / "how much is 20 pounds in dollars" / "convert 100 yen to dollars"

**Limits**: No algebra, word problems, square roots, or date math — those go to the LLM. Single-unit answers only ("2 cups", not "1 lb 4 oz"). Say "fluid ounce" when you mean volume — plain "ounce" is weight. Currency rates are daily reference rates (not live/tradeable) and cover ~30 common currencies; anything else falls to the LLM.

---

## Drive Times & Traffic

**What she knows**: Drive time by car from home to any business, address, town or saved favorite, and whether traffic is worse than usual (and why). Deterministic, no LLM, no Google. Places come from OpenStreetMap (Photon). Routes come from TomTom with live traffic when a key is set, else OSRM. Live Houston speeds and incidents come from TranStar.

**How to ask**:
- "How long to get to Lowe's on Ella?" / "How long to drive to HEB Bunker Hill?"
- "How long to Niko Niko's?" (two nearby, so she asks "which one?"; answer "the Montrose one", "the one by the beltway", "the closer one")
- "How far is Mom's house?" / "What's my ETA to work?"
- "How's traffic to work?" / "How's traffic?" / "Is there traffic on I-10?" / "Any wrecks on 610?"

**Setup**: home and favorites (friends, family, work) go in `server/config/places.yaml`. That file is private: git-ignored, chmod 600, never spoken back or logged. Copy it from `places.example.yaml`. Optional: a free TomTom developer key as `TOMTOM_API_KEY` in `server/config/mcp.env`.

**Limits**: Car only, always from home ("from X to Y" goes to the LLM). Live traffic is Houston-area only, and on the keyless OSRM route it covers only the roads TranStar measures. Elsewhere she says the time is "without live traffic".

---

## Sports Scores

**What she knows**: Live scores, final results, next-game schedules, and season records for NFL, MLB, NBA, and MLS teams — any team in those four leagues, not just the local ones. Formula 1 race results and the next scheduled race (no standings). Deterministic, no LLM. Source: ESPN's public scoreboard API, refreshed every 60 seconds for live scores and every 5 minutes for records/schedules.

**How to ask**:
- "What's the score of the Astros game?" / "Did the Texans win?"
- "Who won the Rockets game last night?" / "How'd the Dynamo do?"
- "When's the next Astros game?" / "When do the Packers play next?"
- "What's the Rockets' record?"
- "Who won the last F1 race?" / "When's the next Grand Prix?"

**Behavior**:
- Any NFL, MLB, NBA or MLS team works by city, full name or nickname.
- A nickname shared by two leagues ("Giants," "Cardinals") isn't guessed at random — naming the sport ("the football Giants," "the baseball Cardinals") picks the right one; left unqualified, it defaults to the NFL team.
- If the score service can't be reached, she says so directly rather than guessing — she'll never make up a score.

**Limits**: No standings/playoff-picture questions yet. No other leagues (NHL, college sports, international soccer beyond MLS). No F1 driver/constructor standings — race results and schedule only.

---

## News Headlines

**What she knows**: Latest headlines from Al Jazeera, BBC, Reuters, AP, and NPR. Refreshed every 20 minutes. Reuters and AP retired their public RSS feeds years ago, so those two are sourced via a live Google News search scoped to their own sites rather than a direct feed — same underlying mechanism as topic search below.

**How to ask**:
- "What's in the news?"
- "Give me the headlines"
- "Al Jazeera headlines" / "Reuters headlines" / "AP headlines"
- "News about Ukraine"
- "What's happening in Texas?"
- "Reuters headlines on the Middle East"
- "Tech news" / "business news" / "science news"

**Behavior**:
- Reads up to 5 headlines naturally
- Attributes each to its source ("Al Jazeera reports...", "Reuters says...")
- No commentary or opinions — just the headlines
- Topic and source filtering work together
- If there's genuinely nothing to report (a source has no fresh items, a topic has no matches), she says so plainly rather than guessing — this skill used to hand an empty context to the LLM and let it fill the gap, which once produced a fully invented headline; it now answers "no headlines" directly instead.
- "Sports news" with no team named points you at the dedicated sports-scores skill instead of an empty headline search — name a team or F1 for that.

**Limits**: No general sports-headlines category — ask about a specific team or F1 instead (see Sports Scores).

---

## Volume Control

**What she does**: Turns up/down whatever is playing: the music if any is playing, otherwise the Living Room TV's soundbar (Apple TV, HDMI-CEC). Never her own voice.

**How to ask**:
- "Turn it up"
- "Louder"
- "Too quiet"
- "Turn it down"
- "Speak up"
- "Too loud"

**Behavior**: One step per command on the music player or TV. "Turn the music up" and "tv volume down" target those directly.

---

## Funny Sounds

**What she does**: Plays a random clip from the top 50 of myinstants' US sound effects (vine boom, airhorn, bruh...) at half her speaking volume.

**How to ask**:
- "Play a funny sound"
- "Play another funny sound" / "Make a funny noise"

**Setup**: the clips aren't in git. Download them with `uv run --no-project --with curl_cffi python server/scripts/fetch_funny_sounds.py`. See [ANTIGUA_SKILLS/funny_sound/](ANTIGUA_SKILLS/funny_sound/README.md).

---

## Govee Lights

**What she does**: Controls Govee lights (power, color, brightness, white warmth) through the Govee MCP server, over the LAN when the light allows it and through the Govee cloud otherwise. Works from the backup server too.

**How to ask**:
- "Turn on the hallway lights" / "Turn off all the lights"
- "Set the rope light to purple"
- "Set the chandelier to 5700 kelvin" / "Make the chandelier daylight"
- "Set the hallway lights to warm white"
- "Set the TV lights to 50 percent"
- "Dim the hallway lights"

**Devices**: whatever is listed under `govee.devices` in `server.yaml`, by the names you give them. Groups work too ("hallway lights", "all the lights").

**Behavior**: Confirms immediately; the command reaches the light a moment later (well under a second over LAN, 1–2 seconds via the cloud). If the light or the Govee cloud is unreachable, the failure is only logged, not spoken.

---

## PinedaDisplay (the airplaypi screen)

**What she does**: Controls the PinedaDisplay dashboard on the airplaypi kiosk and answers questions about what's on it. Details: [ANTIGUA_SKILLS/pineda_display/](ANTIGUA_SKILLS/pineda_display/README.md).

**How to ask**:
- "Change the theme to ocean" / "Switch to the groovy theme" / "Random theme" / "What themes are there?"
- "Restart the display" / "Reboot the Pi" (she asks you to say yes first)
- "Who said this quote?" / "Read the quote" / "Tell me more about this quote"
- "Say the Spanish phrase" / "What's the word of the day?"
- "When was this photo taken?"

**Behavior**: Theme changes stick until you change them again, and each light theme still turns to its night version at sundown. The Spanish phrase plays in the Spanish voice, then the meaning in English, then the Spanish again. Running timers show in the display's small moon card.

**Limits**: Only the primary server can reach the display. Photos without a camera date (screenshots, edited images) can't be dated. A reboot takes music and her voice away for about a minute.

---

## Music

**What she does**: Plays Apple Music on the kitchen speaker, or on the living room TV, the soundbar or the bedroom HomePod if you name one, like Alexa.

**How to ask**:
- "Play Beyoncé" (her Essentials) / "Play the album Lemonade" / "Play Halo by Beyoncé"
- "Play Beyoncé's newest album" / "Play Bad Bunny songs from 2021"
- "Play the song that goes …" (sing or say a line)
- "What are the older albums by Toro y Moi?", then "Play the second one"
- "Play Adele in the bedroom" / "Move the music to the soundbar"
- "Pause", "Next song", "Restart the song", "Replay the album", "Repeat this song", "Shuffle", "What song is this?", "Turn the music down"

**Behavior**: Playback controls just happen, with no spoken reply. The music in the kitchen dips while she's listening and talking.

---

## News podcasts (Apple News Today, Up First)

**What she does**: Plays the day's episode of Apple News Today or NPR's Up First on the music speakers.

**How to ask**:
- "Play Apple News Today" / "Play the Apple News podcast"
- "Play yesterday's Apple News Today" / "Play Friday's Apple News" / "Put on the latest Apple News Today"
- "Play Apple News Today on the soundbar"
- "Play Up First" / "Play NPR's Up First" / "Play Saturday's Up First"
- "Stop Apple News" / "Pause the podcast" / "Resume the podcast"

**Behavior**: She plays today's episode. If it isn't out yet (both drop around 5am Central), she plays the latest one instead. On a day with no news episode she asks "Want me to play Friday's?": Apple News Today skips weekends, and Up First's Sunday episode is a long-form story, not the news. Say the wake word and "yes" within a minute. Shows are listed under `podcasts:` in server.yaml. Apple News Today has no RSS feed, so its episodes come from its Apple Podcasts page.

---

## Living Room TV

**What she does**: Controls the living room TV through two MCP servers. The Apple TV ("Living Room") handles power, volume, mute, apps, navigation and play/pause, and reaches the TV itself over HDMI-CEC. The Roku TV handles switching inputs. Works from the backup server too.

**How to ask**:
- "Turn on the TV" / "Turn the Apple TV off"
- "TV volume up" / "Mute the TV"
- "Pause the TV" / "Resume the TV"
- "TV home" / "Go back on the TV"
- "Open Netflix" / "Put on YouTube on the TV" / "Open Plex"
- "Switch to HDMI 2" / "Switch to the PlayStation" / "Switch to live TV"

**Behavior**: Confirms only after the device acknowledges. If it doesn't answer, she says "The Living Room TV did not respond". For an app that isn't installed on the Apple TV, she says so.

---

## People, History & Events

**How to ask**: "Who was Frida Kahlo?", "Tell me about the Cuban Missile Crisis", "What was Watergate?", "What happened at Chernobyl?", "Quién fue Benito Juárez?"

**Behavior**: A short but substantive overview (3–4 sentences) from the subject's Wikipedia article: who or what, when and where, what they're known for, and one notable detail. Nothing is added that the article doesn't say.

**Follow-ups**: For 15 minutes afterward, wake her and ask about the same subject: "Was she married?", "What's her hometown?", "What are her notable achievements?", "How did it end?", "Tell me more". Answers come from the same article and its Wikidata facts. If the article doesn't cover it, she says she doesn't have that detail.

**Limits**: Role and live questions ("Who is the CEO of…", "Who is playing tonight") go to web search instead.

---

## Recipes

**How to ask**: "How do I make cheesecake?", "Give me a recipe for chicken noodle soup", "Let's bake banana bread", "Chicken noodle soup for 4 people", "Pozole recipe"

**Behavior**: She finds a real recipe online and says where it's from, how many it serves and how long it takes, with a heads-up for long waits like chilling overnight. Then she reads the ingredients (grouped, like "For the crust…") and any special equipment, and asks "Do you have everything?" Recipes are never made up: if she can't find one online, she says so.

**Size**: Ask for "…for 4 people" and the recipe starts scaled to 4. Mid-recipe, "make it for 4", "I'm cooking for 6 people", "double it", "halve it", "back to the original" rescale the amounts; "how many does it serve?" says the current size. She never scales times, temperatures or pan sizes — she says they're from the original recipe, and warns when you may need a bigger or smaller pan. A "for 4" request carries into "another recipe"; a plain "double it" resets with it.

Say "one at a time" and she reads them one by one, waiting for "got it" or "I don't have it".

**Missing something**: "No, I don't have sour cream." She suggests a substitute ("the same amount of plain Greek yogurt. Do you have that?"). Say no and she offers the next one. If nothing works, she tells you it can be left out (a spice, herb or vegetable), or offers to put an essential ingredient on the shopping list and asks whether you want a different recipe.

**Cooking**: "Ready for step one?" Each step ends with "Let me know when you're ready for the next step." Say (with the wake word each time):
- "Next", "I'm ready", "Done": the next step
- "Go back", "Repeat that", "Start over", "Go to step 4"
- "What was step 3?" (reads it without losing your place), "How many steps are left?"
- "How much sugar?", "How long in the oven again?", "What temperature?": answered from the recipe
- "Can I skip the vanilla?", "Is the sour cream really necessary?": the recipe's optional/essential rule, straight
- "How do I know when it's done?", "What's it supposed to look like?": the step's own doneness cue
- "What can I use instead of butter?", "Read the ingredients again"
- "Make it for 4", "Double it", "Halve it", "Back to the original", "How many does it serve?"
- "Another recipe", "Something simpler", "One without nuts"
- "Stop the recipe" / "I'm done cooking"

On a timed step ("bake 45 minutes") she offers to set a timer. While a recipe is on its steps, a bare "next" means the next step; say "next song" to skip music.

**Asking anything else**: "Can I use a hand mixer?", "Why a water bath?", "Can I make this the day before?" — she answers from the recipe she's reading you, plus general kitchen technique. She'll never quote an amount, time, temperature or pan size the recipe doesn't state (a code check, not just a promise); if the recipe doesn't say, she says so.

**Limits**: The recipe stays open for 4 hours after the last thing you said about it. Only one recipe is open at a time, for the whole house. English only for now. The backup server can't look up recipes.

---

## General Knowledge

**What she knows**: History, science, math, cooking, language, culture, stories — anything from her training data.

**How to ask**: Just ask. "Who was the first president of Mexico?", "Why is the sky blue?", "Tell me a joke."

**Limits**: She doesn't know anything after her training cutoff. For live data (stock prices, traffic), she'll say so briefly.

---

## Conversation Memory

**What she does**: Remembers context across follow-up questions within a conversation window.

**How to use**:
1. Say the wake word, ask a question, and get a response.
2. Say the wake word again within 8 seconds of her finishing.
3. Ask a follow-up without repeating context ("What about the second one?", "Tell me more").

**Limits**: Keeps the last 6 messages, for 5 minutes. After that, the context resets. You have to say the wake word again: plain speech doesn't reopen the mic, which prevents her from hearing and answering herself.

---

## Sleep / End Conversation

**How to end**: Say "thank you", "thanks", "stop", "stop listening", "goodbye", or "that's all".

**Behavior**: She answers briefly ("No problem") and then ends the conversation: no follow-up window, and the next wake word starts fresh.

---

## Lists

**What she does**: Named, shared shopping and to-do lists — add, read, remove, and clear.

**How to use**:
- "Add milk, eggs and bread to the shopping list"
- "What's on my shopping list?"
- "Remove milk from the shopping list"
- "Clear the shopping list"

**Limits**: Voice-only — `/lists` returns JSON but there's no phone-viewable page yet.

---

## Speaker Identification

**What she does**: Recognizes enrolled household members' voices, so a memory request that doesn't name anyone ("remember I took my medicine") skips the "who is this for?" question. A background voice check runs alongside speech recognition and only acts when it's confident.

**How to use**: Nothing changes when you talk to her; it's invisible when it works. Naming someone explicitly ("remember **for Alex** that…") always wins.

**Limits**: Primary server only. People have to be enrolled first (`server/scripts/enroll_speaker.py`); until then, and for anyone who isn't enrolled, she asks who it's for. She never guesses: too-short or low-confidence audio falls back to asking.

---

## Not Yet Implemented

- Smart home controls beyond lights and the TV (locks, thermostats)
- Calendar and email
- A custom "Hey Antigua" wake word (the wake word is "Alexa" for now)
