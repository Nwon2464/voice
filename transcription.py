"""Platform-neutral faster-whisper helpers."""

import time
import wave

import numpy as np
from faster_whisper import WhisperModel

from audio_utils import SAMPLE_RATE, SAMPLE_WIDTH


def load_whisper(model_name="small", language="en", warmup_seconds=1):
    started = time.perf_counter()
    load_started = time.perf_counter()
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    load_seconds = time.perf_counter() - load_started
    warmup_started = time.perf_counter()
    segments, _ = model.transcribe(
        np.zeros(SAMPLE_RATE * warmup_seconds, dtype=np.float32),
        language=language,
        vad_filter=False,
        word_timestamps=True,
        condition_on_previous_text=False,
    )
    list(segments)
    return model, {
        "load_seconds": load_seconds,
        "warmup_seconds": time.perf_counter() - warmup_started,
        "startup_seconds": time.perf_counter() - started,
    }


def transcribe_pcm(model, pcm_audio, language="en", padding_ms=200):
    if not pcm_audio:
        return ""
    samples = np.frombuffer(pcm_audio, dtype=np.int16).astype(np.float32)
    samples /= 32768.0
    samples = np.pad(samples, (0, int(SAMPLE_RATE * padding_ms / 1000)))
    segments, _ = model.transcribe(
        samples,
        language=language,
        vad_filter=True,
        condition_on_previous_text=False,
    )
    return " ".join(segment.text.strip() for segment in segments).strip()


def save_wav(path, pcm_audio):
    with wave.open(str(path), "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(SAMPLE_WIDTH)
        file.setframerate(SAMPLE_RATE)
        file.writeframes(pcm_audio)
