"""Modeling 검증자가 댄 인용을 지식베이스와 대조한다.

## 왜 필요한가

의미 검증자는 LLM이다. 그것도 환각한다 — 다만 환각의 모양이 다르다. 생성기의 환각은
"없는 기능을 만든다"이고, **검증자의 환각은 "없는 기준으로 지적한다"**이다. 후자가 더
고약하다. 지적은 그 자체로 권위가 있어 보이고, 반성 루프는 그 지적을 고치려고 예산을
태우며, 산출물에는 "결함 1건"으로 남는다.

구조화 출력이 `rule_id`를 요구하니(`schemas.RuleVerdict`) 대조할 수 있다. 지식베이스에
없는 id를 댄 지적은 버리고, 버렸다는 사실을 저하로 남긴다. `app/cloudkb`가 "도구
출력에 없는 값을 답에 쓰지 않는다"로 세운 규율과 같은 것이다.

**대조는 id 존재 여부까지만 한다.** 그 규칙을 실제로 어겼는지는 판단하지 않는다 — 그건
검증자의 일이고, 여기서 다시 판단하면 판정자가 둘이 된다.

`modeling/validation.py`만 이 모듈을 사용한다. 생성 stage와 분리된 검증자 도구다.
"""
from __future__ import annotations

from collections.abc import Sequence

from app.requirements.knowledge import rules
from app.requirements.runtime import telemetry
from app.requirements.schemas import RuleVerdict


def grounded_findings(
    findings: Sequence[RuleVerdict],
    *,
    prefix: str,
    source: str,
    subject: str | None = None,
) -> tuple[list[str], list[str]]:
    """`(쓸 수 있는 지적, 버린 규칙 id)`. 입력은 **위반이라고 판정된** verdict들이다.

    지적 문구는 `[prefix] 지시 [규칙id · 인용 · 우리 판단]` 형태다 — 꼬리표가 근거를
    들고 가고, 출처가 짐작인 규칙은 그 사실까지 함께 간다(`rules.Rule.tag`).
    """
    known = rules.known_ids()
    usable: list[str] = []
    dropped: list[str] = []
    for finding in findings:
        if finding.rule_id not in known:
            dropped.append(finding.rule_id)
            continue
        usable.append(f"[{prefix}] {finding.directive} {rules.tag_of(finding.rule_id)}")
    if dropped:
        # 검증자가 규칙 목록 밖으로 나갔다. 프롬프트가 "규칙 없는 지적은 하지 말라"고
        # 적어 두었는데도 나간 것이므로, 프롬프트를 고칠 근거로 세어 둔다.
        telemetry.record_degradation(
            f"{source}.ungrounded_rule",
            f"지식베이스에 없는 규칙을 인용한 지적 {len(dropped)}건 버림: {dropped}",
            subject=subject,
        )
    return usable, dropped
