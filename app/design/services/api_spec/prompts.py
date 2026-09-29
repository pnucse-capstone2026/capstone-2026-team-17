"""API LLM에는 HTTP 설계만 맡기고 실행 연결은 코드가 채우게 한다."""

from __future__ import annotations

import json

from app.design.schemas.class_model import BCEModel
from app.design.services.api_spec.normalization import interaction_context

API_SPEC_EXTRACTION_SYSTEM_PROMPT = """
Design the HTTP surface for the supplied finite interaction candidates.
Return one endpoint for each distinct interaction the application exposes and
copy its interactionId exactly. Decide only the HTTP path, method, short summary,
and useful response status descriptions.

- Use resource-oriented absolute paths and standard HTTP method semantics.
- Fill path, method, summary, and responses for every endpoint; do not rely on defaults.
- Choose a concrete resource path such as /registrations or /offerings/{offeringId};
  never use the API root path by itself.
- Every method and path pair must be unique because duplicate pairs overwrite each other.
- Each candidate supplies allowedPathParameters. Use path placeholders only from that
  candidate's exact list. Never use a nested field or invent another placeholder; when
  the list is empty, use no path placeholder.
- Include the successful status and failures stated by the use-case extensions.
- Each candidate's publicReturnType is the Boundary method's approved public
  result. A non-void public result needs a body-bearing successful status (not
  204); use 204 only when publicReturnType is void.
- Do not return operation IDs, parameters, schemas, Control bindings, argument sources,
  result names, class traces, or use-case traces. The application derives all of them
  from the selected interaction.
- Do not invent an interactionId or add an endpoint without a supplied candidate.

Return only the structured response.
""".strip()

API_SPEC_REVISION_SYSTEM_PROMPT = """
Revise only the HTTP contract requested by the feedback. Keep interactionId values
grounded in the supplied candidates and return the full minimal API proposal.
Return only path, method, summary, and response statuses in addition to interactionId.
The application derives operation IDs, parameters, schemas, Control bindings, argument
mappings, outcomes, and trace fields from the accepted class collaboration.
Each candidate supplies allowedPathParameters. Use path placeholders only from that
candidate's exact list; an empty list means that no path placeholder is allowed.
Each candidate's publicReturnType is authoritative: a non-void result must use a
body-bearing successful status, while 204 is reserved for void results.
""".strip()


def _api_use_case_context(scenario_text: str) -> object:
    """전체 요구사항 상태에서 HTTP 판단에 쓰는 유스케이스 내용만 남긴다.

    API 경로·메서드·상태 코드를 고르는 데 actor 설명, 추적 ID, repair 상태와 UML 관계는
    필요하지 않다. JSON이 아닌 이전 호출 형식은 그대로 전달해 입력 호환성을 유지한다.
    """

    try:
        source = json.loads(scenario_text)
    except (TypeError, json.JSONDecodeError):
        return scenario_text
    if not isinstance(source, dict):
        return source
    use_cases = {
        str(item.get("id")): item
        for item in source.get("use_cases", [])
        if isinstance(item, dict) and item.get("id")
    }
    compact = []
    for spec in source.get("use_case_specs", []):
        if not isinstance(spec, dict):
            continue
        use_case_id = str(spec.get("use_case_id") or "")
        use_case = use_cases.get(use_case_id, {})
        compact.append(
            {
                "id": use_case_id,
                "name": spec.get("name") or use_case.get("name") or "",
                "goal": use_case.get("goal") or "",
                "trigger": spec.get("trigger") or "",
                "steps": [
                    step.get("sentence")
                    for step in spec.get("main_scenario", [])
                    if isinstance(step, dict) and step.get("sentence")
                ],
                "extensions": [
                    {
                        "condition": extension.get("condition") or "",
                        "outcome": extension.get("outcome") or "",
                    }
                    for extension in spec.get("extensions", [])
                    if isinstance(extension, dict)
                ],
            }
        )
    return compact or scenario_text


def proposal_messages(
    scenario_text: str,
    bce_model: BCEModel,
) -> list[dict[str, str]]:
    """유스케이스와 유한 interaction 후보만 API 제안 입력으로 만든다."""

    payload = {
        "useCases": _api_use_case_context(scenario_text),
        "interactionCandidates": interaction_context(bce_model),
    }
    return [
        {"role": "system", "content": API_SPEC_EXTRACTION_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        },
    ]


def revision_context(
    scenario_text: str,
    bce_model: BCEModel,
    *,
    interaction_ids: set[str] | None = None,
    reserved_routes: set[tuple[str, str]] | None = None,
) -> str:
    """수정 대상 interaction과 그 UC만 LLM 입력에 포함한다."""

    candidates = interaction_context(bce_model)
    if interaction_ids is not None:
        candidates = [
            item for item in candidates if item["interactionId"] in interaction_ids
        ]
    use_case_ids = {
        str(use_case_id)
        for candidate in candidates
        for use_case_id in candidate.get("useCaseIds") or []
    }
    use_cases = _api_use_case_context(scenario_text)
    if interaction_ids is not None and isinstance(use_cases, list):
        use_cases = [
            item
            for item in use_cases
            if isinstance(item, dict) and str(item.get("id") or "") in use_case_ids
        ]
    payload = {
        "useCases": use_cases,
        "interactionCandidates": candidates,
    }
    if reserved_routes:
        payload["reservedRoutes"] = [
            {"method": method, "path": path}
            for method, path in sorted(reserved_routes)
        ]
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
