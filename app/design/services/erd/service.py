"""사용자 피드백을 ERD의 기준 BCE Entity 모델에 적용한다.

클래스 다이어그램 reviser와 같은 원리다: LLM은 PlantUML 텍스트를 만지지 않고
구조화된 BCE만 편집하고, ERD는 그 뒤 결정론적 변환(plantuml.generate_erd_from_bce_json)
으로 재렌더된다. 그래서 모델과 다이어그램이 어긋나지 않고 문법 오류도 방지된다.

ERD는 클래스 다이어그램의 BCE와 독립된 자기 사본(erd_bce_classes)을 편집하므로,
ERD 피드백이 클래스 다이어그램을 바꾸지 않는다.
"""
from __future__ import annotations

import json
from typing import Any, Protocol

from app.config import settings
from app.design.knowledge import rules
from app.design.schemas.class_model import BCEModel
from app.design.services.common import fields
from app.design.services.common.structured import focus_note, parse_structured


def _rules_section(stage: str) -> str:
    section = f"\n## Rules\n{rules.generation_prompt_block(stage)}\n"
    if not_rules := rules.non_rules_block(stage):
        section += f"\n## Not rules\n{not_rules}\n"
    return section


class ErdProposalCall(Protocol):
    """공통 structured LLM adapter와 테스트 대역이 따르는 호출 계약이다."""

    def __call__(
        self,
        messages: list[dict[str, str]],
        schema: type[BCEModel],
        **kwargs: Any,
    ) -> dict[str, Any]: ...


def _parse_bce(
    messages: list[dict[str, str]],
    proposal_call: ErdProposalCall | None = None,
) -> BCEModel:
    propose = proposal_call or parse_structured
    parsed = propose(
        messages,
        BCEModel,
        reasoning_effort=settings.design_reasoning_effort,
        max_completion_tokens=settings.design_class_structure_max_completion_tokens,
        operation="ErdEntityRevision",
    )
    model = BCEModel.model_validate(parsed).model_dump(by_alias=True)
    for class_item in model.get("Classes") or []:
        class_item["fields"] = [
            fields.normalize_java_field(value)
            for value in class_item.get("fields") or []
        ]
    for data_type in model.get("DataTypes") or []:
        data_type["fields"] = [
            fields.normalize_java_field(value)
            for value in data_type.get("fields") or []
        ]
    return BCEModel.model_validate(model)


_ERD_REVISION_PREAMBLE = """
You edit the entity model that an ERD is derived from. It is expressed in the
Boundary-Control-Entity (BCE) pattern, but only <<Entity>> classes and the
structural relationships between them become tables and foreign keys. You are
given the current BCE model (as JSON), the use-case specification it was derived
from, and the user's natural-language feedback about the ERD.

Apply the feedback to the model and return the FULL revised model, following the
same schema. How to edit:
- Change only what the feedback asks for; leave everything else intact.
- ERD feedback usually concerns entities, their fields (which become columns),
  and entity-to-entity relationships (which become foreign keys and join tables).
  Add, remove, or rename these to satisfy the feedback.
- Keep the model grounded in the use-case specification — do not invent entities,
  fields, or relationships that the feedback and spec do not support.
- Preserve any <<Boundary>> and <<Control>> classes unchanged; they are not part
  of the ERD but must survive so the shared model stays complete.
- Every relationship's source and target must exist among the returned classes.
- When a reported rule identifies a mandatory foreign-key cycle, resolve the
  cycle from the stated domain semantics: use an optional multiplicity only
  when the relationship can genuinely be absent, or remove/correct the
  contradictory relationship. Never delete an entity merely to hide the cycle.
- Do not duplicate an identical Entity relationship. If two links are merely
  opposite-direction restatements of one association, retain the one that
  expresses the real foreign-key ownership. If they are genuinely distinct
  roles between the same entities, make those roles explicit in the model
  instead of emitting anonymous duplicate associations.
- Keep the traceability fields (use_case_ids) accurate. Carry them over unchanged for
  elements you did not touch; update them for elements you changed; fill them
  in for elements you added. Never invent a reference — an empty list is
  honest, a made-up one is a lie the trace matrix will believe.

## How the model becomes tables

Knowing the mapping is what makes the rules below make sense:
- an <<Entity>> becomes a table;
- `identifier` names the fields that already identify it — leave it empty and a
  surrogate key is added for you;
- a field written `name : Type` becomes a typed column; with no type it becomes
  a column with no type;
- a relationship between two Entities becomes a foreign key when one side is
  single and the other is many, a unique foreign key when both are single, and a
  join table when both are many. Multiplicity is what decides this, so a
  relationship missing one is not mapped at all.
"""

_ERD_REVISION_CLOSING = """
The rules above hold for the model you return, not just for the parts you edited.
A revision that fixes what was asked but breaks a rule elsewhere will be rejected
whole, so re-read them against your full answer before returning it.

Return the revised model strictly according to the provided schema. Do not
include markdown, code fences, or any prose outside the schema fields.
"""

#: 수정 프롬프트. **부르는 곳이 둘이다** — 사용자 피드백과 규칙 위반 재생성 루프
#: (`nodes/artifact.py`의 `check_node`). 후자에게는 규범이 여기 실려 있는 것이 결정적이다:
#: 지적받은 위반을 고치라고 하면서 규칙을 안 주면 모델은 지적 문구를 회피하는 쪽으로
#: 고치고, 그 사이 다른 규칙을 새로 어겨 위반 수가 안 줄면 수정본이 통째로 버려진다.
#:
#: 그래서 규범은 산문이 아니라 지식베이스에서 온다. ERD는 **추출 프롬프트가 없으므로**
#: (모델을 클래스 BCE에서 시드한다) 이것이 규칙을 받는 유일한 자리다.
ERD_BCE_REVISION_SYSTEM_PROMPT = (
    _ERD_REVISION_PREAMBLE + _rules_section(rules.ERD) + _ERD_REVISION_CLOSING
)


def revise_erd_model(
    current_bce: BCEModel,
    feedback: str,
    scenario_text: str = "",
    targets: set[str] | None = None,
    *,
    proposal_call: ErdProposalCall | None = None,
) -> BCEModel:
    """현재 typed BCE에 ERD 피드백을 한 번 적용한다.

    Args:
        current_bce: ``erd_bce_classes``에서 검증한 독립 BCE 사본이다.
        feedback: 사용자 또는 기존 semantic gate가 만든 제한된 수정 지시다.
        scenario_text: entity·field·relationship의 유스케이스 근거다.
        targets: graph가 정한 선택적 수정 대상 집합이다.
        proposal_call: 테스트·adapter가 주입할 선택적 structured proposal 호출이다.

    Returns:
        Java field 표기가 정규화된 전체 ``BCEModel`` 수정 결과다.

    Notes:
        빈 피드백이면 같은 객체를 반환한다. 그 밖에는 기존 reasoning·token cap·operation
        이름과 공통 schema repair 범위를 그대로 사용해 structured 호출을 한 번 수행한다.
    """
    if not feedback:
        return current_bce

    user_content = (
        "[Use Case Specification]\n"
        f"{scenario_text}\n\n"
        "[Current Entity Model (BCE)]\n"
        f"{json.dumps(current_bce.model_dump(by_alias=True), ensure_ascii=False, indent=2)}\n\n"
        "[User Feedback on the ERD]\n"
        f"{feedback}" + focus_note(targets)
    )
    messages = [
        {"role": "system", "content": ERD_BCE_REVISION_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]
    return _parse_bce(messages, proposal_call)
