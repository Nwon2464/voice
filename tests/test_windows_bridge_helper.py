import unittest
from unittest.mock import patch

from windows_port.hotkey import HotkeyRegistrationError


class _Writer:
    instances = []

    def __init__(self):
        self.events = []
        self.__class__.instances.append(self)

    def status(self, event, **fields):
        self.events.append((event, fields))

    def hotkey(self, _event):
        pass


class _Capture:
    def __init__(self, *_args, **_kwargs):
        self.device_info = {"name": "Speaker"}
        self.stopped = False

    def start(self):
        pass

    def stop(self):
        self.stopped = True


class _FailingHotkeys:
    def __init__(self, _callback):
        pass

    def start(self):
        raise HotkeyRegistrationError("could not register F8 (Windows error 1409)")

    def stop(self):
        pass


class WindowsBridgeHelperTests(unittest.TestCase):
    def test_hotkey_registration_failure_is_sent_as_a_status(self):
        from windows_port import bridge_helper

        _Writer.instances.clear()
        with (
            patch("windows_port.bridge_helper.os.name", "nt"),
            patch("windows_port.bridge_helper.FrameWriter", _Writer),
            patch("windows_port.bridge_helper.WasapiLoopbackCapture", _Capture),
            patch("windows_port.bridge_helper.GlobalHotkeys", _FailingHotkeys),
        ):
            result = bridge_helper.main(["--hotkeys"])

        self.assertEqual(result, 1)
        self.assertIn(
            ("hotkey_error", {"error": "could not register F8 (Windows error 1409)"}),
            _Writer.instances[0].events,
        )
