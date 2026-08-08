"""WASAPI loopback capture adapter backed by SoundCard."""

import threading

import numpy as np

from audio_utils import SAMPLE_RATE


def _soundcard():
    try:
        import soundcard
    except ImportError as error:
        raise RuntimeError(
            "SoundCard is not installed; run pip install -r requirements-windows.txt"
        ) from error
    return soundcard


def list_output_devices():
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
    """Emit 16 kHz mono signed-int16 PCM from a Windows output endpoint."""

    def __init__(self, on_pcm, on_error, speaker_id=None, block_frames=1600):
        self.on_pcm = on_pcm
        self.on_error = on_error
        self.speaker_id = speaker_id
        self.block_frames = block_frames
        self.stopped = threading.Event()
        self.thread = None
        self.speaker = None

    def start(self):
        if self.thread is not None:
            return
        soundcard = _soundcard()
        if self.speaker_id:
            self.speaker = soundcard.get_speaker(self.speaker_id)
        else:
            self.speaker = default_output_device()
        if self.speaker is None:
            raise RuntimeError(f"Output device was not found: {self.speaker_id}")
        self.thread = threading.Thread(target=self._capture, daemon=True)
        self.thread.start()

    def stop(self):
        self.stopped.set()
        if self.thread is not None:
            self.thread.join(timeout=3)

    @property
    def device_info(self):
        if self.speaker is None:
            return None
        return {"id": str(self.speaker.id), "name": self.speaker.name}

    def _capture(self):
        try:
            soundcard = _soundcard()
            loopback = soundcard.get_microphone(
                str(self.speaker.id),
                include_loopback=True,
            )
            if loopback is None:
                raise RuntimeError(
                    f"WASAPI loopback endpoint was not found for {self.speaker.name}"
                )
            with loopback.recorder(samplerate=SAMPLE_RATE) as recorder:
                while not self.stopped.is_set():
                    frames = recorder.record(numframes=self.block_frames)
                    if frames is None or not len(frames):
                        continue
                    samples = np.asarray(frames, dtype=np.float32)
                    if samples.ndim == 2:
                        samples = samples.mean(axis=1)
                    samples = np.clip(samples, -1.0, 1.0)
                    pcm = (samples * 32767.0).astype("<i2", copy=False).tobytes()
                    self.on_pcm(pcm)
        except Exception as error:
            if not self.stopped.is_set():
                self.on_error(error)
