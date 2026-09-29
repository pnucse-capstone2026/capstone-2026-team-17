"""설계 추적 매트릭스 — 어느 산출물이 무엇에서 나왔는지, 무엇이 영향받는지.

**왜 필요한가.** 설계도 다섯 장은 앞장을 재료로 만들어진다. 그래서 앞장을 고치면 뒷장이
낡는다. 지금은 Workspace의 설계 되감기로 푼다 — 뒤쪽을 **전부** 다시 만들어서
확실하지만 무식하다. 추적표가 있으면 **영향받는 항목만** 다시 만들 수 있다.

**설계 원칙은 요구사항 추적표(app/requirements/traceability.py)와 같다:
추적표를 별도 LLM 호출로 만들지 않는다.** 각 모델이 자기 출처를 필드로 들고 다니고
(`use_case_ids`, `source_classes`, `source_class`), 이 모듈은 그걸 **순수 함수로 모으기만**
한다. LLM을 한 번 더 부르면 산출물과 추적표가 어긋날 기회가 생긴다 — 여기서는 어긋날
자리가 없다.

**추적이 없는 것을 숨기지 않는다.** 링크가 빠졌는데 "일관성 보장"을 믿으면 지금보다
나쁘다 — 지금은 최소한 낡았을 수 있다는 걸 안다. 그래서:
  - `orphan` — 출처를 하나도 안 밝힌 항목. 영향 분석에서 누락될 후보.
  - `unknown_refs` — 존재하지 않는 것을 가리키는 참조(환각 또는 이름 변경의 흔적).

ERD만 추적 필드가 없다. LLM이 만들지 않고 클래스 다이어그램의 `<<Entity>>`에서
결정론적으로 투영되므로, 추적이 **코드에 내재**되어 있다 — 여기서 계산해 준다.

이것은 Phase 1(읽기 전용 물질화)이다. 영향 전파(캐스케이드)는 이 위에 올린다.
"""
from __future__ import annotations

from typing import Any

from app.design.schemas.class_model import operation_method_signature
from app.design.services.erd.plantuml import sanitize_entity_name
from app.design.services.sequence_diagram.methods import method_name

#: 추적 대상 상류: 유스케이스와 클래스. 나머지 산출물은 이 둘을 통해 간접 추적된다.
UPSTREAM_KINDS = ("use_case", "class")


def _class_method_signatures(class_item: dict[str, Any]) -> list[str]:
    """Project current typed operations and retain a read-only legacy fallback."""

    operations = class_item.get("operations")
    if isinstance(operations, list):
        return [
            operation_method_signature(
                str(operation.get("name") or ""),
                list(operation.get("parameters") or []),
                str(operation.get("returnType") or "void"),
            )
            for operation in operations
            if isinstance(operation, dict) and str(operation.get("name") or "").strip()
        ]
    return [str(method) for method in class_item.get("methods") or []]


def upstream_names(state: dict) -> dict[str, set[str]]:
    """참조가 가리킬 수 있는 이름들. 여기 없는 것을 가리키면 unknown_ref다.

    **공개인 이유**: `knowledge/detectors.py`의 `class.usecase-ids-exist` 검출기가 이것과
    **같은 판정**을 한다. 다른 점은 시점뿐이다 — 검출기는 스테이지 안에서 고칠 기회를
    주려고 보고, 여기는 다 만든 뒤 사후 보고로 본다. 판정이 두 벌이면 갈라지고, 갈라지면
    스테이지에서 통과한 것이 추적표에서 환각으로 잡히는(또는 그 반대) 일이 생긴다.
    """
    spec = state.get("usecase_spec") or {}
    use_cases = spec.get("use_cases", []) if isinstance(spec, dict) else []
    classes = (state.get("extracted_bce_classes") or {}).get("Classes", [])
    return {
        "use_case": {u["id"] for u in use_cases if isinstance(u, dict) and u.get("id")},
        "class": {c["className"] for c in classes if c.get("className")},
    }


def _entity_classes(state: dict) -> list[str]:
    """ERD 테이블이 되는 클래스 — <<Entity>> 스테레오타입만."""
    classes = (state.get("erd_bce_classes") or {}).get("Classes", [])
    return [
        c["className"]
        for c in classes
        if c.get("className") and "entity" in str(c.get("stereotype", "")).lower()
    ]


def _row(stage: str, element: str, sources: dict[str, list[str]]) -> dict[str, Any]:
    """추적 행 하나. 출처가 하나도 없으면 orphan."""
    clean = {kind: sorted(set(v)) for kind, v in sources.items() if v}
    return {
        "stage": stage,
        "element": element,
        "sources": clean,
        "status": "traced" if clean else "orphan",
    }


def _as_list(value: Any) -> list[str]:
    """문자열 하나든 목록이든 목록으로. 빈 값은 버린다."""
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value if v]


def _sequence_call_links(sequence: dict) -> list[dict[str, str]]:
    """Return traceable sequence call → receiver-class links.

    A sequence diagram can contain several calls, but the public editable unit is
    a use-case card when the modern ``Diagrams`` collection is present.  Legacy
    singleton models retain the individual message label as their editable unit.
    The result deliberately includes *only* concrete call messages with a known
    receiver class: a participant name or a narrative step is not evidence that
    editing it should rewrite a class contract.
    """
    if not isinstance(sequence, dict):
        return []

    diagrams = sequence.get("Diagrams")
    if isinstance(diagrams, list):
        units = [
            (str(diagram.get("use_case_id") or "").strip(), diagram)
            for diagram in diagrams
            if isinstance(diagram, dict) and str(diagram.get("use_case_id") or "").strip()
        ]
    else:
        units = [("", sequence)]

    calls: list[dict[str, str]] = []
    call_types = {"sync", "async", "self"}
    for use_case_id, diagram in units:
        participants = {
            str(item.get("alias") or item.get("name") or "").strip(): str(
                item.get("source_class") or ""
            ).strip()
            for item in diagram.get("Participants", []) or []
            if isinstance(item, dict)
        }
        for message in diagram.get("Messages", []) or []:
            if not isinstance(message, dict) or str(message.get("type") or "sync").lower() not in call_types:
                continue
            receiver = participants.get(str(message.get("target") or "").strip(), "")
            label = str(message.get("label") or "").strip()
            called_method = method_name(label)
            if not receiver or not called_method:
                continue
            if use_case_id:
                element = use_case_id
                call_use_cases = {use_case_id}
            else:
                element = _message_element(message)
                call_use_cases = set(_as_list(message.get("use_case_ids")))
            if not element:
                continue
            calls.append(
                {
                    "element": element,
                    "receiver_class": receiver,
                    "method": called_method,
                    "use_case_ids": "\u0000".join(sorted(call_use_cases)),
                }
            )
    return calls


def _message_element(message: dict[str, Any]) -> str:
    """Use the same legacy message identity as the RTM row and edit API."""
    return "{} -> {} : {}".format(
        message.get("source", "?"), message.get("target", "?"), message.get("label", "")
    ).strip()


def _direct_links(state: dict, known_classes: set[str]) -> list[dict[str, str]]:
    """Build only exact, model-backed cross-artifact links.

    The old RTM connected every artifact to requirements/classes through broad
    provenance fields.  That is useful for forward regeneration but insufficient
    evidence for changing an earlier contract.  Reverse propagation is allowed
    only for these stronger links:

    * a sequence projection whose collaboration names an exact declared operation;
    * an API operation whose interaction names its exact Boundary operation;
    * an API Control binding that resolves to one exact declared operation;
    * the API operation and sequence call share a use-case id.

    No fuzzy name match and no LLM-derived edge is accepted.  If an existing
    artifact lacks the exact binding, it simply has no reverse-cascade path.
    """
    sequence_calls = _sequence_call_links(state.get("sequence_diagram_model") or {})
    class_model = state.get("extracted_bce_classes") or {}

    def declared_method(operation: dict[str, Any]) -> str:
        operation_id = str(operation.get("operationId") or "").strip()
        signature = operation_id.partition("::")[2] or operation_id
        return method_name(str(operation.get("name") or signature).strip())

    declared_operations = {
        (
            str(item.get("className") or "").strip(),
            declared_method(operation),
        ): str(operation.get("operationId") or "").strip()
        for item in class_model.get("Classes", []) or []
        if isinstance(item, dict) and str(item.get("className") or "").strip()
        for operation in item.get("operations") or []
        if isinstance(operation, dict)
        and str(operation.get("operationId") or "").strip()
        and declared_method(operation)
    }
    declared_methods = {
        (class_name, parsed_method)
        for item in class_model.get("Classes", []) or []
        if isinstance(item, dict)
        if (class_name := str(item.get("className") or "").strip())
        for signature in _class_method_signatures(item)
        if (parsed_method := method_name(signature))
    }
    declared_operation_ids = set(declared_operations.values())
    links: set[tuple[str, str, str]] = set()

    available_sequences = {
        str(item.get("use_case_id") or "").strip()
        for item in (state.get("sequence_diagram_model") or {}).get("Diagrams", []) or []
        if isinstance(item, dict) and str(item.get("use_case_id") or "").strip()
    }
    for item in class_model.get("Collaborations", []) or []:
        if not isinstance(item, dict):
            continue
        collaboration_id = str(item.get("collaborationId") or "").strip()
        collaboration_use_cases = set(_as_list(item.get("useCaseIds")))
        projected_use_cases = collaboration_use_cases & available_sequences
        if not collaboration_id or not projected_use_cases:
            continue
        for use_case_id in projected_use_cases:
            links.add((
                f"sequence_diagram:{use_case_id}",
                f"class_diagram:{collaboration_id}",
                "projects_collaboration",
            ))
        for call in item.get("calls") or []:
            call_id = str(call.get("callId") or "").strip() if isinstance(call, dict) else ""
            operation_id = (
                str(call.get("receiverOperationId") or "").strip()
                if isinstance(call, dict)
                else ""
            )
            for use_case_id in projected_use_cases:
                if call_id:
                    links.add((
                        f"sequence_diagram:{use_case_id}",
                        f"class_diagram:{call_id}",
                        "projects_call",
                    ))
                if operation_id in declared_operation_ids:
                    links.add((
                        f"sequence_diagram:{use_case_id}",
                        f"class_diagram:{operation_id}",
                        "projects",
                    ))

    for call in sequence_calls:
        receiver = call["receiver_class"]
        if receiver in known_classes:
            links.add(
                (
                    f"sequence_diagram:{call['element']}",
                    f"class_diagram:{receiver}",
                    "invokes",
                )
            )

    for endpoint in (state.get("api_spec_model") or {}).get("Endpoints", []) or []:
        if not isinstance(endpoint, dict):
            continue
        operation = str(endpoint.get("operation_id") or "").strip()
        binding = endpoint.get("control_binding")
        if not operation or not isinstance(binding, dict):
            continue
        control = str(binding.get("control") or "").strip()
        # Sequence labels are complete calls while the API binding records the
        # declared method name.  Normalize both through the same parser before
        # comparing; case or surrounding signature syntax must not create a
        # second, weaker name-matching rule.
        method = method_name(str(binding.get("method") or "").strip())
        endpoint_use_cases = set(_as_list(endpoint.get("use_case_ids")))
        control_operation = declared_operations.get((control, method))
        if (
            control not in known_classes
            or (control, method) not in declared_methods
            or not endpoint_use_cases
        ):
            continue

        links.add((f"api_spec:{operation}", f"class_diagram:{control}", "binds"))
        if control_operation:
            links.add((
                f"api_spec:{operation}",
                f"class_diagram:{control_operation}",
                "binds_operation",
            ))
        interaction_id = str(endpoint.get("interaction_id") or "").strip()
        boundary_operation = interaction_id.partition(" -> ")[0].strip()
        if boundary_operation in declared_operation_ids:
            links.add((
                f"api_spec:{operation}",
                f"class_diagram:{boundary_operation}",
                "accepts",
            ))
        for call in sequence_calls:
            if control != call["receiver_class"] or method != call["method"]:
                continue
            if endpoint_use_cases.isdisjoint(call["use_case_ids"].split("\u0000")):
                continue
            links.add(
                (
                    f"api_spec:{operation}",
                    f"sequence_diagram:{call['element']}",
                    "implements",
                )
            )

    return [
        {"from": source, "to": target, "relation": relation}
        for source, target, relation in sorted(links)
    ]


def build_design_rtm(state: dict) -> dict[str, Any]:
    """state에서 설계 추적 매트릭스를 집계한다(순수 함수, LLM 없음).

    반환:
      rows         — 설계 항목별 추적 행 {stage, element, sources, status}
      impact       — 역방향 색인 {"class:Order": ["api_spec:Order", ...]}
                     "이게 바뀌면 무엇이 영향받나"에 답한다. 캐스케이드가 쓸 것.
      unknown_refs — 존재하지 않는 것을 가리킨 참조
      summary      — 집계
    """
    upstream = upstream_names(state)
    rows: list[dict[str, Any]] = []
    unknown: list[dict[str, str]] = []

    def note_unknown(stage: str, element: str, kind: str, refs: list[str]) -> None:
        for ref in refs:
            if ref not in upstream[kind]:
                unknown.append(
                    {"stage": stage, "element": element, "kind": kind, "ref": ref}
                )

    # --- 클래스 다이어그램: 유스케이스에서 나온다 -------------------------------
    class_model = state.get("extracted_bce_classes") or {}
    operation_classes: dict[str, str] = {}
    for cls in class_model.get("Classes", []):
        name = cls.get("className")
        if not name:
            continue
        ucs = _as_list(cls.get("use_case_ids"))
        note_unknown("class_diagram", name, "use_case", ucs)
        rows.append(_row("class_diagram", name, {"use_case": ucs}))
        for operation in cls.get("operations") or []:
            if not isinstance(operation, dict):
                continue
            operation_id = str(operation.get("operationId") or "").strip()
            if not operation_id:
                continue
            operation_classes[operation_id] = str(name)
            step_refs = _as_list(operation.get("stepRefs"))
            operation_sources = {
                "use_case": ucs,
                "class": [name],
                "flow_step": step_refs,
            }
            rows.append(_row("class_diagram", operation_id, operation_sources))

    # Calls, not reusable operations, own concrete argument provenance.
    for collaboration in class_model.get("Collaborations") or []:
        if not isinstance(collaboration, dict):
            continue
        collaboration_id = str(collaboration.get("collaborationId") or "").strip()
        use_case_ids = _as_list(collaboration.get("useCaseIds"))
        if collaboration_id:
            rows.append(
                _row(
                    "class_diagram",
                    collaboration_id,
                    {"use_case": use_case_ids},
                )
            )
        for call in collaboration.get("calls") or []:
            if not isinstance(call, dict):
                continue
            call_id = str(call.get("callId") or "").strip()
            operation_id = str(call.get("receiverOperationId") or "").strip()
            if not call_id or not operation_id:
                continue
            class_name = operation_classes.get(operation_id, "")
            step_refs = _as_list(call.get("stepRefs"))
            rows.append(
                _row(
                    "class_diagram",
                    call_id,
                    {
                        "use_case": use_case_ids,
                        "class": [class_name] if class_name else [],
                        "class_operation": [operation_id],
                        "flow_step": step_refs,
                    },
                )
            )
            for binding in call.get("argumentBindings") or []:
                if not isinstance(binding, dict):
                    continue
                parameter = str(binding.get("parameter") or "").strip()
                if not parameter:
                    continue
                source_ref = str(binding.get("sourceRef") or "").strip()
                rows.append(
                    _row(
                        "class_diagram",
                        f"{call_id}#{parameter}",
                        {
                            "use_case": use_case_ids,
                            "class": [class_name] if class_name else [],
                            "class_operation": [operation_id],
                            "flow_step": step_refs,
                            "value_source": [source_ref] if source_ref else [],
                        },
                    )
                )

    # --- 시퀀스 다이어그램: 참가자는 클래스, 메시지는 유스케이스 + (양끝 참가자의) 클래스 --
    sequence = state.get("sequence_diagram_model") or {}

    diagrams = sequence.get("Diagrams")
    if isinstance(diagrams, list):
        for diagram in diagrams:
            if not isinstance(diagram, dict):
                continue
            use_case_id = str(diagram.get("use_case_id") or "").strip()
            if not use_case_id:
                continue
            classes = sorted(
                {
                    str(participant.get("source_class") or "").strip()
                    for participant in diagram.get("Participants") or []
                    if participant.get("source_class")
                }
            )
            note_unknown("sequence_diagram", use_case_id, "use_case", [use_case_id])
            note_unknown("sequence_diagram", use_case_id, "class", classes)
            rows.append(
                _row(
                    "sequence_diagram",
                    use_case_id,
                    {"use_case": [use_case_id], "class": classes},
                )
            )
        sequence = {}

    # 참가자 이름 → 클래스. 메시지가 어느 클래스에 의존하는지는 여기서 나온다.
    class_of_participant: dict[str, str] = {}
    for participant in sequence.get("Participants", []):
        name = participant.get("name")
        if not name:
            continue
        classes = _as_list(participant.get("source_class"))
        note_unknown("sequence_diagram", name, "class", classes)
        rows.append(_row("sequence_diagram", name, {"class": classes}))
        if classes:
            participant_id = participant.get("alias") or name
            class_of_participant[participant_id] = classes[0]

    for message in sequence.get("Messages", []):
        endpoints = (message.get("source", "?"), message.get("target", "?"))
        label = "{} -> {} : {}".format(*endpoints, message.get("label", "")).strip()
        ucs = _as_list(message.get("use_case_ids"))
        note_unknown("sequence_diagram", label, "use_case", ucs)

        # 메시지의 클래스 의존은 **모델에 안 적혀 있다** — 양끝 참가자를 거쳐 코드로
        # 잇는다. LLM에게 한 번 더 물어보면 참가자와 어긋날 기회만 생긴다.
        # 상류에 실제로 있는 클래스만 남긴다: 참가자의 source_class가 잘못돼 있으면
        # 그건 참가자 행에서 이미 unknown_ref로 보고됐고, 여기서 또 세면 중복이다.
        classes = [
            class_of_participant[end]
            for end in endpoints
            if class_of_participant.get(end) in upstream["class"]
        ]
        rows.append(_row("sequence_diagram", label, {"use_case": ucs, "class": classes}))

    # --- API 명세: 엔드포인트는 Boundary/Control, 스키마는 Entity에서 -----------
    api = state.get("api_spec_model") or {}
    for endpoint in api.get("Endpoints", []):
        name = endpoint.get("operation_id") or "{} {}".format(
            str(endpoint.get("method", "")).upper(), endpoint.get("path", "")
        ).strip()
        classes = _as_list(endpoint.get("source_classes"))
        ucs = _as_list(endpoint.get("use_case_ids"))
        note_unknown("api_spec", name, "class", classes)
        note_unknown("api_spec", name, "use_case", ucs)
        rows.append(_row("api_spec", name, {"class": classes, "use_case": ucs}))

    for schema in api.get("Schemas", []):
        name = schema.get("name")
        if not name:
            continue
        classes = _as_list(schema.get("source_class"))
        note_unknown("api_spec", name, "class", classes)
        rows.append(_row("api_spec", name, {"class": classes}))

    # --- ERD: 추적 필드가 없다. Entity 클래스의 투영이므로 여기서 계산한다 -----
    for entity in _entity_classes(state):
        rows.append(_row("erd", sanitize_entity_name(entity), {"class": [entity]}))

    # --- 배포 다이어그램: 노드·아티팩트가 클래스를 호스팅한다 ------------------
    deployment = state.get("deployment_diagram_model") or {}
    for group in ("Nodes", "Artifacts"):
        for item in deployment.get(group, []):
            name = item.get("name")
            if not name:
                continue
            classes = _as_list(item.get("source_classes"))
            note_unknown("deployment_diagram", name, "class", classes)
            rows.append(_row("deployment_diagram", name, {"class": classes}))

    # --- 역방향 색인: 상류 항목 하나가 바뀌면 무엇이 영향받나 ------------------
    impact: dict[str, list[str]] = {}
    for row in rows:
        target = f"{row['stage']}:{row['element']}"
        for kind, refs in row["sources"].items():
            for ref in refs:
                impact.setdefault(f"{kind}:{ref}", []).append(target)
    impact = {key: sorted(set(value)) for key, value in sorted(impact.items())}

    by_stage: dict[str, dict[str, int]] = {}
    for row in rows:
        counts = by_stage.setdefault(row["stage"], {"traced": 0, "orphan": 0})
        counts[row["status"]] += 1

    orphans = [r for r in rows if r["status"] == "orphan"]
    summary = {
        "total": len(rows),
        "traced": len(rows) - len(orphans),
        "orphan": len(orphans),
        "trace_ratio": round((len(rows) - len(orphans)) / len(rows), 4) if rows else 1.0,
        "unknown_ref_count": len(unknown),
        "by_stage": by_stage,
    }
    matrix = {
        "rows": rows,
        "impact": impact,
        # ``links`` are stronger than the broad provenance index above.  They
        # identify an exact API → sequence-call → class contract route and are
        # the only RTM edges safe enough to use for reverse change planning.
        "links": _direct_links(state, upstream["class"]),
        "unknown_refs": unknown,
        "summary": summary,
    }
    matrix["change_plan"] = _build_change_plan(matrix)
    return matrix


#: 파이프라인 순서. subgraphs.DESIGN_STAGES와 같다 — 이 모듈은 순수 함수라 그래프
#: 쪽을 끌어오고 싶지 않아서 여기 적는다.
_STAGE_ORDER = (
    "class_diagram",
    "sequence_diagram",
    "api_spec",
    "erd",
    "deployment_diagram",
)

#: 추적 행이 **다른 산출물의 참조 대상이기도 한** 경우의 이름 대응.
#:
#: `impact`의 키는 참조 이름("class:Order")이고 값은 행 이름("class_diagram:Order")인데,
#: 이 둘은 **같은 것**이다. 이름이 달라서 연결이 끊기므로 여기서 이어준다. 이게 없으면
#: "Order를 고치면 API·ERD도 바뀐다"의 두 번째 화살표를 못 따라간다.
#:
#: 클래스 다이어그램만 여기 있는 이유: 다른 산출물을 참조 대상으로 삼는 모델이 없다.
#: 시퀀스/API/배포를 가리키는 출처 필드가 생기면 여기에 추가한다.
_ROW_IS_ALSO_A_REF = {"class_diagram": "class"}

#: 지목 수정의 대상이 될 수 없는 스테이지. ERD 는 클래스 BCE 의 결정론적 투영이라
#: 직접 고칠 것이 없다 — 클래스를 고치면 따라온다.
_NOT_TARGETABLE = {"erd"}


def _build_change_plan(matrix: dict) -> list[dict[str, Any]]:
    """"이 항목을 고치면 무엇이 함께 바뀌는가"를 **설계 항목마다** 미리 계산한다.

    사용자는 지금 보고 있는 산출물에서 고칠 곳을 지목한다. 그래서 목록은 상류 참조
    (class:Order)가 아니라 **산출물의 항목**(class_diagram:Order)이다.

    유스케이스는 여기 없다. 그건 요구사항 분석의 산출물이라 설계 화면이 고칠 수 없다 —
    목록에 두면 "고칠 수 있다"는 거짓말이 된다.

    화면이 쓰라고 서버에서 만든다. 브라우저가 순회를 다시 구현하면 서버와 어긋날 수 있고,
    어긋난 쪽이 사용자에게 보이는 쪽이다.
    """
    plan: list[dict[str, Any]] = []
    for row in matrix["rows"]:
        if row["stage"] in _NOT_TARGETABLE:
            continue
        affected = affected_by_element(matrix, row["stage"], row["element"])
        plan.append(
            {
                "ref": f"{row['stage']}:{row['element']}",
                "stage": row["stage"],
                "element": row["element"],
                # Keep the row's exact provenance with the executable view.
                # Context search must not re-derive flow-step or class links
                # from names after the RTM has already established them.
                "sources": {
                    kind: list(refs)
                    for kind, refs in row.get("sources", {}).items()
                },
                # Direct, contract-backed neighbours.  The client can show
                # these as the bounded reverse-change scope before a revision
                # is applied; broad provenance alone is never used to guess it.
                "related": linked_elements(matrix, row["stage"], row["element"]),
                #: 이 항목을 고치면 따라 고쳐야 할 하류 항목들(간접 포함).
                "affects": affected,
                #: 그 항목들이 있는 스테이지, 파이프라인 순서로.
                "affected_stages": [
                    s for s in _STAGE_ORDER
                    if s in {a.partition(":")[0] for a in affected}
                ],
            }
        )
    order = {stage: i for i, stage in enumerate(_STAGE_ORDER)}
    plan.sort(key=lambda item: (order.get(item["stage"], len(order)), item["element"]))
    return plan


def affected_by_element(rtm: dict, stage: str, element: str) -> list[str]:
    """이 **산출물 항목**을 고치면 따라 고쳐야 할 하류 항목들.

    행 이름(class_diagram:Order)을 참조 이름(class:Order)으로 바꿔 전이 추적에 태운다.
    참조 대상이 아닌 항목(api_spec:createOrder 등)은 하류가 없으므로 빈 목록이다 —
    고쳐도 따라 바뀔 것이 없다는 뜻이고, 그게 정답이다.
    """
    alias = _ROW_IS_ALSO_A_REF.get(stage)
    if not alias:
        return []
    return transitively_impacted(rtm, alias, element)


def linked_elements(rtm: dict, stage: str, element: str) -> list[str]:
    """Return elements joined by an exact cross-artifact contract link.

    This is intentionally different from ``affected_by_element``.  The latter
    follows broad provenance forward (for example class → API schema); this
    function is symmetric but returns only links proven by a concrete sequence
    call and API Control binding.  A missing link means "do not propagate", not
    "guess a likely neighbour".
    """
    ref = f"{stage}:{element}"
    linked = {
        str(link["to"])
        if link["from"] == ref
        else str(link["from"])
        for link in exact_contract_links(rtm, stage, element)
    }
    linked.discard("")
    linked.discard(ref)
    return sorted(linked)


def exact_contract_links(
    rtm: dict,
    stage: str,
    element: str,
    *,
    direction: str = "both",
    relations: set[str] | None = None,
) -> list[dict[str, str]]:
    """Return exact RTM contract edges without losing their meaning.

    ``linked_elements`` is intentionally convenient and symmetric, but that
    shape is not sufficient for a revision planner: an API ``implements`` a
    sequence call while it ``binds`` a class, and only the latter may justify a
    reverse class edit.  This pure helper preserves the original ``from``,
    ``to`` and ``relation`` values and lets callers ask for incoming, outgoing
    or both directions.  It never infers a missing edge from names or order.
    """
    if direction not in {"incoming", "outgoing", "both"}:
        raise ValueError("direction must be incoming, outgoing, or both")
    ref = f"{stage}:{element}"
    wanted_relations = set(relations) if relations is not None else None
    result: list[dict[str, str]] = []
    for candidate in rtm.get("links", []) or []:
        if not isinstance(candidate, dict):
            continue
        source = str(candidate.get("from") or "")
        target = str(candidate.get("to") or "")
        relation = str(candidate.get("relation") or "")
        if not source or not target or not relation:
            continue
        if direction == "outgoing" and source != ref:
            continue
        if direction == "incoming" and target != ref:
            continue
        if direction == "both" and source != ref and target != ref:
            continue
        if wanted_relations is not None and relation not in wanted_relations:
            continue
        result.append({"from": source, "to": target, "relation": relation})
    return sorted(result, key=lambda link: (link["from"], link["to"], link["relation"]))


def impacted_by(rtm: dict, kind: str, name: str) -> list[str]:
    """이 상류 항목이 **직접** 가리키는 설계 항목들 (한 단계).

    kind는 "class" 또는 "use_case". 반환은 "{stage}:{element}" 목록이다.
    간접 영향까지 보려면 transitively_impacted를 쓴다.
    """
    return rtm["impact"].get(f"{kind}:{name}", [])


def transitively_impacted(rtm: dict, kind: str, name: str) -> list[str]:
    """이 상류 항목이 바뀌면 다시 봐야 할 **전부** — 간접 영향까지 따라간다.

    유스케이스를 고치면 클래스가 바뀌고, 그 클래스를 재료로 만든 API·ERD·배포도 바뀐다.
    `impacted_by`는 첫 화살표만 보므로 두 번째 이후를 놓친다.

    `impact`를 인접 리스트로 보고 너비 우선으로 훑는다. 방문 표시(seen)가 있으므로
    참조가 순환해도 멈춘다. 캐스케이드가 여기서 출발한다.
    """
    seen: set[str] = set()
    queue: list[str] = [f"{kind}:{name}"]
    while queue:
        for target in rtm["impact"].get(queue.pop(), []):
            if target in seen:
                continue
            seen.add(target)
            stage, _, element = target.partition(":")
            alias = _ROW_IS_ALSO_A_REF.get(stage)
            if alias:
                # 이 행은 다른 산출물이 참조하는 대상이기도 하다 — 거기서 계속 따라간다.
                queue.append(f"{alias}:{element}")
    return sorted(seen)


def impacted_stages(rtm: dict, kind: str, name: str) -> list[str]:
    """영향받는 스테이지 이름만, 파이프라인 순서로. 되감기 대상을 고르는 데 쓴다.

    **간접 영향까지 센다** — 되감기는 그 지점부터 뒤를 전부 다시 만드니, 가장 앞선
    영향 지점을 알아야 한다. 하나라도 놓치면 낡은 산출물이 남는다.
    """
    seen = {t.partition(":")[0] for t in transitively_impacted(rtm, kind, name)}
    return [s for s in _STAGE_ORDER if s in seen]


def render_design_rtm_md(rtm: dict, title: str = "") -> str:
    """추적표를 markdown으로. 사람이 한 장에서 보게 하는 용도."""
    s = rtm["summary"]
    lines: list[str] = []
    lines.append(f"# 설계 추적 매트릭스{f' — {title}' if title else ''}")
    lines.append("")
    lines.append(
        f"- 추적됨: **{s['traced']}/{s['total']}** ({s['trace_ratio']:.0%}) · "
        f"orphan {s['orphan']}"
    )
    if s["unknown_ref_count"]:
        lines.append(f"- **환각/이름불일치 참조**: {s['unknown_ref_count']}건")
    lines.append("")

    lines.append("## 설계 항목 → 출처")
    lines.append("")
    lines.append("| 스테이지 | 항목 | 유스케이스 | 클래스 | 상태 |")
    lines.append("|---|---|---|---|---|")
    for row in rtm["rows"]:
        ucs = ", ".join(row["sources"].get("use_case", [])) or "—"
        classes = ", ".join(row["sources"].get("class", [])) or "—"
        mark = "⚠ orphan" if row["status"] == "orphan" else "traced"
        lines.append(
            f"| {row['stage']} | {_clip(row['element'])} | {ucs} | {classes} | {mark} |"
        )
    lines.append("")

    if rtm["unknown_refs"]:
        lines.append("## 존재하지 않는 것을 가리키는 참조")
        lines.append("")
        lines.append("| 스테이지 | 항목 | 종류 | 참조 |")
        lines.append("|---|---|---|---|")
        for ref in rtm["unknown_refs"]:
            lines.append(
                f"| {ref['stage']} | {_clip(ref['element'])} | {ref['kind']} | "
                f"**{ref['ref']}** |"
            )
        lines.append("")

    lines.append("## 영향 분석 (역방향)")
    lines.append("")
    lines.append("| 이것이 바뀌면 | 직접 | 간접까지 |")
    lines.append("|---|---|---|")
    for source in rtm["impact"]:
        kind, _, name = source.partition(":")
        direct = impacted_by(rtm, kind, name)
        extra = sorted(set(transitively_impacted(rtm, kind, name)) - set(direct))
        lines.append(
            f"| {source} | {', '.join(_clip(x, 32) for x in direct)} | "
            f"{('+ ' + ', '.join(_clip(x, 32) for x in extra)) if extra else '—'} |"
        )
    lines.append("")
    return "\n".join(lines) + "\n"


def _clip(text: str, n: int = 60) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"
