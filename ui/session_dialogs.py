"""Session selection, archive, rename, and context dialogs."""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

from codex_app_server import CodexAppServerClient, CodexAppServerError
from codex.worker import (
    CODEX_DEVELOPER_INSTRUCTIONS,
    CODEX_MODEL,
    CODEX_REASONING,
    CODEX_TIMEOUT_SECONDS,
)
from context_manager import ContextManager
from session_store import normalize_codex_settings

os.environ.setdefault("GDK_BACKEND", "x11")
try:
    import gi
except ModuleNotFoundError:
    sys.path.append("/usr/lib/python3/dist-packages")
    import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gtk, Pango


APP_DIR = Path(__file__).resolve().parents[1]



SESSION_RESPONSE_NEW = 1
SESSION_RESPONSE_ARCHIVE = 2
SESSION_RESPONSE_RENAME = 3
SESSION_RESPONSE_BACK = 4
SESSION_RESPONSE_ARCHIVE_ALL = 5


def session_list_row(session):
    settings = normalize_codex_settings(session.get("settings"))
    last_used = (
        session.get("last_used_at") or session.get("created_at") or ""
    )
    if "T" in last_used:
        last_used = last_used.replace("T", " ")[:16]
    return (
        session.get("name") or "Unnamed Session",
        stt_status_summary(settings["stt_language"]),
        last_used,
        session["session_id"],
    )


def stt_status_summary(language):
    return "JA · Base" if language == "ja" else "EN · Streaming"


def initial_session_settings(environment=None):
    """Read optional benchmark-only settings for a newly created session.

    Normal sessions keep the existing defaults.  Benchmark automation supplies
    this value so each new session is configured before Context Sync creates
    its fresh Codex thread.
    """
    environment = os.environ if environment is None else environment
    raw = environment.get("INTERVIEW_BENCHMARK_INITIAL_SETTINGS")
    if not raw:
        return normalize_codex_settings()
    try:
        settings = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise ValueError(
            "INTERVIEW_BENCHMARK_INITIAL_SETTINGS must be JSON object"
        ) from error
    if not isinstance(settings, dict):
        raise ValueError(
            "INTERVIEW_BENCHMARK_INITIAL_SETTINGS must be JSON object"
        )
    return normalize_codex_settings(settings)


class RenameSessionDialog(Gtk.Dialog):
    """Rename only the user-facing label of an app session."""

    def __init__(self, session):
        super().__init__(title="세션 이름 변경", modal=True)
        self.set_default_size(420, -1)
        self.set_border_width(12)
        self.add_button("취소", Gtk.ResponseType.CANCEL)
        self.rename_button = self.add_button(
            "이름 변경",
            Gtk.ResponseType.OK,
        )
        self.rename_button.get_style_context().add_class("suggested-action")
        self.set_default_response(Gtk.ResponseType.OK)

        content = self.get_content_area()
        content.set_spacing(8)
        label = Gtk.Label(label="Session Name")
        label.set_xalign(0)
        content.pack_start(label, False, False, 0)
        self.name_entry = Gtk.Entry()
        self.name_entry.set_text(session.get("name") or "")
        self.name_entry.set_activates_default(True)
        self.name_entry.connect("changed", self._name_changed)
        content.pack_start(self.name_entry, False, False, 0)
        self._name_changed(self.name_entry)
        self.show_all()
        self.name_entry.grab_focus()
        self.name_entry.select_region(0, -1)

    def session_name(self):
        return self.name_entry.get_text().strip()

    def _name_changed(self, entry):
        self.rename_button.set_sensitive(bool(entry.get_text().strip()))


class SessionChooserDialog(Gtk.Dialog):
    """Keyboard-friendly chooser for Interview Assistant-owned sessions."""

    def __init__(self, sessions, preferred_session_id=None):
        super().__init__(title="Interview Assistant Sessions")
        self.set_default_size(820, 520)
        self.set_resizable(True)
        self.set_type_hint(Gdk.WindowTypeHint.NORMAL)
        self.set_skip_taskbar_hint(False)
        self.set_border_width(12)
        self.set_modal(True)
        self.get_style_context().add_class("session-window")

        self.new_button = self.add_button("+ 새 세션", SESSION_RESPONSE_NEW)
        self.rename_button = self.add_button(
            "이름 변경",
            SESSION_RESPONSE_RENAME,
        )
        self.rename_button.set_sensitive(False)
        self.archive_button = self.add_button(
            "선택 삭제",
            SESSION_RESPONSE_ARCHIVE,
        )
        self.archive_button.set_sensitive(False)
        self.archive_button.get_style_context().add_class(
            "destructive-action"
        )
        self.archive_all_button = self.add_button(
            "전체 삭제",
            SESSION_RESPONSE_ARCHIVE_ALL,
        )
        self.archive_all_button.set_sensitive(bool(sessions))
        self.archive_all_button.get_style_context().add_class(
            "destructive-action"
        )
        self.get_action_area().set_child_secondary(
            self.archive_button,
            True,
        )
        self.get_action_area().set_child_secondary(
            self.archive_all_button,
            True,
        )
        self.add_button("뒤로가기", SESSION_RESPONSE_BACK)
        self.add_button("취소", Gtk.ResponseType.CANCEL)
        self.open_button = self.add_button("열기", Gtk.ResponseType.OK)
        self.open_button.set_sensitive(False)
        self.open_button.get_style_context().add_class("suggested-action")

        content = self.get_content_area()
        content.set_spacing(10)
        heading = Gtk.Label()
        heading.set_markup("<b>면접 세션 선택</b>")
        heading.set_xalign(0)
        content.pack_start(heading, False, False, 0)

        help_text = Gtk.Label(
            label=(
                "최근 사용한 면접 세션부터 표시됩니다.\n"
                "세션 하나를 선택해 열거나 이름을 변경할 수 있습니다.\n"
                "Ctrl/Shift로 여러 세션을 선택해 삭제할 수 있습니다."
            )
        )
        help_text.set_xalign(0)
        content.pack_start(help_text, False, False, 0)

        self.sessions_by_session_id = {
            session["session_id"]: session for session in sessions
        }
        self.model = Gtk.ListStore(str, str, str, str)
        preferred_path = None
        for index, session in enumerate(sessions):
            self.model.append(session_list_row(session))
            if session["session_id"] == preferred_session_id:
                preferred_path = Gtk.TreePath.new_from_indices([index])

        self.tree = Gtk.TreeView(model=self.model)
        self.tree.set_headers_visible(True)
        self.tree.set_activate_on_single_click(False)
        name_renderer = Gtk.CellRendererText()
        name_renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
        name_renderer.set_property("weight", Pango.Weight.BOLD)
        name_column = Gtk.TreeViewColumn("Name", name_renderer, text=0)
        name_column.set_expand(True)
        name_column.set_resizable(True)
        self.tree.append_column(name_column)
        stt_renderer = Gtk.CellRendererText()
        stt_renderer.set_property("foreground", "#9dccff")
        stt_renderer.set_property("weight", Pango.Weight.BOLD)
        stt_column = Gtk.TreeViewColumn("STT", stt_renderer, text=1)
        stt_column.set_sizing(Gtk.TreeViewColumnSizing.AUTOSIZE)
        stt_column.set_cell_data_func(stt_renderer, self._render_stt_cell)
        self.tree.append_column(stt_column)
        last_used_renderer = Gtk.CellRendererText()
        last_used_renderer.set_property("xalign", 1.0)
        last_used_column = Gtk.TreeViewColumn(
            "Last Used",
            last_used_renderer,
            text=2,
        )
        last_used_column.set_sizing(Gtk.TreeViewColumnSizing.AUTOSIZE)
        self.tree.append_column(last_used_column)
        self.tree.connect("row-activated", self._row_activated)
        self.tree.connect("key-press-event", self._key_pressed)
        selection = self.tree.get_selection()
        selection.set_mode(Gtk.SelectionMode.MULTIPLE)
        selection.connect("changed", self._selection_changed)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.add(self.tree)
        content.pack_start(scroller, True, True, 0)

        self.empty_label = Gtk.Label(
            label="저장된 면접 세션이 없습니다. ‘새 세션’을 눌러 만드세요."
        )
        self.empty_label.set_xalign(0)
        content.pack_start(self.empty_label, False, False, 0)
        self.empty_label.set_visible(len(self.model) == 0)

        if len(self.model):
            selection.select_path(preferred_path or Gtk.TreePath.new_first())
        else:
            self._selection_changed(selection)

        self.show_all()
        self.empty_label.set_visible(len(self.model) == 0)
        self.tree.grab_focus()

    def selected_session(self):
        sessions = self.selected_sessions()
        return sessions[0] if len(sessions) == 1 else None

    def selected_sessions(self):
        model, paths = self.tree.get_selection().get_selected_rows()
        return [
            self.sessions_by_session_id[model[path][3]]
            for path in paths
        ]

    def all_sessions(self):
        return list(self.sessions_by_session_id.values())

    def _row_activated(self, _tree, path, *_args):
        selection = self.tree.get_selection()
        selection.unselect_all()
        selection.select_path(path)
        if self.selected_session() is None:
            return None
        self.response(Gtk.ResponseType.OK)

    def _key_pressed(self, _widget, event):
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if self.selected_session() is not None:
                self.response(Gtk.ResponseType.OK)
                return True
        return False

    def _selection_changed(self, selection):
        _model, paths = selection.get_selected_rows()
        selection_count = len(paths)
        self.open_button.set_sensitive(selection_count == 1)
        self.rename_button.set_sensitive(selection_count == 1)
        self.archive_button.set_sensitive(selection_count > 0)
        self.archive_all_button.set_sensitive(len(self.model) > 0)

    def _render_stt_cell(self, _column, cell, model, tree_iter, _data):
        color = (
            "#d6b8ff"
            if model[tree_iter][1].startswith("JA")
            else "#9dccff"
        )
        cell.set_property("foreground", color)


def _new_codex_client(settings=None):
    settings = normalize_codex_settings(settings or {
        "codex_model": CODEX_MODEL,
        "codex_reasoning_effort": CODEX_REASONING,
    })
    return CodexAppServerClient(
        model=settings["codex_model"],
        effort=settings["codex_reasoning_effort"],
        fast_mode=settings["codex_fast_mode"],
        cwd=APP_DIR,
        developer_instructions=CODEX_DEVELOPER_INSTRUCTIONS,
        timeout_seconds=CODEX_TIMEOUT_SECONDS,
    )


def archive_persisted_codex_session(thread_id):
    client = _new_codex_client()
    try:
        client.connect()
        client.archive_thread(thread_id)
    finally:
        client.stop()


def _show_session_error(error):
    dialog = Gtk.MessageDialog(
        message_type=Gtk.MessageType.ERROR,
        buttons=Gtk.ButtonsType.CLOSE,
        text="세션 작업을 완료하지 못했습니다.",
    )
    dialog.format_secondary_text(str(error))
    dialog.run()
    dialog.destroy()


def _confirm_archive(sessions):
    sessions = list(sessions)
    if len(sessions) == 1:
        text = "선택한 세션을 삭제할까요?"
        detail = (
            f"{sessions[0]['name']}\n\n"
            "이 세션은 활성 목록에서 제거됩니다."
        )
    else:
        text = f"{len(sessions)}개의 세션을 삭제할까요?"
        names = "\n".join(
            session.get("name") or "Unnamed Session"
            for session in sessions[:5]
        )
        remaining = len(sessions) - 5
        if remaining > 0:
            names += f"\n외 {remaining}개"
        detail = f"{names}\n\n선택한 세션은 활성 목록에서 제거됩니다."
    dialog = Gtk.MessageDialog(
        message_type=Gtk.MessageType.QUESTION,
        buttons=Gtk.ButtonsType.NONE,
        text=text,
    )
    dialog.format_secondary_text(detail)
    dialog.add_button("취소", Gtk.ResponseType.CANCEL)
    delete_button = dialog.add_button("삭제", Gtk.ResponseType.OK)
    delete_button.get_style_context().add_class("destructive-action")
    response = dialog.run()
    dialog.destroy()
    return response == Gtk.ResponseType.OK


def _archive_session(store, session, codex_enabled):
    interview_thread_id = session.get("interview_thread_id")
    try:
        if codex_enabled and interview_thread_id:
            archive_persisted_codex_session(interview_thread_id)
    except CodexAppServerError as error:
        if "no rollout found" not in str(error).lower():
            raise
    store.mark_archived(session["session_id"])


def _archive_sessions(store, sessions, codex_enabled):
    failures = []
    for session in sessions:
        try:
            _archive_session(store, session, codex_enabled)
        except Exception as error:
            failures.append((session, error))
    return failures


def choose_interview_session(store, context_manager, codex_enabled=True):
    preferred_session_id = None
    while True:
        dialog = SessionChooserDialog(
            store.active(),
            preferred_session_id=preferred_session_id,
        )
        response = dialog.run()
        selected = dialog.selected_session()
        selected_sessions = dialog.selected_sessions()
        all_sessions = dialog.all_sessions()
        dialog.destroy()

        if response == SESSION_RESPONSE_BACK:
            return SESSION_RESPONSE_BACK

        if response == SESSION_RESPONSE_NEW:
            try:
                created = datetime.now().astimezone()
                session = store.create(
                    created.strftime("%Y-%m-%d %H:%M"),
                    created.isoformat(timespec="seconds"),
                    initial_session_settings(),
                )
                session_id = session["session_id"]
                context_manager.ensure_session(session_id)
                preferred_session_id = session_id
            except Exception as error:
                _show_session_error(error)
            continue

        if response == SESSION_RESPONSE_RENAME and selected is not None:
            rename_dialog = RenameSessionDialog(selected)
            rename_response = rename_dialog.run()
            new_name = rename_dialog.session_name()
            rename_dialog.destroy()
            if rename_response == Gtk.ResponseType.OK:
                try:
                    store.update_name(selected["session_id"], new_name)
                    preferred_session_id = selected["session_id"]
                except (OSError, ValueError) as error:
                    _show_session_error(error)
            continue

        sessions_to_archive = (
            selected_sessions
            if response == SESSION_RESPONSE_ARCHIVE else all_sessions
        )
        if (
            response in {
                SESSION_RESPONSE_ARCHIVE,
                SESSION_RESPONSE_ARCHIVE_ALL,
            }
            and sessions_to_archive
        ):
            if _confirm_archive(sessions_to_archive):
                failures = _archive_sessions(
                    store,
                    sessions_to_archive,
                    codex_enabled,
                )
                preferred_session_id = None
                if failures:
                    failure_lines = "\n".join(
                        f"{session.get('name')}: {error}"
                        for session, error in failures
                    )
                    _show_session_error(
                        RuntimeError(
                            "일부 세션을 삭제하지 못했습니다:\n"
                            f"{failure_lines}"
                        )
                    )
            continue

        if response == Gtk.ResponseType.OK and selected is not None:
            try:
                context_manager.ensure_session(selected["session_id"])
            except (OSError, ValueError) as error:
                _show_session_error(error)
                continue
            store.mark_used(selected["session_id"])
            selected["last_used_at"] = datetime.now().astimezone().isoformat(
                timespec="seconds"
            )
            return selected

        return None


class CompactMenuSelector(Gtk.MenuButton):
    """Small GTK3 menu-backed selector without ComboBox popup focus races."""

    def __init__(self, on_changed):
        super().__init__()
        self._on_changed = on_changed
        self._active_id = None
        self._labels = {}
        self._items = {}
        self._menu = Gtk.Menu()
        self.set_popup(self._menu)
        self.set_direction(Gtk.ArrowType.DOWN)

    def remove_all(self):
        for child in self._menu.get_children():
            child.destroy()
        self._labels.clear()
        self._items.clear()
        self._active_id = None
        self.set_label("")

    def append(self, item_id, label, sensitive=True):
        self._labels[item_id] = label
        item = Gtk.MenuItem(label=label)
        item.set_sensitive(sensitive)
        item.connect("activate", self._activate, item_id)
        self._menu.append(item)
        self._items[item_id] = item
        item.show()

    def set_item_sensitive(self, item_id, sensitive):
        item = self._items.get(item_id)
        if item is not None:
            item.set_sensitive(sensitive)

    def set_active_id(self, item_id):
        if item_id not in self._labels:
            return False
        self._active_id = item_id
        self.set_label(self._labels[item_id])
        return True

    def get_active_id(self):
        return self._active_id

    def _activate(self, _item, item_id):
        if item_id == self._active_id:
            return
        self.set_active_id(item_id)
        self._on_changed(self)


class NewContextDialog(Gtk.Dialog):
    """Collect a free-form Context name and destination scope."""

    def __init__(self, parent):
        super().__init__(
            title="New Context",
            transient_for=parent,
            modal=True,
        )
        self.set_default_size(420, -1)
        self.set_border_width(12)
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self.create_button = self.add_button("Create", Gtk.ResponseType.OK)
        self.create_button.get_style_context().add_class("suggested-action")
        self.create_button.set_sensitive(False)
        self.set_default_response(Gtk.ResponseType.OK)

        content = self.get_content_area()
        content.set_spacing(8)
        name_label = Gtk.Label(label="Context Name")
        name_label.set_xalign(0)
        content.pack_start(name_label, False, False, 0)
        self.name_entry = Gtk.Entry()
        self.name_entry.set_activates_default(True)
        self.name_entry.connect("changed", self._name_changed)
        content.pack_start(self.name_entry, False, False, 0)

        scope_label = Gtk.Label(label="Scope")
        scope_label.set_xalign(0)
        content.pack_start(scope_label, False, False, 4)
        self.session_scope = Gtk.RadioButton.new_with_label_from_widget(
            None,
            "Session",
        )
        self.global_scope = Gtk.RadioButton.new_with_label_from_widget(
            self.session_scope,
            "Global",
        )
        self.session_scope.set_active(True)
        content.pack_start(self.session_scope, False, False, 0)
        content.pack_start(self.global_scope, False, False, 0)

        file_label = Gtk.Label(label="File")
        file_label.set_xalign(0)
        content.pack_start(file_label, False, False, 4)
        self.filename_label = Gtk.Label(label="—")
        self.filename_label.set_xalign(0)
        self.filename_label.set_selectable(True)
        content.pack_start(self.filename_label, False, False, 0)
        self.show_all()
        self.name_entry.grab_focus()

    def context_name(self):
        return self.name_entry.get_text()

    def context_scope(self):
        return "session" if self.session_scope.get_active() else "global"

    def _name_changed(self, entry):
        try:
            filename = ContextManager.context_filename(entry.get_text())
        except ValueError:
            filename = "—"
        self.filename_label.set_text(filename)
        self.create_button.set_sensitive(filename != "—")
