#!/usr/bin/env bash
# Run the existing GTK Session → Preparation → Interview flow in WSLg, using
# the already-installed native Windows bridge instead of PulseAudio/GNOME.
set -euo pipefail

app_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

export INTERVIEW_AUDIO_BACKEND=windows_bridge
export INTERVIEW_APP_MODE=stt_diagnostic
export INTERVIEW_DISABLE_CODEX=1
export INTERVIEW_TEST_LOG=1
export INTERVIEW_STT_DIAGNOSTICS=1
export INTERVIEW_TEST_LABEL="windows-bridge"

exec "$app_dir/.venv/bin/python" "$app_dir/interview_app.py"
