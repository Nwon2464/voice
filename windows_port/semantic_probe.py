"""Standalone Windows WASAPI + global-hotkey Moonshine semantic validation."""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time

from moonshine_streaming_worker import MoonshineStreamingWorker
from windows_port.bridge_client import WindowsBridgeClient
from windows_port.moonshine_probe import (
    PcmCursorForwarder,
    finalize_drain,
    stop_bridge_and_drain,
)
from windows_port.semantic_controller import SemanticCommitController


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Validate Windows PCM + global F8/F9 Moonshine semantic commits."
    )
    parser.add_argument("--language", choices=("en", "ja"), default="en")
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--drain-timeout", type=float, default=20.0)
    parser.add_argument("--commit-timeout", type=float, default=30.0)
    args = parser.parse_args(argv)
    if args.seconds <= 0 or args.drain_timeout <= 0 or args.commit_timeout <= 0:
        parser.error("--seconds, --drain-timeout, and --commit-timeout must be positive")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    errors = []
    statuses = []
    worker_ready = threading.Event()
    bridge_ready = threading.Event()
    failure = threading.Event()
    stopping = threading.Event()
    print_lock = threading.Lock()
    controller_ref = {}

    def emit(record) -> None:
        if record["event_type"] == "silence":
            label = "silence"
        else:
            label = record["hotkey_key"]
        with print_lock:
            print(f"[{label}] " + json.dumps(record, ensure_ascii=False), flush=True)

    def report_error(source, error) -> None:
        if stopping.is_set():
            return
        message = f"{source}: {error}"
        errors.append(message)
        failure.set()
        with print_lock:
            print(message, file=sys.stderr, flush=True)

    def on_ready(info, error) -> None:
        if error is not None:
            report_error("moonshine startup", error)
            return
        worker_ready.set()
        with print_lock:
            print("Moonshine ready: " + json.dumps(info, ensure_ascii=False), flush=True)

    worker = MoonshineStreamingWorker(
        on_ready,
        lambda _preview: None,
        lambda error: report_error("moonshine", error),
        on_auto_commit=lambda result, error: controller_ref["controller"].on_silence_segment(result, error),
        language=args.language,
    )
    forwarder = PcmCursorForwarder(worker)
    controller = SemanticCommitController(worker, forwarder, emit=emit)
    controller_ref["controller"] = controller

    def on_pcm(pcm) -> None:
        try:
            if not stopping.is_set() and not forwarder.submit(pcm):
                report_error("moonshine PCM submission", "worker is not accepting audio")
        except Exception as error:
            report_error("Windows bridge PCM", error)

    def on_hotkey(event) -> None:
        try:
            if not stopping.is_set():
                controller.on_hotkey(event)
        except Exception as error:
            report_error("semantic hotkey", error)

    def on_status(status) -> None:
        statuses.append(status)
        if status.get("event") == "ready":
            bridge_ready.set()
        elif status.get("event") in ("error", "audio_error", "hotkey_error"):
            report_error("Windows bridge", status.get("error", "unknown bridge error"))

    bridge = WindowsBridgeClient(on_pcm, on_status, lambda error: report_error("Windows bridge", error), on_hotkey=on_hotkey)
    started = time.perf_counter()
    drain = None
    pending_commits_complete = True
    try:
        worker.start()
        if not worker_ready.wait(timeout=90):
            report_error("moonshine startup", "worker did not become ready within 90 seconds")
        if not failure.is_set():
            bridge.start()
            if not bridge_ready.wait(timeout=10):
                report_error("Windows bridge", "did not become ready within 10 seconds")
        if not failure.is_set():
            print(
                "Play Windows output. Press F8 for a new question; after a valid F8, "
                "press F9 to append a continuation. Focus a native Windows app for keys.",
                flush=True,
            )
            failure.wait(timeout=args.seconds)
    except KeyboardInterrupt:
        errors.append("interrupted")
    except Exception as error:
        report_error("probe", error)
    finally:
        # The bridge reader is joined before target capture, so no later PCM can
        # move this target.  The worker preserves its usual snapshot barriers.
        drain = stop_bridge_and_drain(bridge, forwarder, worker, args.drain_timeout)
        pending_commits_complete = controller.wait_for_pending(args.commit_timeout)
        if not pending_commits_complete:
            errors.append(
                f"semantic commits did not complete within {args.commit_timeout:g} seconds"
            )
        stopping.set()
        worker.stop()

    diagnostics = forwarder.diagnostics()
    final_drain = finalize_drain(drain, diagnostics["consumed_sample_cursor"]) if drain else {}
    semantics = controller.summary()
    report = {
        "ok": (
            bool(diagnostics["pcm_bytes_forwarded"])
            and final_drain.get("drain_status") == "completed_within_timeout"
            and pending_commits_complete
            and not errors
        ),
        "language": args.language,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "statuses": statuses,
        "errors": errors,
        **diagnostics,
        **final_drain,
        **semantics,
        "drain_timeout_seconds": args.drain_timeout,
        "commit_timeout_seconds": args.commit_timeout,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
