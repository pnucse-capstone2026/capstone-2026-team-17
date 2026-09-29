"""구조화된 설계 모델에서 Java 21 BCE 계약을 직접 만든다.

이 모듈은 사람이 보기 위한 PlantUML을 다시 해석하지 않는다. 클래스 설계 단계가 저장한
``BCEModel`` JSON을 검증한 뒤, 같은 입력에는 항상 같은 경로와 같은 UTF-8 내용을 만든다.
생성 결과는 이후 LLM 구현 작업이 따라야 하는 공개 계약이며 업무 동작을 추측하지 않는다.
"""

from __future__ import annotations

import re
import textwrap
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.design.contracts.api_spec import ApiEndpoint, ApiSpecModel
from app.design.contracts.application_runtime import authenticated_uuid_context_required
from app.design.contracts.type_system import (
    java_type_for_design,
    scalar_wire_types_equivalent,
)
from app.design.schemas.class_model import (
    AcceptedBCEClass,
    BCEModel,
    ClassOperation,
    DataType,
)

JAVA_SCAFFOLDER_VERSION = "1.5.0"
IMPLEMENTATION_MARKER = "EASYDEP-IMPLEMENT"
CONTROLLER_BODY_REQUIRED = "EASYDEP_CONTROLLER_BODY_REQUIRED"

_JAVA_IDENTIFIER = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
_FIELD = re.compile(r"^\s*[+#~\-]?\s*(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)\s*:\s*(?P<type>.+?)\s*$")
_JAVA_KEYWORDS = frozenset(
    {
        "abstract",
        "assert",
        "boolean",
        "break",
        "byte",
        "case",
        "catch",
        "char",
        "class",
        "const",
        "continue",
        "default",
        "do",
        "double",
        "else",
        "enum",
        "extends",
        "final",
        "finally",
        "float",
        "for",
        "goto",
        "if",
        "implements",
        "import",
        "instanceof",
        "int",
        "interface",
        "long",
        "native",
        "new",
        "package",
        "private",
        "protected",
        "public",
        "return",
        "short",
        "static",
        "strictfp",
        "super",
        "switch",
        "synchronized",
        "this",
        "throw",
        "throws",
        "transient",
        "try",
        "void",
        "volatile",
        "while",
        "record",
        "sealed",
        "permits",
        "yield",
    }
)
_IMPORTS = {
    "BigDecimal": "java.math.BigDecimal",
    "BigInteger": "java.math.BigInteger",
    "Instant": "java.time.Instant",
    "LocalDate": "java.time.LocalDate",
    "LocalDateTime": "java.time.LocalDateTime",
    "LocalTime": "java.time.LocalTime",
    "OffsetDateTime": "java.time.OffsetDateTime",
    "ZonedDateTime": "java.time.ZonedDateTime",
    "Collection": "java.util.Collection",
    "Iterable": "java.lang.Iterable",
    "List": "java.util.List",
    "Optional": "java.util.Optional",
    "Set": "java.util.Set",
    "UUID": "java.util.UUID",
}
_JAVA_INTERFACE = re.compile(r"\bpublic\s+interface\s+(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)\b[^\{]*\{")
_JAVA_IMPORT = re.compile(r"(?m)^import\s+[^;]+;$")
_OPENAPI_METHOD = re.compile(
    r"(?ms)(?P<mapping>@RequestMapping\s*\(.*?\))\s*"
    r"(?P<signature>(?:(?:public|abstract|default)\s+)*[A-Za-z_$][^;{}]*?)\s*;"
)
_PATH_CONSTANT = re.compile(r'\bString\s+(?P<name>[A-Z][A-Z0-9_]*)\s*=\s*"(?P<path>[^"]+)"\s*;')


class JavaScaffoldInput(BaseModel):
    """Java 생성에 필요한 설계 snapshot을 한 번에 검증하는 입력 계약."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    bce_model: BCEModel = Field(alias="bceModel")
    erd_bce_model: BCEModel | None = Field(default=None, alias="erdBceModel")
    base_package: str = Field(alias="basePackage", min_length=1)
    java_version: Literal[21] = Field(default=21, alias="javaVersion")
    application_name: str = Field(alias="applicationName", min_length=1)

    @field_validator("base_package")
    @classmethod
    def validate_base_package(cls, value: str) -> str:
        """점으로 나눈 모든 package 조각이 Java 식별자인지 확인한다."""
        parts = value.split(".")
        if any(not _valid_identifier(part) for part in parts):
            raise ValueError("basePackage must contain valid Java identifiers")
        return value

    @model_validator(mode="after")
    def validate_java_names(self) -> JavaScaffoldInput:
        """파일을 만들기 전에 Java 이름과 field 선언 형식만 확인한다."""
        models = [self.bce_model]
        if self.erd_bce_model is not None:
            models.append(self.erd_bce_model)
        for model in models:
            for data_type in model.DataTypes:
                _require_identifier(data_type.name, "DataType name")
                if data_type.kind == "enumeration":
                    _java_enum_members(data_type.values)
                for field in data_type.fields:
                    _parse_field(field, owner=data_type.name)
            for component in model.Classes:
                _require_identifier(component.class_name, "className")
                for field in component.fields:
                    _parse_field(field, owner=component.class_name)
                for operation in component.operations:
                    _require_identifier_syntax(operation.name, "operation name")
                    for parameter in operation.parameters:
                        _require_identifier(parameter.name, f"parameter in {operation.name}")
        # API schemas and BCE components intentionally use different Java
        # packages (``.api.model`` and ``.bce``).  A domain entity such as
        # ``Course`` commonly appears in both models, and that is not a Java
        # type collision.  The scaffold renders only BCE declarations; the
        # OpenAPI generator owns API model declarations in its own package.
        # Rejecting equal simple names here therefore blocked valid designs
        # before either generator could create a file.
        return self


def render_java_scaffold(scaffold: JavaScaffoldInput) -> dict[str, str]:
    """BCE 타입별 Java 파일을 경로 기준으로 정렬해 반환한다.

    이 함수가 만드는 BCE 선언에는 class model만 사용하고, HTTP adapter와 실행 순서는
    각 전용 생성·구현 단계가 담당한다.
    """
    package_name = f"{scaffold.base_package}.bce"
    package_path = package_name.replace(".", "/")
    declared_types = {
        *(item.class_name for item in scaffold.bce_model.Classes),
        *(item.name for item in scaffold.bce_model.DataTypes),
    }
    files: dict[str, str] = {}
    for data_type in sorted(scaffold.bce_model.DataTypes, key=lambda item: item.name):
        _require_identifier(data_type.name, "DataType name")
        files[f"{package_path}/{data_type.name}.java"] = _render_data_type(
            package_name, data_type, declared_types
        )
    erd_entities = {
        item.class_name: item
        for item in (scaffold.erd_bce_model.Classes if scaffold.erd_bce_model else [])
        if item.stereotype == "Entity"
    }
    for component in sorted(scaffold.bce_model.Classes, key=lambda item: item.class_name):
        _require_identifier(component.class_name, "className")
        persistence_backed = (
            component.stereotype == "Entity" and component.class_name in erd_entities
        )
        if persistence_backed:
            erd_component = erd_entities[component.class_name]
            component = component.model_copy(
                update={
                    "fields": list(erd_component.fields),
                    "identifier": list(erd_component.identifier),
                }
        )
        files[f"{package_path}/{component.class_name}.java"] = _render_component(
            package_name,
            component,
            declared_types,
            persistence_backed=persistence_backed,
        )
    return dict(sorted(files.items()))


def build_java_scaffold_trace(scaffold: JavaScaffoldInput, files: dict[str, str]) -> dict[str, Any]:
    """생성 파일을 BCE class·operation·use case와 연결하는 추적 정보를 만든다."""
    path_by_name = {path.rsplit("/", 1)[-1].removesuffix(".java"): path for path in files}
    mappings: list[dict[str, Any]] = []
    for component in sorted(scaffold.bce_model.Classes, key=lambda item: item.class_name):
        mappings.append(
            {
                "file": path_by_name[component.class_name],
                "type": component.class_name,
                "kind": component.stereotype,
                "useCaseIds": sorted(set(component.use_case_ids)),
                "operations": [
                    {
                        "operationId": operation.operation_id,
                        "stepRefs": list(operation.step_refs),
                    }
                    for operation in component.operations
                ],
            }
        )
    for data_type in sorted(scaffold.bce_model.DataTypes, key=lambda item: item.name):
        mappings.append(
            {
                "file": path_by_name[data_type.name],
                "type": data_type.name,
                "kind": data_type.kind,
                "useCaseIds": [],
                "operations": [],
            }
        )
    return {
        "schemaVersion": "easydep-java-scaffold-trace/v1",
        "generatorVersion": JAVA_SCAFFOLDER_VERSION,
        "applicationName": scaffold.application_name,
        "mappings": mappings,
    }


def render_openapi_controller_scaffold(
    interface_source: str,
    base_package: str,
    *,
    api_model: ApiSpecModel | None = None,
    bce_model: BCEModel | None = None,
) -> tuple[str, str]:
    """생성된 OpenAPI interface를 typed BCE 호출과 연결한 Controller를 만든다.

    OpenAPI operationId와 DTO 타입을 다시 해석하거나 이름을 추측하지 않는다. 이미
    생성된 interface의 import, method annotation, parameter와 반환 타입을 재사용한다.
    typed API/BCE 모델을 받은 경우에는 명시된 Boundary 호출과 구조 변환도 채운다.
    모델이 없거나 연결할 수 없는 메서드만 기존 실패 유도 표식으로 남긴다.
    """
    interface = _JAVA_INTERFACE.search(interface_source)
    if interface is None:
        raise ValueError("Generated OpenAPI source does not contain a public interface")
    interface_name = interface.group("name")
    constants = {
        match.group("name"): match.group("path")
        for match in _PATH_CONSTANT.finditer(interface_source)
    }
    dependencies: dict[str, str] = {}
    authenticated_actor_provider_needed = False
    mapper_needed = False
    methods: list[str] = []
    for match in _OPENAPI_METHOD.finditer(interface_source):
        mapping = match.group("mapping")
        signature = match.group("signature")
        endpoint: ApiEndpoint | None = None
        body: list[str] | None = None
        if api_model is not None and bce_model is not None:
            endpoint = _endpoint_for_mapping(mapping, constants, api_model)
            if endpoint is not None:
                rendered = _controller_body(endpoint, signature, api_model, bce_model, base_package)
                if rendered is not None:
                    control_name, body, uses_authenticated_actor = rendered
                    dependencies[control_name] = _field_name(control_name)
                    authenticated_actor_provider_needed = (
                        authenticated_actor_provider_needed or uses_authenticated_actor
                    )
                    mapper_needed = mapper_needed or body is not None
        marker = (
            controller_body_marker(endpoint.method, endpoint.path)
            if endpoint is not None
            else f"{CONTROLLER_BODY_REQUIRED}:{_java_method_parts(signature)[0]}"
        )
        methods.append(_render_controller_method(mapping, signature, body, marker=marker))
    if not methods:
        raise ValueError(f"Generated OpenAPI interface {interface_name} has no overridable methods")
    controller_name = f"{interface_name}Controller"
    imports = {item.strip() for item in _JAVA_IMPORT.findall(interface_source)}
    imports.add(f"import {base_package}.api.{interface_name};")
    imports.add("import org.springframework.web.bind.annotation.RestController;")
    imports.update(f"import {base_package}.bce.{name};" for name in dependencies)
    if authenticated_actor_provider_needed:
        imports.add(
            f"import {base_package}.config.AuthenticatedActorIdProvider;"
        )
    if mapper_needed:
        imports.add("import com.fasterxml.jackson.databind.ObjectMapper;")

    fields = [f"    private final {name} {field};" for name, field in sorted(dependencies.items())]
    if authenticated_actor_provider_needed:
        fields.append("    private final AuthenticatedActorIdProvider authenticatedActorIdProvider;")
    if mapper_needed:
        fields.append("    private final ObjectMapper objectMapper;")
    constructor = ""
    if fields:
        parameters = [f"{name} {field}" for name, field in sorted(dependencies.items())]
        if authenticated_actor_provider_needed:
            parameters.append(
                "AuthenticatedActorIdProvider authenticatedActorIdProvider"
            )
        if mapper_needed:
            parameters.append("ObjectMapper objectMapper")
        assignments = [
            f"        this.{field} = {field};" for _name, field in sorted(dependencies.items())
        ]
        if authenticated_actor_provider_needed:
            assignments.append(
                "        this.authenticatedActorIdProvider = authenticatedActorIdProvider;"
            )
        if mapper_needed:
            assignments.append("        this.objectMapper = objectMapper;")
        constructor = (
            "\n\n"
            f"    public {controller_name}({', '.join(parameters)}) {{\n"
            + "\n".join(assignments)
            + "\n    }"
        )
    return (
        controller_name,
        f"package {base_package}.adapter.in.web;\n\n"
        + "\n".join(sorted(imports))
        + "\n\n"
        + "/** Controller scaffold that preserves the generated OpenAPI interface contract. */\n"
        + "@RestController\n"
        + f"public class {controller_name} implements {interface_name} {{\n\n"
        + "\n".join(fields)
        + constructor
        + ("\n\n" if fields else "")
        + "\n\n".join(methods)
        + "\n}\n",
    )


def _render_controller_method(
    mapping: str,
    signature: str,
    body: list[str] | None = None,
    *,
    marker: str,
) -> str:
    """생성 interface 선언을 보존하고 확인된 호출 본문 또는 표식을 넣는다."""
    signature = re.sub(r"^(?:(?:public|abstract|default)\s+)+", "", signature.strip())
    if "(" not in signature or ")" not in signature:
        raise ValueError("Generated OpenAPI method declaration is incomplete")
    rendered = textwrap.indent(mapping.strip(), "    ") + "\n"
    rendered += "    @Override\n"
    rendered += "    public " + textwrap.indent(signature, "    ").lstrip()
    rendered += " {\n"
    if body is None:
        rendered += f'        throw new UnsupportedOperationException("{marker}");\n'
    else:
        rendered += "\n".join(f"        {line}" for line in body) + "\n"
    rendered += "    }"
    return rendered


def controller_body_marker(method: str, path: str) -> str:
    """API 작업과 생성 Controller가 함께 쓰는 읽기 쉬운 미완성 본문 표식이다."""

    return f"{CONTROLLER_BODY_REQUIRED}:{method.upper()}:{path}"


def _endpoint_for_mapping(
    mapping: str,
    constants: dict[str, str],
    api_model: ApiSpecModel,
) -> ApiEndpoint | None:
    """RequestMapping의 확정 method/path를 typed endpoint 하나와 연결한다."""

    method_match = re.search(r"method\s*=\s*RequestMethod\.([A-Z]+)", mapping)
    value_match = re.search(
        r'value\s*=\s*(?:"(?P<literal>[^"]+)"|(?:[A-Za-z_$]\w*\.)?(?P<constant>[A-Z][A-Z0-9_]*))',
        mapping,
    )
    if method_match is None or value_match is None:
        return None
    path = value_match.group("literal") or constants.get(value_match.group("constant") or "", "")
    matches = [
        endpoint
        for endpoint in api_model.Endpoints
        if endpoint.method.upper() == method_match.group(1) and endpoint.path == path
    ]
    return matches[0] if len(matches) == 1 else None


def _controller_body(
    endpoint: ApiEndpoint,
    signature: str,
    api_model: ApiSpecModel,
    bce_model: BCEModel,
    base_package: str,
) -> tuple[str, list[str] | None, bool] | None:
    """HTTP binding을 Control 호출 인자로 바꾸고 성공 응답을 반환한다."""

    binding = endpoint.control_binding
    if binding is None:
        return None
    control = next(
        (
            component
            for component in bce_model.Classes
            if component.stereotype == "Control" and component.class_name == binding.control
        ),
        None,
    )
    if control is None:
        return None
    operation = next(
        (item for item in control.operations if item.name == binding.method),
        None,
    )
    if operation is None:
        return None
    sources = _http_parameter_sources(signature)
    binding_sources = {item.name: item.source for item in binding.arguments}
    success = next(
        (response for response in endpoint.responses if 200 <= response.status < 300),
        None,
    )
    if success is None or not _controller_projection_is_safe(
        endpoint,
        operation,
        api_model=api_model,
        bce_model=bce_model,
    ):
        return control.class_name, None, False
    arguments: list[str] = []
    authenticated_actor_used = False
    declared_types = {
        *(item.class_name for item in bce_model.Classes),
        *(item.name for item in bce_model.DataTypes),
    }
    for parameter in operation.parameters:
        source = binding_sources.get(parameter.name)
        if (
            source is not None
            and source.startswith("$context.")
            and java_type(parameter.type, declared_types=declared_types) == "UUID"
            and authenticated_uuid_context_required(api_model, bce_model)
        ):
            expression = "authenticatedActorIdProvider.currentActorId()"
            authenticated_actor_used = True
        else:
            expression = _http_source_expression(source, sources)
        if expression is None:
            return None
        target_type = _qualified_bce_type(parameter.type, base_package, declared_types)
        arguments.append(
            expression
            if authenticated_actor_used and source is not None and source.startswith("$context.")
            else _object_mapper_conversion(expression, target_type)
        )

    call = (
        f"{_field_name(control.class_name)}."
        f"{java_method_name(operation.name)}({', '.join(arguments)})"
    )
    _method_name, return_type, _parameters = _java_method_parts(signature)
    response_type = _response_body_type(return_type)
    if response_type in {None, "Void", "void"}:
        return control.class_name, [
            f"{call};",
            f"return ResponseEntity.status({success.status}).build();",
        ], authenticated_actor_used
    if operation.return_type == "void":
        return control.class_name, None, authenticated_actor_used
    body = [f"var result = {call};"]
    if response_type.startswith("List<") and response_type.endswith(">"):
        item_type = response_type[5:-1].strip()
        body.append(
            "var response = result.stream()"
            f".map(item -> objectMapper.convertValue(item, {item_type}.class)).toList();"
        )
    else:
        body.append(f"var response = {_object_mapper_conversion('result', response_type)};")
    body.append(f"return ResponseEntity.status({success.status}).body(response);")
    return control.class_name, body, authenticated_actor_used


def _http_parameter_sources(signature: str) -> dict[str, str]:
    """생성된 Java 인자의 annotation에서 body/path/query 변수 이름을 읽는다."""

    _method_name, _return_type, parameters = _java_method_parts(signature)
    result: dict[str, str] = {}
    for parameter in _split_java_parameters(parameters):
        variable_match = re.search(r"([A-Za-z_$][A-Za-z0-9_$]*)\s*$", parameter)
        if variable_match is None:
            continue
        variable = variable_match.group(1)
        if "@RequestBody" in parameter:
            result["$body"] = variable
            continue
        path = re.search(r'@PathVariable\s*\(\s*(?:value\s*=\s*)?"([^"]+)"', parameter)
        if path:
            result[f"$path.{path.group(1)}"] = variable
            continue
        query = re.search(
            r'@RequestParam\s*\(\s*(?:(?:value|name)\s*=\s*)?"([^"]+)"',
            parameter,
        )
        if query:
            result[f"$query.{query.group(1)}"] = variable
            continue
        swagger_parameter = re.search(r"@Parameter\s*\(([^)]*)\)", parameter)
        if swagger_parameter is None:
            continue
        attributes = swagger_parameter.group(1)
        name = re.search(r'\bname\s*=\s*"([^"]+)"', attributes)
        location = re.search(r"\bin\s*=\s*ParameterIn\.(QUERY|PATH)\b", attributes)
        if name is not None and location is not None:
            result[f"${location.group(1).lower()}.{name.group(1)}"] = variable
    return result


def _http_source_expression(
    source: str | None,
    variables: dict[str, str],
) -> str | None:
    if not source:
        return None
    if source in variables:
        return variables[source]
    if source.startswith("$body.") and "$body" in variables:
        field = source.removeprefix("$body.")
        getter = field[:1].upper() + field[1:]
        return f"{variables['$body']}.get{getter}()"
    return None


def _java_method_parts(signature: str) -> tuple[str, str, str]:
    clean = re.sub(r"^(?:(?:public|abstract|default)\s+)+", "", signature.strip())
    opening = clean.find("(")
    closing = clean.rfind(")")
    head = clean[:opening].strip()
    method_name = head.rsplit(maxsplit=1)[-1]
    return_type = head[: -len(method_name)].strip()
    return method_name, return_type, clean[opening + 1 : closing]


def _split_java_parameters(value: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    quoted = False
    escaped = False
    for index, character in enumerate(value):
        if character == '"' and not escaped:
            quoted = not quoted
        escaped = character == "\\" and not escaped
        if quoted:
            continue
        if character in "(<[":
            depth += 1
        elif character in ")>]":
            depth -= 1
        elif character == "," and depth == 0:
            parts.append(value[start:index].strip())
            start = index + 1
    tail = value[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def _response_body_type(return_type: str) -> str | None:
    match = re.fullmatch(r"ResponseEntity\s*<(.+)>", return_type.strip(), re.DOTALL)
    return match.group(1).strip() if match else None


def _qualified_bce_type(
    design_type: str,
    base_package: str,
    declared_types: set[str],
) -> str:
    result = java_type(design_type, declared_types=declared_types)
    for name in sorted(declared_types, key=len, reverse=True):
        result = re.sub(rf"\b{re.escape(name)}\b", f"{base_package}.bce.{name}", result)
    standard_types = {
        "BigDecimal": "java.math.BigDecimal",
        "LocalDate": "java.time.LocalDate",
        "LocalDateTime": "java.time.LocalDateTime",
        "LocalTime": "java.time.LocalTime",
        "List": "java.util.List",
        "Optional": "java.util.Optional",
        "UUID": "java.util.UUID",
    }
    for name, qualified in standard_types.items():
        result = re.sub(rf"\b{name}\b", qualified, result)
    return result


def _object_mapper_conversion(expression: str, target_type: str) -> str:
    if "<" in target_type:
        return (
            f"objectMapper.convertValue({expression}, "
            f"new com.fasterxml.jackson.core.type.TypeReference<{target_type}>() {{}})"
        )
    return f"objectMapper.convertValue({expression}, {target_type}.class)"


def _field_name(type_name: str) -> str:
    return type_name[:1].lower() + type_name[1:]


def _controller_projection_is_safe(
    endpoint: ApiEndpoint,
    operation: ClassOperation,
    *,
    api_model: ApiSpecModel,
    bce_model: BCEModel,
) -> bool:
    """Return whether deterministic ObjectMapper wiring can preserve the contract.

    This is an implementation strategy choice, not an upstream validation gate. A
    false result leaves the controller body to the implementation agent instead of
    rejecting or revising the accepted API/BCE artifacts.
    """

    binding = endpoint.control_binding
    if binding is None:
        return False
    sources = {item.name: item.source for item in binding.arguments}
    for parameter in operation.parameters:
        source_ref = sources.get(parameter.name)
        authenticated_uuid_source = (
            source_ref is not None
            and source_ref.startswith("$context.")
            and java_type(
                parameter.type,
                declared_types={
                    *(item.class_name for item in bce_model.Classes),
                    *(item.name for item in bce_model.DataTypes),
                },
            )
            == "UUID"
            and authenticated_uuid_context_required(api_model, bce_model)
        )
        source = (
            ("uuid", True)
            if authenticated_uuid_source
            else _api_source_contract(endpoint, source_ref, api_model)
        )
        if source is None:
            return False
        source_type, required = source
        target_type, target_optional = _without_optional(parameter.type)
        if not required and not target_optional:
            return False
        if not _types_are_structurally_compatible(
            source_type,
            target_type,
            direction="api-to-bce",
            api_model=api_model,
            bce_model=bce_model,
            visited=set(),
        ):
            return False

    response = next(
        (item for item in endpoint.responses if 200 <= item.status < 300),
        None,
    )
    if response is None:
        return False
    if not response.schema_name:
        return operation.return_type == "void"
    api_type = f"list<{response.schema_name}>" if response.is_array else response.schema_name
    source_type, source_optional = _without_optional(operation.return_type)
    if source_optional:
        return False
    return _types_are_structurally_compatible(
        api_type,
        source_type,
        direction="bce-to-api",
        api_model=api_model,
        bce_model=bce_model,
        visited=set(),
    )


def _api_source_contract(
    endpoint: ApiEndpoint,
    source: str | None,
    api_model: ApiSpecModel,
) -> tuple[str, bool] | None:
    """control binding source가 가리키는 API 타입과 필수 여부를 반환한다."""

    if not source:
        return None
    if source == "$body":
        return (endpoint.request_schema, True) if endpoint.request_schema else None
    if source.startswith("$body."):
        schema = next(
            (item for item in api_model.Schemas if item.name == endpoint.request_schema),
            None,
        )
        field_name = source.removeprefix("$body.")
        field = (
            next(
                (item for item in schema.fields if item.name == field_name),
                None,
            )
            if schema is not None
            else None
        )
        return (field.type, field.required) if field is not None else None
    for prefix, fields in (
        ("$path.", endpoint.path_params),
        ("$query.", endpoint.query_params),
    ):
        if source.startswith(prefix):
            name = source.removeprefix(prefix)
            field = next((item for item in fields if item.name == name), None)
            return (field.type, field.required) if field is not None else None
    return None


def _types_are_structurally_compatible(
    api_type: str,
    bce_type: str,
    *,
    direction: Literal["api-to-bce", "bce-to-api"],
    api_model: ApiSpecModel,
    bce_model: BCEModel,
    visited: set[tuple[str, str, str]],
) -> bool:
    """API와 BCE 타입이 값의 누락 없이 구조 변환 가능한지 재귀적으로 비교한다."""

    api_container, api_item = _container_type(api_type)
    bce_container, bce_item = _container_type(bce_type)
    if api_container != bce_container:
        # OpenAPI의 integer[]는 BCE의 byte[]를 손실 없이 표현한다.
        return (
            api_container == "list"
            and api_item.casefold()
            in {
                "integer",
                "int",
            }
            and bce_container == "binary"
        )
    if api_container == "list":
        return _types_are_structurally_compatible(
            api_item,
            bce_item,
            direction=direction,
            api_model=api_model,
            bce_model=bce_model,
            visited=visited,
        )
    if api_container == "binary":
        return bce_container == "binary"

    api_name = api_item.casefold()
    if scalar_wire_types_equivalent(api_item, bce_item):
        return True

    bce_enum = next(
        (
            item
            for item in bce_model.DataTypes
            if item.kind == "enumeration" and item.name == bce_item
        ),
        None,
    )
    if bce_enum is not None:
        return api_item == bce_item or api_name == "string"

    # BCE Entity는 private 상태와 업무 메서드를 가진다. 필드 이름이 같더라도 일반 DTO처럼
    # 자동 변환할 수 있다고 가정하지 않고 해당 기능 작업이 생성·조회 방식을 정하게 한다.
    bce_entity = next(
        (item for item in bce_model.Classes if item.class_name == bce_item),
        None,
    )
    if bce_entity is not None:
        # Entities are never accepted as request payloads. For responses, their
        # generated fields and accessors make a deterministic API projection safe
        # only when the API's required shape is fully represented by the Entity.
        if direction != "bce-to-api":
            return False
        api_schema = next(
            (item for item in api_model.Schemas if item.name == api_item),
            None,
        )
        if api_schema is None:
            return False
        key = (api_item, bce_item, direction)
        if key in visited:
            return True
        visited.add(key)
        entity_fields = {
            name: field_type
            for declaration in bce_entity.fields
            for name, field_type in [
                _parse_field(declaration, owner=bce_entity.class_name)
            ]
        }
        for api_field in api_schema.fields:
            raw_entity_type = entity_fields.get(api_field.name)
            if raw_entity_type is None:
                if not api_field.required:
                    continue
                return False
            inner_entity_type, optional = _without_optional(raw_entity_type)
            if api_field.required and optional:
                return False
            if not _types_are_structurally_compatible(
                api_field.type,
                inner_entity_type,
                direction=direction,
                api_model=api_model,
                bce_model=bce_model,
                visited=visited,
            ):
                return False
        return True

    api_schema = next(
        (item for item in api_model.Schemas if item.name == api_item),
        None,
    )
    bce_value = next(
        (
            item
            for item in bce_model.DataTypes
            if item.kind == "valueObject" and item.name == bce_item
        ),
        None,
    )
    if api_schema is None or bce_value is None:
        return False
    key = (api_item, bce_item, direction)
    if key in visited:
        return True
    visited.add(key)
    api_fields = {item.name: item for item in api_schema.fields}
    bce_fields = {
        name: field_type
        for declaration in bce_value.fields
        for name, field_type in [_parse_field(declaration, owner=bce_value.name)]
    }
    if direction == "api-to-bce":
        for name, raw_bce_type in bce_fields.items():
            inner_bce_type, optional = _without_optional(raw_bce_type)
            api_field = api_fields.get(name)
            if api_field is None:
                if optional:
                    continue
                return False
            if not api_field.required and not optional:
                return False
            if not _types_are_structurally_compatible(
                api_field.type,
                inner_bce_type,
                direction=direction,
                api_model=api_model,
                bce_model=bce_model,
                visited=visited,
            ):
                return False
        return True

    for name, api_field in api_fields.items():
        raw_bce_type = bce_fields.get(name)
        if raw_bce_type is None:
            if not api_field.required:
                continue
            return False
        inner_bce_type, optional = _without_optional(raw_bce_type)
        if api_field.required and optional:
            return False
        if not _types_are_structurally_compatible(
            api_field.type,
            inner_bce_type,
            direction=direction,
            api_model=api_model,
            bce_model=bce_model,
            visited=visited,
        ):
            return False
    return True


def _without_optional(value: str) -> tuple[str, bool]:
    compact = re.sub(r"\s+", "", value)
    match = re.fullmatch(r"Optional<(.+)>", compact, re.IGNORECASE)
    return (match.group(1), True) if match else (compact, False)


def _container_type(value: str) -> tuple[str, str]:
    compact = re.sub(r"\s+", "", value)
    if compact.casefold() in {"binary", "byte[]", "bytes", "bytes[]"}:
        return "binary", "byte"
    if compact.endswith("[]"):
        return "list", compact[:-2]
    match = re.fullmatch(r"(?:list|array)<(.+)>", compact, re.IGNORECASE)
    if match:
        return "list", match.group(1)
    return "scalar", compact


def _render_data_type(package_name: str, data_type: DataType, declared_types: set[str]) -> str:
    """설계 DataType을 enum 또는 record로 렌더링한다."""
    if data_type.kind == "enumeration":
        members = _java_enum_members(data_type.values)
        preserves_display_values = any(name != value for name, value in members)
        if preserves_display_values:
            body = ",\n".join(
                f'    {name}("{_java_string_literal(value)}")'
                for name, value in members
            )
            body += (
                ";\n\n"
                "    private final String displayValue;\n\n"
                f"    {data_type.name}(String displayValue) {{\n"
                "        this.displayValue = displayValue;\n"
                "    }\n\n"
                "    @com.fasterxml.jackson.annotation.JsonValue\n"
                "    public String getDisplayValue() {\n"
                "        return displayValue;\n"
                "    }\n\n"
                "    @com.fasterxml.jackson.annotation.JsonCreator\n"
                f"    public static {data_type.name} fromDisplayValue(String displayValue) {{\n"
                f"        for ({data_type.name} value : values()) {{\n"
                "            if (value.displayValue.equals(displayValue)) {\n"
                "                return value;\n"
                "            }\n"
                "        }\n"
                f'        throw new IllegalArgumentException("Unknown {data_type.name} value: " + displayValue);\n'
                "    }\n"
            )
        else:
            body = ",\n".join(f"    {name}" for name, _value in members)
        return (
            f"package {package_name};\n\n"
            "/** Enumeration contract generated from the class design. */\n"
            f"public enum {data_type.name} {{\n{body}\n}}\n"
        )

    fields = [
        _field(item, owner=data_type.name, declared_types=declared_types)
        for item in data_type.fields
    ]
    imports = _render_imports(field_type for _name, field_type in fields)
    declarations = ", ".join(f"{field_type} {name}" for name, field_type in fields)
    record_body = f"({declarations})"
    return (
        f"package {package_name};\n\n{imports}"
        "/** Java 21 record contract generated from the designed value type. */\n"
        f"public record {data_type.name}{record_body} {{}}\n"
    )


def _render_component(
    package_name: str,
    component: AcceptedBCEClass,
    declared_types: set[str],
    *,
    persistence_backed: bool = False,
) -> str:
    """BCE class를 설계에 있는 필드와 operation만 가진 최소 Java 선언으로 만든다."""
    signatures: set[str] = set()
    for operation in component.operations:
        parameter_types = ",".join(
            java_type(parameter.type, declared_types=declared_types)
            for parameter in operation.parameters
        )
        signature = f"{java_method_name(operation.name)}({parameter_types})"
        if signature in signatures:
            raise ValueError(f"{component.class_name} emits duplicate Java signature: {signature}")
        signatures.add(signature)
    methods = [_method_declaration(operation, declared_types) for operation in component.operations]
    fields = [
        _field(item, owner=component.class_name, declared_types=declared_types)
        for item in component.fields
    ]
    method_types = [
        java_type(operation.return_type, declared_types=declared_types)
        for operation in component.operations
    ] + [
        java_type(parameter.type, declared_types=declared_types)
        for operation in component.operations
        for parameter in operation.parameters
    ]
    imports = _render_imports([*method_types, *(field_type for _name, field_type in fields)])
    header = f"package {package_name};\n\n{imports}"
    if component.stereotype in {"Boundary", "Control"}:
        interface_methods = "\n".join(
            f"    {declaration};" for declaration, _return_type in methods
        )
        if interface_methods:
            interface_methods += "\n"
        return (
            header
            + f"/** Immutable Java contract for the {component.stereotype} role. */\n"
            + f"public interface {component.class_name} {{\n{interface_methods}}}\n"
        )

    lines = [
        header + "/** Initial entity source that preserves the designed state and operations. */",
        f"public class {component.class_name} {{",
    ]
    for name, field_type in fields:
        lines.append(f"    private {field_type} {name};")
    declared_accessors = {
        java_method_name(operation.name)
        for operation in component.operations
        if not operation.parameters
    }
    if persistence_backed and fields:
        parameters = ", ".join(f"{field_type} {name}" for name, field_type in fields)
        lines.extend(["", f"    public {component.class_name}({parameters}) {{"])
        lines.extend(f"        this.{name} = {name};" for name, _field_type in fields)
        lines.append("    }")
        for name, field_type in fields:
            accessor = f"get{name[:1].upper()}{name[1:]}"
            if accessor in declared_accessors:
                continue
            lines.extend(
                [
                    "",
                    f"    public {field_type} {accessor}() {{",
                    f"        return {name};",
                    "    }",
                ]
            )
    for operation, (declaration, _return_type) in zip(
        component.operations, methods, strict=True
    ):
        stable_id = operation.stable_id or operation.operation_id
        lines.append("")
        lines.append(f"    public {declaration} {{")
        lines.extend(
            [
                f"        // {IMPLEMENTATION_MARKER}: complete {stable_id}",
                "        throw new UnsupportedOperationException("
                f'"{IMPLEMENTATION_MARKER}:{stable_id}");',
            ]
        )
        lines.append("    }")
    lines.append("}")
    return "\n".join(lines) + "\n"


def java_type(design_type: str, *, declared_types: set[str] | None = None) -> str:
    """설계 타입 하나를 컴파일 가능한 Java 타입으로 바꾼다.

    ``declared_types``에는 같은 BCE 설계에 실제로 선언된 class·DataType 이름을 넣는다.
    목록에 없는 타입은 ``Object``로 약화하지 않고 즉시 거부한다.
    """
    return java_type_for_design(design_type, declared_types=declared_types or set())


def _method_declaration(
    operation: ClassOperation, declared_types: set[str]
) -> tuple[str, str]:
    """operation의 검증된 Java 선언과 반환 타입을 만든다."""
    method_name = java_method_name(operation.name)
    parameters: list[str] = []
    for parameter in operation.parameters:
        _require_identifier(parameter.name, f"parameter in {operation.name}")
        parameter_type = java_type_for_design(
            parameter.type, declared_types=declared_types
        )
        parameters.append(f"{parameter_type} {parameter.name}")
    return_type = java_type_for_design(
        operation.return_type, declared_types=declared_types
    )
    return (
        f"{return_type} {method_name}({', '.join(parameters)})",
        return_type,
    )


def _parse_field(value: str, *, owner: str) -> tuple[str, str]:
    match = _FIELD.fullmatch(value)
    if match is None:
        raise ValueError(f"{owner} has an invalid field declaration: {value}")
    name = match.group("name")
    _require_identifier(name, f"field in {owner}")
    return name, match.group("type")


def _field(value: str, *, owner: str, declared_types: set[str]) -> tuple[str, str]:
    name, design_type = _parse_field(value, owner=owner)
    return name, java_type_for_design(design_type, declared_types=declared_types)


def _render_imports(types: Any) -> str:
    imports = sorted(
        {
            import_path
            for value in types
            for token, import_path in _IMPORTS.items()
            if re.search(rf"\b{re.escape(token)}\b", value)
            and not import_path.startswith("java.lang.")
        }
    )
    if not imports:
        return ""
    return "".join(f"import {path};\n" for path in imports) + "\n"


def _valid_identifier(value: str) -> bool:
    return bool(_JAVA_IDENTIFIER.fullmatch(value)) and value not in _JAVA_KEYWORDS


def _java_enum_members(values: list[str]) -> list[tuple[str, str]]:
    """Map display values to unique Java enum constants without changing their meaning."""

    used: set[str] = set()
    members: list[tuple[str, str]] = []
    for position, value in enumerate(values, start=1):
        base = _java_enum_identifier(value, position)
        name = base
        suffix = 2
        while name in used:
            name = f"{base}_{suffix}"
            suffix += 1
        used.add(name)
        members.append((name, value))
    return members


def _java_enum_identifier(value: str, position: int) -> str:
    """Create a conventional ASCII Java enum constant from an arbitrary display value."""

    words = re.findall(r"[A-Za-z0-9]+", value)
    identifier = "_".join(word.upper() for word in words).strip("_")
    if not identifier:
        identifier = f"VALUE_{position}"
    if identifier[0].isdigit():
        identifier = f"VALUE_{identifier}"
    # The uppercase result cannot be a lowercase Java keyword, but retain this
    # guard if the naming convention changes later.
    if identifier in _JAVA_KEYWORDS:
        identifier = f"{identifier}_VALUE"
    return identifier


def _java_string_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")


def _require_identifier_syntax(value: str, label: str) -> None:
    """설계 이름이 Java 이름으로 안전하게 옮길 수 있는 문자 형태인지 확인한다."""

    if not _JAVA_IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} is not a valid Java identifier: {value}")


def java_method_name(design_name: str) -> str:
    """설계 operation 이름을 의미를 보존한 Java method 이름으로 바꾼다.

    ``record``처럼 업무 동작으로 자연스럽지만 Java 21에서는 예약어인 이름만 ``Action``
    접미사를 붙인다. 일반 operation 이름은 그대로 두므로 설계와 코드의 대응을 쉽게 찾을 수
    있다. 같은 변환은 BCE interface와 HTTP Controller 호출에 모두 적용한다.
    """

    _require_identifier_syntax(design_name, "operation name")
    return f"{design_name}Action" if design_name in _JAVA_KEYWORDS else design_name


def _require_identifier(value: str, label: str) -> None:
    if not _valid_identifier(value):
        raise ValueError(f"{label} is not a valid Java identifier: {value}")
