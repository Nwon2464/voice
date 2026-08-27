"""Live interview control and transcript presentation windows."""

import os
import shutil
import sys
import time

os.environ.setdefault("GDK_BACKEND", "x11")
try:
    import gi
except ModuleNotFoundError:
    sys.path.append("/usr/lib/python3/dist-packages")
    import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango


ANSWER_SCROLL_DEBOUNCE_MS = 450
ANSWER_SMOOTH_SCROLL_THRESHOLD = 1.5
ANSWER_CONTENT_SCROLL_PIXELS = 60
ANSWER_POSITION_GUIDE_HEIGHT = 96
TEXT_WIDTH_CHARS = shutil.get_terminal_size(fallback=(100, 24)).columns



class InterviewControlWindow(Gtk.Window):
    """One draggable control surface for interview navigation and exit."""

    def __init__(self, position, on_back, on_close, on_toggle_visibility):
        super().__init__(title="INTERVIEW CONTROLS")
        self.on_close = on_close
        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_accept_focus(False)
        self.set_focus_on_map(False)
        self.set_skip_taskbar_hint(False)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.stick()
        self.set_size_request(132, 44)
        self.move(*position)
        self.connect("delete-event", self._delete)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        row.set_border_width(4)

        drag_handle = Gtk.EventBox()
        drag_handle.set_visible_window(False)
        drag_handle.set_tooltip_text("드래그해서 제어창 이동")
        drag_handle.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        drag_handle.connect("button-press-event", self._drag)
        drag_label = Gtk.Label(label="⠿")
        drag_label.get_style_context().add_class("control-drag")
        drag_handle.add(drag_label)
        row.pack_start(drag_handle, True, True, 0)

        self.visibility_button = Gtk.Button()
        self.visibility_button.set_can_focus(False)
        self.visibility_button.get_style_context().add_class("control-button")
        self.visibility_button.get_style_context().add_class(
            "visibility-button"
        )
        self.visibility_button.connect(
            "clicked",
            lambda _button: on_toggle_visibility(),
        )
        row.pack_start(self.visibility_button, False, False, 0)
        self.set_live_windows_hidden(False)

        back_button = Gtk.Button(label="←")
        back_button.set_tooltip_text("면접 준비 화면으로 돌아가기")
        back_button.get_style_context().add_class("control-button")
        back_button.connect("clicked", lambda _button: on_back())
        row.pack_start(back_button, False, False, 0)

        close_button = Gtk.Button(label="×")
        close_button.set_tooltip_text("앱 종료")
        close_button.get_style_context().add_class("close-button")
        close_button.connect("clicked", lambda _button: on_close())
        row.pack_start(close_button, False, False, 0)

        self.add(row)
        self.get_style_context().add_class("control")

    def set_live_windows_hidden(self, hidden):
        icon_name = (
            "view-reveal-symbolic" if hidden else "view-conceal-symbolic"
        )
        self.visibility_button.set_image(
            Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.BUTTON)
        )
        self.visibility_button.set_tooltip_text(
            "Restore interview windows" if hidden else "Hide interview windows"
        )

    def _drag(self, _widget, event):
        if event.button == 1:
            self.begin_move_drag(
                event.button,
                int(event.x_root),
                int(event.y_root),
                event.time,
            )
            return True
        return False

    def _delete(self, *_args):
        self.on_close()
        return True


class TranscriptWindow(Gtk.Window):
    def __init__(
        self,
        role,
        title,
        width,
        height,
        position,
        on_close,
        focus_mode=False,
        on_back=None,
        show_close=True,
    ):
        super().__init__(title=title)
        self.role = role
        self.on_close = on_close
        self.focus_mode = focus_mode
        self.on_back = on_back
        self.show_close = show_close
        self.last_focus_scroll_at = None
        self.smooth_scroll_delta = 0.0
        self.boundary_status = None
        self.response_status = None
        self.answer_history = []
        self.active_answer = ""
        self.focus_placeholder = ""
        self.latest_answer_mark = None
        self._pending_answer_ui_diagnostics = None
        self._answer_ui_diagnostics_by_mark = {}
        self.set_default_size(width, height)
        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_accept_focus(False)
        self.set_focus_on_map(False)
        self.set_skip_taskbar_hint(False)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.stick()
        # Do not impose an application-level minimum size.  The window
        # manager and GTK's content requirements remain the only limits, so
        # the resize handles can adjust the live windows freely.
        self.move(*position)
        self.connect("delete-event", self._delete)
        self.connect("button-press-event", self._drag)
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_border_width(2 if self.focus_mode else 12)
        box.set_hexpand(True)
        box.set_vexpand(True)
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        heading = Gtk.Label(label=title)
        heading.set_xalign(0)
        heading.get_style_context().add_class("heading")
        close_button = None
        if self.show_close:
            close_button = Gtk.Button(label="×")
            close_button.set_relief(Gtk.ReliefStyle.NONE)
            close_button.set_can_focus(False)
            close_button.set_tooltip_text("Close")
            close_button.get_style_context().add_class("close-button")
            close_button.connect("clicked", lambda _button: self.on_close())
        if not self.focus_mode:
            header.pack_start(heading, True, True, 0)
            if close_button is not None:
                header.pack_end(close_button, False, False, 0)

        if self.focus_mode:
            self.text = Gtk.TextView()
            self.text.set_editable(False)
            self.text.set_cursor_visible(False)
            self.text.set_can_focus(False)
            self.text.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
            self.text.set_left_margin(0)
            self.text.set_right_margin(28)
            self.text.set_top_margin(0)
            self.text.set_bottom_margin(ANSWER_POSITION_GUIDE_HEIGHT)
            self.text.get_buffer().set_text("")
            self.text.get_style_context().add_class("focus-transcript")
        else:
            self.text = Gtk.Label(label="Moonshine loading…")
            self.text.set_xalign(0)
            self.text.set_yalign(0)
            self.text.set_line_wrap(True)
            self.text.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
            self.text.set_max_width_chars(TEXT_WIDTH_CHARS)
            self.text.set_selectable(False)
            self.text.get_style_context().add_class("transcript")

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_shadow_type(Gtk.ShadowType.NONE)
        scroller.add(self.text)

        if not self.focus_mode:
            box.pack_start(header, False, False, 0)
        if self.focus_mode:
            self.focus_scroller = scroller
            answer_overlay = Gtk.Overlay()
            answer_overlay.add(scroller)

            self.position_guide = Gtk.Frame()
            self.position_guide.set_shadow_type(Gtk.ShadowType.NONE)
            self.position_guide.set_size_request(-1, ANSWER_POSITION_GUIDE_HEIGHT)
            self.position_guide.set_halign(Gtk.Align.FILL)
            self.position_guide.set_valign(Gtk.Align.START)
            self.position_guide.get_style_context().add_class("position-guide")
            answer_overlay.add_overlay(self.position_guide)
            answer_overlay.set_overlay_pass_through(self.position_guide, True)

            if close_button is not None:
                close_button.set_halign(Gtk.Align.END)
                close_button.set_valign(Gtk.Align.START)
                close_button.set_margin_top(2)
                close_button.set_margin_end(2)
                answer_overlay.add_overlay(close_button)
            if self.on_back is not None:
                back_button = Gtk.Button(label="←")
                back_button.set_tooltip_text("면접 준비 화면으로 돌아가기")
                back_button.set_halign(Gtk.Align.START)
                back_button.set_valign(Gtk.Align.START)
                back_button.set_margin_top(2)
                back_button.set_margin_start(2)
                back_button.connect(
                    "clicked",
                    lambda _button: self.on_back(),
                )
                answer_overlay.add_overlay(back_button)
            box.pack_start(answer_overlay, True, True, 0)

            if self.role == "ANSWER":
                response_status_row = Gtk.Box(
                    orientation=Gtk.Orientation.HORIZONTAL,
                    spacing=8,
                )
                self.response_status = Gtk.Label(label="")
                self.response_status.set_xalign(0)
                self.response_status.set_single_line_mode(True)
                self.response_status.set_ellipsize(Pango.EllipsizeMode.END)
                self.response_status.set_width_chars(1)
                self.response_status.get_style_context().add_class(
                    "response-status"
                )
                response_status_row.pack_start(
                    self.response_status,
                    True,
                    True,
                    0,
                )
                shortcut_reminder = Gtk.Label(
                    label="F7 CHECKPOINT · F8 NEW · F9 CONTINUE"
                )
                shortcut_reminder.set_xalign(1)
                shortcut_reminder.set_single_line_mode(True)
                shortcut_reminder.set_ellipsize(Pango.EllipsizeMode.END)
                shortcut_reminder.set_width_chars(1)
                shortcut_reminder.get_style_context().add_class(
                    "shortcut-reminder"
                )
                response_status_row.pack_end(
                    shortcut_reminder,
                    False,
                    False,
                    0,
                )
                box.pack_end(response_status_row, False, False, 0)

            for widget in (self, answer_overlay, scroller, self.text):
                widget.add_events(Gdk.EventMask.SCROLL_MASK)
                widget.connect("scroll-event", self._focus_scroll)
            scroller.connect("size-allocate", self._answer_view_resized)
        else:
            box.pack_start(scroller, True, True, 0)
            if self.role == "INTERVIEWER":
                self.boundary_status = Gtk.Label(label="")
                self.boundary_status.set_xalign(0)
                self.boundary_status.set_single_line_mode(True)
                self.boundary_status.get_style_context().add_class(
                    "boundary-status"
                )
                box.pack_end(self.boundary_status, False, False, 0)

        resize_right = self._resize_handle(
            Gdk.WindowEdge.EAST,
            "Drag horizontally to change width",
            8,
            -1,
            "resize-horizontal",
        )
        resize_bottom = self._resize_handle(
            Gdk.WindowEdge.SOUTH,
            "Drag vertically to change height",
            -1,
            8,
            "resize-vertical",
        )
        resize_corner = self._resize_handle(
            Gdk.WindowEdge.SOUTH_EAST,
            "Drag to change width and height",
            14,
            14,
            "resize-corner",
        )
        resize_corner.add(Gtk.Label(label="↘"))
        resize_right.set_vexpand(True)
        resize_bottom.set_hexpand(True)

        grid = Gtk.Grid()
        grid.set_hexpand(True)
        grid.set_vexpand(True)
        grid.attach(box, 0, 0, 1, 1)
        grid.attach(resize_right, 1, 0, 1, 1)
        grid.attach(resize_bottom, 0, 1, 1, 1)
        grid.attach(resize_corner, 1, 1, 1, 1)
        self.add(grid)
        self.get_style_context().add_class(role.lower())

    def set_text(self, text):
        if text:
            if self.focus_mode:
                self.active_answer = ""
                self.answer_history.append(text)
                self.focus_placeholder = ""
                self._render_focus_answers()
                buffer = self.text.get_buffer()
                answer_start = len("\n\n".join(self.answer_history[:-1]))
                if answer_start:
                    answer_start += 2
                self._set_latest_answer_mark(
                    buffer.get_iter_at_offset(answer_start)
                )
                GLib.idle_add(
                    self._align_latest_answer_once,
                    self.latest_answer_mark,
                )
            else:
                self.text.set_text(text)
        elif self.focus_mode:
            self.active_answer = ""
            self._render_focus_answers()

    def discard_current_answer(self, *, remove_completed=False):
        if not self.focus_mode:
            return
        self.active_answer = ""
        if remove_completed and self.answer_history:
            self.answer_history.pop()
        self._render_focus_answers()

    def prepare_corrected_answer_alignment(self):
        """Keep F9's pending corrected answer at its future stream position."""
        if not self.focus_mode:
            return
        buffer = self.text.get_buffer()
        if self.answer_history:
            buffer.insert(buffer.get_end_iter(), "\n\n")
        self._set_latest_answer_mark(buffer.get_end_iter())
        # Align now, then once more after GTK has processed the buffer resize.
        self._align_latest_answer_once(self.latest_answer_mark)
        GLib.idle_add(
            self._align_latest_answer_once,
            self.latest_answer_mark,
        )

    def configure_answer_ui_diagnostics(self, context, logger):
        """Attach one diagnostic record to the next streamed answer only."""
        self._pending_answer_ui_diagnostics = (dict(context), logger)

    def answer_ui_diagnostic_snapshot(self, mark=None):
        """Return UI state for logs without mutating the displayed answer."""
        snapshot = {
            "history_count": len(self.answer_history),
            "latest_answer_mark_offset": None,
            "latest_answer_y": None,
            "scroll_value": None,
            "vadjustment_lower": None,
            "vadjustment_upper": None,
            "vadjustment_page_size": None,
            "maximum_scroll": None,
            "mark_is_current": None,
        }
        if not self.focus_mode:
            return snapshot
        selected_mark = self.latest_answer_mark if mark is None else mark
        snapshot["mark_is_current"] = (
            selected_mark is not None
            and selected_mark is self.latest_answer_mark
        )
        if selected_mark is not None:
            try:
                answer_start = self.text.get_buffer().get_iter_at_mark(
                    selected_mark
                )
                snapshot["latest_answer_mark_offset"] = answer_start.get_offset()
                snapshot["latest_answer_y"] = self.text.get_iter_location(
                    answer_start
                ).y
            except (TypeError, ValueError):
                pass
        adjustment = self.focus_scroller.get_vadjustment()
        lower = adjustment.get_lower()
        upper = adjustment.get_upper()
        page_size = adjustment.get_page_size()
        snapshot.update({
            "scroll_value": adjustment.get_value(),
            "vadjustment_lower": lower,
            "vadjustment_upper": upper,
            "vadjustment_page_size": page_size,
            "maximum_scroll": max(lower, upper - page_size),
        })
        return snapshot

    def set_status(self, text):
        if self.focus_mode:
            self.focus_placeholder = text
            if not self.answer_history and not self.active_answer:
                self._render_focus_answers()
        else:
            self.text.set_text(text)

    def set_boundary_status(self, text):
        if self.boundary_status is not None:
            self.boundary_status.set_text(text)

    def set_response_status(self, text):
        if self.response_status is not None:
            self.response_status.set_text(text)

    def start_stream(self, text):
        if not self.focus_mode:
            self.text.set_text(text)
            return
        self.active_answer = ""
        self.focus_placeholder = ""
        self._render_focus_answers()
        buffer = self.text.get_buffer()
        if self.answer_history:
            buffer.insert(buffer.get_end_iter(), "\n\n")
        self._set_latest_answer_mark(buffer.get_end_iter())
        buffer.insert(buffer.get_end_iter(), text)
        self.active_answer = text
        diagnostic = getattr(self, "_pending_answer_ui_diagnostics", None)
        self._pending_answer_ui_diagnostics = None
        if diagnostic is not None:
            diagnostics_by_mark = getattr(
                self,
                "_answer_ui_diagnostics_by_mark",
                None,
            )
            if diagnostics_by_mark is None:
                diagnostics_by_mark = {}
                self._answer_ui_diagnostics_by_mark = diagnostics_by_mark
            diagnostics_by_mark[id(self.latest_answer_mark)] = (
                diagnostic
            )
        GLib.idle_add(
            self._align_latest_answer_once,
            self.latest_answer_mark,
        )

    def append_stream(self, text):
        if not text:
            return
        if not self.focus_mode:
            self.text.set_text(f"{self.text.get_text()}{text}")
            return
        self.active_answer += text
        self.text.get_buffer().insert(
            self.text.get_buffer().get_end_iter(),
            text,
        )

    def finish_stream(self, text):
        if not self.focus_mode:
            self.set_text(text)
            return
        buffer = self.text.get_buffer()
        if self.latest_answer_mark is None:
            self.set_text(text)
            return
        answer_start = buffer.get_iter_at_mark(self.latest_answer_mark)
        current = buffer.get_text(
            answer_start,
            buffer.get_end_iter(),
            True,
        )
        if current != text:
            buffer.delete(answer_start, buffer.get_end_iter())
            buffer.insert(buffer.get_end_iter(), text)
        self.active_answer = ""
        self.answer_history.append(text)
        self.focus_placeholder = ""

    def _render_focus_answers(self):
        self._clear_latest_answer_mark()
        parts = [*self.answer_history]
        if self.active_answer:
            parts.append(self.active_answer)
        rendered = "\n\n".join(parts) or self.focus_placeholder
        self.text.get_buffer().set_text(rendered)

    def _clear_latest_answer_mark(self):
        if self.latest_answer_mark is not None:
            self.text.get_buffer().delete_mark(self.latest_answer_mark)
            self.latest_answer_mark = None

    def _set_latest_answer_mark(self, position):
        self._clear_latest_answer_mark()
        self.latest_answer_mark = self.text.get_buffer().create_mark(
            None,
            position,
            True,
        )

    def _align_latest_answer_once(self, mark):
        diagnostic = getattr(
            self,
            "_answer_ui_diagnostics_by_mark",
            {},
        ).pop(id(mark), None)
        snapshot = getattr(self, "answer_ui_diagnostic_snapshot", None)
        before = snapshot(mark) if snapshot is not None else None
        if mark is self.latest_answer_mark:
            buffer = self.text.get_buffer()
            answer_start = buffer.get_iter_at_mark(mark)
            answer_rect = self.text.get_iter_location(answer_start)
            adjustment = self.focus_scroller.get_vadjustment()
            minimum = adjustment.get_lower()
            maximum = max(
                minimum,
                adjustment.get_upper() - adjustment.get_page_size(),
            )
            adjustment.set_value(max(minimum, min(answer_rect.y, maximum)))
        after = snapshot(mark) if snapshot is not None else None
        if diagnostic is not None:
            context, logger = diagnostic
            logger("answer_scroll_align", context, before, after)
        return False

    def _focus_scroll(self, _widget, event):
        if not self.focus_mode:
            return True

        step = 0
        if event.direction == Gdk.ScrollDirection.UP:
            step = -1
        elif event.direction == Gdk.ScrollDirection.DOWN:
            step = 1
        elif event.direction == Gdk.ScrollDirection.SMOOTH:
            self.smooth_scroll_delta += event.delta_y
            if abs(self.smooth_scroll_delta) < ANSWER_SMOOTH_SCROLL_THRESHOLD:
                return True
            step = 1 if self.smooth_scroll_delta > 0 else -1
            self.smooth_scroll_delta = 0.0
        if not step:
            return True

        now = time.monotonic()
        if (
            self.last_focus_scroll_at is not None
            and now - self.last_focus_scroll_at
            < ANSWER_SCROLL_DEBOUNCE_MS / 1000
        ):
            return True
        self.last_focus_scroll_at = now
        adjustment = self.focus_scroller.get_vadjustment()
        minimum = adjustment.get_lower()
        maximum = max(minimum, adjustment.get_upper() - adjustment.get_page_size())
        target = adjustment.get_value() + step * ANSWER_CONTENT_SCROLL_PIXELS
        adjustment.set_value(max(minimum, min(target, maximum)))
        return True

    def _answer_view_resized(self, _widget, allocation):
        if self.focus_mode:
            self.text.set_bottom_margin(
                max(ANSWER_POSITION_GUIDE_HEIGHT, allocation.height * 2)
            )

    def _drag(self, _widget, event):
        if event.button == 1:
            self.begin_move_drag(
                event.button,
                int(event.x_root),
                int(event.y_root),
                event.time,
            )
            return True
        if event.button == 3:
            self.begin_resize_drag(
                Gdk.WindowEdge.SOUTH_EAST,
                event.button,
                int(event.x_root),
                int(event.y_root),
                event.time,
            )
            return True
        return False

    def _resize_handle(self, edge, tooltip, width, height, style_class):
        handle = Gtk.EventBox()
        handle.set_visible_window(True)
        handle.set_size_request(width, height)
        handle.set_tooltip_text(tooltip)
        handle.get_style_context().add_class("resize-handle")
        handle.get_style_context().add_class(style_class)
        handle.connect("button-press-event", self._resize, edge)
        return handle

    def _resize(self, _widget, event, edge):
        if event.button == 1:
            self.begin_resize_drag(
                edge,
                event.button,
                int(event.x_root),
                int(event.y_root),
                event.time,
            )
            return True
        return False

    def _delete(self, *_args):
        self.on_close()
        return True
