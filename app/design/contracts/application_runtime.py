"""요구사항과 OpenAPI에서 애플리케이션 실행 보안 요구를 읽는다."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ValidationError

from app.design.contracts.api_spec import ApiControlBinding, ApiSpecModel
from app.design.schemas.class_model import BCEModel

SYNTHETIC_UUID_BASIC_USERNAME = "00000000-0000-0000-0000-000000000001"

_SECURITY_WORDS = re.compile(
    r"\b(?:authenticat(?:e|ed|ion)|authoriz(?:e|ed|ation))\b|인증|인가|접근\s*권한",
    re.IGNORECASE,
)
_EXPLICIT_NO_AUTH = re.compile(
    r"\b(?:shall|must|do|does)\s+not\s+(?:require|need)\s+(?:any\s+)?"
    r"(?:authentication|authorization)\b"
    r"|\b(?:authentication|authorization)\s+is\s+not\s+(?:required|needed)\b"
    r"|\bno\s+(?:authentication|authorization)\s+(?:is\s+)?(?:required|needed)\b"
    r"|(?<!not\s)(?<!never\s)\b(?:allow|allows|permit|permits)\s+(?:all\s+)?"
    r"(?:requests?|access|use)\s+without\s+(?:authentication|authorization)\b"
    r"|\b(?:access|requests?|use)\s+without\s+(?:authentication|authorization)\s+"
    r"(?:is|are)\s+(?:allowed|permitted)\b"
    r"|(?:인증|인가)(?:을|를|이|가|은|는)?\s*(?:요구|필요(?:로)?)하지\s*않"
    r"|(?:인증|인가)(?:이|가|은|는)?\s*필요(?:가)?\s*없"
    r"|(?:인증|인가)(?:이|가|은|는)?\s*요구되지\s*않"
    r"|(?:인증|인가)\s*없이\s*(?:모든\s*)?(?:요청|접근|이용|사용)(?:을|이|은)?\s*"
    r"(?:허용(?:한다|된다|해야\s*한다)|가능(?:하다|해야\s*한다)|할\s*수\s*있)",
    re.IGNORECASE,
)
_STATEMENT_BOUNDARY = re.compile(r"(?:[.!?;。！？；]+|\r?\n+)\s*")


def _has_positive_security_statement(text: str) -> bool:
    """Keep positive security evidence while ignoring explicit no-auth statements."""

    statements = (item.strip() for item in _STATEMENT_BOUNDARY.split(text))
    return any(
        _SECURITY_WORDS.search(_EXPLICIT_NO_AUTH.sub("", statement))
        for statement in statements
        if statement
    )


def application_security_source_refs(
    api_spec: dict[str, Any] | None,
    refined_requirements: Any,
) -> list[str]:
    """명시적인 인증·인가 요구가 있는 설계 주소를 반환한다."""

    document = api_spec if isinstance(api_spec, dict) else {}
    components = document.get("components")
    schemes = components.get("securitySchemes") if isinstance(components, dict) else None
    paths = document.get("paths")
    api_security = bool(document.get("security") or schemes) or (
        isinstance(paths, dict)
        and any(
            operation.get("security")
            for path_item in paths.values()
            if isinstance(path_item, dict)
            for operation in path_item.values()
            if isinstance(operation, dict)
        )
    )
    refs = ["apiSpec:security"] if api_security else []
    requirements = refined_requirements if isinstance(refined_requirements, list) else []
    for index, item in enumerate(requirements):
        if not isinstance(item, dict) or not _has_positive_security_statement(
            str(item.get("text") or "")
        ):
            continue
        requirement_id = str(item.get("id") or item.get("draft_ref") or index + 1)
        refs.append(f"requirement:{requirement_id}")
    return list(dict.fromkeys(refs))


def application_security_required(
    api_spec: dict[str, Any] | None,
    refined_requirements: Any,
) -> bool:
    """Spring 보안 설정이 필요한 명시 근거가 하나라도 있는지 반환한다."""

    return bool(application_security_source_refs(api_spec, refined_requirements))


class _OpenApiControlBinding(BaseModel):
    """Typed projection of the accepted ``x-easydep-control`` OpenAPI extension."""

    control: str
    method: str
    arguments: dict[str, str]


def _api_bindings(api_model: ApiSpecModel | dict[str, Any]) -> list[ApiControlBinding]:
    """Hydrate accepted API bindings from either canonical model or its OpenAPI view."""

    if isinstance(api_model, ApiSpecModel):
        return [
            endpoint.control_binding
            for endpoint in api_model.Endpoints
            if endpoint.control_binding is not None
        ]
    if not isinstance(api_model, dict):
        return []
    if "Endpoints" in api_model or "Schemas" in api_model:
        try:
            model = ApiSpecModel.model_validate(api_model)
        except ValidationError:
            return []
        return _api_bindings(model)

    # Deployment receives the rendered OpenAPI projection. Its extension is a
    # compact mapping rather than the canonical argument list, so validate it
    # structurally before converting it to the shared API contract type.
    bindings: list[ApiControlBinding] = []
    paths = api_model.get("paths")
    if not isinstance(paths, dict):
        return []
    for path_item in paths.values():
        if not isinstance(path_item, dict):
            continue
        for operation in path_item.values():
            if not isinstance(operation, dict) or "x-easydep-control" not in operation:
                continue
            try:
                projected = _OpenApiControlBinding.model_validate(
                    operation["x-easydep-control"]
                )
                bindings.append(
                    ApiControlBinding(
                        control=projected.control,
                        method=projected.method,
                        arguments=[
                            {"name": name, "source": source}
                            for name, source in projected.arguments.items()
                        ],
                    )
                )
            except ValidationError:
                continue
    return bindings


def authenticated_uuid_context_required(
    api_model: ApiSpecModel | dict[str, Any],
    bce_model: BCEModel | dict[str, Any],
) -> bool:
    """Whether an accepted API binding supplies a UUID Control input from context.

    This is structural: the endpoint binding selects the exact Control and
    method, and the argument selects the exact parameter. Names of users,
    roles, classes, or use cases are never inspected for meaning.
    """

    if isinstance(bce_model, BCEModel):
        classes = bce_model.Classes
    elif isinstance(bce_model, dict):
        try:
            classes = BCEModel.model_validate(bce_model).Classes
        except ValidationError:
            return False
    else:
        return False

    for binding in _api_bindings(api_model):
        controls = [
            class_item
            for class_item in classes
            if class_item.stereotype == "Control"
            and class_item.class_name == binding.control
        ]
        if len(controls) != 1:
            continue
        methods = [
            operation
            for operation in controls[0].operations
            if operation.name == binding.method
        ]
        if len(methods) != 1:
            continue
        operation = methods[0]
        for argument in binding.arguments:
            if not re.fullmatch(r"\$context\.[A-Za-z_][A-Za-z0-9_]*", argument.source):
                continue
            parameters = [
                parameter
                for parameter in operation.parameters
                if parameter.name == argument.name
            ]
            if len(parameters) == 1 and parameters[0].type == "UUID":
                return True
    return False


__all__ = [
    "SYNTHETIC_UUID_BASIC_USERNAME",
    "application_security_required",
    "application_security_source_refs",
    "authenticated_uuid_context_required",
]
