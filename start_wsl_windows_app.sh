#!/usr/bin/env bash
set -euo pipefail

app_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

exec "$app_dir/.venv/bin/python" -m windows_port.app
