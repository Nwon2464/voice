"""Create an isolated persistent Codex thread for a live interview."""

from dataclasses import dataclass
from pathlib import Path

from session_store import normalize_codex_settings


@dataclass(frozen=True)
class SnapshotContext:
    scope: str
    name: str
    filename: str
    path: Path
    content: str
    content_hash: str
    logical_key: str


class InterviewThreadBackend:
    """Provision a live thread from one authoritative Context snapshot."""

    def __init__(self, session_store, context_manager, client_factory):
        self.session_store = session_store
        self.context_manager = context_manager
        self.client_factory = client_factory

    def create(self, session):
        session_id = session.get("session_id")
        if not session_id:
            raise ValueError("session has no session_id")
        old_interview_thread_id = session.get("interview_thread_id")
        settings = normalize_codex_settings(session.get("settings"))
        contexts = self._read_effective_snapshot(session_id)
        snapshot_text = self._format_snapshot(contexts)
        new_hashes = {
            context.logical_key: context.content_hash
            for context in contexts
        }

        metadata_path = self.context_manager.sync_metadata_path(
            session_id
        )
        metadata_existed = metadata_path.exists()
        old_sync_hashes = self.context_manager.read_sync_hashes(
            session_id
        )
        client = self.client_factory(settings)
        metadata_changed = False
        local_state_changed = False
        try:
            result = client.start(ephemeral=False)
            new_interview_thread_id = result.get("thread_id")
            if not new_interview_thread_id:
                raise RuntimeError("Codex returned no interview thread id")
            client.inject_items([{
                "type": "message",
                "role": "developer",
                "content": [{
                    "type": "input_text",
                    "text": snapshot_text,
                }],
            }])

            self.context_manager.replace_sync_hashes(
                session_id,
                new_hashes,
            )
            metadata_changed = True
            if not self.session_store.set_interview_thread_id(
                session_id,
                new_interview_thread_id,
            ):
                raise RuntimeError("session disappeared while saving interview thread")
            local_state_changed = True

            if old_interview_thread_id:
                client.archive_thread(old_interview_thread_id)

            return {
                "interview_thread_id": new_interview_thread_id,
                "context_count": len(contexts),
                "snapshot": snapshot_text,
            }
        except Exception:
            if local_state_changed:
                self.session_store.set_interview_thread_id(
                    session_id,
                    old_interview_thread_id,
                )
            if metadata_changed:
                if metadata_existed:
                    self.context_manager.replace_sync_hashes(
                        session_id,
                        old_sync_hashes,
                    )
                else:
                    self.context_manager.remove_sync_metadata(
                        session_id
                    )
            raise
        finally:
            client.stop()

    def _read_effective_snapshot(self, session_id):
        contexts = []
        for context in self.context_manager.resolve_effective_contexts(
            session_id
        ):
            content, content_hash = self.context_manager.read_context_snapshot(
                context
            )
            contexts.append(SnapshotContext(
                scope=context.scope,
                name=self._display_name(context.name),
                filename=context.name,
                path=context.path,
                content=content,
                content_hash=content_hash,
                logical_key=self.context_manager.context_logical_key(
                    context.name
                ),
            ))
        return contexts

    @staticmethod
    def _display_name(filename):
        stem = Path(filename).stem
        return " ".join(
            stem.replace("_", " ").replace("-", " ").split()
        ).title()

    @staticmethod
    def _format_snapshot(contexts):
        lines = [
            "INTERVIEW CONTEXT SNAPSHOT",
            "This snapshot is the only current authoritative Context for this "
            "interview.",
            "Do not use any previous Context; use only the Context entries "
            "contained in this snapshot.",
        ]
        if not contexts:
            lines.extend(["", "No effective Context entries are present."])
        for index, context in enumerate(contexts, start=1):
            lines.extend([
                "",
                f"--- CONTEXT {index} START ---",
                f"Scope: {context.scope.upper()}",
                f"Name: {context.name}",
                f"Filename: {context.filename}",
                "Content:",
                context.content,
                f"--- CONTEXT {index} END ---",
            ])
        return "\n".join(lines)
