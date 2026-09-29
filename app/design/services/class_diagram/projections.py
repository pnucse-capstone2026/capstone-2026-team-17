"""수락된 클래스 모델에서 renderer가 사용할 결정론적 파생값을 만든다.

입력은 typed ``BCEModel`` 또는 같은 별칭 JSON이고 출력은 저장하지 않는 표시 단위다.
service나 LLM을 참조하지 않으며 승인 모델을 수정하지 않는다. 따라서 renderer가 generation
pipeline을 역참조하지 않고도 호출 관계를 그릴 수 있다.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.design.schemas.class_model import BCEModel
from app.design.services.class_diagram.models import CallDependency
from app.design.services.class_diagram.scenario import text
from app.design.services.class_diagram.validation.model import operation_catalog


def project_call_dependencies(
    model: BCEModel | Mapping[str, Any],
) -> list[CallDependency]:
    """영속 호출 트리에서 클래스 다이어그램용 의존선을 결정론적으로 만든다.

    Args:
        model: operation owner와 collaboration call tree를 가진 수락 모델이다.

    Returns:
        source/target이 같은 self edge를 제외하고 중복 제거·정렬한 dependency 목록이다.

    Notes:
        parent call owner가 source, child call owner가 target이다. 결과는 renderer 전용이며
        ``BCEModel.Relationships``에 기록하지 않는다.
    """
    payload = model.model_dump(by_alias=True) if isinstance(model, BCEModel) else dict(model)
    owners = {
        operation_id: operation["className"]
        for operation_id, operation in operation_catalog(payload).items()
    }
    result: dict[tuple[str, str], CallDependency] = {}
    for collaboration in payload.get("Collaborations") or []:
        if not isinstance(collaboration, dict):
            continue
        calls = {
            text(call.get("callId")): call
            for call in collaboration.get("calls") or []
            if isinstance(call, dict)
        }
        for call in calls.values():
            parent = calls.get(text(call.get("parentCallId")))
            if not parent:
                continue
            source = owners.get(text(parent.get("receiverOperationId")), "")
            target = owners.get(text(call.get("receiverOperationId")), "")
            if source and target and source != target:
                result[(source, target)] = CallDependency(source=source, target=target)
    return [result[key] for key in sorted(result)]
