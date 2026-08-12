#!/usr/bin/env python3
"""Thin GTK mode launcher for the existing Interview Assistant entrypoint."""

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk


APP_DIR = Path(__file__).resolve().parent
NORMAL_MODE = "normal"
PERFORMANCE_MODE = "performance"
STT_DIAGNOSTIC_MODE = "stt_diagnostic"
# Compatibility for callers that imported the old constant name.
DEBUG_MODE = STT_DIAGNOSTIC_MODE
MODE_CONFIG = {
    NORMAL_MODE: {
        "title": "Normal Interview",
        "codex": True,
        "logging": False,
        "diagnostics": False,
        "label_required": False,
    },
    PERFORMANCE_MODE: {
        "title": "Performance Test",
        "codex": True,
        "logging": True,
        "diagnostics": False,
        "label_required": True,
    },
    STT_DIAGNOSTIC_MODE: {
        "title": "STT Diagnostic",
        "codex": False,
        "logging": True,
        "diagnostics": True,
        "label_required": True,
    },
}
MODES = set(MODE_CONFIG)
LABEL_REQUIRED_MODES = {
    mode for mode, config in MODE_CONFIG.items() if config["label_required"]
}
STARTUP_CHECK_MS = 500


def runtime_log_path(environment=None):
    environment = os.environ if environment is None else environment
    runtime_dir = environment.get("XDG_RUNTIME_DIR") or "/tmp"
    return Path(runtime_dir) / "interview-assistant.log"


def application_argv(app_dir=APP_DIR):
    app_dir = Path(app_dir)
    return [
        str(app_dir / ".venv" / "bin" / "python"),
        str(app_dir / "interview_app.py"),
    ]


def mode_environment(mode, label="", base_environment=None):
    if mode not in MODES:
        raise ValueError(f"unsupported launch mode: {mode}")
    environment = dict(
        os.environ if base_environment is None else base_environment
    )
    config = MODE_CONFIG[mode]
    environment["INTERVIEW_APP_MODE"] = mode
    environment["INTERVIEW_DISABLE_CODEX"] = "0" if config["codex"] else "1"
    environment["INTERVIEW_TEST_LOG"] = "1" if config["logging"] else "0"
    environment["INTERVIEW_STT_DIAGNOSTICS"] = (
        "1" if config["diagnostics"] else "0"
    )
    if config["label_required"]:
        cleaned_label = label.strip()
        if not cleaned_label:
            raise ValueError("test label must not be empty")
        environment["INTERVIEW_TEST_LABEL"] = cleaned_label
    else:
        environment.pop("INTERVIEW_TEST_LABEL", None)
    return environment


def ensure_codex_cli_path(environment):
    existing = shutil.which("codex", path=environment.get("PATH", ""))
    if existing:
        return Path(existing)

    home = Path(environment.get("HOME") or Path.home())
    candidates = [
        home / ".local/bin/codex",
        home / ".npm-global/bin/codex",
        home / ".volta/bin/codex",
        home / ".asdf/shims/codex",
    ]
    candidates.extend(
        sorted(
            (home / ".nvm/versions/node").glob("*/bin/codex"),
            reverse=True,
        )
    )
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            current_path = environment.get("PATH", "")
            path_parts = [str(candidate.parent)]
            if current_path:
                path_parts.append(current_path)
            environment["PATH"] = os.pathsep.join(path_parts)
            return candidate
    return None


def prepare_launch_environment(mode, label="", base_environment=None):
    environment = mode_environment(mode, label, base_environment)
    if MODE_CONFIG[mode]["codex"] and ensure_codex_cli_path(environment) is None:
        raise FileNotFoundError(
            "Codex CLI was not found. Install Codex or make it available "
            "in PATH, ~/.local/bin, or an NVM Node bin directory."
        )
    return environment


def displayed_command(mode, label=""):
    if mode not in MODES:
        raise ValueError(f"unsupported launch mode: {mode}")
    if mode == NORMAL_MODE:
        return "./start_interview_app.sh"
    config = MODE_CONFIG[mode]
    assignments = [
        f"INTERVIEW_APP_MODE={mode}",
        f"INTERVIEW_DISABLE_CODEX={'0' if config['codex'] else '1'}",
        f"INTERVIEW_TEST_LOG={'1' if config['logging'] else '0'}",
        "INTERVIEW_STT_DIAGNOSTICS="
        f"{'1' if config['diagnostics'] else '0'}",
    ]
    assignments.append(
        f"INTERVIEW_TEST_LABEL={shlex.quote(label.strip() or '<label>')}"
    )
    return " \\\n".join(assignments + [".venv/bin/python interview_app.py"])


def validate_runtime(app_dir=APP_DIR):
    python_path, app_path = map(Path, application_argv(app_dir))
    if not python_path.is_file():
        raise FileNotFoundError(
            f"Python virtual environment was not found: {python_path}"
        )
    if not os.access(python_path, os.X_OK):
        raise PermissionError(f"Python is not executable: {python_path}")
    if not app_path.is_file():
        raise FileNotFoundError(f"Application entrypoint was not found: {app_path}")


class ModeCard(Gtk.Frame):
    def __init__(
        self,
        mode,
        title,
        description,
        on_launch,
        label_prompt=None,
        placeholder=None,
    ):
        super().__init__()
        self.mode = mode
        self.on_launch = on_launch
        self.set_shadow_type(Gtk.ShadowType.NONE)
        self.get_style_context().add_class("mode-card")

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        heading = Gtk.Label(label=title)
        heading.set_xalign(0)
        heading.get_style_context().add_class("mode-title")
        body.pack_start(heading, False, False, 0)
        detail = Gtk.Label(label=description)
        detail.set_xalign(0)
        detail.set_line_wrap(True)
        detail.get_style_context().add_class("mode-description")
        body.pack_start(detail, False, False, 0)

        self.label_entry = None
        self.validation_label = None
        if label_prompt is not None:
            prompt = Gtk.Label(label=label_prompt)
            prompt.set_xalign(0)
            prompt.get_style_context().add_class("label-helper")
            body.pack_start(prompt, False, False, 1)
            self.label_entry = Gtk.Entry()
            self.label_entry.set_placeholder_text(placeholder)
            self.label_entry.connect("changed", self._label_changed)
            body.pack_start(self.label_entry, False, False, 0)
            self.validation_label = Gtk.Label(
                label="Label을 입력한 뒤 다시 실행해주세요."
            )
            self.validation_label.set_xalign(0)
            self.validation_label.get_style_context().add_class(
                "validation-error"
            )
            self.validation_label.set_no_show_all(True)
            body.pack_start(self.validation_label, False, False, 0)

        self.command_revealer = Gtk.Revealer()
        self.command_revealer.set_transition_type(
            Gtk.RevealerTransitionType.NONE
        )
        self.command_label = Gtk.Label()
        self.command_label.set_xalign(0)
        self.command_label.set_selectable(True)
        self.command_label.get_style_context().add_class("command-preview")
        self.command_revealer.add(self.command_label)
        body.pack_start(self.command_revealer, False, False, 2)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.launch_button = Gtk.Button(label="실행")
        self.launch_button.get_style_context().add_class("suggested-action")
        self.launch_button.connect("clicked", self._launch)
        actions.pack_end(self.launch_button, False, False, 0)
        self.command_button = Gtk.Button(label="명령 보기")
        self.command_button.connect("clicked", self._toggle_command)
        actions.pack_end(self.command_button, False, False, 0)
        body.pack_start(actions, False, False, 0)
        self.add(body)
        self._update_command()

    def label(self):
        return self.label_entry.get_text().strip() if self.label_entry else ""

    def set_launch_sensitive(self, sensitive):
        self.launch_button.set_sensitive(sensitive)

    def _label_changed(self, entry):
        entry.get_style_context().remove_class("error")
        if self.validation_label is not None:
            self.validation_label.hide()
        self._update_command()

    def _update_command(self):
        self.command_label.set_text(displayed_command(self.mode, self.label()))

    def _toggle_command(self, _button):
        reveal = not self.command_revealer.get_reveal_child()
        self.command_revealer.set_reveal_child(reveal)
        self.command_button.set_label(
            "명령 숨기기" if reveal else "명령 보기"
        )

    def _launch(self, _button):
        if self.label_entry is not None and not self.label():
            self.label_entry.get_style_context().add_class("error")
            self.validation_label.show()
            self.label_entry.grab_focus()
            return
        self.on_launch(self.mode, self.label())


class InterviewLauncher(Gtk.Window):
    def __init__(self):
        super().__init__(title="Interview Assistant")
        self.set_default_size(680, 680)
        self.set_resizable(True)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_border_width(16)
        self.connect("destroy", self._quit)
        self.pending_process = None
        self.mode_cards = []

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        title = Gtk.Label(label="Interview Assistant")
        title.set_xalign(0)
        title.get_style_context().add_class("launcher-title")
        content.pack_start(title, False, False, 0)
        subtitle = Gtk.Label(label="실행 모드를 선택하세요.")
        subtitle.set_xalign(0)
        subtitle.get_style_context().add_class("launcher-subtitle")
        content.pack_start(subtitle, False, False, 0)

        cards = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        cards.pack_start(self._add_card(
            NORMAL_MODE,
            "Normal Interview",
            "실제 면접에서 사용하는 기본 모드",
        ), False, False, 0)
        cards.pack_start(self._add_card(
            PERFORMANCE_MODE,
            "Performance Test",
            "JSONL 성능 로그를 test_runs/에 기록",
            "테스트 로그를 구분할 이름을 입력하세요.",
            "예: a2z, latency-test, english-practice",
        ), False, False, 0)
        cards.pack_start(self._add_card(
            STT_DIAGNOSTIC_MODE,
            "STT Diagnostic",
            "Codex 없이 Session과 Preparation을 거쳐 STT/F8/F9 진단",
            "디버그 로그를 구분할 이름을 입력하세요.",
            "예: audio-debug, stt-check",
        ), False, False, 0)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_shadow_type(Gtk.ShadowType.NONE)
        scroller.add(cards)
        content.pack_start(scroller, True, True, 0)
        self.add(content)

    def _quit(self, *_args):
        if Gtk.main_level():
            Gtk.main_quit()

    def _add_card(
        self,
        mode,
        title,
        description,
        label_prompt=None,
        placeholder=None,
    ):
        card = ModeCard(
            mode,
            title,
            description,
            self.launch,
            label_prompt,
            placeholder,
        )
        self.mode_cards.append(card)
        return card

    def launch(self, mode, label):
        if self.pending_process is not None:
            return
        log_path = runtime_log_path()
        try:
            validate_runtime()
            environment = prepare_launch_environment(mode, label)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("ab", buffering=0) as log_stream:
                process = subprocess.Popen(
                    application_argv(),
                    cwd=APP_DIR,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=log_stream,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        except (OSError, ValueError) as error:
            self._show_launch_error(error, log_path)
            return
        self.pending_process = process
        for card in self.mode_cards:
            card.set_launch_sensitive(False)
        GLib.timeout_add(
            STARTUP_CHECK_MS,
            self._finish_startup_check,
            process,
            log_path,
        )

    def _finish_startup_check(self, process, log_path):
        if process is not self.pending_process:
            return False
        return_code = process.poll()
        if return_code is None:
            self.destroy()
            return False
        self.pending_process = None
        for card in self.mode_cards:
            card.set_launch_sensitive(True)
        self._show_launch_error(
            RuntimeError(
                f"Interview Assistant exited during startup (code {return_code})."
            ),
            log_path,
        )
        return False

    def _show_launch_error(self, error, log_path):
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.CLOSE,
            text="Interview Assistant를 실행하지 못했습니다.",
        )
        dialog.format_secondary_text(
            f"{error}\n\nRuntime log: {log_path}"
        )
        dialog.run()
        dialog.destroy()


def install_css():
    css = b"""
    window { background-color: #15181d; color: #e8edf3; }
    frame.mode-card {
        background-color: rgba(37, 41, 48, 0.76);
        border: 1px solid rgba(174, 181, 191, 0.16);
        border-radius: 8px;
        padding: 12px;
    }
    .launcher-title { color: #e8edf3; font: bold 20px Sans; }
    .launcher-subtitle { color: #aeb7c3; font: 11px Sans; }
    .mode-title { color: #e8edf3; font: bold 13px Sans; }
    .mode-description { color: #aeb7c3; font: 10px Sans; }
    .label-helper { color: #c6ccd5; font: 10px Sans; }
    .command-preview {
        color: #c9d8e8;
        background-color: #101318;
        border: 1px solid rgba(174, 181, 191, 0.12);
        border-radius: 5px;
        padding: 8px;
        font: 10px Monospace;
    }
    .validation-error { color: #ef8d8d; font: 10px Sans; }
    entry.error { border-color: #d96b6b; }
    """
    provider = Gtk.CssProvider()
    provider.load_from_data(css)
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(),
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )


def main():
    install_css()
    launcher = InterviewLauncher()
    launcher.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
