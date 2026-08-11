import tempfile
import unittest
import json
from pathlib import Path

from context_manager import (
    CONTEXT_STATUS_CHANGED,
    CONTEXT_STATUS_NOT_SYNCED,
    CONTEXT_STATUS_SYNCED,
    ContextManager,
)


class ContextManagerTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.temporary_directory.name)
        self.manager = ContextManager(self.config_dir)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _write_global(self, name, text="global"):
        path = self.manager.global_context_dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def _write_session(self, thread_id, name, text="session"):
        path = self.manager.ensure_session(thread_id) / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_initialization_creates_global_context_directory(self):
        self.assertEqual(
            self.manager.global_context_dir,
            self.config_dir / "global_contexts",
        )
        self.assertTrue(self.manager.global_context_dir.is_dir())

    def test_initialization_secures_existing_context_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory) / "interview-assistant"
            context_dir = config_dir / "sessions/session-old/contexts"
            context_dir.mkdir(parents=True)
            context_path = context_dir / "profile.md"
            context_path.write_text("private profile", encoding="utf-8")
            config_dir.chmod(0o775)
            context_dir.chmod(0o775)
            context_path.chmod(0o664)

            manager = ContextManager(config_dir)
            new_context = manager.create_context(
                "session",
                "session-new",
                "Company",
            )
            manager.record_successful_sync("session-new", new_context)

            self.assertEqual(config_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual(context_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual(context_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(new_context.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                manager.sync_metadata_path("session-new").stat().st_mode
                & 0o777,
                0o600,
            )

    def test_ensure_session_creates_context_directory(self):
        context_dir = self.manager.ensure_session("thread-123")

        self.assertEqual(
            context_dir,
            self.config_dir / "sessions" / "thread-123" / "contexts",
        )
        self.assertTrue(context_dir.is_dir())
        self.assertEqual(list(context_dir.iterdir()), [])

    def test_effective_contexts_include_global_context(self):
        path = self._write_global("profile.md")

        contexts = self.manager.resolve_effective_contexts("thread-123")

        self.assertEqual(
            [(context.scope, context.name, context.path) for context in contexts],
            [("global", "profile.md", path)],
        )

    def test_effective_contexts_include_session_context(self):
        path = self._write_session("thread-123", "company.md")

        contexts = self.manager.resolve_effective_contexts("thread-123")

        self.assertEqual(
            [(context.scope, context.name, context.path) for context in contexts],
            [("session", "company.md", path)],
        )

    def test_session_context_overrides_same_logical_key(self):
        self._write_global("answer_style.md")
        session_path = self._write_session(
            "thread-123",
            "Answer-Style.md",
        )

        contexts = self.manager.resolve_effective_contexts("thread-123")

        self.assertEqual(
            [(context.scope, context.name, context.path) for context in contexts],
            [("session", "Answer-Style.md", session_path)],
        )

    def test_different_global_and_session_contexts_are_both_effective(self):
        global_path = self._write_global("profile.md")
        session_path = self._write_session("thread-123", "company.md")

        contexts = self.manager.resolve_effective_contexts("thread-123")

        self.assertEqual(
            [(context.scope, context.name, context.path) for context in contexts],
            [
                ("global", "profile.md", global_path),
                ("session", "company.md", session_path),
            ],
        )

    def test_unsafe_thread_ids_are_rejected(self):
        for thread_id in (
            "",
            ".",
            "..",
            "../outside",
            "thread/../../outside",
            "/absolute",
            "thread\\outside",
        ):
            with self.subTest(thread_id=thread_id):
                with self.assertRaises(ValueError):
                    self.manager.ensure_session(thread_id)

    def test_context_names_become_safe_markdown_filenames(self):
        cases = {
            "Company": "company.md",
            "Answer Style": "answer-style.md",
            "Interview Focus": "interview-focus.md",
            "한국어 프로필": "한국어-프로필.md",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(
                    self.manager.context_filename(name),
                    expected,
                )

    def test_logical_key_normalizes_case_unicode_and_separators(self):
        filenames = (
            "Answer Style.md",
            "answer_style.md",
            "ANSWER-STYLE.md",
            "Ａｎｓｗｅｒ＿Ｓｔｙｌｅ.md",
        )

        self.assertEqual(
            {self.manager.context_logical_key(name) for name in filenames},
            {"answer-style"},
        )

    def test_empty_context_name_is_rejected(self):
        for name in ("", "   ", "---"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    self.manager.context_filename(name)

    def test_context_name_cannot_be_a_path(self):
        for name in (
            ".",
            "..",
            "../outside",
            "nested/context",
            "nested\\context",
            "/absolute",
        ):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    self.manager.context_filename(name)

    def test_create_session_context_makes_empty_markdown_file(self):
        context = self.manager.create_context(
            "session",
            "thread-123",
            "Company",
        )

        self.assertEqual(context.scope, "session")
        self.assertEqual(context.name, "company.md")
        self.assertEqual(
            context.path,
            self.config_dir / "sessions" / "thread-123" / "contexts"
            / "company.md",
        )
        self.assertEqual(context.path.read_text(encoding="utf-8"), "")

    def test_create_global_context_makes_empty_markdown_file(self):
        context = self.manager.create_context(
            "global",
            "thread-123",
            "Profile",
        )

        self.assertEqual(context.scope, "global")
        self.assertEqual(context.name, "profile.md")
        self.assertEqual(
            context.path,
            self.config_dir / "global_contexts" / "profile.md",
        )
        self.assertEqual(context.path.read_text(encoding="utf-8"), "")

    def test_duplicate_logical_key_in_same_scope_is_rejected(self):
        existing_path = self._write_session(
            "thread-123",
            "answer_style.md",
        )

        with self.assertRaises(FileExistsError):
            self.manager.create_context(
                "session",
                "thread-123",
                "Answer Style",
            )

        self.assertTrue(existing_path.is_file())
        self.assertFalse(existing_path.with_name("answer-style.md").exists())

    def test_same_logical_key_can_be_created_in_different_scopes(self):
        global_path = self._write_global("answer_style.md")
        session_context = self.manager.create_context(
            "session",
            "thread-123",
            "Answer Style",
        )

        self.assertTrue(global_path.is_file())
        self.assertTrue(session_context.path.is_file())
        self.assertEqual(session_context.name, "answer-style.md")

    def test_created_session_context_overrides_global_context(self):
        global_path = self._write_global("answer_style.md")
        session_context = self.manager.create_context(
            "session",
            "thread-123",
            "Answer Style",
        )

        contexts = self.manager.resolve_effective_contexts("thread-123")

        self.assertEqual(contexts, [session_context])
        self.assertEqual(session_context.name, "answer-style.md")
        self.assertTrue(global_path.is_file())

    def test_new_context_is_not_synced(self):
        context = self.manager.create_context(
            "session",
            "thread-123",
            "Company",
        )

        states = self.manager.resolve_effective_context_states("thread-123")

        self.assertEqual(len(states), 1)
        self.assertEqual(states[0].path, context.path)
        self.assertEqual(states[0].status, CONTEXT_STATUS_NOT_SYNCED)
        self.assertIsNone(states[0].synced_hash)

    def test_matching_recorded_hash_is_synced(self):
        context = self.manager.create_context(
            "session",
            "thread-123",
            "Company",
        )
        context.path.write_text("original", encoding="utf-8")
        content_hash = self.manager.context_content_hash(context)

        self.manager.record_successful_sync(
            "thread-123",
            context,
            content_hash,
        )
        state = self.manager.resolve_effective_context_states("thread-123")[0]

        self.assertEqual(state.status, CONTEXT_STATUS_SYNCED)
        self.assertEqual(state.content_hash, content_hash)
        self.assertEqual(state.synced_hash, content_hash)
        metadata = json.loads(
            self.manager.sync_metadata_path("thread-123").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(metadata, {
            "version": 1,
            "sync_hashes": {"company": content_hash},
        })

    def test_modified_context_is_changed_and_reverting_is_synced(self):
        context = self.manager.create_context(
            "session",
            "thread-123",
            "Company",
        )
        context.path.write_text("original", encoding="utf-8")
        self.manager.record_successful_sync("thread-123", context)

        context.path.write_text("modified", encoding="utf-8")
        changed = self.manager.resolve_effective_context_states("thread-123")[0]
        self.assertEqual(changed.status, CONTEXT_STATUS_CHANGED)

        context.path.write_text("original", encoding="utf-8")
        reverted = self.manager.resolve_effective_context_states("thread-123")[0]
        self.assertEqual(reverted.status, CONTEXT_STATUS_SYNCED)

    def test_global_context_sync_state_is_independent_per_thread(self):
        context = self.manager.create_context(
            "global",
            "thread-one",
            "Profile",
        )
        context.path.write_text("global profile", encoding="utf-8")
        self.manager.ensure_session("thread-one")
        self.manager.ensure_session("thread-two")
        self.manager.record_successful_sync("thread-one", context)

        thread_one = self.manager.resolve_effective_context_states("thread-one")
        thread_two = self.manager.resolve_effective_context_states("thread-two")

        self.assertEqual(thread_one[0].status, CONTEXT_STATUS_SYNCED)
        self.assertEqual(thread_two[0].status, CONTEXT_STATUS_NOT_SYNCED)
        self.assertFalse(
            self.manager.sync_metadata_path("thread-two").exists()
        )

    def test_session_override_uses_logical_key_sync_state(self):
        global_path = self._write_global(
            "answer_style.md",
            "global version",
        )
        global_context = self.manager.list_global_contexts()[0]
        self.manager.record_successful_sync("thread-123", global_context)
        session_path = self._write_session(
            "thread-123",
            "Answer-Style.md",
            "session version",
        )

        state = self.manager.resolve_effective_context_states("thread-123")[0]

        self.assertEqual(state.scope, "session")
        self.assertEqual(state.name, "Answer-Style.md")
        self.assertEqual(state.path, session_path)
        self.assertEqual(state.status, CONTEXT_STATUS_CHANGED)
        self.assertTrue(global_path.is_file())


if __name__ == "__main__":
    unittest.main()
