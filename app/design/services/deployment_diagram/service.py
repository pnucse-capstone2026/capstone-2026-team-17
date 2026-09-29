"""코드가 배포 구조를 만들고 LLM에는 표시 이름만 맡기는 서비스 경계다."""

from __future__ import annotations

import copy
from typing import Any, Protocol

from pydantic import BaseModel

from app.design.services.common.structured import parse_structured
from app.design.services.deployment_diagram.models import (
    DeploymentComponentLabels,
    WorkloadGraph,
)
from app.design.services.deployment_diagram.prompts import (
    generation_messages,
    label_revision_messages,
)
from app.design.services.deployment_diagram.template_topology import (
    build_template_workload_graph,
)


class DeploymentLabelProposalCall(Protocol):
    """공통 structured LLM adapter와 테스트 대역의 이름 제안 계약이다."""

    def __call__(
        self,
        messages: list[dict[str, str]],
        schema: type[BaseModel],
    ) -> dict[str, Any]: ...


def _components(graph: WorkloadGraph) -> list[dict[str, str]]:
    """LLM에 노출할 수 있는 ID와 현재 표시 이름만 추린다."""

    return [
        *[{"id": item.id, "name": item.name} for item in graph.workloads],
        *[
            {"id": item.id, "name": item.name}
            for item in graph.externalDependencies
        ],
    ]


def _naming_context(structured_inputs: dict[str, Any]) -> dict[str, Any]:
    """도메인 이름을 판단하는 데 필요한 작은 문맥만 남긴다."""

    classes = structured_inputs.get("classModel")
    class_items = classes.get("Classes") if isinstance(classes, dict) else None
    class_names = [
        str(item.get("className"))
        for item in class_items or []
        if isinstance(item, dict) and item.get("className")
    ]
    if class_names:
        return {"classNames": class_names}
    # 정상 설계 흐름에서는 classModel이 항상 있다. 직접 호출처럼 클래스가 없는 경우만
    # 유스케이스 앞부분을 보조 문맥으로 사용해 이름 하나를 짓는 데 전체 명세를 보내지 않는다.
    scenario = str(structured_inputs.get("useCaseSpecification") or "")
    return {"useCaseSummary": scenario[:2000]}


def _apply_labels(
    graph: WorkloadGraph,
    proposal: DeploymentComponentLabels,
) -> WorkloadGraph:
    """기존 ID와 일치하는 이름만 복사하고 나머지 구조는 그대로 보존한다."""

    names = {
        item.id: item.name.strip()
        for item in proposal.components
        if item.name.strip()
    }
    payload = copy.deepcopy(graph.model_dump())
    for key in ("workloads", "externalDependencies"):
        for component in payload.get(key) or []:
            component_id = str(component.get("id") or "")
            if component_id in names:
                component["name"] = names[component_id]
    return WorkloadGraph.model_validate(payload)


def propose_workload_graph(
    structured_inputs: dict[str, Any],
    proposal_call: DeploymentLabelProposalCall | None = None,
) -> WorkloadGraph:
    """결정론적 템플릿 구조에 LLM의 표시 이름만 적용한다."""

    graph = build_template_workload_graph(structured_inputs)
    components = _components(graph)
    if not components:
        return graph
    propose = proposal_call or parse_structured
    labels = DeploymentComponentLabels.model_validate(
        propose(
            generation_messages(
                {"components": components, "context": _naming_context(structured_inputs)}
            ),
            DeploymentComponentLabels,
        )
    )
    return _apply_labels(graph, labels)


def generate_workload_graph(
    scenario_text: str,
    api_spec: dict[str, Any],
    *,
    refined_requirements: Any = None,
    capability_contract: dict[str, Any] | None = None,
    resource_intake: dict[str, Any] | None = None,
    resource_spec: dict[str, Any] | None = None,
    class_model: Any = None,
    sequence_model: Any = None,
    erd_model: Any = None,
    deployment_planning_facts: list[dict[str, Any]] | None = None,
    proposal_call: DeploymentLabelProposalCall | None = None,
) -> WorkloadGraph:
    """승인 입력으로 구조를 만들고 한 번의 이름 제안만 수행한다."""

    if not scenario_text:
        return WorkloadGraph()
    structured = {
        "refinedRequirements": refined_requirements or [],
        "capabilityContract": capability_contract or {},
        "resourceIntake": resource_intake or {},
        "resourceSpec": resource_spec or {},
        "useCaseSpecification": scenario_text,
        "apiSpec": api_spec,
        "deploymentPlanningFacts": deployment_planning_facts or [],
    }
    if class_model:
        structured["classModel"] = class_model
    if sequence_model:
        structured["sequenceModel"] = sequence_model
    if erd_model:
        structured["erdModel"] = erd_model
    return propose_workload_graph(structured, proposal_call)


def revise_workload_graph(
    current_model: WorkloadGraph,
    feedback: str,
    targets: set[str] | None = None,
    *,
    proposal_call: DeploymentLabelProposalCall | None = None,
) -> WorkloadGraph:
    """피드백에서 기존 컴포넌트의 표시 이름만 수정한다."""

    if not feedback:
        return current_model
    components = _components(current_model)
    if not components:
        return current_model
    propose = proposal_call or parse_structured
    labels = DeploymentComponentLabels.model_validate(
        propose(
            label_revision_messages(components, feedback, targets),
            DeploymentComponentLabels,
        )
    )
    return _apply_labels(current_model, labels)
