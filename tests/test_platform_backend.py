import unittest
from unittest.mock import patch

import interview_app
from linux_port.backend import LinuxPlatformBackend
from platform_backend import (
    PULSEAUDIO_AUDIO_BACKEND,
    WINDOWS_BRIDGE_AUDIO_BACKEND,
    create_platform_backend,
)
from windows_port.app_backend import WindowsPlatformBackend


class _AudioStream:
    def __init__(self, _role, _source, on_pcm, _on_error):
        self.on_pcm = on_pcm
        self.started = False
        self.aborted = False
        self.stopped = False

    def start(self):
        self.started = True

    def abort(self):
        self.aborted = True

    def stop(self):
        self.stopped = True

    def capture_sample_cursor_and(self, enqueue):
        return 160, enqueue(160)


class _BridgeStream:
    def __init__(
        self,
        _role,
        _worker,
        _on_error,
        on_hotkey,
        on_status,
        *,
        on_pcm,
    ):
        self.on_pcm = on_pcm
        self.on_hotkey = on_hotkey
        self.on_status = on_status
        self.started = False
        self.aborted = False
        self.stopped = False

    def start(self):
        self.started = True

    def abort(self):
        self.aborted = True

    def stop(self):
        self.stopped = True

    def capture_sample_cursor_and(self, enqueue):
        return 320, enqueue(320)

    def cursor_state(self):
        return {"received_cursor": 320, "audio_drop_samples": 0}


class PlatformBackendTests(unittest.TestCase):
    def test_runtime_default_remains_pulseaudio(self):
        self.assertEqual(
            interview_app.runtime_options({})["audio_backend"],
            PULSEAUDIO_AUDIO_BACKEND,
        )

    def test_selection_keeps_pulseaudio_as_default_linux_backend(self):
        with patch("linux_port.backend.get_interviewer_audio_source", return_value="sink.monitor"):
            backend = create_platform_backend(
                PULSEAUDIO_AUDIO_BACKEND,
                worker=object(),
                on_pcm=lambda *_args: None,
                on_error=lambda *_args: None,
                on_f8=lambda **_kwargs: None,
                on_f9=lambda **_kwargs: None,
                on_stop=lambda: None,
                on_status=lambda _status: None,
                gio=object(),
                idle_add=lambda *_args: None,
                app_command_path="interview_app.py",
                is_running=lambda: True,
            )

        self.assertIsInstance(backend, LinuxPlatformBackend)
        self.assertEqual(backend.name, PULSEAUDIO_AUDIO_BACKEND)

    def test_selection_uses_windows_backend_without_linux_pulse_setup(self):
        with patch("linux_port.backend.get_interviewer_audio_source") as pulse_source:
            backend = create_platform_backend(
                WINDOWS_BRIDGE_AUDIO_BACKEND,
                worker=object(),
                on_pcm=lambda *_args: None,
                on_error=lambda *_args: None,
                on_f8=lambda **_kwargs: None,
                on_f9=lambda **_kwargs: None,
                on_stop=lambda: None,
                on_status=lambda _status: None,
            )

        self.assertIsInstance(backend, WindowsPlatformBackend)
        pulse_source.assert_not_called()

    def test_linux_pcm_f8_f9_and_stop_contract_stays_transport_only(self):
        pcm = []
        backend = None
        with patch("linux_port.backend.get_interviewer_audio_source", return_value="sink.monitor"):
            backend = LinuxPlatformBackend(
                lambda data, start, end: pcm.append((data, start, end)),
                lambda *_args: None,
                lambda **_kwargs: None,
                lambda **_kwargs: None,
                lambda: None,
                lambda _status: None,
                gio=object(),
                idle_add=lambda *_args: None,
                app_command_path="interview_app.py",
                is_running=lambda: True,
                audio_stream_factory=_AudioStream,
            )

        backend.start()
        target, accepted = backend.capture_sample_cursor_and(lambda cursor: cursor == 160)
        backend.audio_stream.on_pcm(b"\0\0", 0, 1)
        backend.abort()
        backend.stop()

        self.assertTrue(backend.audio_stream.started)
        self.assertTrue(accepted)
        self.assertEqual(target, 160)
        self.assertEqual(pcm, [(b"\0\0", 0, 1)])
        self.assertTrue(backend.audio_stream.aborted)
        self.assertTrue(backend.audio_stream.stopped)

    def test_windows_pcm_f8_f9_and_stop_contract_uses_shared_callbacks(self):
        pcm = []
        f8 = []
        f9 = []
        backend = WindowsPlatformBackend(
            object(),
            lambda data, start, end: pcm.append((data, start, end)),
            lambda *_args: None,
            lambda **kwargs: f8.append(kwargs),
            lambda **kwargs: f9.append(kwargs),
            lambda _status: None,
            stream_factory=_BridgeStream,
        )

        backend.start()
        backend.audio_stream.on_pcm(b"\0\0", 0, 1)
        backend.audio_stream.on_hotkey(
            {"key": "F8", "sequence": 1},
            lambda enqueue: (320, enqueue(320), {}),
        )
        backend.audio_stream.on_hotkey(
            {"key": "F9", "sequence": 2},
            lambda enqueue: (480, enqueue(480), {}),
        )
        target, accepted = backend.capture_sample_cursor_and(lambda cursor: cursor == 320)
        backend.abort()
        backend.stop()

        self.assertTrue(backend.audio_stream.started)
        self.assertEqual(pcm, [(b"\0\0", 0, 1)])
        self.assertEqual(f8[0]["hotkey_event"]["key"], "F8")
        self.assertEqual(f9[0]["hotkey_event"]["key"], "F9")
        self.assertTrue(accepted)
        self.assertEqual(target, 320)
        self.assertTrue(backend.audio_stream.aborted)
        self.assertTrue(backend.audio_stream.stopped)


if __name__ == "__main__":
    unittest.main()
