#!/usr/bin/env bash
set -euo pipefail

app_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
template_path="$app_dir/desktop/interview-assistant.desktop.in"
applications_dir="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
applications_file="$applications_dir/interview-assistant.desktop"

if [[ ! -f "$template_path" ]]; then
    printf 'Desktop template not found: %s\n' "$template_path" >&2
    exit 1
fi

if [[ ! -x "$app_dir/.venv/bin/python" ]]; then
    printf 'Python virtual environment not found: %s\n' \
        "$app_dir/.venv/bin/python" >&2
    exit 1
fi

escaped_app_dir="$(printf '%s' "$app_dir" | sed 's/[&|\\]/\\&/g')"
temporary_file="$(mktemp)"
trap 'rm -f -- "$temporary_file"' EXIT
sed "s|@APP_DIR@|$escaped_app_dir|g" "$template_path" >"$temporary_file"

install -Dm644 "$temporary_file" "$applications_file"
printf 'Applications launcher installed: %s\n' "$applications_file"

if command -v desktop-file-validate >/dev/null 2>&1; then
    desktop-file-validate "$applications_file"
fi

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$applications_dir" >/dev/null 2>&1 || true
fi

if command -v xdg-user-dir >/dev/null 2>&1; then
    desktop_dir="$(xdg-user-dir DESKTOP)"
    if [[ -n "$desktop_dir" && "$desktop_dir" != "$HOME" ]]; then
        desktop_file="$desktop_dir/Interview Assistant.desktop"
        install -Dm755 "$temporary_file" "$desktop_file"
        if command -v gio >/dev/null 2>&1; then
            gio set "$desktop_file" metadata::trusted true >/dev/null 2>&1 \
                || true
        fi
        printf 'Desktop launcher installed: %s\n' "$desktop_file"
    else
        printf 'Desktop directory is disabled; Applications launcher only.\n'
    fi
else
    printf 'xdg-user-dir is unavailable; Applications launcher only.\n'
fi
