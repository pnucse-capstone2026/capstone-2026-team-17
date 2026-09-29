"""Public design-type parsing and deterministic target projections.

The accepted design artifacts keep their compact string representation, but every
consumer must interpret that string through this module.  This prevents a type from
being accepted by class design, weakened to ``object`` in OpenAPI, and only failing
after generated code is executed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal


class DesignTypeError(ValueError):
    """A design type is malformed, unresolved, or unsupported by a target."""


TypeKind = Literal["scalar", "named", "container"]


@dataclass(frozen=True)
class TypeExpression:
    """Parsed meaning of one design type expression."""

    kind: TypeKind
    name: str
    arguments: tuple[TypeExpression, ...] = ()


# Keys are accepted spellings; values are implementation-neutral semantic names.
SCALAR_ALIASES: dict[str, str] = {
    "any": "object",
    "object": "object",
    "java.lang.object": "object",
    "biginteger": "big_integer",
    "java.math.biginteger": "big_integer",
    "bool": "boolean",
    "boolean": "boolean",
    "java.lang.boolean": "boolean",
    "byte": "byte",
    "java.lang.byte": "byte",
    "char": "character",
    "character": "character",
    "java.lang.character": "character",
    "date": "date",
    "localdate": "date",
    "java.time.localdate": "date",
    "datetime": "datetime",
    "localdatetime": "datetime",
    "java.time.localdatetime": "datetime",
    "decimal": "decimal",
    "bigdecimal": "decimal",
    "java.math.bigdecimal": "decimal",
    "number": "decimal",
    "double": "double",
    "java.lang.double": "double",
    "float": "float",
    "java.lang.float": "float",
    "guid": "uuid",
    "uuid": "uuid",
    "java.util.uuid": "uuid",
    "instant": "instant",
    "timestamp": "instant",
    "java.time.instant": "instant",
    "int": "integer",
    "integer": "integer",
    "java.lang.integer": "integer",
    "localtime": "time",
    "time": "time",
    "java.time.localtime": "time",
    "long": "long",
    "java.lang.long": "long",
    "offsetdatetime": "offset_datetime",
    "java.time.offsetdatetime": "offset_datetime",
    "short": "short",
    "java.lang.short": "short",
    "str": "string",
    "string": "string",
    "java.lang.string": "string",
    "void": "void",
    "zoneddatetime": "zoned_datetime",
    "java.time.zoneddatetime": "zoned_datetime",
    "binary": "binary",
    "bytes": "binary",
}

CONTAINER_ALIASES: dict[str, str] = {
    "array": "list",
    "list": "list",
    "java.util.list": "list",
    "set": "set",
    "java.util.set": "set",
    "collection": "collection",
    "java.util.collection": "collection",
    "iterable": "iterable",
    "java.lang.iterable": "iterable",
    "optional": "optional",
    "java.util.optional": "optional",
}

PROMPT_PRIMITIVES = frozenset(
    key
    for key in SCALAR_ALIASES
    if "." not in key and key not in {"binary", "bytes"}
)
PROMPT_CONTAINERS = frozenset(
    key for key in CONTAINER_ALIASES if "." not in key
)

_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")
_BINARY_EXPRESSIONS = frozenset({"byte[]", "bytes[]", "bytes", "binary"})


class _TypeParser:
    def __init__(self, value: str) -> None:
        self.value = re.sub(r"\s+", "", value)
        self.position = 0

    def parse(self) -> TypeExpression:
        expression = self._expression()
        if self.position != len(self.value):
            raise DesignTypeError(
                f"Unexpected type syntax at position {self.position}: {self.value!r}"
            )
        return expression

    def _expression(self) -> TypeExpression:
        match = _TOKEN.match(self.value, self.position)
        if match is None:
            raise DesignTypeError(
                f"Expected a type name at position {self.position}: {self.value!r}"
            )
        raw_name = match.group(0)
        self.position = match.end()
        arguments: list[TypeExpression] = []
        closing = ""
        if self.position < len(self.value) and (
            self.value[self.position] == "<"
            or (
                self.value[self.position] == "["
                and self.value[self.position : self.position + 2] != "[]"
            )
        ):
            opening = self.value[self.position]
            closing = ">" if opening == "<" else "]"
            self.position += 1
            arguments.append(self._expression())
            while self.position < len(self.value) and self.value[self.position] == ",":
                self.position += 1
                arguments.append(self._expression())
            if self.position >= len(self.value) or self.value[self.position] != closing:
                raise DesignTypeError(f"Unclosed type container: {self.value!r}")
            self.position += 1

        lowered = raw_name.casefold()
        if lowered in CONTAINER_ALIASES:
            if len(arguments) != 1:
                raise DesignTypeError(
                    f"Container {raw_name!r} requires exactly one item type"
                )
            expression = TypeExpression(
                "container", CONTAINER_ALIASES[lowered], tuple(arguments)
            )
        else:
            if arguments:
                raise DesignTypeError(f"Unsupported generic type: {raw_name!r}")
            scalar = SCALAR_ALIASES.get(lowered)
            expression = (
                TypeExpression("scalar", scalar)
                if scalar is not None
                else TypeExpression("named", raw_name)
            )

        if self.value[self.position : self.position + 2] == "[]":
            self.position += 2
            expression = (
                TypeExpression("scalar", "binary")
                if expression.kind == "scalar" and expression.name in {"byte", "binary"}
                else TypeExpression("container", "list", (expression,))
            )
        if self.position < len(self.value) and self.value[self.position] == "?":
            self.position += 1
            expression = TypeExpression("container", "optional", (expression,))
        return expression


def parse_type_expression(value: str) -> TypeExpression:
    """Parse one complete type expression or raise ``DesignTypeError``."""

    compact = re.sub(r"\s+", "", str(value or ""))
    if not compact:
        raise DesignTypeError("Design type must not be empty")
    if compact.casefold() in _BINARY_EXPRESSIONS:
        return TypeExpression("scalar", "binary")
    expression = _TypeParser(compact).parse()
    if expression.kind == "container" and _contains_void(expression):
        raise DesignTypeError("void cannot be used as a container item type")
    return expression


def _contains_void(expression: TypeExpression) -> bool:
    return (
        expression.kind == "scalar" and expression.name == "void"
    ) or any(_contains_void(item) for item in expression.arguments)


_DESIGN_SCALARS = {
    "object": "Object",
    "big_integer": "BigInteger",
    "boolean": "boolean",
    "byte": "byte",
    "character": "char",
    "date": "LocalDate",
    "datetime": "LocalDateTime",
    "decimal": "BigDecimal",
    "double": "double",
    "float": "float",
    "uuid": "UUID",
    "instant": "Instant",
    "integer": "int",
    "time": "LocalTime",
    "long": "long",
    "offset_datetime": "OffsetDateTime",
    "short": "short",
    "string": "String",
    "void": "void",
    "zoned_datetime": "ZonedDateTime",
    "binary": "byte[]",
}
_DESIGN_CONTAINERS = {
    "list": "List",
    "set": "Set",
    "collection": "Collection",
    "iterable": "Iterable",
    "optional": "Optional",
}


def render_design_type(expression: TypeExpression) -> str:
    """Render a parsed expression in the one canonical persisted notation."""

    if expression.kind == "named":
        return expression.name
    if expression.kind == "scalar":
        return _DESIGN_SCALARS[expression.name]
    return (
        f"{_DESIGN_CONTAINERS[expression.name]}<"
        f"{render_design_type(expression.arguments[0])}>"
    )


def canonical_design_type(value: str) -> str:
    return render_design_type(parse_type_expression(value))


def referenced_names(expression: TypeExpression) -> set[str]:
    """Return named class/DataType references contained in an expression."""

    if expression.kind == "named":
        return {expression.name}
    return {
        name
        for argument in expression.arguments
        for name in referenced_names(argument)
    }


def types_equivalent(left: str, right: str) -> bool:
    """Compare semantic types after resolving every supported alias."""

    try:
        return parse_type_expression(left) == parse_type_expression(right)
    except DesignTypeError:
        return False


_JAVA_SCALARS = {
    "object": "Object",
    "big_integer": "BigInteger",
    "boolean": "Boolean",
    "byte": "Byte",
    "character": "Character",
    "date": "LocalDate",
    "datetime": "LocalDateTime",
    "decimal": "BigDecimal",
    "double": "Double",
    "float": "Float",
    "uuid": "UUID",
    "instant": "Instant",
    "integer": "Integer",
    "time": "LocalTime",
    "long": "Long",
    "offset_datetime": "OffsetDateTime",
    "short": "Short",
    "string": "String",
    "void": "void",
    "zoned_datetime": "ZonedDateTime",
    "binary": "byte[]",
}


def java_type_for_design(value: str, *, declared_types: set[str]) -> str:
    """Project a resolved design type to Java without an ``Object`` fallback."""

    def project(expression: TypeExpression) -> str:
        if expression.kind == "named":
            if expression.name not in declared_types:
                raise DesignTypeError(
                    f"Design type {expression.name!r} is not a declared Class or DataType"
                )
            return expression.name
        if expression.kind == "scalar":
            return _JAVA_SCALARS[expression.name]
        return (
            f"{_DESIGN_CONTAINERS[expression.name]}<"
            f"{project(expression.arguments[0])}>"
        )

    return project(parse_type_expression(value))


_API_SCALARS = {
    "object": "object",
    "big_integer": "BigInteger",
    "boolean": "boolean",
    "byte": "byte",
    "character": "string",
    "date": "date",
    "datetime": "date-time",
    "decimal": "number",
    "double": "double",
    "float": "float",
    "uuid": "uuid",
    "instant": "date-time",
    "integer": "integer",
    # Keep the temporal kind through API normalization. It is still a JSON
    # string, but OpenAPI needs the distinction to describe LocalTime input.
    "time": "time",
    "long": "long",
    "offset_datetime": "date-time",
    "short": "short",
    "string": "string",
    "void": "void",
    "zoned_datetime": "date-time",
    "binary": "binary",
}


def api_type_for_design(value: str) -> str:
    """Project a design type to the compact typed API-model notation."""

    if re.sub(r"\s+", "", str(value or "")).casefold() == "date-time":
        return "date-time"

    def project(expression: TypeExpression) -> str:
        if expression.kind == "named":
            return expression.name
        if expression.kind == "scalar":
            return _API_SCALARS[expression.name]
        if expression.name == "optional":
            return project(expression.arguments[0])
        return f"{project(expression.arguments[0])}[]"

    return project(parse_type_expression(value))


def wire_types_equivalent(left: str, right: str) -> bool:
    """Compare values after deterministic JSON-wire projection."""

    try:
        actual = wire_type_signature(left)
        expected = wire_type_signature(right)
    except DesignTypeError:
        return False
    return actual == expected or (
        actual == "string" and expected in {"uuid", "date", "date-time"}
    )


def scalar_wire_types_equivalent(left: str, right: str) -> bool:
    """Compare only scalar wire types, leaving named schemas to structural checks."""

    def scalar(value: str) -> bool:
        if re.sub(r"\s+", "", str(value or "")).casefold() == "date-time":
            return True
        try:
            return parse_type_expression(value).kind == "scalar"
        except DesignTypeError:
            return False

    return scalar(left) and scalar(right) and wire_types_equivalent(left, right)


def wire_type_signature(value: str) -> str:
    """Return the JSON value family while retaining meaningful string formats."""

    compact = re.sub(r"\s+", "", str(value or ""))
    if compact.casefold() == "date-time":
        return "date-time"

    def signature(expression: TypeExpression) -> str:
        if expression.kind == "named":
            return expression.name.casefold()
        if expression.kind == "container":
            if expression.name == "optional":
                return signature(expression.arguments[0])
            return f"{signature(expression.arguments[0])}[]"
        if expression.name in {"byte", "short", "integer", "long", "big_integer"}:
            return "integer"
        if expression.name in {"decimal", "double", "float"}:
            return "number"
        if expression.name in {"character", "string", "time"}:
            return "string"
        if expression.name == "uuid":
            return "uuid"
        if expression.name == "date":
            return "date"
        if expression.name in {
            "datetime", "instant", "offset_datetime", "zoned_datetime"
        }:
            return "date-time"
        return _API_SCALARS[expression.name]

    return signature(parse_type_expression(compact))


def openapi_schema_for_type(value: str, *, declared_types: set[str]) -> dict[str, object]:
    """Project an API/design type to OpenAPI and reject unresolved names."""

    compact = re.sub(r"\s+", "", str(value or ""))
    if compact.casefold() == "date-time":
        return {"type": "string", "format": "date-time"}

    def project(expression: TypeExpression) -> dict[str, object]:
        if expression.kind == "named":
            if expression.name not in declared_types:
                raise DesignTypeError(
                    f"API type {expression.name!r} has no declared schema"
                )
            return {"$ref": f"#/components/schemas/{expression.name}"}
        if expression.kind == "container":
            if expression.name == "optional":
                return project(expression.arguments[0])
            return {"type": "array", "items": project(expression.arguments[0])}

        name = expression.name
        if name == "void":
            raise DesignTypeError("void cannot be projected as an OpenAPI value")
        if name == "object":
            return {"type": "object"}
        if name == "binary":
            return {"type": "string", "format": "byte"}
        if name == "uuid":
            return {"type": "string", "format": "uuid"}
        if name == "date":
            return {"type": "string", "format": "date"}
        if name == "time":
            # java.time.LocalTime's ISO_LOCAL_TIME wire form: HH:mm, optionally
            # followed by seconds and a 1–9 digit fractional second. Unlike
            # RFC3339 full-time this deliberately has no UTC offset.
            return {
                "type": "string",
                "format": "local-time",
                "pattern": (
                    r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]"
                    r"(?::[0-5][0-9](?:\.[0-9]{1,9})?)?$"
                ),
                "description": (
                    "Local time without a UTC offset, in Java LocalTime ISO_LOCAL_TIME form."
                ),
            }
        if name in {"datetime", "instant", "offset_datetime", "zoned_datetime"}:
            return {"type": "string", "format": "date-time"}
        if name in {"character", "string"}:
            return {"type": "string"}
        if name in {"byte", "short", "integer", "long", "big_integer"}:
            schema: dict[str, object] = {"type": "integer"}
            if name == "long":
                schema["format"] = "int64"
            elif name in {"byte", "short", "integer"}:
                schema["format"] = "int32"
            return schema
        if name in {"decimal", "double", "float"}:
            schema = {"type": "number"}
            if name in {"double", "float"}:
                schema["format"] = name
            return schema
        if name == "boolean":
            return {"type": "boolean"}
        raise DesignTypeError(f"Unsupported OpenAPI type: {name}")

    return project(parse_type_expression(compact))


_SQL_SCALARS = {
    "big_integer": "DECIMAL(38,0)",
    "boolean": "BOOLEAN",
    "byte": "TINYINT",
    "character": "CHAR(1)",
    "date": "DATE",
    "datetime": "TIMESTAMP",
    "decimal": "DECIMAL(19,4)",
    "double": "DOUBLE",
    "float": "FLOAT",
    "uuid": "UUID",
    "instant": "TIMESTAMP WITH TIME ZONE",
    "integer": "INT",
    "time": "TIME",
    "long": "BIGINT",
    "offset_datetime": "TIMESTAMP WITH TIME ZONE",
    "short": "SMALLINT",
    "string": "VARCHAR(255)",
    "zoned_datetime": "TIMESTAMP WITH TIME ZONE",
    "binary": "BLOB",
}


def sql_type_for_design(value: str) -> str:
    """Project a scalar design type to SQL; complex values require explicit mapping."""

    expression = parse_type_expression(value)
    if expression.kind == "container" and expression.name == "optional":
        expression = expression.arguments[0]
    if expression.kind != "scalar" or expression.name not in _SQL_SCALARS:
        raise DesignTypeError(
            f"Design type {value!r} cannot be stored in one SQL column"
        )
    return _SQL_SCALARS[expression.name]
