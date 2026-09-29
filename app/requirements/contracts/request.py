"""요구사항 HTTP 분석 요청과 구조화 입력의 canonical Pydantic 계약이다."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

FeedbackStage = Literal["actors", "use_cases", "specs", "relationships"]
CloudProvider = Literal["aws", "azure", "gcp"]


class FeedbackEdit(BaseModel):
    """화면이 이미 아는 것을 추측하지 않고 그대로 보내는 구조화 피드백.

    자연어 피드백은 LLM이 `{stage, scope, target_ids}`를 **추측**해야 한다. 그런데
    화면은 사용자가 어느 단계의 어느 항목을 편집 중인지 이미 알고 있다. 알고 있는 것을
    보내면 그 LLM 호출과 오분류가 통째로 사라진다.

    `instruction`만 자연어로 남는다 — 무엇을 어떻게 바꿀지는 사람이 말해야 하고 그건
    생성 모델의 몫이다. **자연어 경로를 대체하지 않는다**: 사용자가 use_cases 게이트에서
    "액터에서 관리자를 분리해줘"라고 적으면 분류기가 actors로 보내 주는데, 그 기능은
    그대로 둔다. 화면이 확신할 때만 이 형태를 쓴다.
    """

    stage: FeedbackStage
    scope: Literal["local", "broad"] = "broad"
    #: local일 때 대상 항목 id. broad면 비운다.
    target_ids: list[str] = Field(default_factory=list)
    instruction: str


class ResourceAnswer(BaseModel):
    """되묻기(`RESOURCE_SPEC`의 못 채운 칸)에 대한 답. **요구사항 피드백이 아니다.**

    같은 게이트에서 두 종류의 입력을 받기 때문에 형태로 가른다. 자연어로 받으면
    "aws 서울 월 3000달러"를 요구사항 편집 지시로 알아듣고 분류를 다시 돌린다 —
    사용자는 질문에 답했을 뿐인데 요구사항이 흔들린다.

    값은 **문자열 그대로** 받는다. 해석은 제약 구조화 에이전트가 산문과 같은 규율로
    한다(`resources/service.py`) — 화면이 "서울"을 코드로 바꿔 보내면 그 해석이
    어디서 왔는지 아무도 모르게 된다. 답했다는 사실이 모호함을 없애 주지도 않는다:
    "서울"은 여전히 카탈로그를 거쳐야 하고, 후보가 여럿이면 여전히 되물어야 한다.
    """

    #: 계약 칸 이름 → 사용자가 쓴 답. 모르는 칸은 단계가 버린다.
    answers: dict[str, str] = Field(default_factory=dict)
    #: ConversationAgent가 질문에 대한 자유문장 답변으로 판정한 원문.
    free_text: str | None = None
    #: 자유문장이 답하려던 화면의 질문 field. 해석 근거이지 직접 대입 대상이 아니다.
    expected_field: str | None = None

    @model_validator(mode="after")
    def _one_answer_mode(self) -> ResourceAnswer:
        """Keep typed UI answers and interpreted prose as distinct inputs."""
        if self.free_text is None:
            if self.expected_field is not None:
                raise ValueError("expected_field requires free_text")
            return self
        self.free_text = self.free_text.strip()
        expected = (self.expected_field or "").strip()
        if not self.free_text or not expected or self.answers:
            raise ValueError("free_text requires expected_field and cannot include direct answers")
        self.expected_field = expected
        return self


class IdentitySourceAnswer(BaseModel):
    """Typed choice for the source of one identify obligation."""

    use_case_id: str = Field(min_length=1)
    obligation_ref: str = Field(min_length=1)
    identity_source_kind: Literal["caller_input", "authenticated_context", "system_result"]
    source_authenticate_obligation_ref: str | None = None

    @model_validator(mode="after")
    def _source_reference_matches_kind(self) -> IdentitySourceAnswer:
        ref = (self.source_authenticate_obligation_ref or "").strip()
        if self.identity_source_kind == "authenticated_context":
            if not ref:
                raise ValueError("authenticated_context requires an authenticate obligation ref")
            self.source_authenticate_obligation_ref = ref
        elif ref:
            raise ValueError("authenticate obligation ref is only valid for authenticated_context")
        else:
            self.source_authenticate_obligation_ref = None
        self.use_case_id = self.use_case_id.strip()
        self.obligation_ref = self.obligation_ref.strip()
        return self


class InitialCloudConstraints(BaseModel):
    """사용자가 분석 시작 전에 직접 고르는 최소 클라우드 제약.

    소프트웨어 요구사항과 섞지 않는다. 정확한 CSP, 사용자가 쓴 지역 표현, 예산을
    구조화해서 받아 제약 추출 LLM이 이미 알려진 값을 다시 추측하지 않게 한다.
    """

    provider: CloudProvider
    region: str = Field(min_length=1)
    monthly_budget_amount: float | None = Field(default=None, gt=0)
    monthly_budget_currency: str = Field(default="USD", min_length=3, max_length=3)

    @field_validator("region")
    @classmethod
    def _non_blank_region(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("region must not be blank")
        return value

    @field_validator("monthly_budget_currency")
    @classmethod
    def _currency_code(cls, value: str) -> str:
        value = value.strip().upper()
        if not value.isalpha() or len(value) != 3:
            raise ValueError("monthly_budget_currency must be a three-letter code")
        return value


class DeploymentTarget(BaseModel):
    """배포 대안 지도에서 선택한 단일 provider·region 대상이다."""

    provider: CloudProvider
    region: str = Field(min_length=1, max_length=100)
    zones: list[str] = Field(default_factory=list, max_length=16)

    @field_validator("region")
    @classmethod
    def _target_region(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("region must not be blank")
        return value

    @field_validator("zones")
    @classmethod
    def _target_zones(cls, values: list[str]) -> list[str]:
        normalized = [str(value).strip() for value in values if str(value).strip()]
        if len(normalized) != len(set(normalized)):
            raise ValueError("zones must not contain duplicates")
        return normalized


class DeploymentPreferences(BaseModel):
    """애플리케이션 요구사항과 분리해 입력하는 클라우드 배포 대안이다."""

    mode: Literal["alternatives"] = "alternatives"
    targets: list[DeploymentTarget] = Field(min_length=1, max_length=3)
    monthly_budget_amount: float | None = Field(default=None, gt=0)
    monthly_budget_currency: str = Field(default="USD", min_length=3, max_length=3)
    resource_constraints_text: str = Field(default="", max_length=12000)

    @field_validator("monthly_budget_currency")
    @classmethod
    def _preference_currency(cls, value: str) -> str:
        value = value.strip().upper()
        if not value.isalpha() or len(value) != 3:
            raise ValueError("monthly_budget_currency must be a three-letter code")
        return value

    @model_validator(mode="after")
    def _coherent_targets(self) -> DeploymentPreferences:
        providers = [target.provider for target in self.targets]
        if len(providers) != len(set(providers)):
            raise ValueError("select at most one region for each provider")
        return self


class AnalyzeRequest(BaseModel):
    """요구사항 분석 세션 시작 또는 진행 요청.

    - 신규 세션: requirements 를 채워 보낸다 (thread_id 없음/무관).
    - 구체화 답변: answer 와 기존 thread_id 를 함께 보낸다.
    """

    requirements: list[str] | None = None
    # 최초 화면에서 받는 최소 클라우드 좌표. 신규 세션에만 사용한다.
    cloud_constraints: InitialCloudConstraints | None = None
    # 대화형 워크스페이스가 요구사항 분석 중 별도로 받은 복수 CSP 배포 대안.
    # 신규 세션의 초기값 또는 요구사항 게이트의 구조화 재개 값으로 사용한다.
    deployment_preferences: DeploymentPreferences | None = None
    # 클라우드 제약 원문(`apps.resource_constraints_text`). 요구사항과 **따로** 받는다 —
    # 여기서 `RESOURCE_SPEC`이 만들어진다(`resources/service.py`). 없으면 필수 칸이
    # 비고, 그 사실이 되묻기 질문으로 나간다.
    resource_constraints_text: str | None = None
    answer: str | None = None
    # 자연어 대신 보내는 구조화 편집(피드백 게이트 전용). answer와 함께 보낼 수 없다 —
    # 둘 다 오면 무엇을 따를지가 모호해지므로 400으로 거절한다.
    edit: FeedbackEdit | None = None
    # 되묻기의 답(칸 이름 → 사용자가 쓴 문자열). answer/edit과 함께 보낼 수 없다 —
    # 재개 값은 하나이고, 섞이면 무엇을 따를지가 모호해진다.
    resource_answers: dict[str, str] | None = None
    # ConversationAgent가 resource 질문의 자유문장 답으로 분류한 typed 재개 입력.
    resource_answer: ResourceAnswer | None = None
    # Typed answer to one requirements-stage identity-source question.
    identity_source_answer: IdentitySourceAnswer | None = None
    thread_id: str | None = None
    # 대화형 게이트(step1 clarify + 각 스텝 피드백) 사용 여부. None이면 서버 기본값(설정)을 따른다.
    # 신규 세션 시작 시에만 의미가 있으며, 이후 재개(answer)는 세션이 시작된 모드를 유지한다.
    feedback_gates: bool | None = None
    # 산출물을 저장할 앱(POST /api/workspace/apps 로 발급). 있으면 분석이 완료된 시점에
    # refined_requirements / usecase_spec / usecase_diagram 이 그 앱에 기록되어
    # 설계 에이전트가 이어받는다. 없으면 저장 없이 응답만 돌려준다(단독 실행).
    app_id: str | None = None
