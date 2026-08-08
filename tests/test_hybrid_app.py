import importlib.util
import os
import threading
import unittest


HAS_PYSIDE = importlib.util.find_spec("PySide6") is not None
HAS_WHISPER = importlib.util.find_spec("faster_whisper") is not None
HAS_HYBRID_DEPS = HAS_PYSIDE and HAS_WHISPER

if HAS_HYBRID_DEPS:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QTextCursor
    from PySide6.QtWidgets import QApplication

    from windows_port.app import AnswerTextEdit, InterviewController, TranscriptWindow


class _FakeFinalWorker:
    def __init__(self):
        self.jobs = []

    def submit(self, priority, processor, callback):
        self.jobs.append((priority, processor, callback))


class _FakePreviewWorker:
    @staticmethod
    def cancel():
        return 0.01


class _QueuedPreviewWorker:
    def __init__(self):
        self.jobs = []

    def submit(self, pcm, callback):
        self.jobs.append((pcm, callback))
        return True


class _FakeSegmenter:
    def __init__(self, marker):
        self.marker = marker

    def capture_question_marker(self):
        return dict(self.marker)


@unittest.skipUnless(HAS_HYBRID_DEPS, "PySide6 and faster-whisper are installed")
class HybridAppTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt_app = QApplication.instance() or QApplication([])

    def test_answer_scroll_position_is_preserved_when_user_scrolled_up(self):
        window = TranscriptWindow("ANSWER", answer=True)
        window.resize(400, 180)
        window.show()
        window.set_text("\n".join(f"line {number}" for number in range(100)))
        self.qt_app.processEvents()
        scrollbar = window.text.verticalScrollBar()
        self.assertGreater(scrollbar.maximum(), 0)
        scrollbar.setValue(0)

        window.append_text("\nnew streamed line")
        self.qt_app.processEvents()

        self.assertEqual(scrollbar.value(), 0)
        window.force_close()

    def test_answer_does_not_auto_follow_when_streaming_at_bottom(self):
        window = TranscriptWindow("ANSWER", answer=True)
        window.resize(400, 180)
        window.show()
        window.set_text("\n".join(f"line {number}" for number in range(100)))
        self.qt_app.processEvents()
        scrollbar = window.text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
        previous_value = scrollbar.value()

        window.append_text("\nnew streamed line")
        self.qt_app.processEvents()

        self.assertEqual(scrollbar.value(), previous_value)
        self.assertLess(scrollbar.value(), scrollbar.maximum())
        window.force_close()

    def test_new_answer_resets_scroll_to_top(self):
        window = TranscriptWindow("ANSWER", answer=True)
        window.resize(400, 180)
        window.show()
        window.set_text("\n".join(f"line {number}" for number in range(100)))
        self.qt_app.processEvents()
        scrollbar = window.text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

        window.set_text("Thinking…")
        self.qt_app.processEvents()

        self.assertEqual(scrollbar.value(), 0)
        window.force_close()

    def test_transcript_window_does_not_accept_focus(self):
        window = TranscriptWindow("ANSWER", answer=True)

        self.assertTrue(window.windowFlags() & Qt.WindowDoesNotAcceptFocus)
        self.assertEqual(window.focusPolicy(), Qt.NoFocus)
        self.assertEqual(window.text.focusPolicy(), Qt.NoFocus)
        window.force_close()

    def test_answer_has_passive_three_line_position_guide(self):
        answer = TranscriptWindow("ANSWER", answer=True)
        interviewer = TranscriptWindow("INTERVIEWER")
        answer.resize(400, 180)
        answer.show()
        self.qt_app.processEvents()

        self.assertIsInstance(answer.text, AnswerTextEdit)
        guide = answer.text.position_guide
        self.assertEqual(
            guide.height(),
            answer.text.fontMetrics().lineSpacing() * 3,
        )
        self.assertEqual(guide.width(), answer.text.viewport().width())
        self.assertTrue(guide.testAttribute(Qt.WA_TransparentForMouseEvents))
        self.assertEqual(guide.focusPolicy(), Qt.NoFocus)
        self.assertNotIsInstance(interviewer.text, AnswerTextEdit)
        answer.force_close()
        interviewer.force_close()

    def test_answer_position_guide_remains_fixed_while_scrolling(self):
        answer = TranscriptWindow("ANSWER", answer=True)
        answer.resize(400, 180)
        answer.set_text("\n".join(f"line {number}" for number in range(100)))
        answer.show()
        self.qt_app.processEvents()
        scrollbar = answer.text.verticalScrollBar()
        guide = answer.text.position_guide
        original_geometry = guide.geometry()
        self.assertGreater(scrollbar.maximum(), 0)

        scrollbar.setValue(scrollbar.maximum())
        self.qt_app.processEvents()

        self.assertTrue(guide.isVisible())
        self.assertEqual(guide.geometry(), original_geometry)
        self.assertEqual(guide.y(), 0)
        answer.force_close()

    def test_answer_has_scroll_space_after_the_last_section(self):
        answer = TranscriptWindow("ANSWER", answer=True)
        answer.resize(400, 180)
        answer.set_text("\n".join(f"line {number}" for number in range(30)))
        answer.show()
        self.qt_app.processEvents()
        scrollbar = answer.text.verticalScrollBar()
        spacer_chars = answer.text._tail_spacer_chars

        self.assertGreaterEqual(
            spacer_chars * answer.text.fontMetrics().lineSpacing(),
            answer.text.viewport().height() * 2,
        )
        scrollbar.setValue(scrollbar.maximum())
        self.qt_app.processEvents()
        end_cursor = QTextCursor(answer.text.document())
        end_cursor.movePosition(QTextCursor.End)
        end_cursor.movePosition(
            QTextCursor.PreviousCharacter,
            QTextCursor.MoveAnchor,
            spacer_chars,
        )
        self.assertLess(
            answer.text.cursorRect(end_cursor).top(),
            answer.text.position_guide.geometry().bottom(),
        )
        answer.force_close()

    def test_long_stream_never_changes_current_answer_view(self):
        answer = TranscriptWindow("ANSWER", answer=True)
        answer.resize(400, 180)
        answer.set_text("\n".join(f"existing line {number}" for number in range(100)))
        answer.show()
        self.qt_app.processEvents()
        scrollbar = answer.text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum() // 2)
        anchored_value = scrollbar.value()

        for batch in range(10):
            answer.append_text(
                "\n" + "\n".join(
                    f"stream batch {batch} line {number}" for number in range(20)
                )
            )
            self.qt_app.processEvents()
            self.assertEqual(scrollbar.value(), anchored_value)

        answer.force_close()

    def test_long_stream_stays_at_top_until_user_scrolls(self):
        answer = TranscriptWindow("ANSWER", answer=True)
        answer.resize(400, 180)
        answer.set_text("First streamed words")
        answer.show()
        self.qt_app.processEvents()
        scrollbar = answer.text.verticalScrollBar()

        answer.append_text("\n" + "\n".join(f"line {number}" for number in range(200)))
        for _ in range(3):
            self.qt_app.processEvents()
        self.assertEqual(scrollbar.value(), 0)

        scrollbar.setValue(scrollbar.maximum() // 2)
        user_position = scrollbar.value()
        answer.append_text("\nfinal streamed text")
        self.qt_app.processEvents()
        self.assertEqual(scrollbar.value(), user_position)
        answer.force_close()

    def test_streaming_does_not_insert_tail_space_between_deltas(self):
        answer = TranscriptWindow("ANSWER", answer=True)
        answer.resize(720, 300)
        answer.show()
        self.qt_app.processEvents()
        chunks = ["One project"] + [f" chunk-{number:03d}" for number in range(1, 107)]

        answer.set_text(chunks[0])
        for chunk in chunks[1:]:
            answer.append_text(chunk)
            self.qt_app.processEvents()

        actual = answer.text.toPlainText()
        content = actual[:-answer.text._tail_spacer_chars]
        self.assertEqual(content, "".join(chunks))
        self.assertNotIn("\n", content)
        self.assertEqual(answer.text.verticalScrollBar().value(), 0)
        answer.force_close()

    def test_background_utterance_waits_until_f8_before_final_whisper(self):
        controller = InterviewController.__new__(InterviewController)
        controller.utterance_count = 0
        controller.session_dir = None
        controller.log_path = None
        controller.remote_utterances = []
        controller.pending_questions = []
        controller.conversation_context = []
        controller.lock = threading.Lock()
        controller.whisper_worker = _FakeFinalWorker()

        controller._final_audio(bytes(32_000), 0, 32_000, {"vad_method": "test"})

        self.assertEqual(controller.whisper_worker.jobs, [])
        self.assertFalse(controller.remote_utterances[0]["submitted"])

        controller.last_f8 = None
        controller.question_count = 0
        controller.preview_worker = _FakePreviewWorker()
        controller.preview_pending = False
        controller.latest_preview_pcm = None
        controller.preview_lock = threading.Lock()
        controller.segmenter = _FakeSegmenter({
            "trigger": 31_000,
            "suggested_start": 0,
            "target_span": (0, 32_000),
            "source": "completed",
        })

        controller.on_f8()

        self.assertEqual(len(controller.whisper_worker.jobs), 1)
        self.assertEqual(controller.whisper_worker.jobs[0][0], 0)
        self.assertTrue(controller.remote_utterances[0]["submitted"])

    def test_preview_keeps_latest_snapshot_while_inference_is_busy(self):
        controller = InterviewController.__new__(InterviewController)
        controller.preview_pending = False
        controller.latest_preview_pcm = None
        controller.preview_lock = threading.Lock()
        controller.preview_worker = _QueuedPreviewWorker()
        controller.running = True

        controller._preview_audio(b"first")
        controller._preview_audio(b"latest")

        self.assertEqual(len(controller.preview_worker.jobs), 1)
        self.assertEqual(controller.latest_preview_pcm, b"latest")

        first_callback = controller.preview_worker.jobs[0][1]
        first_callback("", None, 0.1)

        self.assertEqual(len(controller.preview_worker.jobs), 2)
        self.assertEqual(controller.preview_worker.jobs[1][0], b"latest")
        self.assertTrue(controller.preview_pending)


if __name__ == "__main__":
    unittest.main()
