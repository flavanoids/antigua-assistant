#!/usr/bin/env python3
"""Offline speaker enrollment for the speaker-ID skill (Phase 4).

Extracts a SpeechBrain ECAPA-TDNN embedding from each given WAV and stores
the mean (centroid) as that person's profile in
data/speaker_profiles.json — the same file antigua_server.py reads at
runtime via antigua_core.speaker_id.SpeakerProfiles.

Usage:
    python3 enroll_speaker.py enroll Alex audio_in/alex/*.wav
    python3 enroll_speaker.py enroll Katherine audio_in/katherine/*.wav
    python3 enroll_speaker.py list

5-10 short utterances per person is enough (per the Phase 4 plan). Each
utterance should be >= 1.5s of clear, isolated speech — the same clips
recorded via a normal conversation with Antigua work fine; they don't need
to be scripted. Re-running `enroll` for a person replaces their profile
with the new set of clips (it does not average with what was there before).

This script is deliberately standalone — it does NOT import antigua_server.py,
which would connect to MQTT, load config, and start background threads. It
only needs antigua_core.speaker_id plus SpeechBrain directly.
"""

import argparse
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from antigua_core.speaker_id import SpeakerProfiles, cosine_similarity  # noqa: E402

MIN_AUDIO_S = 1.5


def _wav_duration_s(path: str) -> float:
    with wave.open(path, "rb") as wf:
        return wf.getnframes() / wf.getframerate()


def _extract_embedding(model, path: str) -> list[float]:
    import soundfile as sf
    import torch

    data, sr = sf.read(path, dtype="float32")
    if data.ndim > 1:
        data = data.mean(axis=1)
    sig = torch.from_numpy(data).unsqueeze(0)
    if sr != 16000:
        import torchaudio
        sig = torchaudio.functional.resample(sig, sr, 16000)
    return model.encode_batch(sig).squeeze().tolist()


def _load_model():
    from speechbrain.inference.speaker import EncoderClassifier

    print("Loading SpeechBrain ECAPA-TDNN (first run downloads ~20MB)...")
    return EncoderClassifier.from_hparams(
        source="speechbrain/spkrec-ecapa-voxceleb",
        savedir=str(Path(__file__).parent.parent.parent / "models" / "spkrec-ecapa-voxceleb"),
    )


def cmd_enroll(args):
    profiles = SpeakerProfiles()
    model = _load_model()

    embeddings = []
    for path in args.wavs:
        duration = _wav_duration_s(path)
        if duration < MIN_AUDIO_S:
            print(f"  SKIP {path} — {duration:.1f}s, below the {MIN_AUDIO_S}s floor")
            continue
        emb = _extract_embedding(model, path)
        embeddings.append(emb)
        print(f"  OK   {path} — {duration:.1f}s")

    if len(embeddings) < 3:
        print(f"\nOnly {len(embeddings)} usable clip(s) — need at least 3 for a stable "
              f"centroid. Record more (5-10 recommended) and try again.")
        return 1

    # Self-consistency check: how well does each clip agree with the
    # resulting centroid? A low outlier usually means a mislabeled or noisy
    # file, not a person who "sounds different" — flag it rather than
    # silently averaging it in.
    dims = len(embeddings[0])
    centroid = [sum(e[d] for e in embeddings) / len(embeddings) for d in range(dims)]
    sims = [cosine_similarity(e, centroid) for e in embeddings]
    for path, sim in zip((p for p in args.wavs if _wav_duration_s(p) >= MIN_AUDIO_S), sims):
        flag = "  <- low agreement, consider re-recording or removing" if sim < 0.6 else ""
        print(f"  similarity to centroid: {sim:.3f}  {Path(path).name}{flag}")

    profiles.enroll(args.person, embeddings)
    print(f"\nEnrolled {args.person} from {len(embeddings)} clip(s).")
    print("Restart antigua-server for the new profile to take effect "
          "(SpeakerProfiles loads once at startup).")
    return 0


def cmd_list(args):
    profiles = SpeakerProfiles()
    people = profiles.enrolled_people()
    if not people:
        print("No enrolled speakers yet.")
    else:
        print("Enrolled:", ", ".join(people))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_enroll = sub.add_parser("enroll", help="Enroll (or re-enroll) a person from WAV clips")
    p_enroll.add_argument("person", help="e.g. Alex or Katherine")
    p_enroll.add_argument("wavs", nargs="+", help="Paths to WAV clips (shell-expanded globs work)")
    p_enroll.set_defaults(func=cmd_enroll)

    p_list = sub.add_parser("list", help="Show currently enrolled speakers")
    p_list.set_defaults(func=cmd_list)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
