import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WindowsBridgeSetupTests(unittest.TestCase):
    def test_bridge_dependencies_are_python_314_wheel_compatible(self):
        requirements = (ROOT / "requirements-windows-bridge.txt").read_text()
        self.assertIn("numpy==2.4.2", requirements)
        self.assertIn("SoundCard==0.4.6", requirements)

    def test_setup_requires_wheels_and_uses_a_windows_cwd(self):
        setup = (ROOT / "setup_windows_bridge.sh").read_text()
        self.assertIn("windows_cwd=/mnt/c", setup)
        self.assertIn("cd \"$windows_cwd\"", setup)
        self.assertIn("--only-binary=:all:", setup)

    def test_wsl_app_wrapper_uses_launcher_and_leaves_mode_to_it(self):
        wrapper = (ROOT / "start_wsl_windows_app.sh").read_text()

        self.assertIn("INTERVIEW_AUDIO_BACKEND=windows_bridge", wrapper)
        self.assertIn('"$app_dir/interview_launcher.py"', wrapper)
        self.assertNotIn("INTERVIEW_APP_MODE=", wrapper)
        self.assertNotIn("INTERVIEW_DISABLE_CODEX=", wrapper)
        self.assertNotIn("INTERVIEW_TEST_LOG=", wrapper)
        self.assertNotIn("INTERVIEW_STT_DIAGNOSTICS=", wrapper)


if __name__ == "__main__":
    unittest.main()
