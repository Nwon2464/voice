#!/usr/bin/env python3
"""Read-only summary of one session's current Codex Interview Thread."""

import argparse
import json
import os
import sys
from pathlib import Path

from codex_app_server import CodexAppServerClient
from session_store import normalize_codex_settings


APP_DIR = Path(__file__).resolve().parent
DEFAULT_SESSIONS_PATH = Path(
    os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
) / "interview-assistant" / "sessions.json"
TEXT_PREVIEW_LIMIT = 1200


def stored_session_id(session):
    """Read current and legacy session identities without migrating the file."""
    return (
        session.get("session_id")
        or session.get("preparation_thread_id")
        or session.get("thread_id")
    )


def load_session(sessions_path, session_id):
    payload = json.loads(Path(sessions_path).read_text(encoding="utf-8"))
    sessions = payload.get("sessions", []) if isinstance(payload, dict) else []
    for session in sessions:
        if isinstance(session, dict) and stored_session_id(session) == session_id:
            return session
    raise ValueError(f"session_id not found: {session_id}")


def read_interview_thread(session, client_factory=CodexAppServerClient):
    thread_id = session.get("interview_thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        raise ValueError("the selected session has no interview_thread_id")
    settings = normalize_codex_settings(session.get("settings"))
    client = client_factory(
        model=settings["codex_model"],
        effort=settings["codex_reasoning_effort"],
        fast_mode=settings["codex_fast_mode"],
        cwd=APP_DIR,
        developer_instructions="",
        timeout_seconds=30,
    )
    try:
        client.connect()
        thread = client.read_thread(thread_id, include_turns=True)
    finally:
        client.stop()
    return thread_id, thread


def item_text_parts(item):
    parts = []
    direct_text = item.get("text")
    if isinstance(direct_text, str) and direct_text.strip():
        parts.append(("text", direct_text.strip()))
    content = item.get("content")
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                parts.append((part.get("type") or "content", text.strip()))
    return parts


def item_role(item, text_parts):
    role = item.get("role")
    if isinstance(role, str) and role:
        return role
    combined_text = "\n".join(text for _part_type, text in text_parts)
    if "INTERVIEW CONTEXT SNAPSHOT" in combined_text:
        return "developer/context"
    return {
        "developerMessage": "developer/context",
        "systemMessage": "system",
        "userMessage": "user/interviewer",
        "agentMessage": "assistant/Codex",
    }.get(item.get("type"), "unknown")


def text_preview(text, limit=TEXT_PREVIEW_LIMIT):
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n… [truncated; total characters: {len(text)}]"


def print_item(item, prefix="  "):
    text_parts = item_text_parts(item)
    role = item_role(item, text_parts)
    fields = ", ".join(sorted(item))
    details = [
        f"role={role}",
        f"type={item.get('type', '<missing>')}",
    ]
    if item.get("phase"):
        details.append(f"phase={item['phase']}")
    print(f"{prefix}- {' | '.join(details)}")
    print(f"{prefix}  fields={fields}")
    if not text_parts:
        print(f"{prefix}  text=<none>")
        return
    for part_type, text in text_parts:
        print(f"{prefix}  text[{part_type}]:")
        for line in text_preview(text).splitlines() or [""]:
            print(f"{prefix}    {line}")


def print_thread_summary(session_id, thread_id, thread):
    turns = thread.get("turns")
    turns = turns if isinstance(turns, list) else []
    print(f"session_id={session_id}")
    print(f"interview_thread_id={thread_id}")
    print(f"thread.id={thread.get('id', '<missing>')}")
    print(f"turn_count={len(turns)}")

    thread_items = thread.get("items")
    if isinstance(thread_items, list) and thread_items:
        print("\nTHREAD-LEVEL ITEMS")
        for item in thread_items:
            if isinstance(item, dict):
                print_item(item)

    for turn_number, turn in enumerate(turns, start=1):
        if not isinstance(turn, dict):
            continue
        print(
            f"\nTURN {turn_number} "
            f"id={turn.get('id', '<missing>')} "
            f"status={turn.get('status', '<missing>')}"
        )
        items = turn.get("items")
        items = items if isinstance(items, list) else []
        if not items:
            print("  <no items>")
        for item in items:
            if isinstance(item, dict):
                print_item(item)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Inspect the current Interview Thread without modifying it."
    )
    parser.add_argument("session_id", help="Local Interview Assistant session ID")
    parser.add_argument(
        "--sessions-file",
        type=Path,
        default=DEFAULT_SESSIONS_PATH,
        help=f"sessions.json path (default: {DEFAULT_SESSIONS_PATH})",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        session = load_session(args.sessions_file, args.session_id)
        thread_id, thread = read_interview_thread(session)
        print_thread_summary(args.session_id, thread_id, thread)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
