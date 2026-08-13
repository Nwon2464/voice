"""Windows-Python entry point for the minimal WASAPI bridge.

It intentionally lives at the repository root: when Windows Python executes a
script by path, that directory becomes the import root for ``windows_port``.
"""

import sys

from windows_port.bridge_helper import main


if __name__ == "__main__":
    raise SystemExit(main())
