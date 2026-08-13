"""Native Win32 RegisterHotKey transport for global F8 and F9 events."""

from __future__ import annotations

import os
import threading
import time


WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
VK_F8 = 0x77
VK_F9 = 0x78
HOTKEYS = {1: ("F8", VK_F8), 2: ("F9", VK_F9)}


class HotkeyRegistrationError(RuntimeError):
    """A requested Windows global hotkey is already registered or unavailable."""


def _windows_api():
    import ctypes
    from ctypes import wintypes

    return ctypes, ctypes.windll.user32, ctypes.windll.kernel32, wintypes


class GlobalHotkeys:
    """Register F8/F9 on a Windows message thread and unregister on shutdown."""

    def __init__(self, callback, *, api_factory=None):
        self.callback = callback
        self.api_factory = api_factory or _windows_api
        self.thread = None
        self.thread_id = None
        self.ready = threading.Event()
        self.error = None
        self.sequence = 0

    def start(self) -> None:
        if os.name != "nt":
            raise RuntimeError("GlobalHotkeys is available only on Windows")
        if self.thread is not None:
            return
        self.ready.clear()
        self.error = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        if not self.ready.wait(timeout=3):
            raise RuntimeError("timed out while registering global F8/F9 hotkeys")
        if self.error is not None:
            self.thread.join(timeout=1)
            self.thread = None
            self.thread_id = None
            raise HotkeyRegistrationError(self.error)

    def stop(self) -> None:
        thread = self.thread
        if thread is None:
            return
        if self.thread_id is not None and thread.is_alive():
            ctypes, user32, _kernel32, _wintypes = self.api_factory()
            user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)
        thread.join(timeout=3)
        self.thread = None
        self.thread_id = None

    def _run(self) -> None:
        ctypes, user32, kernel32, wintypes = self.api_factory()
        registered_ids = []
        try:
            self.thread_id = kernel32.GetCurrentThreadId()
            for hotkey_id, (_key, virtual_key) in HOTKEYS.items():
                if not user32.RegisterHotKey(None, hotkey_id, 0, virtual_key):
                    code = ctypes.get_last_error()
                    raise HotkeyRegistrationError(
                        f"could not register {_key} (Windows error {code})"
                    )
                registered_ids.append(hotkey_id)
            self.ready.set()
            message = wintypes.MSG()
            while True:
                result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result <= 0:
                    return
                if message.message != WM_HOTKEY:
                    continue
                hotkey = HOTKEYS.get(int(message.wParam))
                if hotkey is None:
                    continue
                self.sequence += 1
                self.callback({
                    "event": "hotkey",
                    "key": hotkey[0],
                    "sequence": self.sequence,
                    "timestamp_ns": time.time_ns(),
                })
        except Exception as error:
            self.error = str(error)
            self.ready.set()
        finally:
            for hotkey_id in reversed(registered_ids):
                user32.UnregisterHotKey(None, hotkey_id)
