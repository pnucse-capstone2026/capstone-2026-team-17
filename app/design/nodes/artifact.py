"""다섯 설계 산출물이 공유하는 스테이지 골격 — 스펙 하나로 노드를 찍어낸다.

**하나의 골격.** 예전에는 두 계열이 있었다. 클래스·ERD는 구조화된 모델을 받아 결정론적
으로 렌더했고(수리 루프 없음), 시퀀스·API·배포는 LLM이 PlantUML/JSON을 직접 써서
validate → repair 루프가 필요했다. 지금은 다섯 모두 이 골격을 따른다:

    생성:   extract_{stage} → [check_{stage}] → render_{stage} → END
    피드백: revise_{stage}  → [check_{stage}] → render_{stage} → END

  extract — LLM이 **구조화 모델**을 내놓는다(자유 텍스트 아님). ERD만 예외로 LLM 대신
            클래스 다이어그램의 BCE에서 시드한다.
  revise  — 사용자 피드백을 **모델에** 적용한다. 렌더된 텍스트는 절대 편집하지 않는다.
  check   — **모델이 규칙을 지켰는지** 결정론으로 판정하고, 어겼으면 자동으로 재생성한다.
            `check_key`를 가진 스펙에만 생긴다(지금은 클래스 다이어그램뿐).
  render  — 모델 → 산출물. 결정론적이고, 입력을 중화하므로 구성에 의해 유효하다.
            그리고 **그 산출물이 성한지 스스로 확인해** 상태에 기록한다.

## check 와 render 는 다른 것이다

한때 `convert`와 `validate`가 따로 있었다. 나눠 둔 값이 없어서 합쳤다 — 문법 검증은
변환의 출력만 보고, 변환이 sanitize로 구성에 의해 유효한 산출물을 내므로 **원리상 실패할
수 없다.** 그래서 "절대 울리지 않는 노드"가 그래프에 다섯 개 떠 있었다.

그 사실은 동시에, 오랫동안 **"LLM이 낸 내용이 옳은가"를 아무도 묻지 않았다**는 뜻이기도
했다 — 문법은 항상 통과하고 의미는 검사한 적이 없었다. `check`가 그 자리를 채운다.

둘을 합치지 않는 이유는 **보는 것도 실패의 뜻도 다르기** 때문이다:

  - `check`  — **렌더 전, 모델**의 내용(`knowledge/rules.py`). 실패 = LLM이 틀렸다.
  - `render` — **렌더 후, 산출물**의 문법. 실패 = 우리 변환기가 깨졌다.

대응이 정반대다. 앞은 재생성이 옳고, 뒤는 재생성이 정확히 틀렸다(LLM은 우리 렌더러를
고칠 수 없다). 한 목록으로 뭉개면 예전 문법 수리 루프의 버그가 그대로 돌아온다.

## 예전에 없앤 수리 루프와 무엇이 다른가

없앤 것은 **문법 수리 루프**였다. LLM이 PlantUML/JSON 텍스트를 직접 쓰던 시절, 문법
오류를 되먹여 텍스트를 다시 쓰게 했다. 문제가 둘이었다 — 피드백이 렌더된 텍스트를
편집하므로 모델과 그림이 어긋날 수 있었고, LLM이 오류를 못 고치면 **종료 조건이
없었다.** 구조화 출력 + 결정론 렌더로 그 오류 자체가 불가능해지면서 루프도 사라졌다.

여기 있는 것은 그것이 아니다:

  - 고치는 대상이 **텍스트가 아니라 모델**이다(`spec.revise`). 그림은 언제나 모델의
    순수한 투영이므로 어긋날 자리가 없다.
  - 판정이 **결정론**이라 진전을 확인할 수 있다. 같은 입력·finding에 같은 전략을 다시
    쓰지 않고, 미사용 전략이 없으면 정체로 멈추므로 숫자 예산 없이 종료가 보장된다.

## 자동 수리와 사용자 판단의 경계

코드가 의미를 추측해 모델을 직접 고치지는 않는다. 대부분의 위반은 정답이 하나가 아니기
때문이다. 대신 검출 결과와 이전 시도 이력을 LLM에 주고 자동으로 다시 제안받는다. 같은
입력에 같은 전략이나 같은 후보를 반복하지 않으며, 숫자로 정한 수리 횟수 상한은 없다.
사용자에게 묻는 것은 배포 선택처럼 실제 요구사항 결정이 필요하거나, 서로 다른 자동 수리
전략이 모두 같은 실패를 반복해 구체적인 새 정보가 필요한 경우뿐이다.
"""
from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.design.knowledge.detectors import (
    Finding,
    _known_flow_step_ids,
    _known_use_case_ids,
)
from app.design.observability import log_design_timing
from app.design.schemas.architecture_state import ArchitectureState
from app.design.services.persistence_scope import erd_disposition
from app.validation import (
    RepairAttempt,
    RepairLedger,
    stable_digest,
    transient_llm_error,
)

#: 왜 재생성을 멈췄는가. **"위반 0건"과 "예산이 끝났다"를 같은 값으로 두지 않기 위해 있다.**
CLEAN = "clean"                    # 위반이 없다
NO_IMPROVEMENT = "no_improvement"  # 재생성이 위반을 줄이지 못했다 → 직전본을 지켰다
STALLED = "stalled"                # 같은 상태에서 쓸 새 수리 전략이 없다
ERROR = "error"                    # 재생성 호출이 실패했다 → 직전본을 지켰다
WAITING_EXTERNAL = "waiting_external"  # 외부 LLM이 복구될 때까지 대기한다
NEEDS_INPUT = "needs_input"        # 요구사항 결정이 필요해 LLM이 고칠 수 없다
#: 검사는 했고 재생성은 **시도하지 않았다.** 지목 수정(`cascade.py`)의 값이다 — 그 경로는
#: 사용자가 지목한 항목만 고치는 것이 보장인데, 재생성은 전체 수정으로 부르므로 그 보장을
#: 스스로 깬다. 그래서 드러내기만 하고 고칠지는 사용자가 정한다.
#:
CHECKED_ONLY = "checked_only"
#: 위반이 남아 있는 상태들. 원인은 다르지만 결과는 같다 — 남아 있는 findings가 결함의
#: 전부라고 말할 수 없다.
UNRESOLVED = (
    NO_IMPROVEMENT,
    STALLED,
    ERROR,
    WAITING_EXTERNAL,
    CHECKED_ONLY,
    NEEDS_INPUT,
)

# 산출물 LLM이 임의로 고치면 안 되는 결함. 수리 프롬프트에서 제외하되 findings와
# 최종 게이트에는 그대로 남겨 사용자의 요구사항 결정을 기다린다.
NON_REPAIRABLE_RULES = {
    "sequence.unresolved-usecase-step",
    "sequence.class-diagram-version",
}

# These defects are owned by class behavior enrichment's execution-group repair.
# Sending them through the legacy whole-model class reviser can rewrite an
# accepted skeleton and unrelated groups while losing the original binding
# failure.  Keep the findings visible, but do not start a second repair loop.
LOCAL_REPAIR_ONLY_RULES = {
    "class.operation-contract-canonical",
    "class.operation-input-producers",
}

_SEQUENCE_REPAIR_RULE_GROUPS = (
    # 먼저 모델을 안전하게 읽고 호출 방향을 판정할 수 있는 토대를 고친다.
    {
        "sequence.message-participants-exist",
        "sequence.message-bce-flow",
        "sequence.declared-boundary-control-handoff",
        "sequence.boundary-operation-direction",
        "sequence.references-exist",
        "sequence.participant-classes-exist",
        "sequence.initial-message-entry",
        "sequence.causal-call-chain",
        "sequence.database-access-discipline",
        "sequence.no-lifecycle-events",
    },
    # 그 다음 실제 수신 메서드를 확정한다. 이 단계가 고쳐져야 반환·인자 검사가
    # 비로소 판정 가능해질 수 있다.
    {
        "sequence.message-labels-match-methods",
        "sequence.self-call-method-validation",
        "sequence.message-naming-convention",
    },
    # 호출이 확정된 뒤 call/return 계약을 맞춘다.
    {
        "sequence.call-return-links",
        "sequence.unmatched-return-message",
        "sequence.return-label-matches-method-return",
        "sequence.async-call-has-no-return",
        "sequence.call-requires-return",
    },
    # 반환값과 호출 연결이 확정된 뒤에야 인자 출처를 판정할 수 있다.
    {
        "sequence.argument-data-flow",
    },
    # 마지막으로 시나리오 표현과 표시 품질을 맞춘다.
    {
        "sequence.actor-step-involvement",
        "sequence.usecase-step-coverage",
        "sequence.step-operation-distinctness",
        "sequence.flow-order",
        "sequence.fragment-condition-consistency",
        "sequence.orphan-participant-detection",
        "sequence.duplicate-consecutive-messages",
        "sequence.extension-replays-anchor-operation",
    },
)


def repair_directive(findings: list[Finding]) -> str:
    """위반 목록을 재생성 지시문으로. 규칙 꼬리표를 그대로 실어 보낸다.

    꼬리표가 붙는 이유는 근거가 함께 가야 해서다 — 짐작인 규칙은 "우리 판단"까지 달고
    간다(`knowledge/rules.py`의 `Rule.tag`). 모델에게 "왜 그것이 결함인지"를 숨기고
    고치라고 하면, 규칙을 지키는 대신 지적 문구를 회피하는 쪽으로 고친다.
    """
    listed = "\n".join(f"{i}. {f.as_issue()}" for i, f in enumerate(findings, 1))
    return (
        "[YOUR PREVIOUS OUTPUT FAILED THESE CHECKS]\n"
        f"{listed}\n\n"
        "Fix every one of them. Preserve class and DataType declarations, operation names, "
        "parameter names and types, return types, relationships, collaborations, and order. "
        "`requiredValueRef` is provenance metadata, not part of an operation signature: "
        "for each accepted RequiredValue, propagate its exact valueRef to every operation "
        "parameter that consumes or passes that same value in the use-case flow, including "
        "downstream collaborator calls. Use flow and step evidence, not name matching. "
        "Do not change other operation contracts or introduce new violations."
    )


@dataclass(frozen=True)
class DesignArtifactSpec:
    """한 산출물의 스테이지를 만드는 데 필요한 전부.

    산출물마다 다른 것은 (상태 키 넷, 함수 넷)뿐이고 그래프 골격은 같다. 그래서 새
    산출물을 추가하는 일은 스펙 하나를 적는 일이 된다.
    """

    stage: str
    #: LLM이 편집하고 저장소가 보관하는 기준 구조화 모델.
    model_key: str
    #: 모델에서 렌더된 산출물(PlantUML 문자열 또는 OpenAPI dict).
    content_key: str
    valid_key: str
    errors_key: str
    #: 사용자 피드백을 이 스테이지로 실어 나르는 전이(transient) 상태 키.
    feedback_key: str
    #: 렌더 결과의 "아직 없음" 값 — PlantUML은 "", JSON은 {}.
    empty: Any
    #: 상태 → 모델. 앞선 산출물을 재료로 쓰므로 개별 인자가 아니라 상태를 통째로 받는다.
    extract: Callable[[ArchitectureState], Any]
    #: (현재 모델, 피드백, 상태, 대상) → 수정된 모델.
    #: 대상이 비면 전체 수정, 있으면 그 항목만 고치라고 프롬프트에 적는다. 다만 지시는
    #: 지시일 뿐이라 **믿지 않는다** — 실제 보장은 merge_model 이 한다.
    revise: Callable[[Any, str, ArchitectureState, set[str]], Any]
    #: 모델 → 산출물. 결정론적이어야 한다.
    render: Callable[[Any], Any]
    #: Optional state-aware deterministic renderer.  Deployment diagrams need the
    #: logical model plus RESOURCE_SPEC and the finalized provider ResourcePlan.
    #: Other artifacts remain pure model -> document projections.
    #: 산출물 → {syntax_valid, syntax_errors}.
    validate: Callable[[Any], dict[str, Any]]
    #: 모델 안에서 개별 항목을 가리키는 법: {목록 필드 이름: 그 항목의 이름을 뽑는 함수}.
    #: 여기서 나오는 이름은 **추적표의 element 와 같아야 한다** — 그래야 "api_spec:Order 를
    #: 고쳐줘"가 모델의 어느 항목인지 통한다. 비어 있으면 지목 수정을 지원하지 않는다.
    elements: dict[str, Callable[[dict], str]] = field(default_factory=dict)
    render_with_state: Callable[[Any, ArchitectureState], Any] | None = None
    #: (모델, 상태) → 규칙 위반 목록. 렌더 **전에**, 모델에 대해 돈다.
    #: 상태를 받는 이유는 그라운딩 검사(지어낸 유스케이스 id 등)가 상류 산출물을 봐야
    #: 해서다 — 모델만으로는 "지어낸 참조"와 "정당한 참조"를 구별할 수 없다.
    check: Callable[[dict, ArchitectureState], list[Finding]] = lambda model, state: []
    #: Optional digest-bound semantic evidence persisted with this check report.
    #: Read-only hydration consumes it and never repeats a model judgement.
    check_evidence: Callable[[dict, ArchitectureState], dict[str, Any]] | None = None
    #: 검사 결과를 담는 상태 키. **비어 있으면 검사 노드 자체가 안 생긴다** —
    #: 그 산출물에는 아직 규칙이 없다는 뜻이고, 없는 것을 있는 척하지 않는다.
    #: 값은 dict: {findings: list[str], repair_iters: int, stopped: str, error?: str}.
    check_key: str = ""
    #: Optional automatic finding repair. User feedback always uses ``revise``;
    #: stages with owned local repair leave this unset so the generic graph
    #: cannot start a second whole-model correction loop.
    repair: Callable[[Any, str, ArchitectureState, set[str]], Any] | None = None
    #: Optional automatic repair seam that receives the exact typed finding batch.
    repair_batch: Callable[[Any, str, ArchitectureState, set[str], list[Finding]], Any] | None = None
    #: Optional adapter for findings nested inside top-level merge units.
    repair_target_mapper: Callable[[dict, ArchitectureState, list[Finding]], set[str]] | None = None
    #: 모델이 만들어진 뒤, 검사 전에 **다른 산출물과 대사**하는 후크. 그래프에서
    #: extract/revise 노드와 check/render 사이에 선택적으로 끼워진다. 하위 산출물은
    #: 상위 계약을 수정하지 않는 것이 원칙이며, 필요한 스테이지에만 둔다.
    #: None이면 대사 노드가 생기지 않는다 — 그 산출물은 다른 것을 고칠 일이 없다는 뜻이고,
    #: 그래프에 빈 노드가 뜨지 않는다.
    reconcile: Callable[[ArchitectureState], dict] | None = None
    #: 검사·수리가 모델을 바꾼 뒤 렌더 직전에 다시 적용할 산출물 구성 규칙.
    finalize: Callable[[ArchitectureState], dict] | None = None


def merge_targeted(
    original: list[dict],
    revised: list[dict],
    key_of: Callable[[dict], str],
    targets: set[str],
) -> list[dict]:
    """대상 항목만 revised 를 쓰고, 나머지는 original 을 **그대로** 둔다.

    바뀔 수 있는 것은 딱 하나다: **대상 항목의 내용.** 목록의 길이도, 순서도, 다른
    항목의 글자 하나도 안 바뀐다. 비대상에 대해서는 LLM 출력을 아예 읽지 않으므로
    "지시를 어기고 다른 데를 고쳤다"가 **구성에 의해 불가능**하다 — 검사해서 막는 게
    아니라 손댈 수 없게 만든다. sanitize(services/*/plantuml.py)와 같은 철학이다.

    **추가를 안 받는 이유.** 이름 변경은 (삭제 + 추가)처럼 보인다. 삭제 쪽은 보호되니
    추가만 통과하면 결국 **원본과 개명본이 둘 다 남는다.** "Order 에 배송지를 넣어줘"가
    Address 스키마를 부르는 정당한 경우도 있지만, 그건 그 스키마를 지목해 따로 요청하면
    된다 — 조용히 늘어나는 것보다 낫다.

    **빠뜨린 것을 삭제로 안 보는 이유.** 대상이 revised 에 없을 때 그것이 "지우라는 뜻"
    인지 "LLM 이 빠뜨린 것"인지 구별할 수 없다. 빠뜨림이 훨씬 흔하므로 원본을 지킨다.
    삭제가 필요하면 그건 다른 조작이어야 한다.
    """
    revised_by_key = {key_of(item): item for item in revised}
    return [
        revised_by_key[key_of(item)]
        if key_of(item) in targets and key_of(item) in revised_by_key
        else item
        for item in original
    ]


def merge_model(
    spec: DesignArtifactSpec,
    original: dict,
    revised: dict,
    targets: set[str],
) -> dict:
    """스펙이 아는 목록마다 merge_targeted 를 적용한다.

    스펙이 `elements` 를 안 갖고 있거나 대상이 비면 revised 를 그대로 쓴다 — 전체 수정
    (기존 피드백 게이트 경로)이라 좁힐 것이 없다는 뜻이다.

    **바탕은 original 이다.** revised 에서 가져오는 것은 스펙이 아는 목록의, 대상 항목뿐.
    api_spec 의 title/version 처럼 스펙이 모르는 필드는 원본을 지킨다 — 지목 대상이
    아닌데 LLM 이 바꿔놓는 자리이기 때문이다(실제로 그랬다).
    """
    if not spec.elements or not targets:
        return revised

    merged = dict(original)
    for list_field, key_of in spec.elements.items():
        # 한 스펙이 레거시와 컬렉션 모델의 서로 다른 목록 필드를 함께 선언할 수 있다.
        # 현재 모델에 없는 필드를 빈 목록으로 새로 만들면 구조화 스키마의 extra 금지와
        # 충돌하므로 실제로 존재하는 목록만 병합한다.
        if list_field not in original and list_field not in revised:
            continue
        merged[list_field] = merge_targeted(
            original.get(list_field) or [],
            revised.get(list_field) or [],
            key_of,
            targets,
        )
    return merged


def assert_untargeted_elements_preserved(
    spec: DesignArtifactSpec,
    original: dict,
    candidate: dict,
    targets: set[str],
) -> None:
    """Fail closed if a targeted revision changed anything outside its scope.

    ``merge_model`` normally makes this true by construction.  Keep this
    independent assertion at the LLM boundary as a regression guard: before a
    deterministic finalizer derives runtime bundle fields, no unrelated
    element or top-level field from the LLM may enter the candidate.  The
    selected elements may change; all other values must remain byte-for-value
    equal to the persisted original model.
    """
    if not targets or not spec.elements:
        return
    if not isinstance(candidate, dict):
        raise TypeError(f"{spec.stage} targeted revision did not return a model object")

    for field_name, original_value in original.items():
        if field_name not in spec.elements and candidate.get(field_name) != original_value:
            raise ValueError(
                f"{spec.stage} targeted revision changed unscoped field {field_name!r}"
            )
    for field_name in candidate:
        if field_name not in original and field_name not in spec.elements:
            raise ValueError(
                f"{spec.stage} targeted revision added unscoped field {field_name!r}"
            )

    for list_field, key_of in spec.elements.items():
        if list_field not in original and list_field not in candidate:
            continue
        before = original.get(list_field) or []
        after = candidate.get(list_field) or []
        if not isinstance(before, list) or not isinstance(after, list):
            raise TypeError(
                f"{spec.stage} targeted revision changed element collection {list_field!r}"
            )
        before_keys = [key_of(item) for item in before]
        after_keys = [key_of(item) for item in after]
        if before_keys != after_keys:
            raise ValueError(
                f"{spec.stage} targeted revision changed unscoped element identities"
            )
        for before_item, after_item, key in zip(before, after, before_keys):
            if key not in targets and before_item != after_item:
                raise ValueError(
                    f"{spec.stage} targeted revision changed unscoped element {key!r}"
                )


def model_elements(spec: DesignArtifactSpec, model: dict) -> list[str]:
    """이 모델 안에서 지목할 수 있는 항목 이름들."""
    return [
        key_of(item)
        for list_field, key_of in spec.elements.items()
        for item in (model.get(list_field) or [])
        if key_of(item)
    ]


def extract_node(spec: DesignArtifactSpec) -> Callable[[ArchitectureState], dict]:
    """앞선 산출물에서 이 산출물의 구조화 모델을 도출한다."""

    def node(state: ArchitectureState) -> dict:
        result = {spec.model_key: spec.extract(state)}
        if spec.stage == "class_diagram" and state.get(
            "class_binding_repair_guidance"
        ):
            # spec.extract returns only after the accepted model passed its
            # typed validation boundary, so consuming guidance here is atomic
            # with checkpointing that accepted model.
            result["class_binding_repair_guidance"] = ""
        if spec.stage == "class_diagram" and state.get(
            "class_binding_source_decision"
        ):
            result["class_binding_source_decision"] = None
        return result

    return node


def revise_node(spec: DesignArtifactSpec) -> Callable[[ArchitectureState], dict]:
    """사용자 피드백을 기준 모델에 적용한다.

    산출물은 convert 노드가 같은 변환으로 재렌더하므로, 피드백이 렌더된 텍스트를 직접
    건드리는 일이 없고 모델과 산출물이 어긋나지 않는다.
    """

    def node(state: ArchitectureState) -> dict:
        # 게이트 피드백은 산출물 전체를 대상으로 한다 — 사용자가 그 산출물을 보면서
        # 말하는 자리라 항목을 좁힐 근거가 없다. 항목을 지목하는 수정은 cascade 가 한다.
        current = state.get(spec.model_key) or {}
        revised = spec.revise(
            current,
            state.get(spec.feedback_key, ""),
            state,
            set(),
        )
        return {spec.model_key: revised}

    return node


def _is_degenerate(spec: DesignArtifactSpec, original: dict, candidate: dict) -> bool:
    """재생성본이 **산출물을 비워서** 위반을 없앤 것인가.

    "위반이 줄었으면 채택"은 그것만으로 불완전하다. **빈 모델은 거의 모든 검사를
    통과하기 때문이다** — 검사할 것이 없으니 위반도 없다. 그래서 재생성이 클래스를 전부
    날려 버리면 위반 수는 0으로 떨어지고, 루프는 그것을 성공으로 채택하며, 상태에는
    `stopped="clean"`이 적힌다. **산출물을 통째로 잃고도 깨끗하다고 보고하게 된다.**

    검출기 중 커버리지 검사가 이것을 우연히 막아 주기는 한다 — 상류 유스케이스를 아무도
    안 가리키게 되므로 위반이 도로 생긴다. 그러나 **입력에 유스케이스 id가 없으면 그
    검사가 아예 안 돌고**(대조할 상류가 없다), 그때 이 함정이 그대로 열린다.

    그래서 위반 수와 별개로 본다: 원래 항목이 있었는데 재생성본에 하나도 없으면 그것은
    수리가 아니다. 항목을 세는 법은 `model_elements`가 이미 안다(스펙의 `elements`).
    """
    if not spec.elements:
        return False
    return bool(model_elements(spec, original)) and not model_elements(spec, candidate or {})


def _sequence_trace_references(model: dict) -> tuple[set[str], set[str]]:
    """시퀀스 모델이 이미 보존하고 있는 유스케이스/단계 참조를 모은다."""
    diagrams = model.get("Diagrams") if isinstance(model, dict) else None
    if not isinstance(diagrams, list):
        diagrams = [model] if isinstance(model, dict) else []
    use_case_ids: set[str] = set()
    step_ids: set[str] = set()
    for diagram in diagrams:
        if not isinstance(diagram, dict):
            continue
        diagram_id = str(diagram.get("use_case_id") or "").strip()
        if diagram_id:
            use_case_ids.add(diagram_id)
        for message in diagram.get("Messages") or []:
            if not isinstance(message, dict):
                continue
            use_case_ids.update(
                str(value).strip()
                for value in message.get("use_case_ids") or []
                if str(value).strip()
            )
            step_ids.update(
                str(value).strip()
                for value in message.get("step_ids") or []
                if str(value).strip()
            )
    return use_case_ids, step_ids


def _loses_sequence_traceability(
    spec: DesignArtifactSpec,
    original: dict,
    candidate: dict,
    state: dict,
) -> bool:
    """수리본이 기존 추적 정보를 삭제해 위반 수만 낮추는 것을 막는다."""
    if spec.stage != "sequence_diagram":
        return False
    original_use_cases, original_steps = _sequence_trace_references(original)
    candidate_use_cases, candidate_steps = _sequence_trace_references(candidate or {})
    known_use_cases = _known_use_case_ids(state)
    known_steps = _known_flow_step_ids(state)
    if known_use_cases:
        original_use_cases &= known_use_cases
    if known_steps:
        original_steps &= known_steps
    return not (
        original_use_cases <= candidate_use_cases
        and original_steps <= candidate_steps
    )


def _finding_key(finding: Finding) -> tuple[str, str, str]:
    return finding.rule_id, finding.location, finding.message


def finding_details(findings: list[Finding], stage: str) -> list[dict[str, Any]]:
    """Serialize structured findings while retaining the legacy display text."""
    details: list[dict[str, Any]] = []
    for finding in findings:
        item = finding.model_dump(mode="json")
        if finding.location:
            item["authority_ref"] = f"{stage}:{finding.location}"
        details.append(item)
    return details


def _repair_finding_keys(findings: list[Finding]) -> tuple[str, ...]:
    return tuple(
        sorted(
            f"{finding.rule_id}|{finding.location}|{finding.message}"
            for finding in findings
        )
    )


def _dedupe_findings(findings: list[Finding]) -> list[Finding]:
    """같은 결함을 한 번만 수리 프롬프트와 수용 판단에 사용한다."""
    unique: list[Finding] = []
    seen: set[tuple[str, str, str]] = set()
    for finding in findings:
        key = _finding_key(finding)
        if key not in seen:
            seen.add(key)
            unique.append(finding)
    return unique


def _repairable_findings(findings: list[Finding]) -> list[Finding]:
    excluded = NON_REPAIRABLE_RULES | LOCAL_REPAIR_ONLY_RULES
    return [
        finding for finding in findings
        if finding.rule_id not in excluded
        and not finding.requires_user_input
        and not _requires_flow_anchor_input(finding)
    ]


def _requires_flow_anchor_input(finding: Finding) -> bool:
    """Return whether a flow-order finding lacks a deterministic branch anchor."""

    message = finding.message.lower()
    return finding.rule_id == "sequence.flow-order" and (
        "no main step" in message
        or ("주 흐름 단계" in finding.message and "없어" in finding.message)
    )


def _unrepaired_stop(findings: list[Finding]) -> str:
    """Classify findings which the generic artifact repair loop must not edit."""

    if not findings:
        return CLEAN
    if any(
        finding.rule_id in NON_REPAIRABLE_RULES
        or finding.requires_user_input
        or _requires_flow_anchor_input(finding)
        for finding in findings
    ):
        return NEEDS_INPUT
    if all(finding.rule_id in LOCAL_REPAIR_ONLY_RULES for finding in findings):
        return CHECKED_ONLY
    return STALLED
def _repair_batch(
    spec: DesignArtifactSpec,
    findings: list[Finding],
    skipped_findings: set[tuple[str, str, str]],
) -> list[Finding]:
    """시퀀스는 구조와 호출 계약을 나눠 최소 수정 후보를 만든다."""
    if spec.stage != "sequence_diagram" and not spec.repair_target_mapper:
        return findings
    eligible = [
        finding for finding in findings
        if _finding_key(finding) not in skipped_findings
    ]
    if spec.stage != "sequence_diagram":
        return eligible
    for rule_ids in _SEQUENCE_REPAIR_RULE_GROUPS:
        batch = [
            finding
            for finding in eligible
            if finding.rule_id in rule_ids
        ]
        if batch:
            return batch
    return [
        finding
        for finding in eligible
    ]


def _sequence_repair_score(findings: list[Finding]) -> tuple[int, ...]:
    """시퀀스 결함의 단계별 점수. 앞 칸이 줄면 뒤 결함이 드러나도 진전이다.

    검출기는 앞 결함 때문에 판정할 수 없는 후행 규칙을 의도적으로 건너뛴다. 따라서
    메서드 결함을 고친 뒤 반환/인자 결함이 새로 나타나는 것은 회귀가 아니라 정상적인
    검증 진행이다. 단순 finding 집합 부분집합 조건은 그 후보를 버렸으므로, 의존 순서에
    따른 사전식 점수를 사용한다. 어느 단계에도 속하지 않은 규칙은 마지막 칸에서 세어
    새 종류의 결함을 공짜로 허용하지 않는다.
    """
    counts = [0] * (len(_SEQUENCE_REPAIR_RULE_GROUPS) + 1)
    phase_by_rule = {
        rule_id: index
        for index, rules in enumerate(_SEQUENCE_REPAIR_RULE_GROUPS)
        for rule_id in rules
    }
    for finding in findings:
        index = phase_by_rule.get(finding.rule_id, len(counts) - 1)
        if finding.rule_id == "sequence.duplicate-consecutive-messages":
            # A detector reports one compact finding per duplicate run. Its
            # count is still material: accepting 36 copies because another
            # unrelated defect disappeared is not a repair.
            match = re.search(r"(\d+)회\s+연달아", str(finding.message))
            counts[index] += int(match.group(1)) if match else 1
        else:
            counts[index] += 1
    return tuple(counts)


def _repair_is_improvement(
    spec: DesignArtifactSpec,
    current: list[Finding],
    candidate: list[Finding],
) -> bool:
    current_keys = {_finding_key(finding) for finding in current}
    candidate_keys = {_finding_key(finding) for finding in candidate}
    if candidate_keys < current_keys:
        return True
    if spec.stage != "sequence_diagram":
        return False
    return _sequence_repair_score(candidate) < _sequence_repair_score(current)


def _sequence_repair_targets(
    spec: DesignArtifactSpec,
    model: dict,
    state: ArchitectureState,
    batch: list[Finding],
) -> set[str]:
    """현재 batch가 속한 유스케이스만 골라 다른 다이어그램을 코드로 보호한다."""
    diagrams = model.get("Diagrams") if isinstance(model, dict) else None
    if not isinstance(diagrams, list):
        return set()
    wanted = {_finding_key(finding) for finding in batch}
    targets: set[str] = set()
    for diagram in diagrams:
        if not isinstance(diagram, dict):
            continue
        use_case_id = str(diagram.get("use_case_id") or "").strip()
        if not use_case_id:
            continue
        local = {_finding_key(finding) for finding in spec.check(diagram, state)}
        if wanted & local:
            targets.add(use_case_id)
            continue
        # 컬렉션 검출기가 만든 누락/중복 finding은 단일 다이어그램 검사에 나타나지
        # 않을 수 있다. 위치가 유스케이스 id를 직접 가리키는 경우에는 그대로 좁힌다.
        if any(
            str(finding.location or "").startswith(use_case_id)
            for finding in batch
        ):
            targets.add(use_case_id)
    return targets


def check_node(spec: DesignArtifactSpec) -> Callable[[ArchitectureState], dict]:
    """모델이 규칙을 지켰는지 판정하고, 어겼으면 자동으로 재생성한다.

    루프가 그래프 노드가 아니라 함수 안에 있는 이유: 이 저장소는 "라우팅은 전부 정적,
    그래프 그림이 곧 실제 흐름"을 원칙으로 한다(`nodes/gates.py`). 반복 엣지를 넣으면
    그림과 흐름이 갈라진다. 반복은 스테이지 **내부의 세부**이므로 여기 있는 것이 맞다.

    **수용 조건은 검증 단계가 전진해야 한다는 것이다.** 보통은 기존 위반의 엄격한
    부분집합이어야 한다. 시퀀스만은 참가자 → 메서드 → 반환 → 인자 → 흐름의 의존 순서를
    사용한다. 앞 결함을 고쳐서 전에 판정할 수 없던 후행 결함이 드러난 후보는 보존하고
    다음 반복에서 이어 고친다. 횟수 상한 대신 같은 상태에서 이미 실패한 전략과 후보를
    반복하지 않는 조건으로 정체를 판단한다.

    **남은 위반을 숨기지 않는다.** 새로운 자동 수리 전략이 없어도 고쳐지지 않은 것은
    그대로 상태에 실린다. `stopped`가 왜 멈췄는지를 들고 가므로 화면은 "위반 0건"과
    "같은 실패를 반복해 멈춤"을 구별할 수 있다.
    """

    def node(state: ArchitectureState) -> dict:
        started = time.perf_counter()
        model = state.get(spec.model_key) or {}
        initial_model_digest = stable_digest(model)
        # A semantic hook may perform one bounded review.  Make its durable,
        # digest-bound result visible to the check in the same invocation so a
        # successful (or failed) review is never executed twice for one gate.
        evidence: dict[str, Any] | None = None
        check_state = state
        if spec.check_evidence is not None:
            evidence = spec.check_evidence(model, state)
            prior = dict(state.get(spec.check_key) or {})
            prior["semanticEvidence"] = evidence
            check_state = {**state, spec.check_key: prior}
        findings = _dedupe_findings(spec.check(model, check_state))
        iterations = 0
        error: str | None = None
        ledger = RepairLedger()
        # 루프를 한 번도 안 돌 수 있다(위반이 없거나 자동 수리 대상이 아님).
        repairable = _repairable_findings(findings) if spec.repair else []
        stopped = CLEAN if not findings else (
            CHECKED_ONLY if spec.repair is None
            else (STALLED if repairable else _unrepaired_stop(findings))
        )
        skipped_findings: set[tuple[str, str, str]] = set()

        diagrams = model.get("Diagrams") if isinstance(model, dict) else None

        log_design_timing(
            "design.model_check.started",
            stage=spec.stage,
            findings_count=len(findings),
            repairable_findings_count=len(repairable),
            repair_policy="progress-or-untried-strategy/v1",
            diagram_count=len(diagrams) if isinstance(diagrams, list) else None,
        )

        while findings and spec.repair is not None:
            repairable = _repairable_findings(findings)
            if not repairable:
                stopped = _unrepaired_stop(findings)
                break
            batch = _repair_batch(spec, repairable, skipped_findings)
            if not batch:
                stopped = STALLED
                ledger.status = "STALLED"
                ledger.stall_reason = "No untried repair batch remains for this artifact state."
                break
            targets = (
                spec.repair_target_mapper(model, state, batch)
                if spec.repair_target_mapper
                else _sequence_repair_targets(spec, model, state, batch)
            )
            if spec.repair_target_mapper and not targets:
                stopped = STALLED
                ledger.status = "STALLED"
                ledger.stall_reason = "No bounded merge target could be resolved for these findings."
                break
            if len(targets) > 1:
                # 같은 종류의 결함이 여러 유스케이스에 있어도 한 번에 하나만 고친다.
                target = min(targets)
                if spec.repair_target_mapper:
                    batch = [
                        finding
                        for finding in batch
                        if target in spec.repair_target_mapper(model, state, [finding])
                    ]
                else:
                    diagram = next(
                        (
                            item
                            for item in (model.get("Diagrams") or [])
                            if isinstance(item, dict)
                            and str(item.get("use_case_id") or "").strip() == target
                        ),
                        {},
                    )
                    local_keys = {
                        _finding_key(finding)
                        for finding in spec.check(diagram, state)
                    }
                    batch = [
                        finding
                        for finding in batch
                        if _finding_key(finding) in local_keys
                    ]
                targets = {target}
            finding_keys_before = _repair_finding_keys(findings)
            input_digest = stable_digest(
                {"model": model, "findings": finding_keys_before}
            )
            strategy_stem = (
                f"rules={','.join(sorted({finding.rule_id for finding in batch}))};"
                f"targets={','.join(sorted(targets)) or 'all'}"
            )
            strategy = next(
                (
                    f"{mode}:{strategy_stem}"
                    for mode in ("targeted", "alternative")
                    if not ledger.strategy_attempted(
                        input_digest=input_digest,
                        finding_keys=finding_keys_before,
                        strategy_key=f"{mode}:{strategy_stem}",
                    )
                ),
                None,
            )
            if strategy is None:
                if spec.stage == "sequence_diagram":
                    skipped_findings.update(_finding_key(finding) for finding in batch)
                    continue
                if spec.repair_target_mapper:
                    # A bounded target can exhaust both strategies without
                    # exhausting the other independent targets in this batch.
                    # Skip only this narrowed batch and let the next loop
                    # select another target before declaring the stage stalled.
                    skipped_findings.update(_finding_key(finding) for finding in batch)
                    continue
                stopped = STALLED
                ledger.status = "STALLED"
                ledger.stall_reason = "All repair strategies were tried for this artifact state."
                break
            iterations += 1
            repair_started = time.perf_counter()
            try:
                log_design_timing(
                    "design.auto_repair.started",
                    stage=spec.stage,
                    iteration=iterations,
                    rule_ids=sorted({finding.rule_id for finding in batch}),
                    target_use_case_ids=sorted(targets),
                    findings_count=len(findings),
                )
                directive = (
                    f"{repair_directive(batch)}\n\n[REPAIR STRATEGY]\n{strategy}\n\n"
                    f"[ACCUMULATED REPAIR HISTORY]\n{ledger.prompt_context()}"
                )
                if spec.repair_batch is not None:
                    revised = spec.repair_batch(model, directive, state, targets, batch)
                    # Batch callbacks own a typed, already-bounded revision
                    # contract; element-name merging would discard operation
                    # changes whose owner is not the use-case id.
                    candidate = revised
                else:
                    revised = spec.repair(model, directive, state, targets)
                    candidate = merge_model(spec, model, revised, targets)
                # 컬렉션이면 finding이 속한 유스케이스만 LLM 출력을 받아들인다. 대상
                # 추론이 불가능한 컬렉션 수준 결함만 기존처럼 전체 수정한다.
            except Exception as exc:  # noqa: BLE001 - 검증 실패가 스테이지를 죽이면 안 된다
                error = f"{type(exc).__name__}: {exc}"
                waiting = transient_llm_error(exc)
                stopped = WAITING_EXTERNAL if waiting else ERROR
                ledger.record(
                    RepairAttempt(
                        stage=f"design.{spec.stage}",
                        target_ids=tuple(sorted(targets)),
                        strategy_key=strategy,
                        input_digest=input_digest,
                        finding_keys_before=finding_keys_before,
                        finding_keys_after=finding_keys_before,
                        outcome="waiting_external" if waiting else "error",
                        detail=error,
                    )
                )
                ledger.status = "WAITING_EXTERNAL" if waiting else "STALLED"
                ledger.stall_reason = error
                log_design_timing(
                    "design.auto_repair.failed",
                    stage=spec.stage,
                    iteration=iterations,
                    elapsed_ms=round((time.perf_counter() - repair_started) * 1000, 1),
                    error_type=type(exc).__name__,
                )
                if waiting:
                    break
                continue
            candidate_findings = _dedupe_findings(spec.check(candidate or {}, state))
            candidate_digest = stable_digest(candidate)
            candidate_keys = _repair_finding_keys(candidate_findings)
            repeated = ledger.candidate_seen(
                input_digest=input_digest,
                candidate_digest=candidate_digest,
            )
            if _is_degenerate(spec, model, candidate) or _loses_sequence_traceability(
                spec, model, candidate, state
            ):
                ledger.record(
                    RepairAttempt(
                        stage=f"design.{spec.stage}",
                        target_ids=tuple(sorted(targets)),
                        strategy_key=strategy,
                        input_digest=input_digest,
                        candidate_digest=candidate_digest,
                        finding_keys_before=finding_keys_before,
                        finding_keys_after=candidate_keys,
                        outcome="no_improvement",
                        detail="degenerate_or_lost_traceability",
                    )
                )
                log_design_timing(
                    "design.auto_repair.completed",
                    stage=spec.stage,
                    iteration=iterations,
                    elapsed_ms=round((time.perf_counter() - repair_started) * 1000, 1),
                    accepted=False,
                    reason="degenerate_or_lost_traceability",
                    candidate_findings_count=len(candidate_findings),
                )
                continue
            improved = not repeated and _repair_is_improvement(
                spec, findings, candidate_findings
            )
            ledger.record(
                RepairAttempt(
                    stage=f"design.{spec.stage}",
                    target_ids=tuple(sorted(targets)),
                    strategy_key=strategy,
                    input_digest=input_digest,
                    candidate_digest=candidate_digest,
                    finding_keys_before=finding_keys_before,
                    finding_keys_after=candidate_keys,
                    outcome=(
                        "repeated_candidate"
                        if repeated
                        else "clean"
                        if improved and not candidate_findings
                        else "improved"
                        if improved
                        else "no_improvement"
                    ),
                )
            )
            if not improved:
                log_design_timing(
                    "design.auto_repair.completed",
                    stage=spec.stage,
                    iteration=iterations,
                    elapsed_ms=round((time.perf_counter() - repair_started) * 1000, 1),
                    accepted=False,
                    reason="repeated_candidate" if repeated else "no_improvement",
                    candidate_findings_count=len(candidate_findings),
                )
                continue
            model, findings = candidate, candidate_findings
            error = None
            skipped_findings.clear()
            log_design_timing(
                "design.auto_repair.completed",
                stage=spec.stage,
                iteration=iterations,
                elapsed_ms=round((time.perf_counter() - repair_started) * 1000, 1),
                accepted=True,
                candidate_findings_count=len(candidate_findings),
            )
            remaining_repairable = _repairable_findings(findings)
            stopped = (
                CLEAN
                if not findings
                else (STALLED if remaining_repairable else _unrepaired_stop(findings))
            )

        # Candidate checks may have repaired the model after the initial
        # semantic hook ran. Refresh digest-bound evidence for that final model
        # and check against it; accepted-unit caching avoids repeating review
        # work already done while evaluating the candidate.
        if spec.check_evidence is not None and stable_digest(model) != initial_model_digest:
            evidence = spec.check_evidence(model, state)
            prior = dict(state.get(spec.check_key) or {})
            check_state = {**state, spec.check_key: {**prior, "semanticEvidence": evidence}}
            findings = _dedupe_findings(spec.check(model, check_state))
            stopped = CLEAN if not findings else (
                STALLED if _repairable_findings(findings) else _unrepaired_stop(findings)
            )
            if findings and spec.repair:
                ledger.status = "NEEDS_INPUT" if stopped == NEEDS_INPUT else "STALLED"
                ledger.stall_reason = "Final digest-bound check left findings after bounded repair."

        if not findings:
            ledger.status = "COMPLETED"

        report: dict[str, Any] = {
            "findings": [f.as_issue() for f in findings],
            "finding_details": finding_details(findings, spec.stage),
            "repair_iters": iterations,
            "stopped": stopped,
            "repair_history": ledger.model_dump(mode="json"),
        }
        if evidence is not None:
            report["semanticEvidence"] = evidence
        if error:
            report["error"] = error
        log_design_timing(
            "design.model_check.completed",
            stage=spec.stage,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
            findings_count=len(findings),
            repair_iters=iterations,
            stopped=stopped,
        )
        return {spec.model_key: model, spec.check_key: report}

    return node


def render_and_validate(
    spec: DesignArtifactSpec,
    model: dict,
    state: ArchitectureState | None = None,
) -> dict[str, Any]:
    """모델 → `{content_key, valid_key, errors_key}`. **렌더와 그 자기검사는 한 벌이다.**

    따로 부를 일이 없어서 한 함수다. 문법 검증은 렌더러가 제 일을 했는지 확인하려고만
    존재하므로, 렌더 없이 검증하거나 검증 없이 렌더하는 것은 둘 다 뜻이 없다.

    순수 함수인 이유는 부르는 곳이 둘이라서다 — 그래프의 `render_node`와 지목 수정
    (`cascade.py`). 예전에는 이 네 줄이 세 곳에 흩어져 있었다.
    """
    if (
        spec.stage == "erd"
        and state is not None
        and erd_disposition(state.get("extracted_bce_classes") or {}, state)
        == "not_applicable"
    ):
        return {
            spec.content_key: spec.empty,
            spec.valid_key: None,
            spec.errors_key: [],
        }
    started = time.perf_counter()
    content = (
        spec.render_with_state(model or {}, state)
        if spec.render_with_state is not None and state is not None
        else spec.render(model or {})
    )
    rendered_at = time.perf_counter()
    validation = spec.validate(content)
    log_design_timing(
        "design.render_validate.completed",
        stage=spec.stage,
        render_ms=round((rendered_at - started) * 1000, 1),
        validation_ms=round((time.perf_counter() - rendered_at) * 1000, 1),
        content_chars=len(str(content or "")),
        syntax_valid=validation["syntax_valid"],
    )
    return {
        spec.content_key: content,
        spec.valid_key: validation["syntax_valid"],
        spec.errors_key: validation["syntax_errors"],
    }


def render_node(spec: DesignArtifactSpec) -> Callable[[ArchitectureState], dict]:
    """모델을 산출물로 렌더하고, **그 산출물이 성한지 스스로 확인한다.**

    시퀀스 최종 판정이 `renderable=false`를 남긴 경우에는 모델과 findings만 수리 경로에
    보존하고 렌더를 생략한다. 그 외 산출물과 승인된 시퀀스는 아래의 결정론 변환을 따른다.

    예전에는 노드가 둘이었다(`convert` → `validate`). 나눠 둔 값이 없었다 — 검증 노드는
    변환 노드의 출력만 보고, 변환이 sanitize로 구성에 의해 유효한 산출물을 내므로
    **원리상 실패할 수 없었다.** 그래서 그래프에 "절대 울리지 않는 노드"가 다섯 개
    떠 있었고, 그림이 실제로 일어나는 일보다 커 보였다.

    합친 지금은 노드가 자기 출력을 자기가 검사한다. 한 가지 책임이다.

    **여기서 실패하면 그것은 LLM이 아니라 우리 변환기의 결함이다.** 그래서 재생성하지
    않는다 — LLM은 우리 렌더러를 고칠 수 없고, 고치라고 시키면 예전 문법 수리 루프가
    그대로 돌아온다(고칠 수 없는 지적에 예산을 태우고 종료 조건이 없다). 값은 상태에
    기록되어 사람에게 간다.

    ## 규칙 검사(`check_node`)와 왜 합치지 않는가

    보는 것도 실패의 뜻도 다르다:

      - `check`  — **렌더 전, 모델**을 본다. 실패 = LLM이 틀렸다 → 재생성이 옳은 대응.
      - `render` — **렌더 후, 산출물**을 본다. 실패 = 우리 코드가 깨졌다 → 재생성은
        정확히 틀린 대응.

    한 목록·한 루프로 합치면 그 둘이 뭉개진다. 시점도 어긋난다 — `check`는 convert
    앞이어야 하고(sanitize가 이름을 뭉개기 전에 봐야 한다), `render`는 그 뒤다.
    """

    def node(state: ArchitectureState) -> dict:
        if (
            spec.stage == "sequence_diagram"
            and state.get("sequence_diagram_renderable") is False
        ):
            # The structured model is still persisted and shown with its findings so
            # it can be repaired.  Emitting PlantUML here would make an invalid model
            # appear approved and would let the generic image endpoint expose it.
            return {
                spec.content_key: spec.empty,
                spec.valid_key: None,
                spec.errors_key: [],
            }
        return render_and_validate(spec, state.get(spec.model_key) or {}, state)

    return node
