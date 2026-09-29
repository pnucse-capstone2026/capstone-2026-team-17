"""Application-managed stable identities for accepted class-diagram artifacts.

``operationId`` and ``callId`` are intentionally still canonicalized by the
schema.  They are useful renderer/collaboration projections, but they are not
safe rename identities: operation signatures and call positions change during
normal revisions.  This module assigns opaque, deterministic ``stableId``
values at the acceptance boundary and reconciles them only when an exact
identity or unambiguous structural provenance exists.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from app.artifact_trace import TraceRef
from app.design.schemas.class_model import BCEModel


def _text(value: Any) -> str:
    return str(value or "").strip()


def _step_refs(value: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_text(item) for item in value.get("stepRefs") or () if _text(item)))


def _token_variants(value: str) -> set[str]:
    """Normalize catalog aliases while keeping matching exact and structured."""

    token = _text(value)
    if not token:
        return set()
    variants = {token}
    try:
        parsed = TraceRef.parse(token)
    except (TypeError, ValueError):
        parsed = None
    if parsed is not None and parsed.kind in {"operation", "call", "class_diagram"}:
        variants.add(parsed.id)
    return variants


def _opaque_id(kind: str, seed: Any, occupied: set[str]) -> str:
    """Return a deterministic opaque ID, extending only on a real collision."""

    encoded = json.dumps(seed, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    candidate = f"{kind}_{digest[:24]}"
    if candidate not in occupied:
        return candidate
    suffix = 1
    while f"{candidate}_{suffix}" in occupied:
        suffix += 1
    return f"{candidate}_{suffix}"


def stable_operation_id(
    class_name: str, operation_id: str, *, occupied: set[str] | None = None,
) -> str:
    """Build the deterministic fallback identity for an operation."""

    return _opaque_id("op", [class_name, operation_id], occupied or set())


def stable_parameter_ref(
    operation_stable_id: str, position: int, *, occupied: set[str] | None = None,
) -> str:
    """Build an opaque parameter identity from its accepted parent and ordinal."""

    return _opaque_id("param", [operation_stable_id, position], occupied or set())


def stable_data_type_id(name: str, *, occupied: set[str] | None = None) -> str:
    """Build the conservative fallback identity for a declared DataType."""

    return _opaque_id("dtype", [name], occupied or set())


def stable_class_id(name: str, *, occupied: set[str] | None = None) -> str:
    """Build the conservative fallback identity for an accepted BCE class."""

    return _opaque_id("class", [name], occupied or set())


def stable_class_field_ref(
    class_stable_id: str, position: int, *, occupied: set[str] | None = None,
) -> str:
    """Build an opaque class-field identity from parent and ordinal."""

    return _opaque_id("classfield", [class_stable_id, position], occupied or set())


def stable_data_type_field_ref(
    data_type_stable_id: str, position: int, *, occupied: set[str] | None = None,
) -> str:
    """Build an opaque DataType-field identity from parent and ordinal."""

    return _opaque_id("field", [data_type_stable_id, position], occupied or set())


def stable_call_id(
    collaboration_id: str,
    receiver_stable_id: str,
    step_refs: Iterable[str] = (),
    parent_stable_id: str | None = None,
    *,
    occurrence: int | None = None,
    occupied: set[str] | None = None,
) -> str:
    """Build the deterministic fallback identity for a collaboration call."""

    seed = [
        collaboration_id,
        receiver_stable_id,
        list(step_refs),
        parent_stable_id or "",
        occurrence,
    ]
    return _opaque_id("call", seed, occupied or set())


def _allocate_call_ids(
    records: list[dict[str, Any]],
    operation_stable: Mapping[str, str],
    assigned: Mapping[int, str] | None = None,
    *,
    occupied: set[str] | None = None,
) -> dict[int, str]:
    """Complete call identities with the one canonical fallback algorithm.

    Accepted legacy snapshots and newly generated snapshots must derive the
    same IDs.  Parent identities are resolved first, and duplicate occurrences
    are counted within their exact structural key rather than by global call
    position.
    """

    result = dict(assigned or {})
    used = set(occupied or ())
    used.update(result.values())
    by_legacy = {
        _text(record.get("legacy")): index
        for index, record in enumerate(records)
        if _text(record.get("legacy"))
    }
    occurrence: dict[tuple[Any, ...], int] = {}
    pending = set(range(len(records)))

    while pending:
        progress = False
        for index in sorted(pending):
            record = records[index]
            parent_text = _text(record.get("parent"))
            parent_index = by_legacy.get(parent_text) if parent_text else None
            if parent_index is not None and parent_index not in result:
                continue
            parent_stable = result.get(parent_index, "") if parent_index is not None else ""
            key = (
                _text(record.get("collaboration")),
                operation_stable.get(
                    _text(record.get("receiver")), _text(record.get("receiver"))
                ),
                tuple(record.get("steps") or ()),
                parent_stable,
            )
            occurrence[key] = occurrence.get(key, 0) + 1
            if index not in result:
                result[index] = stable_call_id(
                    key[0],
                    key[1],
                    key[2],
                    parent_stable,
                    occurrence=occurrence[key],
                    occupied=used,
                )
            used.add(result[index])
            pending.remove(index)
            progress = True
        if not progress:
            raise ValueError("Call parent references must form an acyclic graph.")

    return result


def _as_payload(value: BCEModel | Mapping[str, Any] | None) -> BCEModel | None:
    if value is None:
        return None
    return value if isinstance(value, BCEModel) else BCEModel.model_validate(value)


def _records(model: BCEModel | None, collection: str) -> list[dict[str, Any]]:
    if model is None:
        return []
    result: list[dict[str, Any]] = []
    if collection == "operations":
        for owner in model.Classes:
            for operation in owner.operations:
                result.append({
                    "item": operation,
                    "legacy": operation.operation_id,
                    "stable": operation.stable_id,
                    "owner": owner.class_name,
                    "steps": tuple(operation.step_refs),
                })
    else:
        for collaboration in model.Collaborations:
            for call in collaboration.calls:
                result.append({
                    "item": call,
                    "legacy": call.call_id,
                    "stable": call.stable_id,
                    "collaboration": collaboration.collaboration_id,
                    "receiver": call.receiver_operation_id,
                    "steps": tuple(call.step_refs),
                    "parent": call.parent_call_id,
                })
    return result


def _binding_identity_refs(model: BCEModel) -> dict[tuple[Any, ...], str]:
    """Capture opaque identities which can occur inside binding source refs."""

    refs: dict[tuple[Any, ...], str] = {}
    for collaboration in model.Collaborations:
        for call in collaboration.calls:
            refs[("call", collaboration.collaboration_id, call.call_id)] = _text(call.stable_id)
    for owner in model.Classes:
        for operation in owner.operations:
            for index, parameter in enumerate(operation.parameters):
                refs[("parameter", owner.class_name, operation.operation_id, index)] = _text(
                    parameter.stable_ref
                )
        for index, field_ref in enumerate(owner.field_refs):
            refs[("classField", owner.class_name, index)] = _text(field_ref)
    for data_type in model.DataTypes:
        for index, field_ref in enumerate(data_type.field_refs):
            refs[("dataTypeField", data_type.name, index)] = _text(field_ref)
    return refs


def _rebase_source_ref(source_ref: str, replacements: Mapping[str, str]) -> str:
    """Rebase only opaque ID tokens in the accepted binding-source grammar."""

    if source_ref.startswith(("value#", "runtime#")):
        return source_ref
    if source_ref.startswith("derived#"):
        match = re.fullmatch(r"(derived#[A-Za-z_][A-Za-z0-9_]*\()(.*)(\))", source_ref)
        if not match:
            return source_ref
        values = []
        for assignment in match.group(2).split(","):
            name, separator, value = assignment.partition("=")
            if not separator:
                return source_ref
            values.append(f"{name}={_rebase_source_ref(value, replacements)}")
        return f"{match.group(1)}{','.join(values)}{match.group(3)}"
    source, separator, path = source_ref.partition("#")
    if not separator:
        return source_ref
    return "#".join((
        replacements.get(source, source),
        ".".join(replacements.get(token, token) for token in path.split(".")),
    ))


def _rebase_binding_source_refs(
    model: BCEModel, before: Mapping[tuple[Any, ...], str],
) -> None:
    after = _binding_identity_refs(model)
    replacements = {
        previous: current
        for key, previous in before.items()
        if previous and (current := after.get(key)) and previous != current
    }
    if not replacements:
        return
    for collaboration in model.Collaborations:
        for call in collaboration.calls:
            for binding in call.argument_bindings:
                binding.source_ref = _rebase_source_ref(binding.source_ref, replacements)


def _target_pairs(targeted_refs: Any) -> list[tuple[str, str]]:
    """Read structured target mappings without interpreting natural language."""

    if not isinstance(targeted_refs, Mapping):
        return []
    pairs: list[tuple[str, str]] = []
    for key, value in targeted_refs.items():
        if key in {"operations", "operation", "calls", "call"} and isinstance(value, Mapping):
            pairs.extend(((_text(left), _text(right))) for left, right in value.items())
            continue
        if isinstance(value, str):
            pairs.append((_text(key), _text(value)))
        elif isinstance(value, Mapping):
            old = _text(value.get("previous") or value.get("from") or value.get("old"))
            new = _text(value.get("revised") or value.get("to") or value.get("new"))
            if old and new:
                pairs.append((old, new))
    return [(left, right) for left, right in pairs if left and right]


def _target_tokens(targeted_refs: Any) -> set[str]:
    if isinstance(targeted_refs, Mapping):
        return set()
    if isinstance(targeted_refs, str):
        return {_text(targeted_refs)}
    if isinstance(targeted_refs, Iterable):
        return {_text(item) for item in targeted_refs if _text(item)}
    return set()


def _matches(record: Mapping[str, Any], token: str) -> bool:
    """Match exact structured identifiers only (never prose/keyword matching)."""

    candidates = {
        _text(record.get("legacy")), _text(record.get("stable")),
        *_text(record.get("steps")).split("\x00"),
    }
    steps = record.get("steps") or ()
    candidates.update(_text(item) for item in steps)
    return bool(_token_variants(token) & candidates)


def _explicit_operation_match(
    previous: list[dict[str, Any]], revised: list[dict[str, Any]], targeted_refs: Any,
) -> dict[int, int]:
    pairs = _target_pairs(targeted_refs)
    if not pairs:
        return {}
    result: dict[int, int] = {}
    for old_token, new_token in pairs:
        old_candidates = [
            index for index, item in enumerate(previous)
            if _token_variants(old_token)
            & {_text(item.get("legacy")), _text(item.get("stable"))}
        ]
        new_candidates = [
            index for index, item in enumerate(revised)
            if _token_variants(new_token)
            & {_text(item.get("legacy")), _text(item.get("stable"))}
        ]
        if len(old_candidates) == 1 and len(new_candidates) == 1:
            result[new_candidates[0]] = old_candidates[0]
    return result


def _reconcile_operations(
    previous: BCEModel | None,
    revised: BCEModel,
    targeted_refs: Any,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    old = _records(previous, "operations")
    new = _records(revised, "operations")
    occupied: set[str] = set()
    old_stable: dict[int, str] = {}
    for index, record in enumerate(old):
        stable = _text(record.get("stable")) or stable_operation_id(
            _text(record.get("owner")), _text(record.get("legacy")), occupied=occupied,
        )
        old_stable[index] = stable
        occupied.add(stable)
    assigned: dict[int, str] = {}
    used_old: set[int] = set()

    explicit = _explicit_operation_match(old, new, targeted_refs)
    for new_index, old_index in explicit.items():
        if old_index not in used_old:
            assigned[new_index] = old_stable[old_index]
            used_old.add(old_index)

    # A legacy operation signature is exact and therefore safe to preserve.
    for index, record in enumerate(new):
        if index in assigned:
            continue
        candidates = [
            old_index for old_index, prior in enumerate(old)
            if old_index not in used_old
            and _text(prior.get("legacy")) == _text(record.get("legacy"))
        ]
        if len(candidates) == 1:
            old_index = candidates[0]
            assigned[index] = old_stable[old_index]
            used_old.add(old_index)

    # A precise target can preserve a renamed operation when exact matching has
    # left one old and one new operation in the same owning class.  This is a
    # bounded merge-unit match, not name or prose similarity.
    tokens = _target_tokens(targeted_refs)
    if tokens:
        targeted_old = {
            old_index
            for token in tokens
            for old_index, prior in enumerate(old)
            if old_index not in used_old and _matches(prior, token)
        }
        owners = {_text(old[index].get("owner")) for index in targeted_old}
        for owner in owners:
            old_candidates = [
                index
                for index in targeted_old
                if _text(old[index].get("owner")) == owner
            ]
            new_candidates = [
                index
                for index, current in enumerate(new)
                if index not in assigned and _text(current.get("owner")) == owner
            ]
            if len(old_candidates) == len(new_candidates) == 1:
                old_index, new_index = old_candidates[0], new_candidates[0]
                assigned[new_index] = old_stable[old_index]
                used_old.add(old_index)

    # Step refs are structural provenance. Only a unique old/new pair is
    # eligible; a duplicate provenance is deliberately left unmatched.
    old_by_provenance: dict[tuple[str, ...], list[int]] = {}
    new_by_provenance: dict[tuple[str, ...], list[int]] = {}
    for index, record in enumerate(old):
        if index not in used_old and record["steps"]:
            old_by_provenance.setdefault(record["steps"], []).append(index)
    for index, record in enumerate(new):
        if index not in assigned and record["steps"]:
            new_by_provenance.setdefault(record["steps"], []).append(index)
    for provenance, old_indices in old_by_provenance.items():
        new_indices = new_by_provenance.get(provenance, [])
        if len(old_indices) == len(new_indices) == 1:
            old_index, new_index = old_indices[0], new_indices[0]
            assigned[new_index] = old_stable[old_index]
            used_old.add(old_index)

    remap: dict[str, Any] = {"operations": {}, "newOperations": [], "ambiguousOperations": []}
    for index, record in enumerate(new):
        assigned_stable = assigned.get(index)
        if assigned_stable is None:
            assigned_stable = stable_operation_id(
                _text(record.get("owner")),
                _text(record.get("legacy")),
                occupied=occupied,
            )
            remap["newOperations"].append(_text(record.get("legacy")))
        occupied.add(assigned_stable)
        record["item"].stable_id = assigned_stable
        if _text(record.get("legacy")):
            remap["operations"][_text(record.get("legacy"))] = assigned_stable
    return new, remap


def _reconcile_parameter_refs(
    previous: BCEModel | None, operation_records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Issue parameter refs using only a reconciled operation parent and ordinal.

    A single edited slot is an unambiguous display rename; multiple changed
    slots (including reorder/insertion) are intentionally reissued so refs
    cannot silently follow the wrong declaration.
    """

    old_by_stable = {
        _text(operation.stable_id): operation
        for owner in (previous.Classes if previous else [])
        for operation in owner.operations
        if _text(operation.stable_id)
    }
    occupied = {
        _text(parameter.stable_ref)
        for owner in (previous.Classes if previous else [])
        for operation in owner.operations
        for parameter in operation.parameters
        if _text(parameter.stable_ref)
    }
    remap: dict[str, Any] = {"parameters": {}, "newParameters": [], "ambiguousParameters": []}
    for record in operation_records:
        operation = record["item"]
        parent = _text(operation.stable_id)
        prior = old_by_stable.get(parent)
        old_parameters = list(prior.parameters) if prior else []
        changed = [
            index for index, parameter in enumerate(operation.parameters)
            if index >= len(old_parameters)
            or (parameter.name, parameter.type) != (
                old_parameters[index].name, old_parameters[index].type
            )
        ]
        safe_positions = set(range(len(operation.parameters))) - set(changed)
        if len(changed) == 1 and len(old_parameters) == len(operation.parameters):
            safe_positions.add(changed[0])
        elif len(changed) > 1:
            remap["ambiguousParameters"].extend(
                f"{operation.operation_id}#{index + 1}" for index in changed
            )
        for index, parameter in enumerate(operation.parameters):
            prior_ref = (
                _text(old_parameters[index].stable_ref)
                if index < len(old_parameters) and index in safe_positions else ""
            )
            parameter.stable_ref = prior_ref or stable_parameter_ref(
                parent, index + 1, occupied=occupied,
            )
            occupied.add(parameter.stable_ref)
            key = f"{operation.operation_id}#{index + 1}"
            remap["parameters"][key] = parameter.stable_ref
            if not prior_ref:
                remap["newParameters"].append(key)
    return remap


def _reconcile_data_type_field_refs(
    previous: BCEModel | None, revised: BCEModel,
) -> dict[str, Any]:
    """Issue DataType and aligned field refs without matching by field names."""

    old_by_name = {item.name: item for item in (previous.DataTypes if previous else [])}
    occupied_types = {
        _text(item.stable_id) for item in (previous.DataTypes if previous else []) if _text(item.stable_id)
    }
    occupied_fields = {
        ref for item in (previous.DataTypes if previous else []) for ref in item.field_refs if _text(ref)
    }
    remap: dict[str, Any] = {"dataTypes": {}, "fields": {}, "newFields": [], "ambiguousFields": []}
    for item in revised.DataTypes:
        prior = old_by_name.get(item.name)
        item.stable_id = _text(prior.stable_id) if prior else ""
        item.stable_id = item.stable_id or stable_data_type_id(item.name, occupied=occupied_types)
        occupied_types.add(item.stable_id)
        remap["dataTypes"][item.name] = item.stable_id
        old_fields = list(prior.fields) if prior else []
        old_refs = list(prior.field_refs) if prior else []
        changed = [
            index for index, declaration in enumerate(item.fields)
            if index >= len(old_fields) or declaration != old_fields[index]
        ]
        safe_positions = set(range(len(item.fields))) - set(changed)
        if len(changed) == 1 and len(old_fields) == len(item.fields):
            safe_positions.add(changed[0])
        elif len(changed) > 1:
            remap["ambiguousFields"].extend(f"{item.name}#{index + 1}" for index in changed)
        refs: list[str] = []
        for index, _declaration in enumerate(item.fields):
            prior_ref = old_refs[index] if index < len(old_refs) and index in safe_positions else ""
            field_ref = prior_ref or stable_data_type_field_ref(
                item.stable_id, index + 1, occupied=occupied_fields,
            )
            occupied_fields.add(field_ref)
            refs.append(field_ref)
            key = f"{item.name}#{index + 1}"
            remap["fields"][key] = field_ref
            if not prior_ref:
                remap["newFields"].append(key)
        item.field_refs = refs
    return remap


def _reconcile_class_field_refs(
    previous: BCEModel | None, revised: BCEModel,
) -> dict[str, Any]:
    """Issue Class and aligned field refs without guessing renamed parents."""

    old_by_name = {item.class_name: item for item in (previous.Classes if previous else [])}
    occupied_classes = {
        _text(item.stable_id) for item in (previous.Classes if previous else []) if _text(item.stable_id)
    }
    occupied_fields = {
        ref for item in (previous.Classes if previous else []) for ref in item.field_refs if _text(ref)
    }
    remap: dict[str, Any] = {"classes": {}, "classFields": {}, "newClassFields": [], "ambiguousClassFields": []}
    for item in revised.Classes:
        # A class display rename has no structural provenance at this boundary;
        # preserve only an exact accepted parent name.
        prior = old_by_name.get(item.class_name)
        item.stable_id = _text(prior.stable_id) if prior else ""
        item.stable_id = item.stable_id or stable_class_id(item.class_name, occupied=occupied_classes)
        occupied_classes.add(item.stable_id)
        remap["classes"][item.class_name] = item.stable_id
        old_fields = list(prior.fields) if prior else []
        old_refs = list(prior.field_refs) if prior else []
        changed = [
            index for index, declaration in enumerate(item.fields)
            if index >= len(old_fields) or declaration != old_fields[index]
        ]
        safe_positions = set(range(len(item.fields))) - set(changed)
        if len(changed) == 1 and len(old_fields) == len(item.fields):
            safe_positions.add(changed[0])
        elif len(changed) > 1:
            remap["ambiguousClassFields"].extend(
                f"{item.class_name}#{index + 1}" for index in changed
            )
        refs: list[str] = []
        for index, _declaration in enumerate(item.fields):
            prior_ref = old_refs[index] if index < len(old_refs) and index in safe_positions else ""
            field_ref = prior_ref or stable_class_field_ref(
                item.stable_id, index + 1, occupied=occupied_fields,
            )
            occupied_fields.add(field_ref)
            refs.append(field_ref)
            key = f"{item.class_name}#{index + 1}"
            remap["classFields"][key] = field_ref
            if not prior_ref:
                remap["newClassFields"].append(key)
        item.field_refs = refs
    return remap


def _reconcile_calls(
    previous: BCEModel | None,
    revised: BCEModel,
    operation_records: list[dict[str, Any]],
    targeted_refs: Any,
) -> dict[str, Any]:
    old = _records(previous, "calls")
    new = _records(revised, "calls")
    operation_stable = {
        _text(record.get("legacy")): _text(record["item"].stable_id)
        for record in operation_records
    }
    old_operation_records = _records(previous, "operations")
    old_operation_stable = {
        _text(record.get("legacy")): (
            _text(record.get("stable"))
            or stable_operation_id(_text(record.get("owner")), _text(record.get("legacy")))
        )
        for record in old_operation_records
    }
    old_stable = {
        index: _text(record.get("stable"))
        for index, record in enumerate(old)
        if _text(record.get("stable"))
    }
    occupied = set(old_stable.values())
    old_stable = _allocate_call_ids(
        old,
        old_operation_stable,
        old_stable,
        occupied=occupied,
    )
    occupied.update(old_stable.values())

    previous_by_legacy = {_text(item.get("legacy")): index for index, item in enumerate(old)}
    revised_by_legacy = {_text(item.get("legacy")): index for index, item in enumerate(new)}
    assigned: dict[int, str] = {}
    used_old: set[int] = set()

    # A stable ID that occurs in both accepted snapshots is an exact identity,
    # even when two calls have identical structural provenance. Unknown IDs in
    # the revised payload are ignored and replaced below.
    old_by_stable: dict[str, list[int]] = {}
    for index, stable in old_stable.items():
        old_by_stable.setdefault(stable, []).append(index)
    for index, record in enumerate(new):
        proposed = _text(record.get("stable"))
        candidates = old_by_stable.get(proposed, [])
        if proposed and len(candidates) == 1:
            old_index = candidates[0]
            assigned[index] = old_stable[old_index]
            used_old.add(old_index)

    # Calls are matched by the canonical operation's stable identity, step
    # provenance, and the stable identity of their parent. This survives a
    # position-based call insertion without trusting the renumbered callId or
    # guessing among duplicate calls.
    old_by_key: dict[tuple[Any, ...], list[int]] = {}
    for index, record in enumerate(old):
        if index in used_old:
            continue
        parent = previous_by_legacy.get(_text(record.get("parent")))
        parent_stable = old_stable.get(parent, "") if parent is not None else ""
        key = (
            _text(record.get("collaboration")),
            old_operation_stable.get(_text(record.get("receiver")), _text(record.get("receiver"))),
            tuple(record.get("steps") or ()), parent_stable,
        )
        old_by_key.setdefault(key, []).append(index)
    pending = set(range(len(new))) - set(assigned)

    def match_structural_calls() -> None:
        while pending:
            eligible: dict[tuple[Any, ...], list[int]] = {}
            for index in sorted(pending):
                record = new[index]
                parent_text = _text(record.get("parent"))
                parent_index = revised_by_legacy.get(parent_text) if parent_text else None
                if parent_index is not None and parent_index not in assigned:
                    continue
                parent_stable = (
                    assigned.get(parent_index, "") if parent_index is not None else ""
                )
                key = (
                    _text(record.get("collaboration")),
                    operation_stable.get(
                        _text(record.get("receiver")), _text(record.get("receiver"))
                    ),
                    tuple(record.get("steps") or ()),
                    parent_stable,
                )
                eligible.setdefault(key, []).append(index)
            progress = False
            for key, new_indices in eligible.items():
                old_indices = [
                    candidate
                    for candidate in old_by_key.get(key, [])
                    if candidate not in used_old
                ]
                if len(old_indices) == len(new_indices) == 1:
                    old_index, new_index = old_indices[0], new_indices[0]
                    assigned[new_index] = old_stable[old_index]
                    used_old.add(old_index)
                    pending.remove(new_index)
                    progress = True
            if not progress:
                break

    match_structural_calls()

    # A precisely selected call may change its step provenance or receiver.
    # Preserve it only when exact matching leaves one selected old call and
    # one revised call in the same collaboration merge unit.
    tokens = _target_tokens(targeted_refs)
    if tokens:
        targeted_old = {
            old_index
            for token in tokens
            for old_index, prior in enumerate(old)
            if old_index not in used_old and _matches(prior, token)
        }
        collaborations = {
            _text(old[index].get("collaboration")) for index in targeted_old
        }
        for collaboration in collaborations:
            old_candidates = [
                index
                for index in targeted_old
                if _text(old[index].get("collaboration")) == collaboration
            ]
            if len(old_candidates) != 1:
                continue
            old_index = old_candidates[0]
            old_parent_text = _text(old[old_index].get("parent"))
            old_parent_index = (
                previous_by_legacy.get(old_parent_text) if old_parent_text else None
            )
            old_parent_stable = (
                old_stable.get(old_parent_index, "")
                if old_parent_index is not None
                else ""
            )
            new_candidates: list[int] = []
            for index, current in enumerate(new):
                if index in assigned or _text(current.get("collaboration")) != collaboration:
                    continue
                new_parent_text = _text(current.get("parent"))
                new_parent_index = (
                    revised_by_legacy.get(new_parent_text) if new_parent_text else None
                )
                if old_parent_index is None:
                    same_parent_scope = new_parent_index is None
                else:
                    same_parent_scope = (
                        new_parent_index is not None
                        and assigned.get(new_parent_index) == old_parent_stable
                    )
                if same_parent_scope:
                    new_candidates.append(index)
            if len(new_candidates) == 1:
                new_index = new_candidates[0]
                assigned[new_index] = old_stable[old_index]
                used_old.add(old_index)
                pending.discard(new_index)

    # A targeted parent can unlock exact structural matches for its children.
    match_structural_calls()

    remap: dict[str, Any] = {"calls": {}, "newCalls": [], "ambiguousCalls": []}
    new_indices = set(range(len(new))) - set(assigned)
    assigned = _allocate_call_ids(
        new,
        operation_stable,
        assigned,
        occupied=occupied,
    )
    for index, record in enumerate(new):
        stable = assigned[index]
        if index in new_indices:
            remap["newCalls"].append(_text(record.get("legacy")))
        occupied.add(stable)
        record["item"].stable_id = stable
        if _text(record.get("legacy")):
            remap["calls"][_text(record.get("legacy"))] = stable
    return remap


def reconcile_stable_ids(
    previous: BCEModel | Mapping[str, Any] | None,
    revised: BCEModel | Mapping[str, Any],
    *,
    targeted_refs: Any = None,
) -> tuple[BCEModel, dict[str, Any]]:
    """Assign and reconcile stable IDs at an accepted-artifact boundary.

    The returned metadata is intentionally JSON-compatible and records legacy
    IDs to stable IDs plus newly allocated and ambiguous elements. Ambiguous
    structural matches receive a new ID; no name/prose similarity is used.
    """

    prior = _as_payload(previous)
    model = _as_payload(revised)
    if model is None:  # pragma: no cover - the type contract rules this out
        raise ValueError("revised class model is required")
    binding_refs_before = _binding_identity_refs(model)
    operation_records, operation_remap = _reconcile_operations(prior, model, targeted_refs)
    parameter_remap = _reconcile_parameter_refs(prior, operation_records)
    class_remap = _reconcile_class_field_refs(prior, model)
    data_type_remap = _reconcile_data_type_field_refs(prior, model)
    call_remap = _reconcile_calls(prior, model, operation_records, targeted_refs)
    _rebase_binding_source_refs(model, binding_refs_before)
    operation_ids = [
        operation.stable_id
        for item in model.Classes
        for operation in item.operations
    ]
    call_ids = [
        call.stable_id
        for collaboration in model.Collaborations
        for call in collaboration.calls
    ]
    if len(operation_ids) != len(set(operation_ids)):
        raise ValueError("operation stable IDs must be unique")
    if len(call_ids) != len(set(call_ids)):
        raise ValueError("call stable IDs must be unique")
    return model, {**operation_remap, **parameter_remap, **class_remap, **data_type_remap, **call_remap}


def materialize_pre_collaboration_refs(
    previous: BCEModel | Mapping[str, Any] | None,
    skeleton: BCEModel | Mapping[str, Any],
    *,
    targeted_refs: Any = None,
) -> BCEModel:
    """Issue operation-side identities before collaboration binding begins.

    Calls intentionally remain outside this seam: their identities depend on a
    materialized collaboration.  Keeping the skeleton collaboration-free also
    makes this safe to use for a targeted operation revision with the previous
    accepted model as its only reconciliation source.
    """

    payload = _as_payload(skeleton)
    if payload is None:  # pragma: no cover - the type contract rules this out
        raise ValueError("class skeleton is required")
    bare = BCEModel.model_validate({
        **payload.model_dump(by_alias=True),
        "Collaborations": [],
    })
    model, _metadata = reconcile_stable_ids(
        previous, bare, targeted_refs=targeted_refs,
    )
    return model


def materialize_pre_binding_call_refs(
    model: BCEModel,
    collaboration_id: str,
    calls: list[dict[str, Any]],
) -> None:
    """Issue call identities once a call forest exists, before binding work.

    The collaboration materializer owns the transient call dictionaries, while
    this module owns their identity policy.  In particular, keep canonical
    ``callId`` as the parent/source-reference wire identity; ``stableId`` is
    added alongside it and is not used to change binding eligibility.
    """

    operation_stable = {
        operation.operation_id: (
            _text(operation.stable_id)
            or stable_operation_id(owner.class_name, operation.operation_id)
        )
        for owner in model.Classes
        for operation in owner.operations
    }
    records = [
        {
            "legacy": _text(call.get("callId")),
            "collaboration": collaboration_id,
            "receiver": _text(call.get("receiverOperationId")),
            "steps": tuple(_text(ref) for ref in call.get("stepRefs") or ()),
            "parent": _text(call.get("parentCallId")),
        }
        for call in calls
    ]
    assigned = _allocate_call_ids(records, operation_stable)
    for index, call in enumerate(calls):
        call["stableId"] = assigned[index]


__all__ = [
    "materialize_pre_binding_call_refs",
    "materialize_pre_collaboration_refs",
    "reconcile_stable_ids",
    "stable_call_id",
    "stable_class_field_ref",
    "stable_class_id",
    "stable_data_type_field_ref",
    "stable_data_type_id",
    "stable_operation_id",
    "stable_parameter_ref",
]
