import tempfile
import threading
import time
import unittest
import json
from pathlib import Path

from session_store import SessionStore, SessionStoreError


class SessionStoreTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary_directory.name) / "sessions.json"
        self.store = SessionStore(self.path)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_active_sessions_are_recent_first(self):
        self.store.add("thread-old", "Old", "2026-08-08T10:00:00+09:00")
        self.store.add("thread-new", "New", "2026-08-08T11:00:00+09:00")

        self.assertEqual(
            [session["session_id"] for session in self.store.active()],
            ["thread-new", "thread-old"],
        )

    def test_mark_used_moves_session_to_top(self):
        self.store.add("thread-a", "A", "2026-08-08T10:00:00+09:00")
        self.store.add("thread-b", "B", "2026-08-08T11:00:00+09:00")

        self.store.mark_used("thread-a", "2026-08-08T12:00:00+09:00")

        self.assertEqual(self.store.active()[0]["session_id"], "thread-a")

    def test_update_name_preserves_session_identity_and_thread(self):
        self.store.add(
            "session-local",
            "Original",
            interview_thread_id="thread-interview",
        )

        self.assertTrue(self.store.update_name(
            "session-local",
            "  Backend Interview  ",
        ))

        session = self.store.get("session-local")
        self.assertEqual(session["name"], "Backend Interview")
        self.assertEqual(session["session_id"], "session-local")
        self.assertEqual(
            session["interview_thread_id"],
            "thread-interview",
        )

    def test_update_name_rejects_empty_name(self):
        self.store.add("session-local", "Original")

        with self.assertRaises(ValueError):
            self.store.update_name("session-local", "   ")

        self.assertEqual(self.store.get("session-local")["name"], "Original")

    def test_archived_session_is_hidden(self):
        self.store.add("thread-a", "A", "2026-08-08T10:00:00+09:00")

        self.assertTrue(
            self.store.mark_archived(
                "thread-a",
                "2026-08-08T12:00:00+09:00",
            )
        )

        self.assertEqual(self.store.active(), [])

    def test_old_session_without_settings_uses_safe_defaults(self):
        self.path.write_text(json.dumps({
            "version": 1,
            "sessions": [{
                "thread_id": "thread-old",
                "name": "Old",
                "created_at": "2026-08-08T10:00:00+09:00",
                "last_used_at": "2026-08-08T10:00:00+09:00",
                "archived_at": None,
            }],
        }), encoding="utf-8")

        session = SessionStore(self.path).active()[0]
        settings = session["settings"]

        self.assertEqual(settings, {
            "codex_model": "gpt-5.6-sol",
            "codex_reasoning_effort": "low",
            "codex_fast_mode": False,
            "stt_language": "en",
        })
        self.assertEqual(session["session_id"], "thread-old")
        self.assertNotIn("thread_id", session)
        self.assertNotIn("preparation_thread_id", session)
        self.assertIsNone(session["interview_thread_id"])
        persisted = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["version"], 2)
        self.assertEqual(persisted["sessions"][0]["session_id"], "thread-old")

    def test_new_session_persists_local_id_and_null_interview_thread(self):
        session = self.store.create("Session")

        persisted = json.loads(
            self.path.read_text(encoding="utf-8")
        )["sessions"][0]

        self.assertTrue(session["session_id"].startswith("session-"))
        self.assertEqual(persisted["session_id"], session["session_id"])
        self.assertNotIn("thread_id", persisted)
        self.assertNotIn("preparation_thread_id", persisted)
        self.assertIsNone(persisted["interview_thread_id"])

    def test_interview_thread_id_can_be_saved_and_read(self):
        self.store.add("session-local", "Session")

        self.assertTrue(self.store.set_interview_thread_id(
            "session-local",
            "thread-interview",
        ))

        session = SessionStore(self.path).active()[0]
        self.assertEqual(session["session_id"], "session-local")
        self.assertEqual(session["interview_thread_id"], "thread-interview")

    def test_get_reads_session_by_session_id(self):
        self.store.add("session-local", "Session")

        session = self.store.get("session-local")

        self.assertIsNotNone(session)
        self.assertEqual(session["session_id"], "session-local")
        self.assertIsNone(self.store.get("session-missing"))

    def test_preparation_thread_id_is_migrated_to_session_id(self):
        self.path.write_text(json.dumps({
            "version": 1,
            "sessions": [{
                "preparation_thread_id": "thread-preparation",
                "interview_thread_id": "thread-interview",
                "name": "Modern",
                "created_at": "2026-08-11T10:00:00+09:00",
                "last_used_at": "2026-08-11T10:00:00+09:00",
                "archived_at": None,
            }],
        }), encoding="utf-8")

        session = SessionStore(self.path).active()[0]

        self.assertEqual(session["session_id"], "thread-preparation")
        self.assertNotIn("thread_id", session)
        self.assertNotIn("preparation_thread_id", session)
        self.assertEqual(session["interview_thread_id"], "thread-interview")

    def test_legacy_context_directory_is_not_moved_during_migration(self):
        legacy_dir = Path(self.temporary_directory.name) / (
            "sessions/thread-preparation/contexts"
        )
        legacy_dir.mkdir(parents=True)
        context_path = legacy_dir / "company.md"
        context_path.write_text("legacy", encoding="utf-8")
        self.path.write_text(json.dumps({
            "version": 1,
            "sessions": [{
                "thread_id": "thread-preparation",
                "name": "Legacy",
            }],
        }), encoding="utf-8")

        session = SessionStore(self.path).active()[0]

        self.assertEqual(session["session_id"], "thread-preparation")
        self.assertEqual(context_path.read_text(encoding="utf-8"), "legacy")

    def test_codex_settings_and_fast_mode_persist_per_session(self):
        self.store.add("thread-a", "A", "2026-08-08T10:00:00+09:00")

        self.assertTrue(self.store.update_settings("thread-a", {
            "codex_model": "gpt-5.6-terra",
            "codex_reasoning_effort": "high",
            "codex_fast_mode": True,
        }))

        reloaded = SessionStore(self.path).active()[0]
        self.assertEqual(reloaded["settings"], {
            "codex_model": "gpt-5.6-terra",
            "codex_reasoning_effort": "high",
            "codex_fast_mode": True,
            "stt_language": "en",
        })

        self.assertTrue(self.store.update_settings("thread-a", {
            **reloaded["settings"],
            "codex_fast_mode": False,
        }))
        self.assertFalse(
            SessionStore(self.path).active()[0]["settings"]["codex_fast_mode"]
        )

    def test_japanese_stt_language_persists_per_session(self):
        self.store.add("thread-a", "A", "2026-08-08T10:00:00+09:00")

        settings = self.store.active()[0]["settings"]
        self.assertTrue(self.store.update_settings("thread-a", {
            **settings,
            "stt_language": "ja",
        }))

        reloaded = SessionStore(self.path).active()[0]["settings"]
        self.assertEqual(reloaded["stt_language"], "ja")

    def test_corrupt_store_blocks_writes_and_preserves_original_bytes(self):
        corrupt = b'{"sessions": [broken'
        self.path.write_bytes(corrupt)

        with self.assertRaises(SessionStoreError):
            self.store.create("Must not overwrite")

        self.assertEqual(self.path.read_bytes(), corrupt)

    def test_concurrent_field_updates_are_serialized_without_loss(self):
        self.store.add("session-local", "Original")
        first = SessionStore(self.path)
        second = SessionStore(self.path)
        first_loaded = threading.Event()
        release_first = threading.Event()
        original_load = first._load

        def delayed_load():
            sessions = original_load()
            first_loaded.set()
            self.assertTrue(release_first.wait(timeout=2))
            return sessions

        first._load = delayed_load
        outcomes = []
        name_thread = threading.Thread(
            target=lambda: outcomes.append(
                first.update_name("session-local", "Renamed")
            )
        )
        id_thread = threading.Thread(
            target=lambda: outcomes.append(
                second.set_interview_thread_id(
                    "session-local",
                    "thread-interview",
                )
            )
        )

        name_thread.start()
        self.assertTrue(first_loaded.wait(timeout=2))
        id_thread.start()
        time.sleep(0.05)
        self.assertTrue(id_thread.is_alive())
        release_first.set()
        name_thread.join(timeout=2)
        id_thread.join(timeout=2)

        session = self.store.get("session-local")
        self.assertEqual(outcomes, [True, True])
        self.assertEqual(session["name"], "Renamed")
        self.assertEqual(session["interview_thread_id"], "thread-interview")

    def test_existing_and_new_session_files_use_private_permissions(self):
        self.path.write_text('{"version": 2, "sessions": []}', encoding="utf-8")
        self.path.chmod(0o664)
        self.path.parent.chmod(0o775)

        store = SessionStore(self.path)
        store.create("Private")

        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        lock_path = self.path.with_suffix(".json.lock")
        self.assertEqual(lock_path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
