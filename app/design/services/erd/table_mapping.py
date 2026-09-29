"""BCE Entity와 field를 논리 table·column으로 옮기는 결정론적 사상이다.

관계의 다중도, 외래키 위치, 연결 표는 이 모듈이 결정하지 않는다. 선언 순서, 대리키
충돌, 자연키, Entity 타입 field 제외와 다중값 field의 지연 목록만 소유한다.
"""
from __future__ import annotations

from typing import Any

from app.design.services.common import fields

_KEY_TYPE = "BIGINT"

KEY_NATURAL = "natural"
KEY_SURROGATE = "surrogate"
KEY_INHERITED = "inherited"

def _column(
    name: str,
    type_: str | None,
    role: str = "attribute",
    references: str | None = None,
    references_column: str | None = None,
    unique: bool = False,
    mandatory: bool = False,
) -> dict[str, Any]:
    """컬럼 하나. 참조는 **표와 칸을 둘 다** 적는다 — 외래키 이름이 참조 대상과 다를 수
    있어서(`Copy.book_isbn` → `Book.isbn`) 이름으로 되짚는 것이 성립하지 않는다.
    """
    return {
        "name": name,
        "type": type_,
        "role": role,
        "references": references,
        "referencesColumn": references_column,
        "unique": unique,
        "mandatory": mandatory,
    }


def _table(name: str, origin: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "primaryKey": [],
        "keyOrigin": KEY_SURROGATE,
        "columns": [],
        # 여러 칸이 **함께** 유일한 것. 컬럼의 `unique`는 불리언 하나라 한 칸의 유일성밖에
        # 못 담는데, 상속이 복합 자연키의 기본키 자리를 뺏을 때 담을 곳이 필요하다.
        "uniqueTogether": [],
        "origin": origin,
    }


def _column_names(table: dict) -> set[str]:
    return {c["name"] for c in table["columns"]}


def _add_column(table: dict, column: dict) -> None:
    """**이름이 겹치면 안 붙인다.** 같은 칸이 두 줄 나오면 한 칸인지 두 칸인지 알 수 없다.
    앞의 것이 이긴다 — 대개 기본키이거나 모델이 직접 적은 필드다.
    """
    if column["name"] not in _column_names(table):
        table["columns"].append(column)


# ---------------------------------------------------------------------------
# 1) 클래스 → 테이블
# ---------------------------------------------------------------------------
def build_entity_tables(
    classes: list[dict],
    data_types: list[dict] | None = None,
) -> tuple[list[dict], dict[str, dict], list[dict]]:
    """Entity마다 표 하나 → (만든 표 전부, 이름으로 찾는 표, 제1정규화 자식들).

    Args:
        classes: 저장 BCE의 ``Classes`` 배열이다. Entity 아닌 항목은 무시한다.

    Returns:
        선언 순서의 table 목록, 첫 이름을 보존한 table 색인, 지연된 1NF field 목록이다.

    Notes:
        table/column/key만 만들며 관계 외래키와 junction은 절대 추가하지 않는다.

    **둘을 함께 돌려주는 이유가 이름 충돌이다.** 겹치면 조회용 표에는 하나만 남지만
    목록에는 둘 다 남는다 — 버리거나 고쳐 주면 표가 사라진 것을 아무도 못 보므로 그대로
    두고 `erd.table-names-unique`가 말한다. 끝점은 조회용 표에서 찾아 먼저 선언된 쪽이 이긴다.
    """
    made: list[dict] = []
    tables: dict[str, dict] = {}
    children: list[dict] = []
    named_types = {
        str(item.get("name") or "").strip(): str(item.get("kind") or "").strip()
        for item in data_types or []
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    }

    for class_item in classes:
        if not fields.is_entity(class_item):
            continue
        name = fields.sanitize_entity_name(class_item.get("className", ""))
        table = _table(name, {"kind": "class", "className": class_item.get("className", "")})
        table["_class"] = class_item
        made.append(table)
        tables.setdefault(name, table)

    entity_names = {t["_class"].get("className", "") for t in made}

    for table in made:
        class_item = table["_class"]
        declared = [
            fields.split_field(f)
            for f in class_item.get("fields", [])
            if fields.sanitize_text(f)
        ]

        # --- 기본키 -------------------------------------------------------
        # **칸이 안 남는 필드는 후보가 아니다.** 다중값은 자식 표로 가고, Entity 타입은
        # 관계가 들고 간다. 후보로 두면 `primaryKey`가 실재하지 않는 칸 이름을 담는다
        # (`assert_sound` ②가 그걸 잡는다). 못 쓰는 이유는 `erd.identifier-fields-exist`가 말한다.
        # **선언 순서를 지키는 목록이다. 집합이면 안 된다** — 아래에서 `next()`로 훑어
        # 먼저 걸린 것을 쓰는데, 문자열 집합의 순회 순서는 프로세스 해시 시드에 달려
        # 있다. 한동안 집합이었고, 그래서 `member_id`와 `memberId`를 둘 다 선언한 모델의
        # 기본키가 **실행마다 달라졌다.** 순수 함수라던 것이 실은 아니었고, 저장된 모델에서
        # 매 로드마다 다시 그리므로 모델을 안 고쳐도 그림이 바뀔 수 있었다.
        # 목록이면 "먼저 선언된 것이 이긴다"로 확정된다.
        keyable = [
            n
            for n, raw in declared
            if n and not fields.is_collection(raw) and not fields.names_an_entity(raw, entity_names)
        ]
        identifier = [
            str(i).strip() for i in (class_item.get("identifier") or []) if str(i).strip()
        ]
        # **짝을 지어 본다** — 개수로 비교하면 중복(`["isbn","isbn"]`)에서 키를 버린다.
        matched = {
            i: next((n for n in keyable if fields.squash(n) == fields.squash(i)), None)
            for i in identifier
        }
        natural = list(dict.fromkeys(n for n in matched.values() if n))
        if identifier and all(matched.values()):
            table["keyOrigin"] = KEY_NATURAL
            table["primaryKey"] = natural
        else:
            # `identifier`가 비었거나 쓸 수 없다 — 뒤엣것은 `erd.identifier-fields-exist`가
            # 지적하고, 여기서는 어느 쪽이든 대리키를 붙인다(`keyOrigin`에 남는다).
            surrogate = f"{table['name'].lower()}_id"
            # 그 이름을 선언 필드가 이미 쓰면 **표시만 하고 덮어쓴다.** 자연키로 승격시키는
            # 것은 이름에서 의도를 읽는 것이라 안 한다.
            #
            # **담는 것은 모델이 적은 이름이지 우리가 지은 이름이 아니다.** 한동안
            # `surrogate`를 담았는데, 대소문자만 다른 충돌(`orderId` ↔ `order_id`)에서
            # 지적이 "선언한 `order_id`가 밀려난다"고 말했다 — 모델은 그렇게 안 적었고,
            # 고치라는 이름이 모델 안에 없으니 어디를 고쳐야 할지 알 수가 없었다.
            table["surrogateCollidesWith"] = next(
                (k for k in keyable if fields.squash(k) == fields.squash(surrogate)), None
            )
            _add_column(table, _column(surrogate, _KEY_TYPE, role="pk"))
            table["keyOrigin"] = KEY_SURROGATE
            table["primaryKey"] = [surrogate]

        # --- 일반 컬럼 ----------------------------------------------------
        for field_name, raw_type in declared:
            if not field_name:
                continue
            # 대리키와 의미상 충돌하는(대소문자만 다른) 필드는 대리키로 덮어써진
            # 것이므로 생략한다.
            collision = table.get("surrogateCollidesWith")
            if collision and fields.squash(field_name) == fields.squash(collision):
                continue
            if fields.is_collection(raw_type):
                inner = fields.inner_type(raw_type or "")
                # 원소가 Entity면 컬럼이 아니라 관계다 — 만들면 같은 사실이 두 곳에서 나온다.
                if fields.names_an_entity(inner, entity_names):
                    continue
                # **여기서 만들지 않는다** — 자식이 들고 갈 부모 기본키를 상속이 바꾼다.
                # 무엇을 만들지만 적어 두고 생성은 관계가 끝난 뒤로 미룬다.
                children.append({
                    "table": table,
                    "field": field_name,
                    "inner": inner,
                    "innerSqlType": fields.sql_type(inner, named_types),
                })
                continue
            # 스칼라인데 타입이 Entity다 — **컬렉션과 똑같은 이유로** 컬럼이 아니다.
            # 한동안 여기만 빠져 있어서 `member : Member`가 관계에서 나온 `member_id`
            # 옆에 `member : MEMBER`를 하나 더 만들었다. 같은 사실이 두 칸에 있었고,
            # `MEMBER`는 SQL 타입도 아닌데 하류가 그것으로 DDL을 만들었다.
            # 관계가 없으면 `erd.entity-typed-field-needs-relationship`이 지적한다.
            if fields.names_an_entity(raw_type, entity_names):
                continue
            role = "pk" if field_name in table["primaryKey"] else "attribute"
            _add_column(
                table,
                _column(field_name, fields.sql_type(raw_type, named_types), role=role),
            )

    return made, tables, children
