"""Deterministic WAV-to-Moonshine F8/F9 semantic barrier validation."""

from __future__ import annotations

import argparse
import difflib
import json
import tempfile
import threading
import time
import unicodedata
import wave
from pathlib import Path

import numpy as np

from moonshine_streaming_worker import MoonshineStreamingWorker
from windows_port.audio import SAMPLE_RATE, SAMPLE_WIDTH_BYTES
from windows_port.moonshine_probe import PcmCursorForwarder
from windows_port.semantic_controller import SemanticCommitController


CHUNK_SAMPLES = 160  # 10 ms at the bridge/worker's fixed 16 kHz rate.


def wave_metadata(path: Path) -> dict:
    with wave.open(str(path), "rb") as source:
        frames = source.getnframes()
        sample_rate = source.getframerate()
        channels = source.getnchannels()
        sample_width = source.getsampwidth()
        return {
            "path": str(path),
            "duration_seconds": round(frames / sample_rate, 6),
            "sample_rate": sample_rate,
            "channels": channels,
            "sample_width_bytes": sample_width,
            "total_frames": frames,
            "compression": source.getcomptype(),
        }


def prepare_pcm(path: Path) -> tuple[bytes, dict, dict | None]:
    """Return s16le/16k/mono PCM, writing a /tmp conversion only when needed."""
    source_metadata = wave_metadata(path)
    with wave.open(str(path), "rb") as source:
        if source.getcomptype() != "NONE" or source.getsampwidth() != SAMPLE_WIDTH_BYTES:
            raise ValueError("only uncompressed 16-bit PCM WAV input is supported")
        samples = np.frombuffer(source.readframes(source.getnframes()), dtype="<i2")
    if source_metadata["channels"] > 1:
        samples = samples.reshape(-1, source_metadata["channels"]).mean(axis=1)
    if (
        source_metadata["sample_rate"] == SAMPLE_RATE
        and source_metadata["channels"] == 1
        and source_metadata["sample_width_bytes"] == SAMPLE_WIDTH_BYTES
    ):
        return samples.astype("<i2", copy=False).tobytes(), source_metadata, None

    output_frames = round(len(samples) * SAMPLE_RATE / source_metadata["sample_rate"])
    positions = np.arange(output_frames, dtype=np.float64) * (
        source_metadata["sample_rate"] / SAMPLE_RATE
    )
    converted = np.interp(
        positions,
        np.arange(len(samples), dtype=np.float64),
        samples.astype(np.float64),
    )
    converted_pcm = np.rint(np.clip(converted, -32768, 32767)).astype("<i2")
    output_path = Path(tempfile.gettempdir()) / f"{path.stem}.semantic-16k-mono.wav"
    with wave.open(str(output_path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(SAMPLE_WIDTH_BYTES)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(converted_pcm.tobytes())
    converted_metadata = wave_metadata(output_path) | {
        "conversion": "numpy linear resample to 16 kHz mono s16le",
    }
    return converted_pcm.tobytes(), source_metadata, converted_metadata


def find_silence_boundary(pcm: bytes) -> dict | None:
    """Find a low-RMS 10 ms boundary between 50% and 70% of the file."""
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    chunk_count = len(samples) // CHUNK_SAMPLES
    if chunk_count < 4:
        return None
    rms = np.array([
        np.sqrt(np.mean(samples[index * CHUNK_SAMPLES:(index + 1) * CHUNK_SAMPLES] ** 2))
        for index in range(chunk_count)
    ])
    first = int(chunk_count * 0.50)
    last = max(first + 1, int(chunk_count * 0.70))
    local_index = first + int(np.argmin(rms[first:last]))
    median = float(np.median(rms))
    # A near-silent TTS pause is required; do not split through an arbitrary word.
    if rms[local_index] > max(80.0, median * 0.08):
        return None
    return {
        "split_cursor": local_index * CHUNK_SAMPLES,
        "split_rms": round(float(rms[local_index]), 1),
        "median_rms": round(median, 1),
    }


def normalize_transcript(text: str, language: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    if language == "en":
        text = text.casefold()
    text = "".join(
        character
        for character in text
        if not unicodedata.category(character).startswith("P")
    )
    text = "".join(text.split()) if language == "ja" else " ".join(text.split())
    return text


def compare_transcripts(reference: str, committed: str, language: str) -> dict:
    normalized_reference = normalize_transcript(reference, language)
    normalized_committed = normalize_transcript(committed, language)
    tail_length = min(30, len(normalized_reference), len(normalized_committed))
    reference_tail = normalized_reference[-tail_length:] if tail_length else ""
    committed_tail = normalized_committed[-tail_length:] if tail_length else ""
    return {
        "raw_reference_transcript": reference,
        "raw_f8_question_text": committed,
        "normalized_exact_match": normalized_reference == normalized_committed,
        "similarity": round(
            difflib.SequenceMatcher(None, normalized_reference, normalized_committed).ratio(), 4
        ),
        "reference_tail": reference_tail,
        "f8_question_tail": committed_tail,
        "tail_suffix_match": bool(reference_tail) and reference_tail == committed_tail,
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Deterministically test Moonshine F8/F9 semantics against a WAV cursor."
    )
    parser.add_argument("--language", choices=("en", "ja"), required=True)
    parser.add_argument("--wav", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    return args


def _start_worker(language, on_auto_commit):
    ready = threading.Event()
    startup_error = []
    runtime_errors = []

    def on_ready(_info, error):
        if error is not None:
            startup_error.append(str(error))
        ready.set()

    worker = MoonshineStreamingWorker(
        on_ready,
        lambda _preview: None,
        lambda error: runtime_errors.append(str(error)),
        on_auto_commit=on_auto_commit,
        language=language,
    )
    worker.start()
    if not ready.wait(timeout=120):
        worker.stop()
        raise RuntimeError("Moonshine worker did not become ready within 120 seconds")
    if startup_error:
        worker.stop()
        raise RuntimeError(startup_error[0])
    return worker, runtime_errors


def _feed_pcm(forwarder, pcm: bytes, start=0, end=None):
    end = len(pcm) // SAMPLE_WIDTH_BYTES if end is None else end
    for cursor in range(start, end, CHUNK_SAMPLES):
        next_cursor = min(cursor + CHUNK_SAMPLES, end)
        start_byte = cursor * SAMPLE_WIDTH_BYTES
        end_byte = next_cursor * SAMPLE_WIDTH_BYTES
        if not forwarder.submit(pcm[start_byte:end_byte]):
            raise RuntimeError("Moonshine worker rejected deterministic PCM")


def _reference_run(language, pcm: bytes, timeout: float) -> dict:
    silence_segments = []
    worker, errors = _start_worker(
        language,
        lambda result, error: silence_segments.append((result, error)),
    )
    forwarder = PcmCursorForwarder(worker)
    target = len(pcm) // SAMPLE_WIDTH_BYTES
    done = threading.Event()
    result_box = []
    try:
        _feed_pcm(forwarder, pcm)
        if not worker.request_snapshot(
            target,
            lambda result, error: (result_box.append((result, error)), done.set()),
        ):
            raise RuntimeError("Moonshine worker rejected reference snapshot")
        if not done.wait(timeout):
            raise RuntimeError("reference snapshot timed out")
        result, error = result_box[0]
        if error is not None:
            raise RuntimeError(f"reference snapshot failed: {error}")
        diagnostics = forwarder.diagnostics()
        return {
            "reference_target_cursor": target,
            "reference_transcript": result["text"],
            "reference_display_transcript": result["display_text"],
            "reference_accumulated_segment_count": result.get("accumulated_segment_count", 0),
            "reference_final_active_transcript": result["text"],
            "reference_silence_callback_count": len(silence_segments),
            "reference_audio_drop_samples": diagnostics["audio_drop_samples"],
            "reference_received_sample_cursor": diagnostics["received_sample_cursor"],
            "reference_queued_sample_cursor": diagnostics["queued_sample_cursor"],
            "reference_consumed_sample_cursor": diagnostics["consumed_sample_cursor"],
            "reference_cursor_complete": result["cursor_complete"],
            "runtime_errors": errors,
        }
    finally:
        worker.stop()


def _f8_end_run(language, pcm: bytes, timeout: float) -> dict:
    controller_ref = {}
    worker, errors = _start_worker(
        language,
        lambda result, error: controller_ref["controller"].on_silence_segment(result, error),
    )
    forwarder = PcmCursorForwarder(worker)
    controller = SemanticCommitController(worker, forwarder)
    controller_ref["controller"] = controller
    target = len(pcm) // SAMPLE_WIDTH_BYTES
    try:
        # Fast 10 ms chunks are intentionally a synthetic backlog stress test.
        _feed_pcm(forwarder, pcm)
        controller.on_hotkey({
            "event": "hotkey", "key": "F8", "sequence": 1, "timestamp_ns": time.time_ns()
        })
        if not controller.wait_for_pending(timeout):
            raise RuntimeError("F8 semantic callback timed out")
        event = next(
            record for record in controller.summary()["semantic_events"]
            if record.get("event_type") == "hotkey"
        )
        diagnostics = forwarder.diagnostics()
        return {
            "test_mode": "synthetic_backlog_stress_fast_10ms_feed",
            "wav_total_samples": target,
            "f8_event": event,
            "f8_diagnostics": diagnostics,
            "runtime_errors": errors,
        }
    finally:
        worker.stop()


def _f8_f9_run(language, pcm: bytes, boundary: dict, timeout: float) -> dict:
    controller_ref = {}
    worker, errors = _start_worker(
        language,
        lambda result, error: controller_ref["controller"].on_silence_segment(result, error),
    )
    forwarder = PcmCursorForwarder(worker)
    controller = SemanticCommitController(worker, forwarder)
    controller_ref["controller"] = controller
    split = boundary["split_cursor"]
    final = len(pcm) // SAMPLE_WIDTH_BYTES
    try:
        _feed_pcm(forwarder, pcm, end=split)
        controller.on_hotkey({"event": "hotkey", "key": "F8", "sequence": 1, "timestamp_ns": time.time_ns()})
        if not controller.wait_for_pending(timeout):
            raise RuntimeError("split F8 semantic callback timed out")
        _feed_pcm(forwarder, pcm, start=split, end=final)
        controller.on_hotkey({"event": "hotkey", "key": "F9", "sequence": 2, "timestamp_ns": time.time_ns()})
        if not controller.wait_for_pending(timeout):
            raise RuntimeError("final F9 semantic callback timed out")
        hotkeys = [
            record for record in controller.summary()["semantic_events"]
            if record.get("event_type") == "hotkey"
        ]
        return {
            "split": boundary,
            "f8_event": hotkeys[0],
            "f9_event": hotkeys[1],
            "runtime_errors": errors,
        }
    finally:
        worker.stop()


def _f9_without_f8_run(language) -> dict:
    worker, errors = _start_worker(language, lambda _result, _error: None)
    forwarder = PcmCursorForwarder(worker)
    controller = SemanticCommitController(worker, forwarder)
    try:
        controller.on_hotkey({"event": "hotkey", "key": "F9", "sequence": 1, "timestamp_ns": time.time_ns()})
        return {"event": controller.summary()["semantic_events"][0], "runtime_errors": errors}
    finally:
        worker.stop()


def evaluate_f8(reference: dict, f8: dict, language: str) -> dict:
    event = f8["f8_event"]
    comparison = compare_transcripts(reference["reference_transcript"], event.get("question_text", ""), language)
    target = f8["wav_total_samples"]
    cursor_ok = (
        event.get("received_cursor_at_press") == target
        and event.get("target_sample_cursor") == target
        and event.get("consumed_cursor_after_barrier", -1) >= target
    )
    audio_ok = event.get("audio_drop_samples") == 0
    semantic_ok = event.get("semantic_commit_accepted") is True
    return {
        **comparison,
        "cursor_barrier_passed": cursor_ok,
        "audio_loss_free": audio_ok,
        "semantic_commit_passed": semantic_ok,
        # Transcript mismatch is reported as ASR comparison evidence, not a
        # cursor-barrier failure when the independent cursor invariants pass.
        "result": "PASS" if cursor_ok and audio_ok and semantic_ok else "FAIL",
    }


def main(argv=None) -> int:
    args = parse_args(argv)
    if not args.wav.is_file():
        raise SystemExit(f"WAV does not exist: {args.wav}")
    pcm, source_metadata, converted_metadata = prepare_pcm(args.wav)
    reference = _reference_run(args.language, pcm, args.timeout)
    f8 = _f8_end_run(args.language, pcm, args.timeout)
    f8_evaluation = evaluate_f8(reference, f8, args.language)
    boundary = find_silence_boundary(pcm)
    f8_f9 = (
        _f8_f9_run(args.language, pcm, boundary, args.timeout)
        if boundary is not None
        else {"skipped": "no stable 50-70% low-energy silence boundary"}
    )
    f9_without_f8 = _f9_without_f8_run(args.language)
    f9_event = f8_f9.get("f9_event", {})
    continuation_ok = (
        f8_f9.get("f8_event", {}).get("semantic_commit_accepted") is True
        and f9_event.get("semantic_commit_accepted") is True
        and f8_f9["f8_event"].get("question_number") == 1
        and f9_event.get("question_number") == 1
        and f8_f9["f8_event"].get("question_text", "") in f9_event.get("question_text", "")
    ) if boundary is not None else False
    no_base_event = f9_without_f8["event"]
    report = {
        "language": args.language,
        "source_wav": source_metadata,
        "converted_wav": converted_metadata,
        "worker_input": {"sample_rate": SAMPLE_RATE, "channels": 1, "sample_width_bytes": SAMPLE_WIDTH_BYTES, "total_samples": len(pcm) // SAMPLE_WIDTH_BYTES},
        "reference": reference,
        "f8_exact_end": f8,
        "f8_evaluation": f8_evaluation,
        "f8_f9_continuation": f8_f9,
        "f9_without_f8": f9_without_f8,
        "f9_without_f8_passed": (
            no_base_event.get("semantic_commit_accepted") is False
            and no_base_event.get("semantic_rejection_reason") == "no_valid_previous_question"
        ),
        "f9_continuation_passed": continuation_ok,
        "ok": (
            f8_evaluation["result"] == "PASS"
            and continuation_ok
            and no_base_event.get("semantic_rejection_reason") == "no_valid_previous_question"
        ),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
