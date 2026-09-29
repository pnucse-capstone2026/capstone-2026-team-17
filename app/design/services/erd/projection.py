"""BCE 모델을 논리 데이터 모델로 옮기는 결정론적 단계 조율이다.

table/field 사상은 ``table_mapping``, relationship/FK/junction 사상은
``relationship_mapping``이 소유한다. 이 모듈은 상속 우선 순서, 중복 관계 판정,
1NF 자식 지연 생성과 기존 출력 배열 순서만 조립한다.
"""
from __future__ import annotations

import json
from typing import Any

from app.design.schemas.class_model import BCEModel
from app.design.services.common import multiplicity
from app.design.services.erd.inheritance import order_for_mapping
from app.design.services.erd.relationship_mapping import (
    UNMAPPED_DUPLICATE_RELATIONSHIP,
    build_multivalued_child,
    map_relationship,
    relationship_endpoints,
)
from app.design.services.erd.table_mapping import build_entity_tables


def project_logical_model(bce_model: BCEModel) -> dict[str, Any]:
    """typed BCE 모델을 기존 논리 데이터 모델 shape로 투영한다.

    Args:
        bce_model: 검증이 끝난 ERD 전용 BCE 사본이다.

    Returns:
        ``Tables``, ``Relations``, ``Unmapped`` 배열을 가진 기존 mapping이다.

    Notes:
        LLM·설정·저장소를 사용하지 않는 순수 함수다. table과 relation의 기존 입력 순서,
        junction·1NF 추가 순서 및 Unmapped 누적 순서를 그대로 유지한다.
    """

    return build_logical_model(bce_model.model_dump(by_alias=True))


def build_logical_model(bce: dict[str, Any]) -> dict[str, Any]:
    """기존 BCE dict를 논리 데이터 모델로 결정론적으로 사상한다.

    Args:
        bce: ``erd_bce_classes``의 기존 alias JSON shape다.

    Returns:
        외부 소비자가 사용하던 ``Tables``·``Relations``·``Unmapped`` mapping이다.

    Notes:
        compatibility facade와 checkpoint 재생성을 위해 느슨한 dict 입력 수용을 유지한다.
    """

    if not bce:
        return {"Tables": [], "Relations": [], "Unmapped": []}

    classes = [
        class_item
        for class_item in (bce.get("Classes") or [])
        if isinstance(class_item, dict)
    ]
    relationships = [
        relationship
        for relationship in (bce.get("Relationships") or [])
        if isinstance(relationship, dict)
    ]
    data_types = [
        item for item in (bce.get("DataTypes") or []) if isinstance(item, dict)
    ]
    made, tables, children = build_entity_tables(classes, data_types)

    relations: list[dict] = []
    unmapped: list[dict] = []
    extra: list[dict] = []
    junctions: set[str] = set()
    mappable, rejected = order_for_mapping(relationships)
    seen_relationships: set[tuple[tuple[str, str], ...]] = set()

    for relationship in mappable:
        source_multiplicity = multiplicity.normalize(
            relationship.get("sourceMultiplicity")
        )
        target_multiplicity = multiplicity.normalize(
            relationship.get("targetMultiplicity")
        )
        is_many_to_many = (
            bool(source_multiplicity)
            and bool(target_multiplicity)
            and multiplicity.is_many(source_multiplicity)
            and multiplicity.is_many(target_multiplicity)
        )
        signature = tuple(
            sorted(
                (
                    str(key),
                    json.dumps(
                        value,
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    ),
                )
                for key, value in relationship.items()
            )
        )
        if not is_many_to_many and signature in seen_relationships:
            source, target = relationship_endpoints(relationship, tables)
            if source and target:
                unmapped.append(
                    {
                        "source": source["name"],
                        "target": target["name"],
                        "reason": UNMAPPED_DUPLICATE_RELATIONSHIP,
                    }
                )
            continue
        if not is_many_to_many:
            seen_relationships.add(signature)
        new_tables, new_relations, new_unmapped = map_relationship(
            relationship, tables, junctions
        )
        extra.extend(new_tables)
        relations.extend(new_relations)
        unmapped.extend(new_unmapped)

    for relationship, reason in rejected:
        source, target = relationship_endpoints(relationship, tables)
        if source and target:
            unmapped.append(
                {
                    "source": source["name"],
                    "target": target["name"],
                    "reason": reason,
                }
            )

    for table in made:
        table.pop("_class", None)

    child_tables: list[dict] = []
    for pending in children:
        child = build_multivalued_child(
            pending["table"],
            pending["field"],
            pending["inner"],
            sql_type=pending["innerSqlType"],
        )
        child_tables.append(child)
        relations.append(
            {
                "source": pending["table"]["name"],
                "target": child["name"],
                "symbol": "||..o{",
                "kind": "multivalued",
                "identifying": False,
            }
        )

    return {
        "Tables": made + extra + child_tables,
        "Relations": _dedupe(relations),
        "Unmapped": unmapped,
    }


def _dedupe(relations: list[dict]) -> list[dict]:
    """자기 다대다 등에서 같은 선이 겹쳐도 기존 첫 위치의 선 하나만 남긴다."""

    by_key = {
        (relation["source"], relation["symbol"], relation["target"], relation["kind"]): relation
        for relation in reversed(relations)
    }
    return list(reversed(by_key.values()))
