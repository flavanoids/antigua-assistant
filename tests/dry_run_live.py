#!/usr/bin/env python3
"""Dry run against the live backends, with nothing in the house touched.

Runs typed utterances through the real pipeline in this process: the real
Spanish front-end, classifier, skills, LLM, translator and Kokoro TTS. Only
device *writes* are swapped for recorders:

- Music Assistant: reads (players, queue, search, library) go through, so
  "is music playing" and catalogue lookups are real; play/pause/volume/duck
  and every other write are recorded instead of sent.
- MCP (TV, lights): nothing reaches the servers; calls are recorded and
  answered with a canned success (a fixed app list for "open Netflix").
- Timers: a private TimerManager that never rings.
- Stores (timers, memories, lists, speaker profiles): scratch copies in a
  temp dir, never data/*.json.
- MQTT: never connected; every publish is dropped. Nothing plays anywhere.

The live antigua-server is not involved, so a real kitchen turn during the
run behaves normally. TTS WAVs are kept (paths printed) so replies can be
listened to later.

Run: venv/bin/python tests/dry_run_live.py            # built-in Spanish set
     venv/bin/python tests/dry_run_live.py "pausa la música" "qué hora es"
"""

import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from antigua_core import settings  # noqa: E402

# Every store works on scratch copies: the live server owns data/*.json, and a
# dry run once overwrote data/timers.json with its test alarm. configure() runs
# inside the server import and resets the paths, so redirect after it.
_SCRATCH = Path(tempfile.mkdtemp(prefix="antigua_dry_run_"))
_STORES = ("MEMORY_STORE_PATH", "TIMER_STORE_PATH", "LISTS_STORE_PATH", "SPEAKER_PROFILES_PATH")
_real_configure = settings.configure


def _configure(cfg, **kw):
    _real_configure(cfg, **kw)
    for name in _STORES:
        live = getattr(settings, name)
        if live.exists():
            shutil.copy(live, _SCRATCH / live.name)
        setattr(settings, name, _SCRATCH / live.name)


settings.configure = _configure

import antigua_server as srv  # noqa: E402  (no connect/threads until main())
from antigua_core import pipeline  # noqa: E402

settings.configure = _real_configure
assert all(str(getattr(settings, n)).startswith(str(_SCRATCH)) for n in _STORES)
from antigua_core.mcp_client import McpResult  # noqa: E402
from antigua_core.stores import TimerManager  # noqa: E402

CALLS: list[str] = []


def _no_mqtt(topic, payload):
    pass


srv.mqtt_publish = _no_mqtt
settings.mqtt_publish = _no_mqtt

# ── MCP: record, never send ──────────────────────────────────────────────────

_APPS = [{"name": n, "bundle_id": f"dry.{n.lower().replace(' ', '')}"}
         for n in ("Netflix", "YouTube", "Disney+", "Max", "Hulu", "Prime Video",
                   "Apple TV", "Spotify", "Plex", "Peacock", "Paramount+")]


class _RecordingHub:
    def __init__(self, real):
        self.real = real

    def get(self, name):          # availability only; never started
        return self.real.get(name)

    def call(self, server, tool, arguments=None, timeout=None):
        args = {k: v for k, v in (arguments or {}).items() if k != "device"}
        if tool == "list_apps":
            return McpResult(ok=True, data={"result": _APPS})
        if tool == "get_volume":
            return McpResult(ok=True, text="30")
        CALLS.append(f"{server}.{tool}({args})" if args else f"{server}.{tool}()")
        return McpResult(ok=True, text="Successfully done (dry run)", data={"ok": True})


srv.home.hub = _RecordingHub(srv.home.hub)

# ── Music Assistant: reads pass, writes recorded ─────────────────────────────

_MA_READS = ("players/all", "players/get", "player_queues/get", "music/search",
             "music/artists/artist_albums")

if srv.music is not None:
    _real_cmd = srv.music.ma.cmd

    def _cmd(command, timeout=None, **args):
        if command in _MA_READS or command.endswith("/library_items"):
            return _real_cmd(command, timeout=timeout, **args)
        brief = {k: v for k, v in args.items() if k not in ("player_id", "queue_id")}
        if "media" in brief:
            brief["media"] = [m.split("/")[-1] if isinstance(m, str) else m
                              for m in (brief["media"] if isinstance(brief["media"], list)
                                        else [brief["media"]])]
        CALLS.append(f"music.{command}({brief})")
        return None

    srv.music.ma.cmd = _cmd
    # Corrections are remembered in data/music_prefs.json; a dry run's must
    # not steer the live server's next "play X".
    srv.music._prefs_path = _SCRATCH / "music_prefs.json"
    srv.music._played_path = _SCRATCH / "music_played.json"

# ── Timers: private, silent ──────────────────────────────────────────────────

pipeline.B.timers = TimerManager(on_fire=lambda *a, **kw: None)

SPANISH = [
    # music (reads real player state; writes recorded)
    "Pausa la música.", "Siguiente canción.", "Súbele a la música.",
    "Pon la música al cuarenta", "¿Qué canción es esta?",
    "Pon música de Bad Bunny.", "Pon el último álbum de Bad Bunny",
    "Pon la canción Tití Me Preguntó de Bad Bunny",
    "Ponme algo de Bad Bunny en la barra de sonido",
    # TV
    "Prende la tele.", "Apaga la tele.", "Silencia la tele", "Pon Netflix",
    # lights
    "Apaga las luces.", "Prende las luces del pasillo", "Pon las luces en azul",
    "Pon el candelabro rojo", "Pon las luces al cincuenta por ciento",
    # time, timers, alarms
    "¿Qué hora es?", "¿Qué día es hoy?", "Pon un temporizador de cinco minutos.",
    "¿Cuánto le falta al temporizador?", "Cancela el temporizador.",
    "Despiértame a las seis y media.",
    # weather + chat (LLM / translator)
    "¿Va a llover mañana?", "¿Qué temperatura hace?",
    "Oye, ¿cómo estuvo tu día?", "Cuéntame algo interesante sobre Guatemala.",
]


def main(utterances):
    ok = True
    for text in utterances:
        CALLS.clear()
        t0 = time.time()
        r = pipeline.dispatch_text(text, quiet=True)
        took = time.time() - t0
        time.sleep(0.3)           # music plays on a thread; let it record
        wav = r.get("audio_file") or ""
        print(f"\n» {text}")
        print(f"  heard as : {r.get('transcript', '')!r}   action={r.get('action', '')}  {took:.2f}s")
        print(f"  reply    : {r.get('response', '')}")
        for c in CALLS:
            print(f"  would do : {c}")
        print(f"  audio    : {wav or '(none)'}")
        if not r.get("response"):
            ok = False
    return ok


if __name__ == "__main__":
    sys.exit(0 if main(sys.argv[1:] or SPANISH) else 1)
