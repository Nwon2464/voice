"""Standalone WSL probe for Windows WASAPI loopback PCM capture."""

from __future__ import annotations

import argparse
import json
import math
import threading
import time
import wave
from pathlib import Path

import numpy as np

from windows_port.audio import CHANNELS, SAMPLE_RATE, SAMPLE_WIDTH_BYTES
from windows_port.bridge_client import WindowsBridgeClient


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Capture Windows system output through WASAPI from WSL."
    )
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--output", type=Path, default=Path("wasapi_probe.wav"))
    args = parser.parse_args(argv)
    if args.seconds <= 0:
        parser.error("--seconds must be positive")
    return args


def pcm_statistics(pcm: bytes) -> dict[str, float | int | bool]:
    """Return signal measurements for little-endian signed-int16 mono PCM."""
    if len(pcm) % SAMPLE_WIDTH_BYTES:
        raise ValueError("PCM must contain complete signed-int16 samples")
    samples = np.frombuffer(pcm, dtype="<i2")
    if not samples.size:
        return {"rms": 0.0, "peak": 0, "audio_detected": False}
    wide_samples = samples.astype(np.int32)
    rms = math.sqrt(float(np.mean(wide_samples.astype(np.float64) ** 2)))
    peak = int(np.max(np.abs(wide_samples)))
    return {"rms": round(rms, 1), "peak": peak, "audio_detected": rms >= 10.0}


def save_wav(path: Path, pcm: bytes) -> None:
    """Write the bridge's exact 16 kHz mono s16le payload as a WAV file."""
    if len(pcm) % SAMPLE_WIDTH_BYTES:
        raise ValueError("PCM must contain complete signed-int16 samples")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(CHANNELS)
        output.setsampwidth(SAMPLE_WIDTH_BYTES)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(pcm)


def main(argv=None) -> int:
    args = parse_args(argv)
    chunks = []
    statuses = []
    errors = []
    ready = threading.Event()

    def on_status(status):
        statuses.append(status)
        if status.get("event") == "ready":
            ready.set()
        elif status.get("event") in ("error", "audio_error"):
            errors.append(status.get("error", "unknown Windows bridge error"))
            ready.set()

    bridge = WindowsBridgeClient(
        chunks.append,
        on_status,
        lambda error: (errors.append(str(error)), ready.set()),
    )
    started = time.perf_counter()
    try:
        bridge.start()
        if not ready.wait(timeout=10):
            errors.append("Windows bridge did not become ready within 10 seconds")
        if not errors:
            print(f"Play Windows audio for {args.seconds:g} seconds.", flush=True)
            time.sleep(args.seconds)
    except Exception as error:
        errors.append(str(error))
    finally:
        bridge.stop()

    pcm = b"".join(chunks)
    measurements = pcm_statistics(pcm)
    output = None
    if pcm:
        save_wav(args.output, pcm)
        output = str(args.output.resolve())
    report = {
        "ok": bool(pcm) and measurements["audio_detected"] and not errors,
        "statuses": statuses,
        "errors": errors,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "captured_seconds": round(
            len(pcm) / (SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH_BYTES), 3
        ),
        "bytes": len(pcm),
        **measurements,
        "output": output,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
