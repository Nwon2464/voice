import unittest
from unittest.mock import Mock

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

    def test_streams_final_answer_but_hides_commentary(self):
        client = CodexAppServerClient(
            model="test-model",
            effort="low",
            cwd=".",
            developer_instructions="test",
            codex_path="/bin/false",
        )
        client.process = Mock()
        client.process.poll.return_value = None
        client.thread_id = "thread-1"
        client._request = Mock(return_value={"turn": {"id": "turn-1"}})
        notifications = iter([
            {
                "method": "item/started",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "item": {
                        "id": "commentary-1",
                        "type": "agentMessage",
                        "phase": "commentary",
                    },
                },
            },
            {
                "method": "item/agentMessage/delta",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "itemId": "commentary-1",
                    "delta": "Working...",
                },
            },
            {
                "method": "item/started",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "item": {
                        "id": "answer-1",
                        "type": "agentMessage",
                        "phase": "final_answer",
                    },
                },
            },
            {
                "method": "item/agentMessage/delta",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "itemId": "answer-1",
                    "delta": "Speakable answer.",
                },
            },
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "item": {
                        "id": "answer-1",
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "Speakable answer.",
                    },
                },
            },
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-1",
                    "turn": {"id": "turn-1", "status": "completed"},
                },
            },
        ])
        client._next_notification = Mock(side_effect=lambda _timeout: next(notifications))
        streamed = []

        result = client.run_turn(
            "test",
            on_delta=lambda delta, _elapsed: streamed.append(delta),
        )

        self.assertEqual(streamed, ["Speakable answer."])
        self.assertEqual(result["text"], "Speakable answer.")
        self.assertEqual(result["stream_delta_count"], 1)
        self.assertIsNotNone(result["first_visible_seconds"])


if __name__ == "__main__":
    unittest.main()
