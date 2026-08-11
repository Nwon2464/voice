import tempfile
import unittest
from pathlib import Path

from context_manager import (
    CONTEXT_STATUS_SYNCED,
    ContextManager,
)
from interview_thread_backend import InterviewThreadBackend
from session_store import SessionStore


class FakeCodexClient:
    def __init__(self, thread_id="thread-interview-new", fail_inject=False,
                 fail_archive=False):
        self.thread_id = thread_id
        self.fail_inject = fail_inject
        self.fail_archive = fail_archive
        self.events = []
        self.injected_items = None

    def start(self, **kwargs):
        self.events.append(("start", kwargs))
        return {"thread_id": self.thread_id}

    def inject_items(self, items):
        self.events.append(("inject", items))
        if self.fail_inject:
            raise RuntimeError("inject failed")
        self.injected_items = items

    def archive_thread(self, thread_id):
        self.events.append(("archive", thread_id))
        if self.fail_archive:
            raise RuntimeError("archive failed")

    def stop(self):
        self.events.append(("stop", None))


class InterviewThreadBackendTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.temporary_directory.name)
        self.context_manager = ContextManager(self.config_dir)
        self.session_store = SessionStore(self.config_dir / "sessions.json")
        self.settings = {
            "codex_model": "gpt-5.6-terra",
            "codex_reasoning_effort": "high",
            "codex_fast_mode": True,
            "stt_language": "en",
        }
        self.session_store.add(
            "session-local",
            "Session",
            settings=self.settings,
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _backend(self, client):
        captured_settings = []

        def factory(settings):
            captured_settings.append(settings)
            return client

        return InterviewThreadBackend(
            self.session_store,
            self.context_manager,
            factory,
        ), captured_settings

    def test_create_injects_one_authoritative_snapshot_and_saves_state(self):
        global_context = self.context_manager.create_context(
            "global",
            "session-local",
            "Profile",
        )
        global_context.path.write_text("Global profile", encoding="utf-8")
        session_context = self.context_manager.create_context(
            "session",
            "session-local",
            "Company",
        )
        session_context.path.write_text("Session company", encoding="utf-8")
        overridden_global = self.context_manager.global_context_dir / (
            "answer_style.md"
        )
        overridden_global.write_text("Old global style", encoding="utf-8")
        override = self.context_manager.create_context(
            "session",
            "session-local",
            "Answer Style",
        )
        override.path.write_text("Current session style", encoding="utf-8")
        client = FakeCodexClient()
        backend, captured_settings = self._backend(client)

        result = backend.create(self.session_store.active()[0])

        self.assertEqual(captured_settings, [self.settings])
        self.assertEqual(client.events[0], ("start", {"ephemeral": False}))
        self.assertEqual(client.events[-1], ("stop", None))
        self.assertEqual(len(client.injected_items), 1)
        message = client.injected_items[0]
        self.assertEqual(message["role"], "developer")
        snapshot = message["content"][0]["text"]
        self.assertIn("only current authoritative Context", snapshot)
        self.assertIn("Do not use any previous Context", snapshot)
        self.assertIn("Scope: GLOBAL", snapshot)
        self.assertIn("Name: Profile", snapshot)
        self.assertIn("Filename: profile.md", snapshot)
        self.assertIn("Global profile", snapshot)
        self.assertIn("Scope: SESSION", snapshot)
        self.assertIn("Name: Company", snapshot)
        self.assertIn("Filename: company.md", snapshot)
        self.assertIn("Session company", snapshot)
        self.assertIn("Filename: answer-style.md", snapshot)
        self.assertIn("Current session style", snapshot)
        self.assertNotIn("Old global style", snapshot)
        self.assertNotIn("session-local", snapshot)
        self.assertEqual(result["interview_thread_id"], "thread-interview-new")
        session = self.session_store.active()[0]
        self.assertEqual(
            session["session_id"],
            "session-local",
        )
        self.assertEqual(
            session["interview_thread_id"],
            "thread-interview-new",
        )
        states = self.context_manager.resolve_effective_context_states(
            "session-local"
        )
        self.assertTrue(states)
        self.assertTrue(all(
            state.status == CONTEXT_STATUS_SYNCED for state in states
        ))

    def test_existing_interview_thread_is_archived_after_new_inject(self):
        self.session_store.set_interview_thread_id(
            "session-local",
            "thread-interview-old",
        )
        context = self.context_manager.create_context(
            "session",
            "session-local",
            "Company",
        )
        context.path.write_text("Company", encoding="utf-8")
        client = FakeCodexClient()
        backend, _settings = self._backend(client)

        backend.create(self.session_store.active()[0])

        event_names = [event[0] for event in client.events]
        self.assertLess(event_names.index("inject"), event_names.index("archive"))
        self.assertIn(("archive", "thread-interview-old"), client.events)

    def test_inject_failure_preserves_old_thread_and_sync_metadata(self):
        self.session_store.set_interview_thread_id(
            "session-local",
            "thread-interview-old",
        )
        context = self.context_manager.create_context(
            "session",
            "session-local",
            "Company",
        )
        context.path.write_text("old synced content", encoding="utf-8")
        self.context_manager.record_successful_sync(
            "session-local",
            context,
        )
        metadata_path = self.context_manager.sync_metadata_path(
            "session-local"
        )
        metadata_before = metadata_path.read_bytes()
        context.path.write_text("new unsynced content", encoding="utf-8")
        client = FakeCodexClient(fail_inject=True)
        backend, _settings = self._backend(client)

        with self.assertRaisesRegex(RuntimeError, "inject failed"):
            backend.create(self.session_store.active()[0])

        session = self.session_store.active()[0]
        self.assertEqual(
            session["interview_thread_id"],
            "thread-interview-old",
        )
        self.assertEqual(metadata_path.read_bytes(), metadata_before)
        self.assertFalse(any(event[0] == "archive" for event in client.events))
        self.assertEqual(client.events[-1], ("stop", None))

    def test_archive_failure_rolls_back_new_thread_and_sync_hashes(self):
        self.session_store.set_interview_thread_id(
            "session-local",
            "thread-interview-old",
        )
        context = self.context_manager.create_context(
            "session",
            "session-local",
            "Company",
        )
        context.path.write_text("old", encoding="utf-8")
        self.context_manager.record_successful_sync(
            "session-local",
            context,
        )
        metadata_path = self.context_manager.sync_metadata_path(
            "session-local"
        )
        metadata_before = metadata_path.read_bytes()
        context.path.write_text("new", encoding="utf-8")
        client = FakeCodexClient(fail_archive=True)
        backend, _settings = self._backend(client)

        with self.assertRaisesRegex(RuntimeError, "archive failed"):
            backend.create(self.session_store.active()[0])

        self.assertEqual(
            self.session_store.active()[0]["interview_thread_id"],
            "thread-interview-old",
        )
        self.assertEqual(metadata_path.read_bytes(), metadata_before)


if __name__ == "__main__":
    unittest.main()
