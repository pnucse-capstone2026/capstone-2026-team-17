from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Diagnostic:
    code: str
    severity: str
    message: str
    source: str | None = None


@dataclass
class CommandEvidence:
    name: str
    command: list[str]
    cwd: str
    exit_code: int
    duration_ms: int
    stdout: str
    stderr: str


@dataclass
class JobSpec:
    job_type: str
    feedback: str
    name: str
    workspace_root: Path
    inputs: dict[str, Path]
    required_inputs: list[str]
    base_package: str
    allow_assumptions: bool
    verify_compile: bool
    output_root: Path
    agent_mode: str
    agent_temperature: float
    agent_max_output_tokens: int
    # The parent web process polls this small file while the independently
    # spawned generator is running.  It is deliberately outside the immutable
    # run directory, which does not exist until generation completes.
    progress_path: Path | None = None
    app_id: str | None = None
    # Testing 수리는 일반 구현 task와 달리 원래 실패한 gate를 그대로 다시 실행한다.
    # 비어 있으면 기존 feedback 작업과 같은 control/Gradle 검사를 사용한다.
    repair_task_type: str = "control"
    repair_file_hints: list[str] = field(default_factory=list)
    repair_verification_profile: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunManifest:
    schema_version: str = "easydep-implementation-agent/v1alpha1"
    job_name: str = ""
    app_id: str | None = None
    status: str = "RECEIVED"
    input_hash: str = ""
    inputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    tools: dict[str, dict[str, str]] = field(default_factory=dict)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    commands: list[CommandEvidence] = field(default_factory=list)
    generated_files: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    implementation_tasks: list[dict[str, Any]] = field(default_factory=list)
    agent_execution: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
