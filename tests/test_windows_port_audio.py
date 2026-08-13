import unittest

import numpy as np

from windows_port.audio import (
    CHANNELS,
    PCM_BYTES_PER_SECOND,
    SAMPLE_RATE,
    SAMPLE_WIDTH_BYTES,
    WasapiLoopbackCapture,
    pcm_s16le_mono,
)


class WindowsPortAudioTests(unittest.TestCase):
    def test_pcm_conversion_downmixes_and_clips_to_s16le(self):
        frames = np.array([[1.0, -1.0], [0.5, 0.5], [2.0, 2.0]], dtype=np.float32)
        pcm = pcm_s16le_mono(frames)
        self.assertEqual(np.frombuffer(pcm, dtype="<i2").tolist(), [0, 16384, 32767])

    def test_pcm_conversion_replaces_non_finite_samples(self):
        pcm = pcm_s16le_mono(np.array([np.nan, np.inf, -np.inf], dtype=np.float32))
        self.assertEqual(np.frombuffer(pcm, dtype="<i2").tolist(), [0, 32767, -32767])

    def test_pcm_format_is_fixed_for_wsl_consumer(self):
        self.assertEqual((SAMPLE_RATE, CHANNELS, SAMPLE_WIDTH_BYTES), (16_000, 1, 2))
        self.assertEqual(PCM_BYTES_PER_SECOND, 32_000)

    def test_capture_rejects_invalid_block_size(self):
        with self.assertRaises(ValueError):
            WasapiLoopbackCapture(lambda _pcm: None, lambda _error: None, block_frames=0)
