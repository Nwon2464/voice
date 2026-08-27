"""Application-wide GTK presentation styles."""

import os
import sys

os.environ.setdefault("GDK_BACKEND", "x11")
try:
    import gi
except ModuleNotFoundError:
    sys.path.append("/usr/lib/python3/dist-packages")
    import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gtk


_APPLICATION_CSS_INSTALLED = False


def install_application_css():
    global _APPLICATION_CSS_INSTALLED
    if _APPLICATION_CSS_INSTALLED:
        return
    css = b"""
    window { background-color: rgba(18, 20, 24, 0.96); }
    window.interviewer, window.answer, window.control { border-radius: 14px; }
    window.interviewer { border: 2px solid rgba(95, 176, 255, 0.85); }
    window.answer { border: 2px solid rgba(255, 195, 92, 0.82); }
    window.control { border: 2px solid rgba(255, 195, 92, 0.82); }
    window.preparation-window, window.session-window {
        background-color: #15181d;
    }
    frame.preparation-card {
        background-color: rgba(37, 41, 48, 0.72);
        border: 1px solid rgba(174, 181, 191, 0.16);
        border-radius: 8px;
        padding: 10px;
    }
    frame.preparation-status-bar {
        background-color: rgba(37, 41, 48, 0.58);
        border: 1px solid rgba(174, 181, 191, 0.14);
        border-radius: 7px;
        padding: 6px 9px;
    }
    .status-session { color: #e8edf3; font: bold 11px Sans; }
    .status-stt { color: #aeb7c3; font: 10px Sans; }
    .context-panel-toggle { padding: 3px 8px; }
    .settings-button { padding: 3px 9px; }
    .settings-heading { color: #dce8f5; font: bold 11px Sans; }
    .section-title {
        color: #e8edf3;
        font: bold 12px Sans;
        padding: 0 0 4px;
    }
    .section-description { color: #9fa8b5; font: 10px Sans; }
    .stt-model-title { color: #dce8f5; font: bold 11px Sans; }
    .stt-model-detail { color: #9fa8b5; font: 10px Sans; }
    .context-header { color: #8f99a7; font: bold 9px Sans; }
    .context-filename { color: #aeb7c3; font: 10px Monospace; }
    .context-badge {
        border-radius: 5px;
        padding: 3px 7px;
        font: bold 9px Sans;
    }
    .scope-global {
        color: #9dccff;
        background-color: rgba(65, 126, 181, 0.28);
        border: 1px solid rgba(115, 177, 231, 0.34);
    }
    .scope-session {
        color: #d6b8ff;
        background-color: rgba(121, 82, 166, 0.30);
        border: 1px solid rgba(172, 130, 219, 0.34);
    }
    .status-synced {
        color: #9ed6ad;
        background-color: rgba(61, 128, 79, 0.25);
        border: 1px solid rgba(102, 170, 120, 0.30);
    }
    .status-changed {
        color: #f0c67c;
        background-color: rgba(159, 111, 37, 0.27);
        border: 1px solid rgba(214, 160, 72, 0.32);
    }
    .status-not-synced {
        color: #c7ccd4;
        background-color: rgba(105, 112, 124, 0.25);
        border: 1px solid rgba(151, 158, 169, 0.28);
    }
    .context-edit { padding: 2px 10px; }
    textview.conversation-view, textview.conversation-view text {
        color: #eef2f6;
        background-color: #101318;
    }
    .heading { color: #8ec8ff; font: bold 12px Sans; letter-spacing: 1px; }
    window.answer .heading { color: #ffc75c; }
    .position-guide { border: 2px solid rgba(255, 195, 92, 0.75); background: transparent; }
    .focus-transcript { color: #fff5d9; background: transparent; font: bold 22px Sans; }
    .focus-transcript text { color: #fff5d9; background: transparent; }
    .transcript { color: #ffffff; font: 20px Sans; }
    .boundary-status { color: rgba(142, 200, 255, 0.78); font: bold 11px Sans; padding: 1px 2px 0; border-top: 1px solid rgba(142, 200, 255, 0.18); }
    .response-status { color: rgba(255, 199, 92, 0.78); font: bold 11px Sans; padding: 1px 2px 0; border-top: 1px solid rgba(255, 199, 92, 0.18); }
    .shortcut-reminder { color: rgba(174, 181, 191, 0.68); font: 10px Sans; padding: 1px 2px 0; }
    .close-button { color: #d8dde5; font: bold 18px Sans; padding: 0 4px; }
    .close-button:hover { color: #ffffff; background: rgba(255, 90, 90, 0.55); }
    .control-button { color: #fff5d9; font: bold 18px Sans; padding: 2px 8px; }
    .visibility-button { padding: 2px 4px; }
    .control-drag { color: #aeb5bf; font: 18px Sans; padding: 0 4px; }
    .resize-handle { background: rgba(139, 146, 157, 0.16); }
    .resize-handle:hover { background: rgba(142, 200, 255, 0.55); }
    .resize-corner { color: #aeb5bf; font: 12px Sans; }
    """
    provider = Gtk.CssProvider()
    provider.load_from_data(css)
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(),
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )
    _APPLICATION_CSS_INSTALLED = True
