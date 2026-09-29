"""Immutable inputs for one Testing run."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.db.models import (
    TYPE_DEPLOYMENT_FILE,
    TYPE_FRONTEND_SOURCE_CODE,
    TYPE_IAC_CODE,
    TYPE_SOURCE_CODE,
    TYPE_TEST_CODE,
)

TESTING_ARTIFACT_TYPES = frozenset(
    {
        TYPE_SOURCE_CODE,
        TYPE_FRONTEND_SOURCE_CODE,
        TYPE_TEST_CODE,
        TYPE_DEPLOYMENT_FILE,
        TYPE_IAC_CODE,
    }
)
REQUIRED_TESTING_ARTIFACT_TYPES = frozenset({TYPE_SOURCE_CODE, TYPE_DEPLOYMENT_FILE})


class FrozenInput(BaseModel):
    """One design contract snapshot, identified by version and/or digest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version_id: int | None = Field(default=None, ge=1)
    digest: str | None = Field(default=None, min_length=1)
    content: Any = None


class TestingContracts(BaseModel):
    """Fixed functional and deployment contracts used by dynamic Testing."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    requirements: FrozenInput | None = None
    use_cases: FrozenInput | None = None
    openapi: FrozenInput | None = None
    # Deployment content includes the final ResourcePlan and package manifest.
    deployment: FrozenInput | None = None


class TestingInput(BaseModel):
    """One implementation snapshot and its frozen upstream contract artifacts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    app_id: str = Field(min_length=1)
    implementation_job_id: str = Field(min_length=1)
    artifact_version_ids: dict[str, int]
    contract_artifacts: TestingContracts = Field(default_factory=TestingContracts)
    # 파일 snapshot이 이전과 같아 version ID를 재사용해도 현재 구현 Job의 RTM은
    # 달라질 수 있다. 파일 metadata에 기대지 않고 실행 입력에 함께 고정한다.
    implementation_traceability: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _valid_artifact_ids(self) -> TestingInput:
        unknown = sorted(set(self.artifact_version_ids) - TESTING_ARTIFACT_TYPES)
        if unknown:
            raise ValueError(
                "Testing input contains unsupported artifact types: " + ", ".join(unknown)
            )
        missing = sorted(REQUIRED_TESTING_ARTIFACT_TYPES - set(self.artifact_version_ids))
        if missing:
            raise ValueError("Testing input is missing required artifact IDs: " + ", ".join(missing))
        invalid = sorted(
            artifact_type
            for artifact_type, version_id in self.artifact_version_ids.items()
            if not isinstance(version_id, int) or isinstance(version_id, bool) or version_id < 1
        )
        if invalid:
            raise ValueError("Artifact IDs must be positive integers: " + ", ".join(invalid))
        return self
