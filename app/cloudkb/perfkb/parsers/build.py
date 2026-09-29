"""행 묶음 → 데이터셋 + 감사 통계.

감사가 있는 이유는 costkb와 같다 — **커버리지 공백이 조용히 지나가면 안 된다.**
성능은 특히 그렇다. "신호가 없다"를 "문제가 없다"로 읽으면 t3a.medium을 그대로 1순위로
추천하게 된다. 그래서 빌드할 때마다 프로바이더별로 *무엇을 알고 무엇을 모르는지*를
숫자로 보여준다.
"""

from __future__ import annotations

import collections
from typing import Any

from .project import SYSTEM_NAMESPACE, project_row

SOURCE_NOTE = (
    "Mirror of the details column (raw CSP response) of cb-tumblebug spec_infos. It comes "
    "from the same rows as costkb and joins by id. **Performance cannot be compared across "
    "providers** — ACU exists only on Azure, clock speed only on AWS. Azure ACU is filled "
    "for only 37.7% and the gaps are not explained by generation, so a missing value means "
    "'unknown', not 'slow'. The sustained-CPU verdict comes from a different mechanism per "
    "provider, so every record carries its evidence and the basis of that evidence."
)

# 성능 신호를 추적한 프로바이더. 나머지 7개는 details 구조를 조사하지 않아 '모름'으로 둔다.
KNOWN_PROVIDERS = frozenset({"aws", "azure", "gcp"})

_SIGNALS = ("sustainedCpu", "currentGeneration", "clockGHz", "acu", "ebsBaselineMbps")


class UnclassifiedKey(ValueError):
    """`details`에 분류표가 모르는 키가 나타났다 — **빌드를 죽인다**(위협 T1).

    `details`는 Go `%v` 문자열이라 계약이 없다. 상위가 키를 하나 추가하면 우리는
    조용히 모르게 되고, 그 조용함이 이 저장소가 계속 막아 온 것이다. 지금까지
    `DetailsMismatch`는 **값**만 지켰다 — 키 집합은 아무도 안 지켰다.
    """


def _check_keys(provider: str, keys, seen: dict[str, set[str]]) -> None:
    """이 행의 키가 전부 분류표에 있는가. 새 키가 나올 때만 실제로 본다.

    **최상위 키만 본다.** 중첩 1단은 `parse_details`가 문자열로 돌려줘서 여기서는
    안 보이고, `tools/tumblebug_inventory.py`를 다시 돌릴 때 잡힌다(그때 핀 파일이
    갱신되고 `test_field_map`이 어긋남을 실패로 만든다). 못 보는 자리를 적어 두는
    것이 이 저장소의 규율이라 그대로 적는다 — 여기서 중첩까지 열면 빌드마다 73,083행에
    깊이 파싱이 붙는다.
    """
    from app.cloudkb.perfkb.parsers.field_map import FIELD_MAP, unknown_keys

    if provider not in FIELD_MAP:
        return
    fresh = set(keys) - seen.setdefault(provider, set())
    if not fresh:
        return
    seen[provider] |= fresh
    unknown = unknown_keys(provider, fresh)
    if unknown:
        raise UnclassifiedKey(
            f"{provider}: details에 분류표가 모르는 키가 있습니다 — {unknown}\n"
            "perfkb/parsers/field_map.py에 판정을 하나 적으세요(사유 필수). "
            "담지 않기로 하는 것도 판정입니다 — 판단하지 않은 칸이 있는 것이 안 됩니다."
        )


def project_rows(rows, *, namespace: str | None = SYSTEM_NAMESPACE) -> tuple[list[dict], dict]:
    """행들을 성능 레코드로 투영하고 감사 통계를 함께 반환한다."""
    records: list[dict] = []
    stats: dict[str, Any] = {
        "total_rows": 0,
        "skipped_no_signal": 0,
        "untracked_providers": collections.Counter(),
        "coverage": collections.defaultdict(collections.Counter),
        "findings": collections.defaultdict(collections.Counter),
    }
    seen_keys: dict[str, set[str]] = {}

    for row in rows:
        stats["total_rows"] += 1
        provider = (row.get("provider_name") or "").strip().lower()
        if provider:
            from .details import parse_details

            try:
                _check_keys(provider, parse_details(row.get("details")), seen_keys)
            except UnclassifiedKey:
                raise
            except Exception:  # noqa: BLE001 — 못 읽는 details는 투영이 따로 다룬다
                pass
        record = project_row(row, namespace=namespace)
        if record is None:
            stats["skipped_no_signal"] += 1
            if provider and provider not in KNOWN_PROVIDERS:
                stats["untracked_providers"][provider] += 1
            continue

        records.append(record)
        cov = stats["coverage"][record["provider"]]
        cov["n"] += 1
        for signal in _SIGNALS:
            if signal in record:
                cov[signal] += 1

        # 실제로 경고를 낳는 것들 — 이 숫자가 0이면 파서가 고장난 것이다.
        find = stats["findings"][record["provider"]]
        sustained = record.get("sustainedCpu")
        if sustained and sustained["value"] is False:
            find["not_sustained"] += 1
        if record.get("currentGeneration") is False:
            find["old_generation"] += 1
        if record.get("networkIsBurst") is True:
            find["burst_network"] += 1

    return records, stats


def field_coverage() -> list[dict]:
    """**무엇을 왜 안 담았나** — 산출물에 남는 판정 기록.

    `ibm-perf`의 `droppedConstantFields`를 일반화한 것이다. 그쪽만 버린 칸 이름을
    적었고 미러 `details`에는 그런 기록이 없어서, 무엇을 왜 안 담았는지 아무도 몰랐다.
    이제 셋 다 같은 규율을 쓴다.
    """
    from app.cloudkb.perfkb.parsers.field_map import (
        FIELD_MAP,
        UNTRACKED_PROVIDERS,
        not_adopted,
        summarize,
    )

    out = [
        {"provider": provider, "fields": summarize(provider),
         "notAdopted": not_adopted(provider)}
        for provider in sorted(FIELD_MAP)
    ]
    out.append({"untrackedProviders": UNTRACKED_PROVIDERS})
    return out


def build_dataset(rows, *, namespace: str | None = SYSTEM_NAMESPACE) -> tuple[dict, dict]:
    """`perfkb/schema.json` 모양의 데이터셋과 감사 통계를 만든다."""
    records, stats = project_rows(rows, namespace=namespace)
    return (
        {"_note": SOURCE_NOTE, "specs": records, "_coverage": field_coverage()},
        stats,
    )


def format_audit(stats: dict) -> str:
    """빌드 요약 — 무엇을 알고 무엇을 모르는지를 숫자로."""
    lines = [
        f"행 {stats['total_rows']:,} → 레코드 "
        f"{stats['total_rows'] - stats['skipped_no_signal']:,} "
        f"(성능 신호 없어 제외 {stats['skipped_no_signal']:,})",
        "프로바이더별 신호 커버리지:",
    ]
    for provider, cov in sorted(stats["coverage"].items(), key=lambda kv: -kv[1]["n"]):
        n = cov["n"]
        parts = [f"{s} {cov[s] / n:5.1%}" for s in _SIGNALS if cov[s]]
        lines.append(f"  {provider:8} n={n:6,}  " + "  ".join(parts))

    lines.append("경고를 낳는 발견(0이면 파서 고장 의심):")
    for provider, find in sorted(stats["findings"].items()):
        if find:
            detail = ", ".join(f"{k} {v:,}" for k, v in sorted(find.items()))
            lines.append(f"  {provider:8} {detail}")

    if stats["untracked_providers"]:
        lines.append(
            f"미추적 프로바이더(성능 신호 없음): {dict(stats['untracked_providers'])} "
            "→ 의도된 공백입니다. 필요해지면 parsers/project.py에 투영을 추가하세요."
        )
    return "\n".join(lines)
