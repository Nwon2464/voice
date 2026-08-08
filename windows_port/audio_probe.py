"""Probe the native Windows audio/F8 bridge from WSL."""

import argparse
import json
import sys
import threading
import time
from pathlib import Path

import numpy as np

from transcription import save_wav
from windows_port.bridge_client import WindowsBridgeClient


def parse_args():
    parser = argparse.ArgumentParser(
        description="Capture Windows WASAPI output and count global F8 events from WSL.",
    )
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--output", type=Path, default=Path("wasapi_probe.wav"))
    return parser.parse_args()


def main():
    args = parse_args()
    chunks = []
    statuses = []
    errors = []
    f8_events = []
    ready = threading.Event()

    def on_status(status):
        statuses.append(status)
        if status.get("event") == "ready":
            ready.set()
        elif status.get("event") in ("error", "audio_error"):
            errors.append(status.get("error", "unknown bridge error"))
            ready.set()

    bridge = WindowsBridgeClient(
        chunks.append,
        lambda: f8_events.append(time.time()),
        on_status,
        lambda error: (errors.append(str(error)), ready.set()),
    )
    started = time.perf_counter()
    try:
        bridge.start()
        if not ready.wait(timeout=10):
            errors.append("Windows bridge did not become ready within 10 seconds")
        if not errors:
            print(
                f"Play Windows audio and press F8 within {args.seconds:g} seconds.",
                flush=True,
            )
            time.sleep(args.seconds)
    except Exception as error:
        errors.append(str(error))
    finally:
        bridge.stop()

    pcm = b"".join(chunks)
    samples = np.frombuffer(pcm, dtype=np.int16) if pcm else np.array([])
    rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2))) if samples.size else 0.0
    peak = int(np.max(np.abs(samples.astype(np.int32)))) if samples.size else 0
    if pcm:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        save_wav(args.output, pcm)
    report = {
        "ok": bool(pcm) and not errors,
        "statuses": statuses,
        "errors": errors,
        "f8_events": len(f8_events),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "captured_seconds": round(len(pcm) / 32_000, 3),
        "bytes": len(pcm),
        "rms": round(rms, 1),
        "peak": peak,
        "audio_detected": rms >= 10,
        "output": str(args.output.resolve()) if pcm else None,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
