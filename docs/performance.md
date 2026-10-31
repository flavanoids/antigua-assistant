# Performance: what to expect

Two numbers matter for how Antigua feels: how long after you stop talking
the first word comes back, and whether that time stays the same from turn to
turn. With an 8 GB GPU from the last few years, skills answer in about 0.7 s
and conversation in about 1.2 s. On an older 4 GB card it still works, but
expect about 2.5 s and 4.5 s: you'll notice the wait.

All figures are medians from real use: spoken turns in a kitchen over several
days, measured from the end of speech to the first audio. Each tier was
measured on one machine. Other cards in the same class should land close, but
they weren't tested.

## By GPU tier

| | 8 GB mid-range GPU | 4 GB entry or older GPU |
|---|---|---|
| Cards in this class (last ~5 years) | Radeon RX 6600 / 6650 XT / 7600, GeForce RTX 3060 8 GB / 4060, Arc A750 | GeForce GTX 1650, Radeon RX 6400 / 6500 XT, and similar laptop GPUs |
| CPU it was paired with | recent 6-performance-core desktop CPU | older 4-core laptop-class CPU |
| What fits on the GPU | the 4B LLM (3.3 GB) **and** speech-to-text (whisper large-v3-turbo, ~2 GB) | the 4B LLM only; speech-to-text runs on the CPU (small.en) |
| **Skills** ("set a timer", weather, TV) | **0.69 s** | **2.54 s** (p90 5.7 s) |
| **Conversation** (the LLM answers) | **1.24 s** | **4.45 s** (p90 6.9 s) |
| Speech-to-text, 11 s clip | 0.35 s | 1.8 s |
| LLM speed, 4B Q4_K_M | 72 tok/s, first token 0.09 s | 24 tok/s, first token ~1 s |
| Speech (Kokoro on the CPU, first chunk) | 0.8 s in real turns; 0.67 s for one new sentence | 0.8 s in real turns (repeated phrases come from the cache); new sentences ~2.4× slower |

On the 4 GB tier, most of the gap comes from two things. Speech-to-text has to
run on the CPU, which costs about 1.5 s, and the LLM runs at a third of the
speed. Skills never wait for the LLM, so they stay usable on either tier.

A CPU-only box wasn't measured. Antigua can run there, but expect conversation
to take several seconds longer than on the 4 GB tier. A backup box from that
class is still worth having: it keeps timers, alarms and skills working while
the GPU box is down or lent out (see [deployment](deployment.md#lending-the-primarys-gpu)).

## Choosing a model

The LLM only writes the conversational reply. Every action (timers, music,
lights, TV) is parsed and run by code, so any small instruct model Ollama
serves will do. The example config uses `qwen3.5:4b`. We happen to run a
Qwen3.5-4B finetune. Pick by what fits:

| VRAM | Start with | Why |
|---|---|---|
| 4 GB | a 3–4B model at Q4 | it fits whole on the GPU, with speech-to-text on the CPU |
| 8 GB | a 3–4B model at Q4 | leaves room for GPU speech-to-text, which saves ~1.5 s per turn |
| 12 GB or more | a 7–8B model at Q4 (not measured here) | better conversation, still beside GPU speech-to-text |

Three rules matter more than which model you pick:

- **All of it in VRAM.** A model that spills even partly to the CPU runs
  several times slower. Ollama's default context (131072 tokens) spilled 22%
  of the 4B. `ollama.num_ctx: 8192` keeps it whole.
- **Keep it loaded.** `keep_alive: -1` plus the warmup unit mean the first
  turn after a reboot isn't a cold load.
- **Leave the prompt alone.** Ollama reuses the cached system prompt between
  turns. Anything that changes it mid-way, such as another program using the
  same model with its own prompt, makes the next turn re-read all of it
  (2–3 s on the 4 GB tier). Point other tools at a different model, or a cloud
  model, so they don't evict Antigua's.

### Swapping models

```bash
server/scripts/use_model.sh --check                 # what each box runs, and whether it fits
server/scripts/use_model.sh qwen3.5:4b              # the primary
server/scripts/use_model.sh --fallback qwen3.5:4b   # the backup (a pinned copy, num_gpu 99)
```

`use_model.sh` pulls the model and loads it. It refuses the swap, and puts the
old model back, unless the new one sits entirely in VRAM. Otherwise it updates
`server.yaml` and the warmup unit, restarts the server, sends a quiet test turn
and prints a short benchmark. To undo, run it again with the old name.

## Measuring your own box

```bash
server/scripts/bench_latency.py                   # LLM, STT and TTS, with idle gaps like real turns
server/scripts/turn_stats.py logs/server.log      # real turns: chat vs skills, each stage's median
```

Every turn writes a `TURN` line with each stage's time, so slow turns show
where the time went (`stt`, `llm_first`, `tts_first`, ...). Hardware tweaks
that made no difference on the 8 GB tier: forcing the GPU's compute power
profile, and the CPU governor (it's already `performance` on most desktops).
Kokoro runs one thread per performance core (`KOKORO_THREADS`, pinned with the unit's CPU affinity).
