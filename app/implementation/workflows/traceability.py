"""Requirements and Design Traceability Matrix (RTM) for EasyDep Implementation Agent."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "implementation-rtm-traceability/v1alpha2"
_JAVA_FQCN_TOKEN = re.compile(
    r"\b([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*){2,})\b"
)


def declared_java_source_matches(
    implementation_rtm: dict[str, Any], text: str
) -> list[dict[str, Any]]:
    """Return RTM rows whose declared Java target exactly matches a named FQCN."""

    fqcn_matches = set(_JAVA_FQCN_TOKEN.findall(text))
    mappings = implementation_rtm.get("mappings")
    matched: list[dict[str, Any]] = []
    for mapping in mappings if isinstance(mappings, list) else []:
        if not isinstance(mapping, dict):
            continue
        target = str(mapping.get("target_file") or "").replace("\\", "/")
        prefix = "application/src/main/java/"
        if not target.startswith(prefix) or not target.endswith(".java"):
            continue
        fqcn = target.removeprefix(prefix).removesuffix(".java").replace("/", ".")
        if fqcn in fqcn_matches:
            matched.append(mapping)
    return matched


def build_rtm_traceability_map(spec: Any, run_root: Path) -> dict[str, Any]:
    """Build and persist an RTM Traceability Map linking implementation files to design artifacts."""
    from ..domain.implementation_ir import build_implementation_ir

    ir = build_implementation_ir(spec, run_root)
    package_path = spec.base_package.replace(".", "/")
    package_root = run_root / "application" / "src" / "main" / "java" / package_path
    mappings: list[dict[str, Any]] = []

    # 1. Control Services & Contracts (BCE)
    for control in ir.controls:
        contract_file = package_root / "bce" / f"{control}.java"
        impl_file = package_root / "application" / "impl" / f"{control}Service.java"
        mappings.append({
            "target_file": _posix(contract_file, run_root),
            "element_name": control,
            "origin_artifact": "bceModel",
            "origin_element": f"component {control} <<control>>",
            "contract_level": "IMMUTABLE_CONTRACT",
            "allowed_edits": [],
            "description": f"BCE Control contract interface for {control}",
        })
        mappings.append({
            "target_file": _posix(impl_file, run_root),
            "element_name": f"{control}Service",
            "origin_artifact": "bceModel",
            "origin_element": f"component {control} <<control>>",
            "contract_level": "IMPLEMENTATION_INTERNAL",
            "allowed_edits": ["METHOD_BODY_ONLY", "PRIVATE_HELPERS"],
            "description": f"Spring Service implementation of Control {control}",
        })
        # Control마다 내용이 거의 같은 테스트 파일을 만들지 않는다. 구현 작업이 만드는
        # ApplicationUseCasesTest 하나가 대표 사용 흐름을 검사하며, 아래 task manifest
        # 순회가 그 실제 파일을 추적표에 추가한다.

    # 2. API Adapters & Schemas (OpenAPI)
    for api_port in ir.api_ports:
        api_name = api_port.name
        interface_file = package_root / "api" / f"{api_name}Api.java"
        controller_file = package_root / "adapter" / "in" / "web" / f"{api_name}ApiController.java"

        mappings.append({
            "target_file": _posix(interface_file, run_root),
            "element_name": f"{api_name}Api",
            "origin_artifact": "apiModel",
            "origin_element": f"API port {api_name} ({len(api_port.operations)} operations)",
            "contract_level": "IMMUTABLE_CONTRACT",
            "allowed_edits": [],
            "description": f"OpenAPI generated Spring interface for {api_name}",
        })
        mappings.append({
            "target_file": _posix(controller_file, run_root),
            "element_name": f"{api_name}ApiController",
            "origin_artifact": "apiModel",
            "origin_element": f"API port {api_name}",
            "contract_level": "IMPLEMENTATION_INTERNAL",
            "allowed_edits": ["METHOD_BODY_ONLY"],
            "description": f"Spring REST Controller implementation for {api_name}Api",
        })

    # 3. Persistence Entities & Repositories (ERD / BCE)
    for entity in ir.persistent_entities:
        entity_file = package_root / "persistence" / "entity" / f"{entity}Entity.java"
        repo_file = package_root / "persistence" / "repository" / f"{entity}Repository.java"

        mappings.append({
            "target_file": _posix(entity_file, run_root),
            "element_name": f"{entity}Entity",
            "origin_artifact": "erdBceModel",
            "origin_element": f"entity {entity}",
            "contract_level": "DATABASE_SCHEMA_BOUND",
            "allowed_edits": ["GETTERS_SETTERS", "CONSTRUCTORS"],
            "description": f"JPA Entity mapped from ERD entity {entity}",
        })
        mappings.append({
            "target_file": _posix(repo_file, run_root),
            "element_name": f"{entity}Repository",
            "origin_artifact": "erdBceModel",
            "origin_element": f"entity {entity}",
            "contract_level": "IMMUTABLE_CONTRACT",
            "allowed_edits": ["CUSTOM_QUERY_METHODS"],
            "description": f"Spring Data JPA Repository for {entity}",
        })

    # 4. Outbound Gateway Adapters (Sequence / BCE)
    for gateway in ir.gateways:
        gw_name = gateway.name
        gw_contract = package_root / "bce" / f"{gw_name}.java"
        mappings.append({
            "target_file": _posix(gw_contract, run_root),
            "element_name": gw_name,
            "origin_artifact": "sequenceModel",
            "origin_element": f"gateway {gw_name} ({gateway.kind})",
            "contract_level": "IMMUTABLE_CONTRACT",
            "allowed_edits": [],
            "description": f"Outbound Gateway contract interface for {gw_name}",
        })
        adapter_name = (
            f"{gw_name}Adapter"
            if gateway.kind == "persistence"
            else f"InMemory{gw_name}Adapter"
        )
        adapter_area = "persistence" if gateway.kind == "persistence" else "gateway"
        mappings.append({
            "target_file": _posix(
                package_root / "adapter" / "out" / adapter_area / f"{adapter_name}.java",
                run_root,
            ),
            "element_name": adapter_name,
            "origin_artifact": "sequenceModel",
            "origin_element": f"gateway implementation {gw_name}",
            "contract_level": "IMPLEMENTATION_INTERNAL",
            "allowed_edits": ["METHOD_BODY_ONLY", "PRIVATE_HELPERS"],
            "description": f"Outbound adapter implementing sequence gateway {gw_name}",
        })

    # 5. Infrastructure & IaC (Deployment Diagram / Cloud Spec)
    cloud = spec.inputs.get("cloud")
    deployment = spec.inputs.get("deploymentBundle")
    terraform_main = run_root / "application/deployment/tofu/main.tf"
    # 리소스 요구사항 파일이 있다고 해서 IaC가 항상 생성되는 것은 아니다. 배포 설계가
    # 아직 사용자 입력을 기다리는 경우에는 로컬 컨테이너까지만 검증하므로, 실제로 만든
    # Terraform 파일만 추적표에 올린다. IaC 생성 자체의 성공 여부는 배포 렌더러가 먼저
    # 검사하므로 여기서는 존재하지 않는 선택 산출물을 필수 파일로 잘못 세지 않는다.
    if cloud and cloud.is_file() and terraform_main.is_file():
        mappings.append({
            "target_file": "application/deployment/tofu/main.tf",
            "element_name": "TerraformMain",
            "origin_artifact": "cloud",
            "origin_element": "cloud resource specification",
            "contract_level": "INFRASTRUCTURE_SPEC_BOUND",
            "allowed_edits": [],
            "description": "Main Terraform HCL infrastructure declaration",
        })
    dockerfile = run_root / "application/Dockerfile"
    if dockerfile.is_file():
        mappings.append({
            "target_file": "application/Dockerfile",
            "element_name": "Dockerfile",
            "origin_artifact": (
                "deploymentBundle"
                if deployment and deployment.is_file()
                else "cloud" if cloud and cloud.is_file() else "generated-contracts"
            ),
            "origin_element": "deployment topology or local release container",
            "contract_level": "INFRASTRUCTURE_SPEC_BOUND",
            "allowed_edits": ["ENVIRONMENT_VARIABLES"],
            "description": "Container image build spec",
        })

    manifest_path = run_root / "reports" / "run-manifest.json"
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_path.is_file()
        else {}
    )
    tasks = [
        task
        for task in manifest.get("implementation_tasks", [])
        if isinstance(task, dict)
    ]
    tasks_by_target = _tasks_by_target(tasks)

    # 먼저 만든 설계 기반 mapping도 task가 실제로 맡은 파일이면 같은 행에서 task 근거를
    # 보여 줘야 한다. 같은 파일을 두 task가 함께 편집할 수 있으므로 SQL join처럼 행을
    # 하나씩 복제한다. 이렇게 해야 어느 task의 UC와 테스트가 파일에 연결됐는지 잃지 않는다.
    joined: list[dict[str, Any]] = []
    existing_targets = {str(mapping["target_file"]) for mapping in mappings}
    for mapping in mappings:
        matching_tasks = tasks_by_target.get(str(mapping["target_file"]), [])
        if matching_tasks:
            joined.extend(
                {**mapping, **_task_trace_fields(task)}
                for task in matching_tasks
            )
        else:
            joined.append(mapping)

    # OpenHands의 파일 목록은 작업을 시작할 위치를 알려 주는 힌트다. 설계 mapping에 아직
    # 없는 힌트도 버리지 않고 task 산출물 행으로 추가하되, 기존 행을 제한하거나 지우지 않는다.
    for target, matching_tasks in tasks_by_target.items():
        if target in existing_targets:
            continue
        for task in matching_tasks:
            task_sources = _task_source_names(task, spec)
            joined.append({
                "target_file": target,
                "element_name": Path(target).stem,
                "origin_artifact": task_sources[0] if task_sources else "generated-contracts",
                "originArtifacts": task_sources,
                "origin_element": f"implementation task {task.get('task_id')}",
                "contract_level": "IMPLEMENTATION_INTERNAL",
                "allowed_edits": ["TASK_ALLOWLIST"],
                "description": f"Output of {task.get('task_id')}",
                **_task_trace_fields(task),
            })
    mappings = joined

    for mapping in mappings:
        target = run_root / str(mapping["target_file"])
        origin_names = list(mapping.get("originArtifacts", [])) or [
            str(mapping["origin_artifact"])
        ]
        origin_hashes = {
            name: _sha256_file(spec.inputs[name])
            for name in origin_names
            if name in spec.inputs and spec.inputs[name].is_file()
        }
        mapping["verificationStatus"] = "VERIFIED" if target.is_file() else "MISSING"
        mapping["targetSha256"] = _sha256_file(target) if target.is_file() else None
        mapping["originSha256s"] = origin_hashes
        mapping["originSha256"] = next(iter(origin_hashes.values()), None)

    verified = sum(
        1 for mapping in mappings if mapping["verificationStatus"] == "VERIFIED"
    )

    rtm_map = {
        "schemaVersion": SCHEMA_VERSION,
        "applicationName": ir.application_name,
        "basePackage": spec.base_package,
        "mappings": mappings,
        "summary": {
            "expected": len(mappings),
            "verified": verified,
            "missing": len(mappings) - verified,
            "coverage": 1.0 if not mappings else verified / len(mappings),
        },
    }

    reports = run_root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "rtm-traceability-map.json").write_text(
        json.dumps(rtm_map, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return rtm_map


def _tasks_by_target(
    tasks: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """필수 산출물 소유자를 우선하고, 단독 편집 힌트만 fallback으로 쓴다."""

    owners: dict[str, list[dict[str, Any]]] = {}
    hints: dict[str, list[dict[str, Any]]] = {}
    for task in tasks:
        for path in _string_list(task.get("required_output_paths")):
            owners.setdefault(path.replace("\\", "/"), []).append(task)
        for path in _string_list(task.get("allowed_write_paths")):
            hints.setdefault(path.replace("\\", "/"), []).append(task)

    result: dict[str, list[dict[str, Any]]] = {}
    for target in owners.keys() | hints.keys():
        if target in owners:
            result[target] = owners[target]
        elif len(hints[target]) == 1:
            # 여러 task의 allowed 목록에 있는 파일은 단순 수리 후보일 수 있다. 소유자를
            # 임의로 고르지 않고, 한 task만 가리킨 경우에만 시작 힌트를 사용한다.
            result[target] = hints[target]
    return result


def _task_trace_fields(task: dict[str, Any]) -> dict[str, Any]:
    """manifest의 snake_case TaskSpec 값을 RTM의 현재 공개 키로 옮긴다."""

    sources = task.get("source_artifacts", {})
    return {
        "taskId": task.get("task_id"),
        "requirementIds": _string_list(task.get("requirement_ids")),
        "useCaseIds": _string_list(task.get("use_case_ids")),
        "requiredTestPaths": [
            value.replace("\\", "/")
            for value in _string_list(task.get("required_test_paths"))
        ],
        "sourceArtifacts": dict(sources) if isinstance(sources, dict) else {},
        "sourceRefs": sorted(set(_string_list(task.get("source_refs")))),
    }


def _task_source_names(task: dict[str, Any], spec: Any) -> list[str]:
    sources = task.get("source_artifacts", {})
    if not isinstance(sources, dict):
        return []
    return [
        str(name)
        for name in sources
        if str(name) in spec.inputs and spec.inputs[str(name)].is_file()
    ]


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _posix(path: Path, relative_to: Path) -> str:
    try:
        return path.relative_to(relative_to).as_posix()
    except ValueError:
        return path.as_posix()
