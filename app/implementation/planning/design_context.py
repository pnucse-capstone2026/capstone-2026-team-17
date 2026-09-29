from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import unquote

from app.artifact_trace import TraceRef
from app.artifact_trace_projection import project_artifact_trace
from app.config import settings
from app.design.contracts.api_spec import ApiSpecModel
from app.design.schemas.class_model import BCEModel
from app.design.schemas.sequence_model import SequenceCollection
from app.design.services.class_diagram.scenario import _extension_specs
from app.llm_connection import build_openhands_llm_connection

from ..domain.implementation_ir import (
    ApiPortIR,
    ComponentIR,
    ImplementationIR,
    build_implementation_ir,
)
from ..domain.models import JobSpec
from ..generation.frontend_scaffold import frontend_feature_operations, operation_ids
from ..generation.java_scaffold import controller_body_marker
from ..generation.operation_contracts import build_generated_operation_contracts
from ..generation.persistence_scaffold import persistence_repository_fqcns
from .frontend_contracts import GeneratedClientContracts, GeneratedClientOperation
from .method_projection import MethodProjection, MethodProjectionResult, project_method_calls


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    control: str
    prompt_file: str
    context_file: str
    allowed_write_paths: list[str]
    immutable_paths: list[str]
    source_artifacts: dict[str, str]
    prompt_sha256: str
    llm: dict[str, object]
    owner: str
    task_type: str = "control"
    # ``allowed_write_paths`` is the complete editable scope.  A work unit can
    # therefore fix a related source file instead of handing the error to a
    # file owner.  ``required_output_paths`` keeps the smaller deterministic
    # completion contract used to decide whether the first implementation
    # request produced every required artifact.
    required_output_paths: list[str] | None = None
    # Use-case work units can share an adapter or an Entity body.  The
    # coordinator uses this explicit order instead of relying on task ids.
    depends_on: list[str] = field(default_factory=list)
    requirement_ids: list[str] = field(default_factory=list)
    use_case_ids: list[str] = field(default_factory=list)
    required_test_paths: list[str] = field(default_factory=list)
    # task가 직접 소비하는 설계 주소다. RTM은 이 값을 다시 추측하지 않고 그대로 옮긴다.
    source_refs: list[str] = field(default_factory=list)
    # Spring 설정은 정상 경로에서 코드가 만든다. 이 작업은 최종 build나 HTTP 검사에서
    # 실제 연결 문제가 발견됐을 때만 OpenHands에게 넘기는 수리용 작업이다.
    repair_only: bool = False
    # 다른 기능 작업과 겹치지 않는 package만 새 파일 생성을 허용한다. 기존 계약 파일은
    # immutable_paths가 계속 보호한다.
    allowed_write_roots: list[str] = field(default_factory=list)
    # Testing feedback 작업만 사용한다. 실패 당시 고정한 OpenAPI·case 등을 같은
    # run_task_check에 넘겨 수리 전후 검사가 달라지지 않게 한다.
    verification_profile: dict[str, object] = field(default_factory=dict)
    # Most tasks always invoke their implementation agent. Integration can first
    # reuse its canonical check and invoke the agent only when a repair is needed.
    completion_mode: str = 'agent'
    owner_tool_mode: str | None = None
    required_completion_markers: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.required_output_paths is None:
            object.__setattr__(self, "required_output_paths", list(self.allowed_write_paths))

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class _UseCaseBundle:
    use_case_ids: tuple[str, ...]
    components: tuple[ComponentIR, ...]
    ports: tuple[ApiPortIR, ...]
    endpoints: tuple[dict[str, object], ...]


def generate_backend_owner_tasks(spec: JobSpec, run_root: Path) -> list[TaskSpec]:
    """Materialize one exact-source backend task for every writable implementation file."""
    package_path = spec.base_package.replace(".", "/")
    java_root = run_root / "application" / "src" / "main" / "java" / package_path
    ir = build_implementation_ir(spec, run_root)
    output = run_root / "reports" / "implementation-tasks"
    output.mkdir(parents=True, exist_ok=True)
    endpoints = _api_model_endpoints(spec)
    _requirements, use_cases, _sources = _all_requirement_artifacts(spec)
    component_ids = _component_use_case_ids(spec)
    use_case_ids = {
        *_artifact_ids(use_cases),
        *(value for values in component_ids.values() for value in values),
        *(value for endpoint in endpoints for value in _use_case_ids(endpoint)),
    }
    bundle = _UseCaseBundle(
        tuple(sorted(use_case_ids, key=_use_case_sort_key)),
        tuple(item for item in ir.components if _is_work_component(item)),
        tuple(ir.api_ports),
        tuple(endpoints),
    )
    bce_paths = [
        path.relative_to(run_root).as_posix()
        for path in sorted((java_root / "bce").rglob("*.java"))
    ]
    required_sources = _backend_writable_sources(ir, package_path, bundle)
    controller_sources = [
        path
        for path in required_sources
        if "/adapter/in/web/" in path
        and _controller_body_markers_for_source(run_root, path, bundle.endpoints)
    ]
    grouped_sources = [path for path in required_sources if path not in controller_sources]
    marked_sources = _completion_marked_sources(run_root)
    if marked_sources is not None:
        grouped_sources = [path for path in grouped_sources if path in marked_sources]
    source_groups: list[tuple[list[str], tuple[str, str] | None]] = [
        ([path], None) for path in controller_sources
    ]
    for path in grouped_sources:
        # Task ownership follows the writable source file. Splitting one Java
        # class by method marker lets a later owner overwrite an earlier edit
        # from its stale whole-file snapshot, so all operations in that file
        # are planned together.
        source_groups.append(([path], None))

    entity_sources = {
        owned_sources[0]
        for owned_sources, _assigned_operation in source_groups
        if owned_sources[0] in bce_paths
    }
    entity_task_ids = [
        _backend_source_task_id([source]) for source in sorted(entity_sources)
    ]
    tasks: list[TaskSpec] = []
    previous_task_by_source: dict[str, str] = {}
    for owned_sources, assigned_operation in source_groups:
        source = owned_sources[0]
        dependencies = (
            [previous_task_by_source[source]]
            if source in previous_task_by_source
            else []
        )
        if source not in entity_sources:
            dependencies.extend(entity_task_ids)
        task = _build_backend_owner_task(
            spec,
            run_root,
            ir,
            output,
            package_path,
            bce_paths,
            bundle,
            owned_sources=owned_sources,
            assigned_operation=assigned_operation,
            depends_on=dependencies,
        )
        tasks.append(task)
        previous_task_by_source[source] = task.task_id
    return tasks


def generate_backend_unit_test_tasks(
    spec: JobSpec, run_root: Path, subject_tasks: list[TaskSpec]
) -> list[TaskSpec]:
    """Plan one test-only owner for each backend implementation source owner."""
    result: list[TaskSpec] = []
    for subject in subject_tasks:
        source_paths = [
            path for path in subject.required_output_paths or [] if path.endswith(".java")
        ]
        if not source_paths:
            continue
        source = source_paths[0]
        source_parts = Path(source).parts
        try:
            java_index = source_parts.index("java")
        except ValueError:
            continue
        package_parts = list(source_parts[java_index + 1 : -1])
        package_name = ".".join(package_parts)
        source_name = Path(source).stem
        test_name = f"{source_name}Test"
        test_path = "/".join(
            ["application/src/test/java", *package_parts, f"{test_name}.java"]
        )
        test_class = ".".join(part for part in (package_name, test_name) if part)
        result.append(
            _build_unit_test_task(
                spec,
                run_root,
                subject,
                task_type="backend-unit-test",
                owner="backend",
                test_path=test_path,
                subject_paths=source_paths,
                verification_profile={
                    "unitTestSubjectPaths": source_paths,
                    "unitTestClass": test_class,
                },
            )
        )
    return result


def generate_frontend_unit_test_tasks(
    spec: JobSpec, run_root: Path, subject_tasks: list[TaskSpec]
) -> list[TaskSpec]:
    """Plan one Vitest owner per frontend feature, with only its test file writable."""
    result: list[TaskSpec] = []
    for subject in subject_tasks:
        source_paths = [
            path
            for path in subject.required_output_paths or []
            if path.startswith("application/frontend/src/features/")
            and path.endswith(".tsx")
        ]
        if not source_paths:
            continue
        source = source_paths[0]
        test_path = source.removesuffix(".tsx") + ".test.tsx"
        result.append(
            _build_unit_test_task(
                spec,
                run_root,
                subject,
                task_type="frontend-unit-test",
                owner="frontend",
                test_path=test_path,
                subject_paths=source_paths,
                verification_profile={"unitTestSubjectPaths": source_paths},
            )
        )
    return result


def _build_unit_test_task(
    spec: JobSpec,
    run_root: Path,
    subject: TaskSpec,
    *,
    task_type: str,
    owner: str,
    test_path: str,
    subject_paths: list[str],
    verification_profile: dict[str, object],
) -> TaskSpec:
    task_id = f"{subject.task_id}-unit-test"
    output = run_root / "reports" / "implementation-tasks"
    output.mkdir(parents=True, exist_ok=True)
    subject_context = _read_json(run_root / subject.context_file)
    design_inputs = subject_context.get("designInputs")
    read_path_set = {
        *subject_paths,
        *(
            str(path)
            for path in subject_context.get("readSourcePaths", [])
            if isinstance(path, str)
        ),
    }
    if isinstance(design_inputs, dict):
        read_path_set.update(str(path) for path in design_inputs.values() if isinstance(path, str))
    read_paths = sorted(read_path_set)
    context = {
        "schemaVersion": "implementation-unit-test-context/v1alpha1",
        "taskId": task_id,
        "taskType": task_type,
        "owner": owner,
        "dependsOn": [subject.task_id],
        "unitTestSubjectPaths": subject_paths,
        "requiredTestPaths": [test_path],
        "readSourcePaths": read_paths,
        "subjectContextPath": subject.context_file,
        **({"designInputs": design_inputs} if isinstance(design_inputs, dict) else {}),
    }
    context_path = output / f"{task_id}.context.json"
    context_path.write_text(json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8")
    language = "Java/JUnit 5 with Mockito" if owner == "backend" else "React/Vitest with Testing Library"
    prompt = f"""# Unit test task: {spec.name} / {Path(subject_paths[0]).stem}

Write focused behavioral unit tests for the completed implementation in `{subject_paths[0]}`.
The current subject source will be supplied as read-only evidence when this task runs; do not
copy it into planning artifacts or modify it.

- Use {language} and write only `{test_path}`. The subject implementation and all API/generated
  contracts are immutable.
- Derive expected behavior from the admitted requirement, operation, and public-contract evidence
  in `{subject.context_file}` and its referenced read-only sources. Assert user-visible or
  collaborator-observable behavior, including a meaningful boundary case when the contract defines one.
- Do not mirror implementation branches mechanically, inspect source text, assert constants without
  behavior, use empty tests, `assert true`, skipped/pending tests, or weaken expectations to fit output.
- Keep dependencies deterministic and local; do not call live services or databases.
- Complete the test source with English identifiers and messages. The focused test runner must execute
  at least one passing test without skipped-only results.
"""
    prompt += render_allowed_output_rules([test_path])
    prompt_path = output / f"{task_id}.prompt.md"
    prompt_path.write_text(prompt, encoding="utf-8")
    task = TaskSpec(
        task_id=task_id,
        control=f"{spec.name} unit tests for {Path(subject_paths[0]).stem}",
        prompt_file=_relative(run_root, prompt_path),
        context_file=_relative(run_root, context_path),
        allowed_write_paths=[test_path],
        required_output_paths=[test_path],
        immutable_paths=sorted(set([*subject_paths, *subject.immutable_paths])),
        source_artifacts=dict(subject.source_artifacts),
        prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        llm=llm_config(spec),
        owner=owner,
        owner_tool_mode="editor",
        task_type=task_type,
        depends_on=[subject.task_id],
        requirement_ids=list(subject.requirement_ids),
        use_case_ids=list(subject.use_case_ids),
        required_test_paths=[test_path],
        source_refs=list(subject.source_refs),
        verification_profile=verification_profile,
    )
    (output / f"{task_id}.task.json").write_text(
        json.dumps(task.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return task


def _is_work_component(component: ComponentIR) -> bool:
    return component.stereotype.casefold() in {
        "control",
        "boundary",
        "entity",
        "gateway",
    }


def _backend_writable_sources(
    ir: ImplementationIR,
    package_path: str,
    bundle: _UseCaseBundle,
) -> list[str]:
    controls = [
        item.name for item in bundle.components if item.stereotype.casefold() == "control"
    ]
    entity_components = [
        item for item in bundle.components if item.stereotype.casefold() == "entity"
    ]
    gateway_kinds = {item.name: item.kind for item in ir.gateways}
    gateways = [item for item in bundle.components if item.name in gateway_kinds]
    return sorted(
        {
            *(
                f"application/src/main/java/{package_path}/application/impl/{name}Service.java"
                for name in controls
            ),
            *(
                f"application/src/main/java/{package_path}/adapter/in/web/{port.name}ApiController.java"
                for port in bundle.ports
            ),
            *(
                _gateway_adapter_path(package_path, item.name, gateway_kinds[item.name])
                for item in gateways
            ),
            *(
                f"application/src/main/java/{package_path}/bce/{item.name}.java"
                for item in entity_components
                if item.operations
            ),
        }
    )


def _completion_marked_sources(run_root: Path) -> set[str] | None:
    contract_path = run_root / "reports" / "generated-operation-contracts.json"
    if not contract_path.is_file():
        return None
    payload = _read_json(contract_path)
    return {
        str(contract.get("writableSource"))
        for contract in payload.get("contracts", [])
        if isinstance(contract, dict)
        and contract.get("writableSource")
        and contract.get("completionMarker")
    }


def _controller_body_markers_for_source(
    run_root: Path,
    source_path: str,
    endpoints: tuple[dict[str, object], ...],
) -> list[str]:
    path = run_root / source_path
    if not path.is_file():
        return []
    scaffold = render_source_contracts(run_root, [path])
    return sorted(
        {
            marker
            for endpoint in endpoints
            for marker in [
                controller_body_marker(
                    str(endpoint.get("method") or ""),
                    str(endpoint.get("path") or ""),
                )
            ]
            if endpoint.get("method") and endpoint.get("path")
            if marker in scaffold
        }
    )


def _controller_contract_use_case_ids(
    contracts: list[dict[str, object]],
    *,
    endpoints: list[dict[str, object]],
    owned_markers: set[str],
) -> set[str]:
    """Trace an owned Controller marker through its exact generated endpoint contract."""
    owned_endpoint_keys = {
        (str(endpoint.get("method") or "").upper(), str(endpoint.get("path") or ""))
        for contract in contracts
        for endpoint in contract.get("endpoints", [])
        if isinstance(contract.get("endpoints", []), list)
        and isinstance(endpoint, dict)
        and endpoint.get("method")
        and endpoint.get("path")
        and controller_body_marker(
            str(endpoint.get("method") or ""), str(endpoint.get("path") or "")
        )
        in owned_markers
    }
    return {
        use_case_id
        for endpoint in endpoints
        if (
            str(endpoint.get("method") or "").upper(),
            str(endpoint.get("path") or ""),
        )
        in owned_endpoint_keys
        for use_case_id in _use_case_ids(endpoint)
    }


def _backend_source_task_id(
    source_paths: list[str], operation_identity: str | None = None
) -> str:
    """Keep task identity stable while making its owned source obvious."""

    ordered_paths = sorted(source_paths)
    if len(ordered_paths) > 1:
        digest = hashlib.sha256("\n".join(ordered_paths).encode("utf-8")).hexdigest()[:12]
        task_id = "implement-backend-domain-core-" + digest
    else:
        source_name = ordered_paths[0].removeprefix(
            "application/src/main/java/"
        ).removesuffix(".java")
        task_id = "implement-backend-" + re.sub(
            r"[^a-z0-9]+", "-", source_name.casefold()
        ).strip("-")
    if operation_identity is None:
        return task_id
    return task_id + "-operation-" + re.sub(
        r"[^a-z0-9]+", "-", operation_identity.casefold()
    ).strip("-")


def _build_backend_owner_task(
    spec: JobSpec,
    run_root: Path,
    ir: ImplementationIR,
    output: Path,
    package_path: str,
    bce_paths: list[str],
    bundle: _UseCaseBundle,
    *,
    owned_sources: list[str],
    assigned_operation: tuple[str, str, str] | None = None,
    depends_on: list[str] | None = None,
    persist: bool = True,
) -> TaskSpec:
    label = ", ".join(bundle.use_case_ids) or "common"
    required = sorted(dict.fromkeys(owned_sources))
    task_id = _backend_source_task_id(
        required,
        operation_identity=assigned_operation[0] if assigned_operation else None,
    )
    task_dependencies = list(depends_on or [])
    all_required = _backend_writable_sources(ir, package_path, bundle)
    if any(path not in all_required for path in required):
        invalid = next(path for path in required if path not in all_required)
        raise ValueError(f"Backend source is not a writable implementation target: {invalid}")
    # persistence 골격은 LLM 작업보다 먼저 생성되고 이후 작업이 수정하지 않는다. 관련
    # Entity와 Repository는 source index가 가리키는 정확한 파일에서 필요한 선언만 읽는다.
    immutable_paths = [
        *(path for path in all_required if path not in required),
        *(path for path in bce_paths if path not in required),
        f"application/src/main/java/{package_path}/api",
        f"application/src/main/java/{package_path}/persistence",
        "application/src/main/resources/db/migration",
    ]
    requirements, use_cases, sources = _all_requirement_artifacts(spec)
    # HTTP Controller는 Boundary adapter를 거치지 않고 typed Control을 직접 호출한다.
    # Boundary가 참조하는 다른 기능 DTO까지 closure에 끌어오지 않고 이번 구현에 실제로
    # 쓰는 Control·Entity·Gateway 계약만 전달한다.
    component_names = {
        item.name for item in bundle.components if item.stereotype.casefold() != "boundary"
    }
    bce_model = BCEModel.model_validate_json(
        spec.inputs["bceModel"].read_text(encoding="utf-8")
    )
    controller_paths = [run_root / path for path in required if "/adapter/in/web/" in path]
    controller_markers_by_path = {
        path.relative_to(run_root).as_posix(): _controller_body_markers_for_source(
            run_root,
            path.relative_to(run_root).as_posix(),
            bundle.endpoints,
        )
        for path in controller_paths
        if path.is_file()
    }
    controller_markers = sorted(
        marker
        for markers in controller_markers_by_path.values()
        for marker in markers
    )
    owned_controller_markers = {
        marker
        for path in required
        for marker in controller_markers_by_path.get(path, [])
    }
    global_operation_contracts_path = run_root / "reports/generated-operation-contracts.json"
    generated_operation_contracts: str | None = None
    task_operation_contracts: list[dict[str, object]] = []
    if global_operation_contracts_path.is_file():
        global_operation_contracts = json.loads(
            global_operation_contracts_path.read_text(encoding="utf-8")
        )
        task_operation_contracts_path = output / f"{task_id}.operation-contracts.json"
        task_operation_contracts = [
            contract
            for contract in global_operation_contracts["contracts"]
            if isinstance(contract, dict)
            and (
                (
                    contract.get("operationId") == assigned_operation[1]
                    and contract.get("completionMarker") == assigned_operation[2]
                    and contract.get("writableSource") in required
                )
                if assigned_operation is not None
                else (
                    contract.get("writableSource") in required
                    or any(
                        isinstance(endpoint, dict)
                        and controller_body_marker(
                            str(endpoint.get("method") or ""),
                            str(endpoint.get("path") or ""),
                        )
                        in owned_controller_markers
                        for endpoint in contract.get("endpoints", [])
                        if isinstance(contract.get("endpoints", []), list)
                    )
                )
            )
        ]
        task_operation_contracts_path.write_text(
            json.dumps(
                {
                    "schemaVersion": global_operation_contracts["schemaVersion"],
                    "contracts": task_operation_contracts,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        generated_operation_contracts = _relative(run_root, task_operation_contracts_path)
    design_inputs = _materialize_design_inputs(
        spec,
        run_root,
        {
            "bceClass",
            "bceModel",
            "sequence",
            "sequenceModel",
            "apiModel",
            "erdBceModel",
            "erdLogicalModel",
            "requirements",
            "useCaseSpec",
        },
    )
    sequence_model = SequenceCollection.model_validate_json(
        spec.inputs["sequenceModel"].read_text(encoding="utf-8")
    )
    method_projection = project_method_calls(
        bce_model=bce_model,
        sequence_model=sequence_model,
    )
    completion_markers: dict[str, set[str]] = {}
    generated_contracts = build_generated_operation_contracts(
        bce_model=bce_model,
        sequence_model=sequence_model,
        api_model=ApiSpecModel.model_validate_json(
            spec.inputs["apiModel"].read_text(encoding="utf-8")
        ),
        base_package=spec.base_package,
        persistence_repositories=persistence_repository_fqcns(
            BCEModel.model_validate_json(spec.inputs["erdBceModel"].read_text(encoding="utf-8")),
            spec.base_package,
            logical_model=_read_json(spec.inputs["erdLogicalModel"]),
        ) if spec.inputs.get("erdBceModel") else {},
    )
    for contract in generated_contracts.contracts:
        if contract.writable_source and contract.completion_marker:
            completion_markers.setdefault(contract.writable_source, set()).add(
                contract.completion_marker
            )
    for path in required:
        if markers := controller_markers_by_path.get(path):
            completion_markers.setdefault(path, set()).update(markers)
    if assigned_operation is not None:
        required_absent_markers = [
            {"path": required[0], "markers": [assigned_operation[2]]}
        ]
    else:
        required_absent_markers = [
            {"path": path, "markers": sorted(markers)}
            for path, markers in sorted(completion_markers.items())
            if path in required
        ]
    # A single Java file is a bounded replace-source contract even when it owns
    # several operation markers: its declarations, operation packets, and
    # bounded Java dependencies are supplied directly to the editor owner.
    owner_tool_mode = (
        "editor"
        if (
            len(required) == 1
            and required[0].endswith(".java")
            and len(required_absent_markers) == 1
            and required_absent_markers[0].get("path") == required[0]
            and bool(required_absent_markers[0].get("markers", []))
        )
        else "restricted"
    )
    method_contexts = _materialize_method_contexts(
        run_root,
        output,
        package_path,
        method_projection,
        requirements=requirements,
        use_cases=use_cases,
        endpoints=list(bundle.endpoints),
        design_inputs=design_inputs,
    )
    owned_method_contexts = [
        item
        for item in method_contexts
        if isinstance(item, dict)
        and set(required).intersection(
            {
            str(path)
            for path in item.get("sourcePaths", [])
            if isinstance(path, str)
            }
        )
    ]
    if assigned_operation is not None:
        assigned_identity, assigned_operation_id, _marker = assigned_operation
        owned_method_contexts = [
            item
            for item in owned_method_contexts
            if item.get("operationId") == assigned_operation_id
            or item.get("stableId") == assigned_identity
        ]
    elif task_operation_contracts:
        # A method-context source path can also name a collaborator reached by
        # another method.  For a file-level task, the generated contract is the
        # exact owner identity; use its stable ID/operation ID rather than
        # pulling that collaborator's scenario into this prompt.
        owned_operation_ids = {
            str(contract.get("operationId"))
            for contract in task_operation_contracts
            if isinstance(contract.get("operationId"), str)
            and contract.get("operationId")
        }
        owned_stable_ids = {
            str(contract.get("stableId"))
            for contract in task_operation_contracts
            if isinstance(contract.get("stableId"), str)
            and contract.get("stableId")
        }
        owned_method_contexts = [
            item
            for item in owned_method_contexts
            if item.get("operationId") in owned_operation_ids
            or item.get("stableId") in owned_stable_ids
        ]
    owned_method_refs = {
        str(ref)
        for item in owned_method_contexts
        for ref in item.get("refs", [])
        if isinstance(ref, str) and ref
    }
    scoped_use_case_ids = sorted(
        {
            ref.removeprefix("use_case:")
            for ref in owned_method_refs
            if ref.startswith("use_case:")
        },
        key=_use_case_sort_key,
    )
    scoped_requirement_ids = sorted(
        {
            ref.removeprefix("requirement:")
            for ref in owned_method_refs
            if ref.startswith("requirement:")
        }
    )
    controller_use_case_ids = _controller_contract_use_case_ids(
        task_operation_contracts,
        endpoints=list(bundle.endpoints),
        owned_markers=owned_controller_markers,
    )
    if not owned_method_contexts and controller_use_case_ids:
        task_use_case_ids = sorted(controller_use_case_ids, key=_use_case_sort_key)
        selected_use_cases = [
            item
            for item in use_cases
            if str(item.get("use_case_id") or item.get("useCaseId") or item.get("id") or "")
            in controller_use_case_ids
        ]
        task_requirement_ids = sorted(
            {
                str(requirement_id)
                for item in selected_use_cases
                for field in ("requirement_ids", "requirementIds", "nfr_ids", "nfrIds")
                for requirement_id in item.get(field, [])
                if isinstance(requirement_id, str) and requirement_id
            }
        )
    else:
        task_use_case_ids = scoped_use_case_ids or sorted(
            bundle.use_case_ids, key=_use_case_sort_key
        )
        task_requirement_ids = scoped_requirement_ids or _artifact_ids(requirements)
    task_source_refs = sorted(
        {
            *owned_method_refs,
            *(f"use_case:{value}" for value in task_use_case_ids),
            *(f"use_case_spec:{value}" for value in task_use_case_ids),
            *_operation_source_refs(spec, set(task_use_case_ids)),
            *_workload_source_refs(_deployment_context(spec, component_names)),
        }
    )
    source_index_path = output / f"{task_id}.source-index.json"
    source_paths = sorted(
        dict.fromkeys(
            path
            for path in [
                *required,
                *_scoped_contract_source_paths(
                    run_root,
                    required,
                    task_operation_contracts,
                    spec.base_package,
                ),
            ]
            if (run_root / path).is_file()
        )
    )
    generated_source_paths = sorted(dict.fromkeys([*source_paths, *required]))
    immutable_import_paths = _local_immutable_java_import_closure(
        run_root,
        generated_source_paths,
        immutable_paths,
    )
    read_evidence_paths = sorted(
        dict.fromkeys(
            [
                *generated_source_paths,
                *immutable_import_paths,
                *[
                    path.relative_to(run_root).as_posix()
                    for path in sorted(
                        (
                            run_root
                            / "application"
                            / "src"
                            / "main"
                            / "java"
                            / package_path
                            / "persistence"
                        ).rglob("*.java")
                    )
                    if path.is_file()
                ],
                *(
                    [generated_operation_contracts]
                    if generated_operation_contracts is not None
                    else []
                ),
            ]
        )
    )
    source_index_path.write_text(
        json.dumps(
            {
                "schemaVersion": "implementation-source-index/v1alpha1",
                "taskId": task_id,
                "startingSourcePaths": source_paths,
                "designInputs": design_inputs,
                "methodContexts": owned_method_contexts,
                **(
                    {"generatedOperationContractsPath": generated_operation_contracts}
                    if generated_operation_contracts is not None
                    else {}
                ),
                "hintsOnly": True,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    context = {
        "schemaVersion": "implementation-context/v1alpha3",
        "taskId": task_id,
        "taskType": "backend-implementation",
        "owner": "backend",
        "dependsOn": task_dependencies,
        "requirementIds": task_requirement_ids,
        "useCaseIds": task_use_case_ids,
        "controllerPaths": [
            path.relative_to(run_root).as_posix() for path in controller_paths if path.is_file()
        ],
        "controllerBodyMarkers": controller_markers,
        "generatedSourcePaths": generated_source_paths,
        "requiredOutputPaths": required,
        "verification": {
            "tool": "run_task_check",
            "policy": "the owner runs the focused check after an edit batch",
        },
        "readSourcePaths": read_evidence_paths,
        "sourceIndexPath": _relative(run_root, source_index_path),
        "methodContextRoot": _relative(run_root, output / "method-context"),
        "designInputs": design_inputs,
        **(
            {"generatedOperationContractsPath": generated_operation_contracts}
            if generated_operation_contracts is not None
            else {}
        ),
    }
    deployment_context = _deployment_context(spec, component_names)
    if deployment_context:
        context["deployment"] = deployment_context
    context_path = output / f"{task_id}.context.json"
    packet_sources = "\n\n".join(
        f"### `{path}`\n```java\n{(run_root / path).read_text(encoding='utf-8').strip()}\n```"
        for path in required
        if (run_root / path).is_file()
    ) or "- no writable Java source is available"
    packet_contracts = json.dumps(
        {"contracts": [_prompt_operation_contract(item) for item in task_operation_contracts]},
        ensure_ascii=False,
        indent=2,
    )
    # Method contexts are already projected by stable operation identity and exact
    # writable source path above.  A file owner needs the same compact behavioral
    # facts as a split owner; withholding them merely because the source has one
    # task makes the model reconstruct the design from broader files.
    packet_operation_behavior = (
        "\n### "
        + (
            "Assigned operation behavior"
            if assigned_operation is not None
            else "Owned operation behavior"
        )
        + "\n```json\n"
        + json.dumps(
            _operation_behavior_packet(run_root, owned_method_contexts),
            ensure_ascii=False,
            indent=2,
        )
        + "\n```\n"
        if owned_method_contexts
        else ""
    )
    marker_instruction = (
        "- Resolve only the assigned completion marker below. Preserve every other marker and "
        "all current edits from preceding tasks; apply a narrow edit instead of restoring or "
        "overwriting the full source from a stale snapshot."
        if assigned_operation is not None
        else "- Resolve every `EASYDEP-IMPLEMENT` and named Controller marker in that source with "
        "contracted behavior. Do not replace them with empty, demo, or always-passing behavior."
    )
    prompt = (
        f"""# Backend source owner: {', '.join(Path(path).stem for path in required)}

Complete the generated backend implementation for the writable sources below.

- Preserve every generated BCE/API public declaration. Implement marked BCE Entity bodies without
  changing their public signatures; keep API, persistence projections, repositories, and migrations frozen.
{marker_instruction}
- Generated class, sequence, RTM, collaborator, and wiring evidence may be incomplete. A missing collaborator
  or wiring entry alone is not an upstream gap when conventional wiring and existing declared dependency APIs
  make behavior unambiguous within the legal writable surface.
- If required public input, output, or externally visible behavior is absent or contradictory, call
  `report_upstream_gap` with one supplied source reference; do not fabricate product meaning.
- Start with one writable source under `Generated source`. Make the first legal `file_editor` edit from its
  local declarations and assigned task behavior. If one concrete dependency signature is needed, inspect only
  that declared read source first; do not spend a turn explaining or broadly exploring.
- Use existing repositories for persistent behavior and constructor injection for Spring beans.
- Generated web Controllers already call their typed Control binding; do not duplicate HTTP or
  Boundary adapters.
- After that first edit, consult only the listed operation contract and declared dependency sources when a concrete implementation need remains. Interaction hints are behavioral evidence; use them to understand delegated behavior, but do not inject dependencies or alter BCE ownership solely because of a hint.
- After an edit batch, call the argument-free `run_task_check` once. Repair only its exact diagnostic and call
  `FinishTool` as soon as the check passes.
- Do not investigate controllers, authentication, build configuration, or migrations unless the
  canonical check diagnostic explicitly names one of them.
- Use English for source comments, tests, validation messages, documentation, and user-visible text.

## Implementation work packet

The writable source and its already-filtered operation contracts are included below. Use them for
the first edit; broader evidence remains available only for a concrete diagnostic.

{packet_sources}

### Operation contracts
```json
{packet_contracts}
```
{packet_operation_behavior}

## Generated source
{chr(10).join(f"- `{path}`" for path in required) or "- none"}

## On-demand evidence
- Operation contract (only for a concrete implementation need): `{generated_operation_contracts or "not available"}`
- Exact generated declarations are listed in the owner workspace guidance. Use `file_editor` only with
  one of those exact file paths; use `grep` to search a containing directory.
- Controller markers: {", ".join(controller_markers) or "none"}
"""
        + render_allowed_output_rules(required)
    )
    prompt_path = output / f"{task_id}.prompt.md"
    if persist:
        context_path.write_text(
            json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        prompt_path.write_text(prompt, encoding="utf-8")
    task = TaskSpec(
        task_id=task_id,
        control=f"{', '.join(Path(path).stem for path in required)}: use cases {', '.join(task_use_case_ids) or label}",
        prompt_file=_relative(run_root, prompt_path),
        context_file=_relative(run_root, context_path),
        allowed_write_paths=required,
        required_output_paths=required,
        immutable_paths=immutable_paths,
        source_artifacts=sources,
        prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        llm=llm_config(spec),
        owner="backend",
        task_type="backend-implementation",
        depends_on=task_dependencies,
        requirement_ids=task_requirement_ids,
        use_case_ids=task_use_case_ids,
        required_test_paths=[],
        source_refs=task_source_refs,
        allowed_write_roots=[],
        verification_profile={"requiredAbsentMarkers": required_absent_markers},
        owner_tool_mode=owner_tool_mode,
    )
    if persist:
        (output / f"{task.task_id}.task.json").write_text(
            json.dumps(task.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return task

def _component_use_case_ids(spec: JobSpec) -> dict[str, set[str]]:
    classes = _read_json(spec.inputs.get("bceModel")).get("Classes", [])
    if not isinstance(classes, list):
        return {}
    return {
        str(item["className"]): _use_case_ids(item)
        for item in classes
        if isinstance(item, dict) and item.get("className")
    }


def _api_model_endpoints(spec: JobSpec) -> list[dict[str, object]]:
    endpoints = _read_json(spec.inputs.get("apiModel")).get("Endpoints", [])
    return (
        [item for item in endpoints if isinstance(item, dict)]
        if isinstance(endpoints, list)
        else []
    )


def _use_case_ids(item: dict[str, object]) -> set[str]:
    values = item.get("use_case_ids") or item.get("useCaseIds") or []
    result = {str(value) for value in values if str(value)} if isinstance(values, list) else set()
    for name in ("use_case_id", "useCaseId"):
        if item.get(name):
            result.add(str(item[name]))
    return result


def _use_case_sort_key(value: str) -> tuple[int, str]:
    match = re.search(r"(\d+)$", value)
    return (int(match.group(1)) if match else 10**9, value)


def _gateway_adapter_path(package_path: str, name: str, kind: str) -> str:
    directory = "persistence" if kind == "persistence" else "gateway"
    adapter = name if kind == "persistence" else f"InMemory{name}"
    return f"application/src/main/java/{package_path}/adapter/out/{directory}/{adapter}Adapter.java"


def _all_requirement_artifacts(
    spec: JobSpec,
) -> tuple[list[dict[str, object]], list[dict[str, object]], dict[str, str]]:
    requirements, requirement_sources = _job_artifact_items(
        spec,
        {"requirements", "refinedrequirements"},
        ("requirements", "refinedRequirements", "refined_requirements"),
    )
    use_cases, use_case_sources = _job_artifact_items(
        spec,
        {"usecases", "usecasespecs", "usecasespec"},
        ("useCases", "useCaseSpecs", "use_case_specs"),
    )
    names = {
        "bceModel",
        "sequenceModel",
        "apiModel",
        "erdBceModel",
        *requirement_sources,
        *use_case_sources,
    }
    return (
        requirements,
        use_cases,
        {name: str(path) for name, path in spec.inputs.items() if name in names and path.is_file()},
    )


def _job_artifact_items(
    spec: JobSpec, input_names: set[str], fields: tuple[str, ...]
) -> tuple[list[dict[str, object]], set[str]]:
    result: list[dict[str, object]] = []
    sources: set[str] = set()
    for name, path in spec.inputs.items():
        if re.sub(r"[^a-z]", "", name.casefold()) not in input_names:
            continue
        value = _read_json_value(path)
        candidates = (
            value
            if isinstance(value, list)
            else next(
                (
                    value.get(field, [])
                    for field in fields
                    if isinstance(value, dict) and field in value
                ),
                [],
            )
        )
        items = (
            [item for item in candidates if isinstance(item, dict)]
            if isinstance(candidates, list)
            else []
        )
        if items:
            result.extend(items)
            sources.add(name)
    return result, sources


def _artifact_ids(items: list[dict[str, object]]) -> list[str]:
    return sorted({str(item["id"]) for item in items if item.get("id")})


def generate_frontend_tasks(
    spec: JobSpec,
    run_root: Path,
) -> list[TaskSpec]:
    """Materialize one frontend owner task for each scaffolded API feature."""
    frontend = run_root / "application" / "frontend"
    generated = frontend / "src" / "generated"
    if not generated.is_dir():
        raise ValueError("OpenAPI Generator frontend client was not found")
    openapi = json.loads(_read(spec.inputs.get("openapi")))
    bce_model = _read_json(spec.inputs.get("bceModel"))
    bce_classes = bce_model.get("Classes", [])
    classes = (
        [item for item in bce_classes if isinstance(item, dict)]
        if isinstance(bce_classes, list)
        else []
    )
    bce_names = {str(item["className"]) for item in classes if item.get("className")}
    operations = operation_ids(openapi)
    client_contracts = GeneratedClientContracts.discover(generated)
    generated_operations = client_contracts.resolve_operations(operations)
    call_skeleton, projected_operations = client_contracts.render_call_skeleton(
        operations, generated_operations
    )
    call_skeleton_path = frontend / "src" / "api.ts"
    call_skeleton_path.write_text(call_skeleton, encoding="utf-8", newline="\n")

    output = run_root / "reports" / "implementation-tasks"
    output.mkdir(parents=True, exist_ok=True)
    design_inputs = _materialize_design_inputs(
        spec,
        run_root,
        {"bceClass", "bceModel", "sequence", "sequenceModel", "openapi"},
    )
    feature_operations = frontend_feature_operations(openapi)
    required = [f"application/frontend/src/features/{slug}.tsx" for slug, _ in feature_operations]
    client_index, operation_context_paths = _frontend_contract_index(
        run_root,
        openapi,
        client_contracts,
        generated_operations,
        required,
    )
    client_index["callSkeletonPath"] = _relative(run_root, call_skeleton_path)
    client_index["fallbackSources"] = design_inputs
    client_index["projectedOperations"] = projected_operations
    client_index["unresolvedOperations"] = sorted(set(operations) - set(projected_operations))
    client_index_path = output / "frontend-generated-client-index.json"
    client_index_path.write_text(
        json.dumps(client_index, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    deployment_context = _deployment_context(spec, {"frontend", *bce_names})
    tasks: list[TaskSpec] = []
    index_operations = {
        str(item.get("operationId")): item
        for item in client_index.get("operations", [])
        if isinstance(item, dict)
    }
    for slug, feature in feature_operations:
        operation_id = str(feature["id"])
        client_entry = index_operations.get(operation_id)
        operation_context_path = (
            str(client_entry["contextPath"])
            if isinstance(client_entry, dict) and client_entry.get("contextPath")
            else None
        )
        feature_path = f"application/frontend/src/features/{slug}.tsx"
        marker = f"EASYDEP-IMPLEMENT: {feature['method']} {feature['path']} ({operation_id})"
        task_id = f"implement-frontend-feature-{slug}"
        task_operation_ids = [operation_id]
        read_paths = sorted(dict.fromkeys([
            *([operation_context_path] if operation_context_path else []),
            *[str(path) for path in design_inputs.values()],
            _relative(run_root, client_index_path),
            "application/frontend/src/api.ts",
        ]))
        context = {
            "schemaVersion": "frontend-implementation-context/v1alpha3",
            "taskId": task_id,
            "taskType": "frontend-implementation",
            "owner": "frontend",
            "dependsOn": [],
            "operationIds": task_operation_ids,
            "featureSlug": slug,
            "completionMarker": marker,
            "generatedImportRoot": client_contracts.import_root,
            "callSkeletonPath": _relative(run_root, call_skeleton_path),
            "clientIndexPath": _relative(run_root, client_index_path),
            "operationContextPaths": [operation_context_path] if operation_context_path else [],
            "designInputs": design_inputs,
            "requiredOutputs": [feature_path],
            "readSourcePaths": read_paths,
        }
        if deployment_context:
            context["deployment"] = deployment_context
        context_path = output / f"{task_id}.context.json"
        context_path.write_text(json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8")
        prompt = f"""# Frontend feature task: {spec.name} / {feature['label']}

Complete the React application using the exact generated-client calls already wired in
`application/frontend/src/api.ts`.

- Treat `application/frontend/src/api.ts` as read-only connector evidence owned by the integration task.
- Preserve `{client_contracts.import_root}` and use `apiCalls`; never hand-write HTTP calls or paths.
- Implement only operation `{operation_id}` using its operation context and the compact client index.
- Edit only `{feature_path}`. Resolve its marker `{marker}` completely.
- Keep imports local to this feature and existing shared contracts so independent feature owners do not overlap.
- Once the compact index and relevant operation contexts define the implementation, the next action is the first source edit.
- Do not inspect generated-client runtime bodies, README files, or package/build metadata without a concrete task-check diagnosis.
- Resolve only the assigned marker `{marker}`. Remove that exact `EASYDEP-IMPLEMENT` marker from the replacement source; completion fails while it remains. Read only the smallest relevant frozen design input if
  an operation context exposes a contract gap.
- Source-index and RTM references are navigation hints, never read or edit limits.
- Keep the existing `HashRouter`, choose page boundaries based on the user experience rather than
  OpenAPI tags, and cover loading, empty, success, validation, and API-error states with accessible
  responsive UI.
- Leave no empty handler, demo fallback, TODO, FIXME, or placeholder, and add no dependency unless
  the existing build requires it.
- Use English for source comments, validation messages, documentation, and user-visible text.

## Client context
- Exact call skeleton: `{_relative(run_root, call_skeleton_path)}`
- Compact index: `{_relative(run_root, client_index_path)}`
"""
        prompt += render_allowed_output_rules([feature_path])
        prompt_path = output / f"{task_id}.prompt.md"
        prompt_path.write_text(prompt, encoding="utf-8")
        operation_context = (
            _read_json(run_root / operation_context_path)
            if operation_context_path
            else {}
        )
        trace_hints = operation_context.get("traceHints", {})
        use_case_ids = _string_ids(trace_hints.get("useCaseIds")) if isinstance(trace_hints, dict) else []
        scenario_refs = _string_ids(trace_hints.get("scenarioStepRefs")) if isinstance(trace_hints, dict) else []
        task = TaskSpec(
            task_id=task_id,
            control=f"{spec.name} frontend feature {feature['label']}",
            prompt_file=_relative(run_root, prompt_path),
            context_file=_relative(run_root, context_path),
            allowed_write_paths=[feature_path],
            required_output_paths=[feature_path],
            immutable_paths=[
                "application/frontend/src/App.tsx",
                "application/frontend/src/styles.css",
                "application/frontend/src/api.ts",
                "application/frontend/src/generated",
            ],
            source_artifacts={
                name: str(path)
                for name, path in spec.inputs.items()
                if name in {"bceModel", "sequenceModel", "openapi", "deploymentBundle"}
                and path.is_file()
            },
            prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            llm=llm_config(spec),
            owner="frontend",
            task_type="frontend-implementation",
            depends_on=[],
            use_case_ids=use_case_ids,
            source_refs=[f"api:{operation_id}", *(f"use_case:{value}" for value in use_case_ids), *(f"step:{value}" for value in scenario_refs), *_workload_source_refs(deployment_context)],
            allowed_write_roots=[],
            required_completion_markers=[marker],
            owner_tool_mode="editor",
            verification_profile={
                "requiredAbsentMarkers": [
                    {"path": feature_path, "markers": [marker]}
                ]
            },
        )
        (output / f"{task_id}.task.json").write_text(
            json.dumps(task.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tasks.append(task)
    return tasks


def _materialize_integration_semantic_evidence(
    spec: JobSpec,
    run_root: Path,
    use_case_ids: list[str],
) -> str:
    """Project the exact public-contract and API binding facts for integration.

    Owner implementation packets intentionally keep this information on demand.  The
    integration admission is a separate semantic decision, though, so it needs the
    public provenance of context-derived values alongside the generated connector
    sources it already reads.
    """

    _, use_cases, _ = _all_requirement_artifacts(spec)
    selected_ids = set(use_case_ids)
    public_contracts = []
    for item in use_cases:
        use_case_id = str(
            item.get("use_case_id") or item.get("useCaseId") or item.get("id") or ""
        )
        contract = item.get("public_contract") or item.get("publicContract")
        if use_case_id not in selected_ids or not isinstance(contract, dict):
            continue
        public_contracts.append(
            {
                "useCaseId": use_case_id,
                "identityObligations": list(
                    contract.get("identity_obligations")
                    or contract.get("identityObligations")
                    or []
                ),
                "requiredValues": list(
                    contract.get("required_values") or contract.get("requiredValues") or []
                ),
            }
        )
    endpoint_bindings = []
    for endpoint in _api_model_endpoints(spec):
        endpoint_use_cases = _use_case_ids(endpoint)
        binding = endpoint.get("control_binding") or endpoint.get("controlBinding")
        if not endpoint_use_cases.intersection(selected_ids) or not isinstance(binding, dict):
            continue
        endpoint_bindings.append(
            {
                "operationId": endpoint.get("operation_id") or endpoint.get("operationId"),
                "method": endpoint.get("method"),
                "path": endpoint.get("path"),
                "useCaseIds": sorted(endpoint_use_cases.intersection(selected_ids)),
                "controlBinding": binding,
            }
        )
    output = run_root / "reports" / "implementation-tasks"
    path = output / "vertical-integration-semantic-evidence.json"
    path.write_text(
        json.dumps(
            {
                "schemaVersion": "vertical-integration-semantic-evidence/v1alpha1",
                "publicContracts": sorted(
                    public_contracts, key=lambda item: str(item["useCaseId"])
                ),
                "apiControlBindings": sorted(
                    endpoint_bindings, key=lambda item: str(item.get("operationId") or "")
                ),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return _relative(run_root, path)


def generate_vertical_integration_task(
    spec: JobSpec,
    run_root: Path,
    prior_tasks: list[dict[str, object]],
) -> TaskSpec:
    """Plan one bounded Implementation pass across the completed owner slices."""

    backend_tasks = [
        task
        for task in prior_tasks
        if task.get("task_type") == "backend-implementation"
    ]
    frontend_tasks = [
        task for task in prior_tasks if task.get("task_type") == "frontend-implementation"
    ]
    unit_test_tasks = [
        task
        for task in prior_tasks
        if task.get("task_type") in {"backend-unit-test", "frontend-unit-test"}
    ]
    if not backend_tasks or not frontend_tasks:
        raise ValueError("Vertical integration requires backend tasks and frontend feature tasks")

    def task_use_cases(task: dict[str, object]) -> list[str]:
        values = task.get("use_case_ids", task.get("useCaseIds", []))
        return [str(value) for value in values if str(value)] if isinstance(values, list) else []

    owner_tasks = [*backend_tasks, *frontend_tasks]
    owner_context_paths = [str(task["context_file"]) for task in owner_tasks]
    owner_contexts = [_read_json(run_root / path) for path in owner_context_paths]
    batch_use_cases = sorted(
        {
            use_case_id
            for task in backend_tasks
            for use_case_id in task_use_cases(task)
        },
        key=_use_case_sort_key,
    )
    semantic_evidence_path = _materialize_integration_semantic_evidence(
        spec, run_root, batch_use_cases
    )
    source_refs = sorted(
        {
            str(value)
            for task in owner_tasks
            for value in task.get("source_refs", [])
            if isinstance(value, str) and value
        }
    )
    dependencies = [
        str(task["task_id"])
        for task in [*owner_tasks, *unit_test_tasks]
        if task.get("task_id")
    ]
    writable_config_candidates = [
        "application/frontend/src/api.ts",
        "application/frontend/src/config.ts",
    ]
    runtime_evidence_candidates = [
        "application/src/main/resources/application.yml",
        "application/frontend/package.json",
        "application/frontend/.env.example",
        "application/deployment/runtime/compose.yaml",
        "application/deployment/runtime/.env.example",
        "application/deployment/tofu/cloud-init.yaml.tftpl",
    ]
    writable_config = [
        path for path in writable_config_candidates if (run_root / path).is_file()
    ]
    runtime_evidence = [
        path for path in runtime_evidence_candidates if (run_root / path).is_file()
    ]
    immutable = sorted(
        (
            {
            str(path)
            for task in owner_tasks
            for path in task.get("immutable_paths", [])
            if isinstance(path, str)
            }
            - set(writable_config_candidates)
        )
        | {"application/frontend/src/generated"}
    )
    writable = _without_immutable_paths(writable_config, immutable)
    frontend_contexts = [
        context for task, context in zip(owner_tasks, owner_contexts)
        if task.get("task_type") == "frontend-implementation"
    ]
    frontend_contract_paths = sorted({
        str(context[key])
        for context in frontend_contexts
        for key in ("clientIndexPath", "callSkeletonPath")
        if isinstance(context.get(key), str)
    })
    operation_context_paths = [
        str(path)
        for context in frontend_contexts
        for path in context.get("operationContextPaths", [])
        if isinstance(path, str)
    ]
    generated_method_paths: list[str] = []
    for operation_context_path in operation_context_paths:
        operation_context = _read_json(run_root / operation_context_path)
        generated_client = operation_context.get("generatedClient")
        generated_method_path = (
            generated_client.get("generatedMethodPath")
            if isinstance(generated_client, dict)
            else None
        )
        if isinstance(generated_method_path, str):
            generated_method_paths.append(generated_method_path)
    owner_outputs = {
        str(path)
        for task in owner_tasks
        for path in task.get("required_output_paths", [])
        if isinstance(path, str)
    }
    read_paths = sorted(
        {
            semantic_evidence_path,
            *frontend_contract_paths,
            *operation_context_paths,
            *generated_method_paths,
            *owner_outputs,
            *runtime_evidence,
            *writable_config,
        }
    )
    frontend_completion_markers = [
        {"path": str(path), "markers": [str(marker)]}
        for task in frontend_tasks
        for path in task.get("required_output_paths", [])
        if isinstance(path, str)
        for marker in task.get("required_completion_markers", [])
        if isinstance(marker, str) and marker
    ]
    task_id = "implement-vertical-integration"
    context = {
        "schemaVersion": "vertical-integration-context/v1alpha1",
        "taskId": task_id,
        "taskType": "integration-implementation",
        "owner": "implementation",
        "dependsOn": dependencies,
        "batchUseCaseIds": batch_use_cases,
        "traceEvidence": {
            "sourceRefs": source_refs,
            "ownerTaskIds": dependencies,
            "semanticEvidencePath": semantic_evidence_path,
        },
        "runtimeConfigPaths": sorted({*runtime_evidence, *writable_config}),
        "readSourcePaths": read_paths,
    }
    deployment_context = next(
        (
            owner_context["deployment"]
            for owner_context in reversed(owner_contexts)
            if isinstance(owner_context.get("deployment"), dict)
        ),
        None,
    )
    if isinstance(deployment_context, dict):
        context["deployment"] = deployment_context
    output = run_root / "reports" / "implementation-tasks"
    context_path = output / f"{task_id}.context.json"
    context_path.write_text(
        json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    prompt = f"""# Thin vertical integration: {spec.name}

Semantic integration admission has already accepted the complete evidence boundary in
`{_relative(run_root, context_path)}`. Review and, only when needed, minimally repair connector
mechanics for one representative happy path after the backend and frontend owners have succeeded.

- Trace one call through the existing frontend `apiCalls` binding, immutable generated API client,
  HTTP endpoint, backend response, and rendered UI success state. Do not reopen or invent admitted
  product and runtime meaning; resolve only concrete connector mechanics in the listed write scope.
- Treat prior owner verification results as accepted evidence. Feature services, entities, and UI
  bodies are read-only here. If the trace finds a defect in any read-only owner, generated-client,
  or deployment file, call `report_upstream_gap` with one supplied source reference so the workflow
  stops with `NEEDS_INPUT` for review or replanning. This is not a repository review.
- Keep Requirements, Design, OpenAPI, generated clients, public BCE/API declarations, and database
  schema unchanged. Edit only the listed durable API/runtime configuration connectors, and make the
  smallest coherent change. Deployment output such as compose, cloud-init, and tofu is read-only.
- Run the supplied integration check once after an edit, or immediately when no edit is needed. It
  runs the backend test suite and frontend production build; call finish when it passes.
"""
    prompt_path = output / f"{task_id}.prompt.md"
    prompt_path.write_text(prompt, encoding="utf-8")
    source_artifacts = {
        str(name): str(path)
        for task in owner_tasks
        for name, path in (
            task.get("source_artifacts", {}).items()
            if isinstance(task.get("source_artifacts"), dict)
            else []
        )
    }
    task = TaskSpec(
        task_id=task_id,
        control=f"{spec.name} thin vertical integration",
        prompt_file=_relative(run_root, prompt_path),
        context_file=_relative(run_root, context_path),
        allowed_write_paths=writable,
        required_output_paths=[],
        immutable_paths=immutable,
        source_artifacts=source_artifacts,
        prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        llm=llm_config(spec),
        owner="implementation",
        task_type="integration-implementation",
        depends_on=dependencies,
        requirement_ids=sorted(
            {
                str(value)
                for task in backend_tasks
                for value in task.get("requirement_ids", [])
                if isinstance(value, str)
            }
        ),
        use_case_ids=batch_use_cases,
        source_refs=source_refs,
        allowed_write_roots=[],
        completion_mode='verify-or-repair',
        verification_profile={
            "requiredAbsentMarkers": frontend_completion_markers
        },
    )
    (output / f"{task_id}.task.json").write_text(
        json.dumps(task.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return task


def render_source_contracts(run_root: Path, paths: list[Path]) -> str:
    sections: list[str] = []
    for path in paths:
        if path.is_file():
            sections.append(
                f"// {path.relative_to(run_root).as_posix()}\n"
                + path.read_text(encoding="utf-8").strip()
            )
    return "\n\n".join(sections) or "// No Java contracts found"


def llm_config(spec: JobSpec) -> dict[str, object]:
    """모든 구현 작업이 공유하는 OpenHands LLM 설정을 만든다."""

    connection = build_openhands_llm_connection()
    return {
        "provider": connection.provider,
        # Stored as execution evidence. Runtime selection still reads the same
        # required root .env settings and never falls back to this snapshot.
        "model": connection.model,
        "baseUrl": connection.base_url,
        "temperature": spec.agent_temperature,
        "maxOutputTokens": spec.agent_max_output_tokens,
        "reasoningEffort": settings.implementation_reasoning_effort,
    }


def _prompt_operation_contract(contract: dict[str, object]) -> dict[str, object]:
    """Keep first-turn behavior facts; the complete sidecar remains readable on demand."""

    prompt_fields = (
        "operationId",
        "signature",
        "returnType",
        "constructorDependencies",
        "collaborators",
        "interactionHints",
        "endpoints",
        "completionMarker",
    )
    return {
        field: contract[field]
        for field in prompt_fields
        if field in contract and contract[field] not in (None, [], {})
    }


def _operation_behavior_packet(
    run_root: Path, method_contexts: list[dict[str, object]]
) -> dict[str, object]:
    """Expose only the owner's exact method behavior without broader design evidence."""

    fields = (
        "method",
        "requirements",
        "scenarioSteps",
        "apiOperations",
        "slices",
        "reasons",
    )
    methods: list[dict[str, object]] = []
    for entry in method_contexts:
        path = entry.get("path")
        if not isinstance(path, str) or not path:
            continue
        payload = _read_json(run_root / path)
        method_packet = {
            field: payload[field]
            for field in fields
            if field in payload and payload[field] not in (None, [], {})
        }
        if method_packet:
            methods.append(method_packet)
    return {"methods": methods}


def render_allowed_output_rules(allowed: list[str]) -> str:
    return "\n\n## Contracted outputs\n\n" + "\n".join(f"- `{path}`" for path in allowed) + "\n"


def _deployment_context(spec: JobSpec, names: set[str]) -> dict[str, object]:
    """구현 대상과 연결된 generatedApplication 실행 조건만 작게 전달한다.

    배포 bundle 전체에는 CSP 계획처럼 코드 task가 소비하지 않는 정보가 많다.
    ArtifactTrace의 typed class→workload 경로가 정확히 있는 workload를 고르고, 연결이
    하나도 없을 때에는 단일 앱인 경우만 fallback하여 실행 계약을 남긴다.
    """
    bundle = _read_json(spec.inputs.get("deploymentBundle"))
    graph = bundle.get("workloadGraph")
    if not isinstance(graph, dict):
        return {}
    generated = [
        item
        for item in graph.get("workloads", [])
        if isinstance(item, dict)
        and str((item.get("artifact") or {}).get("kind") or "") == "generatedApplication"
    ]
    trace = project_artifact_trace(
        {
            "extracted_bce_classes": _read_json(spec.inputs.get("bceModel")),
            "sequence_diagram_model": _read_json(spec.inputs.get("sequenceModel")),
            "api_spec_model": _read_json(spec.inputs.get("apiModel")),
            "erd_bce_classes": _read_json(spec.inputs.get("erdBceModel")),
            "deployment_diagram_bundle": bundle,
        }
    )
    # 이름을 sourceRefs 문자열에서 찾지 않는다. class kind와 정확한 ID로 출발한 뒤
    # projection이 보존한 edge를 따라가 workload kind에 도착한 경우만 연결로 인정한다.
    matched_ids = {
        ref.id
        for name in names
        if name
        for ref in trace.downstream(TraceRef("class", name))
        if ref.kind == "workload"
    }
    matched = [item for item in generated if str(item.get("id") or "") in matched_ids]
    workloads = matched or (generated if len(generated) == 1 else [])
    if not workloads:
        return {}
    workload_ids = {str(item.get("id") or "") for item in workloads}
    return {
        "generatedApplicationCount": len(generated),
        "workloads": [
            {
                "id": item.get("id"),
                "artifact": {
                    "kind": str((item.get("artifact") or {}).get("kind") or ""),
                },
                "interfaces": list(item.get("interfaces") or []),
                # 설정값 자체(특히 secret)는 코드 task가 소비하지 않는다.
                "configuration": [
                    {
                        key: config.get(key)
                        for key in (
                            "id",
                            "name",
                            "kind",
                            "projection",
                            "connectionRef",
                            "sensitive",
                        )
                        if config.get(key) is not None
                    }
                    | (
                        {"value": config.get("value")}
                        if config.get("kind") == "value"
                        and config.get("sensitive") is not True
                        and config.get("value") is not None
                        else {}
                    )
                    for config in item.get("configuration", [])
                    if isinstance(config, dict)
                ],
                "storage": list(item.get("storage") or []),
            }
            for item in workloads
        ],
        "connections": [
            connection
            for connection in graph.get("connections", [])
            if isinstance(connection, dict)
            and {str(connection.get("sourceRef") or ""), str(connection.get("targetRef") or "")}
            & workload_ids
        ],
    }


def _operation_source_refs(spec: JobSpec, use_case_ids: set[str]) -> list[str]:
    """명시된 UC ID가 정확히 연결된 API/BCE operation 주소만 만든다."""

    refs: set[str] = set()
    endpoints = _read_json(spec.inputs.get("apiModel")).get("Endpoints", [])
    if isinstance(endpoints, list):
        for endpoint in endpoints:
            if not isinstance(endpoint, dict):
                continue
            endpoint_use_cases = set(_string_ids(endpoint.get("use_case_ids")))
            operation_id = endpoint.get("operation_id") or endpoint.get("operationId")
            if (
                endpoint_use_cases & use_case_ids
                and isinstance(operation_id, str)
                and operation_id
            ):
                refs.add(f"api:{operation_id}")

    classes = _read_json(spec.inputs.get("bceModel")).get("Classes", [])
    if not isinstance(classes, list):
        return sorted(refs)
    for class_item in classes:
        if not isinstance(class_item, dict):
            continue
        operations = class_item.get("operations", [])
        if not isinstance(operations, list):
            continue
        for operation in operations:
            if not isinstance(operation, dict):
                continue
            step_use_cases = {
                step_ref.partition(":")[0]
                for step_ref in _string_ids(operation.get("stepRefs"))
                if ":" in step_ref
            }
            operation_id = operation.get("operationId")
            if (
                step_use_cases & use_case_ids
                and isinstance(operation_id, str)
                and operation_id
            ):
                refs.add(f"operation:{operation_id}")
    return sorted(refs)


def _workload_source_refs(deployment: dict[str, object]) -> list[str]:
    """exact projection이 고른 workload의 typed 주소만 task에 보존한다."""

    workloads = deployment.get("workloads", [])
    if not isinstance(workloads, list):
        return []
    return [
        f"workload:{item['id']}"
        for item in workloads
        if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]
    ]


def _string_ids(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _read(path: Path | None) -> str:
    return path.read_text(encoding="utf-8") if path and path.is_file() else ""


def _read_json(path: Path | None) -> dict[str, object]:
    """선택 입력이 없거나 JSON object가 아니면 빈 설계로 처리한다."""
    value = _read_json_value(path)
    return value if isinstance(value, dict) else {}


def _read_json_value(path: Path | None) -> object:
    """요구사항 artifact처럼 최상위 list인 선택 입력도 보존한다."""
    try:
        return json.loads(_read(path))
    except json.JSONDecodeError:
        return {}


def _materialize_design_inputs(
    spec: JobSpec,
    run_root: Path,
    names: set[str],
) -> dict[str, str]:
    """Copy frozen design inputs into the run for on-demand agent reads."""
    target_root = run_root / "reports" / "implementation-tasks" / "design-inputs"
    aliases = {
        "requirements": ("refinedRequirements", "requirements", "refined_requirements"),
        "useCaseSpec": ("useCaseSpec", "useCaseSpecs", "useCases", "use_case_specs"),
    }
    materialized: dict[str, str] = {}
    for requested in sorted(names):
        candidates = aliases.get(requested, (requested,))
        source = next((spec.inputs[name] for name in candidates if name in spec.inputs and spec.inputs[name].is_file()), None)
        if source is None:
            continue
        target = target_root / f"{requested}{source.suffix or '.json'}"
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.resolve() != target.resolve():
            target.write_bytes(source.read_bytes())
        materialized[requested] = _relative(run_root, target)
    return materialized


def _materialize_method_contexts(
    run_root: Path,
    output: Path,
    package_path: str,
    projection: MethodProjectionResult,
    *,
    requirements: list[dict[str, object]],
    use_cases: list[dict[str, object]],
    endpoints: list[dict[str, object]],
    design_inputs: dict[str, str],
) -> list[dict[str, object]]:
    """Write one small, on-demand context file per exact BCE operation."""

    context_root = output / "method-context"
    context_root.mkdir(parents=True, exist_ok=True)
    requirements_by_id = {
        str(value["id"]): value
        for value in requirements
        if isinstance(value.get("id"), str) and value["id"]
    }
    entries: list[dict[str, object]] = []
    for item in projection.methods:
        evidence = _method_context_evidence(
            item,
            requirements_by_id=requirements_by_id,
            use_cases=use_cases,
            endpoints=endpoints,
        )
        source_paths = {
            f"application/src/main/java/{package_path}/bce/{item.method.class_name}.java"
        }
        if item.method.stereotype == "Control":
            source_paths.add(
                "application/src/main/java/"
                f"{package_path}/application/impl/{item.method.class_name}Service.java"
            )
        for method_slice in item.slices:
            for call in method_slice.outgoing:
                if call.target is None:
                    continue
                source_paths.add(
                    f"application/src/main/java/{package_path}/bce/{call.target.class_name}.java"
                )
                if call.target.stereotype == "Control":
                    source_paths.add(
                        "application/src/main/java/"
                        f"{package_path}/application/impl/{call.target.class_name}Service.java"
                    )
        path = context_root / f"{item.method.stable_id}.json"
        path.write_text(
            json.dumps(
                {
                    "schemaVersion": "implementation-method-context/v1alpha1",
                    "method": asdict(item.method),
                    "generation": item.generation,
                    "reasons": list(item.reasons),
                    **evidence,
                    "designInputs": design_inputs,
                    "slices": [asdict(value) for value in item.slices],
                    "sourcePaths": sorted(
                        value for value in source_paths if (run_root / value).is_file()
                    ),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        entries.append(
            {
                "operationId": item.method.operation_id,
                "stableId": item.method.stable_id,
                "path": _relative(run_root, path),
                "refs": evidence["refs"],
                "sourcePaths": sorted(source_paths),
            }
        )
    return entries


def _method_context_evidence(
    projection: MethodProjection,
    *,
    requirements_by_id: dict[str, dict[str, object]],
    use_cases: list[dict[str, object]],
    endpoints: list[dict[str, object]],
) -> dict[str, object]:
    """Select only exact requirement, step, call, and API evidence for one method."""

    method = projection.method
    use_case_ids = {
        use_case_id
        for method_slice in projection.slices
        for use_case_id in method_slice.use_case_ids
    }
    step_refs = {
        step_ref
        for method_slice in projection.slices
        for step_ref in method_slice.step_refs
    }
    call_ids = {
        method_slice.incoming_call_id
        for method_slice in projection.slices
        if method_slice.incoming_call_id
    } | {
        call.call_id
        for method_slice in projection.slices
        for call in method_slice.outgoing
        if call.call_id
    }
    matching_use_cases = [
        value
        for value in use_cases
        if str(value.get("use_case_id") or value.get("id") or "") in use_case_ids
    ]
    requirement_ids = {
        str(requirement_id)
        for value in matching_use_cases
        for field in ("requirement_ids", "nfr_ids")
        for requirement_id in value.get(field, [])
        if isinstance(requirement_id, str) and requirement_id
    }
    scenario_steps = []
    for value in matching_use_cases:
        use_case_id = str(value.get("use_case_id") or value.get("id") or "")
        for step in value.get("main_scenario", []):
            if not isinstance(step, dict):
                continue
            step_ref = f"{use_case_id}:main:{step.get('step_number')}"
            if step_ref in step_refs:
                scenario_steps.append({"ref": step_ref, **step})
        for extension, extension_ref in _extension_specs(use_case_id, value):
            for step in extension.get("handling_steps", []):
                if not isinstance(step, dict):
                    continue
                sub_step = str(step.get("sub_step") or "")
                step_ref = f"{extension_ref}:{sub_step}"
                if step_ref in step_refs:
                    scenario_steps.append(
                        {
                            "ref": step_ref,
                            "branchCondition": extension.get("condition"),
                            "outcome": extension.get("outcome"),
                            **step,
                        }
                    )

    api_operations = []
    for endpoint in endpoints:
        binding = endpoint.get("control_binding")
        if not isinstance(binding, dict) or (
            str(binding.get("control") or "") != method.class_name
            or str(binding.get("method") or "") != method.name
        ):
            continue
        api_operations.append(
            {
                key: endpoint[key]
                for key in ("operation_id", "method", "path", "control_binding")
                if endpoint.get(key) is not None
            }
        )

    refs = {
        f"operation:{method.operation_id}",
        *(f"requirement:{value}" for value in requirement_ids),
        *(f"use_case:{value}" for value in use_case_ids),
        *(f"step:{value}" for value in step_refs),
        *(f"call:{value}" for value in call_ids),
        *(
            f"api:{value['operation_id']}"
            for value in api_operations
            if isinstance(value.get("operation_id"), str)
        ),
    }
    return {
        "refs": sorted(refs),
        "requirements": [
            {
                key: requirements_by_id[requirement_id][key]
                for key in ("id", "type", "text")
                if requirements_by_id[requirement_id].get(key) is not None
            }
            for requirement_id in sorted(requirement_ids)
            if requirement_id in requirements_by_id
        ],
        "scenarioSteps": sorted(scenario_steps, key=lambda value: str(value["ref"])),
        "apiOperations": sorted(
            api_operations, key=lambda value: str(value.get("operation_id") or "")
        ),
    }


def _frontend_contract_index(
    run_root: Path,
    openapi: dict[str, object],
    contracts: GeneratedClientContracts,
    generated_operations: dict[str, GeneratedClientOperation],
    required_outputs: list[str],
) -> tuple[dict[str, object], list[str]]:
    """Write a small root index and lazily readable operation contracts."""

    output = run_root / "reports" / "implementation-tasks"
    context_dir = output / "frontend-operation-context"
    context_dir.mkdir(parents=True, exist_ok=True)
    operation_entries: list[dict[str, object]] = []
    operation_context_paths: list[str] = []
    paths = openapi.get("paths", {}) if isinstance(openapi, dict) else {}
    methods = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
    if isinstance(paths, dict):
        for path, path_item in sorted(paths.items()):
            if not isinstance(path_item, dict):
                continue
            for method, operation in sorted(path_item.items()):
                if method.casefold() not in methods or not isinstance(operation, dict):
                    continue
                operation_id = str(operation.get("operationId") or f"{method.upper()} {path}")
                ordinal = len(operation_entries) + 1
                slug = re.sub(r"[^A-Za-z0-9._-]+", "-", operation_id).strip("-.")
                filename = f"{ordinal:04d}-{(slug or 'operation')[:64]}.json"
                context_path = context_dir / filename
                relative_context_path = _relative(run_root, context_path)
                generated = generated_operations.get(operation_id)
                operation_context = _frontend_operation_context(
                    run_root,
                    openapi,
                    path_item,
                    str(path),
                    method,
                    operation_id,
                    operation,
                    generated,
                )
                context_path.write_text(
                    json.dumps(operation_context, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                operation_context_paths.append(relative_context_path)
                operation_entries.append(
                    {
                        "operationId": operation_id,
                        "summary": str(operation.get("summary") or ""),
                        "contextPath": relative_context_path,
                        "generatedClientResolved": generated is not None,
                    }
                )
    entrypoints = [
        "application/frontend/src/App.tsx",
        "application/frontend/src/api.ts",
        "application/frontend/src/main.tsx",
        "application/frontend/src/config.ts",
        "application/frontend/src/styles.css",
    ]
    index = {
        "schemaVersion": "frontend-client-index/v1alpha2",
        "frontendRoot": "application/frontend",
        "entrypoints": [path for path in entrypoints if (run_root / path).is_file()],
        "missingRequiredOutputs": [
            path for path in required_outputs if not (run_root / path).is_file()
        ],
        "verification": {
            "workingDirectory": "application/frontend",
            "command": "npm run build",
        },
        "importRoot": contracts.import_root,
        "operations": operation_entries,
        "indexPath": "reports/implementation-tasks/frontend-generated-client-index.json",
        "hintsOnly": True,
    }
    return index, operation_context_paths


def _frontend_operation_context(
    run_root: Path,
    openapi: dict[str, object],
    path_item: dict[str, object],
    path: str,
    method: str,
    operation_id: str,
    operation: dict[str, object],
    generated: GeneratedClientOperation | None,
) -> dict[str, object]:
    """Project only the explicit contract for one frontend API operation."""

    operation_surface = {
        key: operation[key]
        for key in ("summary", "description", "parameters", "requestBody", "responses")
        if key in operation
    }
    if "parameters" in path_item:
        operation_surface["pathItemParameters"] = path_item["parameters"]
    referenced_components, unresolved_refs = _referenced_openapi_components(
        openapi, operation_surface
    )
    use_case_ids = _string_ids(operation.get("x-easydep-use-case-ids"))
    scenario_step_refs = _string_ids(
        operation.get("x-easydep-scenario-step-refs")
    )
    if generated is None:
        generated_client: dict[str, object] = {
            "resolved": False,
            "call": None,
            "requestType": None,
            "responseType": None,
            "generatedMethodPath": None,
        }
    else:
        request_argument = "request" if generated.request_type else ""
        generated_client = {
            "resolved": True,
            "call": f"apiCalls.{operation_id}({request_argument})",
            "requestType": generated.request_type,
            "responseType": generated.response_type,
            "generatedMethodPath": _relative(run_root, generated.source_path),
        }
    return {
        "schemaVersion": "frontend-operation-context/v1alpha1",
        "operationId": operation_id,
        "http": {
            "method": method.upper(),
            "path": path,
            **operation_surface,
        },
        "generatedClient": generated_client,
        "referencedComponents": referenced_components,
        "unresolvedComponentRefs": unresolved_refs,
        "traceHints": {
            "useCaseIds": use_case_ids,
            "scenarioStepRefs": scenario_step_refs,
        },
        "refs": [
            f"api:{operation_id}",
            *(f"use_case:{value}" for value in use_case_ids),
            *(f"step:{value}" for value in scenario_step_refs),
        ],
        "hintsOnly": True,
    }


def _referenced_openapi_components(
    openapi: dict[str, object], value: object
) -> tuple[dict[str, object], list[str]]:
    """Resolve exact top-level schema refs reached from one operation."""

    components = openapi.get("components")
    schemas = components.get("schemas") if isinstance(components, dict) else None
    schemas = schemas if isinstance(schemas, dict) else {}
    prefix = "#/components/schemas/"
    missing = object()
    pending = sorted(_json_refs(value))
    resolved: dict[str, object] = {}
    unresolved: set[str] = set()
    while pending:
        ref = pending.pop(0)
        if ref in resolved or ref in unresolved:
            continue
        encoded_name = ref.removeprefix(prefix)
        if not ref.startswith(prefix) or "/" in encoded_name:
            unresolved.add(ref)
            continue
        schema_name = unquote(encoded_name).replace("~1", "/").replace("~0", "~")
        target = schemas.get(schema_name, missing)
        if target is missing:
            unresolved.add(ref)
            continue
        resolved[ref] = target
        pending.extend(
            nested
            for nested in sorted(_json_refs(target))
            if nested not in resolved and nested not in unresolved
        )
    return resolved, sorted(unresolved)


def _json_refs(value: object) -> set[str]:
    if isinstance(value, dict):
        refs: set[str] = set()
        for key, item in value.items():
            if key == "$ref" and isinstance(item, str) and item:
                refs.add(item)
            refs.update(_json_refs(item))
        return refs
    if isinstance(value, list):
        refs: set[str] = set()
        for item in value:
            refs.update(_json_refs(item))
        return refs
    return set()


def _work_unit_editable_paths(
    run_root: Path,
    required: list[str],
    scopes: list[str],
) -> list[str]:
    """package 범위를 현재 실행에서 편집할 수 있는 실제 파일 목록으로 바꾼다.

    아직 만들어지지 않은 필수 파일은 그대로 포함하고, 지정한 디렉터리에 이미 있는 파일도
    함께 넣는다. 생성된 공개 계약은 호출자가 이 범위에 넘기지 않는다.
    """
    paths = {path.replace("\\", "/") for path in required}
    for scope in scopes:
        root = run_root / scope
        if root.is_file():
            paths.add(root.relative_to(run_root).as_posix())
        elif root.is_dir():
            paths.update(
                path.relative_to(run_root).as_posix() for path in root.rglob("*") if path.is_file()
            )
    return sorted(paths)


def _local_immutable_java_import_closure(
    run_root: Path,
    seed_paths: list[str],
    immutable_paths: list[str],
) -> list[str]:
    """Return frozen local Java declarations imported by the task's source files."""

    java_root = run_root / "application/src/main/java"
    if not java_root.is_dir():
        return []
    immutable_roots = [
        path.replace(chr(92), "/").rstrip("/") for path in immutable_paths
    ]
    sources: dict[str, str] = {}
    fqcn_paths: dict[str, str] = {}
    package_paths: dict[str, list[str]] = {}
    package_pattern = re.compile(
        r"(?m)^\s*package\s+([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*;"
    )
    import_pattern = re.compile(
        r"(?m)^\s*import\s+(?!static\s)"
        r"([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+(?:\.\*)?)\s*;"
    )
    for source_path in sorted(java_root.rglob("*.java")):
        relative = source_path.relative_to(run_root).as_posix()
        source = source_path.read_text(encoding="utf-8")
        sources[relative] = source
        package = package_pattern.search(source)
        if package is not None:
            package_name = package.group(1)
            fqcn_paths[f"{package_name}.{source_path.stem}"] = relative
            package_paths.setdefault(package_name, []).append(relative)

    pending = [
        path.replace(chr(92), "/")
        for path in seed_paths
        if path.replace(chr(92), "/") in sources
    ]
    visited: set[str] = set()
    closure: set[str] = set()
    while pending:
        relative = pending.pop()
        if relative in visited:
            continue
        visited.add(relative)
        imports = import_pattern.findall(sources[relative])
        for imported_type in imports:
            if imported_type.endswith(".*"):
                imported_package = imported_type[:-2]
                for dependency in package_paths.get(imported_package, []):
                    if dependency in visited or dependency in closure:
                        continue
                    if not any(
                        dependency == root or dependency.startswith(root + "/")
                        for root in immutable_roots
                    ):
                        continue
                    closure.add(dependency)
                    pending.append(dependency)
                continue
            dependency = fqcn_paths.get(imported_type)
            if dependency is None or dependency in visited:
                continue
            if not any(
                dependency == root or dependency.startswith(root + "/")
                for root in immutable_roots
            ):
                continue
            closure.add(dependency)
            pending.append(dependency)
    return sorted(closure)


def _scoped_contract_source_paths(
    run_root: Path,
    required: list[str],
    contracts: list[dict[str, object]],
    base_package: str,
) -> list[str]:
    """Return existing project sources named by scoped contracts or owner imports."""

    project_pattern = re.compile(
        rf"{re.escape(base_package)}(?:\.[A-Za-z_$][\w$]*)+"
    )
    import_pattern = re.compile(
        r"(?m)^\s*import\s+(?!static\s)"
        r"([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+)\s*;"
    )
    paths: set[str] = set()

    def add_fqcn(value: str) -> None:
        candidate = (
            f"application/src/main/java/{value.replace('.', '/')}.java"
        )
        if (run_root / candidate).is_file():
            paths.add(candidate)

    def visit(value: object) -> None:
        if isinstance(value, dict):
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, str):
            for fqcn in project_pattern.findall(value):
                add_fqcn(fqcn)
            if value.endswith(".java") and (run_root / value).is_file():
                paths.add(value)

    for contract in contracts:
        visit(contract)
    for relative in required:
        source = run_root / relative
        if not source.is_file():
            continue
        for imported in import_pattern.findall(source.read_text(encoding="utf-8")):
            if imported.startswith(f"{base_package}."):
                add_fqcn(imported)
    return sorted(paths)


def _without_immutable_paths(paths: list[str], immutable: list[str]) -> list[str]:
    roots = [path.replace("\\", "/").rstrip("/") for path in immutable]
    return [
        path
        for path in paths
        if not any(path == root or path.startswith(root + "/") for root in roots)
    ]


def _relative(root: Path, path: Path) -> str:
    return str(path.relative_to(root)).replace("\\", "/")
