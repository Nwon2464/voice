import tempfile
import unittest
import json
from pathlib import Path

from session_store import SessionStore


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
            [session["thread_id"] for session in self.store.active()],
            ["thread-new", "thread-old"],
        )

    def test_mark_used_moves_session_to_top(self):
        self.store.add("thread-a", "A", "2026-08-08T10:00:00+09:00")
        self.store.add("thread-b", "B", "2026-08-08T11:00:00+09:00")

        self.store.mark_used("thread-a", "2026-08-08T12:00:00+09:00")

        self.assertEqual(self.store.active()[0]["thread_id"], "thread-a")

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

        settings = self.store.active()[0]["settings"]

        self.assertEqual(settings, {
            "codex_model": "gpt-5.6-sol",
            "codex_reasoning_effort": "low",
            "codex_fast_mode": False,
            "stt_language": "en",
        })

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


if __name__ == "__main__":
    unittest.main()
