import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


HELPER_PATH = (
    Path(__file__).resolve().parents[1]
    / "benchmark" / "benchmark_a" / "benchmark_a.py"
)
SPEC = importlib.util.spec_from_file_location("benchmark_a_helper", HELPER_PATH)
benchmark_a = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = benchmark_a
SPEC.loader.exec_module(benchmark_a)


class BenchmarkAPlanTest(unittest.TestCase):
    def test_yaml_supports_multiple_ordered_configurations_and_audio_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "audio"
            audio.mkdir()
            for name in ("warmup.wav", "q1.wav", "q2.wav"):
                (audio / name).write_bytes(b"wav")
            context = root / "profile.md"
            context.write_text("profile", encoding="utf-8")
            config_path = root / "benchmark.yaml"
            config_path.write_text(
                """benchmark_name: a-test
contexts: [profile.md]
audio:
  directory: audio
  warmup: warmup.wav
  questions: [q1.wav, q2.wav]
configurations:
  - name: sol-low
    model: gpt-5.6-sol
    reasoning: low
    runs: 2
    fast_mode: false
    stt_language: en
  - name: luna-medium
    model: gpt-5.6-luna
    reasoning: medium
    runs: 1
    fast_mode: false
    stt_language: en
""",
                encoding="utf-8",
            )

            plan = benchmark_a.parse_plan(config_path)

        self.assertEqual(plan.name, "a-test")
        self.assertEqual(
            [(item.name, item.model, item.reasoning, item.runs)
             for item in plan.configurations],
            [
                ("sol-low", "gpt-5.6-sol", "low", 2),
                ("luna-medium", "gpt-5.6-luna", "medium", 1),
            ],
        )
        self.assertEqual(plan.configurations[0].audio.warmup.name, "warmup.wav")
        self.assertEqual(
            [path.name for path in plan.configurations[0].audio.questions],
            ["q1.wav", "q2.wav"],
        )

    def test_completed_run_requires_only_existing_session_jsonl_events(self):
        config = benchmark_a.Configuration(
            name="sol-low",
            model="gpt-5.6-sol",
            reasoning="low",
            runs=1,
            fast_mode=False,
            stt_language="en",
            audio=benchmark_a.AudioPlan(Path("warmup.wav"), (Path("q1.wav"),)),
            contexts=(),
        )
        label = "benchmark_a-test_sol-low_r1"
        events = [
            {
                "event": "app_session_start",
                "test_label": label,
                "codex_model": "gpt-5.6-sol",
                "codex_reasoning_effort": "low",
                "codex_fast_mode": False,
                "language": "en",
            },
            {"event": "benchmark_wav_start", "wav": "warmup.wav"},
            {"event": "question", "question": 1, "commit_source": "f8", "cursor_complete": True, "audio_drop_samples": 0},
            {"event": "codex_response", "question": 1, "first_visible_seconds": 1.0},
            {"event": "benchmark_wav_start", "wav": "q1.wav"},
            {"event": "question", "question": 2, "commit_source": "f8", "cursor_complete": True, "audio_drop_samples": 0},
            {"event": "codex_response", "question": 2, "first_visible_seconds": 1.0},
            {"event": "app_session_end", "questions": 2, "codex_requests": 2, "cleanup_errors": []},
        ]

        self.assertTrue(benchmark_a.is_completed_run(events, label, config, 2))
        events.pop(1)
        self.assertFalse(benchmark_a.is_completed_run(events, label, config, 2))


if __name__ == "__main__":
    unittest.main()
