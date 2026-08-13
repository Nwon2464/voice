"""Standalone WSL validation of native Windows global F8/F9 transport."""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time

from windows_port.bridge_client import WindowsBridgeClient


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Receive native Windows global F8/F9 events through the bridge."
    )
    parser.add_argument("--seconds", type=float, default=30.0)
    args = parser.parse_args(argv)
    if args.seconds <= 0:
        parser.error("--seconds must be positive")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    events = []
    statuses = []
    errors = []
    ready = threading.Event()
    print_lock = threading.Lock()

    def fail(error) -> None:
        errors.append(str(error))
        ready.set()

    def on_status(status) -> None:
        statuses.append(status)
        if status.get("event") == "ready":
            ready.set()
        elif status.get("event") in ("error", "hotkey_error"):
            fail(status.get("error", "unknown Windows bridge error"))

    def on_hotkey(event) -> None:
        events.append(event)
        with print_lock:
            print(f"[hotkey] {event['key']}", flush=True)

    bridge = WindowsBridgeClient(
        lambda _pcm: None,
        on_status,
        fail,
        on_hotkey=on_hotkey,
        capture_audio=False,
    )
    started = time.perf_counter()
    try:
        bridge.start()
        if not ready.wait(timeout=10):
            fail("Windows bridge did not become ready within 10 seconds")
        if not errors:
            print(
                "Focus Chrome/another Windows application and press F8/F9.",
                flush=True,
            )
            time.sleep(args.seconds)
    except KeyboardInterrupt:
        errors.append("interrupted")
    except Exception as error:
        errors.append(str(error))
    finally:
        bridge.stop()

    report = {
        "ok": any(status.get("event") == "ready" for status in statuses) and not errors,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "f8_count": sum(event["key"] == "F8" for event in events),
        "f9_count": sum(event["key"] == "F9" for event in events),
        "hotkey_events": events,
        "statuses": statuses,
        "errors": errors,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
