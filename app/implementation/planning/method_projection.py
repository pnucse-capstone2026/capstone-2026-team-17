"""Deterministic, method-local implementation context from typed sequence calls."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace

from app.design.contracts.sequence import method_call_signature
from app.design.schemas.class_model import BCEModel, ClassOperation
from app.design.schemas.sequence_model import (
    SequenceArgument,
    SequenceCollection,
    SequenceFragment,
    SequenceMessage,
)


@dataclass(frozen=True)
class MethodRef:
    class_name: str
    stereotype: str
    operation_id: str
    stable_id: str
    name: str
    parameters: tuple[tuple[str, str], ...]
    return_type: str


@dataclass(frozen=True)
class ArgumentProjection:
    parameter: str
    expected_type: str
    source_kind: str
    source_ref: str
    expression: str | None
    reason: str | None = None


@dataclass(frozen=True)
class CallProjection:
    call_id: str
    target: MethodRef | None
    arguments: tuple[ArgumentProjection, ...]
    result_variable: str | None
    fragments: tuple[dict[str, str], ...]
    generation: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class MethodSlice:
    use_case_ids: tuple[str, ...]
    incoming_call_id: str
    incoming_source: str
    method: MethodRef
    outgoing: tuple[CallProjection, ...]
    return_type: str | None
    step_refs: tuple[str, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class MethodProjection:
    method: MethodRef
    slices: tuple[MethodSlice, ...]
    generation: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class ProjectionDiagnostic:
    use_case_id: str
    call_id: str
    reason: str


@dataclass(frozen=True)
class MethodProjectionResult:
    methods: tuple[MethodProjection, ...]
    diagnostics: tuple[ProjectionDiagnostic, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schemaVersion": "implementation-method-projection/v1alpha1",
            "methods": [asdict(item) for item in self.methods],
            "diagnostics": [asdict(item) for item in self.diagnostics],
        }


@dataclass
class _Frame:
    use_case_id: str
    message: SequenceMessage
    source_class: str
    method: MethodRef | None
    children: list[_Frame]
    reasons: list[str]
    return_message: SequenceMessage | None = None


def project_method_calls(
    *, bce_model: BCEModel, sequence_model: SequenceCollection
) -> MethodProjectionResult:
    """Project exact direct-child calls without inferring domain behavior."""

    by_signature, by_id = _operation_catalog(bce_model)
    frames: list[_Frame] = []
    diagnostics: list[ProjectionDiagnostic] = []
    for diagram in sequence_model.Diagrams:
        participants = {item.alias: item for item in diagram.Participants}
        stack: list[_Frame] = []
        seen_calls: set[str] = set()
        for message in diagram.Messages:
            if message.type in {"sync", "self"}:
                target = participants.get(message.target)
                source = participants.get(message.source)
                target_class = target.source_class if target is not None else ""
                method = by_signature.get(
                    (target_class, method_call_signature(message.label))
                )
                reasons = _call_contract_reasons(message, method)
                if message.call_id in seen_calls:
                    reasons.append("duplicate_call_id")
                seen_calls.add(message.call_id)
                if reasons:
                    diagnostics.extend(
                        ProjectionDiagnostic(diagram.use_case_id, message.call_id, reason)
                        for reason in reasons
                    )
                frame = _Frame(
                    use_case_id=diagram.use_case_id,
                    message=message,
                    source_class=source.source_class if source is not None else "",
                    method=method,
                    children=[],
                    reasons=reasons,
                )
                if stack:
                    stack[-1].children.append(frame)
                frames.append(frame)
                stack.append(frame)
            elif message.type == "return":
                if not stack or stack[-1].message.call_id != message.reply_to:
                    diagnostics.append(
                        ProjectionDiagnostic(
                            diagram.use_case_id,
                            message.reply_to,
                            "return_stack_mismatch",
                        )
                    )
                    continue
                frame = stack.pop()
                frame.return_message = message
                if frame.method is not None and message.label != frame.method.return_type:
                    diagnostics.append(
                        ProjectionDiagnostic(
                            diagram.use_case_id,
                            message.reply_to,
                            "return_type_mismatch",
                        )
                    )
                    frame.reasons.append("return_type_mismatch")
        for frame in stack:
            frame.reasons.append("missing_return")
            diagnostics.append(
                ProjectionDiagnostic(
                    diagram.use_case_id,
                    frame.message.call_id,
                    "missing_return",
                )
            )

    cyclic_edges = _cyclic_edges(frames)
    grouped: dict[str, list[MethodSlice]] = {}
    for frame in frames:
        if frame.method is None:
            continue
        outgoing: list[CallProjection] = []
        prior_children: dict[str, CallProjection] = {}
        for position, child in enumerate(frame.children, start=1):
            call = _project_child_call(
                frame, child, prior_children, cyclic_edges, position
            )
            outgoing.append(call)
            prior_children[child.message.call_id] = call
        return_type = frame.return_message.label if frame.return_message is not None else None
        grouped.setdefault(frame.method.operation_id, []).append(
            MethodSlice(
                use_case_ids=tuple(sorted(set(frame.message.use_case_ids))),
                incoming_call_id=frame.message.call_id,
                incoming_source=frame.source_class,
                method=frame.method,
                outgoing=tuple(outgoing),
                return_type=return_type,
                step_refs=tuple(frame.message.step_ids),
                reasons=tuple(dict.fromkeys(frame.reasons)),
            )
        )

    methods: list[MethodProjection] = []
    for operation_id in sorted(by_id):
        slices = grouped.get(operation_id, [])
        method = by_id[operation_id]
        unique: dict[str, MethodSlice] = {}
        for item in slices:
            fingerprint = _slice_fingerprint(item)
            previous = unique.get(fingerprint)
            if previous is None:
                unique[fingerprint] = item
            else:
                unique[fingerprint] = replace(
                    previous,
                    use_case_ids=tuple(
                        sorted({*previous.use_case_ids, *item.use_case_ids})
                    ),
                    step_refs=tuple(sorted({*previous.step_refs, *item.step_refs})),
                )
        projected_slices = tuple(unique[key] for key in sorted(unique))
        reasons: list[str] = []
        if not projected_slices:
            reasons.append("no_sequence_slice")
        if len(projected_slices) > 1:
            reasons.append("conflicting_scenario_slices")
        for item in projected_slices:
            reasons.extend(item.reasons)
            for call in item.outgoing:
                reasons.extend(call.reasons)
        reasons = list(dict.fromkeys(reasons))
        methods.append(
            MethodProjection(
                method=method,
                slices=projected_slices,
                generation="code" if not reasons else "hint",
                reasons=tuple(reasons),
            )
        )

    return MethodProjectionResult(
        methods=tuple(methods),
        diagnostics=tuple(
            sorted(diagnostics, key=lambda item: (item.use_case_id, item.call_id, item.reason))
        ),
    )


def _operation_catalog(
    model: BCEModel,
) -> tuple[dict[tuple[str, str], MethodRef], dict[str, MethodRef]]:
    by_signature: dict[tuple[str, str], MethodRef] = {}
    by_id: dict[str, MethodRef] = {}
    for owner in model.Classes:
        for operation in owner.operations:
            method = _method_ref(owner.class_name, owner.stereotype, operation)
            signature = method_call_signature(operation.method_signature())
            by_signature[(owner.class_name, signature)] = method
            by_id[method.operation_id] = method
    return by_signature, by_id


def _method_ref(class_name: str, stereotype: str, operation: ClassOperation) -> MethodRef:
    return MethodRef(
        class_name=class_name,
        stereotype=stereotype,
        operation_id=operation.operation_id,
        stable_id=operation.stable_id
        or hashlib.sha256(operation.operation_id.encode("utf-8")).hexdigest(),
        name=operation.name,
        parameters=tuple((item.name, item.type) for item in operation.parameters),
        return_type=operation.return_type,
    )


def _call_contract_reasons(
    message: SequenceMessage, method: MethodRef | None
) -> list[str]:
    if method is None:
        return ["unresolved_target_operation"]
    expected = sorted(method.parameters)
    actual = [(item.parameter, item.type) for item in message.arguments]
    return [] if len(actual) == len(expected) and sorted(actual) == expected else [
        "argument_contract_mismatch"
    ]


def _project_child_call(
    parent: _Frame,
    child: _Frame,
    prior: dict[str, CallProjection],
    cyclic_edges: set[tuple[str, str]],
    position: int,
) -> CallProjection:
    reasons = list(child.reasons)
    arguments: list[ArgumentProjection] = []
    if child.method is None:
        reasons.append("unresolved_target_operation")
    else:
        by_name = {item.parameter: item for item in child.message.arguments}
        ordered_arguments = [
            by_name[name]
            for name, _value_type in child.method.parameters
            if name in by_name
        ]
        for argument in ordered_arguments:
            arguments.append(_project_argument(parent, argument, prior))
        reasons.extend(item.reason for item in arguments if item.reason)
        if parent.method is None or parent.method.stereotype != "Control":
            reasons.append("source_is_not_control_implementation")
        if child.method.stereotype != "Control":
            reasons.append("target_is_not_generated_spring_dependency")
        if (parent.method.operation_id, child.method.operation_id) in cyclic_edges:
            reasons.append("cyclic_method_call")
    fragments = _fragment_dicts(child.message.fragments)
    if fragments:
        reasons.append("fragment_condition_not_typed")
    reasons = list(dict.fromkeys(reason for reason in reasons if reason))
    result_variable = (
        f"{child.method.name}Result{position}"
        if child.method is not None and child.method.return_type != "void"
        else None
    )
    return CallProjection(
        call_id=child.message.call_id,
        target=child.method,
        arguments=tuple(arguments),
        result_variable=result_variable,
        fragments=fragments,
        generation="code" if not reasons else "hint",
        reasons=tuple(reasons),
    )


def _project_argument(
    parent: _Frame,
    argument: SequenceArgument,
    prior: dict[str, CallProjection],
) -> ArgumentProjection:
    expression: str | None = None
    reason: str | None = None
    source_id, separator, source_path = argument.source_ref.partition("#")
    parameter_types = dict(parent.method.parameters) if parent.method is not None else {}
    incoming = {item.source_ref: item for item in parent.message.arguments}
    if argument.source_kind == "call_parameter":
        if (
            separator
            and source_id == parent.message.call_id
            and parameter_types.get(source_path) == argument.type
        ):
            expression = source_path
        else:
            reason = "unresolved_call_parameter"
    elif argument.source_kind == "input":
        source = incoming.get(argument.source_ref)
        if source is not None and parameter_types.get(source.parameter) == argument.type:
            expression = source.parameter
        else:
            reason = "unresolved_input"
    elif argument.source_kind == "call_result":
        call = prior.get(source_id)
        if (
            separator
            and source_path == "result"
            and call is not None
            and call.target is not None
            and call.result_variable is not None
            and call.target.return_type == argument.type
            and call.generation == "code"
        ):
            expression = call.result_variable
        else:
            reason = "unresolved_call_result"
    else:
        reason = f"unsupported_argument_source:{argument.source_kind}"
    return ArgumentProjection(
        parameter=argument.parameter,
        expected_type=argument.type,
        source_kind=argument.source_kind,
        source_ref=argument.source_ref,
        expression=expression,
        reason=reason,
    )


def _fragment_dicts(
    fragments: list[SequenceFragment],
) -> tuple[dict[str, str], ...]:
    return tuple(
        {
            "id": item.id,
            "type": item.type,
            "branch": item.branch,
            "condition": item.condition,
        }
        for item in fragments
    )


def _slice_fingerprint(item: MethodSlice) -> str:
    payload = {
        "outgoing": [
            {
                "target": call.target.operation_id if call.target else None,
                "arguments": [
                    (
                        arg.parameter,
                        arg.expected_type,
                        arg.source_kind,
                        arg.expression,
                        arg.reason,
                    )
                    for arg in call.arguments
                ],
                "fragments": call.fragments,
                "generation": call.generation,
                "reasons": call.reasons,
            }
            for call in item.outgoing
        ],
        "returnType": item.return_type,
        "reasons": item.reasons,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _cyclic_edges(frames: list[_Frame]) -> set[tuple[str, str]]:
    edges = {
        (frame.method.operation_id, child.method.operation_id)
        for frame in frames
        if frame.method is not None
        for child in frame.children
        if child.method is not None
    }
    adjacency: dict[str, set[str]] = {}
    for source, target in edges:
        adjacency.setdefault(source, set()).add(target)

    def reaches(start: str, target: str) -> bool:
        pending = [start]
        visited: set[str] = set()
        while pending:
            current = pending.pop()
            if current == target:
                return True
            if current in visited:
                continue
            visited.add(current)
            pending.extend(adjacency.get(current, ()))
        return False

    return {
        edge for edge in edges if edge[0] == edge[1] or reaches(edge[1], edge[0])
    }
