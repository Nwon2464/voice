#!/usr/bin/env bash
# Create the native Windows virtual environment used only by the WASAPI helper.
set -euo pipefail

if ! command -v cmd.exe >/dev/null || ! command -v py.exe >/dev/null; then
    echo "Run this script from WSL with Windows interop enabled." >&2
    exit 1
fi
if [[ ! -d /mnt/c ]]; then
    echo "Windows C: drive is not mounted at /mnt/c; enable WSL automount." >&2
    exit 1
fi

app_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
requirements_path="$(wslpath -w "$app_dir/requirements-windows-bridge.txt")"
# Windows processes launched while WSL is in its UNC repository path print
# "UNC paths are not supported" before running.  /mnt/c maps to C:\\ instead.
windows_cwd=/mnt/c
local_app_data="$(
    cd "$windows_cwd"
    { cmd.exe /d /c echo %LOCALAPPDATA% || true; } \
        | tr -d '\r' \
        | awk 'NF {value=$0} END {print value}'
)"
if [[ -z "$local_app_data" ]]; then
    echo "Could not resolve Windows %LOCALAPPDATA%." >&2
    exit 1
fi

bridge_venv="${local_app_data}\\InterviewAssistantBridge\\.venv"
bridge_python="$(wslpath -u "${bridge_venv}\\Scripts\\python.exe")"

(
    cd "$windows_cwd"
    py.exe -3 -m venv "$bridge_venv"
    "$bridge_python" -m pip install --upgrade pip
    # A missing wheel must fail clearly instead of attempting a local C build.
    "$bridge_python" -m pip install --only-binary=:all: -r "$requirements_path"
)

printf 'Windows WASAPI bridge environment is ready: %s\n' "$bridge_python"
