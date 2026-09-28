# Spanish Support Plan

**Date:** 2026-09-25 22:13 CDT
**Updated:** 2026-09-25 22:23 CDT: Phase 1 implemented and deployed (see "Phase 1 results").
**Updated:** 2026-09-25 23:17 CDT: Phase 2 implemented and deployed (see "Phase 2 results"), using a single front-end module instead of per-parser patterns.
**Status:** Phases 1 and 2 done, apart from live tests with real Spanish speech and a listen to the voice. Phase 3 not started. Decisions 1 and 2 are still open; decision 3 went with the recommended default (mirror the speaker's language).
**Scope:** Antigua understands Spanish and answers in it: first in open conversation, then for everyday commands. It also covers how she sounds when speaking Spanish.
**Origin:** One of seven "general character" improvements proposed on 2026-09-25, chosen by the owner. The others are listed at the end for reference.

---

## Why

- The household plays Spanish-language music (Bad Bunny).
- Antigua's name and backstory come from Antigua Guatemala.
- Her voice is already a blend that includes `ef_dora`, a Kokoro Spanish voice, to give her English a Latin-American accent.
- Understanding and answering in Spanish expands her character. It isn't a new skill.

## Where things stand (checked 2026-09-25)

| Area | Today | What it means for Spanish |
|---|---|---|
| **STT** | whisper.cpp `large-v3-turbo` q8 on the RX 6650 XT (`antigua-whisper.service`, :8178), started with `-l en`. `faster-whisper small.en` on the CPU is the fallback and does hotword re-hearing. `medium.en` gives a second opinion over music. | `large-v3-turbo` is multilingual. The `.en` CPU models are English-only, so Spanish only works on the GPU path. |
| **Language detection** | Tested `-l auto` on 8 kitchen clips. All were detected as `en`, but confidence dropped on short or noisy clips: "Pause" over music p=0.72, "So bad money" p=0.80, "Yeah" p=0.50. Clear commands scored p≥0.98. | Auto-detect works, but it needs guard rails. One-word clips must not flip language. |
| **Routing** | Every skill parser is English regex: `music_intents.py`, `classify.py` (timers, TV, Govee, volume, time/date, weather, news, sports, calc). | Spanish commands currently fall through to the LLM, which chats about them and doesn't act. |
| **Skill replies** | Deterministic English templates, e.g. `"Playing X"`, `"5-minute timer set."`, `_DIDNT_CATCH` in `classify.py:1486`. | They need Spanish variants. |
| **LLM** | Qwen3.5-4B (uncensored) via Ollama. `max_tokens: 65`, temperature 0.4. The system prompt `server/config/system_prompt.txt` is about 1,100 words, English only, and has no LANGUAGE section. | It can answer in Spanish now. A 4B model's Spanish quality is untested. |
| **TTS** | Kokoro ONNX (`server/kokoro_tts_server_flask.py`) with a fixed blend `{"ef_dora": 0.25, "af_bella": 0.45, "af_heart": 0.30}`. The espeak phonemizer is hard-locked to `en-us`: `phonemize()` raises on any other language. | It can't pronounce Spanish yet. Kokoro supports `es` with espeak, so this is a small change to the server. |
| **Wake word** | openWakeWord "alexa" on the kitchen bridge | Unaffected. |
| **Fallback (the backup)** | `antigua_fallback_server.py` runs `faster-whisper tiny`, which is multilingual, and a backup Kokoro with the same blend. | It needs the same TTS change. Its STT could detect Spanish, but tiny's quality is poor, so the fallback stays English-only. |

## Phase 1: conversation in Spanish

Goal: talk to her in Spanish and she answers in Spanish, in her own voice. Estimate: about half a day.

### 1.1 STT: detect English or Spanish, conservatively
- [x] ~~Start `whisper-server` with `-l auto`~~ **Changed:** the language is set per request instead (see results), so the service keeps `-l en`. `verbose_json` does return every language's probability (`language_probabilities`).
- [x] In `antigua_server._transcribe_gpu`, return a `language` field. Use Spanish only when **all** of these hold, otherwise English (`pipeline.pick_language`):
  - detected language is `es`
  - probability ≥ 0.85, tuned on real clips
  - the transcript has at least 3 words, so "pausa" or "sí" alone never flips the conversation
- [x] Any other language (Portuguese, Italian…) counts as English. The CPU fallback path always returns `en`.
- [x] Borderline: a conversation already in Spanish stays Spanish on any Spanish-leaning turn (p_es > p_en), whatever its length or confidence.

### 1.2 Carry the language through the turn
- [x] Pass `language` from `run_pipeline` into `dispatch_text`, then `_Turn`, then the LLM call and TTS.
- [x] Keep a per-conversation language, so a follow-up turn defaults to whatever the conversation was in (`_conv_language`, 300s TTL, cleared by an English turn).

### 1.3 Prompt
- [x] Add a LANGUAGE section to `system_prompt.txt` (named **SPANISH**, because LANGUAGE already covers swearing): *Reply in the language you were spoken to. If the person mixes languages, you can too. Never translate unless asked. Never comment on which language is being used.* Keep it short, because the 4B model already follows a 1,100-word prompt loosely.
- [x] Also pass the detected language as a one-line hint per turn (e.g. `[The user spoke Spanish]`), which a small model follows more reliably than a general rule.
- [x] Keep the daypart-based prompt cache intact: the per-turn hint goes in the user message, not the system prompt. See memory `llm-prompt-cache`.

### 1.4 TTS in Spanish
- [x] Add a `lang` parameter to `/tts` in `kokoro_tts_server_flask.py` (`"en"` by default, or `"es"`).
- [x] Keep one long-lived espeak backend per language. The per-request backend used to leak a copy of libespeak into `/tmp`; see commit `00e1f4a`.
- [x] Add a Spanish voice blend, mostly `ef_dora` with some of the English voices for continuity, so she sounds like the same person. Proposal to tune by ear: `{"ef_dora": 0.7, "af_heart": 0.3}`.
- [x] Carry the TTS cache key by language (English keys are unchanged, so the existing cache stays warm), so identical text in two languages can't collide.
- [x] Check the AM/PM, number and time formatting rules, which are English-specific (commit `57ce08a`). `clean_for_tts(text, lang)` now keeps only the markdown strip for Spanish: espeak `es` reads digits natively, and the "Antigwa"/"No-ee" fixes are only needed for English phonemes.
- [x] Redeploy to the backup via `deploy_fallback.sh`. Its TTS speaks Spanish, and its STT stays English (tiny).

### 1.5 Tests
- [ ] **Still open:** record about 10 Spanish clips in the kitchen (both speakers, with and without music) plus the existing English set.
- [x] Assert that English clips never come back `es`, and that Spanish clips of 3+ words come back `es`. Done with synthetic Spanish (Kokoro) plus 7 real English kitchen clips; see results. `tests/test_pipeline.py` covers `pick_language`.
- [x] Pipeline test: a Spanish transcript reaches the LLM with the Spanish hint, and TTS is called with `lang="es"`. It also checks that the next turn asks STT to prefer Spanish, and that an English turn switches back.
- [ ] **Still open (owner):** listen to a handful of Spanish TTS replies for pronunciation and voice match. Machine check passed: 4 of 4 Spanish TTS sentences round-tripped through Whisper word for word, detected as Spanish at 0.90–1.00.

### Phase 1 results (2026-09-25)

**Language detection, measured with the final code on the live GPU server:**

| Clip | p(en) | p(es) | Picked | Transcript |
|---|---|---|---|---|
| "Pon música de Bad Bunny, por favor." (TTS) | 0.06 | 0.94 | es | exact |
| "¿Qué hora es?" (TTS) | 0.08 | 0.90 | es | exact |
| "Oye, ¿cómo estuvo tu día hoy?" (TTS) | 0.00 | 1.00 | es | exact |
| "Cuéntame algo interesante sobre Guatemala." (TTS) | 0.01 | 0.98 | es | exact |
| "Pausa." (TTS, one word) | 0.77 | 0.02 | en | "Bowser." (Phase 2 has to handle single-word Spanish) |
| "Pause" over music (kitchen) | 0.72 | 0.01 | en | "Pause." |
| "Play Bad Bunny" (kitchen) | 0.99 | 0.00 | en | exact |
| "Turn on the TV" (kitchen) | 1.00 | 0.00 | en | exact |
| "Yeah" (kitchen) | 0.50 | 0.03 | en | "Yeah." |
| garbled "Play Bad Bunny" (kitchen) | 0.80 | 0.00 | en | "Say bad money." (known miss, not language-related) |
| non-speech (kitchen) | 0.63 | 0.06 | en | "" |

No English clip scored above 0.06 for Spanish.

**Deviation from the plan: STT latency.** whisper-server computes language probabilities with a separate encoder pass, and each pass costs about 0.29s on the RX 6650 XT:

| Request | Time |
|---|---|
| `language=en`, `json` (before Phase 1) | 0.30s |
| `language=en`, `verbose_json` (probabilities) | 0.59s |
| `language=auto`, `json` | 0.59s |
| `language=auto`, `verbose_json` | 0.88s |
| `-nlp` server flag | 0.59s, and it drops the probabilities entirely |

The plan assumed detection would be almost free. It isn't. Chosen design: one `language=en` + `verbose_json` request (English transcript plus probabilities), and only for a turn picked as Spanish, a second `language=es` + `json` request. Result: **English turns take 0.59s (+0.29s), Spanish turns 0.91s.** `language=auto` was rejected because a garbled clip can come back transcribed in another language: "Pause" over music gave Turkish p=0.16. Detecting on the CPU instead, in parallel, was tried and rejected: faster-whisper `base` misdetected clear Spanish (es=0.02 on es1), and `small` was accurate but took 0.65s, longer than the GPU pass.

**The 4B model's Spanish (spot check, real system prompt + hint):**
- "¿Cuántas tazas hay en un galón?" → "Dieciséis." Correct and terse, as the FACTUAL ANSWERS rule asks.
- "Cuéntame algo interesante sobre Guatemala." → fluent and in character, but **factually wrong** ("the first country in the Americas to declare independence") and **cut off mid-sentence** ("…Es un") by `max_tokens: 65`. Spanish takes more tokens per word, so the cap cuts Spanish replies sooner.
- "Cuéntame un chiste." → a joke in Spanish that doesn't land.
- "Oye, ¿cómo estuvo tu día?" late at night → "Buen día, ¿tú cómo estás?", the wrong greeting for the time.

These are model-size and token-cap limits, not Phase 1 bugs. Improvements #2 (raise `max_tokens`) and #1 (a bigger model) matter more now.

**What changed (files):**
- `server/antigua_server.py`: `_whisper_gpu` / `_transcribe_gpu` (two-step EN→ES); `transcribe(..., prefer=)`; `ask_llm_stream(..., language=)` adds the per-turn hint; `synthesize(text, lang=)` with a language-aware cache key; `synthesize_remote` passes `lang`
- `server/antigua_core/pipeline.py`: `pick_language()`, `_conv_language` stickiness, `_Turn.language`, language passed to the LLM and TTS; `medium.en` second opinion skipped for Spanish turns
- `server/antigua_core/tts_text.py`: `clean_for_tts(text, lang)`
- `server/kokoro_tts_server_flask.py`: `BLENDS` per language, per-language espeak backends, `lang` request field, warms both voices at start
- `server/antigua_fallback_server.py`: same interface (`prefer=`, `language=`, `lang=`); STT stays English
- `server/config/system_prompt.txt`: SPANISH section
- `server/config/server.yaml`: `whisper.spanish_min_prob: 0.85`, `whisper.spanish_min_words: 3`
- `server/antigua-whisper.service`: comment only; the language is set per request
- `tests/test_pipeline.py`: `pick_language` cases and the Spanish turn flow; synthesize stubs take `lang`

**Deployed:** antigua-tts and antigua-server restarted on the primary; the backup redeployed with `deploy_fallback.sh`. All nine test suites pass.

## Phase 2: Spanish commands

Goal: the everyday commands work in Spanish and get Spanish confirmations. **Done 2026-09-25 23:17 CDT.**

### Design change from the plan

The plan said to put Spanish patterns next to the English ones in each skill parser. That turned out to mean touching about 30 routes, each with its own English number, unit and time parsing. Instead, **one front-end module, `server/antigua_core/spanish.py`**, handles it:

- `to_english_command(text)` rewrites a Spanish command into the English command the existing parsers already handle ("pon un temporizador de cinco minutos" → "set a timer for 5 minutes"). `dispatch_text` runs it first; on a match the turn continues as that English command, marked Spanish. All the tested skill logic is reused, and Spanish lives in one place.
- `reply_in_spanish(reply)` turns the skill's English reply back into Spanish with templates. `_speak` applies it on Spanish turns. With no template, `Backend.translate_to_spanish` (the local 4B model, `/api/generate`, temperature 0, ~0.5–0.75s) translates it. If that fails, the reply is spoken in English with the English voice. Spanish turns skip the pre-recorded English static WAVs (TV replies).
- **A match is also language evidence.** STT only picks Spanish for clips of 3+ words (Phase 1), but "Siguiente canción" comes through as Spanish text even from the English transcription, so a matched command makes the turn Spanish. Measured with the Spanish voice: "Pausa la música" (en 0.03 / es 0.96), "Siguiente" (0.01 / 0.99), "Apaga las luces" (0.00 / 0.99) all transcribe as Spanish text even when forced to English.

### 2.1 Commands (in `spanish.py`)
- [x] **Music:** *pon / ponme / reproduce / toca X* (+ *en la sala / cocina / barra de sonido / cuarto*), *pausa*, *para / apaga la música*, *sigue / continúa*, *siguiente*, *anterior*, *ponla otra vez*, *súbele / bájale (a la música)*, *pon la música al 40*, *pon el último álbum de X*, *pon la canción X de Y*, *qué canción es esta*.
- [x] **Timers and alarms:** *pon un temporizador de cinco minutos / media hora*, *cancela el temporizador*, *cuánto le falta*, *despiértame a las seis y media (de la mañana)*, *ponme una alarma a las ocho menos cuarto de la noche*. Spanish number words 0–99 with a small table (`_num`).
- [x] **Time and date:** *qué hora es*, *qué día es hoy*.
- [x] **Weather:** *va a llover (hoy / mañana)*, *cómo está el clima*, *qué temperatura hace*, *hace calor / frío*.
- [x] **TV:** *prende / apaga / pon la tele*, *silencia la tele*, *súbele a la tele*, *pon / abre Netflix (en la tele)*.
- [x] **Lights:** *prende / apaga las luces (del pasillo)*, *pon las luces en azul*, *pon el candelabro rojo*, *pon las luces al cincuenta por ciento*. Note: the lights are fixtures, not rooms (hallway, chandelier, TV bar, pink lights, neon rope, monitor strip), so *las luces de la sala* isn't a thing even in English.
- [x] **Volume:** *súbele*, *bájale*, *más fuerte*, plus STT's usual spellings (*bachale*, *zubele*).
- [x] Guards: *pon atención a…* and anything over 6 words isn't a music request; a bare *otra vez* ("say that again") isn't a restart; English *play / pause / stop* are left alone.
- [x] Music name re-hearing: the re-transcription runs through `to_english_command` too, so "pon música de X" re-heard still parses.

### 2.2 Replies
- [x] Templates (`_REPLIES`, `_FIXED`): time ("Son las 11:13 de la noche", "Es la 1:05 de la tarde"), date ("Hoy es viernes 25 de septiembre"), timers ("Temporizador de 1 minuto y 30 segundos"), alarms ("Alarma para mañana a las 6:30 de la mañana"), cancels, music ("Poniendo Un Verano Sin Ti, de Bad Bunny en la barra de sonido"), music volume, every TV reply, lights power/color/brightness, and the "can't reach" / didn't-catch messages.
- [x] Everything else is translated by the 4B model (weather, calc, sports, …). The prompt tells it that a bare number in weather is a temperature: without that, "It's 89" came back as "Son las 89", a clock time.
- [x] Chosen by the turn's language (`_speak`).

### 2.3 Tests
- [x] New `tests/test_spanish.py`: 52 Spanish commands (each asserted to become the exact English command *and* route to the right skill), 10 non-commands (Spanish chat, and English commands that must stay English), and 25 reply templates.
- [x] `tests/test_pipeline.py`: a Spanish command answered in Spanish with Spanish TTS; an untemplated reply goes through the translator; a failed translation falls back to English.
- [x] ~~Add Spanish utterances to `tests/fixtures/routing.yaml`~~: `test_spanish.py` asserts the route for every command instead.

### Phase 2 results (2026-09-25)

End to end through the live server (`POST /pipeline`, replies sent to an unused MQTT topic so nothing played), with Spanish TTS clips as input:

| Said | Understood as | Reply | Total |
|---|---|---|---|
| ¿Qué hora es? | what time is it | Son las 11:16 de la noche. | 1.4s |
| ¿Va a llover mañana? | will it rain tomorrow | Mañana se ve seco, solo unos 0 por ciento. (LLM-translated) | 2.0s |
| Pon un temporizador de cinco minutos. | set a timer for 5 minutes | Temporizador de 5 minutos. | 1.4s |
| Cancela el temporizador. | cancel the timer | Cancelé el temporizador. | 1.3s |
| Oye, ¿cómo estuvo tu día? | (chat, stays Spanish) | Todo bien, ¿y tú? | 3.0s |

Commands that act on devices (music, TV, lights) were checked for routing only, not run, to avoid playing music or turning on the TV while nobody was home.

**Deployed:** antigua-server restarted; the backup redeployed. The fallback has the front-end and templates but no translator (untemplated replies stay English there). All ten test suites pass.

### Known limits and open items
- **One-word Spanish is unreliable in STT itself.** "Pausa" alone came back as "Bowser" and "Súbele" as "Zubelé" (the latter is aliased). Two words ("pausa la música") work. The reSpeaker button as play/pause is the robust fix for the most common one.
- **Names lose accents and capitals** in the English command ("play titi me pregunto"). Music search is accent-insensitive, so this doesn't affect results, and replies use the catalogue's own spelling.
- **Translation quality is the 4B model's:** occasional slips ("una galón", "diez y seis"). Improvement #1 would help.
- **Pre-existing English reply bugs** showed up and are now translated faithfully: "You don't have a the timer.", "Cancelled all 2." when "cancel the timer" meets two timers, and weather's "only about 0 percent". Fixing the English fixes the Spanish.
- **Still to test live:** real household voices saying these commands, with and without music, and a listen to the Spanish voice.

## Phase 2 dry run (2026-09-26)

`tests/dry_run_live.py` runs typed utterances through the real pipeline in its own process: the Spanish front-end, skills, LLM, translator and Kokoro. Music Assistant reads go through; every write (play, pause, volume, duck), every MCP call (TV, lights) and every MQTT publish is recorded instead of sent. Nothing plays.

28 Spanish commands: every music, TV and lights command resolved to the right action (for example, "Pon el candelabro rojo" → the 6 chandelier lights to red). Time, date, timer set/cancel and alarm were right too. Found:
- **Bug:** "¿Cuánto le falta al temporizador?" → "No tienes el temporizador" with a timer running. English has it too: `format_timer_status_reply` does `find("the timer")`.
- **Translator:** "Hace 88 afuera, parece que hace 98…" has no *grados* and no *sensación térmica*.
- **LLM fact error:** "Guatemala fue el primer país en América Latina en proclamar su independencia" (Haiti was first, 1804). The 4B model's limit (improvement #1).
- **Harness flaw, fixed 2026-09-27:** the first run persisted its test alarm to the live `data/timers.json` (cleared by hand). The harness now redirects the timer, memory, list and speaker-profile stores to scratch copies; verified with checksums of `data/*.json` before and after a run.

## Phase 3: polish before rollout (neutral Latin American Spanish)

**Decision 2 answered 2026-09-26: neutral Latin American Spanish.** Goal: she sounds like a Latin American Spanish speaker, phrases things like one, and switches language reliably. **Paused 2026-09-26.** Status: 3.1's es-419 switch and Spanish text normalization are coded and tested but **not deployed**; everything else below is not started.

### What existing projects do (research, 2026-09-26)

| Project | What it does | Fit for us |
|---|---|---|
| **Alexa multilingual mode** (US en/es since 2019) | Detects the language of each utterance and answers in it. For Spanglish, it follows the language the command started in. | Same model as our Phase 1 (mirror per turn, with stickiness). Adopt the Spanglish rule. |
| **espeak-ng `es-419`** (Latin American Spanish) | The phonemizer Kokoro already uses. `es` is Castilian. | **The biggest accent problem, fixed with one line.** See below. |
| **Kokoro Spanish voices** (ef_dora, em_alex) | Trained on Latin American speakers (hexgrad/kokoro#246). | We currently drive a Latin American voice with Castilian phonemes, which gives a mismatched accent. |
| **Home Assistant `intents` (es)** | Community Spanish command sentences and replies. | Replies are Spain-leaning ("Lo siento, no he entendido", "planta", present perfect "se han encendido"). Its skip words ("porfa", "me puedes", "podrías", "podés") are useful. A cross-check list, not a drop-in. |
| **Piper `es_MX-claude-high`** | Fast, neutral Latin American (Mexican) neural voice, runs on a Pi. | A different person's voice. Only worth it as an emergency fallback voice. |
| **Chatterbox Multilingual V3** (Resemble AI, MIT) | 23 languages plus a dedicated **Latin American Spanish** pack, zero-shot cloning from about 10s of reference audio. | The long-term upgrade: clone Antigua's English voice into real Latin American Spanish. Costs GPU memory on the RX 6650 XT (ROCm/PyTorch, next to whisper.cpp and Ollama) and latency. Separate experiment, not a blocker. |
| **lingua-py** (text language ID, offline) | 1–5-gram models built for short text; can be restricted to en/es. | Cheap second detection signal on the transcript (see 3.3). |
| **Latam-GPT** (CENIA, Chile, Feb 2026) | Open LLM trained on Latin American Spanish. | Too big for the primary next to STT/TTS. Revisit with improvement #1. |

### 3.1 Accent (TTS)
- [x] (coded, not deployed) **espeak `es` → `es-419`** in `kokoro_tts_server_flask.py` `ESPEAK_LANG` (and the backup's copy). Measured: `es` gives Castilian *θinko, koθina, luθes, ʎamame*; `es-419` gives *sinko, kosina, luses, ʝamame*. Kokoro's vocab has every `es-419` phoneme. A/B clip rendered offline.
- [ ] Re-tune the blend by ear **after** the phoneme fix: part of what sounded "off" was the lisp, not the voice. Rendered A = dora 0.55, B = 0.70 (current), C = 0.85 (bella:heart 2:1). **Waiting on the owner's pick**; A was suggested.
- [x] (coded, not deployed; 18 cases in `test_spanish.py`) **Spanish text normalization** in `clean_for_tts(text, "es")`, with `num2words(lang="es")` (already installed). espeak reads "12:58" as "doce: cincuenta y ocho". Times become "las doce y cincuenta y ocho", "la una y cuarto", "mediodía" / "medianoche"; temperatures become "88 grados"; also percentages and ordinals ("26 de septiembre").
- [ ] Later, as a separate experiment: Chatterbox Multilingual (Latin American pack) cloned from an Antigua reference clip. Measure VRAM and latency before touching the live stack.

### 3.2 Phrasing and slang (templates, translator, LLM)
- [ ] **Style guide** (short, at the top of `spanish.py`, reused in the translator prompt and in the system prompt's SPANISH section):
  - *tú* always: never *vos*, *usted* or *vosotros*.
  - Latin American vocabulary: *prender / apagar*, *celular*, *computadora*, *carro*, *jugo*, *la tele*.
  - No regional slang: no *chido / padre / ahorita* (Mexico), *chévere* (Caribbean/Colombia), *vale / guay / tío / coger* (Spain), *¿mande?*.
  - Preterite over present perfect for things just done: *"Listo, prendí las luces"*, not *"He encendido las luces"*.
  - Warm but short, same persona as English.
- [ ] **Rewrite the templates away from calques.** "Poniendo X" and "Prendiendo la tele" mirror English "Playing X". Proposed: *"Listo, te pongo Bad Bunny."*, *"Ya prendí la tele."*, *"Listo, apagué las luces."*, *"Temporizador de cinco minutos, listo."*, *"Son las doce y cincuenta y ocho."* / *"Es la una y cuarto de la tarde."*, *"Mañana no se ve lluvia, apenas un tres por ciento."*
- [ ] Templates for the common weather replies (temperature / feels like, rain chance), so the translator isn't the path for the most frequent Spanish question. Glossary in the translator prompt: *grados, sensación térmica, pronóstico, chubascos, humedad*.
- [ ] Borrow Home Assistant's Spanish skip words for the command front-end ("porfa", "me puedes", "podrías", "te importa", "podés"), so polite commands still match.
- [ ] System prompt SPANISH section: 2–3 lines of the style guide. The per-turn hint `[The user spoke Spanish]` becomes `[Reply in neutral Latin American Spanish, tú]`.
- [ ] Fix the English bugs that surface in Spanish: timer status "the timer", "Cancelled all 2.", "only about 0 percent".
- [ ] **Native review sheet:** about 40 Spanish replies (all templates plus 10 LLM/translator samples), with audio, for the household to mark ✓/✗ and correct in one pass.

### 3.3 Detection
Today: forced-English whisper pass with `verbose_json` (+0.29s on every English turn), Spanish when p_es ≥ 0.85 and 3+ words, or the conversation is already Spanish, or a Spanish command matched.
- [ ] **Text language ID as a second signal.** Forced-English whisper still writes Spanish words for Spanish speech (measured in Phase 2: "Pausa la música", "Siguiente", "Apaga las luces"). Add lingua-py restricted to {en, es} on the transcript. Rule: Spanish if audio p_es ≥ 0.85, **or** text says es with high confidence on 2+ words, **or** a command matched. Measure on the clip set. If text ID alone is as accurate, drop `verbose_json` and **win back the 0.29s** on English turns.
- [ ] **Spanglish:** reply in the language the utterance *starts* in (Alexa's rule), unless most of the words are the other language. Names (Bad Bunny, Netflix) don't count.
- [ ] One-word Spanish commands ("Pausa" → "Bowser"): keep them on the reSpeaker button, and add the known mishearings as aliases only when they can't collide with English.
- [ ] **Clip set before rollout** (still open from Phase 1): about 10 Spanish plus 10 English kitchen clips per speaker, with and without music. Every detection change is judged on it: no English clip may flip to Spanish.

### 3.4 Validation (no speakers touched)
- [x] Fix the harness first: scratch copies for timers, memories, lists and speaker profiles (2026-09-27).
- [ ] Rerun `tests/dry_run_live.py` after each step and keep its WAVs. Unit tests: `test_spanish.py` (templates, style-guide words banned from templates), `test_pipeline.py` (detection rule).
- [ ] Rollout only after the native review sheet passes and no English clip flips.

**Order and estimate:** 3.4 harness fix → 3.1 es-419 + normalization (≈1h) → 3.2 style guide, templates, weather, bugs (≈2h) → review sheet → 3.3 text language ID + clip set (≈2h plus recording) → blend tuning by ear → rollout. Chatterbox is a separate experiment afterwards.

## Open decisions (owner)

1. **Who speaks Spanish to her:** which household members? This sets how much Phase 2 matters and whose voices go into the test clips.
2. ~~Guatemalan or neutral Latin American Spanish~~ **Neutral Latin American (2026-09-26).**
3. **Reply language:** always mirror the language she's spoken to (recommended), or understand Spanish but always answer in English.

## Risks

- **False language flips.** Short, noisy clips have low detection confidence (p=0.50–0.72 measured). Mitigation: the 3-word minimum, the 0.85 threshold, and conversation-level stickiness. A wrong flip makes her answer an English command in Spanish.
- **4B model's Spanish.** Quality is untested. The Phase 1 tests will show it, and the bigger model (improvement #1) would help.
- **Spanglish within one sentence** ("pon la de Bad Bunny that goes…"). Whisper handles mixed speech poorly. Phase 1 assumes one language per turn.
- **Music names** are unaffected: artist and song re-hearing (Deezer plus hotwords) works on names in any language. The hotword pass runs on the English-only `small.en`, though, so a Spanish request that needs re-hearing may need the multilingual `small` model.
- **Latency.** ~~Language detection happens inside the same STT pass, so the cost should be negligible.~~ **Measured:** +0.29s on every turn (one extra encoder pass); see Phase 1 results. To win it back: patch whisper-server to reuse the encoder output for detection, or skip detection on turns that clearly route to an English skill.
- **Fallback server.** It gets Spanish TTS but not Spanish STT (tiny is too weak), so during a failover Antigua is English-only.

## Files expected to change

- `server/antigua-whisper.service`: `-l auto`
- `server/antigua_server.py`: `_transcribe_gpu` returns the language; language passed to the LLM and TTS
- `server/antigua_core/pipeline.py`: language on `_Turn` and the conversation; Spanish reply templates
- ~~`server/antigua_core/classify.py`, `music_intents.py`: Spanish patterns (Phase 2)~~ `server/antigua_core/spanish.py` (new) instead; see Phase 2
- `server/config/system_prompt.txt`: LANGUAGE section, plus Phase 3 lines
- `server/kokoro_tts_server_flask.py`: `lang` parameter, per-language espeak backend and voice blend
- `server/config/server.yaml`: detection threshold and Spanish voice blend as settings
- `tests/`: routing fixtures, pipeline tests, STT language tests

---

## Context: the other improvements proposed on 2026-09-25

These were proposed alongside this plan to improve Antigua's general conversation and personality. #6 is this plan, chosen first.

1. **A bigger LLM.** Qwen3.5-4B follows the long persona prompt loosely. On 2026-09-25 it riffed on garbled STT ("That's a reference to the idiom about how bad money…") instead of saying it didn't catch it, which the prompt tells it to do. Dropping the unused vision projector (~0.9 GB VRAM) may make room for a ~9B model next to whisper.cpp. Benchmark latency first.
2. **Raise `max_tokens` from 65.** It caps every chat reply at about 45 words and contradicts the prompt's "never cut a good answer short". Suggest 150–200.
3. **Stable tastes and opinions.** A short fixed "who I am" section so questions like "what's your favorite album?" get consistent answers.
4. **Memory that builds a relationship.** Facts pulled from conversation, approved by the user, and brought up naturally. Today's memory only stores explicit "remember" requests for 14 days.
5. **Knowing who's talking.** Speaker ID is built but deferred (nobody enrolled; see memory `speaker-id-deferred`).
6. **Spanish.** This plan.
7. **A conversation and persona test set.** 20–30 graded prompts (a joke, a fact, garbled input, late night, an opinion) to catch regressions when the model or prompt changes. It's a prerequisite for #1 and #3, and Phase 3 here uses it.

Recommended order after this plan: #2 (one line), #7, then #1 tested against it, then #3 and #4.
