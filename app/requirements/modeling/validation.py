"""의미 검증자 — 산출물만 보고 규칙 위반을 판정한다.

## 왜 생성기와 떼어 놓는가

검증은 **컨텍스트 이전이 거의 필요 없다.** 산출물 하나와 규칙 목록만 있으면 판정할 수
있고, 생성 프롬프트·사용자 피드백·재생성 이력은 몰라도 된다. 그래서 이 층을 떼어 내는
비용이 낮고, 떼어 내면 얻는 것이 둘이다.

  - **생성기의 자기 채점이 아니다.** 같은 프롬프트 계보에서 나온 판정은 자기가 방금 쓴
    문장을 옹호하는 쪽으로 기운다. 검증자에게는 결과물만 준다(black-box).
  - **규칙이 한 곳에서 온다.** 무엇을 볼지는 `knowledge/rules.py`가 정하고, 단계는 어느
    단계인지만 말한다. 예전에는 step3·step4가 거의 같은 25줄을 각자 들고 있었다.

## 두 가지 실패 모드를 여기서 막는다

  1. **근거 없는 지적**(검증자의 환각). 지식베이스에 없는 규칙을 인용한 판정은 버린다
     (`steps/grounding.py`). 전부 버려지면 "결함 없음"이 아니라 "판정을 얻지 못함"이다.
  2. **early victory**(대충 보고 통과). 규칙마다 판정을 요구하므로(`schemas.RuleVerdict`)
     빠진 규칙이 응답에서 드러난다. 빠진 것은 저하로 남긴다 — 그 실행의 `issues`는
     결함의 전부가 아니라 하한이다.

`enable_semantic_validator=False`면 부르지 않는다. 그때 상태는 `disabled`이고, 이는
"깨끗하다"가 아니다.
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field

from langchain_core.messages import HumanMessage, SystemMessage

from app.requirements import prompts
from app.requirements.config import settings
from app.requirements.knowledge import rules
from app.requirements.modeling import grounding
from app.requirements.runtime import telemetry
from app.requirements.runtime.structured_llm import invoke_structured
from app.requirements.schemas import Critique, RuleVerdict

#: 검증을 실제로 거쳤는가. **"결함 없음"과 "확인 못 함"을 같은 값으로 두지 않기 위해 있다.**
OK = "ok"                    # 판정을 받았다(결함이 있든 없든)
DISABLED = "disabled"        # 설정으로 껐다
FAILED = "failed"            # 호출이 실패했다
UNGROUNDED = "ungrounded"    # 불렀는데 근거 있는 판정이 하나도 없었다
PENDING = "pending"          # 아직 묻지 않았다(산출물 조립 시점의 초기값)

#: 리포트에서 "이 산출물은 의미 검증을 거치지 못했다"로 묶어야 하는 상태들.
#: 원인은 다르지만 결과는 같다 — 남아 있는 issues가 결함의 전부라고 말할 수 없다.
UNVALIDATED = (FAILED, UNGROUNDED)


@dataclass(frozen=True)
class Review:
    """한 산출물에 대한 판정 결과."""

    findings: list[str] = field(default_factory=list)
    status: str = OK
    #: 검증자가 판정하지 않고 넘어간 규칙 id들(early victory의 흔적).
    unexamined: tuple[str, ...] = ()


def _ask(
    stage: str,
    artifact: dict[str, object],
    only: str | Sequence[str] | None = None,
) -> Critique:
    """검증자에게 한 번 묻는다. `only`면 그 규칙 하나만 묻는다."""
    return invoke_structured(
        Critique,
        [
            SystemMessage(content=prompts.validator_system_for(stage, only)),
            HumanMessage(
                content=f"[ARTIFACT UNDER REVIEW]\n{json.dumps(artifact, ensure_ascii=False)}"
            ),
        ],
    )


def _collect_ballots(
    stage: str,
    artifact: dict[str, object],
    *,
    source: str,
    subject: str | None,
    rule_ids: tuple[str, ...] | None = None,
) -> list[Critique]:
    """표를 모은다 — `settings.validator_votes`장. 하나도 못 받으면 빈 목록.

    `settings.validator_per_rule`이면 **규칙마다 따로 묻고** 그 답들을 한 장으로 합친다.
    한 번에 6개 규칙을 판정하게 하면 판정이 흔들린다는 측정(흔들림 90%,
    `docs/requirements-agent-improvements.md` §7) 때문에 생긴 갈래다 — 과제를 쪼개면
    확률이 0.5에서 멀어지는지 보려는 것이다. 호출 수는 규칙 수만큼 늘고 응답은 짧아진다.

    한 규칙의 호출이 실패해도 나머지 규칙은 살린다(형제를 버리지 않는다). 그러면 그 표에는
    그 규칙의 판정이 없고, 그건 `unexamined`로 드러난다.
    """
    per_rule = settings.validator_per_rule
    selected_ids = [
        rule.id
        for rule in rules.judged_by(stage, rules.JUDGED_VALIDATOR)
        if rule_ids is None or rule.id in rule_ids
    ]

    ballots: list[Critique] = []
    for _ in range(max(1, settings.validator_votes)):
        try:
            if not per_rule:
                ballots.append(_ask(stage, artifact, only=rule_ids))
                continue
            verdicts: list[RuleVerdict] = []
            for rule_id in selected_ids:
                try:
                    verdicts.extend(_ask(stage, artifact, only=rule_id).verdicts)
                except Exception as exc:  # noqa: BLE001 - 규칙 하나 실패로 표를 버리지 않는다
                    telemetry.record_degradation(
                        f"{source}.per_rule", f"{rule_id}: {type(exc).__name__}: {exc}",
                        subject=subject,
                    )
            if verdicts:
                ballots.append(Critique(verdicts=verdicts))
        except Exception as exc:  # noqa: BLE001 - 검증 실패는 치명적이지 않다
            telemetry.record_degradation(source, f"{type(exc).__name__}: {exc}", subject=subject)
    return ballots


def _tally(
    ballots: list[Critique], allowed: set[str] | None = None
) -> tuple[list[RuleVerdict], set[str]]:
    """표를 모아 **과반으로 위반을 확정한다**. `(확정된 위반, 판정된 규칙 id)`.

    왜 과반인가: 같은 명세를 5번 물었을 때 판정 24건 중 4건만 안정적이었다(흔들림 83%,
    2026-07-26 측정). 한 번 물어 얻은 판정 위에 쌓은 수는 그 위에 쌓았다는 사실만으로
    무의미해진다 — 반성 루프도, 실행 비교도.

    지시문은 **과반 표 중 첫 것**을 쓴다. 표마다 문구가 다른데, 여러 개를 합치면 같은
    결함이 여러 지적으로 보인다.
    """
    votes: dict[str, list[RuleVerdict]] = {}
    examined: set[str] = set()
    for ballot in ballots:
        for verdict in ballot.verdicts:
            if (
                allowed is not None
                and verdict.rule_id not in allowed
                and verdict.rule_id in rules.known_ids()
            ):
                continue
            examined.add(verdict.rule_id)
            if verdict.violated:
                votes.setdefault(verdict.rule_id, []).append(verdict)
    threshold = len(ballots) // 2 + 1
    return [v[0] for v in votes.values() if len(v) >= threshold], examined


def review(
    stage: str,
    artifact: dict[str, object],
    *,
    prefix: str,
    source: str,
    subject: str | None = None,
    rule_ids: tuple[str, ...] | None = None,
    confirm_violations: bool = False,
) -> Review:
    """`stage`의 규칙으로 `artifact`를 판정한다.

    artifact는 **산출물뿐이어야 한다** — 생성 프롬프트나 사용자 피드백을 섞으면 검증자가
    "무엇을 지시받았는지"를 알게 되고, 그러면 지시를 따랐는지를 보게 된다. 우리가 물어야
    하는 것은 결과물이 규칙을 지켰는지다.

    prefix는 지적 문구의 머리표(`[semantic]`·`[rel]`), source는 저하 기록의 이름이다.
    `confirm_violations`이면 첫 판정에서 위반한 규칙만 한 번 더 독립적으로 묻고, 두 판정이
    모두 위반이라고 한 규칙만 지적으로 남긴다. 깨끗한 첫 판정에는 추가 호출하지 않는다.
    """
    if not settings.enable_semantic_validator:
        return Review(status=DISABLED)

    expected = [
        rule.id
        for rule in rules.judged_by(stage, rules.JUDGED_VALIDATOR)
        if rule_ids is None or rule.id in rule_ids
    ]
    if not expected:
        # 이 단계에 의미 검증자가 볼 규칙이 없다. 호출은 낭비이고, 통과라고 말하면 거짓이다.
        return Review(status=DISABLED)

    ballots = _collect_ballots(
        stage,
        artifact,
        source=source,
        subject=subject,
        rule_ids=rule_ids,
    )
    if not ballots:
        return Review(status=FAILED)

    violated, examined = _tally(ballots, set(expected))
    findings, dropped = grounding.grounded_findings(
        violated, prefix=prefix, source=source, subject=subject
    )

    # 판정을 받지 못한 규칙. 규칙 id를 안 세고 개수만 보면, 엉뚱한 규칙 하나를 대신 판정한
    # 응답이 "다 봤다"로 통과한다.
    unexamined = tuple(rule_id for rule_id in expected if rule_id not in examined)
    if unexamined:
        telemetry.record_degradation(
            f"{source}.unexamined_rules",
            f"검증자가 판정하지 않은 규칙 {len(unexamined)}건: {list(unexamined)}",
            subject=subject,
        )

    # 아무 규칙도 판정하지 않았거나, 낸 지적이 전부 근거 없어 버려졌다 → 판정을 못 얻었다.
    if not examined or (dropped and not findings):
        return Review(status=UNGROUNDED, unexamined=unexamined)
    initial = Review(findings=findings, status=OK, unexamined=unexamined)
    if not confirm_violations or not findings:
        return initial

    flagged_ids = tuple(dict.fromkeys(
        rule_id
        for finding in findings
        if (rule_id := rules.rule_of(finding)) is not None
    ))
    confirmation = review(
        stage,
        artifact,
        prefix=prefix,
        source=source,
        subject=subject,
        rule_ids=flagged_ids,
    )
    combined_unexamined = tuple(dict.fromkeys(
        (*initial.unexamined, *confirmation.unexamined)
    ))
    if confirmation.status != OK:
        # 확인하지 못한 최초 지적을 결함으로 확정하지 않되, 실패를 깨끗함으로 바꾸지도 않는다.
        return Review(
            status=confirmation.status,
            unexamined=tuple(dict.fromkeys((*combined_unexamined, *flagged_ids))),
        )

    confirmed_ids = {
        rule_id
        for finding in confirmation.findings
        if (rule_id := rules.rule_of(finding)) is not None
    }
    return Review(
        findings=[
            finding for finding in initial.findings
            if rules.rule_of(finding) in confirmed_ids
        ],
        status=OK,
        unexamined=combined_unexamined,
    )
