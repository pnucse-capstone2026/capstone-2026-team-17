"""클래스 모델의 최초 생성·재개·사용자 피드백 수정을 조율한다."""
from __future__ import annotations

from collections.abc import Set as AbstractSet
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from app.config import settings
from app.design.schemas.class_model import BCEModel, Collaboration
from app.design.services.class_diagram import collaboration, generation, inventory, operations
from app.design.services.class_diagram import feedback as feedback_stage
from app.design.services.class_diagram.cache import AcceptedUnitCache
from app.design.services.class_diagram.identity import (
    materialize_pre_collaboration_refs,
    reconcile_stable_ids,
)
from app.design.services.class_diagram.models import AcceptedFragment
from app.design.services.class_diagram.proposals import CallPlanProposal, FeedbackScope
from app.design.services.class_diagram.scenario import ScenarioIndex, UseCase, id_key
from app.design.services.class_diagram.validation.collaboration import (
    COLLABORATION_CHECKS,
    CollaborationContext,
)
from app.design.services.class_diagram.validation.model import validate_class_model
from app.design.services.common.structured import bind_context
from app.validation import run_checks


def _payload(model: BCEModel) -> dict[str, Any]:
    return model.model_dump(by_alias=True)


def _accepted_model(
    previous: BCEModel | None,
    revised: BCEModel,
    *,
    targeted_refs: Any = None,
) -> BCEModel:
    """Cross the accepted-artifact boundary and persist app-managed IDs."""

    model, _metadata = reconcile_stable_ids(
        previous, revised, targeted_refs=targeted_refs,
    )
    return model


def _standalone(index: ScenarioIndex) -> list[UseCase]:
    return [
        use_case for use_case in index.use_cases
        if any(group.use_case_id == use_case.id for group in index.groups)
    ]


def _replace_use_cases(
    index: ScenarioIndex,
    model: BCEModel,
    use_cases: list[UseCase],
    *,
    feedback: str = "",
    cache: AcceptedUnitCache | None = None,
) -> tuple[
    dict[str, Collaboration],
    list[collaboration.CombinedReplacementRequired],
]:
    """유스케이스별 collaboration을 설정된 수만큼 병렬로 교체한다."""

    directive = f"Apply this feedback to the call plan only: {feedback}" if feedback else ""

    def run(
        use_case: UseCase,
    ) -> tuple[
        str,
        Collaboration | None,
        collaboration.CombinedReplacementRequired | None,
    ]:
        try:
            value = collaboration.process_use_case(
                index, model, use_case, directive, cache=cache,
            )
            return use_case.id, value, None
        except collaboration.CombinedReplacementRequired as signal:
            return use_case.id, None, signal

    workers = max(1, min(
        len(use_cases) or 1,
        int(getattr(settings, "design_class_behavior_parallelism", 4)),
    ))
    if workers == 1:
        results = [run(use_case) for use_case in use_cases]
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(bind_context(run), use_case) for use_case in use_cases]
            results = [future.result() for future in futures]
    return (
        {use_case_id: value for use_case_id, value, _signal in results if value},
        [signal for _use_case_id, _value, signal in results if signal],
    )


def _collaboration_valid(
    index: ScenarioIndex,
    model: BCEModel,
    use_case: UseCase,
    value: Collaboration,
) -> bool:
    report = run_checks(
        COLLABORATION_CHECKS,
        value.model_dump(by_alias=True),
        CollaborationContext(index, _payload(model), use_case),
    )
    return not report.errors and not report.findings


def _preserved_call_plan(
    previous_model: BCEModel,
    revised_model: BCEModel,
    value: Collaboration,
) -> CallPlanProposal | None:
    """Keep an accepted call topology while operation signatures are revised."""

    revised_ids = {
        operation.operation_id
        for item in revised_model.Classes
        for operation in item.operations
    }
    revised_by_identity: dict[tuple[str, str], list[str]] = {}
    revised_by_steps: dict[tuple[str, tuple[str, ...]], list[str]] = {}
    for item in revised_model.Classes:
        for operation in item.operations:
            revised_by_identity.setdefault(
                (item.class_name, operation.name), []
            ).append(operation.operation_id)
            revised_by_steps.setdefault(
                (item.class_name, tuple(sorted(operation.step_refs))), []
            ).append(operation.operation_id)

    replacements: dict[str, str] = {}
    for item in previous_model.Classes:
        for operation in item.operations:
            if operation.operation_id in revised_ids:
                replacements[operation.operation_id] = operation.operation_id
                continue
            candidates = revised_by_identity.get(
                (item.class_name, operation.name), []
            )
            if len(candidates) != 1:
                candidates = revised_by_steps.get(
                    (item.class_name, tuple(sorted(operation.step_refs))), []
                )
            replacements[operation.operation_id] = (
                candidates[0] if len(candidates) == 1 else ""
            )
    positions = {
        call.call_id: position
        for position, call in enumerate(value.calls, start=1)
    }
    calls = []
    for call in value.calls:
        receiver = replacements.get(call.receiver_operation_id, "")
        parent = positions.get(call.parent_call_id) if call.parent_call_id else None
        if not receiver or (call.parent_call_id and parent is None):
            return None
        calls.append(
            {
                "receiverOperationId": receiver,
                "parentCallIndex": parent,
            }
        )
    return CallPlanProposal.model_validate({"calls": calls})


def _rematerialize_preserved_collaborations(
    index: ScenarioIndex,
    previous_model: BCEModel,
    revised_model: BCEModel,
    existing: dict[str, Collaboration],
    selected: list[UseCase],
) -> tuple[dict[str, Collaboration], list[UseCase]]:
    """Rebind changed parameters without asking an LLM to redesign valid topology."""

    rebound = dict(existing)
    unresolved: list[UseCase] = []
    for use_case in selected:
        value = existing.get(use_case.id)
        plan = (
            _preserved_call_plan(previous_model, revised_model, value)
            if value is not None
            else None
        )
        if plan is None:
            unresolved.append(use_case)
            continue
        try:
            rebound[use_case.id] = collaboration.materialize(
                index,
                revised_model,
                use_case,
                plan,
            )
        except ValueError:
            unresolved.append(use_case)
    return rebound, unresolved


def _emit_preview(
    index: ScenarioIndex,
    skeleton: BCEModel,
    accepted: dict[str, Collaboration],
    use_case_id: str,
) -> None:
    standalone = _standalone(index)
    operations.emit_preview(
        {
            **_payload(skeleton),
            "Collaborations": [
                accepted[use_case.id].model_dump(by_alias=True)
                for use_case in standalone if use_case.id in accepted
            ],
        },
        "collaborations",
        use_case_id,
        len(accepted),
        len(standalone),
    )


def _complete_collaborations(
    index: ScenarioIndex,
    model: BCEModel,
    existing: dict[str, Collaboration],
    selected: list[UseCase],
    *,
    feedback: str = "",
    cache: AcceptedUnitCache | None = None,
) -> BCEModel:
    """국소 call-plan 수리부터 결합 유스케이스 교체까지 자동으로 이어 간다."""

    skeleton = materialize_pre_collaboration_refs(
        model,
        BCEModel.model_validate({**_payload(model), "Collaborations": []}),
    )
    standalone = _standalone(index)
    accepted = {
        use_case.id: value
        for use_case in standalone
        if (value := existing.get(use_case.id)) is not None
        and _collaboration_valid(index, skeleton, use_case, value)
    }
    pending_ids = {use_case.id for use_case in selected} | {
        use_case.id for use_case in standalone if use_case.id not in accepted
    }
    while pending_ids:
        pending = [use_case for use_case in standalone if use_case.id in pending_ids]
        replacements, signals = _replace_use_cases(
            index, skeleton, pending, feedback=feedback, cache=cache,
        )
        for use_case in pending:
            value = replacements.get(use_case.id)
            if value is None:
                continue
            accepted[use_case.id] = value
            _emit_preview(index, skeleton, accepted, use_case.id)
        if not signals:
            break

        # 여러 worker가 동시에 확대 신호를 보냈더라도 operation 교체는 한 유스케이스씩
        # 적용한다. 새 skeleton에서 나머지 collaboration을 다시 검사해야 하기 때문이다.
        signal = signals[0]
        use_case = index.use_case(signal.use_case_id)
        skeleton, value = generation.replace_use_case_unit(
            index, skeleton, use_case, signal,
        )
        accepted[use_case.id] = value
        accepted = {
            item.id: current
            for item in standalone
            if (current := accepted.get(item.id)) is not None
            and _collaboration_valid(index, skeleton, item, current)
        }
        _emit_preview(index, skeleton, accepted, use_case.id)
        pending_ids = {
            item.id for item in standalone if item.id not in accepted
        }
        feedback = ""

    return BCEModel.model_validate({
        **_payload(skeleton),
        "Collaborations": [
            accepted[use_case.id]
            for use_case in standalone if use_case.id in accepted
        ],
    })


def _replace_selected_collaborations(
    index: ScenarioIndex,
    model: BCEModel,
    current: BCEModel,
    selected: list[UseCase],
    *,
    feedback: str,
    cache: AcceptedUnitCache | None = None,
) -> BCEModel:
    """Replace only selected use-case collaborations and preserve legacy IDs."""

    replacements, signals = _replace_use_cases(
        index, model, selected, feedback=feedback, cache=cache,
    )
    selected_ids = {use_case.id for use_case in selected}
    originals_by_use_case: dict[str, list[Collaboration]] = {
        use_case.id: [
            item for item in current.Collaborations
            if use_case.id in item.use_case_ids
        ]
        for use_case in selected
    }
    if any(len(items) > 1 for items in originals_by_use_case.values()):
        raise ValueError(
            "The selected legacy use case has multiple collaboration roots and cannot be "
            "replaced as one bounded target."
        )

    selected_by_id = {use_case.id: use_case for use_case in selected}
    revised_skeleton = model
    signaled_ids: set[str] = set()
    for signal in signals:
        use_case = selected_by_id.get(signal.use_case_id)
        if use_case is None or signal.use_case_id in signaled_ids:
            raise ValueError(
                "The selected collaboration replacement returned an invalid use-case signal."
            )
        signaled_ids.add(signal.use_case_id)
        revised_skeleton, replacements[use_case.id] = generation.replace_use_case_unit(
            index, revised_skeleton, use_case, signal,
        )

    replacement_by_original_id: dict[str, Collaboration] = {}
    append_replacements: list[Collaboration] = []
    for use_case in selected:
        replacement = replacements.get(use_case.id)
        if replacement is None:
            raise ValueError(f"No revised collaboration was produced for {use_case.id}.")
        originals = originals_by_use_case[use_case.id]
        if originals:
            original_id = originals[0].collaboration_id
            payload = replacement.model_dump(by_alias=True)
            call_id_map = {
                str(call.get("callId") or ""): f"{original_id}::call:{position}"
                for position, call in enumerate(payload.get("calls") or [], start=1)
                if isinstance(call, dict) and call.get("callId")
            }
            for call in payload.get("calls") or []:
                if not isinstance(call, dict) or not call.get("parentCallId"):
                    continue
                call["parentCallId"] = call_id_map.get(
                    str(call["parentCallId"]), str(call["parentCallId"])
                )
            payload["collaborationId"] = original_id
            replacement_by_original_id[original_id] = Collaboration.model_validate(payload)
        else:
            append_replacements.append(replacement)

    preserved = [
        replacement_by_original_id.get(item.collaboration_id, item)
        for item in current.Collaborations
        if not selected_ids.intersection(item.use_case_ids)
        or item.collaboration_id in replacement_by_original_id
    ]
    return BCEModel.model_validate({
        **_payload(revised_skeleton),
        "Collaborations": [*preserved, *append_replacements],
    })


def _validated(model: BCEModel, index: ScenarioIndex, action: str) -> BCEModel:
    report = validate_class_model(model, index)
    if report.errors or report.findings:
        details = [
            *(f"{finding.location}: {finding.message}" for finding in report.findings),
            *report.errors,
        ]
        raise ValueError(f"{action} class model is incomplete or invalid: " + "; ".join(details))
    return model


def generate_class_model(
    index: ScenarioIndex,
    *,
    cache: AcceptedUnitCache | None = None,
    repair_guidance: str | None = None,
    binding_source_decision: dict[str, Any] | None = None,
) -> BCEModel:
    """inventory 한 번과 유스케이스별 결합 호출로 수락 BCE 모델을 생성한다."""

    if not index.use_cases:
        return BCEModel()
    accepted_inventory = inventory.inventory_proposal(index, cache=cache)
    operations.emit_preview(
        _payload(inventory.inventory_model(accepted_inventory)),
        "inventory", "inventory", 1, len(index.use_cases) + 1,
    )
    return _validated(
        _accepted_model(
            None,
            generation.build_model(
                index,
                accepted_inventory,
                cache=cache,
                repair_guidance=repair_guidance,
                binding_source_decision=binding_source_decision,
            ),
        ),
        index,
        "generated",
    )


def resume_class_model(
    index: ScenarioIndex,
    current: BCEModel,
    *,
    cache: AcceptedUnitCache | None = None,
) -> BCEModel:
    """없는 유스케이스 collaboration만 완성하고 기존 수락 결과는 보존한다."""

    existing = {item.collaboration_id: item for item in current.Collaborations}
    selected: list[UseCase] = []
    for use_case in _standalone(index):
        value = existing.get(use_case.id)
        if value is None:
            selected.append(use_case)
    if not selected:
        return _accepted_model(current, current)
    model = _complete_collaborations(
        index, current, existing, selected, cache=cache,
    )
    return _validated(_accepted_model(current, model), index, "resumed")


def revise_class_model(
    current: BCEModel,
    index: ScenarioIndex,
    feedback: str,
    targets: AbstractSet[str],
    *,
    cache: AcceptedUnitCache | None = None,
    operation_use_case_ids: AbstractSet[str] | None = None,
    collaboration_use_case_ids: AbstractSet[str] | None = None,
) -> BCEModel:
    """피드백이 지정한 inventory·operation·유스케이스 협업만 교체한다."""

    if not feedback.strip():
        return current
    if operation_use_case_ids is not None and collaboration_use_case_ids is not None:
        raise ValueError("A repair cannot select both operation and collaboration scopes.")
    if operation_use_case_ids is None and collaboration_use_case_ids is None:
        scope = feedback_stage.feedback_scope(index, current, feedback, targets)
    elif operation_use_case_ids is not None:
        selected = set(operation_use_case_ids)
        known = {use_case.id for use_case in index.use_cases}
        if not selected or not selected <= known:
            raise ValueError("Operation repair scope selected an unknown use case.")
        scope = FeedbackScope(kind="operation", ids=sorted(selected, key=id_key))
    else:
        selected = set(collaboration_use_case_ids or ())
        known = {use_case.id for use_case in index.use_cases}
        if not selected or not selected <= known:
            raise ValueError("Collaboration repair scope selected an unknown use case.")
        scope = FeedbackScope(kind="collaboration", ids=sorted(selected, key=id_key))
    accepted_inventory = feedback_stage.inventory_from_model(current)
    if scope.kind == "inventory":
        revised_inventory = feedback_stage.propose_inventory_revision(
            index, accepted_inventory, feedback, set(scope.ids), cache=cache,
        )
        def behavior_shape(
            value: feedback_stage.AcceptedInventory,
        ) -> tuple[dict[str, tuple[str, tuple[str, ...]]], tuple[str, ...]]:
            payload = value.as_payload()
            classes = {
                str(item.get("className") or ""): (
                    str(item.get("stereotype") or ""),
                    tuple(sorted(
                        str(use_case_id)
                        for use_case_id in item.get("useCaseIds") or []
                    )),
                )
                for item in payload.get("Classes") or []
            }
            data_types = tuple(sorted(
                str(item.get("name") or "") for item in payload.get("DataTypes") or []
            ))
            return classes, data_types

        if behavior_shape(accepted_inventory) != behavior_shape(revised_inventory):
            return _validated(
                _accepted_model(
                    current,
                    generation.build_model(index, revised_inventory, cache=cache),
                    targeted_refs=targets,
                ),
                index,
                "revised",
            )
        # Structural edits do not imply new behavior.  Reuse the accepted
        # operation fragments and call topology first; only collaborations
        # made invalid by the new inventory enter the existing repair path.
        fragments = feedback_stage.fragments_from_model(index, current)
        skeleton = materialize_pre_collaboration_refs(
            current, operations.compose_fragments(revised_inventory, fragments),
            targeted_refs=targets,
        )
        existing = {item.collaboration_id: item for item in current.Collaborations}
        existing, unresolved = _rematerialize_preserved_collaborations(
            index, current, skeleton, existing, _standalone(index),
        )
        revised = _complete_collaborations(
            index,
            skeleton,
            existing,
            unresolved,
            feedback=feedback,
            cache=cache,
        )
        return _validated(
            _accepted_model(
                current,
                revised,
                targeted_refs=targets,
            ),
            index,
            "revised",
        )

    existing = {item.collaboration_id: item for item in current.Collaborations}
    fragments = feedback_stage.fragments_from_model(index, current)
    if scope.kind == "operation":
        selected_ids = set(scope.ids) or {use_case.id for use_case in index.use_cases}
        for use_case_id in sorted(selected_ids, key=id_key):
            use_case = index.use_case(use_case_id)
            others = {key: value for key, value in fragments.items() if key != use_case_id}
            previous = fragments.get(use_case_id)
            if previous is not None:
                others[use_case_id] = AcceptedFragment(
                    use_case_id=use_case_id,
                    payload={
                        "DataTypes": previous.as_payload().get("DataTypes") or [],
                        "Classes": [],
                    },
                )
            base = operations.compose_fragments(accepted_inventory, others)
            replacement = operations.checked_fragment(
                index,
                accepted_inventory,
                use_case,
                previous=previous,
                findings=[f"User feedback: {feedback}"],
                reserved=operations.reserved_operations(base),
                reserved_types=list(_payload(base).get("DataTypes") or []),
                allowed_step_ids=tuple(step.id for step in use_case.steps),
                operation="InteractionOperationFeedback",
                cache=cache,
            )
            prior_types = {
                str(item.get("name") or ""): item
                for item in (previous.as_payload().get("DataTypes") or []) if isinstance(item, dict)
            } if previous is not None else {}
            replacement_payload = replacement.as_payload()
            replacement_types = {
                str(item.get("name") or ""): item
                for item in replacement_payload.get("DataTypes") or [] if isinstance(item, dict)
            }
            fragments[use_case_id] = AcceptedFragment(
                use_case_id=use_case_id,
                payload={
                    **replacement_payload,
                    "DataTypes": list((prior_types | replacement_types).values()),
                },
            )
        composed = operations.compose_fragments(accepted_inventory, fragments)
        skeleton = materialize_pre_collaboration_refs(
            current, composed, targeted_refs=targets,
        )
        operation_ids = {
            operation.operation_id
            for item in skeleton.Classes
            for operation in item.operations
        }
        if all(
            call.receiver_operation_id in operation_ids
            for item in current.Collaborations
            for call in item.calls
        ):
            revised = BCEModel.model_validate({
                **_payload(skeleton),
                "Collaborations": [
                    item.model_dump(by_alias=True) for item in current.Collaborations
                ],
            })
            return _accepted_model(current, revised, targeted_refs=targets)
        selected_use_cases = [
            use_case for use_case in _standalone(index)
            if use_case.id in selected_ids or any(
                set(group.trace_use_case_ids) & selected_ids
                for group in index.groups if group.use_case_id == use_case.id
            )
        ]
        existing, selected_use_cases = _rematerialize_preserved_collaborations(
            index,
            current,
            skeleton,
            existing,
            selected_use_cases,
        )
        directive = ""
    else:
        skeleton = materialize_pre_collaboration_refs(
            current,
            BCEModel.model_validate({**_payload(current), "Collaborations": []}),
            targeted_refs=targets,
        )
        selected_ids = set(scope.ids) or {item.id for item in _standalone(index)}
        selected_use_cases = [
            use_case for use_case in _standalone(index) if use_case.id in selected_ids
        ]
        directive = feedback
        revised = _replace_selected_collaborations(
            index,
            skeleton,
            current,
            selected_use_cases,
            feedback=directive,
            cache=cache,
        )
        return _accepted_model(current, revised, targeted_refs=targets)
    revised = _complete_collaborations(
        index,
        skeleton,
        existing,
        selected_use_cases,
        feedback=directive,
        cache=cache,
    )
    accepted = _accepted_model(current, revised, targeted_refs=targets)
    # Internal operation repairs are checked against the pre-repair baseline by
    # the caller. Requiring a globally clean model here would make unrelated
    # existing findings block a bounded repair.
    if operation_use_case_ids is not None:
        return accepted
    return _validated(accepted, index, "revised")


__all__ = ["generate_class_model", "resume_class_model", "revise_class_model"]
