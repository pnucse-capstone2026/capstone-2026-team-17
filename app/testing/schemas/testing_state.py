import operator
from typing import Annotated, Any, TypedDict


class TestingState(TypedDict):
    """
    LangGraph state for the Testing Agent.
    """

    # Inputs
    run_id: str
    # 검사 대상 앱. 정적분석이 읽을 배포/IaC 스냅샷과 동적 검사가 읽을 기능
    # 요구사항이 모두 이 id로 DB에서 조회된다. LangGraph는 스키마에 없는 키를
    # 조용히 버리므로, 이 칸이 없으면 호출자가 넘겨도 노드에는 닿지 않는다.
    app_id: str
    # TestingInput의 고정 snapshot이 있으면 dynamic node는 DB를 다시 조회하지 않는다.
    testing_input: dict[str, Any]
    # Testing 작업을 시작할 때 한 번 복원한 애플리케이션 폴더다. 모든 정적·동적
    # 검사는 이 폴더를 함께 사용하며 검사 도중 DB에서 파일을 다시 읽지 않는다.
    application_dir: str
    target_url: str  # Target URL for dynamic testing, defaults to localhost:8080
    # 사용자가 선택한 이전 수리 이력. 동적 테스트 생성기는 같은 실패와 후보를
    # 반복하지 않도록 이 값을 프롬프트 문맥으로만 사용한다.
    repair_history: dict[str, Any]
    # Canonical Arazzo resume data. Inputs remain outside candidatePlan so the
    # stored plan is a valid, portable Arazzo document.
    fixed_arazzo_document: dict[str, Any] | None
    fixed_workflow_inputs: dict[str, dict[str, Any]]
    fixed_input_values: dict[str, list[dict[str, Any]]]
    preserved_workflow_results: list[dict[str, Any]]
    priority_workflow_id: str
    iac_expected: bool | None
    deployment_package_expected: bool | None
    # ``None``은 최초 실행처럼 모든 gate를 검사한다. 수리 실행은 실패 원인과
    # 직접 연결된 gate 이름만 넣고, 나머지는 입력 digest가 같은 이전 보고서를 쓴다.
    gate_scope: list[str] | None
    previous_reports: dict[str, Any]
    previous_job_id: str
    # 데모 모드는 계획과 산출물은 그대로 만들되 실제 검증 도구만 실행하지 않는다.
    validation_skipped: bool

    # State
    current_node: str
    errors: Annotated[list[str], operator.add]

    # Reports from each verification node
    static_report: dict[str, Any] | None
    dynamic_functional_report: dict[str, Any] | None
    iac_report: dict[str, Any] | None


# 이름이 ``Test``로 시작하지만 pytest 수집 대상 클래스가 아니다.
TestingState.__test__ = False  # type: ignore[attr-defined]
