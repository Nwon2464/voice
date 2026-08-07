import unittest

from codex_app_server import CodexAppServerClient, CodexAppServerError


class CodexAppServerClientTest(unittest.TestCase):
    def test_final_answer_is_preferred_over_commentary(self):
        messages = [
            {"phase": "commentary", "text": "Working..."},
            {"phase": "final_answer", "text": "Speakable answer."},
        ]
        self.assertEqual(
            CodexAppServerClient._choose_answer(messages, {}),
            "Speakable answer.",
        )

    def test_deltas_are_fallback_when_completed_item_is_missing(self):
        self.assertEqual(
            CodexAppServerClient._choose_answer([], {"a": "First ", "b": "answer"}),
            "First answer",
        )

    def test_unstarted_client_rejects_turn(self):
        client = CodexAppServerClient(
            model="test-model",
            effort="low",
            cwd=".",
            developer_instructions="test",
            codex_path="/bin/false",
        )
        with self.assertRaisesRegex(CodexAppServerError, "has not started"):
            client.run_turn("test")


if __name__ == "__main__":
    unittest.main()
