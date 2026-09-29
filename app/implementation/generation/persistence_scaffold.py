"""ERD의 typed Entity 모델에서 Spring Data persistence 골격을 만든다.

이 모듈은 테이블 이름이나 Java 필드를 LLM에 다시 묻지 않는다. ERD 단계가 확정한
Entity 이름, 필드, 식별자와 타입만 사용해 JPA Entity, Repository와 첫 migration을
항상 같은 내용으로 만든다. ERD에 없는 null 허용 여부나 관계의 외래 키 소유자는
추측하지 않는다.
"""

from __future__ import annotations

import re
from typing import Any

from app.design.contracts.type_system import DesignTypeError, sql_type_for_design
from app.design.schemas.class_model import AcceptedBCEClass, BCEModel

from .java_scaffold import java_type

PERSISTENCE_SCAFFOLDER_VERSION = "1.5.0"

_FIELD = re.compile(r"^\s*[+#~\-]?\s*(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)\s*:\s*(?P<type>.+?)\s*$")
_JAVA_IMPORTS = {
    "BigDecimal": "java.math.BigDecimal",
    "BigInteger": "java.math.BigInteger",
    "Instant": "java.time.Instant",
    "LocalDate": "java.time.LocalDate",
    "LocalDateTime": "java.time.LocalDateTime",
    "LocalTime": "java.time.LocalTime",
    "OffsetDateTime": "java.time.OffsetDateTime",
    "ZonedDateTime": "java.time.ZonedDateTime",
    "Collection": "java.util.Collection",
    "List": "java.util.List",
    "Optional": "java.util.Optional",
    "Set": "java.util.Set",
    "UUID": "java.util.UUID",
}


def persistence_repository_fqcns(
    model: BCEModel,
    base_package: str,
    *,
    logical_model: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Return repository types for exactly the Entity set rendered by persistence."""

    return {
        entity.class_name: f"{base_package}.persistence.repository.{entity.class_name}Repository"
        for entity in _persistence_entities(model, logical_model)
    }
# MySQL과 테스트용 H2가 공통으로 문법 토큰으로 해석하는 대표적인 SQL 단어다. ERD의
# Java 필드 이름은 바꾸지 않고, 실제 DDL/JPA 열 이름에 ``_value``를 붙여 두 DB에서 같은
# 이름으로 조회되게 한다. DB별 인용 부호와 대소문자 규칙에 의존하지 않는다.
_SQL_RESERVED_WORDS = {
    "add",
    "all",
    "alter",
    "and",
    "as",
    "asc",
    "between",
    "by",
    "case",
    "check",
    "column",
    "constraint",
    "create",
    "database",
    "default",
    "delete",
    "desc",
    "distinct",
    "drop",
    "else",
    "end",
    "exists",
    "false",
    "foreign",
    "from",
    "group",
    "having",
    "in",
    "index",
    "inner",
    "insert",
    "into",
    "is",
    "join",
    "key",
    "left",
    "like",
    "limit",
    "not",
    "null",
    "on",
    "or",
    "order",
    "outer",
    "primary",
    "references",
    "right",
    "select",
    "set",
    "table",
    "then",
    "true",
    "union",
    "unique",
    "update",
    "user",
    "values",
    "when",
    "where",
}


def render_persistence_scaffold(
    model: BCEModel,
    base_package: str,
    *,
    logical_model: dict[str, Any] | None = None,
) -> dict[str, str]:
    """애플리케이션 root 기준의 persistence 파일을 경로별로 반환한다.

    ``model``은 도메인 이름과 enum/value object 타입을 제공한다. ``logical_model``은
    ERD 단계가 확정한 실제 테이블·컬럼·기본키를 제공한다. 후자를 받은 경우 대리키와
    외래키처럼 BCE 원본에는 없지만 ERD 투영에서 생긴 컬럼도 골격에 포함한다.
    """

    entities = sorted(
        _persistence_entities(model, logical_model),
        key=lambda item: item.class_name,
    )
    if not entities:
        return {}

    declared_types = {
        *(item.class_name for item in model.Classes),
        *(item.name for item in model.DataTypes),
    }
    enum_types = {item.name for item in model.DataTypes if item.kind == "enumeration"}
    value_types = {item.name for item in model.DataTypes if item.kind == "valueObject"}
    package_path = base_package.replace(".", "/")
    files: dict[str, str] = {}
    for entity in entities:
        fields, identifiers, text_identifier_names = _persistence_fields(
            entity, declared_types, value_types
        )
        entity_package = f"{base_package}.persistence.entity"
        entity_path = (
            f"src/main/java/{package_path}/persistence/entity/{entity.class_name}Entity.java"
        )
        key_type = identifiers[0][1]
        if len(identifiers) > 1:
            key_type = f"{entity.class_name}Key"
            files[f"src/main/java/{package_path}/persistence/entity/{key_type}.java"] = (
                _render_composite_key(entity_package, key_type, identifiers)
            )
        files[entity_path] = _render_entity(
            entity,
            fields,
            identifiers,
            base_package,
            enum_types,
            value_types,
            text_identifier_names,
        )
        files[
            f"src/main/java/{package_path}/persistence/repository/"
            f"{entity.class_name}Repository.java"
        ] = _render_repository(entity.class_name, key_type, base_package)

    files["src/main/resources/db/migration/V1__initial_schema.sql"] = _render_migration(
        entities, declared_types, enum_types, value_types
    )
    return dict(sorted(files.items()))


def _persistence_entities(
    model: BCEModel,
    logical_model: dict[str, Any] | None,
) -> list[AcceptedBCEClass]:
    """논리 테이블을 기존 persistence renderer가 읽는 작은 Entity 목록으로 바꾼다.

    실제 구현 경로는 항상 논리 모델을 넘긴다. 값이 없을 때 BCE Entity를 그대로 쓰는
    경로는 renderer를 독립적으로 호출하는 도구와 기존 단위 테스트를 위한 기본 동작이다.
    """

    original = {item.class_name: item for item in model.Classes if item.stereotype == "Entity"}
    tables = logical_model.get("Tables") if isinstance(logical_model, dict) else None
    if not isinstance(tables, list):
        return list(original.values())

    entities: list[AcceptedBCEClass] = []
    for table in tables:
        if not isinstance(table, dict) or not str(table.get("name") or "").strip():
            continue
        table_name = str(table["name"]).strip()
        origin = table.get("origin") if isinstance(table.get("origin"), dict) else {}
        source_name = str(origin.get("className") or table_name).strip()
        source = original.get(source_name)
        source_fields = _declared_field_types(source.fields if source else [])
        columns = table.get("columns") if isinstance(table.get("columns"), list) else []
        # ERD 투영은 기존 ``studentId`` 필드 옆에 관계용 ``student_id``를 만들 수 있다.
        # 두 이름은 Java에서는 다르지만 DB에서는 모두 ``student_id``가 되므로 그대로
        # 렌더링하면 JPA와 CREATE TABLE에 같은 열이 두 번 생긴다. 같은 DB 열끼리는 원래
        # BCE 필드 이름과 타입을 우선하고, 그런 필드가 없으면 ERD의 첫 열을 사용한다.
        physical_columns: dict[str, dict[str, Any]] = {}
        for column in columns:
            if not isinstance(column, dict):
                continue
            column_name = str(column.get("name") or "").strip()
            if not column_name:
                continue
            database_name = _snake_case(column_name)
            current = physical_columns.get(database_name)
            if current is None or (
                column_name in source_fields and str(current.get("name") or "") not in source_fields
            ):
                physical_columns[database_name] = column
        physical_fields = [
            f"{column['name']} : {_physical_field_type(column, source_fields)}"
            for column in physical_columns.values()
        ]
        selected_names = {
            database_name: str(column["name"]) for database_name, column in physical_columns.items()
        }
        identifiers = list(
            dict.fromkeys(
                selected_names.get(_snake_case(str(name).strip()), str(name).strip())
                for name in (table.get("primaryKey") or [])
                if str(name).strip()
            )
        )
        entities.append(
            AcceptedBCEClass(
                className=table_name,
                stereotype="Entity",
                description=source.description if source else "Physical ERD table.",
                fields=physical_fields,
                use_case_ids=list(source.use_case_ids) if source else [],
                identifier=identifiers,
                operations=list(source.operations) if source else [],
            )
        )
    return entities


def _declared_field_types(declarations: list[str]) -> dict[str, str]:
    """BCE 원본에 있던 필드는 도메인 타입 이름을 그대로 보존한다."""

    result: dict[str, str] = {}
    for declaration in declarations:
        match = _FIELD.fullmatch(declaration)
        if match is not None:
            result[match.group("name")] = match.group("type")
    return result


def _physical_field_type(column: dict[str, Any], source_fields: dict[str, str]) -> str:
    """물리 FK에는 도메인 Entity 타입이 아니라 투영된 키의 스칼라 타입을 사용한다.

    BCE는 관계를 ``studentId : Student``처럼 표현할 수 있지만 ERD는 이를
    ``student_id VARCHAR`` 같은 외래 키 열로 확정한다. 여기서 BCE 타입을 다시 복원하면
    JPA 골격이 존재하지 않는 ``Student`` 영속 타입을 참조하게 된다. 일반 속성의 enum과
    value object 타입은 기존처럼 보존하고, 관계 열만 ERD의 SQL 타입을 따른다.
    """

    is_foreign_key = (
        str(column.get("role") or "").strip().casefold() == "fk"
        or bool(str(column.get("references") or "").strip())
    )
    if is_foreign_key:
        return _design_type_from_sql(column.get("type"))
    return source_fields.get(
        str(column.get("name") or ""),
        _design_type_from_sql(column.get("type")),
    )


def _design_type_from_sql(value: Any) -> str:
    """ERD의 작은 SQL 타입 집합을 Java 골격용 타입으로 옮긴다."""

    sql_type = str(value or "").strip().upper()
    if sql_type.startswith("VARCHAR") or sql_type in {"CHAR", "TEXT"}:
        return "String"
    if sql_type.startswith("DECIMAL") or sql_type.startswith("NUMERIC"):
        return "BigDecimal"
    return {
        "BIGINT": "Long",
        "INT": "Integer",
        "INTEGER": "Integer",
        "BOOLEAN": "Boolean",
        "UUID": "UUID",
        "DATE": "LocalDate",
        "TIMESTAMP": "LocalDateTime",
        "TIME": "LocalTime",
        "BLOB": "byte[]",
        "LONGBLOB": "byte[]",
        "JSON": "Object",
    }.get(sql_type, "Object")


def _entity_fields(
    entity: AcceptedBCEClass,
    declared_types: set[str],
) -> list[tuple[str, str, str]]:
    """설계 필드를 이름, Java 타입, 원래 타입 순서로 읽는다."""

    fields: list[tuple[str, str, str]] = []
    for declaration in entity.fields:
        match = _FIELD.fullmatch(declaration)
        if match is None:
            raise ValueError(f"{entity.class_name} has an invalid field declaration: {declaration}")
        name = match.group("name")
        design_type = match.group("type")
        fields.append((name, java_type(design_type, declared_types=declared_types), design_type))
    return fields


def _identifier_fields(
    entity: AcceptedBCEClass,
    fields: list[tuple[str, str, str]],
) -> list[tuple[str, str, str]]:
    """ERD가 명시한 식별자를 실제 필드와 연결한다."""

    by_name = {name: (name, field_type, source) for name, field_type, source in fields}
    identifiers = [by_name[name] for name in entity.identifier if name in by_name]
    missing = [name for name in entity.identifier if name not in by_name]
    if missing:
        raise ValueError(f"{entity.class_name} identifier fields are missing: {', '.join(missing)}")
    if not identifiers:
        raise ValueError(f"{entity.class_name} requires at least one ERD identifier")
    return identifiers


def _persistence_fields(
    entity: AcceptedBCEClass,
    declared_types: set[str],
    value_types: set[str],
) -> tuple[
    list[tuple[str, str, str]],
    list[tuple[str, str, str]],
    set[str],
]:
    """관계형 DB가 기본 키로 저장할 수 있는 필드 계약을 만든다.

    일반 값 객체와 알 수 없는 설계 타입은 본문 필드라면 JSON으로 보존할 수 있다. 하지만
    MySQL은 JSON/BLOB 열을 기본 키로 허용하지 않는다. 그런 식별자만 인덱싱 가능한 문자열
    저장 표현으로 바꾸고, 원래 설계 타입은 주석에 남겨 업무 코드가 변환 책임을 알 수 있게
    한다. 임의 UUID나 ``null`` 값을 만들어 의미를 추측하지는 않는다.
    """
    original = _entity_fields(entity, declared_types)
    original_identifiers = _identifier_fields(entity, original)
    text_identifier_names = {
        name
        for name, field_type, _source in original_identifiers
        if _is_json_type(field_type, value_types) or field_type == "byte[]"
    }
    fields = [
        (name, "String" if name in text_identifier_names else field_type, source)
        for name, field_type, source in original
    ]
    return fields, _identifier_fields(entity, fields), text_identifier_names


def _render_entity(
    entity: AcceptedBCEClass,
    fields: list[tuple[str, str, str]],
    identifiers: list[tuple[str, str, str]],
    base_package: str,
    enum_types: set[str],
    value_types: set[str],
    text_identifier_names: set[str],
) -> str:
    """한 ERD Entity를 독립적으로 사용할 수 있는 JPA class로 만든다."""

    package = f"{base_package}.persistence.entity"
    identifier_names = {name for name, _field_type, _source in identifiers}
    imports = {
        "jakarta.persistence.Column",
        "jakarta.persistence.Entity",
        "jakarta.persistence.Id",
        "jakarta.persistence.Table",
    }
    if len(identifiers) > 1:
        imports.add("jakarta.persistence.IdClass")
    for _name, field_type, _source in fields:
        imports.update(_imports_for_type(field_type, base_package, enum_types | value_types))
        if _is_enum(field_type, enum_types):
            imports.update({"jakarta.persistence.EnumType", "jakarta.persistence.Enumerated"})
        elif _is_json_type(field_type, value_types):
            imports.update(
                {"org.hibernate.annotations.JdbcTypeCode", "org.hibernate.type.SqlTypes"}
            )
        elif field_type == "byte[]":
            imports.add("jakarta.persistence.Lob")

    lines = [
        f"package {package};",
        "",
        *(f"import {name};" for name in sorted(imports)),
        "",
        "/** JPA persistence object generated from the ERD fields and identifiers. */",
        "@Entity",
        f'@Table(name = "{_table_name(entity.class_name)}")',
    ]
    if len(identifiers) > 1:
        lines.append(f"@IdClass({entity.class_name}Key.class)")
    lines.append(f"public class {entity.class_name}Entity {{")

    for name, field_type, source in fields:
        lines.append("")
        if name in identifier_names:
            lines.append("    @Id")
        if name in text_identifier_names:
            lines.append(
                f"    // Design identifier `{source}` is stored as an indexable string key."
            )
        if _is_enum(field_type, enum_types):
            lines.append("    @Enumerated(EnumType.STRING)")
        elif _is_json_type(field_type, value_types):
            if field_type == "Object":
                lines.append(
                    f"    // Design type `{source}` is stored as JSON because it has no Java scalar mapping."
                )
            lines.append("    @JdbcTypeCode(SqlTypes.JSON)")
        elif field_type == "byte[]":
            lines.append("    @Lob")
        column_parts = [f'name = "{_database_identifier(_snake_case(name))}"']
        if name in identifier_names:
            column_parts.extend(["nullable = false", "updatable = false"])
        if _is_json_type(field_type, value_types):
            column_parts.append('columnDefinition = "json"')
        elif field_type == "BigDecimal":
            column_parts.extend(["precision = 19", "scale = 4"])
        lines.append(f"    @Column({', '.join(column_parts)})")
        lines.append(f"    private {field_type} {name};")

    lines.extend(["", f"    public {entity.class_name}Entity() {{}}"])
    parameters = ", ".join(f"{field_type} {name}" for name, field_type, _ in fields)
    lines.extend(["", f"    public {entity.class_name}Entity({parameters}) {{"])
    lines.extend(f"        this.{name} = {name};" for name, _field_type, _source in fields)
    lines.append("    }")
    for name, field_type, _source in fields:
        method_name = name[:1].upper() + name[1:]
        lines.extend(
            [
                "",
                f"    public {field_type} get{method_name}() {{",
                f"        return {name};",
                "    }",
                "",
                f"    public void set{method_name}({field_type} {name}) {{",
                f"        this.{name} = {name};",
                "    }",
            ]
        )
    lines.append("}")
    return "\n".join(lines) + "\n"


def _render_repository(entity_name: str, key_type: str, base_package: str) -> str:
    imports = [
        f"{base_package}.persistence.entity.{entity_name}Entity",
        "org.springframework.data.jpa.repository.JpaRepository",
    ]
    if "." not in key_type and key_type not in {
        "Boolean",
        "Integer",
        "Long",
        "Object",
        "String",
        "byte[]",
    }:
        if key_type == f"{entity_name}Key":
            imports.append(f"{base_package}.persistence.entity.{key_type}")
        elif key_type in _JAVA_IMPORTS:
            imports.append(_JAVA_IMPORTS[key_type])
        elif key_type not in {"BigDecimal", "LocalDate", "LocalDateTime", "LocalTime"}:
            imports.append(f"{base_package}.bce.{key_type}")
    if key_type in _JAVA_IMPORTS and _JAVA_IMPORTS[key_type] not in imports:
        imports.append(_JAVA_IMPORTS[key_type])
    return (
        f"package {base_package}.persistence.repository;\n\n"
        + "".join(f"import {name};\n" for name in sorted(set(imports)))
        + "\n/** Basic persistence contract for the generated JPA entity. */\n"
        + f"public interface {entity_name}Repository "
        + f"extends JpaRepository<{entity_name}Entity, {key_type}> {{}}\n"
    )


def _render_composite_key(
    package: str,
    key_name: str,
    fields: list[tuple[str, str, str]],
) -> str:
    imports = {"java.io.Serializable", "java.util.Objects"}
    imports.update(
        import_name
        for _name, field_type, _source in fields
        for import_name in _imports_for_type(
            field_type, package.rsplit(".persistence", 1)[0], set()
        )
    )
    lines = [
        f"package {package};",
        "",
        *(f"import {name};" for name in sorted(imports)),
        "",
        "/** JPA composite key for multiple ERD identifier fields. */",
        f"public class {key_name} implements Serializable {{",
    ]
    lines.extend(f"    private {field_type} {name};" for name, field_type, _ in fields)
    lines.extend(["", f"    public {key_name}() {{}}", ""])
    parameters = ", ".join(f"{field_type} {name}" for name, field_type, _ in fields)
    lines.append(f"    public {key_name}({parameters}) {{")
    lines.extend(f"        this.{name} = {name};" for name, _field_type, _ in fields)
    lines.append("    }")
    for name, field_type, _source in fields:
        method = name[:1].upper() + name[1:]
        lines.extend(
            [
                "",
                f"    public {field_type} get{method}() {{ return {name}; }}",
                f"    public void set{method}({field_type} {name}) {{ this.{name} = {name}; }}",
            ]
        )
    comparisons = " && ".join(f"Objects.equals({name}, other.{name})" for name, _, _ in fields)
    values = ", ".join(name for name, _field_type, _source in fields)
    lines.extend(
        [
            "",
            "    @Override",
            "    public boolean equals(Object value) {",
            "        if (this == value) return true;",
            f"        if (!(value instanceof {key_name} other)) return false;",
            f"        return {comparisons};",
            "    }",
            "",
            "    @Override",
            "    public int hashCode() {",
            f"        return Objects.hash({values});",
            "    }",
            "}",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_migration(
    entities: list[AcceptedBCEClass],
    declared_types: set[str],
    enum_types: set[str],
    value_types: set[str],
) -> str:
    statements = ["-- Initial schema generated from the typed ERD model."]
    for entity in entities:
        fields, identifiers, _text_identifier_names = _persistence_fields(
            entity, declared_types, value_types
        )
        identifier_names = {name for name, _field_type, _source in identifiers}
        columns: list[str] = []
        for name, field_type, _source in fields:
            column = (
                f"    {_database_identifier(_snake_case(name))} "
                f"{_sql_type(field_type, enum_types, value_types)}"
            )
            if len(identifiers) == 1 and name in identifier_names:
                column += " PRIMARY KEY"
            columns.append(column)
        if len(identifiers) > 1:
            names = ", ".join(_database_identifier(_snake_case(name)) for name in identifier_names)
            columns.append(f"    PRIMARY KEY ({names})")
        statements.extend(
            [
                "",
                f"CREATE TABLE {_table_name(entity.class_name)} (",
                ",\n".join(columns),
                ");",
            ]
        )
    return "\n".join(statements) + "\n"


def _imports_for_type(
    field_type: str,
    base_package: str,
    bce_types: set[str],
) -> set[str]:
    imports = {
        path for token, path in _JAVA_IMPORTS.items() if re.search(rf"\b{token}\b", field_type)
    }
    imports.update(
        f"{base_package}.bce.{name}"
        for name in bce_types
        if re.search(rf"\b{re.escape(name)}\b", field_type)
    )
    return imports


def _is_enum(field_type: str, enum_types: set[str]) -> bool:
    return field_type in enum_types


def _is_json_type(field_type: str, value_types: set[str]) -> bool:
    return (
        field_type == "Object"
        or field_type in value_types
        or field_type.startswith("List<")
        or field_type.startswith("Optional<")
    )


def _sql_type(field_type: str, enum_types: set[str], value_types: set[str]) -> str:
    if field_type in enum_types:
        return "VARCHAR(255)"
    if _is_json_type(field_type, value_types):
        return "JSON"
    # The logical contract remains vendor-neutral.  The generated application
    # runs its migration in MySQL-compatible H2/MySQL, where these two physical
    # encodings are the portable choices.
    if field_type == "UUID":
        return "BINARY(16)"
    if field_type == "byte[]":
        return "LONGBLOB"
    try:
        return sql_type_for_design(field_type)
    except DesignTypeError:
        return "JSON"


def _snake_case(value: str) -> str:
    first = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", value)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", first).lower()


def _database_identifier(value: str) -> str:
    """예약어인 열 이름을 DB 종류와 무관한 안전한 물리 이름으로 바꾼다."""

    return f"{value}_value" if value.lower() in _SQL_RESERVED_WORDS else value


def _table_name(entity_name: str) -> str:
    """DB 예약어와 겹치지 않도록 생성 테이블에 고정 접두사를 붙인다."""

    return f"easydep_{_snake_case(entity_name)}"
