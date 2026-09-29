"""Stage-owned design checks used by design generation, review, and hydration."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from app.design.graphs.subgraphs import _deployment_model_findings
from app.design.knowledge.detectors import (
    Finding,
    api_spec_validation_report,
    erd_validation_report,
)
from app.design.services.class_diagram.validation.diagram import (
    class_diagram_validation_report,
)
from app.design.services.class_diagram.public_contract_review import semantic_evidence_for_readiness
from app.design.services.class_diagram.scenario import ScenarioIndex, build_scenario_index
from app.design.services.sequence_diagram.validation import (
    validate_sequence_model as _validate_sequence_model,
)
from app.validation import ValidationReport

DESIGN_READINESS_SCHEMA = "easydep-design-readiness/v1alpha1"


def validate_class_model(model: dict[str, Any], state: dict[str, Any]) -> ValidationReport:
    """클래스 다이어그램의 전체 의미 규칙을 typed 보고서로 반환한다."""
    report = class_diagram_validation_report(model, state)
    if isinstance(state, ScenarioIndex):
        return report
    if report.errors:
        return report
    scenario = state.get("usecase_spec") or {}
    if not isinstance(scenario, Mapping):
        return report
    index = build_scenario_index({
        **scenario,
        "relationships": state.get("relationships") or scenario.get("relationships") or {},
    })
    semantic = semantic_evidence_for_readiness(model, state, index)
    if not semantic:
        return report
    return ValidationReport(
        status="needs_input" if all(item.requires_user_input for item in semantic) else "findings",
        findings=(*report.findings, *semantic), errors=report.errors,
        checked_rule_ids=(*report.checked_rule_ids, "class.public-contract-semantic"),
    )


def validate_sequence_model(model: dict[str, Any], state: dict[str, Any]) -> ValidationReport:
    """결정론적 시퀀스 투영의 저장·참조·버전 검사를 반환한다."""
    return _validate_sequence_model(model or {}, state or {})


def validate_deployment_model(
    model: dict[str, Any], state: dict[str, Any]
) -> ValidationReport:
    """WorkloadGraph 정규화 뒤 남은 문제를 설계 준비 상태에 포함한다."""

    findings = tuple(_deployment_model_findings(model or {}, state or {}))
    return ValidationReport(
        status=(
            "clean"
            if not findings
            else "needs_input"
            if all(finding.requires_user_input for finding in findings)
            else "findings"
        ),
        findings=findings,
        checked_rule_ids=("deployment.workload-graph-valid",),
    )


def _finding_payload(finding: Finding) -> dict[str, Any]:
    """Keep display text while exposing typed validation evidence."""
    return {
        "ruleId": finding.rule_id,
        "finding": finding.as_issue(),
        "message": finding.message,
        "location": finding.location,
        "requiresUserInput": finding.requires_user_input,
        "origin": finding.origin,
    }


def _readiness_status(findings: list[Finding], validation_status: str | None = None) -> str:
    """Map typed validation evidence to the design hand-off vocabulary."""
    if validation_status in {"disabled", "error"}:
        return "BLOCKED"
    if not findings:
        return "READY"
    if all(finding.requires_user_input for finding in findings):
        return "NEEDS_INPUT"
    return "BLOCKED"


_CHECKED_STAGES: tuple[tuple[str, str, str, Callable[[dict, dict], ValidationReport]], ...] = (
    ("class_diagram", "extracted_bce_classes", "class_diagram_check", validate_class_model),
    (
        "sequence_diagram",
        "sequence_diagram_model",
        "sequence_diagram_check",
        validate_sequence_model,
    ),
    ("api_spec", "api_spec_model", "api_spec_check", api_spec_validation_report),
    ("erd", "erd_bce_classes", "erd_check", erd_validation_report),
    (
        "deployment_diagram",
        "deployment_diagram_model",
        "deployment_diagram_check",
        validate_deployment_model,
    ),
)


def design_readiness_report(
    state: Mapping[str, Any], stages: Iterable[str] | None = None
) -> dict[str, Any]:
    """Return unresolved deterministic findings in a transport-safe form."""
    selected = set(stages) if stages is not None else None
    reports: list[dict[str, Any]] = []
    for stage, model_key, _, check in _CHECKED_STAGES:
        if selected is not None and stage not in selected:
            continue
        model = state.get(model_key)
        if not isinstance(model, dict) or not model:
            continue
        checked = check(model, dict(state))
        validation_status = checked.status
        # ValidationReport stores the shared base finding. Stage validators add
        # presentation-specific ``as_issue`` subclasses, and a sequence finding is
        # therefore not an instance of the class-diagram presentation subclass
        # imported above. Cross that boundary through the public data shape instead
        # of asking Pydantic to reinterpret a sibling model instance.
        findings = [Finding.model_validate(finding.model_dump()) for finding in checked.findings]
        reports.append(
            {
                "stage": stage,
                "findings": [finding.as_issue() for finding in findings],
                "findingRecords": [_finding_payload(finding) for finding in findings],
                "status": _readiness_status(findings, validation_status),
            }
        )
    unresolved = [
        {"stage": report["stage"], "finding": finding}
        for report in reports
        for finding in report["findings"]
    ]
    status = "READY"
    if any(report["status"] == "BLOCKED" for report in reports):
        status = "BLOCKED"
    elif any(report["status"] == "NEEDS_INPUT" for report in reports):
        status = "NEEDS_INPUT"
    return {
        "schemaVersion": DESIGN_READINESS_SCHEMA,
        "status": status,
        "stages": reports,
        "findings": unresolved,
        "findingRecords": [
            {"stage": report["stage"], **finding}
            for report in reports
            for finding in report["findingRecords"]
        ],
    }


def rehydrated_check_state(
    state: dict[str, Any], stages: Iterable[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Rebuild visible checks only for the requested active design stage."""
    report = design_readiness_report(state, stages=stages)
    by_stage = {str(item["stage"]): item for item in report["stages"]}
    result: dict[str, dict[str, Any]] = {}
    for stage, model_key, check_key, _ in _CHECKED_STAGES:
        item = by_stage.get(stage)
        if item is None:
            continue
        findings = list(item["findings"])
        check = {
            "findings": findings,
            "repair_iters": 0,
            "stopped": "clean" if not findings else "checked_only",
        }
        if stage == "class_diagram":
            prior = state.get(check_key)
            evidence = prior.get("semanticEvidence") if isinstance(prior, Mapping) else None
            if evidence is not None:
                check["semanticEvidence"] = evidence
        result[check_key] = check
    return result
