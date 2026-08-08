"""Audio-boundary and JSONL logging helpers for Interview Assistant."""

import json
import re
import threading
from datetime import datetime

import numpy as np


SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2  # s16le
BYTES_PER_SECOND = SAMPLE_RATE * SAMPLE_WIDTH

# F8 뒤의 짧은 구간은 문장 경계 판단에만 사용하고 다음 질문의 음성은 보존한다.
POST_CONTEXT_MS = 600
SILENCE_PADDING_MS = 300
REACTION_COMPENSATION_MS = 200
BOUNDARY_SEARCH_BEFORE_MS = 1_200
BOUNDARY_SEARCH_AFTER_MS = 250

LOG_WRITE_LOCK = threading.Lock()


def append_log(log_path, event):
    if log_path is None:
        return

    record = {
        "timestamp": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        **event,
    }
    with LOG_WRITE_LOCK:
        with log_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")


def _is_sentence_end(text):
    return bool(re.search(r"[.!?。！？][\"'”’)]*$", text.strip()))


def _choose_boundary(words, trigger_seconds):
    """Prefer punctuation/silence near F8, then compensate for reaction time."""
    target = max(0.0, trigger_seconds - REACTION_COMPENSATION_MS / 1000)
    search_start = max(0.0, trigger_seconds - BOUNDARY_SEARCH_BEFORE_MS / 1000)
    search_end = trigger_seconds + BOUNDARY_SEARCH_AFTER_MS / 1000

    punctuation = [
        word.end
        for word in words
        if search_start <= word.end <= search_end and _is_sentence_end(word.word)
    ]
    if punctuation:
        return min(punctuation, key=lambda value: abs(value - target)), "punctuation"

    silence_boundaries = []
    for current_word, next_word in zip(words, words[1:]):
        gap = next_word.start - current_word.end
        if gap >= 0.25 and search_start <= current_word.end <= search_end:
            silence_boundaries.append(current_word.end)

    if silence_boundaries:
        return min(silence_boundaries, key=lambda value: abs(value - target)), "silence"

    return target, "reaction_compensation"


def transcribe_question(
    model,
    pcm_audio,
    trigger_bytes,
    silence_padding_ms=SILENCE_PADDING_MS,
    language="en",
):
    samples = np.frombuffer(pcm_audio, dtype=np.int16).astype(np.float32) / 32768.0
    silence_samples = int(SAMPLE_RATE * silence_padding_ms / 1000)
    if silence_samples:
        samples = np.pad(samples, (0, silence_samples))

    segments, info = model.transcribe(
        samples,
        language=language,
        vad_filter=True,
        word_timestamps=True,
        condition_on_previous_text=False,
    )
    segments = list(segments)
    words = [word for segment in segments for word in (segment.words or [])]

    trigger_seconds = trigger_bytes / BYTES_PER_SECOND
    boundary_seconds, boundary_reason = _choose_boundary(words, trigger_seconds)
    selected_words = [
        word
        for word in words
        if (word.start + word.end) / 2 <= boundary_seconds
    ]
    question = "".join(word.word for word in selected_words).strip()

    if not question:
        question = " ".join(segment.text.strip() for segment in segments).strip()
        boundary_seconds = trigger_seconds
        boundary_reason = "transcript_fallback"

    boundary_bytes = int(boundary_seconds * BYTES_PER_SECOND)
    boundary_bytes -= boundary_bytes % SAMPLE_WIDTH
    boundary_bytes = max(0, min(boundary_bytes, len(pcm_audio)))
    details = {
        "detected_language": info.language,
        "full_transcript": "".join(word.word for word in words).strip(),
        "trigger_seconds": round(trigger_seconds, 3),
        "boundary_seconds": round(boundary_seconds, 3),
        "boundary_reason": boundary_reason,
        "snapshot_seconds": round(len(pcm_audio) / BYTES_PER_SECOND, 3),
        "retained_seconds": round(
            (len(pcm_audio) - boundary_bytes) / BYTES_PER_SECOND,
            3,
        ),
        "words": [
            {
                "start": round(float(word.start), 3),
                "end": round(float(word.end), 3),
                "text": word.word,
            }
            for word in words
        ],
    }
    return question, boundary_bytes, details
