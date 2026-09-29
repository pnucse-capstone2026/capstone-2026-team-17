"""cb-tumblebug `spec_infos` 행 → costkb 레코드 투영 (순수 함수).

**왜 이 소스인가 — 미러여야 하기 때문이다.**
우리 에이전트의 런타임 경로는 cb-tumblebug MCP의 `recommend_vm_spec`이고, 코드를 추적하면:

    recommend_vm_spec (tb-mcp.py)
      → POST /recommendSpec              (server.go)
      → infra.RecommendSpec
      → resource.FilterSpecsByRange      (평범한 GORM WHERE/ORDER BY)
      → spec_infos                       (spec.go에 raw SQL로 하드코딩)

MCP는 컬럼을 **그대로 투영만** 한다(호출 시점 계산 0). 그래서 같은 테이블에서 빌드하면
오프라인 기준선과 라이브 경로가 같은 세계를 본다.

**AWS/Azure 공개 API에서 직접 빌드하면 오히려 불일치가 생긴다.** 아래 메모리 버그가 그
증거다 — 라이브 MCP는 버그 있는 값으로 필터링하므로, 우리만 "정확"하면 같은 질문에 다른
답을 내게 된다.

## 상위 버그를 일부러 재현한다

CB-Spider의 `ConvertMBToMiBInt64`는 `mb * 1000 / 1024`로 비율을 제곱이 아니라 한 번만
적용하고, 그것도 이미 MiB인 값에 적용한다. 순효과는 `참값 / 1.024`다.

덤프 73,083행 실측으로 확인한 영향 범위 (`×1.024 하면 정수가 되는 비율`):

    gcp   77.6%   azure 64.2%    ← 버그 영향
    aws    0.0%                    ← 정상

즉 **gcp/azure만** 보정 대상이다(코드 추적 결과와 실측이 일치).

**`memGiB`에 보정값을 넣는다.** 예전에는 미러값을 그대로 두고 `memGiBActual`에 보정값을
병기했다 — 라이브 MCP와 필터 결과를 맞추기 위해서였는데 **그건 배포기의 이유**다.
우리는 가이드라인 지식베이스이고, 값 하나를 두 칸에 나눠 두면 **어느 쪽으로 판단하느냐가
자리마다 갈린다.** 실제로 표시는 보정값, 필터는 버그값을 쓰다가 "16 GiB 이상"에서
실제로는 만족하는 3,765건이 조용히 빠졌다.

보정했다는 사실은 **데이터셋 메타데이터**(`_note`·`_corrections`)에 남긴다 — 값마다
칸을 늘리는 대신, 무엇을 어떻게 고쳤는지 한 곳에 적는다.
"""

from __future__ import annotations

import collections
from typing import Any

SOURCE_NOTE = (
    "A mirror of cb-tumblebug's spec_infos table (assets.dump.gz). The agent's live "
    "path, cb-tumblebug MCP recommend_vm_spec, reads **the same table**. The answers "
    "are not always identical, though — there are three places where we deliberately "
    "differ. (1) memGiB is the **corrected actual value** — an upstream CB-Spider bug "
    "records GCP and Azure 2.4% lower than reality, so it is restored by ×1.024 (see "
    "_corrections). (2) Candidates with an unknown price are excluded by default — "
    "Tumblebug only pushes them to the back with 999999 instead of dropping them, so "
    "answers diverge for providers with a high unpriced ratio (that count is stated "
    "in the answer). (3) Regions prefer an exact match, but where no such region "
    "exists we widen to a partial match and say in the answer that we widened. "
    "Prices are a snapshot, they drift over time, and they are not an actual bill. "
    "If you need live accuracy, use the cb-tumblebug MCP."
)

# spec_infos의 namespace. 실측상 73,083행 전부 'system'이라 사실상 no-op이지만,
# REST 핸들러가 model.SystemCommonNs를 하드코딩하므로 명시적으로 맞춰 둔다.
SYSTEM_NAMESPACE = "system"

# CB-Spider 버그(ConvertMBToMiBInt64)의 영향을 받는 프로바이더. 코드 추적 + 덤프 실측 일치.
_MEMORY_BUG_PROVIDERS = frozenset({"gcp", "azure"})
_MEMORY_BUG_FACTOR = 1.024

# 스키마 enum과 같아야 한다. 새 프로바이더가 오면 조용히 흘리지 않고 경고한다.
KNOWN_PROVIDERS = frozenset({"aws", "azure", "gcp"})


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    number = _num(value)
    return None if number is None else int(number)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def correct_memory(provider: str, mem_gib: float) -> float:
    """상위 버그로 낮게 기록된 메모리를 복원한다 (gcp/azure만).

    보정 대상이 아닌 프로바이더는 그대로 돌려준다 — 실측상 정상이기 때문이다.
    """
    if provider in _MEMORY_BUG_PROVIDERS:
        corrected = mem_gib * _MEMORY_BUG_FACTOR
        # 62.5 * 1.024 = 64.0 처럼 정수로 떨어지는 게 정상. 부동소수 잡음만 정리한다.
        rounded = round(corrected)
        return float(rounded) if abs(corrected - rounded) < 1e-6 else round(corrected, 4)
    return mem_gib


def _disk_size(raw) -> float | None:
    """`-1`은 크기가 아니라 **모른다는 표시**다 → null.

    디스크 크기가 음수일 수 없으므로 이 판정에는 애매함이 없다. 실측상 -1은
    37,466건이고 aws·gcp·tencent·ibm 드라이버가 이 관례를 쓴다(azure·kt·nhn은
    안 쓴다 — 즉 값이 아니라 드라이버별 표기다).

    **`0`은 건드리지 않는다.** 0은 "로컬 디스크 없음"이라는 사실일 수도, 미기입일
    수도 있어 가릴 수 없다 — Azure에서 이름상 로컬 디스크가 확실히 있는 v6 계열
    5,225건이 0으로 오는 반면(미기입), 0이 사실인 스펙도 존재한다. 미러가 애매한
    값을 다시 쓰면 그 순간 미러가 아니게 되므로 그대로 싣고 데이터 검사가 건수를 알린다.
    """
    value = _num(raw)
    return None if value is not None and value < 0 else value


def project_row(row: dict) -> dict | None:
    """spec_infos 행 하나를 costkb 레코드로. 쓸 수 없는 행은 None."""
    provider = _text(row.get("provider_name"))
    region = _text(row.get("region_name"))
    spec_name = _text(row.get("csp_spec_name"))
    vcpu = _int(row.get("v_cpu"))
    mem = _num(row.get("memory_gi_b"))

    if provider not in KNOWN_PROVIDERS:
        return None
    if not (region and spec_name) or not vcpu or vcpu < 1:
        return None
    if mem is None or mem <= 0:
        return None

    # Tumblebug의 정렬 SQL이 `CASE WHEN cost_per_hour > 0 ... ELSE 999999`이므로
    # 0도 '가격 미상'이지 무료가 아니다. 실측상 0이 6건 실재한다.
    cost = _num(row.get("cost_per_hour"))
    hourly = cost if cost is not None and cost > 0 else None

    record: dict[str, Any] = {
        "id": _text(row.get("id")),
        "provider": provider,
        "region": region,
        "specName": spec_name,
        "vCPU": vcpu,
        # **보정값을 담는다.** 원본값은 레코드마다 두지 않고 메타데이터에 규칙으로 남긴다
        # (`_corrections`) — 어느 프로바이더에 무슨 배율을 적용했는지가 곧 원본 복원식이다.
        "memGiB": correct_memory(provider, mem),
        "hourlyUSD": hourly,
        "architecture": _text(row.get("architecture")),
        "infraType": _text(row.get("infra_type")),
        "diskSizeGB": _disk_size(row.get("disk_size_gb")),
        "acceleratorType": _text(row.get("accelerator_type")),
        "acceleratorModel": _text(row.get("accelerator_model")),
        "acceleratorCount": _int(row.get("accelerator_count")),
        "acceleratorMemoryGB": _num(row.get("accelerator_memory_gb")),
    }
    return {k: v for k, v in record.items() if v is not None or k == "hourlyUSD"}


def project_rows(rows, *, namespace: str | None = SYSTEM_NAMESPACE) -> tuple[list[dict], dict]:
    """행들을 레코드로 투영하고 감사 통계를 함께 반환한다.

    Args:
        rows: spec_infos 행 dict의 이터러블.
        namespace: 이 namespace의 행만 쓴다. None이면 전체.

    Returns:
        (레코드 목록, 감사 통계). 통계는 빌드 요약과 상위 버그 감시에 쓴다.
    """
    specs: list[dict] = []
    stats: dict[str, Any] = {
        "total_rows": 0,
        "skipped_namespace": 0,
        "skipped_invalid": 0,
        "unknown_providers": collections.Counter(),
        "memory_audit": collections.defaultdict(
            lambda: {"n": 0, "integral": 0, "bug_fingerprint": 0}
        ),
    }

    for row in rows:
        stats["total_rows"] += 1
        if namespace is not None and _text(row.get("namespace")) != namespace:
            stats["skipped_namespace"] += 1
            continue
        record = project_row(row)
        if record is None:
            stats["skipped_invalid"] += 1
            continue
        provider = record["provider"]
        # 상위 버그 감시: ×1.024로 정수가 되면 버그 지문. 새 프로바이더가 버그 영향권에
        # 들어오면 여기서 드러난다 (보정 대상을 코드로 못 박아 두었으므로).
        audit = stats["memory_audit"][provider]
        # 감사는 **원본값**으로 한다 — 보정 후 값으로 지문을 세면 늘 정수라 뜻이 없다.
        # 컬럼 이름은 `memory_gi_b`다. `mem_gib`로 잘못 적었더니 전부 0이 되어
        # "정수 100% · bug 0%"라는 **무해해 보이는 거짓 통계**가 나왔다 —
        # 감사가 조용히 죽으면 새 프로바이더가 버그 영향권에 들어와도 못 잡는다.
        mem = _num(row.get("memory_gi_b")) or 0.0
        audit["n"] += 1
        if mem == int(mem):
            audit["integral"] += 1
        elif abs(mem * _MEMORY_BUG_FACTOR - round(mem * _MEMORY_BUG_FACTOR)) < 1e-6:
            audit["bug_fingerprint"] += 1

        specs.append(record)

    stats["memory_audit"] = dict(stats["memory_audit"])
    return specs, stats


#: 원본에 무엇을 어떻게 했는지. **값마다 칸을 늘리는 대신 규칙을 한 곳에 적는다** —
#: 이 식이 곧 원본 복원식이라 `memGiB / factor`로 되돌릴 수 있다.
CORRECTIONS = [
    {
        "field": "memGiB",
        "providers": sorted(_MEMORY_BUG_PROVIDERS),
        "operation": f"×{_MEMORY_BUG_FACTOR}",
        "reason": (
            "Upstream CB-Spider's ConvertMBToMiBInt64 applies the MB→MiB ratio only "
            "once, and to a value that is already MiB, so the value is recorded 2.4% "
            "below reality. To get back to the original, divide this value by "
            f"{_MEMORY_BUG_FACTOR}."
        ),
    }
]


def build_dataset(rows, *, namespace: str | None = SYSTEM_NAMESPACE) -> tuple[dict, dict]:
    """`costkb/schema.json` 모양의 데이터셋과 감사 통계를 만든다."""
    specs, stats = project_rows(rows, namespace=namespace)
    return (
        {"_note": SOURCE_NOTE, "_corrections": CORRECTIONS, "specs": specs},
        stats,
    )


def format_audit(stats: dict) -> str:
    """빌드 요약용 텍스트 — 특히 메모리 버그 영향 범위를 눈에 보이게."""
    lines = [
        f"행 {stats['total_rows']:,} → 레코드 {stats['total_rows'] - stats['skipped_namespace'] - stats['skipped_invalid']:,}"
        f" (namespace 제외 {stats['skipped_namespace']:,}, 무효 {stats['skipped_invalid']:,})",
        "프로바이더별 메모리 감사 (bug=×1.024하면 정수 → 상위 버그 지문):",
    ]
    for provider, audit in sorted(
        stats["memory_audit"].items(), key=lambda kv: -kv[1]["n"]
    ):
        n = audit["n"]
        flag = " ← 보정 적용" if provider in _MEMORY_BUG_PROVIDERS else ""
        lines.append(
            f"  {provider:10} n={n:6,}  정수 {audit['integral'] / n:5.1%}  "
            f"bug {audit['bug_fingerprint'] / n:5.1%}{flag}"
        )
    if stats["unknown_providers"]:
        lines.append(
            f"⚠️ 스키마에 없는 프로바이더: {dict(stats['unknown_providers'])} "
            "→ costkb/schema.json의 provider enum과 tumblebug.py의 KNOWN_PROVIDERS를 넓히세요."
        )
    return "\n".join(lines)
