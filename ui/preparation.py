"""Interview preparation dialog and its presentation helpers."""

import os
import sys
import threading
from importlib import metadata
from pathlib import Path

from codex_app_server import CodexAppServerClient
from codex.worker import CodexWorker
from context_manager import CONTEXT_STATUS_SYNCED
from interview_thread_backend import InterviewThreadBackend
from session_store import normalize_codex_settings
from ui.session_dialogs import (
    CompactMenuSelector,
    NewContextDialog,
    _new_codex_client,
    stt_status_summary,
)

os.environ.setdefault("GDK_BACKEND", "x11")
try:
    import gi
except ModuleNotFoundError:
    sys.path.append("/usr/lib/python3/dist-packages")
    import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango


BACKGROUND_JOIN_TIMEOUT_SECONDS = 5
NO_INTERVIEW_THREAD_TEXT = "아직 Interview Thread가 없습니다."
NO_INTERVIEW_CONVERSATION_TEXT = "아직 면접 대화가 없습니다."
INTERVIEW_QUESTION_MARKER = "CURRENT INTERVIEWER QUESTION:"
PREPARATION_MESSAGE_MARKER = "PREPARATION MESSAGE:"



STT_PRESENTATION = {
    "en": {
        "language": "English",
        "title": "Moonshine Small Streaming",
        "model": "small-streaming-en",
        "mode": "Streaming ASR",
    },
    "ja": {
        "language": "Japanese",
        "title": "Moonshine Small Streaming",
        "model": "small-streaming-ja",
        "mode": "Streaming ASR",
    },
}
try:
    MOONSHINE_VOICE_VERSION = metadata.version("moonshine-voice")
except metadata.PackageNotFoundError:
    MOONSHINE_VOICE_VERSION = "unknown"
APP_MODE_TITLES = {
    "normal": "Normal Interview",
    "performance": "Performance Test",
    "stt_diagnostic": "STT Diagnostic",
}


FALLBACK_CODEX_MODELS = [
    {
        "model": "gpt-5.6-sol",
        "displayName": "GPT-5.6 Sol",
        "additionalSpeedTiers": ["fast"],
        "defaultReasoningEffort": "low",
        "supportedReasoningEfforts": [
            {"reasoningEffort": effort}
            for effort in ("low", "medium", "high", "xhigh", "max", "ultra")
        ],
    },
    {
        "model": "gpt-5.6-terra",
        "displayName": "GPT-5.6 Terra",
        "additionalSpeedTiers": ["fast"],
        "defaultReasoningEffort": "medium",
        "supportedReasoningEfforts": [
            {"reasoningEffort": effort}
            for effort in ("low", "medium", "high", "xhigh", "max", "ultra")
        ],
    },
    {
        "model": "gpt-5.6-luna",
        "displayName": "GPT-5.6 Luna",
        "additionalSpeedTiers": ["fast"],
        "defaultReasoningEffort": "medium",
        "supportedReasoningEfforts": [
            {"reasoningEffort": effort}
            for effort in ("low", "medium", "high", "xhigh", "max")
        ],
    },
    {
        "model": "gpt-5.5",
        "displayName": "GPT-5.5",
        "additionalSpeedTiers": ["fast"],
        "defaultReasoningEffort": "medium",
        "supportedReasoningEfforts": [
            {"reasoningEffort": effort}
            for effort in ("low", "medium", "high", "xhigh")
        ],
    },
    {
        "model": "gpt-5.2",
        "displayName": "GPT-5.2",
        "additionalSpeedTiers": [],
        "defaultReasoningEffort": "medium",
        "supportedReasoningEfforts": [
            {"reasoningEffort": effort}
            for effort in ("low", "medium", "high", "xhigh")
        ],
    },
]


def model_reasoning_efforts(model):
    efforts = []
    for option in model.get("supportedReasoningEfforts", []):
        effort = (
            option.get("reasoningEffort")
            if isinstance(option, dict)
            else option
        )
        if effort and effort not in efforts:
            efforts.append(effort)
    return efforts


def model_supports_fast(model):
    speed_tiers = {
        str(tier).lower() for tier in model.get("additionalSpeedTiers", [])
    }
    if "fast" in speed_tiers:
        return True
    return any(
        str(tier.get("name", "")).lower() == "fast"
        for tier in model.get("serviceTiers", [])
        if isinstance(tier, dict)
    )


def stt_presentation(language):
    return STT_PRESENTATION.get(language, STT_PRESENTATION["en"])


def stt_model_detail(language):
    presentation = stt_presentation(language)
    return (
        f"model: {presentation['model']}  ·  "
        f"moonshine-voice {MOONSHINE_VOICE_VERSION}  ·  "
        f"{presentation['mode']}"
    )


def runtime_options(environment=None):
    environment = os.environ if environment is None else environment
    return {
        "mode": environment.get("INTERVIEW_APP_MODE", "normal"),
        "codex_enabled": environment.get("INTERVIEW_DISABLE_CODEX", "0")
        == "0",
        "logging_enabled": environment.get("INTERVIEW_TEST_LOG", "0")
        != "0",
        "diagnostics_enabled": environment.get(
            "INTERVIEW_STT_DIAGNOSTICS", "0"
        )
        != "0",
    }


def preparation_runtime_summary(options, language):
    title = APP_MODE_TITLES.get(options["mode"], options["mode"])
    presentation = stt_presentation(language)
    return (
        f"Mode: {title}  ·  "
        f"Codex: {'On' if options['codex_enabled'] else 'Off'}  ·  "
        f"Logging: {'On' if options['logging_enabled'] else 'Off'}  ·  "
        f"STT: {presentation['language']} / {presentation['model']}"
    )


def context_scope_style(scope):
    return "scope-session" if scope == "SESSION" else "scope-global"


def context_status_style(status):
    return {
        "SYNCED": "status-synced",
        "CHANGED": "status-changed",
        "NOT SYNCED": "status-not-synced",
    }.get(status, "status-not-synced")


def context_status_summary(context_rows):
    statuses = {row.get("status") for row in context_rows}
    if "CHANGED" in statuses:
        return "● Context Changed", "status-changed"
    if "NOT SYNCED" in statuses:
        return "● Context Not Synced", "status-not-synced"
    return "● Context Synced", "status-synced"


PREPARATION_CONVERSATION_RATIO = 0.72


def preparation_conversation_position(width):
    return max(0, round(width * PREPARATION_CONVERSATION_RATIO))


def preparation_section(title, description=None):
    frame = Gtk.Frame()
    frame.set_shadow_type(Gtk.ShadowType.NONE)
    frame.get_style_context().add_class("preparation-card")
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    heading = Gtk.Label(label=title)
    heading.set_xalign(0)
    heading.get_style_context().add_class("section-title")
    box.pack_start(heading, False, False, 0)
    if description:
        detail = Gtk.Label(label=description)
        detail.set_xalign(0)
        detail.set_line_wrap(True)
        detail.get_style_context().add_class("section-description")
        box.pack_start(detail, False, False, 0)
    frame.add(box)
    return frame, box


def context_display_name(filename):
    stem = Path(filename).stem
    return " ".join(stem.replace("_", " ").replace("-", " ").split()).title()


def context_display_rows(contexts):
    return [
        {
            "scope": context.scope.upper(),
            "display_name": context_display_name(context.name),
            "filename": context.name,
            "path": context.path,
            "status": context.status,
        }
        for context in contexts
    ]


def load_context_display_rows(context_manager, session_id):
    return context_display_rows(
        context_manager.resolve_effective_context_states(session_id)
    )


def interview_conversation_messages(thread):
    """Extract only interviewer questions and final Codex answers."""
    messages = []
    for turn in thread.get("turns", []):
        if not isinstance(turn, dict):
            continue
        for item in turn.get("items", []):
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "userMessage":
                text = CodexAppServerClient._content_text(
                    item.get("content", [])
                )
                if INTERVIEW_QUESTION_MARKER in text:
                    question = text.rsplit(
                        INTERVIEW_QUESTION_MARKER,
                        1,
                    )[1].strip()
                    if question:
                        messages.append({
                            "role": "interviewer",
                            "text": question,
                        })
                elif text.startswith(PREPARATION_MESSAGE_MARKER):
                    question = text[len(PREPARATION_MESSAGE_MARKER):].strip()
                    if question:
                        messages.append({
                            "role": "candidate",
                            "text": question,
                        })
            elif (
                item_type == "agentMessage"
                and item.get("phase") == "final_answer"
            ):
                text = item.get("text", "").strip()
                if text:
                    messages.append({"role": "codex", "text": text})
    return messages


def can_start_interview(session, context_rows, codex_enabled=True):
    if not session:
        return False
    if not codex_enabled:
        return True
    return bool(
        session.get("interview_thread_id")
        and all(
            row.get("status") == CONTEXT_STATUS_SYNCED
            for row in context_rows
        )
    )


CHAT_RESPONSE_BACK = 10
CHAT_RESPONSE_START_INTERVIEW = 11


class PreparationDialog(Gtk.Dialog):
    """Configure Context and live settings before starting an interview."""

    def __init__(
        self,
        session_id,
        session_store=None,
        session_settings=None,
        context_manager=None,
        runtime=None,
    ):
        super().__init__(title="Interview Preparation")
        self.session_id = session_id
        self.session_store = session_store
        self.context_manager = context_manager
        self.runtime = runtime_options() if runtime is None else dict(runtime)
        self.codex_enabled = self.runtime["codex_enabled"]
        self.codex_settings = normalize_codex_settings(session_settings)
        self.codex_models = list(FALLBACK_CODEX_MODELS)
        self._updating_settings_ui = False
        self.active = False
        self.context_sync_in_progress = False
        self.context_sync_generation = 0
        self.model_catalog_load_generation = 0
        self.conversation_load_generation = 0
        self.preparation_worker = None
        self.preparation_ready = False
        self.preparation_busy = False
        self.preparation_stream_started = False
        self.background_stop = threading.Event()
        self.background_lock = threading.Lock()
        self.background_threads = set()
        self.background_clients = set()
        self.session = (
            self.session_store.get(self.session_id)
            if self.session_store is not None
            else None
        )
        self.set_default_size(940, 760)
        self.set_resizable(True)
        self.set_type_hint(Gdk.WindowTypeHint.NORMAL)
        self.set_skip_taskbar_hint(False)
        self.set_border_width(12)
        self.set_modal(False)
        self.get_style_context().add_class("preparation-window")

        self.back_button = self.add_button("뒤로가기", CHAT_RESPONSE_BACK)
        self.start_button = self.add_button(
            "면접 시작",
            CHAT_RESPONSE_START_INTERVIEW,
        )
        self.start_button.get_style_context().add_class("suggested-action")
        self.start_button.set_sensitive(False)

        content = self.get_content_area()
        content.set_spacing(8)
        session_name = (
            self.session.get("name")
            if self.session is not None
            else "Interview Session"
        ) or "Interview Session"
        status_frame = Gtk.Frame()
        status_frame.set_shadow_type(Gtk.ShadowType.NONE)
        status_frame.get_style_context().add_class(
            "preparation-status-bar"
        )
        status_bar = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=10,
        )
        self.session_summary_label = Gtk.Label(label=session_name)
        self.session_summary_label.set_xalign(0)
        self.session_summary_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.session_summary_label.get_style_context().add_class(
            "status-session"
        )
        status_bar.pack_start(
            self.session_summary_label,
            True,
            True,
            0,
        )
        self.runtime_summary_label = Gtk.Label()
        self.runtime_summary_label.set_xalign(0)
        self.runtime_summary_label.set_ellipsize(Pango.EllipsizeMode.END)
        status_bar.pack_start(
            self.runtime_summary_label,
            True,
            True,
            0,
        )
        self.context_panel_button = Gtk.ToggleButton()
        self.context_panel_button.set_active(True)
        self.context_panel_button.set_relief(Gtk.ReliefStyle.NONE)
        self.context_panel_button.set_tooltip_text(
            "Context panel 접기/펼치기"
        )
        self.context_panel_button.get_style_context().add_class(
            "context-panel-toggle"
        )
        status_bar.pack_start(
            self.context_panel_button,
            False,
            False,
            0,
        )
        self.stt_summary_label = Gtk.Label()
        self.stt_summary_label.get_style_context().add_class("status-stt")
        status_bar.pack_start(self.stt_summary_label, False, False, 0)
        self.settings_button = Gtk.Button(label="⚙ Settings")
        self.settings_button.set_relief(Gtk.ReliefStyle.NONE)
        self.settings_button.get_style_context().add_class("settings-button")
        self.settings_button.connect("clicked", self._show_settings)
        status_bar.pack_end(self.settings_button, False, False, 0)
        status_frame.add(status_bar)
        content.pack_start(status_frame, False, False, 0)

        self.settings_dialog = Gtk.Dialog(
            title="Interview Settings",
            transient_for=self,
            modal=True,
        )
        self.settings_dialog.set_default_size(620, 390)
        self.settings_dialog.set_resizable(True)
        self.settings_dialog.add_button("닫기", Gtk.ResponseType.CLOSE)
        self.settings_dialog.connect("delete-event", self._close_settings)
        settings_content = self.settings_dialog.get_content_area()
        settings_content.set_border_width(12)
        settings_content.set_spacing(10)

        session_heading = Gtk.Label(label="Session")
        session_heading.set_xalign(0)
        session_heading.get_style_context().add_class("settings-heading")
        settings_content.pack_start(session_heading, False, False, 0)
        session_grid = Gtk.Grid(column_spacing=12, row_spacing=6)
        session_grid.attach(Gtk.Label(label="Session Name"), 0, 0, 1, 1)
        session_name_label = Gtk.Label(label=session_name)
        session_name_label.set_xalign(0)
        session_name_label.set_selectable(True)
        session_name_label.set_hexpand(True)
        session_grid.attach(session_name_label, 1, 0, 1, 1)
        session_grid.attach(Gtk.Label(label="Session ID"), 0, 1, 1, 1)
        session_id_label = Gtk.Label(label=session_id)
        session_id_label.set_xalign(0)
        session_id_label.set_selectable(True)
        session_id_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        session_grid.attach(session_id_label, 1, 1, 1, 1)
        settings_content.pack_start(session_grid, False, False, 0)
        settings_content.pack_start(
            Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL),
            False,
            False,
            2,
        )

        codex_heading = Gtk.Label(label="Codex")
        codex_heading.set_xalign(0)
        codex_heading.get_style_context().add_class("settings-heading")
        settings_content.pack_start(codex_heading, False, False, 0)
        codex_grid = Gtk.Grid(column_spacing=8, row_spacing=4)
        model_label = Gtk.Label(label="Model")
        model_label.set_xalign(0)
        codex_grid.attach(model_label, 0, 0, 1, 1)
        self.model_combo = CompactMenuSelector(self._model_changed)
        self.model_combo.set_size_request(180, -1)
        codex_grid.attach(self.model_combo, 0, 1, 1, 1)
        reasoning_label = Gtk.Label(label="Reasoning")
        reasoning_label.set_xalign(0)
        codex_grid.attach(reasoning_label, 1, 0, 1, 1)
        self.reasoning_combo = CompactMenuSelector(self._reasoning_changed)
        self.reasoning_combo.set_size_request(110, -1)
        codex_grid.attach(self.reasoning_combo, 1, 1, 1, 1)
        fast_label = Gtk.Label(label="Fast")
        fast_label.set_xalign(0)
        codex_grid.attach(fast_label, 2, 0, 1, 1)
        self.fast_combo = CompactMenuSelector(self._fast_changed)
        self.fast_combo.set_size_request(72, -1)
        self.fast_combo.append("off", "Off")
        self.fast_combo.append("on", "On")
        codex_grid.attach(self.fast_combo, 2, 1, 1, 1)
        settings_content.pack_start(codex_grid, False, False, 0)
        settings_content.pack_start(
            Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL),
            False,
            False,
            2,
        )

        stt_heading = Gtk.Label(label="Speech Recognition")
        stt_heading.set_xalign(0)
        stt_heading.get_style_context().add_class("settings-heading")
        settings_content.pack_start(stt_heading, False, False, 0)
        stt_grid = Gtk.Grid(column_spacing=16, row_spacing=4)
        language_label = Gtk.Label(label="STT Language")
        language_label.set_xalign(0)
        stt_grid.attach(language_label, 0, 0, 1, 1)
        self.stt_language_combo = CompactMenuSelector(
            self._stt_language_changed
        )
        self.stt_language_combo.set_size_request(160, -1)
        self.stt_language_combo.append("en", "English")
        self.stt_language_combo.append("ja", "Japanese")
        self.stt_language_combo.set_active_id(
            self.codex_settings["stt_language"]
        )
        stt_grid.attach(self.stt_language_combo, 0, 1, 1, 1)
        self.stt_model_info = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=2,
        )
        self.stt_model_title = Gtk.Label()
        self.stt_model_title.set_xalign(0)
        self.stt_model_title.get_style_context().add_class("stt-model-title")
        self.stt_model_detail = Gtk.Label()
        self.stt_model_detail.set_xalign(0)
        self.stt_model_detail.get_style_context().add_class("stt-model-detail")
        self.stt_model_info.pack_start(
            self.stt_model_title,
            False,
            False,
            0,
        )
        self.stt_model_info.pack_start(
            self.stt_model_detail,
            False,
            False,
            0,
        )
        stt_grid.attach(self.stt_model_info, 1, 1, 1, 1)
        settings_content.pack_start(stt_grid, False, False, 0)
        self._update_stt_model_info(self.codex_settings["stt_language"])
        self._set_model_catalog(self.codex_models, persist=False)
        self._set_settings_sensitive(True)

        workspace = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        workspace.set_wide_handle(True)
        workspace.set_hexpand(True)
        workspace.set_vexpand(True)
        workspace.connect("size-allocate", self._allocate_workspace)
        content.pack_start(workspace, True, True, 0)
        self.workspace_paned = workspace

        conversation_frame = Gtk.Frame()
        conversation_frame.set_shadow_type(Gtk.ShadowType.NONE)
        conversation_frame.get_style_context().add_class("preparation-card")
        conversation_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=8,
        )
        conversation_heading_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )
        conversation_heading = Gtk.Label(label="Interview Conversation")
        conversation_heading.set_xalign(0)
        conversation_heading.get_style_context().add_class("section-title")
        conversation_heading_row.pack_start(
            conversation_heading,
            True,
            True,
            0,
        )
        self.conversation_refresh_button = Gtk.Button(
            label="Refresh Conversation"
        )
        self.conversation_refresh_button.connect(
            "clicked",
            self._refresh_conversation,
        )
        conversation_heading_row.pack_end(
            self.conversation_refresh_button,
            False,
            False,
            0,
        )
        conversation_box.pack_start(
            conversation_heading_row,
            False,
            False,
            0,
        )
        self.conversation_view = Gtk.TextView()
        self.conversation_view.set_editable(False)
        self.conversation_view.set_cursor_visible(False)
        self.conversation_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.conversation_view.set_left_margin(14)
        self.conversation_view.set_right_margin(14)
        self.conversation_view.set_top_margin(12)
        self.conversation_view.set_bottom_margin(12)
        self.conversation_view.get_style_context().add_class(
            "conversation-view"
        )
        self.conversation_buffer = self.conversation_view.get_buffer()
        self.conversation_interviewer_tag = self.conversation_buffer.create_tag(
            "conversation-interviewer",
            foreground="#8ec8ff",
            weight=Pango.Weight.BOLD,
        )
        self.conversation_codex_tag = self.conversation_buffer.create_tag(
            "conversation-codex",
            foreground="#ffc75c",
            weight=Pango.Weight.BOLD,
        )
        self.conversation_candidate_tag = self.conversation_buffer.create_tag(
            "conversation-candidate",
            foreground="#9dccff",
            weight=Pango.Weight.BOLD,
        )
        self.conversation_body_tag = self.conversation_buffer.create_tag(
            "conversation-body",
            foreground="#f2f4f7",
        )
        conversation_scroller = Gtk.ScrolledWindow()
        conversation_scroller.set_policy(
            Gtk.PolicyType.AUTOMATIC,
            Gtk.PolicyType.AUTOMATIC,
        )
        conversation_scroller.set_shadow_type(Gtk.ShadowType.IN)
        conversation_scroller.add(self.conversation_view)
        conversation_box.pack_start(conversation_scroller, True, True, 0)
        self.preparation_chat_status = Gtk.Label()
        self.preparation_chat_status.set_xalign(0)
        self.preparation_chat_status.set_line_wrap(True)
        self.preparation_chat_status.get_style_context().add_class(
            "section-description"
        )
        conversation_box.pack_start(
            self.preparation_chat_status,
            False,
            False,
            0,
        )
        preparation_input_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )
        preparation_input_scroller = Gtk.ScrolledWindow()
        preparation_input_scroller.set_policy(
            Gtk.PolicyType.AUTOMATIC,
            Gtk.PolicyType.AUTOMATIC,
        )
        preparation_input_scroller.set_size_request(-1, 76)
        self.preparation_input = Gtk.TextView()
        self.preparation_input.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.preparation_input.set_left_margin(10)
        self.preparation_input.set_right_margin(10)
        self.preparation_input.set_top_margin(8)
        self.preparation_input.set_bottom_margin(8)
        self.preparation_input.set_sensitive(False)
        self.preparation_input.set_tooltip_text(
            "Context를 Sync한 뒤 준비 질문을 입력할 수 있습니다."
        )
        self.preparation_input.connect(
            "key-press-event",
            self._preparation_input_key_pressed,
        )
        preparation_input_scroller.add(self.preparation_input)
        preparation_input_row.pack_start(
            preparation_input_scroller,
            True,
            True,
            0,
        )
        self.preparation_send_button = Gtk.Button(label="질문 보내기")
        self.preparation_send_button.set_sensitive(False)
        self.preparation_send_button.connect(
            "clicked",
            self._send_preparation_message,
        )
        preparation_input_row.pack_end(
            self.preparation_send_button,
            False,
            False,
            0,
        )
        conversation_box.pack_start(preparation_input_row, False, False, 0)
        conversation_frame.add(conversation_box)
        workspace.pack1(conversation_frame, resize=True, shrink=False)

        self.context_frame, context_box = preparation_section(
            "Context",
            "Global Context와 이 세션의 override 및 sync 상태입니다.",
        )
        self.context_list_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
        )
        self.context_list_box.set_vexpand(True)
        context_box.pack_start(self.context_list_box, True, True, 0)
        context_primary_actions = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )
        self.add_context_button = Gtk.Button(label="+ Context")
        self.add_context_button.set_sensitive(self.context_manager is not None)
        self.add_context_button.connect("clicked", self._new_context)
        context_primary_actions.pack_start(
            self.add_context_button,
            True,
            True,
            0,
        )
        self.refresh_context_button = Gtk.Button(label="Refresh")
        self.refresh_context_button.set_sensitive(
            self.context_manager is not None
        )
        self.refresh_context_button.connect("clicked", self._refresh_contexts)
        context_primary_actions.pack_start(
            self.refresh_context_button,
            True,
            True,
            0,
        )
        context_box.pack_start(context_primary_actions, False, False, 2)
        self.sync_context_button = Gtk.Button(label="Sync Context")
        self.sync_context_button.set_sensitive(
            self.codex_enabled
            and self.context_manager is not None
            and self.session_store is not None
        )
        self.sync_context_button.connect("clicked", self._sync_contexts)
        context_box.pack_start(
            self.sync_context_button,
            False,
            True,
            0,
        )
        self.context_rows = []
        self._refresh_contexts()
        workspace.pack2(self.context_frame, resize=True, shrink=False)
        self.context_panel_button.connect(
            "toggled",
            self._toggle_context_panel,
        )
        self._set_conversation_text(NO_INTERVIEW_THREAD_TEXT)

        self.connect("delete-event", self._delete)
        self.show_all()

    def _allocate_workspace(self, paned, allocation):
        position = (
            preparation_conversation_position(allocation.width)
            if self.context_panel_button.get_active()
            else allocation.width
        )
        if paned.get_position() != position:
            paned.set_position(position)

    def _toggle_context_panel(self, button):
        if button.get_active():
            self.context_frame.show_all()
        else:
            self.context_frame.hide()
        self._update_context_summary()
        self.workspace_paned.queue_resize()

    def _show_settings(self, *_args):
        self.settings_dialog.show_all()
        self.settings_dialog.run()
        self.settings_dialog.hide()

    def _close_settings(self, dialog, _event):
        dialog.response(Gtk.ResponseType.CLOSE)
        return True

    def _refresh_contexts(self, *_args):
        self.context_rows = (
            load_context_display_rows(self.context_manager, self.session_id)
            if self.context_manager is not None
            else []
        )
        for child in self.context_list_box.get_children():
            child.destroy()
        context_grid = Gtk.Grid(column_spacing=7, row_spacing=5)
        context_grid.set_hexpand(True)
        for column, title in enumerate(("SCOPE", "NAME", "STATUS", "")):
            header = Gtk.Label(label=title)
            header.set_xalign(0)
            header.get_style_context().add_class("context-header")
            context_grid.attach(header, column, 0, 1, 1)
        for row_number, row in enumerate(self.context_rows):
            grid_row = row_number + 1
            scope_label = Gtk.Label(label=row["scope"])
            scope_label.set_xalign(0.5)
            scope_label.get_style_context().add_class("context-badge")
            scope_label.get_style_context().add_class(
                context_scope_style(row["scope"])
            )
            display_label = Gtk.Label(label=row["display_name"])
            display_label.set_xalign(0)
            display_label.set_hexpand(True)
            display_label.set_ellipsize(Pango.EllipsizeMode.END)
            display_label.set_tooltip_text(row["filename"])
            status_label = Gtk.Label(label=row["status"])
            status_label.set_xalign(0.5)
            status_label.get_style_context().add_class("context-badge")
            status_label.get_style_context().add_class(
                context_status_style(row["status"])
            )
            edit_button = Gtk.Button(label="Edit")
            edit_button.get_style_context().add_class("context-edit")
            edit_button.connect("clicked", self._edit_context, row)
            context_grid.attach(scope_label, 0, grid_row, 1, 1)
            context_grid.attach(display_label, 1, grid_row, 1, 1)
            context_grid.attach(status_label, 2, grid_row, 1, 1)
            context_grid.attach(edit_button, 3, grid_row, 1, 1)
        if not self.context_rows:
            empty_label = Gtk.Label(label="등록된 Context가 없습니다.")
            empty_label.set_xalign(0)
            context_grid.attach(empty_label, 0, 1, 4, 1)
        context_scroller = Gtk.ScrolledWindow()
        context_scroller.set_policy(
            Gtk.PolicyType.NEVER,
            Gtk.PolicyType.AUTOMATIC,
        )
        context_scroller.set_shadow_type(Gtk.ShadowType.NONE)
        context_scroller.add(context_grid)
        self.context_list_box.pack_start(
            context_scroller,
            True,
            True,
            0,
        )
        self.context_list_box.show_all()
        self._update_context_summary()
        self._update_start_button()
        self._update_preparation_chat()

    def _update_context_summary(self):
        if not hasattr(self, "context_panel_button"):
            return
        label, style_class = context_status_summary(self.context_rows)
        if self.context_sync_in_progress:
            label = "◌ Context Syncing..."
            style_class = "status-not-synced"
        arrow = "▾" if self.context_panel_button.get_active() else "▸"
        self.context_panel_button.set_label(f"{label}  {arrow}")
        style = self.context_panel_button.get_style_context()
        for candidate in (
            "status-synced",
            "status-changed",
            "status-not-synced",
        ):
            style.remove_class(candidate)
        style.add_class(style_class)

    def _new_context(self, *_args):
        dialog = NewContextDialog(self)
        while True:
            response = dialog.run()
            if response != Gtk.ResponseType.OK:
                break
            try:
                self.context_manager.create_context(
                    dialog.context_scope(),
                    self.session_id,
                    dialog.context_name(),
                )
            except FileExistsError:
                self._show_context_error(
                    "Context가 이미 존재합니다.",
                    "같은 scope에 동일한 filename의 Context가 있습니다.",
                    parent=dialog,
                )
                continue
            except (OSError, ValueError) as error:
                self._show_context_error(
                    "Context를 만들 수 없습니다.",
                    str(error),
                    parent=dialog,
                )
                continue
            self._refresh_contexts()
            break
        dialog.destroy()

    def _sync_contexts(self, *_args):
        if (
            not getattr(self, "codex_enabled", True)
            or self.context_sync_in_progress
        ):
            return
        session = (
            self.session_store.get(self.session_id)
            if self.session_store is not None
            else None
        )
        if session is None:
            self._show_context_error(
                "Context를 sync할 수 없습니다.",
                "현재 세션 정보를 찾을 수 없습니다.",
            )
            return
        self._ensure_background_state()
        self._stop_preparation_worker()
        self.context_sync_in_progress = True
        self.context_sync_generation += 1
        generation = self.context_sync_generation
        self.sync_context_button.set_sensitive(False)
        self._update_context_summary()
        self._update_start_button()
        self._update_preparation_chat()
        self._start_background_task(
            self._run_context_sync,
            session,
            generation,
        )

    def _run_context_sync(self, session, generation):
        client = None

        def client_factory(settings):
            nonlocal client
            client = self._new_background_client(settings)
            return client

        try:
            backend = InterviewThreadBackend(
                self.session_store,
                self.context_manager,
                client_factory,
            )
            result = backend.create(session)
            error = None
        except Exception as caught_error:
            result = None
            error = caught_error
        finally:
            self._unregister_background_client(client)
        GLib.idle_add(
            self._context_sync_finished,
            generation,
            result,
            error,
        )

    def _context_sync_finished(self, generation, _result, error):
        if generation != self.context_sync_generation:
            return False
        self.context_sync_in_progress = False
        if not self.active:
            return False
        self.sync_context_button.set_sensitive(
            getattr(self, "codex_enabled", True)
        )
        if error is not None:
            self._update_context_summary()
            self._update_start_button()
            self._update_preparation_chat()
            self._show_context_error(
                "Context sync에 실패했습니다.",
                str(error),
            )
            return False
        self.session = self.session_store.get(self.session_id)
        self._refresh_contexts()
        self._refresh_conversation()
        return False

    def _set_conversation_text(self, text):
        self.conversation_buffer.set_text(text)

    def _refresh_conversation(self, *_args):
        if self.session_store is not None:
            self.session = self.session_store.get(self.session_id)
        self.conversation_load_generation += 1
        generation = self.conversation_load_generation
        if not getattr(self, "codex_enabled", True):
            self.conversation_refresh_button.set_sensitive(False)
            self._set_conversation_text("Codex is disabled in this mode.")
            return
        thread_id = (
            self.session.get("interview_thread_id")
            if self.session is not None
            else None
        )
        if not thread_id:
            self.conversation_refresh_button.set_sensitive(True)
            self._set_conversation_text(NO_INTERVIEW_THREAD_TEXT)
            return
        self.conversation_refresh_button.set_sensitive(False)
        self._set_conversation_text("면접 대화를 불러오는 중…")
        self._start_background_task(
            self._run_conversation_load,
            generation,
            thread_id,
            self.settings_snapshot(),
        )

    def _run_conversation_load(self, generation, thread_id, settings):
        client = None
        try:
            client = self._new_background_client(settings)
            client.connect()
            thread = client.read_thread(thread_id, include_turns=True)
            error = None
        except Exception as caught_error:
            thread = None
            error = caught_error
        finally:
            if client is not None:
                try:
                    client.stop()
                finally:
                    self._unregister_background_client(client)
        GLib.idle_add(
            self._conversation_load_finished,
            generation,
            thread_id,
            thread,
            error,
        )

    def _conversation_load_finished(
        self,
        generation,
        thread_id,
        thread,
        error,
    ):
        current_thread_id = (
            self.session.get("interview_thread_id")
            if self.session is not None
            else None
        )
        if (
            not self.active
            or generation != self.conversation_load_generation
            or thread_id != current_thread_id
        ):
            return False
        self.conversation_refresh_button.set_sensitive(True)
        if error is not None:
            self._set_conversation_text(
                f"면접 대화를 불러올 수 없습니다: {error}"
            )
            return False
        messages = interview_conversation_messages(thread or {})
        self.conversation_buffer.set_text("")
        if not messages:
            self._set_conversation_text(NO_INTERVIEW_CONVERSATION_TEXT)
            return False
        for message in messages:
            self._append_conversation_message(
                message["role"],
                message["text"],
            )
        return False

    def _append_conversation_message(self, role, text):
        end = self.conversation_buffer.get_end_iter()
        if self.conversation_buffer.get_char_count():
            self.conversation_buffer.insert(end, "\n\n")
            end = self.conversation_buffer.get_end_iter()
        if role == "interviewer":
            label = "INTERVIEWER\n"
            tag = self.conversation_interviewer_tag
        elif role == "candidate":
            label = "YOU\n"
            tag = self.conversation_candidate_tag
        else:
            label = "CODEX\n"
            tag = self.conversation_codex_tag
        self.conversation_buffer.insert_with_tags(end, label, tag)
        end = self.conversation_buffer.get_end_iter()
        self.conversation_buffer.insert_with_tags(
            end,
            text,
            self.conversation_body_tag,
        )

    def _preparation_chat_thread_id(self):
        if not (
            self.active
            and getattr(self, "codex_enabled", True)
            and not self.context_sync_in_progress
            and can_start_interview(self.session, self.context_rows)
        ):
            return None
        return self.session.get("interview_thread_id")

    def _stop_preparation_worker(self):
        worker = getattr(self, "preparation_worker", None)
        self.preparation_worker = None
        self.preparation_ready = False
        self.preparation_busy = False
        self.preparation_stream_started = False
        if worker is not None:
            worker.stop()

    def _update_preparation_chat(self):
        if not hasattr(self, "preparation_input"):
            return
        thread_id = self._preparation_chat_thread_id()
        worker = self.preparation_worker
        if not thread_id:
            if worker is not None:
                self._stop_preparation_worker()
            self.preparation_input.set_sensitive(False)
            self.preparation_send_button.set_sensitive(False)
            if not getattr(self, "codex_enabled", True):
                status = "Codex is disabled in this mode."
            elif self.context_sync_in_progress:
                status = "Context를 Sync하는 중입니다…"
            else:
                status = "Context를 Sync한 뒤 준비 질문을 입력할 수 있습니다."
            self.preparation_chat_status.set_text(status)
            return
        if worker is not None and worker.thread_id != thread_id:
            self._stop_preparation_worker()
            worker = None
        if worker is None:
            self.preparation_ready = False
            self.preparation_busy = False
            self.preparation_stream_started = False
            self.preparation_input.set_sensitive(False)
            self.preparation_send_button.set_sensitive(False)
            self.preparation_chat_status.set_text(
                "준비 대화에 연결하는 중입니다…"
            )
            settings = self.settings_snapshot()
            self.preparation_worker = CodexWorker(
                self._preparation_chat_ready,
                thread_id=thread_id,
                model=settings["codex_model"],
                effort=settings["codex_reasoning_effort"],
                fast_mode=settings["codex_fast_mode"],
            )
            return
        self._set_preparation_chat_busy(self.preparation_busy)

    def _preparation_chat_ready(self, result, error):
        if not self.active or self.preparation_worker is None:
            return False
        if error is not None:
            self.preparation_ready = False
            self.preparation_input.set_sensitive(False)
            self.preparation_send_button.set_sensitive(False)
            self.preparation_chat_status.set_text(
                f"준비 대화에 연결할 수 없습니다: {error}"
            )
            return False
        expected_thread_id = self._preparation_chat_thread_id()
        if not expected_thread_id or result.get("thread_id") != expected_thread_id:
            self._stop_preparation_worker()
            self._update_preparation_chat()
            return False
        self.preparation_ready = True
        self.preparation_chat_status.set_text(
            "준비 질문을 입력하세요. Enter 전송 · Shift+Enter 줄바꿈"
        )
        self._set_preparation_chat_busy(False)
        self.preparation_input.grab_focus()
        return False

    def _set_preparation_chat_busy(self, busy):
        self.preparation_busy = busy
        enabled = self.preparation_ready and not busy
        self.preparation_input.set_sensitive(enabled)
        self.preparation_send_button.set_sensitive(enabled)

    def _send_preparation_message(self, *_args):
        if (
            not self.preparation_ready
            or self.preparation_busy
            or self.preparation_worker is None
        ):
            return
        buffer = self.preparation_input.get_buffer()
        text = buffer.get_text(
            buffer.get_start_iter(),
            buffer.get_end_iter(),
            True,
        ).strip()
        if not text:
            return
        buffer.set_text("")
        self._append_conversation_message("candidate", text)
        self.preparation_stream_started = False
        self._set_preparation_chat_busy(True)
        self.preparation_chat_status.set_text("Codex가 답변을 작성 중입니다…")

        def streamed(delta, _elapsed):
            if not self.active or not self.preparation_busy:
                return False
            if self.preparation_stream_started:
                end = self.conversation_buffer.get_end_iter()
                self.conversation_buffer.insert_with_tags(
                    end,
                    delta,
                    self.conversation_body_tag,
                )
            else:
                self.preparation_stream_started = True
                self._append_conversation_message("codex", delta)
            return False

        def finished(result, error):
            if not self.active:
                return False
            if error is not None:
                self.preparation_chat_status.set_text(
                    f"준비 질문을 전송할 수 없습니다: {error}"
                )
            elif not self.preparation_stream_started:
                self._append_conversation_message("codex", result["text"])
            self._set_preparation_chat_busy(False)
            if error is None:
                self.preparation_chat_status.set_text(
                    "준비 질문을 입력하세요. Enter 전송 · Shift+Enter 줄바꿈"
                )
                self._refresh_conversation()
            self.preparation_input.grab_focus()
            return False

        self.preparation_worker.submit(
            f"{PREPARATION_MESSAGE_MARKER}\n{text}",
            finished,
            streamed,
            interactive=True,
            on_approval=self._approve_preparation_tool,
        )

    def _preparation_input_key_pressed(self, _widget, event):
        if event.keyval not in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            return False
        if event.state & Gdk.ModifierType.SHIFT_MASK:
            return False
        self._send_preparation_message()
        return True

    def _approve_preparation_tool(self, method, params):
        is_command = method == "item/commandExecution/requestApproval"
        title = "명령 실행을 허용할까요?" if is_command else "파일 변경을 허용할까요?"
        detail = params.get("reason") or "Codex가 작업 승인을 요청했습니다."
        if is_command and params.get("command"):
            command = params["command"]
            if isinstance(command, list):
                command = " ".join(str(part) for part in command)
            detail = f"{detail}\n\n{command}"
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE,
            text=title,
        )
        dialog.format_secondary_text(detail)
        dialog.add_button("거부", Gtk.ResponseType.CANCEL)
        dialog.add_button("허용", Gtk.ResponseType.OK)
        response = dialog.run()
        dialog.destroy()
        return "accept" if response == Gtk.ResponseType.OK else "decline"

    def interview_thread_id(self):
        if not can_start_interview(
            self.session,
            self.context_rows,
            getattr(self, "codex_enabled", True),
        ):
            return None
        return self.session.get("interview_thread_id")

    def _edit_context(self, _button, row):
        try:
            path = Path(row["path"]).resolve(strict=True)
            if not path.is_file():
                raise OSError(f"Context file does not exist: {path}")
            if not Gio.AppInfo.launch_default_for_uri(path.as_uri(), None):
                raise OSError(f"No application can open: {path.name}")
        except (OSError, ValueError, GLib.Error) as error:
            self._show_context_error(
                "Context 파일을 열 수 없습니다.",
                str(error),
            )

    def _show_context_error(self, title, detail, parent=None):
        dialog = Gtk.MessageDialog(
            transient_for=parent or self,
            modal=True,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.CLOSE,
            text=title,
        )
        dialog.format_secondary_text(detail)
        dialog.run()
        dialog.destroy()

    def run_session(self):
        self._ensure_background_state()
        self.background_stop.clear()
        self.session = (
            self.session_store.get(self.session_id)
            if self.session_store is not None
            else None
        )
        self._refresh_contexts()
        self.sync_context_button.set_sensitive(
            getattr(self, "codex_enabled", True)
            and not self.context_sync_in_progress
            and self.context_manager is not None
            and self.session_store is not None
        )
        self.active = True
        self._update_preparation_chat()
        if getattr(self, "codex_enabled", True):
            self._load_model_catalog()
        self._refresh_conversation()
        response = self.run()
        self.active = False
        self.context_sync_generation += 1
        self.model_catalog_load_generation += 1
        self.conversation_load_generation += 1
        self._stop_preparation_worker()
        self._stop_background_tasks()
        self.hide()
        return response

    def _load_model_catalog(self):
        self.model_catalog_load_generation += 1
        generation = self.model_catalog_load_generation
        self._start_background_task(
            self._run_model_catalog_load,
            generation,
            self.settings_snapshot(),
        )

    def _run_model_catalog_load(self, generation, settings):
        client = None
        try:
            client = self._new_background_client(settings)
            client.connect()
            models = client.list_models()
        except Exception:
            models = None
        finally:
            if client is not None:
                try:
                    client.stop()
                finally:
                    self._unregister_background_client(client)
        GLib.idle_add(self._model_catalog_finished, generation, models)

    def _ensure_background_state(self):
        if not hasattr(self, "background_stop"):
            self.background_stop = threading.Event()
            self.background_lock = threading.Lock()
            self.background_threads = set()
            self.background_clients = set()
        if not hasattr(self, "context_sync_generation"):
            self.context_sync_generation = 0

    def _start_background_task(self, target, *args):
        self._ensure_background_state()
        if self.background_stop.is_set():
            return None
        thread_holder = {}

        def run():
            try:
                target(*args)
            finally:
                with self.background_lock:
                    self.background_threads.discard(thread_holder["thread"])

        thread = threading.Thread(target=run, daemon=True)
        thread_holder["thread"] = thread
        with self.background_lock:
            self.background_threads.add(thread)
        thread.start()
        return thread

    def _new_background_client(self, settings):
        self._ensure_background_state()
        client = _new_codex_client(settings)
        with self.background_lock:
            if self.background_stop.is_set():
                client.stop()
                raise RuntimeError("Preparation background work is stopping")
            self.background_clients.add(client)
        return client

    def _unregister_background_client(self, client):
        if client is None:
            return
        self._ensure_background_state()
        with self.background_lock:
            self.background_clients.discard(client)

    def _stop_background_tasks(self):
        self._ensure_background_state()
        self.background_stop.set()
        with self.background_lock:
            clients = list(self.background_clients)
            threads = list(self.background_threads)
        for client in clients:
            try:
                client.stop()
            except Exception:
                pass
        current = threading.current_thread()
        for thread in threads:
            if thread is not current:
                thread.join(timeout=BACKGROUND_JOIN_TIMEOUT_SECONDS)

    def _model_catalog_finished(self, generation, models):
        if (
            not self.active
            or generation != self.model_catalog_load_generation
        ):
            return False
        if models:
            self._set_model_catalog(models, persist=True)
        return False

    def settings_snapshot(self):
        snapshot = dict(self.codex_settings)
        selected_model = next(
            (
                model for model in self.codex_models
                if model.get("model") == snapshot["codex_model"]
            ),
            None,
        )
        snapshot["codex_fast_mode"] = bool(
            snapshot["codex_fast_mode"]
            and selected_model is not None
            and model_supports_fast(selected_model)
        )
        return snapshot

    def _set_settings_sensitive(self, sensitive):
        codex_sensitive = sensitive and getattr(self, "codex_enabled", True)
        self.model_combo.set_sensitive(codex_sensitive)
        self.reasoning_combo.set_sensitive(codex_sensitive)
        self.fast_combo.set_sensitive(codex_sensitive)
        self.stt_language_combo.set_sensitive(sensitive)

    def _set_model_catalog(self, models, persist):
        visible = [model for model in models if not model.get("hidden", False)]
        if not visible:
            visible = list(FALLBACK_CODEX_MODELS)
        self.codex_models = visible
        selected_model = self.codex_settings["codex_model"]
        available = {model.get("model"): model for model in visible}
        if selected_model not in available:
            default = next(
                (model for model in visible if model.get("isDefault")),
                visible[0],
            )
            selected_model = default["model"]
            self.codex_settings["codex_model"] = selected_model

        self._updating_settings_ui = True
        self.model_combo.remove_all()
        for model in visible:
            model_id = model.get("model")
            if model_id:
                self.model_combo.append(
                    model_id,
                    model.get("displayName") or model_id,
                )
        self.model_combo.set_active_id(selected_model)
        self._populate_reasoning(available[selected_model])
        self._sync_fast(available[selected_model])
        self._updating_settings_ui = False
        if persist:
            self._persist_settings()

    def _populate_reasoning(self, model):
        efforts = model_reasoning_efforts(model)
        selected = self.codex_settings["codex_reasoning_effort"]
        if selected not in efforts:
            selected = model.get("defaultReasoningEffort")
            if selected not in efforts:
                selected = efforts[0]
            self.codex_settings["codex_reasoning_effort"] = selected
        self.reasoning_combo.remove_all()
        for effort in efforts:
            self.reasoning_combo.append(effort, effort.capitalize())
        self.reasoning_combo.set_active_id(selected)

    def _sync_fast(self, model):
        supported = model_supports_fast(model)
        self.fast_combo.set_item_sensitive("on", supported)
        if not supported:
            self.codex_settings["codex_fast_mode"] = False
        self.fast_combo.set_active_id(
            "on" if self.codex_settings["codex_fast_mode"] else "off"
        )

    def _model_changed(self, combo):
        if self._updating_settings_ui:
            return
        model_id = combo.get_active_id()
        model = next(
            (item for item in self.codex_models if item.get("model") == model_id),
            None,
        )
        if model is None:
            return
        self._updating_settings_ui = True
        self.codex_settings["codex_model"] = model_id
        self._populate_reasoning(model)
        self._sync_fast(model)
        self._updating_settings_ui = False
        self._persist_settings()

    def _reasoning_changed(self, combo):
        if self._updating_settings_ui:
            return
        effort = combo.get_active_id()
        if not effort:
            return
        self.codex_settings["codex_reasoning_effort"] = effort
        self._persist_settings()

    def _fast_changed(self, combo):
        if self._updating_settings_ui:
            return
        requested = combo.get_active_id() == "on"
        selected_model = next(
            (
                model for model in self.codex_models
                if model.get("model") == self.codex_settings["codex_model"]
            ),
            None,
        )
        enabled = bool(
            requested
            and selected_model is not None
            and model_supports_fast(selected_model)
        )
        self.codex_settings["codex_fast_mode"] = enabled
        if requested and not enabled:
            self._updating_settings_ui = True
            combo.set_active_id("off")
            self._updating_settings_ui = False
        self._persist_settings()

    def _stt_language_changed(self, combo):
        if self._updating_settings_ui:
            return
        language = combo.get_active_id()
        if language not in {"en", "ja"}:
            return
        self.codex_settings["stt_language"] = language
        self._update_stt_model_info(language)
        self._persist_settings()

    def _update_stt_model_info(self, language):
        presentation = stt_presentation(language)
        self.stt_model_title.set_text(presentation["title"])
        self.stt_model_detail.set_text(stt_model_detail(language))
        self.stt_summary_label.set_text(stt_status_summary(language))
        self.stt_summary_label.set_tooltip_text(
            f"{presentation['title']}\n"
            f"model: {presentation['model']}\n"
            f"moonshine-voice {MOONSHINE_VOICE_VERSION}\n"
            f"{presentation['mode']}"
        )
        if hasattr(self, "runtime_summary_label"):
            summary = preparation_runtime_summary(self.runtime, language)
            self.runtime_summary_label.set_text(summary)
            self.runtime_summary_label.set_tooltip_text(summary)

    def _persist_settings(self):
        if self.session_store is not None:
            self.session_store.update_settings(
                self.session_id,
                self.codex_settings,
            )
        worker = getattr(self, "preparation_worker", None)
        if worker is not None:
            worker.set_model_and_effort(
                self.codex_settings["codex_model"],
                self.codex_settings["codex_reasoning_effort"],
            )

    def _update_start_button(self):
        self.start_button.set_sensitive(
            not self.context_sync_in_progress
            and can_start_interview(
                self.session,
                self.context_rows,
                getattr(self, "codex_enabled", True),
            )
        )

    def _delete(self, *_args):
        self.response(Gtk.ResponseType.DELETE_EVENT)
        return True
