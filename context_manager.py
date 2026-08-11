"""Filesystem-backed Interview Context discovery and resolution."""

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path


_SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")
CONTEXT_STATUS_NOT_SYNCED = "NOT SYNCED"
CONTEXT_STATUS_CHANGED = "CHANGED"
CONTEXT_STATUS_SYNCED = "SYNCED"


@dataclass(frozen=True)
class ContextFile:
    """A context Markdown file available to an interview session."""

    scope: str
    name: str
    path: Path


@dataclass(frozen=True)
class ContextState:
    """Current content and per-thread sync state for an effective Context."""

    scope: str
    name: str
    path: Path
    status: str
    content_hash: str
    synced_hash: str | None


class ContextManager:
    """Locate global and per-session context Markdown files."""

    def __init__(self, config_dir):
        self.config_dir = Path(config_dir)
        self.global_context_dir = self.config_dir / "global_contexts"
        self.sessions_dir = self.config_dir / "sessions"
        self.global_context_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._secure_existing_tree()

    def session_context_dir(self, session_id):
        session_id = self._validate_session_id(session_id)
        return self.sessions_dir / session_id / "contexts"

    def ensure_session(self, session_id):
        context_dir = self.session_context_dir(session_id)
        context_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        context_dir.parent.chmod(0o700)
        context_dir.chmod(0o700)
        return context_dir

    def sync_metadata_path(self, session_id):
        session_id = self._validate_session_id(session_id)
        return self.sessions_dir / session_id / "context_sync.json"

    @staticmethod
    def context_filename(name):
        if not isinstance(name, str):
            raise ValueError("Context name must be text")
        normalized = unicodedata.normalize("NFKC", name).strip()
        if (
            not normalized
            or normalized in {".", ".."}
            or "\x00" in normalized
            or "/" in normalized
            or "\\" in normalized
            or Path(normalized).is_absolute()
        ):
            raise ValueError("Context name must not contain a filesystem path")
        if normalized.lower().endswith(".md"):
            normalized = normalized[:-3].strip()
        normalized = re.sub(r"[\s_]+", "-", normalized)
        normalized = re.sub(r"[^\w-]+", "-", normalized, flags=re.UNICODE)
        normalized = re.sub(r"-+", "-", normalized).strip("-")
        if not normalized or normalized in {".", ".."}:
            raise ValueError("Context name must contain filename characters")
        return f"{normalized.casefold()}.md"

    @staticmethod
    def context_logical_key(filename):
        stem = Path(filename).stem
        normalized = unicodedata.normalize("NFKC", stem).casefold().strip()
        return re.sub(r"[\s_-]+", "-", normalized).strip("-")

    def create_context(self, scope, session_id, name):
        filename = self.context_filename(name)
        if scope == "global":
            directory = self.global_context_dir
        elif scope == "session":
            directory = self.ensure_session(session_id)
        else:
            raise ValueError(f"unsupported Context scope: {scope!r}")
        directory.mkdir(parents=True, exist_ok=True)
        logical_key = self.context_logical_key(filename)
        for context in self._list_contexts(directory, scope):
            if self.context_logical_key(context.name) == logical_key:
                raise FileExistsError(
                    f"Context already exists in {scope} scope: {context.name}"
                )
        path = directory / filename
        with path.open("x", encoding="utf-8"):
            pass
        path.chmod(0o600)
        return ContextFile(scope=scope, name=filename, path=path)

    def list_global_contexts(self):
        return self._list_contexts(self.global_context_dir, "global")

    def list_session_contexts(self, session_id):
        context_dir = self.session_context_dir(session_id)
        return self._list_contexts(context_dir, "session")

    def resolve_effective_contexts(self, session_id):
        global_contexts = self.list_global_contexts()
        session_contexts = self.list_session_contexts(session_id)
        session_keys = {
            self.context_logical_key(context.name)
            for context in session_contexts
        }
        return [
            context
            for context in global_contexts
            if self.context_logical_key(context.name) not in session_keys
        ] + session_contexts

    def resolve_effective_context_states(self, session_id):
        sync_hashes = self._load_sync_hashes(session_id)
        states = []
        for context in self.resolve_effective_contexts(session_id):
            logical_key = self.context_logical_key(context.name)
            current_hash = self.context_content_hash(context)
            synced_hash = sync_hashes.get(logical_key)
            if synced_hash is None:
                status = CONTEXT_STATUS_NOT_SYNCED
            elif current_hash == synced_hash:
                status = CONTEXT_STATUS_SYNCED
            else:
                status = CONTEXT_STATUS_CHANGED
            states.append(ContextState(
                scope=context.scope,
                name=context.name,
                path=context.path,
                status=status,
                content_hash=current_hash,
                synced_hash=synced_hash,
            ))
        return states

    @staticmethod
    def context_content_hash(context):
        path = Path(context.path)
        digest = hashlib.sha256()
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(64 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def read_context_snapshot(context):
        content_bytes = Path(context.path).read_bytes()
        return (
            content_bytes.decode("utf-8"),
            hashlib.sha256(content_bytes).hexdigest(),
        )

    def record_successful_sync(self, session_id, context, content_hash=None):
        content_hash = content_hash or self.context_content_hash(context)
        content_hash = str(content_hash).casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", content_hash):
            raise ValueError("Context sync hash must be a SHA-256 hex digest")
        sync_hashes = self.read_sync_hashes(session_id)
        sync_hashes[self.context_logical_key(context.name)] = content_hash
        self.replace_sync_hashes(session_id, sync_hashes)
        return content_hash

    def read_sync_hashes(self, session_id):
        return dict(self._load_sync_hashes(session_id))

    def replace_sync_hashes(self, session_id, sync_hashes):
        normalized = {}
        for logical_key, content_hash in sync_hashes.items():
            content_hash = str(content_hash).casefold()
            if not re.fullmatch(r"[0-9a-f]{64}", content_hash):
                raise ValueError("Context sync hash must be a SHA-256 hex digest")
            normalized[str(logical_key)] = content_hash
        self._save_sync_hashes(session_id, normalized)

    def remove_sync_metadata(self, session_id):
        self.sync_metadata_path(session_id).unlink(missing_ok=True)

    def _load_sync_hashes(self, session_id):
        path = self.sync_metadata_path(session_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        sync_hashes = payload.get("sync_hashes", {})
        if not isinstance(sync_hashes, dict):
            return {}
        return {
            str(key): value.casefold()
            for key, value in sync_hashes.items()
            if isinstance(value, str)
            and re.fullmatch(r"[0-9a-fA-F]{64}", value)
        }

    def _save_sync_hashes(self, session_id, sync_hashes):
        self.ensure_session(session_id)
        path = self.sync_metadata_path(session_id)
        temporary_path = path.with_suffix(path.suffix + ".tmp")
        temporary_path.write_text(
            json.dumps(
                {"version": 1, "sync_hashes": sync_hashes},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ) + "\n",
            encoding="utf-8",
        )
        temporary_path.chmod(0o600)
        temporary_path.replace(path)
        path.chmod(0o600)

    @staticmethod
    def _list_contexts(directory, scope):
        if not directory.is_dir():
            return []
        contexts = []
        for path in sorted(directory.glob("*.md"), key=lambda path: path.name):
            if path.is_file():
                if not path.is_symlink():
                    path.chmod(0o600)
                contexts.append(
                    ContextFile(scope=scope, name=path.name, path=path)
                )
        return contexts

    def _secure_existing_tree(self):
        self.config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        for path in [self.config_dir, *self.config_dir.rglob("*")]:
            if path.is_symlink():
                continue
            path.chmod(0o700 if path.is_dir() else 0o600)

    @staticmethod
    def _validate_session_id(session_id):
        if not isinstance(session_id, str) or not _SESSION_ID_PATTERN.fullmatch(
            session_id
        ):
            raise ValueError(f"unsafe session id: {session_id!r}")
        return session_id
