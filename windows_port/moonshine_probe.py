"""Standalone Windows-WASAPI to Moonshine streaming validation probe."""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time

from moonshine_streaming_worker import MoonshineStreamingWorker
from windows_port.audio import SAMPLE_RATE, SAMPLE_WIDTH_BYTES
from windows_port.bridge_client import WindowsBridgeClient


class PcmCursorForwarder:
    """Assign contiguous sample cursors while forwarding bridge PCM to Moonshine."""

    def __init__(self, worker):
        self.worker = worker
        self.next_sample_cursor = 0
        self.pcm_bytes = 0
        self.lock = threading.Lock()

    def submit(self, pcm: bytes) -> bool:
        """Submit one complete s16le chunk with an atomic, contiguous cursor span."""
        if len(pcm) % SAMPLE_WIDTH_BYTES:
            raise ValueError("Windows bridge PCM must contain complete s16le samples")
        with self.lock:
            start_cursor = self.next_sample_cursor
            end_cursor = start_cursor + len(pcm) // SAMPLE_WIDTH_BYTES
            accepted = self.worker.submit_pcm(pcm, start_cursor, end_cursor)
            if accepted:
                self.next_sample_cursor = end_cursor
                self.pcm_bytes += len(pcm)
            return accepted

    def diagnostics(self) -> dict[str, int | float]:
        """Snapshot bridge and worker counters for the terminal report."""
        with self.lock, self.worker.lock:
            return {
                "received_sample_cursor": self.next_sample_cursor,
                "queued_sample_cursor": self.worker.queued_sample_cursor,
                "consumed_sample_cursor": self.worker.consumed_sample_cursor,
                "audio_drop_samples": self.worker.audio_drop_samples,
                "max_backlog_samples": self.worker.max_backlog_samples,
                "max_backlog_ms": round(
                    self.worker.max_backlog_samples / SAMPLE_RATE * 1000, 1
                ),
                "pcm_bytes_forwarded": self.pcm_bytes,
            }

    def target_cursor(self) -> int:
        """Freeze the last successfully queued bridge sample cursor."""
        with self.lock:
            return self.next_sample_cursor


def drain_worker(worker, target_cursor: int, timeout: float, *, poll_interval=0.01):
    """Wait for the existing worker to consume every sample through *target_cursor*."""
    started = time.perf_counter()
    deadline = started + timeout

    def result(*, deadline_met, timed_out, reason, consumed_cursor):
        return {
            "drain_deadline_met": deadline_met,
            "drain_timeout": timed_out,
            "drain_reason": reason,
            "drain_target_sample_cursor": target_cursor,
            # On a timeout this is the exact cursor observed at the deadline;
            # on success it is the cursor observed when the deadline was met.
            "drain_consumed_at_deadline_sample_cursor": consumed_cursor,
            "drain_seconds": round(time.perf_counter() - started, 3),
            "_drain_started_at": started,
        }

    while True:
        with worker.lock:
            consumed_cursor = worker.consumed_sample_cursor
            accepting = worker.accepting
        if consumed_cursor >= target_cursor:
            return result(
                deadline_met=True,
                timed_out=False,
                reason=None,
                consumed_cursor=consumed_cursor,
            )
        if not accepting:
            return result(
                deadline_met=False,
                timed_out=False,
                reason="Moonshine worker stopped before the drain target",
                consumed_cursor=consumed_cursor,
            )
        if time.perf_counter() >= deadline:
            return result(
                deadline_met=False,
                timed_out=True,
                reason=f"timed out after {timeout:g} seconds",
                consumed_cursor=consumed_cursor,
            )
        time.sleep(poll_interval)


def stop_bridge_and_drain(bridge, forwarder, worker, timeout: float):
    """Stop PCM ingress, then drain the fixed final cursor through Moonshine."""
    bridge.stop()
    target_cursor = forwarder.target_cursor()
    return drain_worker(worker, target_cursor, timeout)


def finalize_drain(drain, final_consumed_cursor: int, *, finished_at=None):
    """Separate the drain-deadline observation from state after worker shutdown."""
    finished_at = time.perf_counter() if finished_at is None else finished_at
    target_cursor = drain["drain_target_sample_cursor"]
    completed_after_shutdown = final_consumed_cursor >= target_cursor
    if drain["drain_deadline_met"]:
        status = "completed_within_timeout"
    elif completed_after_shutdown and drain["drain_timeout"]:
        status = "timed_out_but_completed_during_shutdown"
    else:
        status = "incomplete_after_shutdown"
    return {
        key: value for key, value in drain.items() if key != "_drain_started_at"
    } | {
        "drain_status": status,
        "final_consumed_after_shutdown_sample_cursor": final_consumed_cursor,
        "completed_after_shutdown": completed_after_shutdown,
        "unprocessed_after_shutdown_samples": max(
            target_cursor - final_consumed_cursor, 0
        ),
        "final_catch_up_seconds": round(
            finished_at - drain["_drain_started_at"], 3
        ),
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Stream Windows WASAPI loopback PCM into the existing Moonshine worker."
    )
    parser.add_argument("--language", choices=("en", "ja"), default="en")
    parser.add_argument("--seconds", type=float, default=20.0)
    parser.add_argument("--drain-timeout", type=float, default=10.0)
    args = parser.parse_args(argv)
    if args.seconds <= 0:
        parser.error("--seconds must be positive")
    if args.drain_timeout <= 0:
        parser.error("--drain-timeout must be positive")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    errors = []
    statuses = []
    last_preview = {"text": "", "lines": []}
    worker_ready = threading.Event()
    bridge_ready = threading.Event()
    failure = threading.Event()
    stopping = threading.Event()
    print_lock = threading.Lock()

    def report_error(source, error) -> None:
        if stopping.is_set():
            return
        message = f"{source}: {error}"
        errors.append(message)
        failure.set()
        with print_lock:
            print(message, file=sys.stderr, flush=True)

    def on_worker_ready(info, error) -> None:
        if error is not None:
            report_error("moonshine startup error", error)
        else:
            worker_ready.set()
            with print_lock:
                print(
                    "Moonshine ready: " + json.dumps(info, ensure_ascii=False),
                    flush=True,
                )

    def on_preview(preview) -> None:
        text = preview.get("text", "").strip()
        if not text or text == last_preview["text"]:
            return
        last_preview.update({"text": text, "lines": preview.get("lines", [])})
        with print_lock:
            print(
                f"[preview @ sample {preview.get('consumed_sample_cursor', 0)}] {text}",
                flush=True,
            )

    worker = MoonshineStreamingWorker(
        on_worker_ready,
        on_preview,
        lambda error: report_error("moonshine error", error),
        language=args.language,
    )
    forwarder = PcmCursorForwarder(worker)

    def on_pcm(pcm) -> None:
        try:
            if not stopping.is_set() and not forwarder.submit(pcm):
                report_error("moonshine PCM submission", "worker is not accepting audio")
        except Exception as error:
            report_error("Windows bridge PCM", error)

    def on_status(status) -> None:
        statuses.append(status)
        event = status.get("event")
        if event == "ready":
            bridge_ready.set()
            with print_lock:
                print(
                    "Windows bridge ready: " + json.dumps(status, ensure_ascii=False),
                    flush=True,
                )
        elif event in ("error", "audio_error"):
            report_error("Windows bridge", status.get("error", "unknown bridge error"))

    bridge = WindowsBridgeClient(
        on_pcm,
        on_status,
        lambda error: report_error("Windows bridge", error),
    )
    started = time.perf_counter()
    drain = None
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
                f"Play {args.language} speech through Windows output for {args.seconds:g} seconds.",
                flush=True,
            )
            failure.wait(timeout=args.seconds)
    except KeyboardInterrupt:
        errors.append("interrupted")
    except Exception as error:
        report_error("probe", error)
    finally:
        # Keep the PCM callback active until bridge.stop() has joined its reader.
        # This makes the cursor captured immediately afterwards the final target.
        drain = stop_bridge_and_drain(
            bridge, forwarder, worker, args.drain_timeout
        )
        stopping.set()
        worker.stop()

    diagnostics = forwarder.diagnostics()
    final_drain = finalize_drain(
        drain,
        diagnostics["consumed_sample_cursor"],
    ) if drain else {}
    report = {
        "ok": (
            bool(diagnostics["pcm_bytes_forwarded"])
            and final_drain.get("drain_status") == "completed_within_timeout"
            and not errors
        ),
        "language": args.language,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "statuses": statuses,
        "errors": errors,
        "preview_text": last_preview["text"],
        "transcript_detected": bool(last_preview["text"]),
        # Worker drop accounting and a missed drain deadline are independent:
        # a slow worker can catch up during shutdown without losing PCM.
        "audio_loss_detected": diagnostics["audio_drop_samples"] > 0,
        **diagnostics,
        **final_drain,
        "drain_timeout_seconds": args.drain_timeout,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
