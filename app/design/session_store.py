"""Design graph persistence on the shared agent checkpoint tables."""

from __future__ import annotations

from app.db.checkpointer import SqlCheckpointSaver as _SqlCheckpointSaver
from app.db.models import AgentCheckpoint, AgentCheckpointBlob, AgentCheckpointWrite

# Compatibility exports for callers that inspect the bound ORM models.
DesignCheckpoint = AgentCheckpoint
DesignCheckpointBlob = AgentCheckpointBlob
DesignCheckpointWrite = AgentCheckpointWrite


class SqlCheckpointSaver(_SqlCheckpointSaver):
    """Bind the shared tables to the design graph namespace."""

    def __init__(self) -> None:
        super().__init__("design")
