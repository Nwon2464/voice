import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
