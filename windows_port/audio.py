"""WASAPI loopback capture and PCM normalization for the Windows helper."""

from __future__ import annotations

import threading

import numpy as np


SAMPLE_RATE = 16_000
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2
PCM_BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH_BYTES


def _soundcard():
    """Import the Windows-only capture dependency only when it is needed."""
    try:
        import soundcard
    except ImportError as error:
        raise RuntimeError(
            "SoundCard is not installed; run ./setup_windows_bridge.sh from WSL"
        ) from error
    return soundcard


def pcm_s16le_mono(frames) -> bytes:
    """Downmix SoundCard float frames to 16 kHz mono signed-int16 little-endian.

    SoundCard produces normalized floating-point frames.  The helper requests
    16 kHz from its recorder, and this function only handles channel mixing and
    the final PCM representation required by the WSL side.
    """
    samples = np.asarray(frames, dtype=np.float32)
    if samples.ndim == 0 or samples.ndim > 2:
        raise ValueError("audio frames must be a one- or two-dimensional array")
    if samples.ndim == 2:
        samples = samples.mean(axis=1, dtype=np.float32)
    samples = np.nan_to_num(samples, nan=0.0, posinf=1.0, neginf=-1.0)
    samples = np.clip(samples, -1.0, 1.0)
    return np.rint(samples * 32767.0).astype("<i2", copy=False).tobytes()


def list_output_devices() -> list[dict[str, str]]:
    """Return SoundCard output endpoints for diagnostics and explicit selection."""
    return [
        {"id": str(speaker.id), "name": speaker.name}
        for speaker in _soundcard().all_speakers()
    ]


def default_output_device():
    speaker = _soundcard().default_speaker()
    if speaker is None:
        raise RuntimeError("Windows has no default output device")
    return speaker


class WasapiLoopbackCapture:
    """Capture a Windows output endpoint and emit 16 kHz mono s16le PCM."""

    def __init__(self, on_pcm, on_error, *, speaker_id=None, block_frames=1600):
        if block_frames <= 0:
            raise ValueError("block_frames must be positive")
        self.on_pcm = on_pcm
        self.on_error = on_error
        self.speaker_id = speaker_id
        self.block_frames = block_frames
        self.stopped = threading.Event()
        self.thread = None
        self.speaker = None

    def start(self) -> None:
        if self.thread is not None:
            return
        soundcard = _soundcard()
        self.speaker = (
            soundcard.get_speaker(self.speaker_id)
            if self.speaker_id
            else default_output_device()
        )
        if self.speaker is None:
            raise RuntimeError(f"Windows output device was not found: {self.speaker_id}")
        self.thread = threading.Thread(target=self._capture, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stopped.set()
        if self.thread is not None:
            self.thread.join(timeout=3)

    @property
    def device_info(self):
        if self.speaker is None:
            return None
        return {"id": str(self.speaker.id), "name": self.speaker.name}

    def _capture(self) -> None:
        try:
            soundcard = _soundcard()
            loopback = soundcard.get_microphone(
                str(self.speaker.id), include_loopback=True
            )
            if loopback is None:
                raise RuntimeError(
                    f"WASAPI loopback endpoint was not found for {self.speaker.name}"
                )
            with loopback.recorder(samplerate=SAMPLE_RATE) as recorder:
                while not self.stopped.is_set():
                    frames = recorder.record(numframes=self.block_frames)
                    if frames is not None and len(frames):
                        self.on_pcm(pcm_s16le_mono(frames))
        except Exception as error:
            if not self.stopped.is_set():
                self.on_error(error)
