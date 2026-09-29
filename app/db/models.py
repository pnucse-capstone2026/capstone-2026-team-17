from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    CHAR,
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.mysql import DATETIME, LONGBLOB, LONGTEXT, MEDIUMTEXT
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


_Blob = LargeBinary().with_variant(LONGBLOB(), "mysql")
_MediumText = Text().with_variant(MEDIUMTEXT(), "mysql")
_LongText = Text().with_variant(LONGTEXT(), "mysql")


# Artifact types. Kept as plain strings so later phases (source code, IaC,
# test results) can be added without a schema migration.
TYPE_REFINE_REQ = "REFINE_REQ"
TYPE_CAPABILITY_CONTRACT = "CAPABILITY_CONTRACT"
TYPE_RESOURCE_INTAKE = "RESOURCE_INTAKE"
TYPE_USECASE_SPEC = "USECASE_SPEC"
TYPE_USECASE_DIAGRAM = "USECASE_DIAGRAM"
TYPE_RESOURCE_SPEC = "RESOURCE_SPEC"

TYPE_CLASS = "CLASS"
TYPE_SEQUENCE = "SEQUENCE"
TYPE_API_SPEC = "API_SPEC"
TYPE_ERD = "ERD"
TYPE_DEPLOYMENT = "DEPLOYMENT"

# Implementation and test agents produce file trees.  Their version metadata
# lives in artifact_versions while the immutable files live in artifact_files.
TYPE_SOURCE_CODE = "SOURCE_CODE"
TYPE_FRONTEND_SOURCE_CODE = "FRONTEND_SOURCE_CODE"
TYPE_TEST_CODE = "TEST_CODE"
TYPE_DEPLOYMENT_FILE = "DEPLOYMENT_FILE"
TYPE_IAC_CODE = "IAC_CODE"

FORMAT_PUML = "PUML"
FORMAT_JSON = "JSON"

ORIGIN_GENERATED = "GENERATED"
ORIGIN_AUTO_FIXED = "AUTO_FIXED"
ORIGIN_FEEDBACK_REVISED = "FEEDBACK_REVISED"
# Supplied from outside this service (another agent, or entered by hand).
ORIGIN_IMPORTED = "IMPORTED"


class App(Base):
    """One cloud-native application development session, identified by a UUID."""

    __tablename__ = "apps"

    app_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # What the user typed. Kept because the refined requirements interpret it
    # and cannot be turned back into it, and because regenerating REFINE_REQ
    # after feedback needs the original wording.
    requirements_text: Mapped[str | None] = mapped_column(_MediumText, nullable=True)
    resource_constraints_text: Mapped[str | None] = mapped_column(_MediumText, nullable=True)
    # 개발 진행 상태: the stage whose artifact was written most recently.
    current_stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 앱당 하나뿐이고 독립 검색·이력이 없는 요구사항 단계의 최신 배포 선택이다.
    deployment_preferences: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    # 요구사항 그래프의 topology도 앱 수명주기에 속하므로 별도 session 표를 두지 않는다.
    requirements_gated: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=6), nullable=False, server_default=func.now(6)
    )

    artifact_versions: Mapped[list[ArtifactVersion]] = relationship(
        back_populates="app",
        cascade="all, delete-orphan",
    )

    __table_args__ = (Index("ix_apps_created_at", "created_at"),)


class ArtifactVersion(Base):
    """One immutable revision, identified directly by app, type, and version."""

    __tablename__ = "artifact_versions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    app_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("apps.app_id", name="fk_artifact_versions_app", ondelete="CASCADE"),
        nullable=False,
    )
    artifact_type: Mapped[str] = mapped_column(String(32), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(_LongText, nullable=False)
    syntax_valid: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    syntax_errors: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    origin: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=ORIGIN_GENERATED,
        server_default=ORIGIN_GENERATED,
    )
    created_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=6), nullable=False, server_default=func.now(6)
    )

    app: Mapped[App] = relationship(back_populates="artifact_versions")
    files: Mapped[list[ArtifactFile]] = relationship(
        back_populates="artifact_version",
        cascade="all, delete-orphan",
        order_by="ArtifactFile.file_path",
    )

    __table_args__ = (
        UniqueConstraint(
            "app_id",
            "artifact_type",
            "version_no",
            name="uq_artifact_versions_app_type_no",
        ),
        CheckConstraint("version_no > 0", name="ck_versions_version_positive"),
    )


class ArtifactFile(Base):
    """One file in an immutable implementation artifact snapshot."""

    __tablename__ = "artifact_files"

    artifact_version_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "artifact_versions.id",
            name="fk_artifact_files_version",
            ondelete="CASCADE",
        ),
        primary_key=True,
        nullable=False,
    )
    # Generated files run on Linux, where paths are case-sensitive. The database
    # must not collapse README.md and readme.md under its default ai_ci collation.
    file_path: Mapped[str] = mapped_column(
        String(512, collation="utf8mb4_0900_as_cs"),
        primary_key=True,
        nullable=False,
    )
    content: Mapped[str] = mapped_column(_LongText, nullable=False)
    sha256: Mapped[str] = mapped_column(CHAR(64, collation="ascii_bin"), nullable=False)

    artifact_version: Mapped[ArtifactVersion] = relationship(back_populates="files")

    __table_args__ = (
        CheckConstraint("CHAR_LENGTH(file_path) > 0", name="ck_artifact_files_path"),
        CheckConstraint(
            "sha256 REGEXP '^[0-9a-f]{64}$'",
            name="ck_artifact_files_sha256",
        ),
    )


class WorkspaceCommand(Base):
    """One user-visible command executed from the conversational workspace."""

    __tablename__ = "workspace_commands"

    command_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    app_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("apps.app_id", name="fk_workspace_commands_app", ondelete="CASCADE"),
        nullable=False,
    )
    action: Mapped[str] = mapped_column(String(48), nullable=False)
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    payload: Mapped[Any] = mapped_column(JSON, nullable=False, default=dict)
    result: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(_LongText, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=6), nullable=False, server_default=func.now(6)
    )
    started_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6), nullable=True)

    __table_args__ = (
        Index("ix_workspace_commands_app_created", "app_id", "created_at"),
        Index("ix_workspace_commands_app_status", "app_id", "status"),
        Index("ix_workspace_commands_status", "status"),
    )


class AgentCheckpoint(Base):
    """Requirements and design checkpoints share one graph-scoped table."""

    __tablename__ = "agent_checkpoints"

    graph_type: Mapped[str] = mapped_column(String(16), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    checkpoint_ns: Mapped[str] = mapped_column(
        String(128), primary_key=True, default="", server_default=""
    )
    checkpoint_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    parent_checkpoint_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    checkpoint_type: Mapped[str] = mapped_column(String(32), nullable=False)
    checkpoint: Mapped[bytes] = mapped_column(_Blob, nullable=False)
    metadata_type: Mapped[str] = mapped_column(String(32), nullable=False)
    checkpoint_metadata: Mapped[bytes] = mapped_column(_Blob, nullable=False)

    __table_args__ = (
        Index("ix_agent_checkpoints_graph_checkpoint", "graph_type", "checkpoint_id"),
    )


class AgentCheckpointBlob(Base):
    """One graph-scoped channel value version."""

    __tablename__ = "agent_checkpoint_blobs"

    graph_type: Mapped[str] = mapped_column(String(16), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    checkpoint_ns: Mapped[str] = mapped_column(
        String(128), primary_key=True, default="", server_default=""
    )
    channel: Mapped[str] = mapped_column(String(255), primary_key=True)
    version: Mapped[str] = mapped_column(String(64), primary_key=True)
    blob_type: Mapped[str] = mapped_column(String(32), nullable=False)
    blob: Mapped[bytes | None] = mapped_column(_Blob, nullable=True)


class AgentCheckpointWrite(Base):
    """One graph-scoped write awaiting checkpoint application."""

    __tablename__ = "agent_checkpoint_writes"

    graph_type: Mapped[str] = mapped_column(String(16), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    checkpoint_ns: Mapped[str] = mapped_column(
        String(128), primary_key=True, default="", server_default=""
    )
    checkpoint_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    idx: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel: Mapped[str] = mapped_column(String(255), nullable=False)
    write_type: Mapped[str] = mapped_column(String(32), nullable=False)
    blob: Mapped[bytes] = mapped_column(_Blob, nullable=False)
    task_path: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", server_default=""
    )
