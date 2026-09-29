"""API 제안과 승인 endpoint가 공유하는 공개 타입 계약이다.

LLM은 작은 proposal만 답하고, 코드는 실행 연결이 추가된 저장 모델을 만든다.
렌더된 OpenAPI가 아니라 이 두 Pydantic 모델을 생성과 수정의 기준으로 삼는다.
설계 서비스뿐 아니라 구현 단계도 이 파일의 승인된 저장 모델만 참조한다.
"""

from __future__ import annotations

from collections import Counter
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ApiSpecRecord(BaseModel):
    """API proposal과 저장 모델이 공유하는 Pydantic 기반이다."""


class ApiField(ApiSpecRecord):
    name: str
    type: str = Field(default="string")
    required: bool = Field(default=True)
    description: str = Field(default="")


class ApiResponse(ApiSpecRecord):
    status: int = Field(default=200)
    description: str = Field(default="")
    schema_name: str = Field(default="")
    is_array: bool = Field(default=False)


class ApiResponseProposal(ApiSpecRecord):
    """LLM이 정하는 HTTP 상태다. 응답 데이터 타입은 코드가 채운다."""

    status: int = Field(ge=100, le=599, description="HTTP status selected for this outcome.")
    description: str = Field(
        min_length=1,
        max_length=200,
        description="Short domain outcome represented by this status.",
    )


class ApiControlArgument(ApiSpecRecord):
    """HTTP 요청 값 하나가 Control parameter로 흐르는 명시적 연결이다."""

    name: str
    source: str
    stable_ref: str = Field(default_factory=str, exclude_if=lambda value: not value)
    required_value_ref: str = Field(default_factory=str, exclude_if=lambda value: not value)


class ApiControlOutcome(ApiSpecRecord):
    """문서화된 HTTP status 하나를 만드는 이름 있는 Control 결과다."""

    status: int
    outcome: str = Field(min_length=1)


class ApiControlBinding(ApiSpecRecord):
    """HTTP operation 하나의 뒤에 있는 실행 가능한 application 계약이다."""

    control: str
    method: str
    arguments: list[ApiControlArgument] = Field(default_factory=list)
    outcomes: list[ApiControlOutcome] = Field(default_factory=list)


class ApiEndpointProposal(ApiSpecRecord):
    """LLM이 고르는 최소 HTTP 표현이다.

    이미 BCE에 있는 parameter, return type, Control 연결을 다시 출력하게 하지 않는다.
    ``interaction_id``로 기존 계약을 고르고 HTTP에서만 의미가 있는 값만 답한다.
    """

    interaction_id: str = Field(
        min_length=1,
        max_length=600,
        description="One supplied interaction candidate copied exactly.",
    )
    path: str = Field(
        max_length=200,
        description=(
            "Concrete resource-oriented absolute path, for example /registrations or "
            "/offerings/{offeringId}; never a one-letter placeholder."
        ),
    )
    method: Literal["get", "post", "put", "patch", "delete"] = Field(
        description="HTTP method selected from the interaction's business meaning."
    )
    summary: str = Field(
        min_length=1,
        max_length=200,
        description="One short sentence describing this endpoint.",
    )
    responses: list[ApiResponseProposal] = Field(
        min_length=1,
        max_length=8,
        description="Success and use-case failure outcomes represented as HTTP statuses.",
    )


class ApiSpecProposal(ApiSpecRecord):
    """각 상호작용의 HTTP 표현만 담는 일시적인 LLM 응답이다."""

    Endpoints: list[ApiEndpointProposal] = Field(default_factory=list)

    @model_validator(mode="after")
    def interaction_ids_are_unique(self) -> ApiSpecProposal:
        """상호작용 누락과 OpenAPI에서 서로 덮어쓰는 중복 경로를 거부한다."""

        ids = [endpoint.interaction_id for endpoint in self.Endpoints]
        if len(ids) != len(set(ids)):
            raise ValueError("API proposal interaction_id values must be unique")
        invalid_paths = [
            endpoint.path
            for endpoint in self.Endpoints
            if len(endpoint.path) < 2 or not endpoint.path.startswith("/")
        ]
        if invalid_paths:
            raise ValueError(
                "API proposal paths must be concrete absolute paths; invalid: "
                + ", ".join(repr(path) for path in invalid_paths)
            )
        routes = [(endpoint.method, endpoint.path) for endpoint in self.Endpoints]
        if len(routes) != len(set(routes)):
            duplicates = [
                f"{method.upper()} {path}"
                for (method, path), count in Counter(routes).items()
                if count > 1
            ]
            raise ValueError(
                "API proposal method and path pairs must be unique; duplicates: "
                + ", ".join(duplicates)
            )
        return self


class ApiEndpoint(ApiSpecRecord):
    interaction_id: str = Field(default="")
    path: str = Field(default="/")
    method: str = Field(default="get")
    summary: str = Field(default="")
    operation_id: str = Field(default="")
    path_params: list[ApiField] = Field(default_factory=list)
    query_params: list[ApiField] = Field(default_factory=list)
    request_schema: str = Field(default="")
    responses: list[ApiResponse] = Field(default_factory=list)
    source_classes: list[str] = Field(default_factory=list)
    use_case_ids: list[str] = Field(default_factory=list)
    # Boundary 진입 interaction이 직접 추적하는 canonical use-case step이다. Testing은
    # 이 값이 있을 때만 여러 HTTP operation의 필수 집합과 순서를 확정한다.
    scenario_step_refs: list[str] = Field(default_factory=list)
    control_binding: ApiControlBinding | None = None


class ApiSchema(ApiSpecRecord):
    name: str
    description: str = Field(default="")
    fields: list[ApiField] = Field(default_factory=list)
    values: list[str] = Field(default_factory=list)
    source_class: str = Field(default="")


class ApiSpecModel(ApiSpecRecord):
    """``api_spec_model``에 저장되는 canonical endpoint 모델이다."""

    title: str = Field(default="API")
    version: str = Field(default="1.0.0")
    Endpoints: list[ApiEndpoint] = Field(default_factory=list)
    Schemas: list[ApiSchema] = Field(default_factory=list)
