"""자유문장 클라우드 제약의 구조화 proposal 경계를 제공한다.

이 모듈은 자연어 해석 한 번만 소유한다. 근거 대조, 값 정규화, 계약 검증과 사용자
질문은 ``service``가 이어서 수행하므로 proposal 자체를 수락된 RESOURCE_SPEC으로
취급하지 않는다.
"""

from __future__ import annotations

from collections.abc import Callable

from langchain_core.messages import HumanMessage, SystemMessage

from app.requirements.runtime.structured_llm import invoke_structured
from app.requirements.schemas import CloudConstraintExtraction

ResourceConstraintProposalCall = Callable[[str], CloudConstraintExtraction]

_SYSTEM_PROMPT = """Extract only cloud constraints explicitly stated by the user.
Return one structured object. Do not infer defaults or recommendations.
Each *_evidence value must be an exact contiguous quote from the input.
Use null and empty evidence when a value is absent. If statements conflict or are
ambiguous, leave the value null and add its RESOURCE_SPEC field name to
ambiguous_fields. The deployment workload is fixed by the system and is not extracted.
Provider must be aws, azure, or gcp when explicit. Region stays in the user's words;
code resolves it later. A monthly price or instance price is not a monthly budget.
steady means sustained load; spiky means intermittent peaks. Do not derive vCPU or memory
from users or traffic. Availability is a system deployment policy, not a user-supplied
RESOURCE_SPEC field."""


def resource_constraint_messages(
    briefing: str,
) -> list[SystemMessage | HumanMessage]:
    """Resource proposal의 기존 메시지 envelope을 조립한다.

    Args:
        briefing: 사용자 제약과 요구사항을 구분해 렌더링한 입력이다.

    Returns:
        기존 system prompt와 briefing human message를 순서대로 담은 목록이다.

    Notes:
        메시지 계약만 조립하며 LLM 호출이나 값 정규화를 수행하지 않는다.
    """
    return [SystemMessage(content=_SYSTEM_PROMPT), HumanMessage(content=briefing)]


def propose_resource_constraints(
    briefing: str,
    *,
    proposal_call: ResourceConstraintProposalCall | None = None,
) -> CloudConstraintExtraction:
    """자유문장 입력을 한 번의 구조화 proposal로 읽는다.

    Args:
        briefing: 사용자 제약과 요구사항을 구분해 렌더링한 입력이다.
        proposal_call: 테스트나 상위 adapter가 주입하는 동일 계약의 호출 함수다.

    Returns:
        아직 정규화·검증하지 않은 구조화 제약 proposal이다.

    Notes:
        주입 함수가 없을 때만 기존 ``CloudConstraintExtraction`` operation으로 LLM을
        한 번 호출한다. retry와 native structured→JSON fallback은 runtime adapter가
        기존 설정대로 소유한다.
    """
    if proposal_call is not None:
        return proposal_call(briefing)
    return invoke_structured(
        CloudConstraintExtraction,
        resource_constraint_messages(briefing),
    )


__all__ = [
    "ResourceConstraintProposalCall",
    "propose_resource_constraints",
    "resource_constraint_messages",
]
