"""Windows-Python entry point for the WSL audio and hotkey bridge."""

import sys

from windows_port.bridge_helper import main


if __name__ == "__main__":
    sys.exit(main())
