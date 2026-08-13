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


if __name__ == "__main__":
    unittest.main()
