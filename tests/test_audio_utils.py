import unittest

import numpy as np

from audio_utils import BYTES_PER_SECOND, transcribe_question


class _Word:
    def __init__(self, start, end, text):
        self.start = start
        self.end = end
        self.word = text


class _Segment:
    def __init__(self, words):
        self.words = words
        self.text = "".join(word.word for word in words)


class _Info:
    language = "en"


class _Model:
    def __init__(self, words):
        self.words = words

    def transcribe(self, *_args, **_kwargs):
        return iter([_Segment(self.words)]), _Info()


def _pcm(seconds):
    return np.zeros(int(16_000 * seconds), dtype=np.int16).tobytes()


def _trigger_bytes(seconds):
    return int(seconds * BYTES_PER_SECOND)


class QuestionBoundaryTest(unittest.TestCase):
    def test_last_word_survives_f8_before_during_and_after_it(self):
        words = [
            _Word(0.00, 0.30, " seem"),
            _Word(0.30, 0.45, " to"),
            _Word(0.45, 0.75, " defy"),
            _Word(0.75, 0.95, " all"),
            _Word(0.95, 1.05, " of"),
            _Word(1.05, 1.20, " the"),
            _Word(1.20, 1.70, " assumptions?"),
        ]
        expected = "seem to defy all of the assumptions?"

        for trigger in (1.15, 1.40, 1.85):
            with self.subTest(trigger=trigger):
                text, boundary, replay_start, details = transcribe_question(
                    _Model(words),
                    _pcm(trigger + 0.6),
                    _trigger_bytes(trigger),
                    silence_padding_ms=200,
                )
                self.assertEqual(text, expected)
                self.assertGreaterEqual(
                    boundary / BYTES_PER_SECOND,
                    words[-1].end,
                )
                self.assertEqual(details["boundary_reason"], "punctuation")
                self.assertEqual(replay_start, len(_pcm(trigger + 0.6)))

    def test_first_sentence_boundary_leaves_second_sentence_in_remainder(self):
        words = [
            _Word(0.00, 0.30, " How"),
            _Word(0.30, 0.48, " do"),
            _Word(0.48, 0.65, " you"),
            _Word(0.65, 1.05, " explain"),
            _Word(1.05, 1.22, " that?"),
            _Word(1.45, 1.62, " Or"),
            _Word(1.62, 1.95, " better,"),
            _Word(1.95, 2.15, " why"),
        ]
        pcm_audio = _pcm(2.2)
        text, boundary, replay_start, details = transcribe_question(
            _Model(words),
            pcm_audio,
            _trigger_bytes(1.35),
            silence_padding_ms=200,
        )

        self.assertEqual(text, "How do you explain that?")
        self.assertAlmostEqual(
            boundary / BYTES_PER_SECOND,
            1.22,
            places=2,
        )
        self.assertEqual(
            len(pcm_audio[boundary:]),
            len(pcm_audio) - boundary,
        )
        self.assertEqual(details["boundary_reason"], "punctuation")
        self.assertAlmostEqual(
            replay_start / BYTES_PER_SECOND,
            1.37,
            places=2,
        )
        self.assertEqual(details["replay_reason"], "next_word")

    def test_trailing_silence_is_used_without_punctuation(self):
        words = [
            _Word(0.00, 0.28, " Why"),
            _Word(0.28, 0.48, " does"),
            _Word(0.48, 0.62, " this"),
            _Word(0.62, 1.10, " happen"),
        ]
        text, boundary, replay_start, details = transcribe_question(
            _Model(words),
            _pcm(1.8),
            _trigger_bytes(1.25),
            silence_padding_ms=200,
        )

        self.assertEqual(text, "Why does this happen")
        self.assertAlmostEqual(
            boundary / BYTES_PER_SECOND,
            words[-1].end,
            places=2,
        )
        self.assertEqual(details["boundary_reason"], "trailing_silence")
        self.assertEqual(replay_start, len(_pcm(1.8)))
        self.assertEqual(details["replay_reason"], "no_following_speech")

    def test_late_f8_keeps_started_next_sentence_in_remainder(self):
        words = [
            _Word(0.00, 0.28, " How"),
            _Word(0.28, 0.45, " do"),
            _Word(0.45, 0.62, " you"),
            _Word(0.62, 1.02, " explain"),
            _Word(1.02, 1.20, " that?"),
            _Word(1.48, 1.65, " Or"),
            _Word(1.65, 1.90, " better,"),
            _Word(1.90, 2.05, " why"),
            _Word(2.05, 2.18, " does"),
            _Word(2.18, 2.30, " this"),
            _Word(2.30, 2.45, " happen?"),
        ]
        pcm_audio = _pcm(2.6)
        text, boundary, replay_start, details = transcribe_question(
            _Model(words),
            pcm_audio,
            _trigger_bytes(2.00),
            silence_padding_ms=200,
        )

        self.assertEqual(text, "How do you explain that?")
        self.assertAlmostEqual(
            boundary / BYTES_PER_SECOND,
            1.20,
            places=2,
        )
        self.assertGreater(len(pcm_audio[boundary:]), 0)
        self.assertEqual(details["boundary_reason"], "punctuation")
        self.assertAlmostEqual(
            replay_start / BYTES_PER_SECOND,
            1.40,
            places=2,
        )
        self.assertEqual(details["replay_reason"], "next_word")

    def test_word_tail_followed_only_by_silence_is_not_replayed(self):
        words = [_Word(0.00, 1.00, " assumptions?")]
        samples = np.zeros(int(16_000 * 2.0), dtype=np.int16)
        samples[int(16_000 * 1.00):int(16_000 * 1.34)] = 1_000
        pcm_audio = samples.tobytes()

        text, boundary, replay_start, details = transcribe_question(
            _Model(words),
            pcm_audio,
            _trigger_bytes(1.05),
            silence_padding_ms=200,
        )

        self.assertEqual(text, "assumptions?")
        self.assertAlmostEqual(boundary / BYTES_PER_SECOND, 1.00, places=2)
        self.assertEqual(replay_start, len(pcm_audio))
        self.assertEqual(details["replay_reason"], "no_following_speech")

    def test_unrecognized_new_speech_after_silence_is_replayed(self):
        words = [_Word(0.00, 1.00, " question?")]
        samples = np.zeros(int(16_000 * 2.0), dtype=np.int16)
        samples[int(16_000 * 1.00):int(16_000 * 1.12)] = 1_000
        samples[int(16_000 * 1.40):int(16_000 * 1.65)] = 1_000
        pcm_audio = samples.tobytes()

        text, boundary, replay_start, details = transcribe_question(
            _Model(words),
            pcm_audio,
            _trigger_bytes(1.05),
            silence_padding_ms=200,
        )

        self.assertEqual(text, "question?")
        self.assertAlmostEqual(boundary / BYTES_PER_SECOND, 1.00, places=2)
        self.assertAlmostEqual(
            replay_start / BYTES_PER_SECOND,
            1.32,
            places=2,
        )
        self.assertEqual(
            details["replay_reason"],
            "new_speech_after_silence",
        )


if __name__ == "__main__":
    unittest.main()
