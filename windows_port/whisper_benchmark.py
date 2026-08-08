"""Measure Whisper small startup and transcription on a probe WAV."""

import argparse
import json
import sys
import time
import wave
from pathlib import Path

from audio_utils import SAMPLE_RATE, SAMPLE_WIDTH
from transcription import load_whisper, transcribe_pcm


def parse_args():
    parser = argparse.ArgumentParser(description="Benchmark faster-whisper on a WAV.")
    parser.add_argument("wav", type=Path)
    parser.add_argument("--model", default="small")
    parser.add_argument("--language", default="en")
    parser.add_argument("--repeat", type=int, default=3)
    return parser.parse_args()


def read_pcm(path):
    with wave.open(str(path), "rb") as file:
        actual = {
            "channels": file.getnchannels(),
            "sample_width": file.getsampwidth(),
            "sample_rate": file.getframerate(),
        }
        expected = {
            "channels": 1,
            "sample_width": SAMPLE_WIDTH,
            "sample_rate": SAMPLE_RATE,
        }
        if actual != expected:
            raise ValueError(f"WAV format must be {expected}, got {actual}")
        return file.readframes(file.getnframes())


def main():
    args = parse_args()
    try:
        pcm = read_pcm(args.wav)
        model, startup = load_whisper(args.model, args.language)
        runs = []
        for _ in range(args.repeat):
            started = time.perf_counter()
            text = transcribe_pcm(model, pcm, args.language)
            runs.append({
                "seconds": round(time.perf_counter() - started, 3),
                "text": text,
            })
        report = {
            "ok": True,
            "model": args.model,
            "language": args.language,
            "audio_seconds": round(len(pcm) / 32_000, 3),
            "load_seconds": round(startup["load_seconds"], 3),
            "warmup_seconds": round(startup["warmup_seconds"], 3),
            "startup_seconds": round(startup["startup_seconds"], 3),
            "runs": runs,
        }
    except Exception as error:
        report = {"ok": False, "error": str(error)}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
