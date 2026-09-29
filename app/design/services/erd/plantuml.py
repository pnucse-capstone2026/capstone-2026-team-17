"""논리 데이터 모델 → ERD PlantUML. **그리기만 한다.**

사상 결정(무엇이 테이블이 되고 외래키가 어디 붙는가)은 전부 `mapping.py`에 있다. 예전에는
그 결정들이 이 파일의 f-string 사이사이에 흩어져 있었고, 그래서 "모든 관계를 1:N으로
단정한다" 같은 것이 결정으로 보이지 않았다.

클래스 다이어그램(`class_diagram.plantuml`)과 같은 성격의 결정론적 변환이다: LLM은 구조화된
모델만 편집하고, 다이어그램 텍스트는 이 함수가 **구성에 의해** 항상 문법적으로 유효한
PlantUML로 재렌더한다. jar 실행·렌더는 `common.plantuml`이 맡는다.

구현 단계는 저장된 `erdBceModel`을 직접 사용한다. 이 PlantUML은 화면과 다운로드용 표현이므로
문구나 줄바꿈이 구현 입력 계약이 되지 않는다.
"""
from __future__ import annotations

from typing import Any

# `sanitize_entity_name`은 여기서 안 쓰지만 다시 내보낸다 — `app/design/rtm.py`가 추적표의
# ERD 항목 이름을 이 이름으로 맞추려고 여기서 가져간다. 그림에 적히는 이름과 추적표가
# 가리키는 이름이 갈라지면 "erd:Order를 고쳐줘"가 통하지 않는다.
from app.design.services.common.fields import sanitize_entity_name  # noqa: F401
from app.design.services.erd.projection import build_logical_model


def _column_line(column: dict[str, Any]) -> str:
    """컬럼 한 줄. **타입이 없으면 안 적는다.**

    예전에는 타입 없는 필드에 `VARCHAR(255)`가 붙었다. 아무도 고르지 않은 타입이 그림에
    사실로 적히고, 하류의 DDL 생성까지 그대로 갔다. 모르면 이름만 적는 편이 정직하다.
    """
    line = "  " + ("* " if column["role"] == "pk" else "") + column["name"]
    if column["type"]:
        line += f" : {column['type']}"
    tags = []
    # **`role`이 아니라 `references`로 판정한다.** 상속된 키와 연결 테이블의 키는 기본키
    # **이면서** 외래키다. `role`은 값이 하나뿐이라 그 둘을 함께 담지 못하는데, 참조가
    # 있다는 사실은 `references`가 따로 들고 있다.
    if column["references"]:
        tags.append("<<FK>>")
    if column["unique"]:
        tags.append("<<unique>>")
    # 기본키는 필수인 것이 당연하므로 안 적는다. 적을 값이 있는 것은 **합성 관계의
    # 외래키**다 — 부분이 전체 없이 존재할 수 없다는 뜻이며 그림에도 그 차이를 보여 준다.
    if column["mandatory"] and column["role"] != "pk":
        tags.append("<<not null>>")
    return (line + (" " + " ".join(tags) if tags else "")).rstrip()


def _table_block(table: dict[str, Any]) -> str:
    """테이블 하나를 사람이 읽을 수 있는 PlantUML entity 블록으로 만든다.

    표 수준 유일 제약(`uniqueTogether`)은 관련 컬럼과 함께 보이도록 블록 안에 적는다.
    """
    lines = [f'entity "{table["name"]}" as {table["name"]} {{']
    keys = [c for c in table["columns"] if c["role"] == "pk"]
    rest = [c for c in table["columns"] if c["role"] != "pk"]
    lines.extend(_column_line(c) for c in keys)
    if keys and rest:
        lines.append("  --")
    lines.extend(_column_line(c) for c in rest)
    for together in table.get("uniqueTogether") or []:
        lines.append("  .. unique (" + ", ".join(together) + ") ..")
    lines.append("}")
    return "\n".join(lines)


def render_logical_model(logical: dict[str, Any]) -> str:
    """논리 데이터 모델 → PlantUML. 테이블이 하나도 없으면 빈 문자열.

    Args:
        logical: ``Tables``·``Relations``·``Unmapped``를 가진 결정론적 projection이다.

    Returns:
        기존 정렬과 annotation을 유지한 ERD PlantUML 문자열이다.

    Notes:
        ``Unmapped``는 그리지 않으며 table과 relation의 입력 순서를 그대로 사용한다.

    **`Unmapped`는 그리지 않는다.** 옮기지 못한 관계를 그림에 주석으로라도 적으면, 그림이
    "이런 관계가 있다"고 말하게 된다. 실제로 있는 것은 관계가 아니라 **모델의 결함**이고,
    그것을 말할 자리는 그림이 아니라 검사 결과다(`knowledge/detectors.py`).
    """
    tables = logical.get("Tables") or []
    if not tables:
        return ""

    lines = ["@startuml", "hide circle", "!theme plain", "", "skinparam linetype ortho", ""]

    for table in tables:
        if table.get("origin", {}).get("kind") == "multivalued":
            continue
        lines.extend([_table_block(table), ""])

    children = [t for t in tables if t.get("origin", {}).get("kind") == "multivalued"]
    if children:
        lines.append("' === 제1정규화(1NF) 분리 테이블 ===")
        for child in children:
            # This is a machine-readable provenance contract for implementation.
            # A 1NF child is a persistence detail, not a missing BCE Entity.  The
            # visible section heading above is intentionally kept for people and
            # older artifacts; this annotation is the stable downstream contract.
            origin = child.get("origin") or {}
            lines.append(
                "' easydep:erd-origin "
                f"kind=multivalued alias={child['name']} "
                f"parent={origin.get('table', '')} field={origin.get('field', '')}"
            )
            lines.extend([_table_block(child), ""])

    for relation in logical.get("Relations") or []:
        lines.append(f"{relation['source']} {relation['symbol']} {relation['target']}")

    lines.extend(["", "@enduml"])
    return "\n".join(lines).replace("\xa0", " ").replace("​", "")


def generate_erd_from_bce_json(json_data: dict[str, Any]) -> str:
    """BCE 모델 → ERD PlantUML. 사상 → 렌더 두 단계.

    **이름과 시그니처를 유지한다** — `app/repositories/artifact_repository.py`가 저장된
    모델에서 다이어그램을 다시 만들 때 이 함수를 부른다.
    """
    return render_logical_model(build_logical_model(json_data or {}))
