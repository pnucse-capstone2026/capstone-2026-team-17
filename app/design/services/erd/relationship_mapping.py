"""관계의 다중도에서 FK 위치·연결 표·식별 관계를 결정론적으로 사상한다.

이 모듈은 이미 만들어진 table을 입력으로 받아 관계 하나씩 외래키와 junction으로 옮긴다.
옮길 수 없는 관계는 이름이나 다중도를 추측하지 않고 기존 Unmapped 사유를 반환한다.
"""
from __future__ import annotations

from app.design.services.common import fields, multiplicity
from app.design.services.erd.table_mapping import (
    _KEY_TYPE,
    KEY_INHERITED,
    KEY_NATURAL,
    _add_column,
    _column,
    _column_names,
    _table,
)

STRUCTURAL_TYPES = ("Association", "Aggregation", "Composition")

UNMAPPED_MULTIPLICITY = "multiplicity-missing"
UNMAPPED_DEPENDENCY = "dependency-between-entities"
UNMAPPED_DUPLICATE_JUNCTION = "duplicate-junction"
UNMAPPED_DUPLICATE_RELATIONSHIP = "duplicate-relationship"
UNMAPPED_MANDATORY_REFERENCE_CYCLE = "mandatory-reference-cycle"

def build_multivalued_child(
    parent: dict,
    field_name: str,
    inner_type: str | None,
    *,
    sql_type: str | None = None,
) -> dict:
    """다중값 필드 하나를 제1정규화로 떼어낸 자식 표.

    Args:
        parent: 상속 처리가 끝나 기본키가 확정된 부모 table이다.
        field_name: 원본 BCE collection field 이름이다.
        inner_type: collection 원소의 Java 타입이다.

    Returns:
        부모 FK와 value column을 가진 기존 1NF child table shape다.

    Notes:
        projection이 모든 관계를 처리한 뒤 호출해야 기존 table·relation 순서를 유지한다.

    **부모의 기본키가 확정된 뒤에 부를 것** — 상속이 그 키를 바꾼다.
    """
    pascal = field_name[0].upper() + field_name[1:] if field_name else "Items"
    child = _table(
        fields.sanitize_entity_name(f"{parent['name']}{pascal}"),
        {"kind": "multivalued", "table": parent["name"], "field": field_name},
    )
    own_key = f"{child['name'].lower()}_id"
    _add_column(child, _column(own_key, _KEY_TYPE, role="pk"))
    child["primaryKey"] = [own_key]
    # **부모 없는 자식 행은 뜻이 없다.** 이 표는 부모의 다중값 필드를 떼어낸 것이므로
    # 부모를 안 가리키는 행은 무엇의 값인지 말할 수 없다. 한동안 기본값(nullable)으로
    # 불렀는데, 그리는 기호는 `||..o{`(자식마다 부모가 정확히 하나)라 **그림과 컬럼이
    # 서로 다른 말을 했다.** 연결 표는 처음부터 필수였고 여기만 빠져 있었다.
    for fk in _foreign_key_columns(parent, child, mandatory=True):
        _add_column(child, fk)
    _add_column(
        child,
        _column(
            f"{field_name}_value",
            sql_type if sql_type is not None else fields.sql_type(inner_type),
        ),
    )
    return child


# ---------------------------------------------------------------------------
# 2) 외래키 컬럼
# ---------------------------------------------------------------------------
def _foreign_key_columns(
    referenced: dict,
    holder: dict,
    unique: bool = False,
    mandatory: bool = False,
    inherited: bool = False,
) -> list[dict]:
    """`referenced`의 기본키를 `holder`가 들고 있을 컬럼들. 복합 기본키면 여러 개가 된다
    — 외래키는 참조되는 키의 **모양을 그대로** 따라야 하고 뭉뚱그릴 방법이 없다.

    `inherited`면 부모의 키 이름을 그대로 쓴다 — 상속에서는 그 칸이 자식의 기본키
    **이기도** 해서, 표 이름을 앞에 붙이면 같은 키가 층마다 다른 이름을 갖게 된다.
    """
    prefix = referenced["name"].lower()
    columns: list[dict] = []
    for pk_name in referenced["primaryKey"]:
        pk_column = next(
            (c for c in referenced["columns"] if c["name"] == pk_name),
            _column(pk_name, _KEY_TYPE),
        )
        if inherited:
            name = pk_name
        else:
            name = pk_name if pk_name.lower().startswith(prefix) else f"{prefix}_{pk_name}"

        # 자기 참조·같은 표를 가리키는 관계들. **칸이 계속 필요한** 자리라 못 버린다.
        wanted, counter = name, 1
        while name in _column_names(holder):
            name = f"related_{wanted}" if counter == 1 else f"related{counter}_{wanted}"
            counter += 1

        columns.append(
            _column(
                name,
                pk_column["type"],
                role="fk",
                references=referenced["name"],
                references_column=pk_name,
                # **복합키는 함께 유일하지 각자 유일하지 않다.** 칸마다 걸면
                # `UNIQUE(a,b)`가 `UNIQUE(a) AND UNIQUE(b)`가 되어 모델에 없는 제약이
                # 생긴다. 한 칸일 때만 컬럼에 적고, 여럿이면 아래에서 표 수준으로 올린다.
                unique=unique and len(referenced["primaryKey"]) == 1,
                mandatory=mandatory,
            )
        )

    if unique and len(columns) > 1:
        holder["uniqueTogether"].append([c["name"] for c in columns])

    return columns


# ---------------------------------------------------------------------------
# 3) 관계 → 외래키 · 연결 테이블
# ---------------------------------------------------------------------------
def relationship_endpoints(
    relationship: dict, tables: dict[str, dict]
) -> tuple[dict | None, dict | None]:
    """관계의 양 끝을 table로 조회한다.

    Args:
        relationship: source·target을 가진 BCE relationship이다.
        tables: sanitize된 이름으로 찾는 Entity table 색인이다.

    Returns:
        source·target table 쌍이며 Entity가 아닌 끝은 ``None``이다.

    Notes:
        이름을 추측하거나 새 table을 만들지 않는다.
    """
    return (
        tables.get(fields.sanitize_entity_name(relationship.get("source", ""))),
        tables.get(fields.sanitize_entity_name(relationship.get("target", ""))),
    )


def _junction(left: dict, right: dict) -> dict:
    """다 : 다 → 연결 테이블. 두 외래키가 함께 복합 기본키가 된다."""
    table = _table(
        fields.sanitize_entity_name(f"{left['name']}{right['name']}"),
        {"kind": "junction", "tables": [left["name"], right["name"]]},
    )
    for referenced in (left, right):
        for fk in _foreign_key_columns(referenced, table, mandatory=True):
            fk["role"] = "pk"
            _add_column(table, fk)
    table["primaryKey"] = [c["name"] for c in table["columns"]]
    return table


def _has_mandatory_reference_path(
    tables: dict[str, dict], start: str, destination: str
) -> bool:
    """현재까지 만든 필수 FK를 따라 ``start``에서 ``destination``으로 갈 수 있는가.

    사상기는 클래스 모델의 다중도를 바꾸지 않는다. 다만 새 필수 FK가 이미 존재하는
    경로를 닫아 물리적으로 삽입 불가능한 스키마를 만들려 하면, 그 관계는 ERD에 억지로
    그리지 않고 ``Unmapped``으로 남긴다. 이 탐색은 사상 순서와 무관하게 이전에 채택된
    모든 필수 참조만 보며, 표 수가 작아 DFS가 가장 단순하고 충분하다.
    """
    pending = [start]
    visited: set[str] = set()
    while pending:
        table_name = pending.pop()
        if table_name == destination:
            return True
        if table_name in visited:
            continue
        visited.add(table_name)
        table = tables.get(table_name)
        if table is None:
            continue
        pending.extend(
            str(column.get("references"))
            for column in table.get("columns") or []
            if column.get("mandatory") and column.get("references")
        )
    return False


def map_relationship(
    relationship: dict, tables: dict[str, dict], junctions: set[str]
) -> tuple[list[dict], list[dict], list[dict]]:
    """관계 하나 → (새 표들, 그려질 선들, 사상 못 한 것들).

    Args:
        relationship: BCE relationship 하나다.
        tables: table/field 단계가 만든 Entity table 색인이다.
        junctions: 앞선 관계가 만든 junction 이름 집합이며 제자리 갱신된다.

    Returns:
        새 junction table, relation line, Unmapped 항목의 기존 배열 shape다.

    Notes:
        입력 관계 하나의 multiplicity·FK·junction만 결정하며 전체 출력 순서는 projection이
        소유한다.

    양 끝이 모두 표일 때만 본다. Boundary·Control이 낀 링크는 행위 흐름이라 **말없이
    지나간다** — 결함이 아니라 정상이다.

    `junctions`는 **이미 만든 연결 표 이름**이고 여기서 채워 나간다. 이름이 두 끝점에서
    나오므로 같은 두 표를 잇는 다 : 다가 둘이면 같은 이름이 두 번 나온다.
    """
    source, target = relationship_endpoints(relationship, tables)
    if not source or not target:
        return [], [], []

    kind = str(relationship.get("type") or "Association")

    # --- 상속: 클래스별 테이블. **모델의 source가 자식이다** -----------------
    # (`class_diagram/plantuml.py`가 `target <|-- source`로 그린다.)
    if kind == "Inheritance":
        if source["name"] == target["name"]:
            return [], [], []
        # **걷어내는 것과 자리를 내주는 것은 다르다.** 대리키는 우리가 붙인 것이니 지우고,
        # 자연키는 모델이 적은 속성이니 칸으로 남긴다(기본키 자리만 내주고 유일성은 유지).
        if source["keyOrigin"] == KEY_NATURAL:
            demoted = [c for c in source["columns"] if c["role"] == "pk"]
            for column in demoted:
                column["role"] = "attribute"
            # **복합 자연키는 함께 유일한 것이지 각자 유일한 것이 아니다.** 칸마다
            # `unique`를 걸면 `UNIQUE(a,b)`가 `UNIQUE(a) AND UNIQUE(b)`가 되어, 모델이
            # 적지 않은 제약을 우리가 얹고 정당한 행을 거부한다. 한 칸일 때만 컬럼에
            # 적고(그림이 안 바뀐다), 여럿이면 표 수준으로 올린다.
            if len(demoted) == 1:
                demoted[0]["unique"] = True
            elif demoted:
                source["uniqueTogether"].append([c["name"] for c in demoted])
        else:
            source["columns"] = [c for c in source["columns"] if c["role"] != "pk"]
        source["primaryKey"] = []
        # **한 번에 앞에 붙인다.** 하나씩 `insert(0, …)` 하면 순서가 뒤집혀서, 부모가
        # `(a, b)`인데 자식은 `(b, a)`가 됐다. 외래키 자체는 칸마다 `referencesColumn`을
        # 들고 있어 어긋나지 않지만, 그림이 부모와 자식을 다르게 보여주고 하류는 그
        # 텍스트로 DDL을 만든다.
        inherited_keys = _foreign_key_columns(target, source, mandatory=True, inherited=True)
        for fk in inherited_keys:
            fk["role"] = "pk"
        source["columns"][:0] = inherited_keys
        source["primaryKey"] = [c["name"] for c in source["columns"] if c["role"] == "pk"]
        source["keyOrigin"] = KEY_INHERITED
        # 상속은 언제나 식별 관계다 — 자식 행은 부모 행 없이 존재할 수 없다.
        return [], [{"source": target["name"], "target": source["name"],
                     "symbol": "||--||", "kind": "inheritance", "identifying": True}], []

    if kind not in STRUCTURAL_TYPES:
        # Entity 둘 사이의 `Dependency`. 종류를 잘못 적은 경우와 구별할 수 없어 드러낸다.
        return [], [], [{"source": source["name"], "target": target["name"],
                         "reason": UNMAPPED_DEPENDENCY, "type": kind}]

    # `0..*`≡`*`, `1..1`≡`1`만 접는다 — **표준이 같다고 정한 것뿐**이고 `n`·`many`는
    # 여전히 모르는 표기다(`common/multiplicity.py`).
    source_multiplicity = multiplicity.normalize(relationship.get("sourceMultiplicity"))
    target_multiplicity = multiplicity.normalize(relationship.get("targetMultiplicity"))
    if not source_multiplicity or not target_multiplicity:
        # **모르는 것을 옮기지 않는다.** 1:N으로 단정하면 그림이 모델에 없는 말을 한다.
        return [], [], [{"source": source["name"], "target": target["name"],
                         "reason": UNMAPPED_MULTIPLICITY,
                         "sourceMultiplicity": str(relationship.get("sourceMultiplicity") or ""),
                         "targetMultiplicity": str(relationship.get("targetMultiplicity") or "")}]

    # 합성은 식별 관계(실선), 연관·집약은 비식별(점선). **정하는 것은 선의 모양뿐이고**
    # 널 허용은 아래에서 다중도가 정한다 — 두 뜻을 한 값에 얹지 않는다.
    identifying = kind == "Composition"
    symbol = (
        multiplicity.crow_left(source_multiplicity)
        + ("--" if identifying else "..")
        + multiplicity.crow_right(target_multiplicity)
    )
    source_many = multiplicity.is_many(source_multiplicity)
    target_many = multiplicity.is_many(target_multiplicity)

    # --- 다 : 다 → 연결 테이블 --------------------------------------------
    if source_many and target_many:
        junction = _junction(source, target)
        # 같은 두 표를 잇는 다 : 다가 이미 있다. **둘째 표를 만들지 않는다** — 이름이
        # 같아 완전히 똑같은 표가 둘 생기고, `erd.table-names-unique`가 그것을 지적해도
        # 연결 표 이름은 사상이 짓는 것이라 **모델이 고칠 수가 없다.** 지적은 나오는데
        # 고칠 방법이 없으면 재생성이 위반 수를 못 줄여 수정본이 통째로 버려진다.
        # 무엇을 하면 되는지 말할 수 있는 사유로 드러낸다.
        if junction["name"] in junctions:
            return [], [], [{"source": source["name"], "target": target["name"],
                             "reason": UNMAPPED_DUPLICATE_JUNCTION,
                             "junction": junction["name"]}]
        junctions.add(junction["name"])
        # 연결 표의 두 외래키는 **언제나** 식별 관계다 — 둘이 합쳐 그 행의 기본키다.
        return (
            [junction],
            [
                {"source": end["name"], "target": junction["name"],
                 "symbol": "||--|{", "kind": "many-to-many", "identifying": True}
                for end in (source, target)
            ],
            [],
        )

    source_needed = multiplicity.is_mandatory(source_multiplicity)
    target_needed = multiplicity.is_mandatory(target_multiplicity)

    if source_many != target_many:
        # 1 : 다 — **다쪽이** 외래키를 든다.
        holder, referenced = (source, target) if source_many else (target, source)
        relation_kind, unique = "one-to-many", False
    else:
        # 1 : 1 — **선택인 쪽이** 든다. 그래야 외래키가 NULL이 될 수 있는 자리와 실제로
        # 선택인 자리가 같아진다. 둘 다 필수(또는 둘 다 선택)면 target이 든다(우리 규약).
        holder, referenced = (
            (source, target) if target_needed and not source_needed else (target, source)
        )
        relation_kind, unique = "one-to-one", True

    # NOT NULL은 **가리켜지는 쪽 끝**의 다중도가 정한다 — `A "1" — "*" B`면 B 하나마다
    # A가 정확히 하나이므로 `B.a_id`는 비울 수 없다.
    mandatory = source_needed if referenced is source else target_needed
    if mandatory and _has_mandatory_reference_path(
        tables, referenced["name"], holder["name"]
    ):
        # 다중도 ``1``을 ``0..1``로 바꾸어 통과시키지 않는다. 어느 관계가 독립적인지,
        # 혹은 둘이 사실 같은 연관의 중복 표기인지 BCE만으로 단정할 수 없기 때문이다.
        # 유효하지 않은 DDL을 내보내는 것보다, 이 링크를 명시적으로 상위 모델의 수정
        # 대상으로 남기는 편이 안전하다.
        return [], [], [{
            "source": source["name"],
            "target": target["name"],
            "reason": UNMAPPED_MANDATORY_REFERENCE_CYCLE,
        }]
    for fk in _foreign_key_columns(referenced, holder, unique=unique, mandatory=mandatory):
        _add_column(holder, fk)
    return [], [{"source": source["name"], "target": target["name"], "symbol": symbol,
                 "kind": relation_kind, "identifying": identifying}], []
