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
- "Snooze" / "Snooze for 10 minutes" (after an alarm rings)

**Behavior**:
- Fires: alarm sound + TTS voice ("Your pasta timer is done." / "Good morning, your alarm is going off." / "Reminder: move the laundry.")
- A reminder with no time ("remind me to buy milk") can't be set — she answers from the LLM instead.
- Bare daypart words default: morning 8 AM, afternoon 2 PM, evening 7 PM, night/tonight 9 PM.
- If you give a day but no time ("remind me to call the dentist tomorrow"), she asks "What time tomorrow?" and sets it from your answer.
- Rings for `alarm_ring_seconds` (default 5s — the mic sits next to the speaker, so the wake word is unreliable while it rings), or until you say the wake word, which also drops into conversation mode so you can immediately say "snooze"
- Recurring alarms reschedule themselves after each fire
- Multiple timers/alarms supported; survive a server restart (alarm day-labels refresh on reload)

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

## General Knowledge

**What she knows**: History, science, math, cooking, language, culture, stories — anything from her training data.

**How to ask**: Just ask. "Who was the first president of Mexico?", "How do you make risotto?", "Tell me a joke."

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
