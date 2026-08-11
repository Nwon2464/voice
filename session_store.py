"""Local registry for Interview Assistant sessions."""

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from uuid import uuid4


DEFAULT_CODEX_MODEL = "gpt-5.6-sol"
DEFAULT_CODEX_REASONING_EFFORT = "low"
DEFAULT_STT_LANGUAGE = "en"
SUPPORTED_STT_LANGUAGES = {"en", "ja"}


class SessionStoreError(RuntimeError):
    """Raised when persisted session data cannot be read safely."""


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
    """Persist app-owned session ids and their current Interview thread."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        if self.path.exists() and not self.path.is_symlink():
            self.path.chmod(0o600)
        self._migrate_legacy_sessions()

    def active(self):
        sessions = [
            self._with_defaults(session)
            for session in self._load()
            if not session.get("archived_at")
        ]
        return sorted(
            sessions,
            key=lambda session: session.get("last_used_at", ""),
            reverse=True,
        )

    def get(self, session_id):
        for session in self._load():
            if self._session_id(session) == session_id:
                return self._with_defaults(session)
        return None

    def add(
        self,
        session_id,
        name,
        timestamp=None,
        settings=None,
        interview_thread_id=None,
    ):
        now = timestamp or self._now()
        with self._exclusive_lock():
            sessions = self._load()
            sessions = [
                session for session in sessions
                if self._session_id(session) != session_id
            ]
            sessions.append({
                "session_id": session_id,
                "interview_thread_id": interview_thread_id,
                "name": name,
                "created_at": now,
                "last_used_at": now,
                "archived_at": None,
                "settings": normalize_codex_settings(settings),
            })
            self._save(sessions)

    def create(self, name, timestamp=None, settings=None):
        """Create a local session without provisioning a Codex thread."""
        session_id = f"session-{uuid4()}"
        self.add(session_id, name, timestamp, settings)
        return self.get(session_id)

    def set_interview_thread_id(
        self,
        session_id,
        interview_thread_id,
    ):
        if interview_thread_id is not None and not (
            isinstance(interview_thread_id, str) and interview_thread_id
        ):
            raise ValueError("interview_thread_id must be non-empty text or None")
        return self._update(
            session_id,
            "interview_thread_id",
            interview_thread_id,
        )

    def update_settings(self, session_id, settings):
        return self._update(
            session_id,
            "settings",
            normalize_codex_settings(settings),
        )

    def update_name(self, session_id, name):
        if not isinstance(name, str) or not name.strip():
            raise ValueError("session name must not be empty")
        return self._update(session_id, "name", name.strip())

    def mark_used(self, session_id, timestamp=None):
        return self._update(
            session_id,
            "last_used_at",
            timestamp or self._now(),
        )

    def mark_archived(self, session_id, timestamp=None):
        return self._update(
            session_id,
            "archived_at",
            timestamp or self._now(),
        )

    def _update(self, session_id, field, value):
        with self._exclusive_lock():
            sessions = self._load()
            found = False
            for session in sessions:
                if self._session_id(session) == session_id:
                    session[field] = value
                    found = True
                    break
            if found:
                self._save(sessions)
            return found

    def _load(self):
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as error:
            raise SessionStoreError(
                f"Cannot safely read session store {self.path}: {error}"
            ) from error
        if not isinstance(payload, dict) or not isinstance(
            payload.get("sessions", []), list
        ):
            raise SessionStoreError(
                f"Invalid session store structure: {self.path}"
            )
        sessions = payload.get("sessions", [])
        if not all(isinstance(session, dict) for session in sessions):
            raise SessionStoreError(
                f"Invalid session entry in store: {self.path}"
            )
        return sessions

    @classmethod
    def _with_defaults(cls, session):
        normalized = cls._with_thread_fields(session)
        normalized["settings"] = normalize_codex_settings(
            normalized.get("settings")
        )
        return normalized

    @classmethod
    def _with_thread_fields(cls, session):
        normalized = dict(session)
        session_id = cls._session_id(normalized)
        normalized["session_id"] = session_id
        normalized.pop("thread_id", None)
        normalized.pop("preparation_thread_id", None)
        interview_thread_id = normalized.get("interview_thread_id")
        normalized["interview_thread_id"] = (
            interview_thread_id
            if isinstance(interview_thread_id, str) and interview_thread_id
            else None
        )
        return normalized

    @staticmethod
    def _session_id(session):
        return (
            session.get("session_id")
            or session.get("preparation_thread_id")
            or session.get("thread_id")
        )

    def _save(self, sessions):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        sessions = [self._with_thread_fields(session) for session in sessions]
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=self.path.parent,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                file.write(json.dumps(
                    {"version": 2, "sessions": sessions},
                    ensure_ascii=False,
                    indent=2,
                ) + "\n")
                file.flush()
                os.fsync(file.fileno())
            temporary_path.replace(self.path)
            self.path.chmod(0o600)
        finally:
            temporary_path.unlink(missing_ok=True)

    def _migrate_legacy_sessions(self):
        with self._exclusive_lock():
            sessions = self._load()
            if not sessions:
                return
            if any(
                "session_id" not in session
                or "thread_id" in session
                or "preparation_thread_id" in session
                for session in sessions
            ):
                self._save(sessions)

    @contextmanager
    def _exclusive_lock(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            lock_path.chmod(0o600)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _now():
        return datetime.now().astimezone().isoformat(timespec="seconds")
