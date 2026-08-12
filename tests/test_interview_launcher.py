import tempfile
import unittest
from pathlib import Path

import interview_launcher


class InterviewLauncherTest(unittest.TestCase):
    def test_mode_matrix_separates_codex_logging_and_diagnostics(self):
        expected = {
            interview_launcher.NORMAL_MODE: (True, False, False),
            interview_launcher.PERFORMANCE_MODE: (
                True, True, False
            ),
            interview_launcher.STT_DIAGNOSTIC_MODE: (
                False, True, True
            ),
        }

        self.assertEqual(set(interview_launcher.MODE_CONFIG), set(expected))
        for mode, values in expected.items():
            config = interview_launcher.MODE_CONFIG[mode]
            with self.subTest(mode=mode):
                self.assertEqual(
                    (
                        config["codex"],
                        config["logging"],
                        config["diagnostics"],
                    ),
                    values,
                )

    def test_normal_mode_disables_test_and_debug_environment(self):
        environment = interview_launcher.mode_environment(
            interview_launcher.NORMAL_MODE,
            base_environment={
                "INTERVIEW_DISABLE_CODEX": "1",
                "INTERVIEW_TEST_LOG": "1",
                "INTERVIEW_TEST_LABEL": "stale",
            },
        )

        self.assertEqual(environment["INTERVIEW_DISABLE_CODEX"], "0")
        self.assertEqual(environment["INTERVIEW_TEST_LOG"], "0")
        self.assertEqual(environment["INTERVIEW_STT_DIAGNOSTICS"], "0")
        self.assertEqual(environment["INTERVIEW_APP_MODE"], "normal")
        self.assertNotIn("INTERVIEW_TEST_LABEL", environment)

    def test_performance_mode_passes_trimmed_test_label(self):
        environment = interview_launcher.mode_environment(
            interview_launcher.PERFORMANCE_MODE,
            "  latency-test  ",
            base_environment={},
        )

        self.assertEqual(environment["INTERVIEW_DISABLE_CODEX"], "0")
        self.assertEqual(environment["INTERVIEW_TEST_LOG"], "1")
        self.assertEqual(environment["INTERVIEW_STT_DIAGNOSTICS"], "0")
        self.assertEqual(
            environment["INTERVIEW_TEST_LABEL"],
            "latency-test",
        )

    def test_stt_diagnostic_keeps_session_flow_without_codex(self):
        environment = interview_launcher.mode_environment(
            interview_launcher.DEBUG_MODE,
            "audio-debug",
            base_environment={},
        )

        self.assertEqual(environment["INTERVIEW_DISABLE_CODEX"], "1")
        self.assertEqual(environment["INTERVIEW_TEST_LOG"], "1")
        self.assertEqual(environment["INTERVIEW_STT_DIAGNOSTICS"], "1")
        self.assertEqual(environment["INTERVIEW_TEST_LABEL"], "audio-debug")

    def test_label_modes_reject_empty_label(self):
        for mode in (
            interview_launcher.PERFORMANCE_MODE,
            interview_launcher.DEBUG_MODE,
        ):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                interview_launcher.mode_environment(
                    mode,
                    "   ",
                    base_environment={},
                )

    def test_displayed_command_updates_and_shell_quotes_label(self):
        self.assertEqual(
            interview_launcher.displayed_command(
                interview_launcher.NORMAL_MODE
            ),
            "./start_interview_app.sh",
        )
        command = interview_launcher.displayed_command(
            interview_launcher.PERFORMANCE_MODE,
            "english practice",
        )
        self.assertIn("INTERVIEW_TEST_LOG=1", command)
        self.assertIn("INTERVIEW_TEST_LABEL='english practice'", command)
        self.assertIn(".venv/bin/python interview_app.py", command)

    def test_application_argv_uses_project_virtual_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(
                interview_launcher.application_argv(directory),
                [
                    str(Path(directory) / ".venv/bin/python"),
                    str(Path(directory) / "interview_app.py"),
                ],
            )

    def test_codex_cli_in_nvm_is_added_to_desktop_launch_path(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            codex = home / ".nvm/versions/node/v20.16.0/bin/codex"
            codex.parent.mkdir(parents=True)
            codex.write_text("#!/bin/sh\n", encoding="utf-8")
            codex.chmod(0o755)

            environment = interview_launcher.prepare_launch_environment(
                interview_launcher.NORMAL_MODE,
                base_environment={
                    "HOME": str(home),
                    "PATH": "/usr/bin:/bin",
                },
            )

            self.assertEqual(
                environment["PATH"].split(":", 1)[0],
                str(codex.parent),
            )

    def test_stt_diagnostic_does_not_require_codex_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = interview_launcher.prepare_launch_environment(
                interview_launcher.STT_DIAGNOSTIC_MODE,
                "audio-debug",
                base_environment={
                    "HOME": directory,
                    "PATH": "",
                },
            )

            self.assertEqual(environment["INTERVIEW_DISABLE_CODEX"], "1")

    def test_desktop_template_opens_launcher_without_terminal(self):
        template = (
            interview_launcher.APP_DIR
            / "desktop/interview-assistant.desktop.in"
        ).read_text(encoding="utf-8")

        self.assertIn("Name=Interview Assistant", template)
        self.assertIn("interview_launcher.py", template)
        self.assertIn("@APP_DIR@", template)
        self.assertIn("Terminal=false", template)


if __name__ == "__main__":
    unittest.main()
