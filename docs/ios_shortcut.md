# iPhones, iPads and the HomePod

Three things work with the household's Apple devices:

- **Ask Antigua**: a Shortcut on any iPhone, iPad, Mac or Apple Watch on the
  home Wi-Fi. "Hey Siri, Ask Antigua" (or tap it), say or type the request,
  and Antigua answers on the phone. It's the same skills as the kitchen: timers,
  lists, music, weather, "remind me when I get home".
- **Who's home**: Antigua watches for each person's phone on the Wi-Fi.
  "Who's home?", "Is Alex home yet?", and reminders for arriving:
  "remind me to take out the trash when I get home", "tell Katherine dinner's in
  the fridge when they get home". These are spoken in the kitchen about a minute
  after the phone comes back.
- **Announcements** on the bedroom HomePod: "tell the bedroom dinner's ready",
  "announce the movie's starting upstairs", "tell everyone we're leaving". The
  HomePod chimes, lowers any music, says it in Antigua's voice, and puts the
  music back. Said in the kitchen, "announce ..." goes to the bedroom; from a
  phone it goes everywhere (kitchen included).

## Ask Antigua: building the Shortcut

The household key is in `/etc/antigua/phone.key` on primary-server
(`sudo cat /etc/antigua/phone.key`). Build the Shortcut once on one iPhone, then
share it with everyone else.

In the Shortcuts app, tap **+** and name the shortcut **Ask Antigua**. The name
is what you say to Siri. Then add these actions in order:

1. **Ask for Input**. Type: Text. Prompt: `What do you want to ask Antigua?`
   (With Siri, this is a spoken question you answer out loud.)
2. **Get Device Details**. Detail: Device Name.
3. **Get Contents of URL**
   - URL: `http://<primary-server LAN IP>:9395/ask` (the lan-ip column for primary-server in network/hosts.conf)
   - Method: **POST**
   - Headers: `X-Antigua-Key` = *the key*
   - Request Body: **JSON**, with two Text fields:
     `text` = *Provided Input*, `device` = *Device Details*
4. **Get Dictionary Value**. Get **Value** for key `reply` in *Contents of URL*.
5. **Show Result** with *Dictionary Value*. Siri reads it aloud; tapped, it shows
   on screen. Use **Speak Text** instead if you also want it read aloud when
   tapped.
6. Optional, for questions back ("What time should I remind you?"):
   **Get Dictionary Value** `expects_reply` from *Contents of URL*, then
   **If** *Dictionary Value* **is true** → **Run Shortcut** *Ask Antigua*.

The first run asks to let Shortcuts reach devices on the local network. Allow it.

Optional extras:

- **Apple Watch**: in the shortcut's settings (ⓘ), turn on Show on Apple Watch.
- **HomePod**: "Hey Siri, Ask Antigua" works there too when Personal Requests
  is on for that person's iPhone in the Home app.
- **Home Screen / Control Center / Action button**: add the shortcut from
  its share menu or from Settings > Action Button.

**Sharing**: from the shortcut's menu, use Share > AirDrop to each family member's
phone. The key and address go with it, so share it only inside the household.

**Who's asking**: Antigua matches the phone to a person by its Wi-Fi address
(the `devices:` below), or else by a household name in the device name
("Alex's iPhone"). That's who "me" is in "remind me ..." and who
"Alex says: ..." comes from in an announcement. A device with no match still
works; it just can't use "me".

**Away from home**: the port only answers on the LAN (and wg0). Phones on
cellular can't reach it.

**Changing the key**: write a new one to `/etc/antigua/phone.key` (16+
characters, root-only), restart antigua-server, and update the header in each
copy of the shortcut.

## Who's home: adding phones

On each person's iPhone, open Settings > Wi-Fi > ⓘ next to the home network.
Set **Private Wi-Fi Address** to **Fixed** (not Rotating), then copy the address
shown. Add it under that person in `server/config/server.yaml`:

```yaml
household:
  - name: Alex
    devices: ["aa:bb:cc:dd:ee:ff"]      # Alex's iPhone (fixed private address)
```

Then restart antigua-server. Only list phones that leave with their owner:
an iPad that stays home would make its owner always look home.
`curl -s localhost:9393/presence` on primary-server shows what Antigua sees.

Tuning (`presence:` in server.yaml): `away_after_minutes` (15) is how long a
phone can go unseen before its owner counts as gone. A sleeping iPhone still
answers the scans, but it can miss a few. `arrival_delay_seconds` (60) is the
wait between the phone joining the Wi-Fi and the kitchen reminder.

## Announcements: rooms

`announce.rooms` in server.yaml maps a spoken room to a Music Assistant player
(`kitchen` = the satellite). Any AirPlay speaker Music Assistant sees can be
added. `volume` sets the speaker's level during the announcement.
