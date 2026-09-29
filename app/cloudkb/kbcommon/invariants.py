"""레코드 **간** 정합성 검사 (지식베이스 패키지 공유).

## 왜 이게 있나

`Graph.validate()`·`CapacitySet.validate()`·각 `schema.json`은 전부 **레코드 하나의
형태**만 본다. "value가 숫자인가", "evidence가 아는 라벨인가" 같은 것들이다. 그래서
아래 데이터가 전부 스키마를 **통과했다**:

```
AWS::MediaLive::CloudWatchAlarmTemplate Period: default=0, min=10
AWS::NotificationsContacts::EmailContact EmailContact/Arn: read_only인데 required=True
```

레코드 하나씩 보면 둘 다 멀쩡하다. 모순은 **두 레코드를 나란히 놓아야** 보인다.
그런 검사를 놓을 자리가 없었다는 것이 감사의 결론이었다(kb-data-audit-2026-07-20 §1-(1)).

## 두 가지 심각도

검사를 전부 빌드 실패로 만들면 안 된다. 위반의 성격이 다르기 때문이다:

- **`error`** — 우리가 만든 모순이고 고칠 수 있다. 산출물을 쓰지 않는다.
  나쁜 데이터가 존재하지 않는 것이 존재하는 것보다 낫다(`artifact.py`의 원칙).
- **`report`** — 위반이지만 **우리 권한 밖**이다. 상류 데이터가 원래 그렇거나
  (costkb는 미러라 고쳐 쓰면 계약 위반이다), 고치려면 다른 설계 결정이 필요하다.
  산출물은 쓰되 **몇 건인지 반드시 말한다.**

`report`를 조용히 넘기면 안 되는 이유는 이 저장소에서 반복된 교훈이다 — 침묵은
"문제 없음"으로 읽힌다. 건수를 말해야 다음에 늘었는지 줄었는지 알 수 있다.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Violation:
    """데이터 일관성 규칙 위반 하나. ``where``는 사람이 원본을 찾을 수 있는 위치다."""

    where: str
    detail: str


@dataclass(frozen=True)
class Invariant:
    """검사 하나.

    `question`은 **사람 말로 무엇을 묻는지**다. 이름만 있으면 위반 보고를 읽고도
    무엇이 문제인지 모른다.
    """

    name: str
    question: str
    severity: str  # "error" | "report"
    check: Callable[[dict], Iterable[Violation]]

    def __post_init__(self) -> None:
        if self.severity not in ("error", "report"):
            raise ValueError(f"severity는 error 또는 report여야 합니다: {self.severity}")


@dataclass(frozen=True)
class Result:
    errors: list[tuple[Invariant, list[Violation]]]
    reports: list[tuple[Invariant, list[Violation]]]

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self, *, limit: int = 3) -> str:
        """위반을 사람이 읽는 여러 줄로. 없으면 빈 문자열."""
        lines: list[str] = []
        for label, group in (("✗", self.errors), ("·", self.reports)):
            for inv, violations in group:
                lines.append(f"{label} {inv.name}: {len(violations)}건 — {inv.question}")
                for v in violations[:limit]:
                    lines.append(f"    {v.where}: {v.detail}")
                if len(violations) > limit:
                    lines.append(f"    … 외 {len(violations) - limit}건")
        return "\n".join(lines)


def run(dataset: dict, invariants: Sequence[Invariant]) -> Result:
    """검사를 전부 돌린다. **예외를 던지지 않는다** — 판단은 호출자 몫이다."""
    errors: list[tuple[Invariant, list[Violation]]] = []
    reports: list[tuple[Invariant, list[Violation]]] = []
    for inv in invariants:
        violations = list(inv.check(dataset))
        if not violations:
            continue
        (errors if inv.severity == "error" else reports).append((inv, violations))
    return Result(errors=errors, reports=reports)


def announce(result: Result, label: str) -> None:
    """`report` 등급 위반을 알린다. **빌드마다 부르는 것이 계약이다.**

    조용히 넘기면 위반이 늘어도 아무도 모른다. 건수를 찍어야 다음 빌드와 비교된다.
    """
    if not result.reports:
        return
    total = sum(len(v) for _, v in result.reports)
    print(f"· {label}: 알려진 위반 {total}건 (산출물에는 그대로 실었습니다)", file=sys.stderr)
    print(result.summary(), file=sys.stderr)


# --- 여러 KB가 함께 쓰는 검사 -------------------------------------------------


def accelerator_fields_agree(record_key: str) -> Callable[[dict], Iterable[Violation]]:
    """가속기 필드끼리 말이 맞는가.

    `acceleratorType`이 있는데 `acceleratorCount`가 0이면 그 스펙에 가속기가 있다는
    건지 없다는 건지 알 수 없다. 234건 중 211건은 `acceleratorMemoryGB`도 0이라
    "종류만 적고 나머지는 안 채운" 레코드로 보인다.

    cb-tumblebug 덤프를 미러하는 쪽에서는 이 위반을 **고치면 안 된다.** 미러의
    계약은 상류를 그대로 옮기는 것이고, 우리가 개수를 지어내면 그 순간 미러가
    아니게 된다. 그래서 건수만 밝힌다.
    """

    def check(dataset: dict) -> Iterable[Violation]:
        for record in dataset.get(record_key) or []:
            if not isinstance(record, dict):
                continue
            if record.get("acceleratorType") and not record.get("acceleratorCount"):
                yield Violation(
                    where=f"{record.get('provider')} {record.get('specName')}",
                    detail=(
                        f"acceleratorType={record['acceleratorType']}인데 "
                        f"개수가 {record.get('acceleratorCount')}"
                    ),
                )

    return check


def no_negative_measurements(record_key: str, *fields: str) -> Callable[[dict], Iterable[Violation]]:
    """크기·개수 필드에 음수가 남아 있는가.

    음수는 측정값이 아니라 **모른다는 표시**다(상류가 `-1`을 쓴다). 그대로 실으면
    소비자가 계산에 넣어 버리므로 파서가 `null`로 바꾼다 — 이 검사는 그 정규화가
    빠진 경로가 생기면 울린다. "모른다"를 표현하는 방법이 필드마다 다르면
    소비자가 매번 다시 배워야 한다.
    """

    def check(dataset: dict) -> Iterable[Violation]:
        seen: dict[str, int] = {}
        for record in dataset.get(record_key) or []:
            if not isinstance(record, dict):
                continue
            for field in fields:
                value = record.get(field)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and value < 0:
                    seen[field] = seen.get(field, 0) + 1
        for field, count in sorted(seen.items()):
            yield Violation(
                where=field,
                detail=f"음수 값이 {count:,}건 남아 있습니다 (모름은 null로 적어야 한다)",
            )

    return check


def one_basis_per_evidence(*record_keys: str) -> Callable[[dict], Iterable[Violation]]:
    """한 근거 라벨의 성격이 하나로 일치하는가.

    라벨이 "어떻게 알았는가"를 뜻한다면 그 성격(원본 명시/짐작)은 라벨에서 정해져야
    한다. 갈린다는 것은 **그 라벨이 서로 다른 두 가지를 뭉뚱그리고 있다**는 뜻이므로,
    위반이 곧 "라벨을 쪼개라"는 신호다.

    실제로 이 검사가 신뢰도 시절에 두 라벨을 지목했고(`kcc-ref` 0.9/1.0,
    `azure-limits-doc` 0.7/0.9), 둘 다 쪼개는 것이 답이었다. `kcc-ref`는 그 탓에
    라벨 단위 검수가 **짐작까지 싸잡아 승인**하고 있었다.
    """

    def check(dataset: dict) -> Iterable[Violation]:
        seen: dict[str, set[str]] = {}
        for key in record_keys:
            for record in dataset.get(key) or []:
                if not isinstance(record, dict):
                    continue
                evidence, basis = record.get("evidence"), record.get("basis")
                if evidence is None or basis is None:
                    continue
                seen.setdefault(evidence, set()).add(basis)
        for evidence, values in sorted(seen.items()):
            if len(values) > 1:
                yield Violation(
                    where=evidence,
                    detail=f"근거의 성격이 {sorted(values)}로 갈립니다",
                )

    return check
