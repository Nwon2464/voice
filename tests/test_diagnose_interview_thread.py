import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import diagnose_interview_thread as diagnostic


class FakeClient:
    instance = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.events = []
        self.__class__.instance = self

    def connect(self):
        self.events.append(("connect", None))

    def read_thread(self, thread_id, include_turns=True):
        self.events.append(("thread/read", thread_id, include_turns))
        return {"id": thread_id, "turns": []}

    def stop(self):
        self.events.append(("stop", None))


class InterviewThreadDiagnosticTest(unittest.TestCase):
    def test_load_session_reads_current_and_legacy_ids_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions.json"
            path.write_text(json.dumps({
                "sessions": [
                    {"session_id": "session-current"},
                    {"preparation_thread_id": "session-legacy"},
                ],
            }), encoding="utf-8")
            before = path.read_bytes()

            current = diagnostic.load_session(path, "session-current")
            legacy = diagnostic.load_session(path, "session-legacy")

            self.assertEqual(current["session_id"], "session-current")
            self.assertEqual(
                legacy["preparation_thread_id"],
                "session-legacy",
            )
            self.assertEqual(path.read_bytes(), before)

    def test_read_uses_only_thread_read_with_turns(self):
        session = {
            "interview_thread_id": "thread-interview",
            "settings": {
                "codex_model": "gpt-test",
                "codex_reasoning_effort": "low",
                "codex_fast_mode": False,
            },
        }

        thread_id, thread = diagnostic.read_interview_thread(
            session,
            client_factory=FakeClient,
        )

        self.assertEqual(thread_id, "thread-interview")
        self.assertEqual(thread["id"], "thread-interview")
        self.assertEqual(FakeClient.instance.events, [
            ("connect", None),
            ("thread/read", "thread-interview", True),
            ("stop", None),
        ])

    def test_summary_shows_roles_types_and_text_without_raw_json(self):
        thread = {
            "id": "thread-interview",
            "turns": [{
                "id": "turn-1",
                "status": "completed",
                "items": [
                    {
                        "type": "userMessage",
                        "content": [{
                            "type": "input_text",
                            "text": "INTERVIEW CONTEXT SNAPSHOT\nProfile",
                        }],
                    },
                    {
                        "type": "userMessage",
                        "content": [{
                            "type": "input_text",
                            "text": "CURRENT INTERVIEWER QUESTION:\nWhy us?",
                        }],
                    },
                    {
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "Because your mission fits my experience.",
                    },
                ],
            }],
        }
        output = io.StringIO()

        with redirect_stdout(output):
            diagnostic.print_thread_summary(
                "session-current",
                "thread-interview",
                thread,
            )

        text = output.getvalue()
        self.assertIn("TURN 1 id=turn-1 status=completed", text)
        self.assertIn("role=developer/context | type=userMessage", text)
        self.assertIn("role=user/interviewer | type=userMessage", text)
        self.assertIn("role=assistant/Codex | type=agentMessage", text)
        self.assertIn("Because your mission fits my experience.", text)
        self.assertNotIn("{'id':", text)


if __name__ == "__main__":
    unittest.main()
