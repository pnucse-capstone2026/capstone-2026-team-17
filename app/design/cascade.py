"""지목 수정 — 고를 항목 하나를 고치고, 추적표가 알려준 하류 항목만 따라 고친다.

**왜 되감기로는 안 되나.** 되감기는 그 스테이지를 처음부터 다시 만든다.
시퀀스 추출은 클래스 다이어그램 전체를 프롬프트로 받아 유스케이스별 모델을
새로 생성하므로, 클래스 하나가 바뀌면 **수정과 무관한 다이어그램까지 달라질 수 있다.**
사용자가 승인해둔 내용이 날아간다. "필드 하나 추가"의 대가로 산출물 전체를 잃는 것이다.

**그래서 여기서는 고칠 것만 고친다.**

    "Order 클래스에 주문일시 추가"
       ↓ class 모델의 Order 만 수정
       ↓ 추적표: class:Order → api_spec:Order, erd:Order, deployment:order.jar
    그 항목들만 수정. 나머지는 글자 하나 안 바뀐다.

**보장은 프롬프트가 아니라 코드가 한다.** 리바이저는 여전히 모델 전체를 돌려주고, LLM은
지시를 어길 수 있다. `merge_model`(nodes/artifact.py)이 **비대상 항목에 대해서는 LLM
출력을 아예 읽지 않으므로**, 어겨도 결과에 닿지 못한다. 프롬프트의 범위 지시는 대상이
잘 고쳐지도록 초점을 좁히는 보조 수단일 뿐이다.
"""
from __future__ import annotations

import json
from typing import Any

from app.artifact_trace import TraceRef
from app.db.models import ORIGIN_FEEDBACK_REVISED
from app.design.class_target_scope import (
    AmbiguousClassMergeTarget,
    UnknownClassMergeTarget,
    class_execution_merge_targets,
)
from app.design.graphs.subgraphs import DESIGN_SPECS
from app.design.nodes.artifact import (
    CHECKED_ONLY,
    CLEAN,
    DesignArtifactSpec,
    assert_untargeted_elements_preserved,
    finding_details,
    merge_model,
    render_and_validate,
)
from app.design.revision_impact import design_revision_impact
from app.design.rtm import (
    build_design_rtm,
    linked_elements,
)
from app.design.schemas.architecture_state import ArchitectureState
from app.design.schemas.class_model import BCEModel
from app.design.services.class_diagram.patches import apply_structured_patches
from app.repositories import artifact_repository


class UnknownTarget(Exception):
    """지목한 항목이 지금 산출물에 없다."""


class UnapprovedScopeExpansion(Exception):
    """A revision would edit an authority/downstream target absent from the plan.

    This is deliberately distinct from ``UnknownTarget``.  The target can be
    real and exactly linked, but a natural-language request is not permission
    to mutate its authoritative contract.  Callers must surface the planned
    scope and retry with that frozen approval.
    """


def _design_target(value: str) -> TraceRef | None:
    """Parse a stage-qualified target through the shared ref contract."""

    try:
        parsed = TraceRef.parse(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.kind in DESIGN_SPECS else None


def _class_execution_merge_targets(
    state: ArchitectureState, requested_targets: set[str]
) -> set[str]:
    """Map a catalog class row to its real, bounded merge unit.

    Class operations and calls are visible RTM rows, but are nested inside
    ``Classes`` and ``Collaborations`` respectively.  Passing their row IDs to
    ``merge_model`` would silently preserve the old value.  Keep the precise
    row ID for the reviser and use this adapter only at the merge boundary.
    """
    class_spec = DESIGN_SPECS["class_diagram"]
    model = state.get(class_spec.model_key) or {}
    if not isinstance(model, dict):
        raise UnapprovedScopeExpansion("The class model is unavailable for target normalization.")
    try:
        return class_execution_merge_targets(model, requested_targets)
    except AmbiguousClassMergeTarget as error:
        raise UnapprovedScopeExpansion(str(error)) from error
    except UnknownClassMergeTarget as error:
        raise UnknownTarget(str(error)) from error


def _check_report(
    spec: DesignArtifactSpec, model: dict, state: ArchitectureState
) -> dict[str, Any]:
    """고친 모델을 규칙으로 검사한다 — **재생성은 하지 않는다.**

    **왜 여기서도 검사해야 하나.** 예전에는 이 경로가 규칙 검사를 아예 안 돌렸다. 그래서
    그래프 실행이 남긴 `{findings: [], stopped: "clean"}`이 상태에 그대로 남고, 지목 수정이
    모델을 고친 뒤에도 화면은 **아무도 검사하지 않은 새 모델에 대해 계속 "clean"을**
    보여줬다. 낡은 판정이 새 산출물의 보증으로 둔갑하는 것이고, 이 기능 전체가 막으려던
    실패("위반 없음"과 "검사하지 않았음"을 구별하기)와 정확히 같은 것이다.

    **왜 재생성은 안 하나.** 이 경로의 보장은 "지목한 항목만 바뀐다"이고, 그것은
    `merge_model`이 비대상 항목에 대해 LLM 출력을 아예 안 읽어서 성립한다. 그런데 재생성은
    `targets=set()`(전체 수정)으로 부르므로, 여기서 루프를 돌리면 **그 보장을 스스로 깬다.**
    사용자가 "Order 에 주문일시 추가"를 요청했는데 다른 클래스가 조용히 바뀌는 것이다.
    그래서 드러내기만 하고, 고칠지는 사용자가 정한다.
    """
    findings = spec.check(model, state)
    return {
        "findings": [f.as_issue() for f in findings],
        "finding_details": finding_details(findings, spec.stage),
        "repair_iters": 0,
        "stopped": CLEAN if not findings else CHECKED_ONLY,
    }


def _apply(
    spec: DesignArtifactSpec,
    state: ArchitectureState,
    feedback: str,
    targets: set[str],
    *,
    revision_targets: set[str] | None = None,
    patch_intents: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """한 스테이지에서 대상 항목만 고치고, 검사·렌더까지 마친 상태 조각을 돌려준다."""
    original = state.get(spec.model_key) or {}
    reviser_targets = revision_targets if revision_targets is not None else targets
    if patch_intents:
        if spec.stage != "class_diagram":
            raise ValueError("Structured design patches currently require a class target.")
        revised = apply_structured_patches(original, patch_intents)
        # Validate the accepted schema without serializing it again: a dump of
        # a legacy artifact would add optional ``stableId: null`` fields to
        # every untouched element and violate the minimal-diff contract.
        BCEModel.model_validate(revised)
    else:
        revised = spec.revise(original, feedback, state, reviser_targets)

    merge_targets = set(targets)
    if spec.stage == "class_diagram":
        merge_targets.update(
            _changed_collaboration_targets(original, revised)
            if patch_intents
            else _class_collaboration_dependency_targets(original, revised, targets)
        )
    merged = merge_model(spec, original, revised, merge_targets)
    # The LLM boundary ends here: merge_model must retain every non-target
    # value from the persisted source before a deterministic finalizer derives
    # its own runtime bundle fields.
    assert_untargeted_elements_preserved(spec, original, merged, merge_targets)

    # 지목 수정은 비대상 보존이 계약이므로 전체 흐름 재추출을 포함하는 reconcile은
    # 실행하지 않는다. 대신 최종 구성 규칙은 반드시 적용해, 새 시퀀스 호출의 메서드는
    # 수신 클래스에 결정론적으로 보강한 뒤에만 렌더한다.
    working: ArchitectureState = {**state, spec.model_key: merged}
    patch: dict[str, Any] = {spec.model_key: merged}
    if spec.finalize:
        finalized = spec.finalize(working)
        patch.update(finalized)
        working.update(finalized)
        merged = working.get(spec.model_key) or merged

    patch.update(render_and_validate(spec, merged, working))
    if spec.check_key:
        patch[spec.check_key] = _check_report(spec, merged, working)
    return patch


def _changed_collaboration_targets(
    original: dict[str, Any], revised: dict[str, Any]
) -> set[str]:
    """Return exact collaboration IDs changed by a deterministic patch.

    Operation renames must update receiver references, while operation additions
    do not own any collaboration change. Comparing the two bounded artifacts
    avoids widening the merge to every collaboration that mentions the class.
    """

    def indexed(model: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {
            str(item.get("collaborationId") or "").strip(): item
            for item in model.get("Collaborations") or []
            if isinstance(item, dict)
            and str(item.get("collaborationId") or "").strip()
        }

    before = indexed(original)
    after = indexed(revised)
    return {
        collaboration_id
        for collaboration_id in before.keys() | after.keys()
        if before.get(collaboration_id) != after.get(collaboration_id)
    }


def _class_collaboration_dependency_targets(
    original: dict[str, Any],
    revised: dict[str, Any],
    targets: set[str],
) -> set[str]:
    """Include collaborations whose operation reference changes with a class.

    Operation IDs contain the parameter signature.  Replacing a targeted class
    can therefore turn ``Boundary::login(request:LoginRequest)`` into a different
    canonical ID.  Keeping the old collaboration byte-for-byte then creates a
    dangling receiverOperationId.  ``revise_class_model`` already returns a fully
    validated model with repaired collaborations, so accept only those exact
    dependency-owned collaboration replacements alongside the selected class.
    """

    def operation_ids(model: dict[str, Any]) -> set[str]:
        return {
            str(operation.get("operationId") or "").strip()
            for class_item in model.get("Classes") or []
            if isinstance(class_item, dict)
            and str(class_item.get("className") or "").strip() in targets
            for operation in class_item.get("operations") or []
            if isinstance(operation, dict)
            and str(operation.get("operationId") or "").strip()
        }

    owned_operations = operation_ids(original) | operation_ids(revised)
    if not owned_operations:
        return set()
    return {
        str(collaboration.get("collaborationId") or "").strip()
        for model in (original, revised)
        for collaboration in model.get("Collaborations") or []
        if isinstance(collaboration, dict)
        and str(collaboration.get("collaborationId") or "").strip()
        and any(
            isinstance(call, dict)
            and str(call.get("receiverOperationId") or "").strip()
            in owned_operations
            for call in collaboration.get("calls") or []
        )
    }


def _refs_by_stage(refs: list[str]) -> dict[str, set[str]]:
    """Split ``stage:element`` references without accepting unknown stages."""
    grouped: dict[str, set[str]] = {}
    for ref in refs:
        parsed = _design_target(ref)
        if parsed is not None:
            grouped.setdefault(parsed.kind, set()).add(parsed.id)
    return grouped


def _selected_source_payload(
    state: ArchitectureState, stage: str, elements: set[str]
) -> dict[str, Any]:
    """Return only the source elements that justify a downstream update.

    Passing an entire diagram/API document back to a reviser gives it needless
    opportunities to reinterpret unrelated content.  The feedback already
    carries the user's instruction; this compact payload provides just the
    exact RTM-linked evidence for the other artifact.
    """
    spec = DESIGN_SPECS[stage]
    model = state.get(spec.model_key) or {}
    if not isinstance(model, dict):
        return {}
    selected: dict[str, list[dict[str, Any]]] = {}
    for field, key_of in spec.elements.items():
        matches = [
            item
            for item in model.get(field, []) or []
            if isinstance(item, dict) and key_of(item) in elements
        ]
        if matches:
            selected[field] = matches
    return selected


def _trace_backed_feedback(
    state: ArchitectureState,
    source_stage: str,
    source_elements: set[str],
    feedback: str,
) -> str:
    """Give a related artifact only approved, trace-backed revision evidence."""
    evidence = _selected_source_payload(state, source_stage, source_elements)
    return (
        f'The user explicitly revised {source_stage}:{", ".join(sorted(source_elements))} '
        f'with: "{feedback}". This is an exact RTM contract link, not a name match. '
        "Update only the listed target elements so their existing contract agrees. "
        "Do not add, remove, rename, or alter any other element or any unrelated "
        "field, method, relationship, message, endpoint, or schema. If the evidence "
        "does not determine a safe change, preserve the target unchanged.\n"
        "[Trace-backed source elements]\n"
        + json.dumps(evidence, ensure_ascii=False, indent=2)
    )


def _reproject_erd(state: ArchitectureState) -> dict[str, Any]:
    """ERD 를 다시 만든다 — **LLM 을 부르지 않는다.**

    ERD 는 클래스 BCE 의 <<Entity>> 를 결정론적으로 투영한 것이다. 클래스가 바뀌면
    다시 투영하면 그만이고, 물어볼 것이 없다.

    **재투영한 모델도 검사한다.** 클래스 다이어그램이 통과했다는 것이 ERD 의 보증이
    아니기 때문이다 — 같은 BCE 라도 두 스테이지가 보는 규칙이 다르다(다중도가 없는 관계,
    기본키 없는 테이블, 이름으로 가리킨 참조는 전부 ERD 쪽에서만 결함이다). 검사를
    빼면 클래스 쪽 수정이 ERD 를 조용히 망가뜨려도 화면은 아무 말을 안 한다.

    여기서도 재생성은 안 한다(`checked_only`) — 이 경로의 보장은 "지목한 것만 바뀐다"이고,
    ERD 는 애초에 물어보지 않고 다시 그리는 자리다.
    """
    spec = DESIGN_SPECS["erd"]
    model = spec.extract(state)
    patch: dict[str, Any] = {
        spec.model_key: model,
        **render_and_validate(spec, model, state),
    }
    if spec.check_key:
        patch[spec.check_key] = _check_report(spec, model, state)
    return patch


def _apply_projection(
    spec: DesignArtifactSpec,
    state: ArchitectureState,
    targets: set[str],
) -> dict[str, Any]:
    """Refresh derived sequence units without invoking a reviser.

    A class-authority change is already approved and sequence is its
    deterministic projection.  It never invokes a sequence feedback reviser.
    """
    original = state.get(spec.model_key) or {}
    projected = spec.extract(state)
    merged = merge_model(spec, original, projected, targets)
    assert_untargeted_elements_preserved(spec, original, merged, targets)
    # ``projected`` comes from deterministic code, not an LLM. Targeted list
    # merging must preserve unrelated diagrams, while collection-level
    # provenance (for example the class diagram hash) must describe the new
    # projection rather than the old persisted collection.
    for field_name, value in projected.items():
        if field_name not in spec.elements:
            merged[field_name] = value
    working: ArchitectureState = {**state, spec.model_key: merged}
    patch: dict[str, Any] = {spec.model_key: merged}
    if spec.finalize:
        finalized = spec.finalize(working)
        patch.update(finalized)
        working.update(finalized)
        merged = working.get(spec.model_key) or merged
    patch.update(render_and_validate(spec, merged, working))
    if spec.check_key:
        patch[spec.check_key] = _check_report(spec, merged, working)
    return patch


def _frozen_cascade_scope(
    state: ArchitectureState,
    rtm: dict,
    stage: str,
    element: str,
    approved_downstream_targets: set[str] | None,
) -> dict[str, set[str]]:
    """Calculate bounded forward targets from the frozen pre-change RTM."""
    scheduled = design_revision_impact(state, rtm, stage, element)

    if approved_downstream_targets is not None:
        approved = set(approved_downstream_targets)
        unapproved = sorted(
            str(TraceRef(target_stage, target_element))
            for target_stage, elements in scheduled.items()
            if target_stage != "erd"
            for target_element in elements
            if str(TraceRef(target_stage, target_element)) not in approved
        )
        if unapproved:
            raise UnapprovedScopeExpansion(
                "The frozen approved downstream scope excludes: " + ", ".join(unapproved)
            )
    return scheduled


def revise_and_cascade(
    state: ArchitectureState,
    target: str,
    feedback: str,
    *,
    approved_authority_targets: set[str] | None = None,
    approved_downstream_targets: set[str] | None = None,
    revision_context_targets: set[str] | None = None,
    patch_intents: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """`{stage}:{element}` 를 고치고, 증명된 관련 항목만 따라 고친다.

    반환 {"state": 바뀐 상태, "changed": [스테이지...], "touched": {스테이지: [항목...]}}
    — 화면이 "무엇을 고쳤는지" 보여줄 재료다.

    클래스 authority는 반드시 class target으로 실행하고 frozen RTM을 따라 하류를 고친다.
    sequence는 class의 결정론적 projection이므로 직접 수정할 수 없으며, API 수정도 API
    산출물에만 국소 적용한다. 이전 design stage를 역방향으로 수정하지 않는다.

    어느 경로든 무관한 스테이지는 리바이저를 **부르지도 않는다**. LLM 출력은
    finalizer보다 먼저 ``assert_untargeted_elements_preserved``를 통과해야 하므로,
    환각한 형제 변경은 저장·전파되기 전에 거절된다. 그 뒤의 finalizer는 별도 LLM
    출력 없이 실행되는 결정론적 번들 투영이다.
    """
    parsed_target = _design_target(target)
    if parsed_target is None or parsed_target.kind == "erd":
        raise UnknownTarget(f"{target} is not an editable design element.")
    stage, element = parsed_target.kind, parsed_target.id
    if stage == "sequence_diagram":
        raise UnapprovedScopeExpansion(
            "Sequence diagrams are deterministic class projections; revise the exact "
            "linked class authority instead."
        )
    approved_authority_targets = approved_authority_targets or set()
    unexpected_authority = sorted(
        ref for ref in approved_authority_targets if ref != target
    )
    if unexpected_authority:
        raise UnapprovedScopeExpansion(
            "A targeted design revision cannot apply additional authority targets: "
            + ", ".join(unexpected_authority)
        )

    working: ArchitectureState = dict(state)
    rtm = build_design_rtm(working)
    if not any(
        row["stage"] == stage and row["element"] == element for row in rtm["rows"]
    ):
        raise UnknownTarget(f"{target} is not in the current artifacts.")
    context_elements: set[str] = set()
    for context_target in revision_context_targets or set():
        parsed_context = _design_target(context_target)
        if parsed_context is None or parsed_context.kind != stage:
            continue
        if not any(
            row["stage"] == stage and row["element"] == parsed_context.id
            for row in rtm["rows"]
        ):
            raise UnknownTarget(f"{context_target} is not in the current artifacts.")
        context_elements.add(parsed_context.id)

    scheduled = _frozen_cascade_scope(
        working,
        rtm,
        stage,
        element,
        approved_downstream_targets,
    )

    changed: list[str] = []
    touched: dict[str, list[str]] = {}
    processed: dict[str, set[str]] = {}

    def apply_targets(
        target_stage: str,
        targets: set[str],
        revision_feedback: str,
        *,
        deterministic_projection: bool = False,
    ) -> None:
        """Apply one bounded stage patch and record its immutable scope."""
        pending = targets - processed.get(target_stage, set())
        if not pending:
            return
        merge_pending = (
            _class_execution_merge_targets(working, pending)
            if target_stage == "class_diagram"
            else pending
        )
        if deterministic_projection:
            patch = _apply_projection(DESIGN_SPECS[target_stage], working, pending)
        else:
            reviser_pending = set(pending)
            if target_stage == stage and element in pending:
                reviser_pending.update(context_elements)
            patch = _apply(
                DESIGN_SPECS[target_stage],
                working,
                revision_feedback,
                merge_pending,
                revision_targets=reviser_pending,
                patch_intents=(
                    patch_intents
                    if target_stage == stage and element in pending
                    else None
                ),
            )
        working.update(patch)
        processed.setdefault(target_stage, set()).update(pending)
        if target_stage not in changed:
            changed.append(target_stage)
            touched[target_stage] = []
        touched[target_stage] = sorted(set(touched[target_stage]) | pending)

    # ① Direct links and forward targets were frozen from the *pre-change* RTM
    # above.  A revision cannot create a newly editable neighbour mid-flight.
    source_elements = {element}
    trace_feedback = _trace_backed_feedback(state, stage, source_elements, feedback)

    # The user-selected element is the only editable source.  Earlier stages
    # are never reverse-mutated from a later design artifact.
    apply_targets(stage, {element}, feedback)

    # ③ A touched class is now the authoritative structural change.  Follow its
    # frozen forward provenance links, but do not re-edit the user-selected
    # source element.  Reprocessing it could overwrite the feedback it just
    # approved and would create a new LLM opportunity for unrelated changes.
    for next_stage in ("sequence_diagram", "api_spec"):
        apply_targets(
            next_stage,
            scheduled.get(next_stage, set()),
            trace_feedback,
            deterministic_projection=(next_stage == "sequence_diagram" and stage != "sequence_diagram"),
        )

    # ERD is deterministic, never LLM-revised.  As before, do not materialize a
    # future stage solely because an earlier one was edited.
    if (
        processed.get("class_diagram")
        and "erd" in DESIGN_SPECS
        and working.get(DESIGN_SPECS["erd"].model_key)
    ):
        working.update(_reproject_erd(working))
        if "erd" not in changed:
            changed.append("erd")
        touched["erd"] = ["(reprojected from class BCE)"]

    # Deployment has no reverse contract link.  It is updated only when the
    # frozen class provenance explicitly names one of its elements.
    apply_targets(
        "deployment_diagram",
        scheduled.get("deployment_diagram", set()),
        trace_feedback,
    )

    return {
        "state": working,
        "changed": changed,
        "touched": touched,
        "related": sorted(linked_elements(rtm, stage, element)),
        "regenerated": (
            {"erd": ["(reprojected from class BCE)"]} if "erd" in changed else {}
        ),
    }


def persist_cascade(app_id: str, result: dict[str, Any]) -> None:
    """고친 스테이지만 새 버전으로 남긴다. 안 고친 것은 저장하지 않는다."""
    # The revision service keeps the whole batch in memory first; persist its
    # changed stages in the repository's single transaction as well so a DB
    # error cannot leave half a cascade visible.
    artifact_repository.save_stages(
        app_id,
        result["changed"],
        result["state"],
        origin=ORIGIN_FEEDBACK_REVISED,
    )
