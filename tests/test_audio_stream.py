import importlib.util
import unittest

HAS_NUMPY = importlib.util.find_spec("numpy") is not None
if HAS_NUMPY:
    import numpy as np

    from audio_stream import PcmSpeechSegmenter
    from audio_utils import BYTES_PER_SECOND


def pcm(milliseconds, amplitude):
    frames = int(16_000 * milliseconds / 1000)
    return np.full(frames, amplitude, dtype=np.int16).tobytes()


@unittest.skipUnless(HAS_NUMPY, "numpy is installed in the application environment")
class PcmSpeechSegmenterTest(unittest.TestCase):
    def test_speech_and_silence_emit_one_utterance(self):
        utterances = []
        segmenter = PcmSpeechSegmenter(
            lambda _audio: None,
            lambda *result: utterances.append(result),
            vad_rms=250,
        )

        segmenter.feed(pcm(100, 1000))
        for _ in range(10):
            segmenter.feed(pcm(100, 1000))
        for _ in range(10):
            segmenter.feed(pcm(100, 0))

        self.assertEqual(len(utterances), 1)
        audio, start, end, details = utterances[0]
        self.assertGreater(len(audio), BYTES_PER_SECOND)
        self.assertEqual(end - start, len(audio))
        self.assertEqual(details["vad_method"], "rms")

    def test_f8_marker_finishes_active_speech_after_post_context(self):
        utterances = []
        segmenter = PcmSpeechSegmenter(
            lambda _audio: None,
            lambda *result: utterances.append(result),
            vad_rms=250,
        )
        segmenter.feed(pcm(100, 1000))
        marker = segmenter.capture_question_marker()
        for _ in range(6):
            segmenter.feed(pcm(100, 1000))

        self.assertEqual(marker["source"], "active")
        self.assertEqual(len(utterances), 1)

    def test_quiet_input_does_not_start_an_utterance(self):
        utterances = []
        segmenter = PcmSpeechSegmenter(
            lambda _audio: None,
            lambda *result: utterances.append(result),
        )
        for _ in range(20):
            segmenter.feed(pcm(100, 0))

        self.assertFalse(segmenter.active)
        self.assertEqual(utterances, [])

    def test_first_preview_can_arrive_after_initial_preview_window(self):
        previews = []
        segmenter = PcmSpeechSegmenter(
            previews.append,
            lambda *_result: None,
            vad_rms=250,
            preview_interval_ms=600,
        )

        for _ in range(6):
            segmenter.feed(pcm(100, 1000))

        self.assertEqual(len(previews), 1)


if __name__ == "__main__":
    unittest.main()
