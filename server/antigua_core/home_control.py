"""Living room TV (Apple TV + Roku) and Govee lights, driven through MCP servers.

Shared by the primary and the fallback: each box runs its own copies of the
MCP servers (see server/mcp/ and the `mcp:` block in server.yaml), so the TV
and lights keep working when the backup takes over.

Intent parsing stays deterministic (regexes in classify.py and here); MCP is
only the execution layer. Nothing in this module asks the LLM anything.

The TV is one device served by two MCP servers. `living_room_tv.providers`
in server.yaml says which server handles each function, in order of
preference; the first one that's configured, not cooling down and succeeds
wins:

  appletv → mcp-pyatv:          turn_on/turn_off, volume_up/down, get/set_volume,
                                navigate, launch_app/list_apps, play/pause.
                                Reaches the TV itself over HDMI-CEC.
  roku    → mcp-remote-control: press_key(key), power_on, launch_app(app_name)
  govee   → govee-mcp:          set_power/set_brightness/set_color/set_color_temp,
                                addressed by Govee device ID (the app's names for
                                the chandelier bulbs are duplicated/misspelled)
"""

import logging
import re
import time
from threading import Lock, Semaphore, Thread

from .classify import _HDMI_NUM_RE, _TV_APP_RE, _TV_INPUT_RE, _TV_RE

log = logging.getLogger("antigua_core.home")

# Govee aliases (what parse_govee_request returns) whose hardware only takes
# 2700-6500K; the rest accept 2000-9000K.
_NARROW_CCT = ("chandelier-", "monitor-strip")

# Spoken HDMI number → ECP input key
_HDMI_KEYS = {"1": "InputHDMI1", "one": "InputHDMI1", "2": "InputHDMI2", "two": "InputHDMI2",
              "3": "InputHDMI3", "three": "InputHDMI3", "4": "InputHDMI4", "four": "InputHDMI4"}


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower().replace("+", "plus"))


# Apps _TV_APP_RE can name, keyed by _norm() of what it captured:
# (name to say back, Roku MCP launch_app name or None, Apple TV app names to
# match against list_apps, most specific first).
_TV_APPS = {
    "netflix": ("Netflix", "netflix", ["Netflix"]),
    "youtube": ("YouTube", "youtube", ["YouTube"]),
    "youtubetv": ("YouTube TV", None, ["YouTube TV"]),
    "primevideo": ("Prime Video", "prime video", ["Prime Video"]),
    "amazonprime": ("Prime Video", "prime video", ["Prime Video"]),
    "amazonprimevideo": ("Prime Video", "prime video", ["Prime Video"]),
    "hulu": ("Hulu", "hulu", ["Hulu"]),
    "disneyplus": ("Disney Plus", "disney plus", ["Disney+"]),
    "hbomax": ("HBO Max", "hbo max", ["HBO Max", "Max"]),
    "appletvplus": ("Apple TV Plus", "apple tv", ["TV", "Apple TV"]),
    "peacock": ("Peacock", "peacock", ["Peacock"]),
    "paramountplus": ("Paramount Plus", "paramount plus", ["Paramount+"]),
    "espn": ("ESPN", "espn", ["ESPN"]),
    "tubi": ("Tubi", "tubi", ["Tubi"]),
    "sling": ("Sling", "sling tv", ["Sling TV", "Sling"]),
    "slingtv": ("Sling", "sling tv", ["Sling TV", "Sling"]),
    "starz": ("Starz", "starz", ["Starz"]),
    "pluto": ("Pluto TV", "pluto tv", ["Pluto TV"]),
    "plutotv": ("Pluto TV", "pluto tv", ["Pluto TV"]),
    "plex": ("Plex", None, ["Plex"]),
    "twitch": ("Twitch", None, ["Twitch"]),
    "spotify": ("Spotify", None, ["Spotify"]),
    "crunchyroll": ("Crunchyroll", None, ["Crunchyroll"]),
    "applemusic": ("Apple Music", None, ["Music", "Apple Music"]),
}

# ECP input key → how to say it back
_INPUT_NAMES = {"InputTuner": "Live TV", "InputAV1": "AV"}

# TV action → the function name providers are configured under
TV_FUNCTIONS = {
    "tv_power_on": "power", "tv_power_off": "power",
    "tv_volume_up": "volume", "tv_volume_down": "volume",
    "tv_mute": "mute", "tv_unmute": "mute",
    "tv_home": "navigation", "tv_back": "navigation",
    "tv_app": "apps", "tv_input": "inputs",
    "tv_play": "playback", "tv_pause": "playback",
}

# Roku ECP keys. Power is discrete (PowerOn/PowerOff); mute is a toggle,
# Roku has no discrete mute.
_ROKU_KEYS = {
    "tv_power_off": "PowerOff", "tv_mute": "VolumeMute", "tv_unmute": "VolumeMute",
    "tv_volume_up": "VolumeUp", "tv_volume_down": "VolumeDown",
    "tv_home": "Home", "tv_back": "Back", "tv_play": "Play", "tv_pause": "Play",
}

# Apple TV: action → (tool, arguments). Mute and apps need more than one call.
_APPLETV_CALLS = {
    "tv_power_on": ("turn_on", {}), "tv_power_off": ("turn_off", {}),
    "tv_volume_up": ("volume_up", {}), "tv_volume_down": ("volume_down", {}),
    "tv_home": ("navigate", {"direction": "home"}),
    "tv_back": ("navigate", {"direction": "menu"}),
    "tv_play": ("play", {}), "tv_pause": ("pause", {}),
}

# A result that isn't "ok": the TV is unreachable, or the command worked but
# the app isn't installed there.
TV_FAILED, TV_NO_APP = "failed", "no_app"


class HomeControl:
    def __init__(self, hub, cfg: dict):
        self.hub = hub
        t = cfg.get("living_room_tv") or {}
        self.tv_providers = {f: list(p or []) for f, p in (t.get("providers") or {}).items()}
        # Spoken input label → ECP key, e.g. {"playstation 5": "InputHDMI1"}.
        self.tv_inputs = {k.lower(): v for k, v in (t.get("inputs") or {}).items()}
        self.appletv_device = t.get("appletv_device")      # name in mcp-pyatv; None = the only one
        # After a provider fails, skip it this long — but only when a later
        # provider can take over; a lone provider is always tried.
        self.tv_cooldown = float(t.get("failure_cooldown_seconds", 120))
        self._down_until: dict[str, float] = {}
        self._atv_apps: list[dict] = []
        self._atv_apps_at = 0.0
        self._atv_volume_before_mute = None
        g = cfg.get("govee") or {}
        self.govee_server = g.get("mcp_server", "govee")
        self.govee_devices = dict(g.get("devices") or {})   # alias → Govee device ID
        self.govee_concurrency = int(g.get("concurrency", 6))
        self._govee_refresh_lock = Lock()
        self._govee_refreshed_at = 0.0
        # Timers/alarms/reminders run a scene on these lights while the
        # satellite rings, then restore them.
        f = g.get("alarm_flash") or {}
        self.flash_enabled = bool(f.get("enabled", False))
        self.flash_devices = list(f.get("devices") or [])
        self.flash_seconds = float(f.get("seconds", 8))
        self.flash_scenes = dict(f.get("scenes") or {})     # kind → Govee scene name
        self._flashing: set[str] = set()
        self._flash_lock = Lock()

    # ── Living room TV ───────────────────────────────────────────────────

    def tv_available(self) -> bool:
        return any(self.hub.get(p) is not None
                   for ps in self.tv_providers.values() for p in ps)

    def parse_tv(self, transcript: str) -> tuple[str, str] | None:
        """Return (action, arg): ("tv_app", _TV_APPS key), ("tv_input", ECP
        key), or ("tv_power_on"|…, ""), or None if nothing actionable."""
        am = _TV_APP_RE.search(transcript)
        if am:
            return ("tv_app", _norm(am.group(1)))
        m = _TV_INPUT_RE.search(transcript)
        if m:
            key = self._input_key(m.group(1))
            if key:
                return ("tv_input", key)
        if not _TV_RE.search(transcript):
            return None
        low = transcript.lower()
        if "unmute" in low:
            return ("tv_unmute", "")
        if "mute" in low:
            return ("tv_mute", "")
        if "pause" in low and "unpause" not in low:
            return ("tv_pause", "")
        if re.search(r"\b(?:unpause|resume|play)\b", low):
            return ("tv_play", "")
        if "home" in low:
            return ("tv_home", "")
        if "back" in low:
            return ("tv_back", "")
        if any(w in low for w in ("up", "louder")):
            return ("tv_volume_up", "")
        if any(w in low for w in ("down", "quieter", "softer")):
            return ("tv_volume_down", "")
        if "off" in low:
            return ("tv_power_off", "")
        if "on" in low:
            return ("tv_power_on", "")
        return None

    def _input_key(self, target: str) -> str | None:
        target = re.sub(r"^the\s+", "", target.strip().lower().rstrip("?.,!"))
        hm = _HDMI_NUM_RE.match(target)
        if hm and hm.group(1).lower() in _HDMI_KEYS:
            return _HDMI_KEYS[hm.group(1).lower()]
        if any(w in target for w in ("live tv", "live", "antenna", "cable", "dtv")):
            return "InputTuner"
        if target in ("av", "composite", "aux"):
            return "InputAV1"
        for label, key in self.tv_inputs.items():
            if target in label or label in target:
                return key
        return None

    def spoken_target(self, action: str, arg: str) -> str:
        """What to call an app/input in the confirmation ("Switching to …")."""
        if action == "tv_app":
            return _TV_APPS[arg][0]
        if arg in _INPUT_NAMES:
            return _INPUT_NAMES[arg]
        label = next((lbl for lbl, k in self.tv_inputs.items() if k == arg), None)
        return label.title() if label else arg.replace("Input", "").replace("HDMI", "HDMI ")

    def tv(self, action: str, arg: str = "") -> str:
        """Run one parsed TV action on the first provider that can do it.
        Returns "ok", TV_NO_APP, or TV_FAILED."""
        result = TV_FAILED
        providers = [p for p in self.tv_providers.get(TV_FUNCTIONS[action], [])
                     if self.hub.get(p) is not None]
        for i, provider in enumerate(providers):
            has_backup = i < len(providers) - 1
            if has_backup and time.time() < self._down_until.get(provider, 0):
                log.info("TV: skipping %s for %s (cooling down after a failure)", provider, action)
                continue
            run = {"appletv": self._appletv, "roku": self._roku}.get(provider)
            if run is None:
                log.warning("TV: unknown provider %s", provider)
                continue
            result = run(action, arg)
            if result is None:          # provider can't do this action
                result = TV_FAILED
                continue
            if result == TV_FAILED:
                self._down_until[provider] = time.time() + self.tv_cooldown
                continue
            self._down_until.pop(provider, None)
            return result
        return result

    def warm(self):
        """Connect to the Apple TV in the background (pyatv scans ~3s on the
        first connection), so the first spoken command doesn't pay for it."""
        if any("appletv" in ps for ps in self.tv_providers.values()) and self.hub.get("appletv"):
            def _go():
                ok, res = self._atv("power_state")
                log.info("TV/appletv warm: ok=%s %s", ok, res.text[:120])
            Thread(target=_go, daemon=True, name="appletv-warm").start()

    def _roku(self, action, arg):
        if action == "tv_power_on":
            res = self.hub.call("roku", "power_on")
        elif action == "tv_app":
            roku_name = _TV_APPS[arg][1]
            if roku_name is None:
                return None
            res = self.hub.call("roku", "launch_app", {"app_name": roku_name})
        elif action == "tv_input":
            res = self.hub.call("roku", "press_key", {"key_name": arg})
        else:
            res = self.hub.call("roku", "press_key", {"key_name": _ROKU_KEYS[action]})
        # The server reports ECP failures as isError=False "Failed to …" text.
        ok = res.ok and res.text.startswith("Successfully")
        log.log(logging.INFO if ok else logging.WARNING,
                "TV/roku %s(%s): ok=%s %s", action, arg, ok, res.text[:120])
        return "ok" if ok else TV_FAILED

    def _atv(self, tool, args=None):
        a = dict(args or {})
        if self.appletv_device:
            a["device"] = self.appletv_device
        res = self.hub.call("appletv", tool, a)
        # Tool exceptions come back isError=True; navigate's bad-argument
        # replies are plain "Unknown …" text.
        ok = res.ok and not res.text.startswith("Unknown")
        return ok, res

    def _appletv(self, action, arg):
        if action == "tv_input":
            return None
        if action == "tv_app":
            return self._appletv_launch(arg)
        if action in ("tv_mute", "tv_unmute"):
            ok, res = self._appletv_mute(action == "tv_mute")
        else:
            tool, args = _APPLETV_CALLS[action]
            ok, res = self._atv(tool, args)
        log.log(logging.INFO if ok else logging.WARNING,
                "TV/appletv %s(%s): ok=%s %s", action, arg, ok, res.text[:160])
        return "ok" if ok else TV_FAILED

    def _appletv_mute(self, mute: bool):
        """The MCP has no mute: remember the level and set 0, then restore it."""
        if mute:
            ok, res = self._atv("get_volume")
            if not ok:
                return ok, res
            try:
                level = float(res.value())
            except (TypeError, ValueError):
                level = None
            if level:
                self._atv_volume_before_mute = level
            return self._atv("set_volume", {"level": 0})
        return self._atv("set_volume", {"level": self._atv_volume_before_mute or 30})

    def _appletv_launch(self, key):
        spoken, _, names = _TV_APPS[key]
        if not self._atv_apps or time.time() - self._atv_apps_at > 3600:
            ok, res = self._atv("list_apps")
            if not ok or not isinstance(res.value(), list):
                log.warning("TV/appletv list_apps failed: %s", res.text[:160])
                return TV_FAILED
            self._atv_apps, self._atv_apps_at = res.value(), time.time()
        installed = {_norm(a["name"]): a for a in self._atv_apps}
        app = next((installed[_norm(n)] for n in names if _norm(n) in installed), None)
        if app is None:
            log.info("TV/appletv: %s not installed (have %s)", spoken,
                     sorted(a["name"] for a in self._atv_apps))
            return TV_NO_APP
        ok, res = self._atv("launch_app", {"app": app["bundle_id"]})
        log.log(logging.INFO if ok else logging.WARNING,
                "TV/appletv launch %s: ok=%s %s", app["bundle_id"], ok, res.text[:160])
        return "ok" if ok else TV_FAILED

    # ── Govee ────────────────────────────────────────────────────────────

    def govee_available(self) -> bool:
        return self.hub.get(self.govee_server) is not None and bool(self.govee_devices)

    def _govee_call(self, tool: str, args: dict):
        res = self.hub.call(self.govee_server, tool, args)
        val = res.value()
        ok = res.ok and isinstance(val, dict) and val.get("ok") is not False
        return ok, val if isinstance(val, dict) else {"error": res.text}

    def _govee_refresh(self):
        """Re-discover devices, at most once a minute. The server caches its
        device list on disk for a day, so a light that was offline (or the API
        key being added) otherwise stays invisible until the cache expires."""
        with self._govee_refresh_lock:
            if time.time() - self._govee_refreshed_at < 60:
                return
            self._govee_refreshed_at = time.time()
            res = self.hub.call(self.govee_server, "refresh", timeout=30)
            log.info("Govee: device refresh ok=%s", res.ok)

    def govee(self, action: str, aliases: list, param) -> None:
        """Apply one parsed command to each device in parallel; results go to
        the log (the spoken reply is optimistic)."""
        if action == "power":
            tool, args = "set_power", {"on": param}
        elif action == "color":
            r, g, b = param[1]
            tool, args = "set_color", {"red": r, "green": g, "blue": b}
        elif action == "temperature":
            tool, args = "set_color_temp", {"kelvin": param[1]}
        else:
            tool, args = "set_brightness", {"level": param}

        gate = Semaphore(self.govee_concurrency)

        def worker(alias):
            device = self.govee_devices.get(alias)
            if not device:
                log.warning("Govee: no device ID configured for %s", alias)
                return
            wargs = dict(args)
            if tool == "set_color_temp":
                lo, hi = (2700, 6500) if alias.startswith(_NARROW_CCT) else (2000, 9000)
                wargs["kelvin"] = max(lo, min(hi, wargs["kelvin"]))
            calls = [(tool, wargs)]
            if action != "power":
                # A color/warmth/brightness implies on — some models (the H6054
                # TV bar) otherwise stage the setting without lighting up.
                calls.insert(0, ("set_power", {"on": True}))
            with gate:
                for t, a in calls:
                    ok, val = self._govee_call(t, {"name": device, **a})
                    if not ok and "no device named" in str(val.get("error", "")):
                        self._govee_refresh()
                        ok, val = self._govee_call(t, {"name": device, **a})
                    log.log(logging.INFO if ok else logging.WARNING,
                            "Govee %s %s(%s): ok=%s %s", alias, t, a, ok,
                            val.get("transport") or val.get("error", ""))
                    if not ok:
                        break

        threads = [Thread(target=worker, args=(a,), daemon=True) for a in aliases]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    def govee_flash(self, kind: str = "timer") -> None:
        """Run the kind's scene on the alarm_flash lights for flash_seconds,
        then put each one back how it was. Blocks — call it from a thread.

        A scene animates on the device itself, so this is ~4 cloud calls per
        light; pulsing brightness from here gets the cloud-only TV bar
        rate-limited (429) within a single alarm. A light already flashing is
        skipped, so two timers firing together don't restore to the scene."""
        if not (self.flash_enabled and self.govee_available()):
            return
        scene = self.flash_scenes.get(kind) or self.flash_scenes.get("timer")
        if not scene:
            return

        def worker(alias):
            device = self.govee_devices.get(alias)
            if not device:
                log.warning("Govee flash: no device ID configured for %s", alias)
                return
            with self._flash_lock:
                if alias in self._flashing:
                    return
                self._flashing.add(alias)
            try:
                ok, before = self._govee_call("get_device_state", {"name": device})
                started, val = self._govee_call("set_scene", {"name": device, "scene": scene})
                log.log(logging.INFO if started else logging.WARNING,
                        "Govee flash %s: %s (%s) ok=%s %s", alias, scene, kind, started,
                        val.get("error", ""))
                if not started:
                    return
                time.sleep(self.flash_seconds)
                if ok:
                    self._govee_restore(alias, device, before)
            finally:
                with self._flash_lock:
                    self._flashing.discard(alias)

        threads = [Thread(target=worker, args=(a,), daemon=True) for a in self.flash_devices]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    def _govee_restore(self, alias: str, device: str, state: dict):
        """Put a light back to a get_device_state snapshot. The server reports
        both an RGB and a color temperature; a nonzero temperature means the
        light was in white mode."""
        calls = []
        if state.get("power") == "off":
            calls.append(("set_power", {"on": False}))
        else:
            if state.get("color_temp_k"):
                lo, hi = (2700, 6500) if alias.startswith(_NARROW_CCT) else (2000, 9000)
                calls.append(("set_color_temp", {"kelvin": max(lo, min(hi, state["color_temp_k"]))}))
            elif isinstance(state.get("color"), dict):
                c = state["color"]
                calls.append(("set_color", {"red": c.get("r", 255), "green": c.get("g", 255),
                                            "blue": c.get("b", 255)}))
            if state.get("brightness") is not None:
                calls.append(("set_brightness", {"level": state["brightness"]}))
        for t, a in calls:
            ok, val = self._govee_call(t, {"name": device, **a})
            if not ok:
                log.warning("Govee flash %s: restore %s failed: %s", alias, t, val.get("error", ""))
