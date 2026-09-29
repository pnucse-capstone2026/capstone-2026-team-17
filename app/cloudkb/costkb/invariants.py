"""costkb 산출물의 레코드 간 데이터 일관성 검사.

여기 있는 검사는 대부분 `report`다. **원본을 고쳐 쓰면 무엇이 원본이었는지 잃기
때문**이다 — 상류가 무엇을 말했는지는 계속 남아야 다음 사람이 판단할 수 있다.
대신 몇 건인지 밝혀서 상류가 나빠지는지 좋아지는지 빌드마다 보이게 한다.

**"원본 보존"이 "그 값으로 판단"과 같은 말은 아니다.** 상류 버그로 GCP·Azure
메모리가 2.4% 낮게 적혀 있는데, 우리는 원본을 그대로 두면서도 **판단은 보정값으로**
한다(`dataset.actual_memory`). 예전에는 라이브 MCP와 답을 맞추려고 버그값으로
필터링했는데 그건 배포기의 이유였고, 그 탓에 "16 GiB 이상"에서 실제로는 만족하는
3,765건이 조용히 빠졌다. 우리는 가이드라인 지식베이스다.

얼개는 `kbcommon/invariants.py` 참고.
"""

from __future__ import annotations

import collections
from collections.abc import Iterable

from app.cloudkb.kbcommon.invariants import (
    Invariant,
    Violation,
    accelerator_fields_agree,
    no_negative_measurements,
)


def _disk_size_is_ambiguous(dataset: dict) -> Iterable[Violation]:
    """`diskSizeGB == 0`이 몇 건인가 — 사실인지 미기입인지 **가릴 수 없는** 값이다.

    0은 "로컬 디스크 없음"이라는 사실일 수도, 드라이버가 안 채운 것일 수도 있다.
    Azure에서 이름상 로컬 디스크가 확실히 있는 v6 계열(`Standard_E48ads_v6` 등)
    5,225건이 0으로 오므로 사실이라고 단정할 수 없고, 반대로 0이 맞는 스펙도 있어
    미기입이라고 단정할 수도 없다.

    미러는 애매한 값을 다시 쓰지 않는다. 대신 **몇 건인지 말한다** — 소비자가
    "디스크 0GB"를 사실로 읽지 않도록.
    """
    by_provider: collections.Counter = collections.Counter()
    for record in dataset.get("specs") or []:
        if isinstance(record, dict) and record.get("diskSizeGB") == 0:
            by_provider[record.get("provider")] += 1
    for provider, count in by_provider.most_common():
        yield Violation(
            where=str(provider),
            detail=f"diskSizeGB=0이 {count:,}건 (로컬 디스크 없음인지 미기입인지 불명)",
        )


INVARIANTS = (
    Invariant(
        name="accelerator-fields-agree",
        question="가속기 종류가 적힌 스펙에 개수도 적혀 있는가?",
        severity="report",
        check=accelerator_fields_agree("specs"),
    ),
    Invariant(
        name="no-negative-measurements",
        question="크기·개수 필드에 음수가 남아 있지 않은가?",
        # 이건 우리 책임이다 — 상류의 -1을 null로 옮기는 건 파서의 일이다.
        severity="error",
        check=no_negative_measurements(
            "specs", "diskSizeGB", "acceleratorMemoryGB", "acceleratorCount",
            "memGiB", "vCPU", "hourlyUSD",
        ),
    ),
    Invariant(
        name="disk-size-zero-is-ambiguous",
        question="디스크 크기 0이 몇 건인가? (사실/미기입을 가릴 수 없다)",
        severity="report",
        check=_disk_size_is_ambiguous,
    ),
)
