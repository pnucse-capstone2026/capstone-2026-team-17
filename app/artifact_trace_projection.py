"""저장된 산출물의 직접 근거를 ``ArtifactTrace``로 읽는 순수 어댑터.

DB·repository·stage service를 호출하지 않는다. 저장 JSON의 확정 ID와 ``sourceRefs``만
사용하며, 이름 유사성이나 부분 문자열로 산출물을 연결하지 않는다.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from app.artifact_trace import ArtifactTrace, TraceNode, TraceRef
from app.design.services.class_diagram.scenario import _extension_specs
from app.validation import stable_digest

_OPENAPI_TRACE_ONLY_FIELDS = frozenset({"x-easydep-scenario-step-refs"})


def same_implementation_contracts(
    source: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> bool:
    """복제 ID와 Testing 전용 trace 확장을 제외한 구현 계약을 비교한다."""

    if set(source) != set(candidate):
        return False
    return all(
        _same_implementation_contract(source[name], candidate[name], name=name) for name in source
    )


def _same_implementation_contract(source: Any, candidate: Any, *, name: str) -> bool:
    if isinstance(source, Mapping) and isinstance(candidate, Mapping):
        source_digest = source.get("digest")
        candidate_digest = candidate.get("digest")
        # MySQL JSON can round-trip catalog floats at a slightly different binary
        # precision. The stored content digest remains the canonical identity.
        if source_digest and source_digest == candidate_digest:
            return True
    return stable_digest(_implementation_contract_value(source, name=name)) == stable_digest(
        _implementation_contract_value(candidate, name=name)
    )


def _implementation_contract_value(value: Any, *, name: str) -> tuple[str, Any]:
    if not isinstance(value, Mapping):
        return "raw", value
    contract = value
    if "content" in contract:
        content = contract.get("content")
        if name == "openapi":
            content = _without_openapi_trace_fields(content)
        return "content", content
    return "digest", contract.get("digest")


def _without_openapi_trace_fields(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _without_openapi_trace_fields(item)
            for key, item in value.items()
            if key not in _OPENAPI_TRACE_ONLY_FIELDS
        }
    if isinstance(value, list):
        return [_without_openapi_trace_fields(item) for item in value]
    return value


def projection_state_from_testing_contracts(
    contracts: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """고정 Testing 계약을 trace projection이 읽는 최소 state로 바꾼다."""
    frozen = _map(contracts)

    def content(name: str) -> Any:
        return _map(frozen.get(name)).get("content")

    return {
        key: value
        for key, value in {
            "refined_requirements": content("requirements"),
            "usecase_spec": content("use_cases"),
            "openapi": content("openapi"),
            "deployment_diagram_bundle": content("deployment"),
        }.items()
        if value is not None
    }


def project_artifact_trace(
    state: Mapping[str, Any] | None,
    implementation_rtm: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
    testing_result: Mapping[str, Any] | None = None,
    implementation_verification: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
) -> ArtifactTrace:
    """한 snapshot에서 requirement→설계→배포→파일/테스트 근거를 투영한다.

    접두사가 없는 ``sourceRefs``는 종류를 추측하지 않고 ``opaque``로 보존한다.
    """

    state = state if isinstance(state, Mapping) else {}
    nodes: list[TraceNode] = []
    requirement_refs = _requirements(nodes, state.get("refined_requirements"))
    specification = _map(state.get("usecase_spec"))
    _use_cases(nodes, specification)
    operations, calls = _bce(nodes, _map(state.get("extracted_bce_classes")))
    _sequence(nodes, _map(state.get("sequence_diagram_model")), calls)
    _api(nodes, _map(state.get("api_spec_model")), operations)
    _openapi(nodes, _map(state.get("openapi")))
    _erd(nodes, _map(state.get("erd_bce_classes")))
    resources_by_workload = _deployment(
        nodes,
        _map(state.get("deployment_diagram_bundle")),
        requirement_refs,
    )
    _implementation(
        nodes,
        implementation_rtm or state.get("implementation_rtm"),
        resources_by_workload,
    )
    _implementation_evidence(nodes, implementation_verification)
    _testing(nodes, testing_result or _map(state.get("testing_result")))
    return ArtifactTrace(nodes)


def _requirements(nodes: list[TraceNode], value: Any) -> dict[str, TraceRef]:
    """refined requirement의 ID와 원문 source_refs를 있는 그대로 보존한다."""

    refs: dict[str, TraceRef] = {}
    for item in _records(value, "requirements"):
        requirement_id = _id(item, "id")
        if requirement_id:
            ref = TraceRef("requirement", requirement_id)
            refs[requirement_id] = ref
            _add(nodes, ref, _source_refs(item))
    return refs


def _use_cases(nodes: list[TraceNode], specification: Mapping[str, Any]) -> None:
    """UC, UC 명세, requirements traceability의 명시 ID 관계만 읽는다."""

    for item in _records(specification.get("use_cases")):
        use_case_id = _id(item, "id")
        if use_case_id:
            _add(
                nodes,
                TraceRef("use_case", use_case_id),
                [
                    *_source_refs(item),
                    *_refs(item, "requirement", "requirement_ids", "nfr_ids"),
                ],
            )

    for item in _records(specification.get("use_case_specs")):
        use_case_id = _id(item, "use_case_id")
        if not use_case_id:
            continue
        spec_ref = TraceRef("use_case_spec", use_case_id)
        _add(
            nodes,
            spec_ref,
            [
                TraceRef("use_case", use_case_id),
                *_source_refs(item),
                *_refs(item, "requirement", "requirement_ids", "nfr_ids"),
            ],
        )
        for step in _records(item.get("main_scenario")):
            number = step.get("step_number")
            if isinstance(number, (int, str)) and str(number):
                _add(
                    nodes,
                    TraceRef("step", f"{use_case_id}:main:{number}"),
                    [spec_ref, *_refs(step, "requirement", "covered_req_ids")],
                )
        for extension, extension_ref in _extension_specs(use_case_id, item):
            for step in _records(extension.get("handling_steps")):
                sub_step = _id(step, "sub_step")
                if sub_step:
                    _add(
                        nodes,
                        TraceRef("step", f"{extension_ref}:{sub_step}"),
                        [spec_ref, *_source_refs(step), *_refs(step, "requirement", "covered_req_ids")],
                    )

    traceability = _map(specification.get("traceability"))
    for requirement_id, item in _map(traceability.get("requirements")).items():
        if (
            not isinstance(requirement_id, str)
            or not requirement_id
            or not isinstance(item, Mapping)
        ):
            continue
        for use_case_id in _strings(
            item.get("use_cases"),
            item.get("realized_by_use_cases"),
            item.get("constrains_use_cases"),
        ):
            _add(
                nodes,
                TraceRef("use_case", use_case_id),
                [TraceRef("requirement", requirement_id)],
            )


def _bce(
    nodes: list[TraceNode], model: Mapping[str, Any]
) -> tuple[dict[tuple[str, str], TraceRef], dict[str, TraceRef]]:
    """BCE Classes/operations/Collaborations의 직접 ID만 투영한다."""

    candidates: dict[tuple[str, str], list[TraceRef]] = {}
    operations_by_legacy: dict[str, TraceRef] = {}
    for item in _records(model.get("Classes")):
        class_name = _id(item, "className")
        if not class_name:
            continue
        class_ref = TraceRef("class", class_name)
        use_cases = _refs(item, "use_case", "use_case_ids")
        _add(nodes, class_ref, [*_source_refs(item), *use_cases])
        for operation in _records(item.get("operations")):
            operation_id = _id(operation, "operationId")
            if not operation_id:
                continue
            legacy_ref = TraceRef("operation", operation_id)
            stable_id = _id(operation, "stableId")
            operation_ref = TraceRef("operation", stable_id or operation_id)
            operations_by_legacy[operation_id] = operation_ref
            _add(
                nodes,
                legacy_ref,
                [
                    class_ref,
                    *use_cases,
                    *_source_refs(operation),
                    *_refs(operation, "step", "stepRefs"),
                ],
            )
            if operation_ref != legacy_ref:
                _add(nodes, operation_ref, [legacy_ref])
            name = _id(operation, "name")
            if name:
                candidates.setdefault((class_name, name), []).append(operation_ref)

    for item in _records(model.get("DataTypes")):
        name = _id(item, "name")
        if name:
            _add(nodes, TraceRef("data_type", name), _source_refs(item))

    calls_by_legacy = {
        call_id: TraceRef("call", _id(call, "stableId") or call_id)
        for item in _records(model.get("Collaborations"))
        for call in _records(item.get("calls"))
        if (call_id := _id(call, "callId"))
    }
    for item in _records(model.get("Collaborations")):
        collaboration_id = _id(item, "collaborationId")
        if not collaboration_id:
            continue
        collaboration_ref = TraceRef("collaboration", collaboration_id)
        use_cases = _refs(item, "use_case", "useCaseIds")
        _add(nodes, collaboration_ref, [*_source_refs(item), *use_cases])
        for call in _records(item.get("calls")):
            call_id = _id(call, "callId")
            if call_id:
                legacy_ref = TraceRef("call", call_id)
                call_ref = calls_by_legacy[call_id]
                receiver_id = _id(call, "receiverOperationId")
                parent_id = _id(call, "parentCallId")
                _add(
                    nodes,
                    legacy_ref,
                    [
                        collaboration_ref,
                        *use_cases,
                        *_source_refs(call),
                        *(
                            [
                                operations_by_legacy.get(
                                    receiver_id, TraceRef("operation", receiver_id)
                                )
                            ]
                            if receiver_id
                            else []
                        ),
                        *(
                            [calls_by_legacy.get(parent_id, TraceRef("call", parent_id))]
                            if parent_id
                            else []
                        ),
                        *_refs(call, "step", "stepRefs"),
                    ],
                )
                if call_ref != legacy_ref:
                    _add(nodes, call_ref, [legacy_ref])

    return (
        {key: values[0] for key, values in candidates.items() if len(values) == 1},
        calls_by_legacy,
    )


def _sequence(
    nodes: list[TraceNode],
    model: Mapping[str, Any],
    calls_by_legacy: Mapping[str, TraceRef],
) -> None:
    """Diagrams/Messages의 UC·call/reply ID를 정확히 BCE call로 연결한다."""

    for diagram in _records(model.get("Diagrams")):
        use_case_id = _id(diagram, "use_case_id")
        if not use_case_id:
            continue
        sequence_ref = TraceRef("sequence", use_case_id)
        _add(nodes, sequence_ref, [TraceRef("use_case", use_case_id), *_source_refs(diagram)])
        for message in _records(diagram.get("Messages")):
            call_id = _id(message, "call_id") or _id(message, "reply_to")
            if not call_id:
                continue
            _add(
                nodes,
                TraceRef("message", call_id),
                [
                    sequence_ref,
                    calls_by_legacy.get(call_id, TraceRef("call", call_id)),
                    *_source_refs(message),
                    *_refs(message, "use_case", "use_case_ids"),
                    *_refs(message, "step", "step_ids"),
                ],
            )


def _api(
    nodes: list[TraceNode],
    model: Mapping[str, Any],
    operations: Mapping[tuple[str, str], TraceRef],
) -> None:
    """API endpoint/schema를 추적하고 확정 binding일 때만 BCE operation을 잇는다."""

    for item in _records(model.get("Endpoints")):
        method, path = _id(item, "method"), _id(item, "path")
        # UI feedback과 저장 모델이 사용하는 operationId를 우선한다. operationId가
        # 없는 불완전 draft만 HTTP method/path를 임시 주소로 사용한다.
        endpoint_id = _id(item, "operation_id")
        if not endpoint_id and method and path:
            endpoint_id = f"{method.upper()} {path}"
        if not endpoint_id:
            continue
        sources = [
            *_source_refs(item),
            *_refs(item, "class", "source_classes"),
            *_refs(item, "use_case", "use_case_ids"),
            *_refs(item, "schema", "request_schema"),
        ]
        for response in _records(item.get("responses")):
            sources.extend(_refs(response, "schema", "schema_name"))
        binding = _map(item.get("control_binding"))
        control, method_name = _id(binding, "control"), _id(binding, "method")
        operation = operations.get((control, method_name)) if control and method_name else None
        if operation:
            sources.append(operation)
        interaction_id = _id(item, "interaction_id") or ""
        boundary_signature, separator, _control_signature = interaction_id.partition(" -> ")
        boundary_owner, owner_separator, boundary_call = boundary_signature.partition("::")
        boundary_method = boundary_call.partition("(")[0].strip()
        boundary_operation = (
            operations.get((boundary_owner, boundary_method))
            if separator and owner_separator and boundary_method
            else None
        )
        if boundary_operation:
            sources.append(boundary_operation)
        _add(nodes, TraceRef("api", endpoint_id), sources)

    for item in _records(model.get("Schemas")):
        name = _id(item, "name")
        if name:
            _add(
                nodes,
                TraceRef("schema", name),
                [*_source_refs(item), *_refs(item, "class", "source_class")],
            )


def _openapi(nodes: list[TraceNode], document: Mapping[str, Any]) -> None:
    """고정 OpenAPI의 operationId와 명시된 UC extension만 읽는다."""
    methods = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
    for path_item in _map(document.get("paths")).values():
        if not isinstance(path_item, Mapping):
            continue
        for method, operation in path_item.items():
            if str(method).lower() not in methods or not isinstance(operation, Mapping):
                continue
            operation_id = _id(operation, "operationId")
            if operation_id:
                _add(
                    nodes,
                    TraceRef("api", operation_id),
                    [
                        *_source_refs(operation),
                        *_refs(
                            operation,
                            "use_case",
                            "x-easydep-use-case-ids",
                        ),
                    ],
                )


def _erd(nodes: list[TraceNode], model: Mapping[str, Any]) -> None:
    """ERD Entity는 동명 BCE Entity의 결정론적 투영으로만 연결한다."""

    for item in _records(model.get("Classes")):
        name = _id(item, "className")
        if name and item.get("stereotype") == "Entity":
            _add(
                nodes,
                TraceRef("entity", name),
                [
                    TraceRef("class", name),
                    *_refs(item, "use_case", "use_case_ids"),
                    *_source_refs(item),
                ],
            )


def _deployment(
    nodes: list[TraceNode],
    bundle: Mapping[str, Any],
    requirement_refs: Mapping[str, TraceRef],
) -> dict[TraceRef, set[TraceRef]]:
    """WorkloadGraph와 각 projection ResourcePlan의 식별 가능한 sourceRefs를 읽는다."""

    resources_by_workload: dict[TraceRef, set[TraceRef]] = {}
    fact_refs: dict[str, TraceRef] = {}
    for fact in _records(_map(bundle.get("planningFacts")).get("facts")):
        identifier = _id(fact, "id")
        if identifier:
            fact_ref = TraceRef("planning_fact", identifier)
            fact_refs[identifier] = fact_ref
            _add(
                nodes,
                fact_ref,
                _source_refs(fact, requirement_refs),
            )

    graph = _map(bundle.get("workloadGraph"))
    exact_refs = {**fact_refs, **requirement_refs}
    workload_refs: dict[str, TraceRef] = {}
    for item in _records(graph.get("workloads")):
        identifier = _id(item, "id")
        if not identifier:
            continue
        workload_ref = TraceRef("workload", identifier)
        workload_refs[identifier] = workload_ref
        _add(nodes, workload_ref, _source_refs(item, exact_refs))

    for collection, kind in (
        ("externalDependencies", "external_dependency"),
        ("connections", "connection"),
        ("constraints", "constraint"),
    ):
        for item in _records(graph.get(collection)):
            identifier = _id(item, "id")
            if identifier:
                _add(
                    nodes,
                    TraceRef(kind, identifier),
                    _source_refs(item, exact_refs),
                )

    for projection in _records(bundle.get("projections")):
        provider = _id(projection, "provider")
        region = _id(projection, "region")
        if not provider or not region:
            continue
        target_id = f"{provider}:{region}"
        plan = _map(projection.get("resourcePlan"))
        # ResourcePlan의 top-level collection은 provider 기능이 늘면 함께 늘어난다.
        # 특정 collection 이름을 복제하지 않고 ID/sourceRefs가 있는 record만 읽는다.
        for collection, value in plan.items():
            for item in _records(value):
                identifier = _id(item, "id") or _id(item, "ruleId")
                if identifier:
                    source_tokens = set(_strings(item.get("sourceRefs"), item.get("source_refs")))
                    linked_workloads = {
                        workload_ref
                        for workload_id, workload_ref in workload_refs.items()
                        if workload_id == item.get("workloadRef")
                        or workload_id in source_tokens
                        or workload_ref.format() in source_tokens
                    }
                    resource_ref = TraceRef("resource", f"{target_id}:{collection}:{identifier}")
                    _add(
                        nodes,
                        resource_ref,
                        [*_source_refs(item, exact_refs), *linked_workloads],
                    )
                    for workload_ref in linked_workloads:
                        resources_by_workload.setdefault(workload_ref, set()).add(resource_ref)
    return resources_by_workload


def _implementation(
    nodes: list[TraceNode],
    value: Any,
    resources_by_workload: Mapping[TraceRef, set[TraceRef]],
) -> None:
    """implementation RTM의 taskId→target_file 근거를 requirement/use case와 잇는다."""

    for item in _records(value, "mappings"):
        task_id, target_file = _id(item, "taskId"), _id(item, "target_file")
        sources = [
            *_refs(item, "requirement", "requirementIds"),
            *_refs(item, "use_case", "useCaseIds"),
            *_source_refs(item),
        ]
        sources.extend(
            resource_ref
            for source in tuple(sources)
            for resource_ref in resources_by_workload.get(source, set())
        )
        task_ref = TraceRef("task", task_id) if task_id else None
        if task_ref:
            _add(nodes, task_ref, sources)
        if target_file:
            _add(nodes, TraceRef("file", target_file), [task_ref] if task_ref else sources)


def _implementation_evidence(nodes: list[TraceNode], value: Any) -> None:
    """구현 단계가 남긴 작은 검증 요약을 정확한 task에만 연결한다."""
    for item in _records(value):
        job_id = _id(item, "jobId")
        report = _id(item, "report")
        if not job_id or not report:
            continue
        _add(
            nodes,
            TraceRef("evidence", f"implementation:{job_id}:verification"),
            _refs(item, "task", "taskIds"),
        )


def _testing(nodes: list[TraceNode], result: Mapping[str, Any]) -> None:
    """Connect Arazzo workflows to requirements, use cases, APIs, and findings."""

    report = _map(result.get("dynamic_functional_report"))
    if not report:
        verification_reports = _map(_map(result.get("verification")).get("reports"))
        report = _map(verification_reports.get("dynamicFunctional"))
    report = _map(report) if report else result
    digest = _id(report, "candidateDigest") or "unversioned"
    for workflow in _records(_map(report.get("candidatePlan")).get("workflows")):
        workflow_id = _id(workflow, "workflowId")
        if not workflow_id:
            continue
        trace = _map(workflow.get("x-easydep-trace"))
        sources = [
            *_refs(trace, "requirement", "requirementIds"),
            *_refs(trace, "use_case", "useCaseIds"),
        ]
        sources.extend(
            TraceRef("api", operation_id)
            for step in _records(workflow.get("steps"))
            if (operation_id := _id(step, "operationId"))
        )
        test_ref = TraceRef("test", f"{digest}:{workflow_id}")
        _add(nodes, test_ref, sources)

    for workflow_result in _records(report.get("workflows")):
        workflow_id = _id(workflow_result, "workflowId")
        finding = _map(_map(workflow_result.get("result")).get("finding"))
        finding_id = _id(finding, "code")
        if workflow_id and finding_id:
            source = TraceRef("test", f"{digest}:{workflow_id}")
            _add(nodes, TraceRef("finding", f"{workflow_id}:{finding_id}"), [source])

    for item in _records(result.get("blocking_findings")):
        finding_id = _id(item, "code") or _id(item, "id")
        if finding_id:
            exact_tests: list[TraceRef] = []
            for value in item.get("target_ids") or []:
                if not isinstance(value, str):
                    continue
                try:
                    ref = TraceRef.parse(value)
                except ValueError:
                    continue
                if ref.kind == "test":
                    exact_tests.append(ref)
            _add(nodes, TraceRef("finding", finding_id), exact_tests)


def _add(nodes: list[TraceNode], ref: TraceRef, sources: Iterable[TraceRef]) -> None:
    nodes.append(TraceNode(ref=ref, direct_sources=tuple(sources)))


def _map(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _records(value: Any, key: str | None = None) -> list[Mapping[str, Any]]:
    if key and isinstance(value, Mapping):
        value = value.get(key)
    if isinstance(value, Mapping):
        return [value]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [item for item in value if isinstance(item, Mapping)]
    return []


def _id(item: Mapping[str, Any], key: str) -> str | None:
    value = item.get(key)
    return value if isinstance(value, str) and value else None


def _strings(*values: Any) -> list[str]:
    result: list[str] = []
    for value in values:
        if isinstance(value, str) and value:
            result.append(value)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            result.extend(item for item in value if isinstance(item, str) and item)
    return result


def _refs(item: Mapping[str, Any], kind: str, *keys: str) -> list[TraceRef]:
    return [TraceRef(kind, value) for value in _strings(*(item.get(key) for key in keys))]


def _source_refs(
    item: Mapping[str, Any], exact_refs: Mapping[str, TraceRef] | None = None
) -> list[TraceRef]:
    refs: list[TraceRef] = []
    for value in _strings(item.get("sourceRefs"), item.get("source_refs")):
        kind, separator, identifier = value.partition(":")
        if separator and kind and identifier:
            refs.append(TraceRef(kind, identifier))
        elif exact_refs and value in exact_refs:
            refs.append(exact_refs[value])
        else:
            refs.append(TraceRef("opaque", value))
    return refs


__all__ = ["project_artifact_trace", "projection_state_from_testing_contracts"]
