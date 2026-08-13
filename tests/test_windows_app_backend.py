import unittest
from windows_port.app_backend import WindowsBridgeAudioStream


class _Worker:
    def __init__(self):
        self.lock = __import__("threading").Lock()
        self.submissions = []
        self.queued_sample_cursor = 0
        self.consumed_sample_cursor = 0
        self.audio_drop_samples = 0

    def submit_pcm(self, pcm, start, end):
        self.submissions.append((pcm, start, end))
        self.queued_sample_cursor = end
        return True


class _BridgeClient:
    instances = []

    def __init__(self, on_pcm, on_status, on_error, *, on_hotkey, capture_audio):
        self.on_pcm = on_pcm
        self.on_status = on_status
        self.on_error = on_error
        self.on_hotkey = on_hotkey
        self.capture_audio = capture_audio
        self.started = False
        self.stopped = False
        self.__class__.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class WindowsBridgeAudioBackendTests(unittest.TestCase):
    def setUp(self):
        _BridgeClient.instances.clear()

    def _backend(self, hotkeys=None, statuses=None, errors=None):
        return WindowsBridgeAudioStream(
            "INTERVIEWER",
            _Worker(),
            lambda role, error: (errors if errors is not None else []).append((role, error)),
            lambda event, capture: (hotkeys if hotkeys is not None else []).append((event, capture)),
            lambda status: (statuses if statuses is not None else []).append(status),
            client_factory=_BridgeClient,
        )

    def test_pcm_is_contiguous_and_f8_snapshot_is_atomic(self):
        hotkeys = []
        backend = self._backend(hotkeys=hotkeys)
        backend.start()
        client = _BridgeClient.instances[0]
        client.on_pcm(bytes(320))
        client.on_pcm(bytes(640))
        self.assertEqual(
            backend.worker.submissions,
            [(bytes(320), 0, 160), (bytes(640), 160, 480)],
        )

        client.on_hotkey({"key": "F8", "sequence": 9, "timestamp_ns": 10})
        event, capture = hotkeys[0]
        targets = []
        target, accepted, state = capture(
            lambda cursor: targets.append(cursor) or True
        )

        self.assertEqual(event["sequence"], 9)
        self.assertTrue(accepted)
        self.assertEqual((target, targets), (480, [480]))
        self.assertEqual(state["received_cursor"], 480)
        self.assertEqual(state["consumed_cursor"], 0)

    def test_status_error_and_shutdown_are_forwarded(self):
        statuses = []
        errors = []
        backend = self._backend(statuses=statuses, errors=errors)
        backend.start()
        client = _BridgeClient.instances[0]
        client.on_status({"event": "ready", "device": {"name": "Speakers"}})
        client.on_error(RuntimeError("helper failed"))
        backend.stop()

        self.assertEqual(statuses[0]["event"], "ready")
        self.assertEqual(str(errors[0][1]), "helper failed")
        self.assertTrue(client.stopped)

if __name__ == "__main__":
    unittest.main()
