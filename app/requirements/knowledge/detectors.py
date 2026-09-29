"""결정론 검출기 — 규칙 하나에 검출기 하나.

## 왜 규칙 옆으로 옮겼나

이 정규식들은 `step3_specifications.py` 안에 있었고, 각자의 근거는 그 옆 주석에만 있었다.
그래서 지적 문구가 `"step 2: UI 용어 ['screen'] — black-box 위반"`으로 나갔다. 어느 규칙을
어긴 것인지, 그 규칙이 책의 것인지 우리 것인지는 산출물에서 사라졌다.

검출기를 규칙에 묶으면 지적이 근거를 들고 나간다:

    step 2: UI 용어 ['screen'] — black-box 위반 [spec.black-box-no-ui-mechanics · p.209 (Reminder 7); p.91-92 · 우리 판단]

`우리 판단`이 붙는 이유는 그 단어 목록이 **책이 든 예시에서 우리가 일반화한 것**이라서다
(`rules.py`의 `spec.black-box-no-ui-mechanics` caveat). 이 사실이 전에는 코드 주석에만 있었다.

## 검출기의 사정거리

여기 있는 것은 **결정론으로 참인 것만**이다. 문자열이 있는지, 참조가 가리키는 스텝이
실제로 있는지. 의미 판정(숨은 분기·내부컴포넌트 누출·scope creep)은 검출기가 아니라
LLM 검증자가 하고, 그 규칙들은 `detector=None`으로 남아 있다.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterator

from app.requirements.knowledge import rules
from app.validation import CheckSpec, ValidationReport, run_checks
from app.validation import Finding as ValidationFinding


class Finding(ValidationFinding):
    """규칙 위반 하나. **어느 규칙인지를 들고 다닌다.**"""

    rule_id: str
    message: str
    #: 명세 안의 위치(`trigger`, `step 3`, `3a1`, 확장 라벨). 전체에 대한 지적이면 None.
    location: str | None = None

    def as_issue(self) -> str:
        """상태·리포트에 실리는 한 줄. 꼬리표로 근거가 함께 간다."""
        head = f"{self.location}: {self.message}" if self.location else self.message
        return f"{head} {rules.tag_of(self.rule_id)}"


# Cockburn: MSS/확장은 무분기 → 명시적 if/else만 걸러낸다(미묘한 분기는 LLM 몫).
_BRANCH = re.compile(r"\b(if|else)\b", re.IGNORECASE)
_CONTROL_TOKEN = re.compile(r"(success!|fail!)", re.IGNORECASE)
# ⚠ 이 목록은 **완전목록이 아니다.** 그가 예로 든 단어(p.209·p.91-92)로만 한정한다 —
# page/menu/form 등은 그의 예시에 없어 제외한다(form은 p.177에서 오히려 긍정적으로 등장).
# 나머지 내부컴포넌트 누출 판단은 임의 사전이 아니라 LLM 검증자에 위임한다.
# ``field`` is also ordinary domain language (a record field, a field of study). A bare
# occurrence therefore cannot prove a UI-mechanics violation. Explicit interaction terms stay
# deterministic; ambiguous wording remains the semantic reviewer's responsibility.
_UI_TERMS = ("screen", "button", "click", "clicks", "clicked", "tab")
_UI_PATTERNS = {w: re.compile(rf"\b{re.escape(w)}\b", re.IGNORECASE) for w in _UI_TERMS}


def ui_words(text: str) -> list[str]:
    """문장에 있는 Cockburn 예시 UI 용어(단어 경계 매칭). 공개 — 테스트·리포트가 쓴다."""
    return sorted(w for w, pat in _UI_PATTERNS.items() if pat.search(text))


def _locations(spec: dict) -> Iterator[tuple[str, str]]:
    """검사 대상 문장과 그 위치. trigger + MSS 스텝 + 확장 handling."""
    yield "trigger", spec.get("trigger", "")
    for step in spec.get("main_scenario", []):
        yield f"step {step['step_number']}", step["sentence"]
    for ext in spec.get("extensions", []):
        for handling in ext.get("handling_steps", []):
            yield handling["sub_step"], handling["sentence"]


def extension_refs(spec: dict, _context: object | None = None) -> list[Finding]:
    """확장의 분기·복귀 참조가 주 시나리오를 실제로 가리키는지."""
    rule_id = "spec.extension-reference-integrity"
    step_nums = {s["step_number"] for s in spec.get("main_scenario", [])}
    found: list[Finding] = []
    for ext in spec.get("extensions", []):
        label = ext.get("label") or "?"
        branch = ext.get("branch_step")
        if branch is None:
            found.append(
                Finding(
                    rule_id,
                    "branch_step is required; global extensions must be represented "
                    "by an anchored extension at a supported main-scenario step",
                    label,
                )
            )
        elif branch not in step_nums:
            found.append(Finding(rule_id, f"branch_step {branch} is not in the main scenario", label))
        outcome = ext.get("outcome")
        resume = ext.get("resume_at_step")
        if outcome == "resume":
            if resume is None:
                found.append(Finding(rule_id, "outcome=resume requires resume_at_step", label))
            elif resume not in step_nums:
                found.append(
                    Finding(rule_id, f"resume_at_step {resume} is not in the main scenario", label)
                )
        elif resume is not None:
            found.append(
                Finding(rule_id, f"outcome={outcome} must not set resume_at_step", label)
            )
    return found


def branch_words(spec: dict, _context: object | None = None) -> list[Finding]:
    """문장에 명시적 분기어(if/else)가 있는지."""
    return [
        Finding("spec.no-branching-in-a-step", "branch word (if/else); move the branch to an extension", loc)
        for loc, sentence in _locations(spec)
        if _BRANCH.search(sentence)
    ]


def control_tokens(spec: dict, _context: object | None = None) -> list[Finding]:
    """문장에 시나리오 종결 토큰(Success!/Fail!)이 산문으로 섞였는지."""
    return [
        Finding("spec.no-control-tokens-in-prose", "control token (Success!/Fail!); use the outcome field", loc)
        for loc, sentence in _locations(spec)
        if _CONTROL_TOKEN.search(sentence)
    ]


def ui_terms(spec: dict, _context: object | None = None) -> list[Finding]:
    """문장에 UI 용어(예시 단어)가 있는지."""
    found: list[Finding] = []
    for loc, sentence in _locations(spec):
        words = ui_words(sentence)
        if words:
            found.append(
                Finding("spec.black-box-no-ui-mechanics", f"UI terms {words} violate black-box wording", loc)
            )
    return found


def contract_fields(spec: dict, _context: object | None = None) -> list[Finding]:
    """계약의 성공보장이 비어 있는지.

    구조화 스키마에는 전제조건 필드가 항상 존재한다. 빈 목록은 누락이 아니라 이 유스케이스가
    가정할 근거 있는 사전 상태가 없다는 표현일 수 있으므로 결함으로 판정하지 않는다.
    """
    rule_id = "spec.contract-completeness"
    found: list[Finding] = []
    if not spec.get("success_guarantee"):
        found.append(Finding(rule_id, "success_guarantee is missing"))
    return found


def scenario_requirement_refs(spec: dict, _context: object | None = None) -> list[Finding]:
    """Keep scenario and guarantee coverage aligned with accepted FRs.

    일부 요구는 actor/system action이 아니라 저장·감사·상태 갱신 같은 사후 보장이다.
    그런 요구를 main scenario에 억지로 되풀이하지 않고 typed guarantee에서 추적한다.
    """
    rule_id = "spec.scenario-requirement-reference-integrity"
    accepted = set(spec.get("requirement_ids") or [])
    found: list[Finding] = []
    covered: set[str] = set()
    for step in spec.get("main_scenario", []):
        location = f"step {step.get('step_number', '?')}"
        for requirement_id in step.get("covered_req_ids", []) or []:
            covered.add(requirement_id)
            if requirement_id not in accepted:
                found.append(
                    Finding(
                        rule_id,
                        f"covered_req_id {requirement_id!r} is not an accepted functional requirement",
                        location,
                    )
                )
    for field_name in ("success_guarantee", "minimal_guarantee"):
        for index, guarantee in enumerate(spec.get(field_name, []) or [], 1):
            location = f"{field_name} {index}"
            for requirement_id in guarantee.get("covered_req_ids", []) or []:
                covered.add(requirement_id)
                if requirement_id not in accepted:
                    found.append(
                        Finding(
                            rule_id,
                            f"covered_req_id {requirement_id!r} is not an accepted functional requirement",
                            location,
                        )
                    )
    for requirement_id in spec.get("requirement_ids") or []:
        if requirement_id not in covered:
            found.append(
                Finding(
                    rule_id,
                    f"accepted functional requirement {requirement_id!r} is not covered by the main scenario or guarantees",
                )
            )
    return found


#: Runtime validation uses the direct registry below.  It follows the legacy
#: detector order exactly so existing text output remains stable.
SPEC_CHECKS: tuple[CheckSpec[dict, object | None], ...] = (
    CheckSpec("spec.extension-reference-integrity", extension_refs),
    CheckSpec("spec.no-branching-in-a-step", branch_words),
    CheckSpec("spec.no-control-tokens-in-prose", control_tokens),
    CheckSpec("spec.black-box-no-ui-mechanics", ui_terms),
    CheckSpec("spec.contract-completeness", contract_fields),
    CheckSpec("spec.scenario-requirement-reference-integrity", scenario_requirement_refs),
)

#: Legacy name-to-callable catalog retained for rule-audit and external callers.
SPEC_DETECTORS: dict[str, Callable[[dict], list[Finding]]] = {
    "extension_refs": extension_refs,
    "branch_words": branch_words,
    "control_tokens": control_tokens,
    "ui_terms": ui_terms,
    "contract_fields": contract_fields,
    "scenario_requirement_refs": scenario_requirement_refs,
}


def spec_findings(spec: dict) -> list[Finding]:
    """명세 하나에 대한 결정론 검증 전부.

    검출기 등록 순서를 따른다 — 참조 무결성 → 문장 정적 체크 → 계약 완결성.
    """
    report = spec_validation_report(spec)
    if report.errors:
        raise RuntimeError("; ".join(report.errors))
    return [Finding.model_validate(finding) for finding in report.findings]


def spec_validation_report(spec: dict) -> ValidationReport:
    """Return typed deterministic evidence while preserving ``spec_findings``."""
    return run_checks(SPEC_CHECKS, spec, None)
