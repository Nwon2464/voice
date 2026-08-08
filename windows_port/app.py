"""WSL-hosted PySide6 app using a minimal native Windows bridge."""

import json
import math
import multiprocessing
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QEventLoop, QObject, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from audio_stream import PcmSpeechSegmenter
from audio_utils import BYTES_PER_SECOND, append_log, transcribe_question
from codex_app_server import CodexAppServerClient, CodexAppServerError
from session_store import SessionStore
from transcription import save_wav
from windows_port.bridge_client import WindowsBridgeClient
from windows_port.workers import CodexWorker, PreviewWhisperWorker, WhisperWorker


APP_DIR = Path(__file__).resolve().parents[1]
CONFIG_DIR = Path(
    os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
) / "interview-assistant"
SESSION_STORE_PATH = CONFIG_DIR / "sessions.json"
WINDOW_STATE_PATH = CONFIG_DIR / "window_state_qt.json"
WHISPER_MODEL = os.environ.get("INTERVIEW_WHISPER_MODEL", "small")
LANGUAGE = os.environ.get("INTERVIEW_LANGUAGE", "en")
CODEX_MODEL = os.environ.get("INTERVIEW_CODEX_MODEL", "gpt-5.6-sol")
CODEX_REASONING = os.environ.get("INTERVIEW_CODEX_REASONING", "low")
CODEX_TIMEOUT_SECONDS = 60
VAD_RMS = int(os.environ.get("INTERVIEW_VAD_RMS", "250"))
WHISPER_CPU_THREADS = int(os.environ.get("INTERVIEW_WHISPER_CPU_THREADS", "8"))
PREVIEW_WHISPER_CPU_THREADS = int(
    os.environ.get("INTERVIEW_PREVIEW_WHISPER_CPU_THREADS", "8")
)
TEST_LOGGING = os.environ.get("INTERVIEW_TEST_LOG", "0") != "0"
TEST_LABEL = os.environ.get("INTERVIEW_TEST_LABEL")
DEBOUNCE_SECONDS = 0.3
DEVELOPER_INSTRUCTIONS = """You assist a job candidate with interview preparation and live answers.
Follow the candidate's preferences, background, speaking style, and answer format established in the conversation.
When a turn contains CURRENT INTERVIEWER QUESTION, return an immediately speakable answer draft in the same language as that question.
Do not invent specific personal facts; ask during preparation or use adaptable wording when details are missing.
During the live interview, assume the candidate spoke your previous live-answer draft unless the later interviewer transcript indicates otherwise.
Each live-interview turn contains only conversation transcribed since the previous request plus the current interviewer question."""
SESSION_INITIALIZATION_TEXT = (
    "Interview Assistant persistent session initialized. "
    "This is background metadata, not an interview question."
)


class DispatchBridge(QObject):
    call = Signal(object)
    f8 = Signal()


class ChatInput(QTextEdit):
    send_requested = Signal()

    def keyPressEvent(self, event):
        if (
            event.key() in (Qt.Key_Return, Qt.Key_Enter)
            and not event.modifiers() & Qt.ShiftModifier
        ):
            self.send_requested.emit()
            return
        super().keyPressEvent(event)


class AnswerTextEdit(QTextEdit):
    """Read-only answer text with a passive three-line reading guide."""

    GUIDE_LINES = 3

    def __init__(self):
        super().__init__()
        self._scroll_lock_value = None
        self._scroll_lock_generation = 0
        self._tail_spacer_chars = 0
        self._tail_spacer_updating = False
        self.position_guide = QFrame(self.viewport())
        self.position_guide.setObjectName("answerPositionGuide")
        self.position_guide.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.position_guide.setFocusPolicy(Qt.NoFocus)
        self.position_guide.setStyleSheet(
            "#answerPositionGuide {"
            "border: 2px solid rgba(255, 195, 92, 191);"
            "background: transparent;"
            "}"
        )
        self.position_guide.show()
        self.verticalScrollBar().rangeChanged.connect(self._restore_scroll_lock)
        self.verticalScrollBar().valueChanged.connect(self._restore_scroll_lock)
        self.verticalScrollBar().valueChanged.connect(self._layout_position_guide)
        self.horizontalScrollBar().valueChanged.connect(self._layout_position_guide)

    def replace_text_at_top(self, text):
        self._begin_scroll_lock(0)
        self._tail_spacer_updating = True
        try:
            self._tail_spacer_chars = 0
            self.setPlainText(text)
            self._insert_tail_spacer()
        finally:
            self._tail_spacer_updating = False
        self._restore_scroll_lock()

    def append_text_without_scrolling(self, text):
        self._begin_scroll_lock(self.verticalScrollBar().value())
        self._tail_spacer_updating = True
        try:
            self._remove_tail_spacer()
            cursor = QTextCursor(self.document())
            cursor.movePosition(QTextCursor.End)
            cursor.insertText(text)
            self._insert_tail_spacer()
        finally:
            self._tail_spacer_updating = False
        self._restore_scroll_lock()

    def _remove_tail_spacer(self):
        if not self._tail_spacer_chars:
            return
        spacer_chars = self._tail_spacer_chars
        self._tail_spacer_chars = 0
        cursor = QTextCursor(self.document())
        cursor.movePosition(QTextCursor.End)
        cursor.movePosition(
            QTextCursor.PreviousCharacter,
            QTextCursor.KeepAnchor,
            spacer_chars,
        )
        cursor.removeSelectedText()

    def _desired_tail_spacer_chars(self):
        line_spacing = max(1, self.fontMetrics().lineSpacing())
        return max(
            self.GUIDE_LINES,
            math.ceil(self.viewport().height() * 2 / line_spacing),
        )

    def _insert_tail_spacer(self):
        desired_chars = self._desired_tail_spacer_chars()
        self._tail_spacer_chars = desired_chars
        cursor = QTextCursor(self.document())
        cursor.movePosition(QTextCursor.End)
        cursor.insertText("\n" * desired_chars)

    def _refresh_tail_spacer(self):
        if self._tail_spacer_updating:
            return
        desired_chars = self._desired_tail_spacer_chars()
        if desired_chars == self._tail_spacer_chars:
            return
        if self._scroll_lock_value is None:
            self._begin_scroll_lock(self.verticalScrollBar().value())
        self._tail_spacer_updating = True
        try:
            self._remove_tail_spacer()
            self._insert_tail_spacer()
        finally:
            self._tail_spacer_updating = False

    def _begin_scroll_lock(self, value):
        self._scroll_lock_generation += 1
        generation = self._scroll_lock_generation
        self._scroll_lock_value = value
        QTimer.singleShot(0, lambda: self._end_scroll_lock(generation))

    def _restore_scroll_lock(self, *_args):
        if self._scroll_lock_value is None:
            return
        scrollbar = self.verticalScrollBar()
        target = min(self._scroll_lock_value, scrollbar.maximum())
        if scrollbar.value() != target:
            scrollbar.setValue(target)

    def _end_scroll_lock(self, generation):
        if generation != self._scroll_lock_generation:
            return
        self._restore_scroll_lock()
        self._scroll_lock_value = None

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout_position_guide()
        self._refresh_tail_spacer()

    def _layout_position_guide(self, _value=None):
        guide_height = self.fontMetrics().lineSpacing() * self.GUIDE_LINES
        self.position_guide.setGeometry(0, 0, self.viewport().width(), guide_height)
        self.position_guide.show()
        self.position_guide.raise_()


def new_codex_client():
    return CodexAppServerClient(
        model=CODEX_MODEL,
        effort=CODEX_REASONING,
        cwd=APP_DIR,
        developer_instructions=DEVELOPER_INSTRUCTIONS,
        timeout_seconds=CODEX_TIMEOUT_SECONDS,
    )


def create_codex_session():
    client = new_codex_client()
    try:
        result = client.start(ephemeral=False)
        client.inject_items([{
            "type": "message",
            "role": "developer",
            "content": [{"type": "input_text", "text": SESSION_INITIALIZATION_TEXT}],
        }])
        return result
    finally:
        client.stop()


def archive_codex_session(thread_id):
    client = new_codex_client()
    try:
        client.connect()
        client.archive_thread(thread_id)
    finally:
        client.stop()


def create_test_session():
    if not TEST_LOGGING:
        return None, None
    stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    directory = APP_DIR / "test_runs" / f"hybrid_app_session_{stamp}_{os.getpid()}"
    directory.mkdir(parents=True, exist_ok=False)
    return directory, directory / "session.jsonl"


class SessionDialog(QDialog):
    def __init__(self, store):
        super().__init__()
        self.store = store
        self.thread_id = None
        self.setWindowTitle("Interview Assistant Sessions")
        self.resize(720, 420)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Select an interview session"))
        layout.addWidget(QLabel("Use Up/Down and press Enter to select."))
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _item: self._select())
        layout.addWidget(self.list)
        buttons = QHBoxLayout()
        new_button = QPushButton("New Session")
        archive_button = QPushButton("Archive Session")
        cancel_button = QPushButton("Cancel")
        select_button = QPushButton("Select")
        new_button.clicked.connect(self._new)
        archive_button.clicked.connect(self._archive)
        cancel_button.clicked.connect(self.reject)
        select_button.clicked.connect(self._select)
        for button in (new_button, archive_button, cancel_button, select_button):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self._reload()

    def _reload(self, preferred=None):
        self.list.clear()
        for session in self.store.active():
            item = QListWidgetItem(f"{session.get('name', '')}    {session['thread_id']}")
            item.setData(Qt.UserRole, session)
            self.list.addItem(item)
            if session["thread_id"] == preferred:
                self.list.setCurrentItem(item)
        if self.list.currentItem() is None and self.list.count():
            self.list.setCurrentRow(0)

    def _new(self):
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            result = create_codex_session()
            created = datetime.now().astimezone()
            self.store.add(
                result["thread_id"],
                created.strftime("%Y-%m-%d %H:%M"),
                created.isoformat(timespec="seconds"),
            )
            self._reload(result["thread_id"])
        except Exception as error:
            QMessageBox.critical(self, "Session Error", str(error))
        finally:
            QApplication.restoreOverrideCursor()

    def _archive(self):
        item = self.list.currentItem()
        if item is None:
            return
        session = item.data(Qt.UserRole)
        answer = QMessageBox.question(
            self,
            "Archive Session",
            f"Move {session['name']} to the Codex archive?",
        )
        if answer != QMessageBox.Yes:
            return
        try:
            archive_codex_session(session["thread_id"])
            self.store.mark_archived(session["thread_id"])
            self._reload()
        except Exception as error:
            if isinstance(error, CodexAppServerError) and "no rollout found" in str(error).lower():
                self.store.mark_archived(session["thread_id"])
                self._reload()
            else:
                QMessageBox.critical(self, "Session Error", str(error))

    def _select(self):
        item = self.list.currentItem()
        if item is None:
            return
        self.thread_id = item.data(Qt.UserRole)["thread_id"]
        self.store.mark_used(self.thread_id)
        self.accept()


class PreparationDialog(QDialog):
    START_INTERVIEW = 10
    BACK = 11

    def __init__(self, thread_id):
        super().__init__()
        self.thread_id = thread_id
        self.ready = False
        self.busy = False
        self.streaming = False
        self.bridge = DispatchBridge()
        self.bridge.call.connect(self._dispatch)
        self.worker = None
        self.setWindowTitle("Interview Preparation")
        self.resize(820, 640)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Interview Preparation\nSession ID: {thread_id}"))
        self.history = QTextEdit()
        self.history.setReadOnly(True)
        layout.addWidget(self.history)
        self.status = QLabel("Connecting to the Codex session…")
        layout.addWidget(self.status)
        row = QHBoxLayout()
        self.input = ChatInput()
        self.input.setMaximumHeight(100)
        self.input.send_requested.connect(self._send)
        self.send_button = QPushButton("Send")
        self.send_button.clicked.connect(self._send)
        row.addWidget(self.input)
        row.addWidget(self.send_button)
        layout.addLayout(row)
        buttons = QHBoxLayout()
        back = QPushButton("Back")
        self.start_button = QPushButton("Start Interview")
        back.clicked.connect(lambda: self.done(self.BACK))
        self.start_button.clicked.connect(lambda: self.done(self.START_INTERVIEW))
        buttons.addWidget(back)
        buttons.addStretch()
        buttons.addWidget(self.start_button)
        layout.addLayout(buttons)
        self._set_enabled(False)
        self.worker = CodexWorker(
            CODEX_MODEL,
            CODEX_REASONING,
            APP_DIR,
            DEVELOPER_INSTRUCTIONS,
            CODEX_TIMEOUT_SECONDS,
            lambda result, error: self.bridge.call.emit(
                lambda: self._ready(result, error)
            ),
            thread_id=thread_id,
        )

    @Slot(object)
    def _dispatch(self, callback):
        callback()

    def _ready(self, result, error):
        if error:
            self.status.setText(f"Codex connection error: {error}")
            return
        turns = CodexAppServerClient.conversation_turns(result.get("thread", {}))
        for turn in turns:
            for message in turn:
                self._append_message(message["role"], message["text"])
        self.ready = True
        self.status.setText("Enter your preparation notes.")
        self._set_enabled(True)

    def _append_message(self, role, text):
        label = "YOU" if role == "user" else "CODEX"
        if self.history.toPlainText():
            self.history.append("")
        self.history.append(f"{label}\n{text}")
        self.history.moveCursor(QTextCursor.End)

    def _send(self):
        text = self.input.toPlainText().strip()
        if not text or not self.ready or self.busy:
            return
        self.input.clear()
        self._append_message("user", text)
        self.busy = True
        self.streaming = False
        self._set_enabled(False)
        self.status.setText("Codex is writing a response…")

        def delta(value, _elapsed):
            self.bridge.call.emit(lambda: self._stream(value))

        def finished(result, error):
            self.bridge.call.emit(lambda: self._finished(result, error))

        self.worker.submit(text, finished, delta)

    def _stream(self, delta):
        cursor = self.history.textCursor()
        cursor.movePosition(QTextCursor.End)
        if not self.streaming:
            cursor.insertText("\n\nCODEX\n")
            self.streaming = True
        cursor.insertText(delta)
        self.history.setTextCursor(cursor)

    def _finished(self, result, error):
        if error:
            self.history.append(f"\nCodex error: {error}")
        elif not self.streaming:
            self._append_message("assistant", result["text"])
        self.busy = False
        self.status.setText("Enter your preparation notes.")
        self._set_enabled(True)

    def _set_enabled(self, enabled):
        self.input.setEnabled(enabled)
        self.send_button.setEnabled(enabled)
        self.start_button.setEnabled(enabled)

    def done(self, result):
        if self.worker is not None:
            worker = self.worker
            self.worker = None
            worker.stop()
        super().done(result)


class TranscriptWindow(QWidget):
    close_requested = Signal()

    def __init__(self, title, answer=False):
        super().__init__()
        self.allow_close = False
        self.setWindowTitle(title)
        self.setWindowFlags(
            Qt.Tool
            | Qt.WindowStaysOnTopHint
            | Qt.WindowTitleHint
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setFocusPolicy(Qt.NoFocus)
        self.resize(720, 300 if answer else 170)
        layout = QVBoxLayout(self)
        heading = QLabel(title)
        heading.setStyleSheet("font-weight: bold; color: #ffc75c;" if answer else "font-weight: bold; color: #8ec8ff;")
        layout.addWidget(heading)
        self.text = AnswerTextEdit() if answer else QTextEdit()
        self.text.setReadOnly(True)
        self.text.setFocusPolicy(Qt.NoFocus)
        self.text.setFont(QFont("Segoe UI", 18 if answer else 16))
        if answer:
            self.text._layout_position_guide()
            self.text.replace_text_at_top("Waiting for F8…")
        else:
            self.text.setPlainText("Whisper loading…")
        layout.addWidget(self.text)
        self.setStyleSheet("background: #121418; color: white;")

    def set_text(self, text):
        if isinstance(self.text, AnswerTextEdit):
            self.text.replace_text_at_top(text)
        else:
            self.text.setPlainText(text)

    def append_text(self, text):
        if isinstance(self.text, AnswerTextEdit):
            self.text.append_text_without_scrolling(text)
            return
        cursor = QTextCursor(self.text.document())
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(text)

    def force_close(self):
        self.allow_close = True
        self.close()

    def closeEvent(self, event):
        if self.allow_close:
            event.accept()
        else:
            event.ignore()
            self.close_requested.emit()


class InterviewControl(QWidget):
    back = Signal()
    close_app = Signal()

    def __init__(self):
        super().__init__()
        self.allow_close = False
        self.setWindowFlags(Qt.Tool | Qt.WindowStaysOnTopHint)
        row = QHBoxLayout(self)
        back = QPushButton("←")
        close = QPushButton("×")
        back.clicked.connect(self.back.emit)
        close.clicked.connect(self.close_app.emit)
        row.addWidget(back)
        row.addWidget(close)

    def force_close(self):
        self.allow_close = True
        self.close()

    def closeEvent(self, event):
        if self.allow_close:
            event.accept()
        else:
            event.ignore()
            self.close_app.emit()


class InterviewController(QObject):
    finished = Signal(str)

    def __init__(self, thread_id):
        super().__init__()
        self.thread_id = thread_id
        self.running = True
        self.last_f8 = None
        self.question_count = 0
        self.utterance_count = 0
        self.codex_request_count = 0
        self.preview_pending = False
        self.latest_preview_pcm = None
        self.preview_lock = threading.Lock()
        self.remote_utterances = []
        self.pending_questions = []
        self.conversation_context = []
        self.codex_context_cursor = 0
        self.lock = threading.Lock()
        self.session_dir, self.log_path = create_test_session()
        self.bridge = DispatchBridge()
        self.bridge.call.connect(self._dispatch)
        self.bridge.f8.connect(self.on_f8)
        self.interviewer = TranscriptWindow("INTERVIEWER")
        self.answer = TranscriptWindow("ANSWER", answer=True)
        self.control = InterviewControl()
        self.control.back.connect(lambda: self.stop("back"))
        self.control.close_app.connect(lambda: self.stop("quit"))
        self.interviewer.close_requested.connect(lambda: self.stop("quit"))
        self.answer.close_requested.connect(lambda: self.stop("quit"))
        window_state = self._load_window_state()
        self._restore_geometry(self.interviewer, window_state.get("INTERVIEWER"), (300, 60))
        self._restore_geometry(self.answer, window_state.get("ANSWER"), (300, 250))
        self._restore_geometry(self.control, window_state.get("CONTROL"), (300, 550))
        for window in (self.interviewer, self.answer, self.control):
            window.show()

        self.segmenter = PcmSpeechSegmenter(
            self._preview_audio,
            self._final_audio,
            vad_rms=VAD_RMS,
            preview_interval_ms=600,
            preview_window_seconds=8,
        )
        self.preview_worker = PreviewWhisperWorker(
            WHISPER_MODEL,
            LANGUAGE,
            lambda result, error: self.bridge.call.emit(
                lambda: self._preview_ready(result, error)
            ),
            cpu_threads=PREVIEW_WHISPER_CPU_THREADS,
        )
        self.whisper_worker = WhisperWorker(
            WHISPER_MODEL,
            LANGUAGE,
            lambda result, error: self.bridge.call.emit(
                lambda: self._whisper_ready(result, error)
            ),
            cpu_threads=WHISPER_CPU_THREADS,
        )
        self.codex_worker = CodexWorker(
            CODEX_MODEL,
            CODEX_REASONING,
            APP_DIR,
            DEVELOPER_INSTRUCTIONS,
            CODEX_TIMEOUT_SECONDS,
            lambda result, error: self.bridge.call.emit(
                lambda: self._codex_ready(result, error)
            ),
            thread_id=thread_id,
        )
        self.windows_bridge = WindowsBridgeClient(
            self.segmenter.feed,
            self.bridge.f8.emit,
            lambda status: self.bridge.call.emit(
                lambda: self._bridge_status(status)
            ),
            lambda error: self.bridge.call.emit(
                lambda: self._audio_error(error)
            ),
        )
        try:
            self.windows_bridge.start()
        except Exception:
            self.stop("quit")
            raise
        append_log(self.log_path, {
            "event": "app_session_start",
            "app_version": "wsl-windows-hybrid-dev2",
            "remote_source": "windows_bridge_pending",
            "microphone_capture": False,
            "whisper_model": WHISPER_MODEL,
            "whisper_cpu_threads": WHISPER_CPU_THREADS,
            "preview_whisper_cpu_threads": PREVIEW_WHISPER_CPU_THREADS,
            "final_transcription_mode": "f8_question_only",
            "preview_queue_mode": "latest_snapshot_only",
            "language": LANGUAGE,
            "codex_model": CODEX_MODEL,
            "codex_reasoning_effort": CODEX_REASONING,
            "codex_fast_mode": False,
            "codex_transport": "app_server_stdio",
            "codex_session_scope": "persistent_selected_thread",
            "codex_thread_id": thread_id,
            "audio_backend": "windows_wasapi_stdio_bridge",
            "global_f8": "windows_bridge_pending",
            "test_label": TEST_LABEL,
        })

    @staticmethod
    def _load_window_state():
        try:
            return json.loads(WINDOW_STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _restore_geometry(window, state, fallback_position):
        if isinstance(state, list) and len(state) == 4:
            window.setGeometry(*state)
        else:
            window.move(*fallback_position)

    def _save_window_state(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        state = {}
        for role, window in (
            ("INTERVIEWER", self.interviewer),
            ("ANSWER", self.answer),
            ("CONTROL", self.control),
        ):
            geometry = window.geometry()
            state[role] = [geometry.x(), geometry.y(), geometry.width(), geometry.height()]
        WINDOW_STATE_PATH.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @Slot(object)
    def _dispatch(self, callback):
        if self.running:
            callback()

    def _whisper_ready(self, result, error):
        if error:
            self.interviewer.set_text(f"Whisper error: {error}")
            append_log(self.log_path, {"event": "whisper_error", "error": str(error)})
            return
        append_log(self.log_path, {"event": "whisper_ready", **{
            key: round(value, 3) for key, value in result.items()
        }})
        self.interviewer.set_text("Preparing live preview…")
        self.preview_worker.start()

    def _preview_ready(self, result, error):
        if error:
            self.interviewer.set_text(f"Listening… (preview unavailable: {error})")
            append_log(self.log_path, {"event": "preview_whisper_error", "error": str(error)})
        else:
            self.interviewer.set_text("Listening…")
            append_log(self.log_path, {"event": "preview_whisper_ready", **{
                key: round(value, 3) for key, value in result.items()
            }})

    def _codex_ready(self, result, error):
        if error:
            self.answer.set_text(f"Codex startup error: {error}")
            append_log(self.log_path, {"event": "codex_app_server_error", "error": str(error)})
        else:
            append_log(self.log_path, {
                "event": "codex_app_server_ready",
                "thread_id": result["thread_id"],
                "startup_seconds": round(result["startup_seconds"], 3),
            })

    def _preview_audio(self, pcm):
        with self.preview_lock:
            if self.preview_pending:
                self.latest_preview_pcm = pcm
                return
        self._submit_preview(pcm)

    def _submit_preview(self, pcm):

        def finished(text, error, elapsed):
            with self.preview_lock:
                self.preview_pending = False
                latest = self.latest_preview_pcm
                self.latest_preview_pcm = None
            if text and not error:
                append_log(self.log_path, {
                    "event": "preview_transcript",
                    "text": text,
                    "stt_seconds": round(elapsed, 3),
                    "snapshot_seconds": round(len(pcm) / BYTES_PER_SECOND, 3),
                    "newer_snapshot_waiting": latest is not None,
                })
                self.bridge.call.emit(lambda: self.interviewer.set_text(text))
            if latest is not None and self.running:
                self._submit_preview(latest)

        accepted = self.preview_worker.submit(pcm, finished)
        with self.preview_lock:
            self.preview_pending = accepted

    def _final_audio(self, pcm, start, end, vad_details):
        self.utterance_count += 1
        utterance = self.utterance_count
        audio_file = None
        if self.session_dir:
            audio_file = f"interviewer_{utterance:03d}.wav"
            save_wav(self.session_dir / audio_file, pcm)
        state = {
            "utterance": utterance,
            "start": start,
            "end": end,
            "audio_file": audio_file,
            "questions": [],
            "result": None,
            "error": None,
            "pcm": pcm,
            "vad_details": vad_details,
            "submitted": False,
        }
        append_log(self.log_path, {
            "event": "utterance_captured",
            "role": "INTERVIEWER",
            "utterance": utterance,
            "audio_file": audio_file,
            "start_absolute_seconds": round(start / BYTES_PER_SECOND, 3),
            "end_absolute_seconds": round(end / BYTES_PER_SECOND, 3),
            **vad_details,
        })
        with self.lock:
            self.remote_utterances.append(state)
            for marker in list(self.pending_questions):
                if self._question_matches(marker, state):
                    state["questions"].append(marker)
                    self.pending_questions.remove(marker)
            self.remote_utterances[:] = self.remote_utterances[-20:]

        if state["questions"]:
            self._submit_question_utterance(state)

    def _submit_question_utterance(self, state):
        with self.lock:
            if state["submitted"] or not state["questions"]:
                return
            state["submitted"] = True
            pcm = state["pcm"]
            marker = state["questions"][0]
            start = state["start"]
            end = state["end"]
            utterance = state["utterance"]
            audio_file = state["audio_file"]
            vad_details = state["vad_details"]

        def processor(model):
            started_at = time.perf_counter()
            text, _boundary, details = transcribe_question(
                model,
                pcm,
                max(0, marker["trigger"] - start),
                silence_padding_ms=200,
                language=LANGUAGE,
            )
            result = {
                "text": text,
                "elapsed": time.perf_counter() - started_at,
                "details": details,
            }
            append_log(self.log_path, {
                "event": "utterance",
                "role": "INTERVIEWER",
                "utterance": utterance,
                "audio_file": audio_file,
                "start_absolute_seconds": round(start / BYTES_PER_SECOND, 3),
                "end_absolute_seconds": round(end / BYTES_PER_SECOND, 3),
                "text": text,
                "stt_seconds": round(result["elapsed"], 3),
                **vad_details,
                **details,
            })
            return result

        def finished(result, error):
            with self.lock:
                state["result"] = result
                state["error"] = error
                markers = list(state["questions"])
            if result and result["text"]:
                self.conversation_context.append(("INTERVIEWER", result["text"]))
                self.bridge.call.emit(lambda: self.interviewer.set_text(result["text"]))
            for marker in markers:
                self.bridge.call.emit(
                    lambda marker=marker: self._commit_question(marker, state, result, error)
                )

        self.whisper_worker.submit(0, processor, finished)

    @staticmethod
    def _question_matches(marker, state):
        if marker["target_span"] is not None:
            return marker["target_span"] == (state["start"], state["end"])
        return (
            marker["source"] == "active"
            and marker["suggested_start"] == state["start"]
            and marker["trigger"] <= state["end"]
        )

    @Slot()
    def on_f8(self):
        now = time.perf_counter()
        if self.last_f8 is not None and now - self.last_f8 < DEBOUNCE_SECONDS:
            append_log(self.log_path, {"event": "f8_ignored", "reason": "debounce"})
            return
        self.last_f8 = now
        self.question_count += 1
        marker = self.segmenter.capture_question_marker()
        cancel_seconds = self.preview_worker.cancel()
        with self.preview_lock:
            self.preview_pending = False
            self.latest_preview_pcm = None
        marker.update({"question": self.question_count, "committed": False})
        append_log(self.log_path, {
            "event": "f8_trigger",
            "question": self.question_count,
            "trigger_absolute_seconds": round(marker["trigger"] / BYTES_PER_SECOND, 3),
            "utterance_state": marker["source"],
            "preview_cancel_seconds": round(cancel_seconds, 3),
        })
        matched = None
        with self.lock:
            for state in reversed(self.remote_utterances):
                if self._question_matches(marker, state):
                    state["questions"].append(marker)
                    matched = state
                    break
            if matched is None:
                self.pending_questions.append(marker)
        if matched is not None:
            if matched["result"] is not None or matched["error"] is not None:
                self._commit_question(marker, matched, matched["result"], matched["error"])
            else:
                self._submit_question_utterance(matched)

    def _commit_question(self, marker, state, result, error):
        if marker["committed"]:
            return
        marker["committed"] = True
        if error or not result:
            append_log(self.log_path, {
                "event": "question_error",
                "question": marker["question"],
                "error": str(error),
            })
            self._restart_preview()
            return
        append_log(self.log_path, {
            "event": "question",
            "question": marker["question"],
            "source_utterance": state["utterance"],
            "audio_file": state["audio_file"],
            "text": result["text"],
            "stt_seconds": round(result["elapsed"], 3),
            "additional_stt_seconds": 0,
            "transcript_reused": True,
            **result["details"],
        })
        if result["text"]:
            self.interviewer.set_text(result["text"])
            self._request_answer(marker["question"], result["text"])
            self._restart_preview()
        else:
            self._restart_preview()

    def _restart_preview(self):
        if self.running and self.preview_worker.start():
            append_log(self.log_path, {"event": "preview_whisper_restart"})

    def _request_answer(self, question_number, question_text):
        end = len(self.conversation_context)
        context = self.conversation_context[self.codex_context_cursor:end]
        self.codex_context_cursor = end
        if context and context[-1] == ("INTERVIEWER", question_text):
            context = context[:-1]
        context_text = "\n".join(f"{role}: {text}" for role, text in context) or "(none)"
        prompt = (
            "NEW CONVERSATION SINCE THE PREVIOUS REQUEST:\n"
            f"{context_text}\n\nCURRENT INTERVIEWER QUESTION:\n{question_text}\n"
        )
        self.codex_request_count += 1
        request = self.codex_request_count
        stream_started = {"value": False}
        self.answer.set_text("Thinking…")
        append_log(self.log_path, {
            "event": "codex_request",
            "request": request,
            "question": question_number,
            "model": CODEX_MODEL,
            "reasoning_effort": CODEX_REASONING,
            "fast_mode": False,
            "context_items": len(context),
        })

        def delta(value, elapsed):
            def show():
                if not stream_started["value"]:
                    stream_started["value"] = True
                    self.answer.set_text(value)
                    append_log(self.log_path, {
                        "event": "codex_stream_start",
                        "request": request,
                        "question": question_number,
                        "elapsed_seconds": round(elapsed, 3),
                    })
                else:
                    self.answer.append_text(value)
            self.bridge.call.emit(show)

        def finished(result, error):
            def show():
                if error:
                    self.answer.set_text(f"Codex error: {error}")
                    append_log(self.log_path, {
                        "event": "codex_error",
                        "request": request,
                        "question": question_number,
                        "error": str(error),
                    })
                    return
                if not stream_started["value"]:
                    self.answer.set_text(result["text"])
                append_log(self.log_path, {
                    "event": "codex_response",
                    "request": request,
                    "question": question_number,
                    "text": result["text"],
                    "elapsed_seconds": round(result["elapsed"], 3),
                    "first_token_seconds": result["first_token_seconds"],
                    "first_visible_seconds": result["first_visible_seconds"],
                    "stream_delta_count": result["stream_delta_count"],
                    "thread_id": result["thread_id"],
                    "turn_id": result["turn_id"],
                })
            self.bridge.call.emit(show)

        self.codex_worker.submit(prompt, finished, delta)

    def _audio_error(self, error):
        self.interviewer.set_text(f"Audio error: {error}")
        append_log(self.log_path, {"event": "audio_error", "role": "INTERVIEWER", "error": str(error)})

    def _bridge_status(self, status):
        event = status.get("event")
        append_log(self.log_path, {
            "event": "windows_bridge_status",
            "bridge_event": event,
            **{key: value for key, value in status.items() if key != "event"},
        })
        if event == "ready":
            device = status.get("device") or {}
            name = device.get("name", "Windows default output")
            self.interviewer.set_text(f"Windows audio ready: {name}\nWhisper loading…")
        elif event in ("error", "audio_error"):
            self._audio_error(status.get("error", "unknown Windows bridge error"))

    def stop(self, action):
        if not self.running:
            return
        self.running = False
        self._save_window_state()
        self.windows_bridge.stop()
        self.preview_worker.stop()
        self.whisper_worker.stop()
        self.codex_worker.stop()
        append_log(self.log_path, {
            "event": "app_session_end",
            "exit_action": action,
            "questions": self.question_count,
            "interviewer_utterances": self.utterance_count,
            "codex_requests": self.codex_request_count,
        })
        for window in (self.interviewer, self.answer, self.control):
            window.force_close()
        self.finished.emit(action)


def main():
    multiprocessing.freeze_support()
    application = QApplication(sys.argv)
    application.setQuitOnLastWindowClosed(False)
    store = SessionStore(SESSION_STORE_PATH)
    while True:
        chooser = SessionDialog(store)
        if chooser.exec() != QDialog.Accepted:
            return 0
        thread_id = chooser.thread_id
        while True:
            preparation = PreparationDialog(thread_id)
            response = preparation.exec()
            if response == PreparationDialog.BACK:
                break
            if response != PreparationDialog.START_INTERVIEW:
                return 0
            try:
                controller = InterviewController(thread_id)
            except Exception as error:
                QMessageBox.critical(None, "Interview Startup Error", str(error))
                continue
            loop = QEventLoop()
            outcome = {"action": "quit"}

            def finished(action):
                outcome["action"] = action
                loop.quit()

            controller.finished.connect(finished)
            loop.exec()
            if outcome["action"] == "back":
                continue
            return 0


if __name__ == "__main__":
    sys.exit(main())
