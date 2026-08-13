#!/usr/bin/env bash
# Run the existing GTK launcher in WSLg, using the already-installed native
# Windows bridge instead of PulseAudio/GNOME. The launcher owns the mode
# decision (including Codex), just as it does for the Linux backend.
set -euo pipefail

app_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

export INTERVIEW_AUDIO_BACKEND=windows_bridge

exec "$app_dir/.venv/bin/python" "$app_dir/interview_launcher.py"
