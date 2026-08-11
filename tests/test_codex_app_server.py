import signal
import unittest
from unittest.mock import Mock, call, patch

from codex_app_server import (
    CodexAppServerClient,
    CodexAppServerError,
    CodexAppServerTimeoutError,
    CodexAppServerTransportError,
)


class CodexAppServerClientTest(unittest.TestCase):
    def test_fast_feature_flag_is_selected_at_app_server_startup(self):
        off = CodexAppServerClient(
            model="test-model",
            effort="low",
            cwd=".",
            developer_instructions="test",
            codex_path="/usr/bin/codex",
            fast_mode=False,
        )
        on = CodexAppServerClient(
            model="test-model",
            effort="low",
            cwd=".",
            developer_instructions="test",
            codex_path="/usr/bin/codex",
            fast_mode=True,
        )

        self.assertIn(["--disable", "fast_mode"], [off._command()[i:i + 2]
                      for i in range(len(off._command()) - 1)])
        self.assertIn(["--enable", "fast_mode"], [on._command()[i:i + 2]
                      for i in range(len(on._command()) - 1)])

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

    def test_start_resumes_requested_thread(self):
        client = CodexAppServerClient(
            model="test-model",
            effort="low",
            cwd=".",
            developer_instructions="test",
            codex_path="/bin/false",
        )
        client.connect = Mock()
        client._request = Mock(
            return_value={"thread": {"id": "thread-existing"}}
        )

        result = client.start(thread_id="thread-existing")

        method, params = client._request.call_args.args[:2]
        self.assertEqual(method, "thread/resume")
        self.assertEqual(params["threadId"], "thread-existing")
        self.assertNotIn("serviceName", params)
        self.assertEqual(result["thread_id"], "thread-existing")
        self.assertEqual(result["thread"]["id"], "thread-existing")

    def test_start_can_create_persisted_thread(self):
        client = CodexAppServerClient(
            model="test-model",
            effort="low",
            cwd=".",
            developer_instructions="test",
            codex_path="/bin/false",
        )
        client.connect = Mock()
        client._request = Mock(return_value={"thread": {"id": "thread-new"}})

        client.start(ephemeral=False)

        method, params = client._request.call_args.args[:2]
        self.assertEqual(method, "thread/start")
        self.assertFalse(params["ephemeral"])

    def test_archive_thread_uses_app_server_archive(self):
        client = CodexAppServerClient(
            model="test-model",
            effort="low",
            cwd=".",
            developer_instructions="test",
            codex_path="/bin/false",
        )
        client.process = Mock()
        client.process.poll.return_value = None
        client._request = Mock(return_value={})

        client.archive_thread("thread-old")

        client._request.assert_called_once_with(
            "thread/archive",
            {"threadId": "thread-old"},
            timeout=15,
        )

    def test_read_thread_requests_turns_without_resuming(self):
        client = CodexAppServerClient(
            model="test-model",
            effort="low",
            cwd=".",
            developer_instructions="test",
            codex_path="/bin/false",
        )
        client.process = Mock()
        client.process.poll.return_value = None
        client._request = Mock(return_value={
            "thread": {"id": "thread-current", "turns": []},
        })

        thread = client.read_thread("thread-current", include_turns=True)

        self.assertEqual(thread["id"], "thread-current")
        client._request.assert_called_once_with(
            "thread/read",
            {"threadId": "thread-current", "includeTurns": True},
            timeout=30,
        )

    def test_inject_items_persists_items_without_turn(self):
        client = CodexAppServerClient(
            model="test-model",
            effort="low",
            cwd=".",
            developer_instructions="test",
            codex_path="/bin/false",
        )
        client.process = Mock()
        client.process.poll.return_value = None
        client.thread_id = "thread-new"
        client._request = Mock(return_value={})
        items = [{"type": "message", "role": "developer", "content": []}]

        client.inject_items(items)

        client._request.assert_called_once_with(
            "thread/inject_items",
            {"threadId": "thread-new", "items": items},
            timeout=15,
        )

    def test_list_models_requests_visible_catalog_and_filters_hidden_rows(self):
        client = CodexAppServerClient(
            model="test-model",
            effort="low",
            cwd=".",
            developer_instructions="test",
            codex_path="/bin/false",
        )
        client.process = Mock()
        client.process.poll.return_value = None
        client._request = Mock(return_value={
            "data": [
                {"model": "visible", "hidden": False},
                {"model": "hidden", "hidden": True},
            ],
            "nextCursor": None,
        })

        self.assertEqual(client.list_models(), [
            {"model": "visible", "hidden": False},
        ])
        client._request.assert_called_once_with(
            "model/list",
            {"includeHidden": False},
            timeout=15,
        )

    def test_conversation_turns_keeps_full_user_text_and_final_answers(self):
        thread = {
            "turns": [{
                "items": [
                    {
                        "type": "userMessage",
                        "content": [{
                            "type": "text",
                            "text": "RECENT CONTEXT:\nME: full transcript",
                        }],
                    },
                    {
                        "type": "agentMessage",
                        "phase": "commentary",
                        "text": "Thinking",
                    },
                    {
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "Full answer",
                    },
                ]
            }]
        }

        self.assertEqual(
            CodexAppServerClient.conversation_turns(thread),
            [[
                {"role": "user", "text": "RECENT CONTEXT:\nME: full transcript"},
                {"role": "assistant", "text": "Full answer"},
            ]],
        )

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
        client._poll_notification = Mock(side_effect=lambda _timeout: next(notifications))
        streamed = []

        result = client.run_turn(
            "test",
            on_delta=lambda delta, _elapsed: streamed.append(delta),
        )

        self.assertEqual(streamed, ["Speakable answer."])
        self.assertEqual(result["text"], "Speakable answer.")
        self.assertEqual(result["stream_delta_count"], 1)
        self.assertIsNotNone(result["first_visible_seconds"])

    def test_interactive_turn_handles_command_approval(self):
        client = CodexAppServerClient(
            model="test-model",
            effort="low",
            cwd="/workspace",
            developer_instructions="test",
            codex_path="/bin/false",
        )
        client.process = Mock()
        client.process.poll.return_value = None
        client.thread_id = "thread-1"
        client._request = Mock(return_value={"turn": {"id": "turn-1"}})
        client._send = Mock()
        notifications = iter([
            {
                "id": 99,
                "method": "item/commandExecution/requestApproval",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "command": ["touch", "example"],
                },
            },
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "item": {
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "Done",
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
        client._poll_notification = Mock(side_effect=lambda _timeout: next(notifications))

        client.run_turn(
            "edit",
            interactive=True,
            on_approval=lambda _method, _params: "accept",
        )

        turn_params = client._request.call_args.args[1]
        self.assertEqual(turn_params["approvalPolicy"], "on-request")
        self.assertEqual(turn_params["sandboxPolicy"]["type"], "workspaceWrite")
        client._send.assert_called_once_with({
            "id": 99,
            "result": {"decision": "accept"},
        })

    def test_turn_interrupt_is_sent_from_active_loop(self):
        client = CodexAppServerClient(
            model="test-model",
            effort="low",
            cwd="/workspace",
            developer_instructions="test",
            codex_path="/bin/false",
        )
        client.process = Mock()
        client.process.poll.return_value = None
        client.thread_id = "thread-1"
        client._request = Mock(side_effect=[
            {"turn": {"id": "turn-1"}},
            {},
        ])
        client._poll_notification = Mock(return_value={
            "method": "turn/completed",
            "params": {
                "threadId": "thread-1",
                "turn": {"id": "turn-1", "status": "interrupted"},
            },
        })
        client.request_interrupt()

        with self.assertRaisesRegex(CodexAppServerError, "interrupted"):
            client.run_turn("cancel me")

        self.assertEqual(client._request.call_args_list[1].args[:2], (
            "turn/interrupt",
            {"threadId": "thread-1", "turnId": "turn-1"},
        ))

    def test_clear_interrupt_request_discards_stale_signal(self):
        client = CodexAppServerClient(
            model="test-model",
            effort="low",
            cwd="/workspace",
            developer_instructions="test",
            codex_path="/bin/false",
        )
        client.request_interrupt()

        client.clear_interrupt_request()

        self.assertFalse(client._interrupt_requested.is_set())

    def test_transport_eof_is_recoverable(self):
        client = CodexAppServerClient(
            model="test-model",
            effort="low",
            cwd="/workspace",
            developer_instructions="test",
            codex_path="/bin/false",
        )
        client.process = Mock()
        client.process.poll.return_value = -9

        with self.assertRaises(CodexAppServerTransportError):
            client._check_transport_message({"_transport_eof": True})

    def test_raw_message_timeout_is_recoverable(self):
        client = CodexAppServerClient(
            model="test-model",
            effort="low",
            cwd="/workspace",
            developer_instructions="test",
            codex_path="/bin/false",
        )

        with self.assertRaises(CodexAppServerTimeoutError):
            client._next_raw_message(0.001)

    def test_stop_terminates_the_whole_app_server_process_group(self):
        client = CodexAppServerClient(
            model="test-model",
            effort="low",
            cwd="/workspace",
            developer_instructions="test",
            codex_path="/bin/false",
        )
        client.process = Mock()
        client.process.poll.side_effect = [None, -15]
        client._process_group_id = 4321

        with patch("codex_app_server.os.killpg") as killpg:
            client.stop()

        self.assertEqual(killpg.call_args_list, [
            call(4321, signal.SIGTERM),
            call(4321, signal.SIGKILL),
        ])


if __name__ == "__main__":
    unittest.main()
