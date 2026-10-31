#!/usr/bin/env python3
"""Time the three stages a voice turn waits on, with idle gaps between calls
so the GPU drops to idle clocks first, like a real kitchen turn does:

    LLM   first token and tok/s from Ollama (the model in server.yaml)
    STT   whisper.cpp server, one 11 s clip                (skipped if not running)
    TTS   Kokoro server, one short sentence                (skipped if not running)

    server/scripts/bench_latency.py                  # ollama.model, 6 rounds, 12 s gaps
    server/scripts/bench_latency.py --fallback       # fallback.model (run on the backup)
    server/scripts/bench_latency.py --model qwen3:8b --rounds 3 --gap 5

It talks to the local services only and leaves the model loaded. Its own system
prompt evicts the voice prompt's cache: the next real turn re-reads it once.
Real turns: server/scripts/turn_stats.py logs/server.log.
"""

import argparse
import json
import statistics
import time
import urllib.request
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent.parent
SYS = "You are a helpful kitchen voice assistant. Answer in one short sentence. " * 30
QS = ["What's a good snack?", "How long do eggs boil?", "Name a fruit.", "What is two plus two?",
      "Say hello.", "Give me a color.", "Name a dog breed.", "Pick a number."]


def config():
    for name in ("server.yaml", "server.example.yaml"):
        p = REPO / "server" / "config" / name
        if p.exists():
            return yaml.safe_load(p.read_text())
    return {}


def llm(host, model, num_ctx, q):
    body = json.dumps({"model": model, "stream": True, "keep_alive": -1, "think": False,
                       "options": {"num_ctx": num_ctx, "num_predict": 24},
                       "messages": [{"role": "system", "content": SYS}, {"role": "user", "content": q}]})
    t0 = time.monotonic()
    first = None
    with urllib.request.urlopen(urllib.request.Request(f"{host}/api/chat", body.encode()), timeout=120) as r:
        for line in r:
            d = json.loads(line)
            if first is None and d.get("message", {}).get("content"):
                first = time.monotonic() - t0
            if d.get("done"):
                return first or time.monotonic() - t0, d["eval_count"] / (d["eval_duration"] / 1e9)
    raise RuntimeError("no done message from Ollama")


def stt(url, wav):
    b = "----bench"
    body = (f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="a.wav"\r\n'
            "Content-Type: audio/wav\r\n\r\n").encode() + wav.read_bytes() + f"\r\n--{b}--\r\n".encode()
    t0 = time.monotonic()
    urllib.request.urlopen(urllib.request.Request(
        url, body, {"Content-Type": f"multipart/form-data; boundary={b}"}), timeout=60).read()
    return time.monotonic() - t0


def tts(url, i):
    text = f"Your timer for {i + 7} minutes is set, and the oven is preheating now."
    t0 = time.monotonic()
    urllib.request.urlopen(urllib.request.Request(
        url, json.dumps({"text": text}).encode(), {"Content-Type": "application/json"}), timeout=60).read()
    return time.monotonic() - t0


def up(url):
    try:
        urllib.request.urlopen(url, timeout=2)
        return True
    except urllib.error.HTTPError:
        return True          # answering, just not to a bare GET
    except OSError:
        return False


def main():
    cfg = config()
    o, fb = cfg.get("ollama") or {}, cfg.get("fallback") or {}
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--fallback", action="store_true", help="use fallback.model / fallback.num_ctx")
    ap.add_argument("--model", help="override the model")
    ap.add_argument("--num-ctx", type=int)
    ap.add_argument("--rounds", type=int, default=6)
    ap.add_argument("--gap", type=float, default=12, help="idle seconds before each call")
    ap.add_argument("--ollama", default=o.get("host", "http://localhost:11434"))
    ap.add_argument("--stt", default="http://127.0.0.1:8178/inference")
    ap.add_argument("--tts", default="http://127.0.0.1:5500/tts")
    ap.add_argument("--wav", type=Path, default=REPO.parent / "whisper.cpp" / "samples" / "jfk.wav")
    a = ap.parse_args()
    model = a.model or (fb.get("model") if a.fallback else o.get("model"))
    num_ctx = a.num_ctx or int((fb if a.fallback else o).get("num_ctx") or 8192)
    do_stt = a.wav.exists() and up(a.stt.rsplit("/", 1)[0] + "/")
    do_tts = up(a.tts.rsplit("/", 1)[0] + "/health")

    res = {"llm_first": [], "tok_s": [], "stt": [], "tts": []}
    llm(a.ollama, model, num_ctx, "warm up")
    for i in range(a.rounds):
        time.sleep(a.gap)
        f, tps = llm(a.ollama, model, num_ctx, QS[i % len(QS)])
        res["llm_first"].append(f)
        res["tok_s"].append(tps)
        if do_stt:
            time.sleep(a.gap)
            res["stt"].append(stt(a.stt, a.wav))
        if do_tts:
            time.sleep(a.gap)
            res["tts"].append(tts(a.tts, i))
    print(f"model {model}  num_ctx {num_ctx}  rounds {a.rounds}  gap {a.gap:g}s")
    for k, v in res.items():
        if v:
            unit = "" if k == "tok_s" else " s"
            print(f"  {k:9} median {statistics.median(v):7.2f}{unit}   (min {min(v):.2f}, max {max(v):.2f})")
        else:
            print(f"  {k:9} skipped (service not running here)")


if __name__ == "__main__":
    main()
