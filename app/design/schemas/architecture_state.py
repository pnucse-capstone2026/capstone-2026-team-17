from __future__ import annotations

import json
from typing import Any

from typing_extensions import TypedDict


class ArchitectureState(TypedDict, total=False):
    """요구사항 산출물과 설계 graph의 실행 상태를 함께 전달하는 typed dict.

    ``total=False``이므로 아직 실행하지 않은 stage의 필드는 없어도 된다. 각 stage는 자신이
    만든 필드만 추가하고, 저장소는 완료된 산출물로 같은 구조를 다시 구성한다.
    """

    app_id: str

    # 사용자가 입력한 원문이며 apps 테이블의 같은 행에 저장한다.
    requirements_text: str
    resource_constraints_text: str

    # 요구사항 분석 산출물이다. 현재 orchestration은 분류된 요구사항 list를 전달한다.
    # 설계를 직접 호출하는 일부 경로는 배포 planning fact가 사용하는 이름 있는 wrapper도 받는다.
    refined_requirements: list[dict[str, Any]] | dict[str, Any]
    capability_contract: dict[str, Any]
    resource_intake: dict[str, Any]
    usecase_spec: dict[str, Any]
    relationships: dict[str, Any]
    usecase_diagram_puml: str
    usecase_diagram_syntax_valid: bool
    usecase_diagram_syntax_errors: list[str]
    resource_spec: dict[str, Any]
    # 배포 단계에서만 사용하는 명시적인 사실 목록이다. 애플리케이션 설계 산출물에는
    # 섞지 않고 코드가 WorkloadGraph 템플릿을 선택하고 검사할 때만 사용한다.
    deployment_planning_facts: list[dict[str, Any]]

    # 설계 stage는 LLM이 만들고 수정하는 구조화 모델(*_model / *_bce_classes)을 저장한다.
    # *_puml과 api_spec은 그 모델로 다시 만들 수 있는 표시용 산출물이다. 피드백은 기준
    # 모델에 적용하고 표시용 산출물을 다시 생성하므로 두 내용이 따로 바뀌지 않는다.
    # *_feedback 필드는 사용자 피드백 한 건을 stage의 feedback subgraph로 전달할 때만
    # 사용하며 저장되는 산출물에는 포함하지 않는다.
    extracted_bce_classes: dict[str, Any]
    class_diagram_feedback: str
    # One-shot guidance for retrying a failed class-generation checkpoint. The
    # extract node clears it in the same checkpoint update that accepts a model.
    class_binding_repair_guidance: str
    # One-shot structured answer to a class argument-source question.
    class_binding_source_decision: dict[str, Any]
    class_diagram_puml: str
    class_diagram_syntax_valid: bool
    class_diagram_syntax_errors: list[str]
    # BCE 모델을 PlantUML로 만들기 전에 실행한 코드 기반 설계 검사 결과다.
    # {findings: list[str], repair_iters: int, stopped: str, error?: str}
    #
    # *_syntax_valid와는 검사 대상이 다르다. syntax 필드는 렌더링한 PlantUML을 parser가
    # 읽을 수 있는지 나타낸다. 여기서는 LLM이 만든 모델이 app/design/knowledge/rules.py의
    # 설계 규칙을 지키는지 확인한다. stopped에는 repair loop가 끝난 이유를 기록하므로,
    # "문제가 없음"과 "수리를 계속해도 나아지지 않음"을 구분할 수 있다.
    class_diagram_check: dict[str, Any]

    # 현재 실행은 {Diagrams: [{use_case_id, use_case_name, Participants, Messages}]}를 저장한다.
    # 이전 형식에는 {Participants, Messages} 한 건만 들어 있을 수 있다.
    sequence_diagram_model: dict[str, Any]
    sequence_diagram_feedback: str
    sequence_diagram_puml: str
    sequence_diagram_syntax_valid: bool
    sequence_diagram_syntax_errors: list[str]
    sequence_diagram_check: dict[str, Any]
    # False이면 구조화 모델과 finding은 다음 사용자/LLM repair를 위해 남아 있지만,
    # 아직 검토 가능한 PlantUML 이미지로 공개할 수 없다는 뜻이다.
    sequence_diagram_renderable: bool

    api_spec_model: dict[str, Any]
    api_spec_feedback: str
    api_spec: dict[str, Any]
    api_spec_syntax_valid: bool
    api_spec_syntax_errors: list[str]
    api_spec_check: dict[str, Any]

    # ERD는 BCE Entity 사본을 따로 보관한다. ERD 피드백이 클래스 다이어그램의 기준 모델을
    # 함께 바꾸지 않도록 분리한 것이다.
    erd_bce_classes: dict[str, Any]
    erd_feedback: str
    erd_puml: str
    erd_syntax_valid: bool
    erd_syntax_errors: list[str]
    # class_diagram_check와 형태는 같지만 두 대상을 검사한다. BCE 모델과 그 모델에서 만든
    # 논리 데이터 모델(table, primary key, foreign key)을 함께 본다. BCE 모델에는 table이
    # 없으므로 "모든 table에 primary key가 있는가" 같은 항목은 mapping 결과에서 검사한다.
    erd_check: dict[str, Any]

    deployment_diagram_model: dict[str, Any]
    # 설계 단계가 관리하는 bundle로, 편집 가능한 논리 모델, 선택한 CSP topology와
    # ResourcePlan을 담는다. 실행 구조도와 provisioning 구조도는 이 bundle에서 만든다.
    deployment_diagram_bundle: dict[str, Any]
    # WorkloadGraph 원본과 코드로 계산한 후속 planning 결과다.
    deployment_workload_graph: dict[str, Any]
    deployment_plan: dict[str, Any]
    deployment_topology: dict[str, Any]
    deployment_resource_plan: dict[str, Any]
    artifact_versions: dict[str, Any]
    deployment_diagram_feedback: str
    deployment_diagram_puml: str
    deployment_diagram_provisioning_puml: str
    deployment_diagram_syntax_valid: bool
    deployment_diagram_syntax_errors: list[str]
    deployment_diagram_check: dict[str, Any]

    # 아래 필드는 app/design/graphs/design_graph.py가 실행 중에만 사용하며 산출물이 아니다.
    # gate_route는 조건부 edge가 "advance"(다음 stage)와 "loop"(현재 stage 수정) 중 무엇을
    # 선택할지 알려 준다. stage_origin은 현재 결과가 최초 생성인지 피드백 수정인지 기록해
    # 저장 node가 산출물 version의 origin을 올바르게 표시하게 한다.
    gate_route: str
    stage_origin: str

    artifact_status: dict[str, str]


def usecase_spec_text(state: ArchitectureState) -> str:
    """use case specification을 LLM prompt에 넣을 문자열로 반환한다.

    모든 설계 산출물은 요구사항 분석이 만든 use case specification에서 시작하므로, 각
    설계 LLM 호출은 이 내용을 공통 문맥으로 사용한다. dict 입력은 읽기 쉬운 JSON으로 만든다.
    """
    spec = state.get("usecase_spec")
    if not spec:
        return ""
    if isinstance(spec, str):
        return spec
    return json.dumps(spec, ensure_ascii=False, indent=2)
