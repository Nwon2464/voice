import unittest
from types import SimpleNamespace
from unittest.mock import patch

from windows_port.hotkey import GlobalHotkeys, HotkeyRegistrationError


class _Message:
    message = 0
    wParam = 0


class _FakeUser32:
    def __init__(self, messages=(), fail_hotkey_id=None):
        self.messages = iter(messages)
        self.fail_hotkey_id = fail_hotkey_id
        self.registered = []
        self.unregistered = []
        self.posted = []

    def RegisterHotKey(self, _window, hotkey_id, _modifiers, _virtual_key):
        self.registered.append(hotkey_id)
        return hotkey_id != self.fail_hotkey_id

    def UnregisterHotKey(self, _window, hotkey_id):
        self.unregistered.append(hotkey_id)
        return True

    def GetMessageW(self, message, _window, _minimum, _maximum):
        try:
            message.message, message.wParam = next(self.messages)
            return 1
        except StopIteration:
            return 0

    def PostThreadMessageW(self, *args):
        self.posted.append(args)
        return True


class WindowsHotkeyTests(unittest.TestCase):
    def _hotkeys(self, user32, callback):
        api = (
            SimpleNamespace(byref=lambda value: value, get_last_error=lambda: 1409),
            user32,
            SimpleNamespace(GetCurrentThreadId=lambda: 123),
            SimpleNamespace(MSG=_Message),
        )
        return GlobalHotkeys(callback, api_factory=lambda: api)

    def test_f8_and_f9_events_are_emitted_in_message_order(self):
        events = []
        user32 = _FakeUser32(messages=((0x0312, 1), (0x0312, 2)))
        hotkeys = self._hotkeys(user32, events.append)

        hotkeys._run()

        self.assertEqual(user32.registered, [1, 2])
        self.assertEqual(user32.unregistered, [2, 1])
        self.assertEqual([event["key"] for event in events], ["F8", "F9"])
        self.assertEqual([event["sequence"] for event in events], [1, 2])
        self.assertTrue(all(event["timestamp_ns"] > 0 for event in events))

    def test_registration_failure_unregisters_already_registered_f8(self):
        user32 = _FakeUser32(fail_hotkey_id=2)
        hotkeys = self._hotkeys(user32, lambda _event: None)

        hotkeys._run()

        self.assertIn("could not register F9", hotkeys.error)
        self.assertTrue(hotkeys.ready.is_set())
        self.assertEqual(user32.registered, [1, 2])
        self.assertEqual(user32.unregistered, [1])

    def test_start_surfaces_registration_error(self):
        user32 = _FakeUser32(fail_hotkey_id=1)
        hotkeys = self._hotkeys(user32, lambda _event: None)

        with patch("windows_port.hotkey.os.name", "nt"), self.assertRaises(HotkeyRegistrationError):
            hotkeys.start()

        self.assertEqual(user32.unregistered, [])
