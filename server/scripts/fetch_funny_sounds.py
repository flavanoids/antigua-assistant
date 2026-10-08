"""Download the top myinstants sound effects for "play a funny sound".

    uv run --no-project --with curl_cffi python server/scripts/fetch_funny_sounds.py

myinstants sits behind a Cloudflare challenge that plain urllib/curl fail, so
this impersonates Chrome with curl_cffi (run it through uv, not the server
venv). Each clip becomes server/static_audio/funny_<name>.wav: mono 24 kHz
like the cues, loudness-matched to Antigua's voice (the cues measure about
-20.5 LUFS) and then played at --volume percent of that, trimmed to
--max-seconds so the mic isn't muted for a whole song. The WAVs are
git-ignored; run this on each server (deploy_fallback.sh rsyncs them over).
"""

import argparse
import re
import subprocess
import tempfile
from pathlib import Path

from curl_cffi import requests

SITE = "https://www.myinstants.com"
CATEGORY = SITE + "/en/categories/sound%20effects/us/?page={page}"
OUT_DIR = Path(__file__).resolve().parents[1] / "static_audio"
_PLAY_RE = re.compile(r"play\('(/media/sounds/[^']+\.mp3)'")


def top_sounds(count: int) -> list[str]:
    paths: list[str] = []
    page = 1
    while len(paths) < count:
        r = requests.get(CATEGORY.format(page=page), impersonate="chrome", timeout=30)
        r.raise_for_status()
        found = [p for p in _PLAY_RE.findall(r.text) if p not in paths]
        if not found:
            break
        paths += found
        page += 1
    return paths[:count]


def convert(mp3: bytes, dest: Path, volume_pct: int, max_seconds: float):
    with tempfile.NamedTemporaryFile(suffix=".mp3") as src:
        src.write(mp3)
        src.flush()
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", src.name, "-t", str(max_seconds),
             "-af", f"loudnorm=I=-20.5:TP=-1.5,volume={volume_pct / 100}",
             "-ac", "1", "-ar", "24000", "-sample_fmt", "s16", str(dest)],
            check=True,
        )


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--count", type=int, default=50)
    ap.add_argument("--volume", type=int, default=50, help="percent of Antigua's voice level")
    ap.add_argument("--max-seconds", type=float, default=10)
    args = ap.parse_args()

    paths = top_sounds(args.count)
    for old in OUT_DIR.glob("funny_*.wav"):
        old.unlink()
    for path in paths:
        name = re.sub(r"[^a-z0-9]+", "-", Path(path).stem.lower()).strip("-")
        dest = OUT_DIR / f"funny_{name}.wav"
        try:
            r = requests.get(SITE + path, impersonate="chrome", timeout=30)
            r.raise_for_status()
            convert(r.content, dest, args.volume, args.max_seconds)
            print(f"ok   {dest.name}")
        except Exception as e:  # one bad clip shouldn't stop the rest
            print(f"skip {path}: {e}")
    print(f"{len(list(OUT_DIR.glob('funny_*.wav')))} sounds in {OUT_DIR}")


if __name__ == "__main__":
    main()
