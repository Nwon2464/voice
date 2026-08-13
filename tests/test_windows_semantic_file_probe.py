import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

from windows_port.semantic_file_probe import (
    CHUNK_SAMPLES,
    compare_transcripts,
    find_silence_boundary,
    normalize_transcript,
    prepare_pcm,
    wave_metadata,
)


class WindowsSemanticFileProbeTests(unittest.TestCase):
    def _write_wave(self, path, *, sample_rate, samples):
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(sample_rate)
            output.writeframes(np.asarray(samples, dtype="<i2").tobytes())

    def test_prepare_pcm_converts_24khz_input_to_16khz_test_wav(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.wav"
            self._write_wave(source, sample_rate=24_000, samples=np.arange(240, dtype=np.int16))

            pcm, source_metadata, converted_metadata = prepare_pcm(source)

        self.assertEqual(source_metadata["sample_rate"], 24_000)
        self.assertEqual(len(pcm) // 2, 160)
        self.assertIsNotNone(converted_metadata)
        self.assertEqual(converted_metadata["sample_rate"], 16_000)
        self.assertEqual(converted_metadata["channels"], 1)
        self.assertEqual(converted_metadata["sample_width_bytes"], 2)

    def test_prepare_pcm_keeps_native_bridge_format_without_conversion(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "native.wav"
            self._write_wave(source, sample_rate=16_000, samples=np.arange(160, dtype=np.int16))

            pcm, source_metadata, converted_metadata = prepare_pcm(source)

        self.assertEqual(len(pcm), 320)
        self.assertEqual(source_metadata["total_frames"], 160)
        self.assertIsNone(converted_metadata)

    def test_finds_a_natural_silence_boundary_in_the_middle_range(self):
        samples = np.full(CHUNK_SAMPLES * 10, 1000, dtype=np.int16)
        samples[CHUNK_SAMPLES * 6:CHUNK_SAMPLES * 7] = 0

        boundary = find_silence_boundary(samples.tobytes())

        self.assertEqual(boundary["split_cursor"], CHUNK_SAMPLES * 6)
        self.assertEqual(boundary["split_rms"], 0.0)

    def test_comparison_normalizes_japanese_spacing_and_english_case_punctuation(self):
        ja = compare_transcripts("質問 です。", "質問です", "ja")
        en = compare_transcripts("Why This Role?", "why this role", "en")

        self.assertTrue(ja["normalized_exact_match"])
        self.assertTrue(en["normalized_exact_match"])
        self.assertEqual(normalize_transcript("A, B!", "en"), "a b")
