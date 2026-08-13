import tempfile
import unittest
import wave
from pathlib import Path

from windows_port.audio_probe import pcm_statistics, save_wav


class WindowsAudioProbeTests(unittest.TestCase):
    def test_statistics_detects_audible_signed_int16_pcm(self):
        pcm = (1000).to_bytes(2, "little", signed=True) * 160
        report = pcm_statistics(pcm)
        self.assertTrue(report["audio_detected"])
        self.assertEqual(report["peak"], 1000)
        self.assertEqual(report["rms"], 1000.0)

    def test_statistics_marks_silence_as_not_detected(self):
        self.assertFalse(pcm_statistics(bytes(320))["audio_detected"])

    def test_save_wav_preserves_bridge_format(self):
        pcm = (123).to_bytes(2, "little", signed=True) * 4
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "probe.wav"
            save_wav(path, pcm)
            with wave.open(str(path), "rb") as saved:
                self.assertEqual(saved.getparams()[:3], (1, 2, 16_000))
                self.assertEqual(saved.readframes(4), pcm)
