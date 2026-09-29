from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .models import JobSpec


@dataclass(frozen=True)
class ComponentIR:
    name: str
    stereotype: str
    operations: tuple[str, ...]


@dataclass(frozen=True)
class ApiResponseIR:
    status: int
    description: str


@dataclass(frozen=True)
class ApiOperationIR:
    method: str
    path: str
    operation_id: str | None
    responses: tuple[ApiResponseIR, ...]


@dataclass(frozen=True)
class ApiPortIR:
    name: str
    interface_file: str
    operations: tuple[ApiOperationIR, ...]


@dataclass(frozen=True)
class GatewayIR:
    name: str
    kind: str


@dataclass(frozen=True)
class E2EScenarioIR:
    method: str
    path: str
    status: int
    label: str


@dataclass(frozen=True)
class ImplementationIR:
    schema_version: str
    application_name: str
    application_class: str
    components: tuple[ComponentIR, ...]
    controls: tuple[str, ...]
    boundaries: tuple[str, ...]
    entities: tuple[str, ...]
    persistent_entities: tuple[str, ...]
    gateways: tuple[GatewayIR, ...]
    api_ports: tuple[ApiPortIR, ...]
    e2e_scenarios: tuple[E2EScenarioIR, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def build_implementation_ir(
    spec: JobSpec, run_root: Path, *, persist: bool = True
) -> ImplementationIR:
    """저장된 typed 설계 모델을 구현 작업이 쓰는 작은 모델로 옮긴다."""
    bce_model = _read_json(spec.inputs.get("bceModel"))
    api_model = _read_json(spec.inputs.get("apiModel"))
    erd_model = _read_json(spec.inputs.get("erdBceModel"))
    components = tuple(components_from_model(bce_model))
    api_operations = tuple(api_operations_from_model(api_model))
    api_ports = tuple(discover_api_ports(spec, run_root, api_operations))
    gateways = tuple(
        GatewayIR(item.name, "external")
        for item in components
        if item.stereotype.lower() == "gateway"
    )
    erd_entities = entity_names(erd_model)
    ir = ImplementationIR(
        schema_version="implementation-ir/v1alpha1",
        application_name=spec.name,
        application_class=f"{pascal_case(spec.name)}Application",
        components=components,
        controls=tuple(sorted(c.name for c in components if c.stereotype.lower() == "control")),
        boundaries=tuple(sorted(c.name for c in components if c.stereotype.lower() == "boundary")),
        entities=tuple(sorted(c.name for c in components if c.stereotype.lower() == "entity")),
        persistent_entities=tuple(sorted(erd_entities)),
        gateways=gateways,
        api_ports=api_ports,
        e2e_scenarios=tuple(derive_e2e_scenarios(api_operations)),
    )
    if persist:
        target = run_root / "reports" / "implementation-ir.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(ir.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return ir


def components_from_model(model: dict[str, object]) -> list[ComponentIR]:
    """BCEModel의 class와 operation을 문자열 다이어그램을 거치지 않고 읽는다."""
    classes = model.get("Classes", [])
    if not isinstance(classes, list):
        return []
    result: list[ComponentIR] = []
    for item in classes:
        if not isinstance(item, dict) or not item.get("className"):
            continue
        operations = item.get("operations", [])
        result.append(
            ComponentIR(
                name=str(item["className"]),
                stereotype=str(item.get("stereotype", "")),
                operations=tuple(
                    _operation_contract(operation)
                    for operation in operations
                    if isinstance(operation, dict) and operation.get("name")
                ) if isinstance(operations, list) else (),
            )
        )
    return result


def api_operations_from_model(model: dict[str, object]) -> list[ApiOperationIR]:
    """ApiSpecModel endpoint를 구현·HTTP 검사에 필요한 항목으로 줄인다."""
    endpoints = model.get("Endpoints", [])
    if not isinstance(endpoints, list):
        return []
    result: list[ApiOperationIR] = []
    for endpoint in endpoints:
        if not isinstance(endpoint, dict) or not endpoint.get("path") or not endpoint.get("method"):
            continue
        responses = endpoint.get("responses", [])
        result.append(
            ApiOperationIR(
                method=str(endpoint["method"]).upper(),
                path=str(endpoint["path"]),
                operation_id=str(endpoint.get("operation_id") or "") or None,
                responses=tuple(
                    ApiResponseIR(
                        int(response.get("status", 200)),
                        str(response.get("description", "")),
                    )
                    for response in responses
                    if isinstance(response, dict)
                ) if isinstance(responses, list) else (),
            )
        )
    return result


def entity_names(model: dict[str, object]) -> set[str]:
    classes = model.get("Classes", [])
    return {
        str(item["className"])
        for item in classes
        if isinstance(item, dict)
        and item.get("className")
        and str(item.get("stereotype", "")).casefold() == "entity"
    } if isinstance(classes, list) else set()


def _operation_contract(operation: dict[str, object]) -> str:
    parameters = operation.get("parameters", [])
    arguments = ", ".join(
        f"{item.get('name', '')}:{item.get('type', 'Object')}"
        for item in parameters
        if isinstance(item, dict)
    ) if isinstance(parameters, list) else ""
    return_type = str(operation.get("returnType") or "void")
    return f"{operation['name']}({arguments}): {return_type}"


def discover_api_ports(
    spec: JobSpec, run_root: Path, operations: tuple[ApiOperationIR, ...]
) -> list[ApiPortIR]:
    package_path = Path(spec.base_package.replace(".", "/"))
    api_root = run_root / "application" / "src" / "main" / "java" / package_path / "api"
    interfaces = sorted(
        path for path in api_root.glob("*Api.java") if path.name != "ApiUtil.java"
    ) if api_root.is_dir() else []
    result: list[ApiPortIR] = []
    for path in interfaces:
        source = path.read_text(encoding="utf-8")
        matched = tuple(
            operation for operation in operations
            if operation.path in source
            or (operation.operation_id and operation.operation_id in source)
        )
        result.append(
            ApiPortIR(
                path.stem.removesuffix("Api"),
                path.relative_to(run_root).as_posix(),
                matched,
            )
        )
    return result


def derive_e2e_scenarios(
    operations: tuple[ApiOperationIR, ...],
) -> list[E2EScenarioIR]:
    """각 API 동작에서 대표 성공 응답 하나만 실제 HTTP 검증 대상으로 고른다.

    오류 응답의 세부 분기는 API adapter 단위 테스트가 맡는다. E2E에서는 같은 경로를
    상태 코드마다 복제하지 않고, 여러 API 호출을 실제 Spring 구성으로 연결하는 사용자
    흐름에 집중한다. 한 동작이 여러 성공 상태를 선언했다면 OpenAPI에 먼저 적힌 응답을
    사용한다.
    """

    scenarios: list[E2EScenarioIR] = []
    for operation in operations:
        response = next(
            (item for item in operation.responses if 200 <= item.status < 300),
            None,
        )
        if response is not None:
            scenarios.append(
                E2EScenarioIR(
                    operation.method,
                    operation.path,
                    response.status,
                    f"{operation.method} {operation.path} success",
                )
            )
    return list(dict.fromkeys(scenarios))


def pascal_case(value: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", value)
    result = "".join(word[:1].upper() + word[1:] for word in words)
    return result or "Generated"


def kebab_case(value: str) -> str:
    s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1-\2", value)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", s1).lower()


camel_to_kebab = kebab_case


def _read_json(path: Path | None) -> dict[str, object]:
    if not path or not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def remove_readonly(function: Any, path: str, _error: Any) -> None:
    """Windows가 복사 파일을 읽기 전용으로 표시한 경우 권한을 풀고 정리를 재시도한다."""
    try:
        os.chmod(path, stat.S_IWRITE)
    except OSError:
        pass
    function(path)

