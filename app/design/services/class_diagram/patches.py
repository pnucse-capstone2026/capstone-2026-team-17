"""Deterministic, minimal patches for persisted class-diagram artifacts.

The feedback planner may decide *what* needs changing, but it must not require
the class-diagram generator to recreate a whole collaboration.  This module
applies the resulting small patch vocabulary directly to an artifact.  It is
intentionally independent of the workspace and database layers.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, MutableMapping
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from app.design.schemas.class_model import canonical_call_id, canonical_operation_id
from app.design.services.class_diagram.type_system import types_equivalent


class DesignPatchError(ValueError):
    """A structured patch cannot be safely applied to the supplied artifact."""


def _text(value: object) -> str:
    return str(value or "").strip()


def _target(value: object) -> str:
    """Accept a raw target ID or the public ``class_diagram:`` authority form."""

    target = _text(value)
    prefix = "class_diagram:"
    return target.removeprefix(prefix)


def _patch_kind(patch: Mapping[str, Any]) -> str:
    return _text(patch.get("operation") or patch.get("kind") or patch.get("type"))


def _occurrence(value: object, *, field_name: str) -> int:
    """Use first occurrence by default, including an explicit JSON null."""

    if value is None:
        return 1
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise DesignPatchError(f"{field_name} must be a positive integer or null")
    return value


def _class_target(patch: Mapping[str, Any]) -> str:
    target = patch.get("className") or patch.get("target")
    result = _target(target)
    if not result:
        raise DesignPatchError("add_operation requires className or target")
    return result


def _collaboration_target(patch: Mapping[str, Any]) -> str:
    target = patch.get("collaborationId") or patch.get("target")
    result = _target(target)
    if not result:
        raise DesignPatchError("call insertion requires collaborationId or target")
    return result


def _mapping_list(value: object, *, field_name: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise DesignPatchError(f"{field_name} must be a list of objects")
    return [deepcopy(dict(item)) for item in value]


def _operation_from_patch(patch: Mapping[str, Any], owner: str) -> dict[str, Any]:
    supplied = patch.get("newOperation") or patch.get("definition") or patch
    if not isinstance(supplied, Mapping):
        raise DesignPatchError("add_operation definition must be an object")
    name = _text(supplied.get("name"))
    if not name:
        raise DesignPatchError("add_operation requires name")
    parameters = _mapping_list(supplied.get("parameters"), field_name="parameters")
    for parameter in parameters:
        if not _text(parameter.get("name")) or not _text(parameter.get("type")):
            raise DesignPatchError("each operation parameter requires name and type")
    return_type = _text(supplied.get("returnType") or "void")
    operation = {
        "operationId": canonical_operation_id(owner, name, parameters),
        "name": name,
        "parameters": parameters,
        "returnType": return_type,
        "stepRefs": list(supplied.get("stepRefs") or []),
    }
    stable_id = supplied.get("stableId")
    if stable_id is not None:
        operation["stableId"] = _text(stable_id)
    return operation


def _operation_catalog(model: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    catalog: dict[str, dict[str, Any]] = {}
    for class_item in model.get("Classes") or []:
        if not isinstance(class_item, Mapping):
            continue
        owner = _text(class_item.get("className"))
        for operation in class_item.get("operations") or []:
            if not isinstance(operation, Mapping):
                continue
            operation_id = _text(operation.get("operationId"))
            if not operation_id:
                operation_id = canonical_operation_id(
                    owner,
                    _text(operation.get("name")),
                    operation.get("parameters") or [],
                )
            catalog[operation_id] = dict(operation)
    return catalog


def _signature_parameters(value: str) -> tuple[tuple[str, str], ...] | None:
    """Parse the parameter portion of a persisted ``receiverOperationId``."""

    if not value:
        return ()
    pieces: list[str] = []
    start = 0
    depth = 0
    for index, character in enumerate(value):
        if character in "<[":
            depth += 1
        elif character in ">]":
            depth -= 1
            if depth < 0:
                return None
        elif character == "," and depth == 0:
            pieces.append(value[start:index])
            start = index + 1
    if depth:
        return None
    pieces.append(value[start:])
    result: list[tuple[str, str]] = []
    for piece in pieces:
        name, separator, type_name = piece.partition(":")
        if not separator or not _text(name) or not _text(type_name):
            return None
        result.append((_text(name), _text(type_name)))
    return tuple(result)


def _receiver_signature(value: str) -> tuple[str, str, tuple[tuple[str, str], ...]] | None:
    owner, separator, method = value.partition("::")
    if not separator or not _text(owner) or not method.endswith(")"):
        return None
    name, open_paren, parameter_text = method.partition("(")
    if not open_paren or not _text(name):
        return None
    parameters = _signature_parameters(parameter_text[:-1])
    return (_text(owner), _text(name), parameters) if parameters is not None else None


def _receiver_ids_equivalent(left: str, right: str) -> bool:
    """Compare receiver IDs without rewriting their persisted spelling."""

    left_signature = _receiver_signature(left)
    right_signature = _receiver_signature(right)
    if left_signature is None or right_signature is None:
        return False
    left_owner, left_name, left_parameters = left_signature
    right_owner, right_name, right_parameters = right_signature
    if (left_owner, left_name) != (right_owner, right_name):
        return False
    if len(left_parameters) != len(right_parameters):
        return False
    for (left_parameter, left_type), (right_parameter, right_type) in zip(
        left_parameters, right_parameters, strict=True,
    ):
        if left_parameter != right_parameter:
            return False
        try:
            if not types_equivalent(left_type, right_type):
                return False
        except ValueError:
            return False
    return True


def _equivalent_receiver_matches(
    requested: str, candidates: Iterable[tuple[Any, str]],
) -> list[tuple[Any, str]]:
    """Use exact receiver IDs first; semantic aliases are a unique fallback."""

    candidate_list = list(candidates)
    exact = [item for item in candidate_list if item[1] == requested]
    if exact:
        return exact
    return [item for item in candidate_list if _receiver_ids_equivalent(item[1], requested)]


def _catalog_receiver(
    catalog: Mapping[str, dict[str, Any]], requested: str,
) -> tuple[str, dict[str, Any]]:
    matches = _equivalent_receiver_matches(
        requested,
        (((receiver_id, operation), receiver_id) for receiver_id, operation in catalog.items()),
    )
    if not matches:
        raise DesignPatchError(f"inserted receiver operation does not exist: {requested}")
    if len(matches) > 1:
        raise DesignPatchError(
            f"inserted receiver operation is ambiguous: {requested}"
        )
    receiver_id, operation = matches[0][0]
    return receiver_id, operation


def _rename_operation(model: MutableMapping[str, Any], patch: Mapping[str, Any]) -> None:
    """Rename one explicitly addressed operation and its collaboration uses."""

    operation_id = _target(patch.get("operationId") or patch.get("target"))
    if "::" not in operation_id:
        raise DesignPatchError("rename_operation requires an exact operationId target")
    new_name = _text(
        patch.get("newName")
        or patch.get("new_name")
        or patch.get("newOperationName")
    )
    if not new_name:
        raise DesignPatchError("rename_operation requires newName")
    matches: list[tuple[MutableMapping[str, Any], MutableMapping[str, Any]]] = []
    for class_item in model.get("Classes") or []:
        if not isinstance(class_item, MutableMapping):
            continue
        for operation in class_item.get("operations") or []:
            if isinstance(operation, MutableMapping) and _text(operation.get("operationId")) == operation_id:
                matches.append((class_item, operation))
    if len(matches) != 1:
        raise DesignPatchError(
            f"rename_operation target must identify exactly one operation: {operation_id}"
        )
    owner, operation = matches[0]
    owner_name = _text(owner.get("className"))
    siblings = owner.get("operations") or []
    if any(
        item is not operation and isinstance(item, Mapping) and _text(item.get("name")) == new_name
        for item in siblings
    ):
        raise DesignPatchError(f"operation name already exists on {owner_name}: {new_name}")
    replacement_id = canonical_operation_id(
        owner_name, new_name, operation.get("parameters") or []
    )
    operation["name"] = new_name
    operation["operationId"] = replacement_id
    for collaboration in model.get("Collaborations") or []:
        if not isinstance(collaboration, Mapping):
            continue
        for call in collaboration.get("calls") or []:
            if isinstance(call, MutableMapping) and _text(call.get("receiverOperationId")) == operation_id:
                call["receiverOperationId"] = replacement_id


@dataclass
class _CollaborationState:
    collaboration: MutableMapping[str, Any]
    parents: dict[int, dict[str, Any] | None]
    parent_key_present: dict[int, bool]
    aliases: dict[str, dict[str, Any]]
    original_calls: tuple[dict[str, Any], ...]
    after_offsets: dict[int, int] = field(default_factory=dict)
    before_offsets: dict[int, int] = field(default_factory=dict)

    @classmethod
    def create(cls, collaboration: MutableMapping[str, Any]) -> _CollaborationState:
        collaboration_id = _text(collaboration.get("collaborationId"))
        calls = collaboration.get("calls")
        if not isinstance(calls, list) or not all(isinstance(call, MutableMapping) for call in calls):
            raise DesignPatchError(f"{collaboration_id} has invalid calls")
        aliases: dict[str, dict[str, Any]] = {}
        for position, call in enumerate(calls, start=1):
            aliases[_text(call.get("callId"))] = call
            # Old artifacts may already be position-canonical even if their raw
            # callId has not been normalized yet.
            aliases[canonical_call_id(collaboration_id, position)] = call
        parents: dict[int, dict[str, Any] | None] = {}
        parent_key_present: dict[int, bool] = {}
        for call in calls:
            parent_key_present[id(call)] = "parentCallId" in call
            parent_id = _text(call.get("parentCallId"))
            parent = aliases.get(parent_id) if parent_id else None
            if parent_id and parent is None:
                raise DesignPatchError(
                    f"{collaboration_id} has an unresolved parentCallId: {parent_id}"
                )
            parents[id(call)] = parent
        return cls(
            collaboration=collaboration,
            parents=parents,
            parent_key_present=parent_key_present,
            aliases=aliases,
            original_calls=tuple(calls),
        )

    @property
    def calls(self) -> list[dict[str, Any]]:
        return self.collaboration["calls"]

    def anchor(self, receiver_operation_id: str, occurrence: int) -> tuple[int, dict[str, Any]]:
        matches = _equivalent_receiver_matches(
            receiver_operation_id,
            (
                ((index, call), _text(call.get("receiverOperationId")))
                for index, call in enumerate(self.calls)
                if _text(call.get("receiverOperationId"))
            ),
        )
        if occurrence < 1 or occurrence > len(matches):
            raise DesignPatchError(
                f"anchor {receiver_operation_id!r} occurrence {occurrence} does not exist in "
                f"{_text(self.collaboration.get('collaborationId'))}"
            )
        if len(matches) > 1 and occurrence == 1:
            raise DesignPatchError(
                f"anchor {receiver_operation_id!r} is ambiguous; specify anchorOccurrence"
            )
        index, call = matches[occurrence - 1][0]
        return index, call

    def insert(self, patch: Mapping[str, Any], *, after: bool, catalog: Mapping[str, dict[str, Any]]) -> None:
        anchor_id = _text(patch.get("anchor") or patch.get("anchorReceiverOperationId"))
        receiver = patch.get("receiverOperationId")
        if not receiver and isinstance(patch.get("call"), str):
            receiver = patch["call"]
        receiver_id = _text(receiver)
        if not anchor_id or not receiver_id:
            raise DesignPatchError("call insertion requires anchor and receiverOperationId")
        receiver_id, operation = _catalog_receiver(catalog, receiver_id)
        occurrence = _occurrence(patch.get("anchorOccurrence"), field_name="anchorOccurrence")
        index, anchor = self.anchor(anchor_id, occurrence)
        bindings = _mapping_list(patch.get("argumentBindings"), field_name="argumentBindings")
        expected = {
            _text(parameter.get("name"))
            for parameter in operation.get("parameters") or []
            if isinstance(parameter, Mapping)
        }
        actual = {
            _text(binding.get("parameter"))
            for binding in bindings
        }
        if actual != expected:
            raise DesignPatchError("argumentBindings must exactly match inserted receiver parameters")
        inserted: dict[str, Any] = {
            "receiverOperationId": receiver_id,
            "stepRefs": list(patch.get("stepRefs") or []),
            "argumentBindings": bindings,
        }
        stable_id = patch.get("stableId")
        if stable_id is not None:
            inserted["stableId"] = _text(stable_id)
        key = id(anchor)
        if after:
            offset = self.after_offsets.get(key, 0)
            self.calls.insert(index + 1 + offset, inserted)
            self.after_offsets[key] = offset + 1
            # A call positioned next to an existing call is normally another
            # message from the same sender, not a message invoked by its
            # preceding sibling.  Both before/after therefore retain the
            # anchor's parent topology by default.
            self.parents[id(inserted)] = self.parents[id(anchor)]
            self.parent_key_present[id(inserted)] = False
        else:
            offset = self.before_offsets.get(key, 0)
            self.calls.insert(index + offset, inserted)
            self.before_offsets[key] = offset + 1
            self.parents[id(inserted)] = self.parents[id(anchor)]
            self.parent_key_present[id(inserted)] = False

    def remove(self, patch: Mapping[str, Any]) -> None:
        receiver_id = _text(
            patch.get("receiverOperationId")
            or patch.get("receiver")
            or patch.get("call")
        )
        if not receiver_id:
            raise DesignPatchError("remove_call requires receiverOperationId")
        occurrence = _occurrence(
            patch.get("anchorOccurrence")
            if "anchorOccurrence" in patch
            else patch.get("occurrence"),
            field_name="anchorOccurrence",
        )
        index, target = self.anchor(receiver_id, occurrence)
        if any(parent is target for parent in self.parents.values()):
            raise DesignPatchError(
                "remove_call cannot remove a call that still has child calls"
            )
        for call in self.calls:
            for binding in call.get("argumentBindings") or []:
                if not isinstance(binding, Mapping):
                    continue
                source, separator, _suffix = _text(binding.get("sourceRef")).partition("#")
                if separator and self.aliases.get(source) is target:
                    raise DesignPatchError(
                        "remove_call cannot remove a call referenced by an argument binding"
                    )
        self.calls.pop(index)
        self.parents.pop(id(target), None)
        self.parent_key_present.pop(id(target), None)

    def assert_existing_order(self) -> None:
        """Ensure survivors from the pre-patch artifact remain a subsequence."""

        surviving = {id(call) for call in self.calls}
        expected = [id(call) for call in self.original_calls if id(call) in surviving]
        original_ids = {id(call) for call in self.original_calls}
        observed = [id(call) for call in self.calls if id(call) in original_ids]
        if observed != expected:
            raise DesignPatchError("preserve_existing_order constraint was violated")

    def finalize(self) -> None:
        collaboration_id = _text(self.collaboration.get("collaborationId"))
        if not collaboration_id:
            raise DesignPatchError("collaborationId is required")
        new_ids = {
            id(call): canonical_call_id(collaboration_id, position)
            for position, call in enumerate(self.calls, start=1)
        }
        aliases = dict(self.aliases)
        stable_call_ids = {
            _text(call.get("stableId")) for call in self.calls
            if _text(call.get("stableId"))
        }
        for call in self.calls:
            aliases[new_ids[id(call)]] = call
        for call in self.calls:
            call["callId"] = new_ids[id(call)]
            parent = self.parents.get(id(call))
            if parent is None:
                if self.parent_key_present.get(id(call), False):
                    call["parentCallId"] = None
                else:
                    call.pop("parentCallId", None)
            else:
                call["parentCallId"] = new_ids[id(parent)]
            for binding in call.get("argumentBindings") or []:
                if not isinstance(binding, MutableMapping):
                    continue
                source, separator, suffix = _text(binding.get("sourceRef")).partition("#")
                # Stable sourceRefs identify calls by stableId and must survive
                # callId re-indexing.  Only legacy callId-prefixed references
                # are rewritten below.
                if source in stable_call_ids:
                    continue
                source_call = aliases.get(source)
                if separator and source_call is not None:
                    binding["sourceRef"] = f"{new_ids[id(source_call)]}#{suffix}"


def apply_structured_patches(
    model: Mapping[str, Any], patches: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Apply a minimal class/collaboration patch list without regenerating calls.

    Supported operations are ``add_operation``, ``rename_operation``,
    ``remove_call``, ``insert_call_before``, ``insert_call_after``, and the
    no-op constraint ``preserve_existing_order``. Insertions preserve every
    existing call's receiver, relative order, parent relationship, and
    call-result bindings. The targeted collaboration keeps its original
    ``collaborationId``; only position-based ``callId`` values are re-indexed
    as required by the persisted contract.
    """

    result = deepcopy(dict(model))
    classes = result.get("Classes")
    collaborations = result.get("Collaborations")
    if not isinstance(classes, list) or not isinstance(collaborations, list):
        raise DesignPatchError("model requires Classes and Collaborations lists")
    class_index = {
        _text(item.get("className")): item
        for item in classes
        if isinstance(item, MutableMapping)
    }
    collaboration_index = {
        _text(item.get("collaborationId")): item
        for item in collaborations
        if isinstance(item, MutableMapping)
    }
    states: dict[str, _CollaborationState] = {}
    preserve_order: set[str] = set()

    for patch in patches:
        if not isinstance(patch, Mapping):
            raise DesignPatchError("each patch must be an object")
        kind = _patch_kind(patch)
        if kind == "add_operation":
            owner = _class_target(patch)
            class_item = class_index.get(owner)
            if class_item is None:
                raise DesignPatchError(f"target class does not exist: {owner}")
            operation = _operation_from_patch(patch, owner)
            operations = class_item.get("operations")
            if not isinstance(operations, list):
                raise DesignPatchError(f"{owner} has invalid operations")
            existing = next(
                (item for item in operations if isinstance(item, Mapping) and _text(item.get("name")) == operation["name"]),
                None,
            )
            if existing is not None:
                existing_id = _text(existing.get("operationId"))
                if existing_id != operation["operationId"] or _text(existing.get("returnType")) != operation["returnType"]:
                    raise DesignPatchError(f"operation already exists with a different signature: {owner}.{operation['name']}")
                continue
            operations.append(operation)
        elif kind == "rename_operation":
            _rename_operation(result, patch)
        elif kind == "remove_call":
            collaboration_id = _collaboration_target(patch)
            collaboration = collaboration_index.get(collaboration_id)
            if collaboration is None:
                raise DesignPatchError(f"target collaboration does not exist: {collaboration_id}")
            state = states.setdefault(collaboration_id, _CollaborationState.create(collaboration))
            state.remove(patch)
        elif kind in {"insert_call_before", "insert_call_after"}:
            collaboration_id = _collaboration_target(patch)
            collaboration = collaboration_index.get(collaboration_id)
            if collaboration is None:
                raise DesignPatchError(f"target collaboration does not exist: {collaboration_id}")
            state = states.setdefault(collaboration_id, _CollaborationState.create(collaboration))
            state.insert(
                patch,
                after=kind == "insert_call_after",
                catalog=_operation_catalog(result),
            )
        elif kind == "preserve_existing_order":
            target = _target(patch.get("collaborationId") or patch.get("target"))
            target_ids = [target] if target else list(collaboration_index)
            for collaboration_id in target_ids:
                collaboration = collaboration_index.get(collaboration_id)
                if collaboration is None:
                    raise DesignPatchError(
                        f"target collaboration does not exist: {collaboration_id}"
                    )
                states.setdefault(
                    collaboration_id, _CollaborationState.create(collaboration)
                )
                preserve_order.add(collaboration_id)
        else:
            raise DesignPatchError(f"unsupported structured patch operation: {kind or '<missing>'}")

    for collaboration_id in preserve_order:
        states[collaboration_id].assert_existing_order()
    for state in states.values():
        state.finalize()
    return result


__all__ = ["DesignPatchError", "apply_structured_patches"]
