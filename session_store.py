"""Local registry for Codex threads created by Interview Assistant."""

import json
from datetime import datetime
from pathlib import Path


DEFAULT_CODEX_MODEL = "gpt-5.6-sol"
DEFAULT_CODEX_REASONING_EFFORT = "low"
DEFAULT_STT_LANGUAGE = "en"
SUPPORTED_STT_LANGUAGES = {"en", "ja"}


def normalize_codex_settings(settings=None):
    """Return supported per-session settings with safe legacy defaults."""
    settings = settings if isinstance(settings, dict) else {}
    stt_language = settings.get("stt_language")
    if stt_language not in SUPPORTED_STT_LANGUAGES:
        stt_language = DEFAULT_STT_LANGUAGE
    return {
        "codex_model": settings.get("codex_model") or DEFAULT_CODEX_MODEL,
        "codex_reasoning_effort": (
            settings.get("codex_reasoning_effort")
            or DEFAULT_CODEX_REASONING_EFFORT
        ),
        "codex_fast_mode": settings.get("codex_fast_mode") is True,
        "stt_language": stt_language,
    }


class SessionStore:
    """Persist the app-owned thread ids shown by the session chooser."""

    def __init__(self, path):
        self.path = Path(path)

    def active(self):
        sessions = [
            self._with_settings(session)
            for session in self._load()
            if not session.get("archived_at")
        ]
        return sorted(
            sessions,
            key=lambda session: session.get("last_used_at", ""),
            reverse=True,
        )

    def add(self, thread_id, name, timestamp=None, settings=None):
        now = timestamp or self._now()
        sessions = self._load()
        sessions = [
            session for session in sessions if session.get("thread_id") != thread_id
        ]
        sessions.append({
            "thread_id": thread_id,
            "name": name,
            "created_at": now,
            "last_used_at": now,
            "archived_at": None,
            "settings": normalize_codex_settings(settings),
        })
        self._save(sessions)

    def update_settings(self, thread_id, settings):
        return self._update(
            thread_id,
            "settings",
            normalize_codex_settings(settings),
        )

    def mark_used(self, thread_id, timestamp=None):
        return self._update(
            thread_id,
            "last_used_at",
            timestamp or self._now(),
        )

    def mark_archived(self, thread_id, timestamp=None):
        return self._update(
            thread_id,
            "archived_at",
            timestamp or self._now(),
        )

    def _update(self, thread_id, field, value):
        sessions = self._load()
        found = False
        for session in sessions:
            if session.get("thread_id") == thread_id:
                session[field] = value
                found = True
                break
        if found:
            self._save(sessions)
        return found

    def _load(self):
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        sessions = payload.get("sessions", []) if isinstance(payload, dict) else []
        return [session for session in sessions if isinstance(session, dict)]

    @staticmethod
    def _with_settings(session):
        normalized = dict(session)
        normalized["settings"] = normalize_codex_settings(
            normalized.get("settings")
        )
        return normalized

    def _save(self, sessions):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary_path.write_text(
            json.dumps(
                {"version": 1, "sessions": sessions},
                ensure_ascii=False,
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(self.path)

    @staticmethod
    def _now():
        return datetime.now().astimezone().isoformat(timespec="seconds")
