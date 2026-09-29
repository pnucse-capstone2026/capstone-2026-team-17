"""Requirements graph persistence on the shared agent checkpoint tables."""

from __future__ import annotations

from app.db.checkpointer import SqlCheckpointSaver as _SqlCheckpointSaver
from app.db.models import (
    AgentCheckpoint,
    AgentCheckpointBlob,
    AgentCheckpointWrite,
    App,
)
from app.db.session import session_scope

# Compatibility exports for callers that inspect the bound ORM models.
RequirementsCheckpoint = AgentCheckpoint
RequirementsCheckpointBlob = AgentCheckpointBlob
RequirementsCheckpointWrite = AgentCheckpointWrite


def remember_session_mode(thread_id: str, gated: bool) -> None:
    """Store the requirements topology on its owning app."""
    with session_scope() as db:
        app = db.get(App, thread_id)
        if app is None:
            raise KeyError(thread_id)
        app.requirements_gated = gated


def session_mode(thread_id: str) -> bool | None:
    """Return the stored topology, or ``None`` for an unknown/unstarted app."""
    with session_scope() as db:
        app = db.get(App, thread_id)
        if app is None or app.requirements_gated is None:
            return None
        return bool(app.requirements_gated)


class SqlCheckpointSaver(_SqlCheckpointSaver):
    """Bind the shared tables to the requirements graph namespace."""

    def __init__(self) -> None:
        super().__init__("requirements")

    def delete_thread(self, thread_id: str) -> None:
        super().delete_thread(thread_id)
        with session_scope() as db:
            app = db.get(App, thread_id)
            if app is not None:
                app.requirements_gated = None
