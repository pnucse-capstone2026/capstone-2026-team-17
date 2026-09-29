"""BCE 추출 결과(JSON)를 클래스 다이어그램 PlantUML로 변환한다.

jar 실행·렌더는 common.plantuml이 맡고, 여기서는 클래스 다이어그램 고유의
"무엇을 그릴지"(스테레오타입·필드·메서드·관계 매핑)만 다룬다.

콘텐츠 문자열(필드·메서드·설명)은 LLM이 만든 자유 텍스트라 PlantUML 구조 문자를
품을 수 있다. sanitize_text가 그것을 중화하므로 이 변환은 "구성에 의해" 항상 문법적으로
유효한 PlantUML을 낸다 — 별도의 문법 수리 루프가 필요 없다.
"""
from __future__ import annotations

import re
from typing import Any

from app.design.schemas.class_model import operation_method_signature
from app.design.services.common import fields, multiplicity

# PlantUML 구조 문자: 멤버/라벨 텍스트에 그대로 들어가면 class 본문을 조기에 닫거나
# 라벨을 깨뜨린다. 의미는 최대한 보존하며 안전한 문자로 바꾼다.
_PUML_UNSAFE = str.maketrans(
    {
        "{": "(",
        "}": ")",
        '"': "'",
        "\n": " ",
        "\r": " ",
    }
)


#: 모델의 관계 종류 → PlantUML 기호. **이 표가 곧 아는 종류의 전부다.**
#:
#: 여기 없는 값은 조용히 `-->`(단순 연관)로 그려진다. 그 조용함이 문제라서
#: `class.relationship-type-known` 규칙이 이 표를 기준으로 판정한다 — 검출기가 이것을
#: import하므로 표를 늘리면 판정도 함께 늘어난다(두 벌로 적지 않는다).
RELATION_SYMBOLS: dict[str, str] = {
    "Inheritance": "<|--",
    "Dependency": "..>",
    "Association": "-->",
    "Aggregation": "o--",
    "Composition": "*--",
}

def sanitize_class_name(name: str) -> str:
    if not name:
        return "UnknownClass"
    return re.sub(r"[^a-zA-Z0-9_]", "_", name)


def sanitize_stereotype(stereotype: str) -> str:
    """스테레오타입은 class 선언의 <<...>>에 들어가므로 단어 문자로만 남긴다."""
    cleaned = stereotype.replace("<", "").replace(">", "").strip()
    return re.sub(r"[^a-zA-Z0-9_ ]", "", cleaned).strip()


def sanitize_text(text: str) -> str:
    """멤버/설명 텍스트를 한 줄의 PlantUML-안전 토큰으로 만든다."""
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    text = text.replace("\u2011", "-")
    return text.translate(_PUML_UNSAFE)


def generate_plantuml_from_bce_json(json_data: dict[str, Any]) -> str:
    """저장 BCE JSON을 구조 관계와 투영 dependency가 포함된 PlantUML로 만든다.

    Args:
        json_data: 기존 alias의 ``BCEModel`` JSON 또는 호환 저장본이다.

    Returns:
        빈 모델이면 빈 문자열, 아니면 문법 문자를 중화한 PlantUML 문서다.

    Notes:
        collaboration 호출 dependency는 표시할 때만 투영하며 저장 ``Relationships``를
        수정하지 않는다. renderer는 의미 검증이나 LLM repair를 수행하지 않는다.
    """
    if not json_data:
        return ""

    classes = json_data.get("Classes", [])
    relationships = json_data.get("Relationships", [])
    data_types = json_data.get("DataTypes", [])

    if not classes and not data_types and not relationships:
        return ""

    puml_lines = [
        "@startuml",
        "allowmixing",
        "!theme plain",
        "skinparam classAttributeIconSize 0",
        "",
    ]

    for class_item in classes:
        raw_name = class_item.get("className", "UnknownClass")
        class_name = sanitize_class_name(raw_name)
        stereotype_raw = class_item.get("stereotype", "")

        clean_stereotype = sanitize_stereotype(stereotype_raw)
        stereo_tag = f" <<{clean_stereotype}>>" if clean_stereotype else ""

        puml_lines.append(f"class {class_name}{stereo_tag} {{")

        for field in class_item.get("fields", []):
            # 기존에 저장된 BCE도 다시 열었을 때 `decimal`을 그대로 보이지 않게 한다.
            # 새 BCE는 extractor에서 이미 정규화되지만, 렌더러는 외부/과거 모델도 받는다.
            clean_field = sanitize_text(fields.normalize_java_field(str(field)))
            if clean_field:
                puml_lines.append(f"  - {clean_field}")

        operation_methods = [
            operation_method_signature(
                str(operation.get("name") or ""),
                [parameter for parameter in operation.get("parameters") or [] if isinstance(parameter, dict)],
                str(operation.get("returnType") or "void"),
            )
            for operation in class_item.get("operations") or []
            if isinstance(operation, dict) and operation.get("name")
        ]
        for method in operation_methods or class_item.get("methods", []):
            # 과거/외부 BCE도 `Integer`·`Decimal` 매개변수나 반환형을 그대로 보이지
            # 않게 한다. 새 BCE는 extractor에서 이미 정규화되지만, 렌더러가 마지막
            # 방어선이다.
            clean_method = sanitize_text(fields.normalize_java_method(str(method)))
            if clean_method:
                puml_lines.append(f"  + {clean_method}")

        puml_lines.append("}")

        puml_lines.append("")

    if data_types:
        puml_lines.append('package "Data Types" {')
        for data_type in data_types:
            if not isinstance(data_type, dict):
                continue
            type_name = sanitize_class_name(str(data_type.get("name") or ""))
            kind = str(data_type.get("kind") or "")
            if kind == "enumeration":
                puml_lines.append(f"  enum {type_name} {{")
                for value in data_type.get("values") or []:
                    clean_value = sanitize_text(str(value))
                    if clean_value:
                        puml_lines.append(f"    {clean_value}")
                puml_lines.append("  }")
                continue
            puml_lines.append(f"  class {type_name} <<ValueObject>> {{")
            for field in data_type.get("fields") or []:
                clean_field = sanitize_text(fields.normalize_java_field(str(field)))
                if clean_field:
                    puml_lines.append(f"    - {clean_field}")
            puml_lines.append("  }")
        puml_lines.append("}")
        puml_lines.append("")

    if json_data.get("Collaborations"):
        # 호출 dependency의 단일 원천은 저장 Collaboration tree다. inventory의 구조 Entity
        # 관계를 대체하지 않고 화면에서만 나란히 투영한다.
        from app.design.services.class_diagram.projections import project_call_dependencies

        relationships = [
            *relationships,
            *project_call_dependencies(json_data),
        ]

    for relationship in relationships:
        source = sanitize_class_name(relationship.get("source", ""))
        target = sanitize_class_name(relationship.get("target", ""))
        relation_type = relationship.get("type", "Association")
        description = relationship.get("description", "")

        if relation_type == "Inheritance":
            # 상속만 방향이 뒤집힌다: PlantUML의 `<|--`는 부모에서 자식으로 그리는데
            # 모델의 source는 자식이다. **다중도는 붙이지 않는다** — 일반화 관계에
            # 다중도를 다는 것은 UML이 아니다.
            line = f"{target} <|-- {source}"
        else:
            puml_symbol = RELATION_SYMBOLS.get(relation_type, "-->")
            line = " ".join(
                part
                for part in (
                    source,
                    multiplicity.label(relationship.get("sourceMultiplicity")),
                    puml_symbol,
                    multiplicity.label(relationship.get("targetMultiplicity")),
                    target,
                )
                if part
            )

        if description:
            clean_description = sanitize_text(description)
            if clean_description:
                line += f" : {clean_description}"

        puml_lines.append(line)

    puml_lines.append("")
    puml_lines.append("@enduml")

    final_puml = "\n".join(puml_lines)
    return final_puml.replace("\xa0", " ").replace("\u200b", "")
