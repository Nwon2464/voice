#!/usr/bin/env bash
set -euo pipefail

app_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
requirements_path="$(wslpath -w "$app_dir/requirements-windows-bridge.txt")"
local_app_data="$(
    cd /mnt/c
    cmd.exe /d /c echo %LOCALAPPDATA% \
        | tr -d '\r' \
        | tail -n 1
)"
bridge_directory="${local_app_data}\\InterviewAssistantBridge\\.venv"
bridge_python="$(wslpath -u "${bridge_directory}\\Scripts\\python.exe")"

py.exe -3 -m venv "$bridge_directory"
"$bridge_python" -m pip install --upgrade pip
"$bridge_python" -m pip install -r "$requirements_path"

printf 'Windows bridge environment is ready.\n'
