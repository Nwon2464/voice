import sys
import threading
import time
import unittest
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import patch

from moonshine_streaming_worker import (
    MoonshineStreamingWorker,
    lines_display_text,
    lines_question_text,
)


@dataclass
class _Line:
    text: str
    start_time: float
    duration: float
    line_id: int
    is_complete: bool
    is_new: bool = True
    is_updated: bool = True
    has_text_changed: bool = True


@dataclass
class _Transcript:
    lines: list


class _Stream:
    def __init__(self):
        self.listeners = []
        self.samples = 0
        self.closed = False
        self.update_count = 0

    def add_listener(self, listener):
        self.listeners.append(listener)

    def remove_listener(self, listener):
        self.listeners.remove(listener)

    def start(self):
        pass

    def add_audio(self, audio, sample_rate):
        self.samples += len(audio)
        line = _Line("live preview", 0, self.samples / sample_rate, 1, False)
        for listener in self.listeners:
            listener.on_line_text_changed(type("Event", (), {"line": line})())

    def update_transcription(self, _flag):
        self.update_count += 1
        return _Transcript([
            _Line("first line", 0, 0.5, 1, True),
            _Line("second line", 0.5, 0.5, 2, False),
        ])

    def close(self):
        self.closed = True


class _Transcriber:
    def __init__(self):
        self.streams = []
        self.closed = False

    def create_stream(self):
        stream = _Stream()
        self.streams.append(stream)
        return stream

    def close(self):
        self.closed = True


class _FailingStream(_Stream):
    def update_transcription(self, _flag):
        raise RuntimeError("force update failed")


class _FailingTranscriber(_Transcriber):
    def create_stream(self):
        stream = _FailingStream()
        self.streams.append(stream)
        return stream


class MoonshineStreamingWorkerTests(unittest.TestCase):
    @staticmethod
    def _pcm(amplitude):
        return int(amplitude).to_bytes(2, "little", signed=True) * 160

    @staticmethod
    def _wait_for_cursor(worker, cursor, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if worker.consumed_sample_cursor >= cursor:
                return True
            time.sleep(0.005)
        return False

    def test_text_helpers_keep_lines_for_ui_and_flatten_question(self):
        lines = [{"text": "one"}, {"text": "two"}]
        self.assertEqual(lines_display_text(lines), "one\ntwo")
        self.assertEqual(lines_question_text(lines), "one two")

    def test_default_english_uses_small_streaming_model_arch(self):
        self._assert_default_model_selection(
            language="en",
            arch_name="SMALL_STREAMING",
            max_tokens_per_second=None,
        )

    def test_japanese_uses_base_model_arch(self):
        self._assert_default_model_selection(
            language="ja",
            arch_name="BASE",
            max_tokens_per_second="13.0",
        )

    def _assert_default_model_selection(
        self,
        language,
        arch_name,
        max_tokens_per_second,
    ):
        selected = {}
        model_arch = SimpleNamespace(
            SMALL_STREAMING=object(),
            BASE=object(),
        )

        def get_model_for_language(selected_language, selected_arch):
            selected["language"] = selected_language
            selected["arch"] = selected_arch
            return "/models/test", selected_arch

        class Transcriber:
            def __init__(self, **kwargs):
                selected["transcriber"] = kwargs

        moonshine_module = SimpleNamespace(
            ModelArch=model_arch,
            Transcriber=Transcriber,
            get_model_for_language=get_model_for_language,
        )
        transcriber_module = SimpleNamespace(MOONSHINE_FLAG_FORCE_UPDATE=99)
        worker = MoonshineStreamingWorker(
            lambda *_args: None,
            lambda *_args: None,
            lambda *_args: None,
            language=language,
        )

        with patch.dict(sys.modules, {
            "moonshine_voice": moonshine_module,
            "moonshine_voice.transcriber": transcriber_module,
        }):
            _transcriber, force_flag = worker._default_engine_factory()

        expected_arch = getattr(model_arch, arch_name)
        self.assertEqual(selected["language"], language)
        self.assertIs(selected["arch"], expected_arch)
        self.assertIs(selected["transcriber"]["model_arch"], expected_arch)
        self.assertEqual(selected["transcriber"]["model_path"], "/models/test")
        options = selected["transcriber"]["options"]
        self.assertEqual(options["return_audio_data"], "false")
        if max_tokens_per_second is None:
            self.assertNotIn("max_tokens_per_second", options)
        else:
            self.assertEqual(
                options["max_tokens_per_second"],
                max_tokens_per_second,
            )
        self.assertEqual(force_flag, 99)

    def test_pcm_barrier_force_snapshot_and_stream_reset(self):
        transcriber = _Transcriber()
        ready = threading.Event()
        done = threading.Event()
        previews = []
        results = []
        errors = []
        worker = MoonshineStreamingWorker(
            lambda _result, error: ready.set() if error is None else errors.append(error),
            previews.append,
            errors.append,
            engine_factory=lambda: transcriber,
            force_update_flag=99,
        )
        worker.start()
        self.assertTrue(ready.wait(timeout=2))
        worker.submit_pcm(bytes(320), 0, 160)
        worker.submit_pcm(bytes(320), 160, 320)
        worker.request_snapshot(
            320,
            lambda result, error: (
                results.append((result, error)),
                done.set(),
            ),
        )
        self.assertTrue(done.wait(timeout=2))
        worker.stop()

        result, error = results[0]
        self.assertIsNone(error)
        self.assertEqual(result["text"], "first line second line")
        self.assertEqual(result["target_sample_cursor"], 320)
        self.assertEqual(result["consumed_sample_cursor"], 320)
        self.assertTrue(result["cursor_complete"])
        self.assertEqual(result["audio_drop_samples"], 0)
        self.assertEqual(result["commit_source"], "f8")
        self.assertTrue(result["committed"])
        self.assertTrue(previews)
        self.assertGreaterEqual(len(transcriber.streams), 2)
        self.assertTrue(transcriber.closed)
        self.assertEqual(errors, [])

    def test_rejects_non_contiguous_pcm_and_non_atomic_f8_cursor(self):
        worker = MoonshineStreamingWorker(
            lambda *_args: None,
            lambda *_args: None,
            lambda *_args: None,
        )
        worker.accepting = True
        with self.assertRaisesRegex(ValueError, "non-contiguous"):
            worker.submit_pcm(bytes(20), 5, 15)
        with self.assertRaisesRegex(ValueError, "target must equal"):
            worker.request_snapshot(5, lambda *_args: None)

    def test_force_update_error_completes_current_f8_callback_once(self):
        ready = threading.Event()
        done = threading.Event()
        callbacks = []
        worker = MoonshineStreamingWorker(
            lambda _result, _error: ready.set(),
            lambda *_args: None,
            lambda *_args: None,
            engine_factory=_FailingTranscriber,
            force_update_flag=99,
        )
        worker.start()
        self.assertTrue(ready.wait(timeout=2))
        worker.submit_pcm(bytes(320), 0, 160)
        worker.request_snapshot(
            160,
            lambda result, error: (
                callbacks.append((result, error)),
                done.set(),
            ),
        )

        self.assertTrue(done.wait(timeout=2))
        worker.stop()
        self.assertEqual(len(callbacks), 1)
        self.assertIsNone(callbacks[0][0])
        self.assertRegex(str(callbacks[0][1]), "force update failed")

    def test_auto_commit_requires_speech_and_1500ms_continuous_silence(self):
        transcriber = _Transcriber()
        ready = threading.Event()
        committed = threading.Event()
        results = []
        worker = MoonshineStreamingWorker(
            lambda _result, _error: ready.set(),
            lambda *_args: None,
            lambda *_args: None,
            on_auto_commit=lambda result, error: (
                results.append((result, error)),
                committed.set(),
            ),
            engine_factory=lambda: transcriber,
            force_update_flag=99,
        )
        worker.start()
        self.assertTrue(ready.wait(timeout=2))

        cursor = 0
        for _ in range(150):
            worker.submit_pcm(self._pcm(0), cursor, cursor + 160)
            cursor += 160
        self.assertTrue(self._wait_for_cursor(worker, cursor))
        self.assertFalse(committed.is_set())

        worker.submit_pcm(self._pcm(1000), cursor, cursor + 160)
        cursor += 160
        for _ in range(149):
            worker.submit_pcm(self._pcm(0), cursor, cursor + 160)
            cursor += 160
        self.assertTrue(self._wait_for_cursor(worker, cursor))
        self.assertFalse(committed.is_set())

        worker.submit_pcm(self._pcm(0), cursor, cursor + 160)
        cursor += 160
        self.assertTrue(committed.wait(timeout=2))
        result, error = results[0]
        self.assertIsNone(error)
        self.assertEqual(result["commit_source"], "silence")
        self.assertEqual(result["target_sample_cursor"], cursor)
        self.assertEqual(result["queued_sample_cursor"], cursor)
        self.assertEqual(result["consumed_sample_cursor"], cursor)
        self.assertTrue(result["cursor_complete"])
        self.assertEqual(result["audio_drop_samples"], 0)

        for _ in range(20):
            worker.submit_pcm(self._pcm(0), cursor, cursor + 160)
            cursor += 160
        self.assertTrue(self._wait_for_cursor(worker, cursor))
        worker.stop()
        self.assertEqual(len(results), 1)

    def test_new_speech_resets_auto_silence_counter(self):
        transcriber = _Transcriber()
        ready = threading.Event()
        committed = threading.Event()
        worker = MoonshineStreamingWorker(
            lambda _result, _error: ready.set(),
            lambda *_args: None,
            lambda *_args: None,
            on_auto_commit=lambda *_args: committed.set(),
            engine_factory=lambda: transcriber,
            auto_silence_ms=30,
        )
        worker.start()
        self.assertTrue(ready.wait(timeout=2))
        chunks = [1000, 0, 0, 1000, 0, 0]
        cursor = 0
        for amplitude in chunks:
            worker.submit_pcm(self._pcm(amplitude), cursor, cursor + 160)
            cursor += 160
        self.assertTrue(self._wait_for_cursor(worker, cursor))
        self.assertFalse(committed.is_set())
        worker.submit_pcm(self._pcm(0), cursor, cursor + 160)
        self.assertTrue(committed.wait(timeout=2))
        worker.stop()

    def test_f8_after_auto_without_new_speech_is_suppressed(self):
        transcriber = _Transcriber()
        ready = threading.Event()
        auto_done = threading.Event()
        f8_done = threading.Event()
        auto_results = []
        f8_results = []
        worker = MoonshineStreamingWorker(
            lambda _result, _error: ready.set(),
            lambda *_args: None,
            lambda *_args: None,
            on_auto_commit=lambda result, error: (
                auto_results.append((result, error)),
                auto_done.set(),
            ),
            engine_factory=lambda: transcriber,
            auto_silence_ms=20,
        )
        worker.start()
        self.assertTrue(ready.wait(timeout=2))
        cursor = 0
        for amplitude in (1000, 0, 0):
            worker.submit_pcm(self._pcm(amplitude), cursor, cursor + 160)
            cursor += 160
        self.assertTrue(auto_done.wait(timeout=2))

        worker.submit_pcm(self._pcm(0), cursor, cursor + 160)
        cursor += 160
        worker.request_snapshot(
            cursor,
            lambda result, error: (
                f8_results.append((result, error)),
                f8_done.set(),
            ),
        )
        self.assertTrue(f8_done.wait(timeout=2))
        worker.stop()

        self.assertTrue(auto_results[0][0]["committed"])
        self.assertFalse(f8_results[0][0]["committed"])
        self.assertTrue(f8_results[0][0]["duplicate_suppressed"])
        self.assertEqual(f8_results[0][0]["commit_source"], "f8")
        self.assertEqual(sum(stream.update_count for stream in transcriber.streams), 1)

    def test_f8_commit_prevents_later_auto_commit_without_new_speech(self):
        transcriber = _Transcriber()
        ready = threading.Event()
        f8_done = threading.Event()
        auto_results = []
        worker = MoonshineStreamingWorker(
            lambda _result, _error: ready.set(),
            lambda *_args: None,
            lambda *_args: None,
            on_auto_commit=lambda result, error: auto_results.append((result, error)),
            engine_factory=lambda: transcriber,
            auto_silence_ms=20,
        )
        worker.start()
        self.assertTrue(ready.wait(timeout=2))
        cursor = 0
        worker.submit_pcm(self._pcm(1000), cursor, cursor + 160)
        cursor += 160
        worker.request_snapshot(
            cursor,
            lambda *_args: f8_done.set(),
        )
        self.assertTrue(f8_done.wait(timeout=2))
        for _ in range(5):
            worker.submit_pcm(self._pcm(0), cursor, cursor + 160)
            cursor += 160
        self.assertTrue(self._wait_for_cursor(worker, cursor))
        worker.stop()
        self.assertEqual(auto_results, [])

    def test_stop_times_out_instead_of_waiting_forever(self):
        errors = []

        class StuckThread:
            def __init__(self):
                self.join_timeout = None

            def join(self, timeout=None):
                self.join_timeout = timeout

            def is_alive(self):
                return True

        worker = MoonshineStreamingWorker(
            lambda *_args: None,
            lambda *_args: None,
            errors.append,
        )
        stuck = StuckThread()
        worker.thread = stuck
        worker.accepting = True

        worker.stop()

        self.assertEqual(stuck.join_timeout, 5.0)
        self.assertFalse(worker.accepting)
        self.assertIs(worker.thread, stuck)
        self.assertEqual(len(errors), 1)
        self.assertIn("did not stop within 5 seconds", str(errors[0]))


if __name__ == "__main__":
    unittest.main()
