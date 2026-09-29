"""결정론 검출기 — 규칙 하나에 검출기 하나.

## 무엇을 보는가: 그림이 아니라 모델

검사 대상은 렌더된 PlantUML이 아니라 **BCE 모델**이다. 이유가 셋이다.

  1. **렌더는 정보를 지운다.** `plantuml.py`의 `sanitize_*`가 `Payment Service!`를
     `Payment_Service_`로 만들고, 중괄호를 괄호로 바꾼다. 그림만 보면 LLM이 무엇을 냈는지
     알 수 없다 — 중화된 뒤를 보는 검사는 중화가 숨긴 것을 못 본다.
  2. **지적이 가리킬 이름이 모델에 있다.** 지적의 `location`은 `className`인데, 이것은
     `subgraphs.py`의 `CLASS_DIAGRAM_SPEC.elements`가 쓰는 이름이고 `rtm.py`의 element와도
     같다. 그래야 "class_diagram:Order를 고쳐줘"가 통한다.
  3. **고칠 대상이 모델이다.** 재생성은 `revise_bce_classes`가 모델을 편집한다. 그림을
     보고 지적해 놓고 모델을 고치라고 하면 좌표가 어긋난다.

## 사정거리

여기 있는 것은 **결정론으로 참인 것만**이다. 이름이 중복인지, 참조가 실제로 존재하는지,
스테레오타입 조합이 금지된 것인지. **의미 판정은 여기 없다** — "이 명사가 정말 Entity인가",
"MSS 스텝이 실제로 표현됐는가"는 사람이나 LLM 검증자의 몫이고, 그 규칙은 아직 없다
(`rules.unjudged_defects()`가 비어 있는 것이 지금의 사실이다).

## 검사 순서가 뜻을 갖는다

`stereotype_is_bce`가 `communication_rules`보다 먼저 돈다. 스테레오타입을 못 읽으면 통신
규칙 세 개가 전부 **무판정**이 되는데, 무판정은 겉보기에 통과와 같다. 순서가 그 조용한
실패를 드러낸다: 스테레오타입이 깨졌다는 지적이 먼저 나온다.

## 위반이 가려질 수 있다 — 그리고 그래도 된다

뒤의 검출기가 앞의 것이 이미 지적한 자리를 건너뛰므로, **결함 하나가 다른 결함을 가린다.**
예: `Order`의 스테레오타입이 `Repository`(BCE 밖)이면 `OrderForm → Order`가 Boundary-Entity
직결인지 판정할 수 없어 건너뛴다. 스테레오타입을 `Entity`로 고치고 나서야 그 링크 위반이
드러난다.

의도한 것이다. 한 실수를 지적 둘로 세면 위반 수가 부풀고, 재생성이 실제로 하나를 고쳤는데도
수가 안 줄어 버려진다(`no_improvement`). 대신 **고칠 때마다 새 위반이 나타날 수 있다**는
성질이 생기는데, 그래도 루프는 반드시 멈춘다 — 채택 조건이 "위반 수가 **줄어야** 한다"라
가려졌던 것이 드러나 수가 안 줄면 그 자리에서 멈추고 남은 것을 사람에게 보고한다.
"""
from __future__ import annotations

import re
from collections.abc import Callable

from pydantic import ValidationError

from app.design.contracts.type_system import (
    DesignTypeError,
    parse_type_expression,
    referenced_names,
)
from app.design.schemas.class_model import BCEModel
from app.design.services.api_spec.normalization import interaction_contracts
from app.design.services.class_diagram.scenario import build_scenario_index
from app.design.services.class_diagram.trusted_context import required_value_catalog
from app.design.services.class_diagram.validation.diagram import (
    Finding,
    _broken_stereotypes,
    _class_method_signatures,
    _classes,
    _dangling_endpoints,
    _findings_from_report,
    _relation_label,
    _relationships,
)
from app.design.services.common import fields, multiplicity
from app.design.services.erd import mapping
from app.design.services.persistence_scope import entity_names
from app.design.services.sequence_diagram.methods import (
    method_call_signature,
    method_return_type,
)
from app.design.services.sequence_diagram.validation import (
    _class_names_from_puml,
    _known_use_case_ids,
    _participant_id,
)
from app.design.services.sequence_diagram.validation import (
    _known_flow_step_ids as _sequence_known_flow_step_ids,
)
from app.validation import CheckSpec, ValidationReport, run_checks


def _known_flow_step_ids(state: dict) -> set[str]:
    """API 검증 어댑터가 공유하는 시나리오 단계 식별자를 반환한다."""
    return _sequence_known_flow_step_ids(state)


# ---------------------------------------------------------------------------
# ERD 검출기 — 두 층을 본다
# ---------------------------------------------------------------------------
# 앞의 검출기들은 전부 BCE 모델 하나만 봤다. ERD는 그럴 수 없다: "이 테이블에 기본키가
# 있나", "이 외래키가 실재 테이블을 가리키나"는 **사상 결과**에 대한 질문이고, BCE에는
# 테이블도 키도 없다.
#
# 그래서 여기 있는 것들은 `mapping.build_logical_model()`이 낸 논리 데이터 모델을 받는다.
# 그림을 파싱하지 않는 이유는 앞의 것들과 같다 — 렌더는 정보를 지우고, 우리가 방금 만든
# 문자열을 되읽는 것은 아무것도 확인하지 않는다.
def _tables(logical: dict) -> list[dict]:
    return [t for t in (logical.get("Tables") or []) if isinstance(t, dict)]


def erd_relationship_endpoints(model: dict, logical: dict) -> list[Finding]:
    return _dangling_endpoints(
        model, "erd.relationship-endpoints-exist", "그 관계가 ERD에서 통째로 사라진다"
    )


def erd_stereotype_is_bce(model: dict, logical: dict) -> list[Finding]:
    """`erd_has_entity`보다 **먼저** 돈다 — 딱지가 전부 깨져 표가 0개가 되면 원인은
    하나인데 지적이 둘 나온다. 원인 쪽을 먼저 보여준다.
    """
    return _broken_stereotypes(model, "erd.stereotype-is-bce", "ERD에서 빠진다")


def erd_entity_name_usable(model: dict, logical: dict) -> list[Finding]:
    """딱지가 Entity인 것만 본다 — Boundary·Control은 표가 안 되므로 이름이 비어도 ERD에
    영향이 없고, 그건 클래스 다이어그램 쪽이 볼 일이다.
    """
    rule_id = "erd.entity-name-usable"
    found: list[Finding] = []
    for class_item in _classes(model):
        if not fields.is_entity(class_item):
            continue
        name = str(class_item.get("className") or "")
        if not name.strip():
            found.append(Finding(rule_id, "className이 비어 있음 — 'UnknownEntity'가 된다"))
        elif not fields.sanitize_entity_name(name).strip("_"):
            found.append(
                Finding(rule_id, f"'{name}'은 전부 기호라 표 이름이 될 수 없음", name)
            )
    return found


def _entity_relationships(model: dict, names: set[str] | frozenset[str]) -> set[str]:
    """Return the undirected Entity relationship pairs used by persistence mapping."""

    return {
        " <-> ".join(sorted((str(item.get("source")), str(item.get("target")))))
        for item in _relationships(model)
        if str(item.get("source")) in names and str(item.get("target")) in names
    }


def erd_source_entity_consistency(model: dict, logical: dict) -> list[Finding]:
    """Keep the ERD's domain entities aligned with the accepted class model."""

    source_entities = logical.get("_sourceEntityNames")
    source_relationships = logical.get("_sourceEntityRelationships")
    if not isinstance(source_entities, list) or not isinstance(source_relationships, list):
        return []
    current_entities = entity_names(model)
    expected_entities = frozenset(
        str(name).strip() for name in source_entities if str(name).strip()
    )
    current_relationships = _entity_relationships(model, current_entities)
    expected_relationships = {
        str(value).strip() for value in source_relationships if str(value).strip()
    }
    if (
        current_entities == expected_entities
        and current_relationships == expected_relationships
    ):
        return []
    return [
        Finding(
            "erd.source-entity-consistency",
            "ERD Entity names and relationships must match the accepted class model: "
            f"expectedEntities={sorted(expected_entities)}, "
            f"actualEntities={sorted(current_entities)}, "
            f"expectedRelationships={sorted(expected_relationships)}, "
            f"actualRelationships={sorted(current_relationships)}",
        )
    ]


def erd_has_entity(model: dict, logical: dict) -> list[Finding]:
    """표가 하나라도 있는가.

    클래스 모델이 함께 제공되면 Entity 집합과 관계 일치는
    ``erd_source_entity_consistency``가 맡는다. ERD 적용 여부는 별도의 영속성 계약과
    클래스 모델을 함께 보는 결정에서 정한다. 독립 검증처럼 상류 문맥이 없을 때에만
    기존의 비어 있는 ERD 오류를 유지한다.

    재생성이 모델을 **비워서** 위반을 없애는 길도 이것이 막는다 — 비우면 이 위반이 새로
    생겨 위반 수가 안 줄고 후보가 버려진다. ERD 스펙에는 `elements`가 없어
    `_is_degenerate`가 그 함정을 못 막으므로 막는 것은 이 규칙 하나다.
    """
    if "_sourceEntityNames" in logical:
        return []
    if _tables(logical):
        return []
    return [Finding("erd.has-entity", "<<Entity>> 클래스가 하나도 없어 ERD가 비어 있다")]


#: 사상 못 한 사유 → **사람이 읽을 말.** 빠지면 `str(reason)`이 그대로 나가서
#: `multiple-inheritance` 같은 영어 슬러그가 게이트 화면에 뜬다.
#:
#: **모듈 수준에 있는 것이 요점이다.** 사유는 `mapping.py`에서 늘어나고 문구는 여기서
#: 붙는데, 함수 안에 숨어 있으면 둘이 갈라진 것을 아무도 못 본다. 밖에 있으면
#: `tests/test_erd_check.py`가 `mapping`의 `UNMAPPED_*` 상수를 전수해 이 표와 대조한다.
UNMAPPED_PROSE: dict[str, str] = {
    mapping.UNMAPPED_MULTIPLICITY: "다중도가 없어 사상하지 못했다",
    # `{type}`는 사상이 담아 둔 **실제 관계 종류**로 바뀐다(아래 함수). 이 사유는
    # `Dependency`뿐 아니라 구조적이지 않은 종류 전부를 받으므로, 문구에 종류를 박아 두면
    # `Realization`을 적은 모델에게 "Dependency로 이었다"고 **없는 말을 하게 된다.**
    mapping.UNMAPPED_DEPENDENCY: "Entity 둘을 {type}로 이었다 — 데이터 관계가 아니다",
    mapping.UNMAPPED_MULTIPLE_INHERITANCE: (
        "부모가 둘 이상이다 — 관계형에는 다중 상속이 없어 하나도 옮기지 않았다. "
        "부모를 하나로 줄이거나, 나머지는 연관으로 바꿔라"
    ),
    mapping.UNMAPPED_INHERITANCE_CYCLE: (
        "상속이 순환한다 — 어느 행도 먼저 만들 수 없어 하나도 옮기지 않았다"
    ),
    mapping.UNMAPPED_DUPLICATE_JUNCTION: (
        "같은 두 Entity를 잇는 다대다가 둘 이상이다 — 연결 테이블 이름이 같아져 "
        "둘째는 옮기지 않았다. 하나로 합치거나, 둘을 구별해야 한다면 그 관계를 "
        "Entity로 승격시켜 양쪽과 각각 관계를 맺어라"
    ),
    mapping.UNMAPPED_DUPLICATE_RELATIONSHIP: (
        "동일한 Entity 관계가 두 번 선언되어 둘째를 옮기지 않았다 — 하나를 제거하라. "
        "서로 다른 역할이라면 역할이 드러나는 별도 Entity 또는 관계로 모델링하라"
    ),
    mapping.UNMAPPED_MANDATORY_REFERENCE_CYCLE: (
        "이 관계를 필수 외래키로 옮기면 삽입 불가능한 참조 순환이 된다 — "
        "중복된 역방향 관계를 제거하거나, 실제로 선택적인 끝만 '0..1'로 고쳐라"
    ),
}


def erd_relationships_mapped(model: dict, logical: dict) -> list[Finding]:
    """옮기지 못한 관계가 남아 있는가 — **그림에 없는 관계가 모델에 있다.**

    `{type}`을 사상이 담아 둔 실제 종류로 바꾼다. **`.format`이 아니라 `.replace`다** —
    문구는 사람이 쓰는 자유 텍스트라 언젠가 중괄호가 들어가고, 그때 `.format`은 검사
    노드를 통째로 터뜨린다. 자리표시자가 없는 문구에서 `.replace`는 아무 일도 안 한다.
    """
    return [
        Finding(
            "erd.relationship-mapped",
            UNMAPPED_PROSE.get(item.get("reason"), str(item.get("reason"))).replace(
                "{type}", str(item.get("type") or "Dependency")
            ),
            "{} -> {}".format(item.get("source", "?"), item.get("target", "?")),
        )
        for item in (logical.get("Unmapped") or [])
    ]


def erd_composition_owner(model: dict, logical: dict) -> list[Finding]:
    """**BCE 층에서 본다** — 논리 모델에는 한쪽으로 정리된 결과만 남아 모순이 안 보인다."""
    rule_id = "erd.composition-owner-is-mandatory"
    entities = {c.get("className") for c in _classes(model) if fields.is_entity(c)}

    found: list[Finding] = []
    for relationship in _relationships(model):
        if str(relationship.get("type") or "") != "Composition":
            continue
        if not {relationship.get("source"), relationship.get("target")} <= entities:
            continue
        # source 가 전체(whole)이고 target 이 부분(part)이다 — 사상이 그렇게 읽는다.
        owner = multiplicity.normalize(relationship.get("sourceMultiplicity"))
        if not owner or owner == "1":
            # 빈 값은 다중도 자체가 없는 것이라 `erd.relationship-mapped`가 이미 말한다.
            continue
        found.append(
            Finding(
                rule_id,
                f"합성인데 전체 쪽이 '{owner}'다 — 합성은 부분이 전체 없이 존재할 수 없다는 "
                "뜻이라 전체 쪽은 '1'이어야 한다. 관계 종류를 Association으로 바꾸거나 "
                "다중도를 '1'로 고쳐라",
                _relation_label(relationship),
            )
        )
    return found


def erd_identifier_fields(model: dict, logical: dict) -> list[Finding]:
    """**없는 필드**를 가리키는 것과 있지만 **키가 될 수 없는** 필드(다중값)를 가리키는
    것을 구별해 말한다 — 고치는 쪽이 할 일이 다르다. BCE 층에서 본다(논리 모델에는 이미
    대리키로 떨어진 결과만 남아 있다).
    """
    rule_id = "erd.identifier-fields-exist"
    found: list[Finding] = []
    for class_item in _classes(model):
        if not fields.is_entity(class_item):
            continue
        name = class_item.get("className") or "?"
        declared = {
            field: raw_type
            for field, raw_type in (
                fields.split_field(f) for f in class_item.get("fields") or [] if str(f).strip()
            )
            if field
        }
        for wanted in class_item.get("identifier") or []:
            match = next(
                (d for d in declared if fields.squash(str(wanted)) == fields.squash(d)), None
            )
            if match is None:
                found.append(
                    Finding(rule_id, f"identifier '{wanted}'가 이 Entity의 필드에 없음", name)
                )
            elif fields.is_collection(declared[match]):
                found.append(
                    Finding(
                        rule_id,
                        f"identifier '{wanted}'는 다중값 필드라 키가 될 수 없음 — "
                        "제1정규화로 자식 표에 가므로 이 표에 칸이 안 남는다",
                        name,
                    )
                )
    return found


def erd_surrogate_key_collides(model: dict, logical: dict) -> list[Finding]:
    """사상이 표시해 둔 충돌(`surrogateCollidesWith`)을 지적으로 옮긴다."""
    return [
        Finding(
            "erd.surrogate-key-collides",
            f"우리가 붙이는 대리키와 이름이 같은 필드가 있다 — 선언한 '{collision}'이 밀려난다. "
            "식별자라면 identifier에 적고, 아니라면 이름을 바꿔라",
            str(table.get("name") or "?"),
        )
        for table in _tables(logical)
        if (collision := table.get("surrogateCollidesWith"))
    ]


def erd_table_names_unique(model: dict, logical: dict) -> list[Finding]:
    """테이블 이름이 유일한가 — **사상이 만든 이름(연결 표·1NF 자식)까지 세어서.**"""
    rule_id = "erd.table-names-unique"
    seen: set[str] = set()
    found: list[Finding] = []
    for table in _tables(logical):
        name = str(table.get("name") or "")
        if name in seen:
            kind = table.get("origin", {}).get("kind", "class")
            found.append(Finding(rule_id, f"테이블 이름이 겹친다 (출처: {kind})", name))
        seen.add(name)
    return found


# 기본키 유무와 외래키 참조를 보는 검출기는 **일부러 없다.** 사상이 그 둘을 구성에 의해
# 보장하므로 어떤 모델로도 위반이 안 나오고, 그러면 그 검출기의 "0건"은 아무 정보가
# 아니다. 변환 코드의 조건은 `tests/test_erd_mapping.py`가 확인한다. 매핑이 깨지면 그 테스트가 실패한다.


def erd_entity_typed_field_needs_relationship(model: dict, logical: dict) -> list[Finding]:
    """필드 타입이 Entity인데 그 둘 사이에 관계가 없다 — **사상이 조용히 버리는 자리다.**

    `member : Member`나 `lines : List<OrderLine>`는 컬럼이 아니다. 그 사실을 들고 가는
    것은 관계이고(`mapping.py`의 사상표), 그래서 사상은 컬럼을 안 만든다. 관계까지 없으면
    **모델이 적은 링크가 산출물 어디에도 안 남는다** — 컬럼도 자식 표도 관계선도 없고
    `Unmapped`에도 안 들어간다. 드러날 자리가 여기뿐이다.

    한동안 컬렉션은 버려지고 스칼라는 가짜 컬럼이 됐다(`member : MEMBER` — SQL 타입도
    아닌 것이 하류 DDL까지 갔다). 지금은 둘 다 안 만들고, 둘 다 여기서 말한다.

    **이름이 아니라 타입을 본다.** `erd.fk-from-field-name`이 금지하는 추측과 다른
    일이라는 것은 `fields.names_an_entity`의 docstring에 적어 두었다.
    """
    rule_id = "erd.entity-typed-field-needs-relationship"
    entities = {
        c.get("className"): c for c in _classes(model)
        if fields.is_entity(c) and c.get("className")
    }
    linked: set[frozenset[str]] = {
        frozenset((str(r.get("source")), str(r.get("target"))))
        for r in _relationships(model)
    }

    found: list[Finding] = []
    for name, class_item in entities.items():
        for raw in class_item.get("fields") or []:
            field_name, raw_type = fields.split_field(raw)
            if not field_name or not raw_type:
                continue
            target = fields.referenced_entity(raw_type, entities)
            # 자기 자신을 가리키는 필드도 관계를 요구한다 — 자기 참조는 정상적인 관계다.
            if not target or frozenset((name, target)) in linked:
                continue
            collection = fields.is_collection(raw_type)
            found.append(
                Finding(
                    rule_id,
                    f"'{field_name} : {raw_type}'가 Entity '{target}'를 가리키는데 둘 사이에 "
                    "관계가 없다 — Entity 타입 필드는 컬럼이 되지 않으므로 이대로면 "
                    "ERD에 아무것도 안 남는다. "
                    + (
                        f"'{name}'과 '{target}'을 다중도와 함께 관계로 적어라"
                        if collection
                        else f"'{name}'과 '{target}'을 관계로 적거나, 참조가 아니라면 "
                        "필드 타입을 자료형으로 바꿔라"
                    ),
                    name,
                )
            )
    return found


#: `memberId` · `member_id` · `MemberID` 처럼 뒤에 붙은 식별자 접미사.
_ID_SUFFIX = re.compile(r"[_\s]*id$", re.IGNORECASE)


def erd_reference_like_fields(model: dict, logical: dict) -> list[Finding]:
    """**좁게 건다.** 필드가 `<X>Id` 꼴이고, `X`가 실재 Entity이고, 그 둘 사이에 관계가
    하나도 없을 때만 센다. 관계가 이미 있으면 일부러 적어 둔 칸일 수 있고, 그때 지적하면
    고칠 것이 없는 지적으로 재생성 예산만 태운다.

    **타입이 이미 Entity를 말하는 필드는 건너뛴다** —
    `erd.entity-typed-field-needs-relationship`이 그쪽을 맡는다. `member : Member`처럼
    이름과 타입이 둘 다 걸리는 필드가 있어서, 안 비키면 실수 하나가 지적 둘이 되어
    재생성이 하나를 고쳐도 위반 수가 안 줄고 수정본이 통째로 버려진다.
    """
    rule_id = "erd.field-looks-like-reference"
    entities = {
        c.get("className"): c for c in _classes(model)
        if fields.is_entity(c) and c.get("className")
    }
    linked: set[frozenset[str]] = {
        frozenset((str(r.get("source")), str(r.get("target"))))
        for r in _relationships(model)
    }

    found: list[Finding] = []
    for name, class_item in entities.items():
        for raw in class_item.get("fields") or []:
            field_name, raw_type = fields.split_field(raw)
            if fields.referenced_entity(raw_type, entities):
                continue
            stem = _ID_SUFFIX.sub("", field_name).strip()
            if not stem or fields.squash(stem) == fields.squash(name):
                continue
            target = next(
                (e for e in entities if fields.squash(e) == fields.squash(stem)), None
            )
            if target and frozenset((name, target)) not in linked:
                found.append(
                    Finding(
                        rule_id,
                        f"'{field_name}'가 Entity '{target}'를 이름으로 가리킨다 — "
                        "관계로 적어야 외래키가 된다",
                        name,
                    )
                )
    return found


#: ERD 검출기. 클래스 쪽과 시그니처가 다르다 — 둘째 인자가 상태가 아니라 **논리 데이터
#: 모델**이다. 같은 이름으로 두면 "상태를 받는다"고 읽히고, 실제로 상태를 넘기는 실수가
#: 조용히 통과한다(dict라 아무 오류도 안 난다).
#:
#: **순서가 뜻을 갖는다**: 구조(끝점·딱지·이름) → 산출물이 있는가 → 관계가 옮겨졌는가
#: → 키 → 이름 신호. 구조가 먼저인 이유는 그것이 깨지면 뒤의 것들이 **볼 것이 없어져
#: 조용해지기** 때문이다 — 딱지가 깨져 표가 사라지면 그 표에 대한 어떤 검사도 안 돈다.
ERD_DETECTORS: dict[str, Callable[[dict, dict], list[Finding]]] = {
    "erd_relationship_endpoints": erd_relationship_endpoints,
    "erd_stereotype_is_bce": erd_stereotype_is_bce,
    "erd_entity_name_usable": erd_entity_name_usable,
    "erd_source_entity_consistency": erd_source_entity_consistency,
    "erd_has_entity": erd_has_entity,
    "erd_relationships_mapped": erd_relationships_mapped,
    "erd_composition_owner": erd_composition_owner,
    "erd_identifier_fields": erd_identifier_fields,
    "erd_surrogate_key_collides": erd_surrogate_key_collides,
    "erd_table_names_unique": erd_table_names_unique,
    # 타입 신호가 이름 신호보다 **먼저**다. 앞엣것은 모델이 적은 자료형을 읽는 것이고
    # 뒤엣것은 이름에서 짐작하는 것이라, 둘 다 걸리는 필드에서는 앞엣것이 맡는다.
    "erd_entity_typed_field_needs_relationship": erd_entity_typed_field_needs_relationship,
    "erd_reference_like_fields": erd_reference_like_fields,
}


ERD_CHECKS: tuple[CheckSpec[dict, dict], ...] = (
    CheckSpec("erd.relationship-endpoints-exist", erd_relationship_endpoints),
    CheckSpec("erd.stereotype-is-bce", erd_stereotype_is_bce),
    CheckSpec("erd.entity-name-usable", erd_entity_name_usable),
    CheckSpec("erd.source-entity-consistency", erd_source_entity_consistency),
    CheckSpec("erd.has-entity", erd_has_entity),
    CheckSpec("erd.relationship-mapped", erd_relationships_mapped),
    CheckSpec("erd.composition-owner-is-mandatory", erd_composition_owner),
    CheckSpec("erd.identifier-fields-exist", erd_identifier_fields),
    CheckSpec("erd.surrogate-key-collides", erd_surrogate_key_collides),
    CheckSpec("erd.table-names-unique", erd_table_names_unique),
    CheckSpec(
        "erd.entity-typed-field-needs-relationship",
        erd_entity_typed_field_needs_relationship,
    ),
    CheckSpec("erd.field-looks-like-reference", erd_reference_like_fields),
)


def erd_validation_report(model: dict, state: dict) -> ValidationReport:
    """Return shared validation evidence for one ERD model."""
    logical = mapping.build_logical_model(model or {})
    source = state.get("extracted_bce_classes") if isinstance(state, dict) else None
    if isinstance(source, dict):
        source_entities = entity_names(source)
        logical["_sourceEntityNames"] = sorted(source_entities)
        logical["_sourceEntityRelationships"] = sorted(
            _entity_relationships(source, source_entities)
        )
    return run_checks(ERD_CHECKS, model or {}, logical)


def erd_findings(model: dict, state: dict) -> list[Finding]:
    """ERD 모델 하나에 대한 결정론 검증 전부.

    `state`의 승인된 클래스 모델과 ERD의 Entity 이름·관계 집합을 대조한다. ERD 적용 여부는
    요구사항의 영속성 결정과 클래스 모델을 함께 보는 stage projection이 맡는다.

    사상을 여기서 한 번 돌린다. 렌더가 다시 돌리므로 두 번 도는 셈인데, 순수 함수라
    결과가 같고 캐시를 두면 "언제 무효화하나"가 새 문제가 된다.
    """
    return _findings_from_report(erd_validation_report(model, state))


#: 검출기 이름 → 구현. 이름은 `rules.Rule.detector`가 가리키는 그것이다.
#: 양방향으로 맞물려 있어야 한다 — 선언만 있고 구현이 없거나, 구현만 있고 아무 규칙도
#: 안 쓰는 검출기가 있으면 테스트가 실패한다.
#:
#: **순서가 뜻을 갖는다**: 참조 무결성 → 스테레오타입 → 통신 규칙 → 형태 → 커버리지.
#: 뒤의 검출기들이 앞의 것이 이미 지적한 것을 건너뛰므로(중복 지적 방지), 순서가 곧
#: 어느 지적이 살아남는가다.

def api_path_parameters(model: dict, state: dict) -> list[Finding]:
    found: list[Finding] = []
    for endpoint in model.get("Endpoints", []):
        path = str(endpoint.get("path", ""))
        expected = set(re.findall(r"\{([^{}]+)\}", path))
        actual = {str(item.get("name", "")).strip() for item in endpoint.get("path_params", []) if item.get("name")}
        if expected != actual:
            found.append(Finding("api.path-parameters-match", f"경로 변수 {sorted(expected)}와 path_params {sorted(actual)}가 일치하지 않음", f"{endpoint.get('method', 'get').upper()} {path}"))
    return found


def api_operations_present(model: dict, state: dict) -> list[Finding]:
    """Require a generated API model to contain an implementable operation.

    The implementation pipeline always runs OpenAPI Generator, which rejects a
    schema-only document.  More importantly, a schema without an operation does
    not realize any user-visible system behaviour.  Treat this as a design-model
    defect so the normal API repair loop can regenerate grounded endpoints before
    implementation starts.
    """
    endpoints = model.get("Endpoints") if isinstance(model, dict) else None
    if isinstance(endpoints, list) and any(isinstance(endpoint, dict) for endpoint in endpoints):
        return []
    return [
        Finding(
            "api.operations-present",
            "구현 가능한 API operation이 없음 — 유스케이스·BCE Control·시퀀스 호출에 근거한 endpoint를 생성해야 함",
        )
    ]


def api_schema_references(model: dict, state: dict) -> list[Finding]:
    schemas = {str(item.get("name", "")).strip() for item in model.get("Schemas", []) if item.get("name")}
    found: list[Finding] = []
    for endpoint in model.get("Endpoints", []):
        location = f"{endpoint.get('method', 'get').upper()} {endpoint.get('path', '')}"
        references = [endpoint.get("request_schema", "")] + [item.get("schema_name", "") for item in endpoint.get("responses", [])]
        for reference in references:
            type_name = str(reference or "").strip()
            if not type_name or type_name in schemas:
                continue
            try:
                missing = referenced_names(parse_type_expression(type_name)) - schemas
            except DesignTypeError:
                missing = {type_name}
            if missing:
                found.append(Finding("api.schema-references-exist", f"Schemas에 없는 참조 '{type_name}'", location))
    return found


def api_operation_ids(model: dict, state: dict) -> list[Finding]:
    seen: set[str] = set()
    found: list[Finding] = []
    for endpoint in model.get("Endpoints", []):
        operation_id = str(endpoint.get("operation_id", "")).strip()
        location = f"{endpoint.get('method', 'get').upper()} {endpoint.get('path', '')}"
        if not operation_id:
            found.append(Finding("api.operation-ids-unique", "operation_id가 비어 있음", location))
        elif operation_id in seen:
            found.append(Finding("api.operation-ids-unique", f"중복 operation_id '{operation_id}'", location))
        seen.add(operation_id)
    return found


def api_traceability(model: dict, state: dict) -> list[Finding]:
    classes, use_cases = _class_names_from_puml(state), _known_use_case_ids(state)
    found: list[Finding] = []
    for endpoint in model.get("Endpoints", []):
        location = f"{endpoint.get('method', 'get').upper()} {endpoint.get('path', '')}"
        for source_class in endpoint.get("source_classes", []):
            if source_class and source_class not in classes:
                found.append(Finding("api.references-exist", f"클래스 다이어그램에 없는 source_class '{source_class}'", location))
        if use_cases:
            for use_case in endpoint.get("use_case_ids", []):
                if use_case and use_case not in use_cases:
                    found.append(Finding("api.references-exist", f"입력에 없는 유스케이스 id '{use_case}'", location))
    return found


def _api_location(endpoint: dict) -> str:
    return f"{endpoint.get('method', 'get').upper()} {endpoint.get('path', '')}"


def _control_method_contracts(state: dict) -> dict[str, dict[str, dict[str, object]]]:
    """Return exact BCE Control method contracts keyed by class then method name."""
    controls: dict[str, dict[str, dict[str, object]]] = {}
    for class_item in (state.get("extracted_bce_classes") or {}).get("Classes", []):
        stereotype = str(class_item.get("stereotype", "")).replace("<", "").replace(">", "").strip().lower()
        if stereotype != "control":
            continue
        class_name = str(class_item.get("className") or "").strip()
        if not class_name:
            continue
        methods: dict[str, dict[str, object]] = {}
        for raw in _class_method_signatures(class_item):
            text = str(raw)
            signature = method_call_signature(text)
            if not signature:
                continue
            match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\((.*)\)$", signature)
            if not match:
                continue
            parameters: list[tuple[str, str]] = []
            raw_parameters = match.group(2).strip()
            if raw_parameters:
                for value in raw_parameters.split(","):
                    parameter = value.strip()
                    name, separator, type_name = parameter.partition(":")
                    if not separator or not name.strip() or not type_name.strip():
                        # The sequence/class contract validator reports the
                        # malformed declaration itself. Do not pretend it is
                        # a callable API target here.
                        parameters = []
                        break
                    parameters.append((name.strip(), type_name.strip()))
            methods[match.group(1)] = {
                "signature": signature,
                "parameters": tuple(parameters),
                "returnType": method_return_type(text),
            }
        controls[class_name] = methods
    return controls


def _normalise_contract_type(value: str) -> str:
    """API wire 타입과 BCE 타입의 같은 의미를 한 표기로 맞춘다.

    JSON에서 UUID는 문자열로 전달되고, ``T[]``와 ``array<T>``는 같은 배열이다. 이
    차이는 HTTP 표현 차이일 뿐이므로 LLM에게 API 전체를 다시 쓰게 하지 않는다.
    """
    from app.design.contracts.type_system import wire_type_signature

    token = re.sub(r"\s+", "", value or "")
    if token.casefold() == "date-time":
        return "date-time"
    return wire_type_signature(token)


def _contract_types_compatible(actual: str, expected: str) -> bool:
    """Treat inbound request DTO wrappers as the represented domain type."""
    from app.design.contracts.type_system import DesignTypeError

    try:
        actual_normalized = _normalise_contract_type(actual)
        expected_normalized = _normalise_contract_type(expected)
    except DesignTypeError:
        return False
    if actual_normalized == expected_normalized:
        return True
    # Java date/time values are serialized as ISO strings on the HTTP wire.
    # Treating them as incompatible would reject a valid JSON representation.
    if actual_normalized == "string" and expected_normalized in {
        "uuid", "date", "date-time"
    }:
        return True
    # Only inbound DTO conventions may stand in for a Control parameter.
    # Accepting ``Response`` here would hide a directionally-invalid mapping.
    suffixes = ("createrequest", "updaterequest", "request", "dto")
    def base(value: str) -> str:
        lowered = value.lower()
        for suffix in suffixes:
            if lowered.endswith(suffix) and len(lowered) > len(suffix):
                return lowered[: -len(suffix)]
        return lowered
    return base(actual_normalized) == base(expected_normalized)


def _request_value_types(endpoint: dict, schemas: dict[str, dict]) -> dict[str, str]:
    """Enumerate the only request values an API binding may consume."""
    values: dict[str, str] = {}
    for prefix, key in (("$path.", "path_params"), ("$query.", "query_params")):
        for parameter in endpoint.get(key, []) or []:
            if not isinstance(parameter, dict):
                continue
            name = str(parameter.get("name") or "").strip()
            if name:
                values[prefix + name] = str(parameter.get("type") or "string")
    request_schema = str(endpoint.get("request_schema") or "").strip()
    if request_schema:
        values["$body"] = request_schema
        for field in schemas.get(request_schema, {}).get("fields", []) or []:
            if not isinstance(field, dict):
                continue
            name = str(field.get("name") or "").strip()
            if name:
                values[f"$body.{name}"] = str(field.get("type") or "string")
    return values


def _binding(endpoint: dict) -> dict | None:
    value = endpoint.get("control_binding")
    return value if isinstance(value, dict) else None


def api_control_binding(model: dict, state: dict) -> list[Finding]:
    """Ensure every API operation has a real, reviewable Control target."""
    controls = _control_method_contracts(state)
    found: list[Finding] = []
    for endpoint in model.get("Endpoints", []) or []:
        if not isinstance(endpoint, dict):
            continue
        location = _api_location(endpoint)
        binding = _binding(endpoint)
        if binding is None:
            found.append(Finding(
                "api.control-binding-exists",
                "endpoint에 control_binding이 없음 — 구현할 BCE Control과 메서드를 명시해야 함",
                location,
            ))
            continue
        control = str(binding.get("control") or "").strip()
        method = str(binding.get("method") or "").strip()
        if not control or control not in controls:
            found.append(Finding(
                "api.control-binding-exists",
                f"Control '{control or '<empty>'}'이 BCE의 <<Control>> 클래스로 존재하지 않음",
                location,
            ))
            continue
        if not method or method not in controls[control]:
            found.append(Finding(
                "api.control-binding-exists",
                f"{control}에 '{method or '<empty>'}' 메서드가 정의되어 있지 않음",
                location,
            ))
            continue
        source_classes = {
            str(item).strip() for item in endpoint.get("source_classes", []) or [] if str(item).strip()
        }
        if control not in source_classes:
            found.append(Finding(
                "api.control-binding-exists",
                f"control_binding의 {control}이 source_classes에 추적되지 않음",
                location,
            ))
    return found


def api_control_arguments(model: dict, state: dict) -> list[Finding]:
    """Reject implicit, missing, or type-incompatible HTTP-to-Control values."""
    controls = _control_method_contracts(state)
    schemas = {
        str(item.get("name") or "").strip(): item
        for item in model.get("Schemas", []) or []
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    }
    found: list[Finding] = []
    for endpoint in model.get("Endpoints", []) or []:
        if not isinstance(endpoint, dict):
            continue
        binding = _binding(endpoint)
        if binding is None:
            continue
        control = str(binding.get("control") or "").strip()
        method = str(binding.get("method") or "").strip()
        contract = controls.get(control, {}).get(method)
        if contract is None:
            continue  # api_control_binding owns unknown targets.
        location = _api_location(endpoint)
        expected = dict(contract["parameters"])
        supplied: dict[str, str] = {}
        duplicate: set[str] = set()
        for argument in binding.get("arguments", []) or []:
            if not isinstance(argument, dict):
                continue
            name = str(argument.get("name") or "").strip()
            source = str(argument.get("source") or "").strip()
            if name in supplied:
                duplicate.add(name)
            elif name:
                supplied[name] = source
        if duplicate:
            found.append(Finding(
                "api.control-arguments-match",
                f"Control 인자 {sorted(duplicate)}가 둘 이상 매핑됨",
                location,
            ))
        if set(supplied) != set(expected):
            found.append(Finding(
                "api.control-arguments-match",
                f"{control}.{method} 인자 {sorted(expected)}와 바인딩 인자 {sorted(supplied)}가 일치하지 않음",
                location,
            ))
        available = _request_value_types(endpoint, schemas)
        for name, source in supplied.items():
            if source == f"$context.{name}":
                # The trusted-context provenance rule checks an explicit BCE
                # context# binding.  Its type is the Control parameter's type;
                # it need not be a String (for example, a typed principal).
                continue
            if source not in available:
                found.append(Finding(
                    "api.control-arguments-match",
                    f"'{name}'의 원천 '{source or '<empty>'}'이 선언된 path/query/body 값이 아님",
                    location,
                ))
                continue
            if name not in expected:
                continue
            if not _contract_types_compatible(available[source], expected[name]):
                found.append(Finding(
                    "api.control-arguments-match",
                    f"'{name}'의 원천 타입 {available[source]}이 Control 파라미터 타입 {expected[name]}과 호환되지 않음",
                    location,
                ))
    return found


def _trusted_context_arguments(
    state: dict,
    control: str,
    method: str,
    use_case_ids: set[str],
) -> dict[str, set[str]]:
    """Return accepted server-context values declared by BCE collaboration.

    The API model represents server-owned values as ``$context.<parameter>``.
    A value is trustworthy here only when the accepted Collaboration binds the
    same Control parameter to an exact value# reference in that use case's
    accepted required-value catalog, and that catalog marks it as
    authenticated_actor_context/server_context.
    """
    declared: dict[str, set[str]] = {}
    if not use_case_ids:
        return declared
    scenario = state.get("usecase_spec") or {}
    try:
        index = build_scenario_index(scenario) if isinstance(scenario, dict) else None
    except (TypeError, ValueError):
        index = None
    eligible_sources: dict[str, set[str]] = {}
    if index is not None:
        for use_case in index.use_cases:
            eligible_sources[use_case.id] = {
                item["sourceRef"]
                for item in required_value_catalog(use_case)
                if item["availability"] == "server_context"
            }
    collaborations = (state.get("extracted_bce_classes") or {}).get(
        "Collaborations", []
    )
    for collaboration in collaborations or []:
        if not isinstance(collaboration, dict):
            continue
        collaboration_use_cases = {
            str(item).strip()
            for item in collaboration.get("useCaseIds", []) or []
            if str(item).strip()
        }
        covered_use_cases = use_case_ids & collaboration_use_cases
        if not covered_use_cases:
            continue
        for call in collaboration.get("calls", []) or []:
            if not isinstance(call, dict):
                continue
            target = str(call.get("receiverOperationId") or "").strip()
            if not target.startswith(f"{control}::{method}("):
                continue
            for argument in call.get("argumentBindings", []) or []:
                if not isinstance(argument, dict):
                    continue
                parameter = str(argument.get("parameter") or "").strip()
                source_ref = str(argument.get("sourceRef") or "").strip()
                if parameter:
                    for use_case_id in covered_use_cases:
                        if source_ref in eligible_sources.get(use_case_id, set()):
                            declared.setdefault(parameter, set()).add(use_case_id)
    return declared


def api_trusted_context_provenance(model: dict, state: dict) -> list[Finding]:
    """Require each API trusted-context value to have accepted BCE evidence.

    Requirement policy is reviewed upstream. This API-owned check rejects only
    a concrete projection gap: ``$context.<name>`` without a matching accepted
    collaboration ``context#`` binding.
    """
    found: list[Finding] = []
    for endpoint in model.get("Endpoints", []) or []:
        if not isinstance(endpoint, dict):
            continue
        binding = _binding(endpoint)
        if binding is None:
            continue
        control = str(binding.get("control") or "").strip()
        method = str(binding.get("method") or "").strip()
        if not control or not method:
            continue  # api.control-binding-exists owns incomplete targets.
        use_case_ids = {
            str(item).strip()
            for item in endpoint.get("use_case_ids", []) or []
            if str(item).strip()
        }
        declared = _trusted_context_arguments(
            state, control, method, use_case_ids
        )
        for argument in binding.get("arguments", []) or []:
            if not isinstance(argument, dict):
                continue
            parameter = str(argument.get("name") or "").strip()
            source = str(argument.get("source") or "").strip()
            if not parameter or source != f"$context.{parameter}":
                continue
            if use_case_ids <= declared.get(parameter, set()):
                continue
            found.append(Finding(
                "api.trusted-context-provenance",
                f"'{parameter}'의 trusted context에 모든 endpoint 유스케이스를 덮는 같은 Control context# 인자 근거가 없음",
                _api_location(endpoint),
            ))
    return found


def api_accepted_interactions_covered(model: dict, state: dict) -> list[Finding]:
    """Check that every accepted Boundary→Control interaction has an API endpoint.

    The class stage owns the collaboration's meaning and arguments.  This API
    stage only checks that its generated interaction ID, Control target, and
    use-case coverage survived HTTP projection.
    """
    raw_bce = state.get("extracted_bce_classes")
    if not isinstance(raw_bce, dict) or not raw_bce.get("Collaborations"):
        return []
    try:
        contracts = interaction_contracts(BCEModel.model_validate(raw_bce))
    except ValidationError:
        # Invalid BCE data is owned by class validation, not this API rule.
        return []

    found: list[Finding] = []
    for contract in contracts:
        covered: set[str] = set()
        for endpoint in model.get("Endpoints", []) or []:
            if not isinstance(endpoint, dict):
                continue
            if str(endpoint.get("interaction_id") or "").strip() != contract.interaction_id:
                continue
            binding = _binding(endpoint)
            if (
                binding is None
                or str(binding.get("control") or "").strip() != contract.control_class
                or str(binding.get("method") or "").strip() != contract.control_method
                or contract.boundary_class not in (endpoint.get("source_classes") or [])
            ):
                continue
            covered.update(
                str(item).strip()
                for item in endpoint.get("use_case_ids", []) or []
                if str(item).strip()
            )
        for use_case_id in contract.use_case_ids:
            if use_case_id not in covered:
                found.append(Finding(
                    "api.accepted-interactions-covered",
                    f"승인된 Boundary→Control 상호작용의 {use_case_id}에 대응하는 API endpoint가 없음",
                    contract.interaction_id,
                ))
    return found


def api_control_outcomes(model: dict, state: dict) -> list[Finding]:
    """Require a named Control result for every documented HTTP result."""
    controls = _control_method_contracts(state)
    public_returns: dict[str, str] = {}
    raw_bce = state.get("extracted_bce_classes")
    if isinstance(raw_bce, dict) and raw_bce.get("Collaborations"):
        try:
            public_returns = {
                item.interaction_id: item.boundary_return_type
                for item in interaction_contracts(BCEModel.model_validate(raw_bce))
            }
        except ValidationError:
            # Class validation owns malformed BCE input.  Keep the legacy
            # Control-only checks available for partially saved models.
            pass
    found: list[Finding] = []
    for endpoint in model.get("Endpoints", []) or []:
        if not isinstance(endpoint, dict):
            continue
        binding = _binding(endpoint)
        if binding is None:
            continue
        control = str(binding.get("control") or "").strip()
        method = str(binding.get("method") or "").strip()
        contract = controls.get(control, {}).get(method)
        if contract is None:
            continue
        location = _api_location(endpoint)
        public_return_type = _normalise_contract_type(
            public_returns.get(str(endpoint.get("interaction_id") or ""), "")
        )
        successful_responses = [
            response
            for response in endpoint.get("responses", []) or []
            if isinstance(response, dict)
            and 200 <= int(response.get("status", 0) or 0) < 300
        ]
        if public_return_type and public_return_type != "void" and successful_responses and all(
            int(response.get("status", 0) or 0) == 204
            and not str(response.get("schema_name") or "").strip()
            for response in successful_responses
        ):
            found.append(Finding(
                "api.control-outcomes-cover-responses",
                "Boundary 공개 반환 타입 "
                f"'{public_returns.get(str(endpoint.get('interaction_id') or ''), '')}'은 "
                "응답 본문이 필요한데 모든 성공 응답이 204 No Content임",
                location,
            ))
        documented = {
            int(item.get("status"))
            for item in endpoint.get("responses", []) or []
            if isinstance(item, dict) and str(item.get("status", "")).isdigit()
        }
        outcomes: dict[int, str] = {}
        duplicate: set[int] = set()
        for outcome in binding.get("outcomes", []) or []:
            if not isinstance(outcome, dict) or not str(outcome.get("status", "")).isdigit():
                continue
            status = int(outcome["status"])
            if status in outcomes:
                duplicate.add(status)
            outcomes[status] = str(outcome.get("outcome") or "").strip()
        if documented != set(outcomes) or any(not value for value in outcomes.values()) or duplicate:
            found.append(Finding(
                "api.control-outcomes-cover-responses",
                f"문서화한 응답 {sorted(documented)}과 Control outcome {sorted(outcomes)}이 일치하지 않거나 outcome 이름이 비어 있음",
                location,
            ))
        for response in endpoint.get("responses", []) or []:
            if not isinstance(response, dict):
                continue
            status = int(response.get("status", 0) or 0)
            if 200 <= status < 300 and status != 204 and not str(response.get("schema_name") or "").strip():
                found.append(Finding(
                    "api.control-outcomes-cover-responses",
                    f"{status} 성공 응답에 schema_name이 없어 생성 코드가 Object 응답으로 약화됨",
                    location,
                ))
                break
        # Error statuses can be raised by a void command without becoming its
        # return value.  A concrete Control result is required only when the
        # endpoint promises a successful response body (or a non-204 success).
        needs_result = any(
            200 <= int(response.get("status", 0) or 0) < 300
            and (
                int(response.get("status", 0) or 0) != 204
                or bool(str(response.get("schema_name") or "").strip())
            )
            for response in endpoint.get("responses", []) or []
            if isinstance(response, dict)
        )
        return_type = _normalise_contract_type(str(contract.get("returnType") or ""))
        if needs_result and return_type in {"", "void", "object", "any", "map", "dict"}:
            found.append(Finding(
                "api.control-outcomes-cover-responses",
                f"{control}.{method}의 반환 타입 '{contract.get('returnType') or '<none>'}'은 문서화한 결과를 구분할 수 없음",
                location,
            ))
        for response in endpoint.get("responses", []) or []:
            if not isinstance(response, dict) or not (
                200 <= int(response.get("status", 0) or 0) < 300
            ):
                continue
            schema_name = str(response.get("schema_name") or "").strip()
            if not schema_name or return_type in {
                "", "void", "object", "any", "map", "dict",
            }:
                continue
            is_array = bool(response.get("is_array"))
            is_collection = return_type.endswith("[]")
            if is_array != is_collection:
                found.append(Finding(
                    "api.control-outcomes-cover-responses",
                    f"{control}.{method} 반환 타입 '{contract.get('returnType')}'과 성공 응답 schema '{schema_name}'의 배열 여부가 일치하지 않음",
                    location,
                ))
                continue
            primitive_return = return_type in {
                "string", "integer", "number", "boolean", "char",
                "byte", "short", "long", "float", "double",
            }
            if not is_array and primitive_return:
                if _normalise_contract_type(schema_name) != return_type:
                    found.append(Finding(
                        "api.control-outcomes-cover-responses",
                        f"{control}.{method} 반환 타입 '{contract.get('returnType')}'이 성공 응답 schema '{schema_name}'과 일치하지 않음",
                        location,
                    ))
            elif is_array:
                element = return_type[:-2].split(".")[-1]
                if element and element != _normalise_contract_type(schema_name):
                    found.append(Finding(
                        "api.control-outcomes-cover-responses",
                        f"{control}.{method} 요소 타입 '{element}'이 성공 응답 schema '{schema_name}'과 일치하지 않음",
                        location,
                    ))
            break
    return found


def api_executable_schema_fields(model: dict, state: dict) -> list[Finding]:
    """HTTP에서 실제 사용하는 schema가 구현 가능한 모양인지 확인한다.

    BCE enumeration은 field 대신 values를 가지므로 유효하다. 둘 다 없는 named object와
    item 타입이 빠진 array/object만 API 단계의 자동 수리 대상으로 돌려보낸다.
    """

    primitive = {
        "string", "integer", "number", "boolean", "uuid", "localdate",
        "localdatetime", "localtime", "instant", "byte",
    }
    used: set[str] = set()
    found: list[Finding] = []
    for endpoint in model.get("Endpoints", []) or []:
        if not isinstance(endpoint, dict):
            continue
        request_schema = str(endpoint.get("request_schema") or "").strip()
        if request_schema:
            used.add(request_schema)
        used.update(
            str(response.get("schema_name") or "").strip()
            for response in endpoint.get("responses", []) or []
            if isinstance(response, dict)
            and str(response.get("schema_name") or "").strip()
            and str(response.get("schema_name") or "").strip().casefold()
            not in primitive
        )
        for parameter in [
            *(endpoint.get("path_params") or []),
            *(endpoint.get("query_params") or []),
        ]:
            if not isinstance(parameter, dict):
                continue
            type_name = str(parameter.get("type") or "").strip()
            if type_name.casefold() in {"array", "object"}:
                found.append(Finding(
                    "api.executable-schema-fields",
                    f"파라미터 '{parameter.get('name')}'의 '{type_name}'에 구체적인 값 타입이 없음",
                    _api_location(endpoint),
                ))
    schemas = {
        str(schema.get("name") or "").strip(): schema
        for schema in model.get("Schemas", []) or []
        if isinstance(schema, dict) and str(schema.get("name") or "").strip()
    }
    for name in sorted(used):
        schema = schemas.get(name)
        if schema is None:  # 존재 여부는 api.schema-references-exist가 보고한다.
            continue
        if not schema.get("fields") and not schema.get("values"):
            found.append(Finding(
                "api.executable-schema-fields",
                "요청이나 응답 값을 표현할 field 또는 enum value가 없음",
                name,
            ))
            continue
        for field in schema.get("fields") or []:
            if isinstance(field, dict) and str(field.get("type") or "").casefold() in {
                "array", "object",
            }:
                found.append(Finding(
                    "api.executable-schema-fields",
                    f"field '{field.get('name')}'의 타입에 구체적인 값 타입이 없음",
                    name,
                ))
    return found


def _sequence_diagrams_for_api(state: dict) -> list[dict]:
    model = state.get("sequence_diagram_model") or {}
    diagrams = model.get("Diagrams") if isinstance(model, dict) else None
    if isinstance(diagrams, list):
        return [item for item in diagrams if isinstance(item, dict)]
    return [model] if isinstance(model, dict) else []


def api_control_sequence(model: dict, state: dict) -> list[Finding]:
    """Prove the API binding is represented by an executable sequence path."""
    controls = _control_method_contracts(state)
    diagrams = _sequence_diagrams_for_api(state)
    found: list[Finding] = []
    for endpoint in model.get("Endpoints", []) or []:
        if not isinstance(endpoint, dict):
            continue
        binding = _binding(endpoint)
        if binding is None:
            continue
        control = str(binding.get("control") or "").strip()
        method = str(binding.get("method") or "").strip()
        contract = controls.get(control, {}).get(method)
        if contract is None:
            continue
        expected_signature = str(contract["signature"])
        endpoint_use_cases = {
            str(item).strip() for item in endpoint.get("use_case_ids", []) or [] if str(item).strip()
        }
        matches = False
        decomposed_path = False
        for diagram in diagrams:
            participant_classes = {
                _participant_id(participant): str(
                    participant.get("source_class") or participant.get("name") or ""
                ).strip()
                for participant in diagram.get("Participants", []) or []
                if isinstance(participant, dict)
            }
            for message in diagram.get("Messages", []) or []:
                if not isinstance(message, dict) or str(message.get("type", "sync")).lower() not in {"sync", "async", "self"}:
                    continue
                if participant_classes.get(str(message.get("target") or "").strip()) != control:
                    continue
                message_use_cases = {
                    str(item).strip() for item in message.get("use_case_ids", []) or [] if str(item).strip()
                }
                if endpoint_use_cases and not (endpoint_use_cases & message_use_cases):
                    continue
                candidate_signature = method_call_signature(
                    str(message.get("label") or "")
                )
                if candidate_signature == expected_signature:
                    matches = True
                    break
                candidate_match = re.match(
                    r"([A-Za-z_][A-Za-z0-9_]*)\(.*\)$", candidate_signature
                )
                expected_match = re.match(
                    r"([A-Za-z_][A-Za-z0-9_]*)\(.*\)$", expected_signature
                )
                if not candidate_match or not expected_match:
                    continue
                candidate_contract = controls.get(control, {}).get(
                    candidate_match.group(1)
                )
                if candidate_contract is None:
                    continue
                expected_tokens = set(re.findall(
                    r"[a-z]+",
                    re.sub(
                        r"([a-z])([A-Z])", r"\1 \2", expected_match.group(1)
                    ).lower(),
                ))
                candidate_tokens = set(re.findall(
                    r"[a-z]+",
                    re.sub(
                        r"([a-z])([A-Z])", r"\1 \2", candidate_match.group(1)
                    ).lower(),
                ))
                return_compatible = _contract_types_compatible(
                    str(candidate_contract.get("returnType") or ""),
                    str(contract.get("returnType") or ""),
                )
                if expected_tokens & candidate_tokens and return_compatible:
                    decomposed_path = True
            if matches:
                break
        if not matches and not decomposed_path:
            found.append(Finding(
                "api.control-call-in-sequence",
                f"{control}의 endpoint 유스케이스 경로에 Control 호출이 없음",
                _api_location(endpoint),
            ))
    return found

API_SPEC_DETECTORS: dict[str, Callable[[dict, dict], list[Finding]]] = {
    "api_operations_present": api_operations_present,
    "api_path_parameters": api_path_parameters,
    "api_schema_references": api_schema_references,
    "api_executable_schema_fields": api_executable_schema_fields,
    "api_operation_ids": api_operation_ids,
    "api_traceability": api_traceability,
    "api_control_binding": api_control_binding,
    "api_control_arguments": api_control_arguments,
    "api_trusted_context_provenance": api_trusted_context_provenance,
    "api_accepted_interactions_covered": api_accepted_interactions_covered,
    "api_control_outcomes": api_control_outcomes,
    "api_control_sequence": api_control_sequence,
}


API_SPEC_CHECKS: tuple[CheckSpec[dict, dict], ...] = (
    CheckSpec("api.operations-present", api_operations_present),
    CheckSpec("api.path-parameters-match", api_path_parameters),
    CheckSpec("api.schema-references-exist", api_schema_references),
    CheckSpec("api.executable-schema-fields", api_executable_schema_fields),
    CheckSpec("api.operation-ids-unique", api_operation_ids),
    CheckSpec("api.references-exist", api_traceability),
    CheckSpec("api.control-binding-exists", api_control_binding),
    CheckSpec("api.control-arguments-match", api_control_arguments),
    CheckSpec("api.trusted-context-provenance", api_trusted_context_provenance),
    CheckSpec("api.accepted-interactions-covered", api_accepted_interactions_covered),
    CheckSpec("api.control-outcomes-cover-responses", api_control_outcomes),
    CheckSpec("api.control-call-in-sequence", api_control_sequence),
)

def api_spec_validation_report(model: dict, state: dict) -> ValidationReport:
    """Return shared validation evidence for one API specification model."""
    return run_checks(API_SPEC_CHECKS, model or {}, state or {})


def api_spec_findings(model: dict, state: dict) -> list[Finding]:
    return _findings_from_report(api_spec_validation_report(model, state))
