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
REPLAY_PRE_ROLL_MS = 80
REPLAY_REQUIRED_SILENCE_MS = 200
REPLAY_REQUIRED_SPEECH_MS = 100
REPLAY_VAD_RMS = 250

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


def _choose_boundary(words, trigger_seconds, audio_duration_seconds):
    """Prefer semantic and acoustic evidence around the F8 hint."""
    target = max(0.0, trigger_seconds - REACTION_COMPENSATION_MS / 1000)
    search_start = max(0.0, trigger_seconds - BOUNDARY_SEARCH_BEFORE_MS / 1000)
    search_end = trigger_seconds + BOUNDARY_SEARCH_AFTER_MS / 1000
    post_context_end = trigger_seconds + POST_CONTEXT_MS / 1000

    punctuation_before = [
        word.end
        for word in words
        if (
            search_start <= word.end <= trigger_seconds
            and _is_sentence_end(word.word)
        )
    ]
    punctuation_after = [
        word.end
        for word in words
        if (
            trigger_seconds < word.end <= post_context_end
            and _is_sentence_end(word.word)
        )
    ]
    if punctuation_before:
        latest_before = max(punctuation_before)
        late_trigger_grace = (
            POST_CONTEXT_MS + BOUNDARY_SEARCH_AFTER_MS
        ) / 1000
        if (
            trigger_seconds - latest_before <= late_trigger_grace
            or not punctuation_after
        ):
            return latest_before, "punctuation"
    if punctuation_after:
        return min(punctuation_after), "punctuation"
    if punctuation_before:
        return max(punctuation_before), "punctuation"

    silence_boundaries = []
    for current_word, next_word in zip(words, words[1:]):
        gap = next_word.start - current_word.end
        if gap >= 0.25 and search_start <= current_word.end <= search_end:
            silence_boundaries.append(current_word.end)

    # A final silence has no next word, so the pair-wise scan above cannot see it.
    if words:
        final_word = words[-1]
        trailing_silence = audio_duration_seconds - final_word.end
        if (
            trailing_silence >= 0.25
            and search_start <= final_word.end <= post_context_end
        ):
            return final_word.end, "trailing_silence"

    if silence_boundaries:
        return min(silence_boundaries, key=lambda value: abs(value - target)), "silence"

    # If F8 lands immediately before or inside a word, keep that complete word.
    timestamp_candidates = [
        word.end
        for word in words
        if (
            word.end >= target
            and word.start <= search_end
            and word.end <= post_context_end
        )
    ]
    if timestamp_candidates:
        return max(timestamp_candidates), "word_timestamp"

    return target, "reaction_compensation"


def _find_acoustic_replay_start(
    pcm_audio,
    boundary_bytes,
    vad_rms,
):
    """Find new speech after a real silence, excluding the prior word's tail."""
    samples = np.frombuffer(pcm_audio, dtype=np.int16)
    boundary_sample = boundary_bytes // SAMPLE_WIDTH
    chunk_samples = max(1, SAMPLE_RATE // 100)  # 10 ms
    required_silence_chunks = REPLAY_REQUIRED_SILENCE_MS // 10
    required_speech_chunks = REPLAY_REQUIRED_SPEECH_MS // 10
    pre_roll_samples = int(SAMPLE_RATE * REPLAY_PRE_ROLL_MS / 1000)
    silence_chunks = 0
    speech_chunks = 0
    speech_start = None
    found_separating_silence = False

    for start in range(boundary_sample, len(samples), chunk_samples):
        chunk = samples[start:start + chunk_samples].astype(np.float32)
        rms = int(np.sqrt(np.mean(chunk * chunk))) if chunk.size else 0
        loud = rms >= vad_rms

        if not found_separating_silence:
            silence_chunks = 0 if loud else silence_chunks + 1
            if silence_chunks >= required_silence_chunks:
                found_separating_silence = True
            continue

        if loud:
            if speech_chunks == 0:
                speech_start = start
            speech_chunks += 1
            if speech_chunks >= required_speech_chunks:
                replay_sample = max(
                    boundary_sample,
                    speech_start - pre_roll_samples,
                )
                return replay_sample * SAMPLE_WIDTH
        else:
            speech_chunks = 0
            speech_start = None

    return len(pcm_audio)


def transcribe_question(
    model,
    pcm_audio,
    trigger_bytes,
    silence_padding_ms=SILENCE_PADDING_MS,
    replay_vad_rms=REPLAY_VAD_RMS,
):
    audio_duration_seconds = len(pcm_audio) / BYTES_PER_SECOND
    samples = np.frombuffer(pcm_audio, dtype=np.int16).astype(np.float32) / 32768.0
    silence_samples = int(SAMPLE_RATE * silence_padding_ms / 1000)
    if silence_samples:
        samples = np.pad(samples, (0, silence_samples))

    segments, info = model.transcribe(
        samples,
        language="en",
        vad_filter=True,
        word_timestamps=True,
        condition_on_previous_text=False,
    )
    segments = list(segments)
    words = [word for segment in segments for word in (segment.words or [])]

    trigger_seconds = trigger_bytes / BYTES_PER_SECOND
    boundary_seconds, boundary_reason = _choose_boundary(
        words,
        trigger_seconds,
        audio_duration_seconds,
    )
    selected_words = [
        word
        for word in words
        if (word.start + word.end) / 2 <= boundary_seconds
    ]
    if selected_words:
        # Never split a word that was selected as part of the current question.
        boundary_seconds = max(boundary_seconds, selected_words[-1].end)
    question = "".join(word.word for word in selected_words).strip()

    if not question:
        question = " ".join(segment.text.strip() for segment in segments).strip()
        boundary_seconds = trigger_seconds
        boundary_reason = "transcript_fallback"

    boundary_bytes = int(boundary_seconds * BYTES_PER_SECOND)
    boundary_bytes -= boundary_bytes % SAMPLE_WIDTH
    boundary_bytes = max(0, min(boundary_bytes, len(pcm_audio)))
    remaining_words = words[len(selected_words):]
    if selected_words and remaining_words:
        next_word = remaining_words[0]
        replay_start_seconds = max(
            boundary_seconds,
            next_word.start - REPLAY_PRE_ROLL_MS / 1000,
        )
        replay_start_bytes = int(replay_start_seconds * BYTES_PER_SECOND)
        replay_start_bytes -= replay_start_bytes % SAMPLE_WIDTH
        replay_start_bytes = max(
            boundary_bytes,
            min(replay_start_bytes, len(pcm_audio)),
        )
        replay_reason = "next_word"
    elif selected_words:
        replay_start_bytes = _find_acoustic_replay_start(
            pcm_audio,
            boundary_bytes,
            replay_vad_rms,
        )
        replay_reason = (
            "new_speech_after_silence"
            if replay_start_bytes < len(pcm_audio)
            else "no_following_speech"
        )
    else:
        replay_start_bytes = len(pcm_audio)
        replay_reason = "no_word_timestamps"
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
        "replay_start_seconds": round(
            replay_start_bytes / BYTES_PER_SECOND,
            3,
        ),
        "replay_snapshot_seconds": round(
            (len(pcm_audio) - replay_start_bytes) / BYTES_PER_SECOND,
            3,
        ),
        "replay_reason": replay_reason,
        "words": [
            {
                "start": round(float(word.start), 3),
                "end": round(float(word.end), 3),
                "text": word.word,
            }
            for word in words
        ],
    }
    return question, boundary_bytes, replay_start_bytes, details
