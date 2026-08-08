"""Native Windows global F8 registration."""

import os
import threading


WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
VK_F8 = 0x77


class GlobalF8Hotkey:
    def __init__(self, callback):
        self.callback = callback
        self.thread = None
        self.thread_id = None
        self.ready = threading.Event()
        self.error = None

    def start(self):
        if os.name != "nt":
            raise RuntimeError("GlobalF8Hotkey is available only on Windows")
        if self.thread is not None:
            return
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        self.ready.wait(timeout=3)
        if not self.ready.is_set():
            raise RuntimeError("Timed out while registering global F8")
        if self.error is not None:
            raise RuntimeError(self.error)

    def stop(self):
        if self.thread is None:
            return
        if self.thread_id is not None:
            import ctypes

            ctypes.windll.user32.PostThreadMessageW(
                self.thread_id,
                WM_QUIT,
                0,
                0,
            )
        self.thread.join(timeout=3)
        self.thread = None
        self.thread_id = None

    def _run(self):
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        self.thread_id = kernel32.GetCurrentThreadId()
        hotkey_id = 1
        if not user32.RegisterHotKey(None, hotkey_id, 0, VK_F8):
            code = ctypes.get_last_error()
            self.error = f"Could not register F8 (Windows error {code})"
            self.ready.set()
            return
        self.ready.set()
        message = wintypes.MSG()
        try:
            while True:
                result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result <= 0:
                    return
                if message.message == WM_HOTKEY and message.wParam == hotkey_id:
                    self.callback()
        finally:
            user32.UnregisterHotKey(None, hotkey_id)
