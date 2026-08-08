import importlib.util
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

HAS_FASTER_WHISPER = importlib.util.find_spec("faster_whisper") is not None
HAS_INTERVIEW_APP_DEPS = False
if HAS_FASTER_WHISPER:
    try:
        import interview_app
        HAS_INTERVIEW_APP_DEPS = True
    except (ModuleNotFoundError, ValueError):
        interview_app = None


class _Segment:
    text = "final utterance before shutdown"


class _FakeWhisperModel:
    def __init__(self, *_args, **_kwargs):
        pass

    def transcribe(self, *_args, **_kwargs):
        return iter([_Segment()]), None


@unittest.skipUnless(
    HAS_INTERVIEW_APP_DEPS,
    "faster_whisper and GTK are available in the application environment",
)
class InterviewAppShutdownTest(unittest.TestCase):
    def test_pending_utterance_is_logged_before_worker_stops(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            app = interview_app.InterviewApp.__new__(
                interview_app.InterviewApp
            )
            app.session_dir = root
            app.log_path = root / "session.jsonl"
            app.interviewer_utterance_count = 0
            app.remote_utterances = []
            app.pending_questions = []
            app.transcript_lock = threading.Lock()

            with patch.object(
                interview_app,
                "WhisperModel",
                _FakeWhisperModel,
            ):
                app.worker = interview_app.WhisperWorker(
                    lambda *_args: False
                )
                pcm_audio = bytes(32_000)
                app._final_audio(
                    "INTERVIEWER",
                    pcm_audio,
                    0,
                    len(pcm_audio),
                    {"vad_method": "test"},
                )
                app.worker.stop()

            events = [
                json.loads(line)
                for line in app.log_path.read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertEqual(len(list(root.glob("interviewer_*.wav"))), 1)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["event"], "utterance")
            self.assertEqual(
                events[0]["text"],
                "final utterance before shutdown",
            )
            self.assertFalse(app.worker.thread.is_alive())


if __name__ == "__main__":
    unittest.main()
