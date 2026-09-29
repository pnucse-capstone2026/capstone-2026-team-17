"""에이전트용 사전 정의 질의 API (perfkb).

다른 KB의 agent_api와 같은 관례: 예외 대신 에이전트가 그대로 읽을 수 있는 텍스트를 준다.

## 무엇을 경고로 삼는가 — 노이즈를 피한다

Phase 1 실빌드에서 세 발견의 빈도가 크게 갈렸다:

    not_sustained (상시 CPU 미보장)   aws 609 · azure 1,390 · gcp 182
    old_generation (구세대)           aws 2,085 (11%)
    burst_network ('Up to N Gigabit') aws 8,897 (47.9%)  ← 절반이라 경고로 쓰면 노이즈

`burst_network`는 AWS의 절반이라 추천마다 붙으면 무의미해진다 — 상세 프로파일(`describe`)
에서만 보여주고 **추천 경고에는 넣지 않는다.** 추천 경고는 *구매 결정을 바꾸는* 두 가지,
상시 CPU 미보장과 구세대만 다룬다.

**fail-open이되 침묵하지는 않는다**: 성능 데이터가 없으면 경고를 **지어내지 않는다**.
잘못 경고하는 것보다 모른다고 하는 게 낫다 — 다른 cloudkb와 같은 원칙. 다만
*조용히* 넘어가지는 않는다. 예전에는 (a)성능 정상 (b)레코드 없음 (c)미추적 프로바이더가
전부 `None`이라 출력이 바이트 단위로 같았고, 사용자는 침묵을 "이상 없음"으로 읽었다
(결함 C4 — Alibaba 버스트 계열이 검증된 비버스트 스펙과 똑같이 무표시로 나왔다).
그래서 `recommend_note`가 네 상태를 **구분해** 돌려준다. 표시 방법은 호출자 몫이다.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.cloudkb.kbcommon.basis import needs_hedge
from app.cloudkb.perfkb.dataset import (
    find,
    get_by_id,
    get_by_spec_name,
    is_built,
    load_perf,
    tracked_providers,
)
from app.cloudkb.perfkb.fields import COMPARE_FIELDS, FIELDS

_OLD_GEN_NOTE = (
    "This is a previous-generation instance — a current generation may offer "
    "better price/performance."
)

#: 경고를 만들 수 있는 신호. **레코드에 이 중 하나도 없으면 "확인했다"고 말할 수 없다.**
#:
#: IBM을 붙이면서 드러난 구멍이다. IBM 카탈로그엔 버스트 여부도 세대 표시도 없어서,
#: 레코드는 있는데 `_warning_for`가 아무것도 못 찾는다. 그걸 `ok`로 돌려주면 비용
#: 도구의 꼬리말("경고가 없는 후보는 성능 확인됨 — 상시 CPU 보장·최신 세대")이
#: **거짓말**이 된다. 결함 C4에서 침묵을 안전 신호로 오독하게 만든 것과 같은 모양이라
#: 상태를 하나 더 뒀다.
_WARNING_SIGNALS = ("sustainedCpu", "currentGeneration")


def has_warning_signals(rec: dict) -> bool:
    """이 레코드로 버스트·세대를 판정할 수 있나."""
    return any(key in rec for key in _WARNING_SIGNALS)


def _warning_for(rec: dict) -> str | None:
    """레코드 하나에서 경고 문구를 만든다. 구매 결정을 바꾸는 것만."""
    parts: list[str] = []
    sustained = rec.get("sustainedCpu")
    if sustained and sustained["value"] is False:
        # note가 메커니즘(크레딧/공유코어/B계열)을 담고 있다. 없으면 일반 문구.
        parts.append(
            sustained.get("note") or "Sustained CPU performance is not guaranteed."
        )
    if rec.get("currentGeneration") is False:
        parts.append(_OLD_GEN_NOTE)
    return " ".join(parts) if parts else None


def recommend_warning(spec_id: str | None, output_dir: Path | str | None = None) -> str | None:
    """`id` 하나로 성능 경고만 얻는 저수준 진입점. 문제가 없거나 데이터가 없으면 None.

    ⚠️ **추천 조인에는 `recommend_note`를 쓰세요.** 이 함수는 "경고 없음"과 "정보 없음"을
    구분하지 못하고, id가 없는 costkb 번들 스펙을 조회조차 못 한다(결함 C3·C4).
    """
    if not spec_id:
        return None
    rec = get_by_id(spec_id, output_dir)
    return None if rec is None else _warning_for(rec)


#: `recommend_note`가 돌려주는 상태. 표시 기호는 호출자(도구 계층)가 정한다.
NOTE_WARN = "warn"  # 성능 함정 있음
NOTE_OK = "ok"  # 레코드가 있고 **판정에 필요한 신호를 다 보고** 경고할 것이 없음
NOTE_PARTIAL = "partial"  # 레코드는 있으나 버스트·세대 신호가 없어 판정 불가
NOTE_NO_RECORD = "no_record"  # 추적 대상 프로바이더인데 이 스펙이 없음
NOTE_UNTRACKED = "untracked"  # 이 프로바이더는 성능 신호를 수록하지 않음
NOTE_NOT_BUILT = "not_built"  # 성능 지식베이스 자체가 없음


@dataclass(frozen=True)
class PerfNote:
    """성능 조인 결과. `text`가 None이면 후보 줄에 붙일 말이 없다는 뜻."""

    status: str
    text: str | None = None


def recommend_note(
    provider: str,
    spec_name: str,
    spec_id: str | None = None,
    output_dir: Path | str | None = None,
) -> PerfNote:
    """추천 후보 하나에 대한 성능 소견 — costkb×perfkb 조인의 진입점.

    `spec_id`로 먼저 찾고, 없으면 `(provider, specName)`으로 **폴백**한다. costkb 번들
    36건에는 id가 없고 미러 레코드도 그 리전이 perfkb에 없을 수 있어서, id만 쓰면
    경고가 통째로 사라진다(결함 C3 — 번들이 t3.*/B*/e2-*라 하필 추천 상위를 독점한다).

    데이터가 없을 때 `None`을 돌려주지 않는 게 핵심이다 — 호출자가 "확인했고 괜찮다"와
    "모른다"를 구분해 표시할 수 있어야 한다(결함 C4).
    """
    if not is_built(output_dir):
        return PerfNote(NOTE_NOT_BUILT)

    rec = get_by_id(spec_id, output_dir) if spec_id else None
    if rec is None:
        rec = get_by_spec_name(provider, spec_name, output_dir)

    if rec is None:
        if provider and provider.lower() not in tracked_providers(output_dir):
            tracked = "/".join(sorted(tracked_providers(output_dir))) or "none"
            return PerfNote(
                NOTE_UNTRACKED,
                f"No performance data — performance signals are not tracked for "
                f"{provider} (only {tracked} are included).",
            )
        return PerfNote(
            NOTE_NO_RECORD,
            f"No performance data — the performance knowledge base has no record "
            f"for {spec_name}.",
        )

    warning = _warning_for(rec)
    if warning:
        return PerfNote(NOTE_WARN, warning)
    if not has_warning_signals(rec):
        # **"경고가 없다"와 "판정할 수 없다"는 다른 말이다.**
        return PerfNote(
            NOTE_PARTIAL,
            f"Cannot judge burst or generation — the source carries no such signal "
            f"for {provider} (other performance values are present).",
        )
    return PerfNote(NOTE_OK)


def recommendation_profile(
    provider: str,
    spec_name: str,
    spec_id: str | None = None,
    output_dir: Path | str | None = None,
) -> dict:
    """Return structured performance evidence for a VM selection surface.

    The recommendation UI must not reconstruct provider-specific burst rules from
    SKU names.  This projection therefore carries the same status and evidence as
    :func:`recommend_note`, plus the provider-specific fields that are actually
    present in perfkb.  Missing data remains explicit and never blocks sizing.
    """

    note = recommend_note(provider, spec_name, spec_id, output_dir)
    result: dict = {
        "status": note.status,
        "warning": note.text,
        "sustainedCpu": None,
        "attributes": [],
    }
    if note.status in {NOTE_NOT_BUILT, NOTE_NO_RECORD, NOTE_UNTRACKED}:
        return result

    rec = get_by_id(spec_id, output_dir) if spec_id else None
    if rec is None:
        rec = get_by_spec_name(provider, spec_name, output_dir)
    if rec is None:
        return result

    sustained = rec.get("sustainedCpu")
    if isinstance(sustained, dict):
        result["sustainedCpu"] = {
            key: sustained.get(key)
            for key in ("value", "note", "evidence", "basis")
        }

    attributes = []
    for field in FIELDS:
        value = rec.get(field.key)
        if value is None:
            continue
        item = {
            "key": field.key,
            "label": field.label,
            "value": value,
            "display": field.render(value),
        }
        if field.key == "networkPerformance" and rec.get("networkIsBurst") is True:
            item["warning"] = "Advertised network bandwidth is burst, not sustained."
        attributes.append(item)
    result["attributes"] = attributes
    return result


def _describe(rec: dict) -> str:
    lines = []
    sustained = rec.get("sustainedCpu")
    if sustained is not None:
        mark = "guaranteed" if sustained["value"] else "not guaranteed"
        # 유보를 붙일지는 `kbcommon.basis`가 정한다 — 여기서 리터럴로 비교하면
        # 같은 규칙이 KB마다 한 벌씩 생긴다.
        guessed = needs_hedge(sustained.get("basis", ""), sustained.get("reviewed", False))
        hedge = " (a guess from the naming convention)" if guessed else ""
        lines.append(f"  Sustained CPU performance: {mark}{hedge}")
        if sustained.get("note"):
            lines.append(f"    ⚠ {sustained['note']}")
    for field in FIELDS:
        if rec.get(field.key) is None:
            continue
        lines.append(f"  {field.label}: {field.render(rec[field.key])}")
        # 경고는 **그 줄 바로 밑**에 붙여야 한다. 예전엔 블록 맨 끝에 붙였는데,
        # 그러면 네트워크 이야기가 마지막 줄(EBS 최대 대역폭)에 달린 것처럼 읽혔다.
        # EBS 버스트와 네트워크 버스트는 다른 사안이라 오해가 판단을 바꾼다.
        if field.key == "networkPerformance" and rec.get("networkIsBurst"):
            lines.append(
                "    ⚠ Network bandwidth is burst ('Up to'), not a sustained value."
            )

    # **하드웨어 사실은 따로 묶는다.** 위쪽은 cb-tumblebug에서 온 성능 신호이고
    # 이쪽은 다른 소스(실제 인스턴스에서 확인한 것)라, 섞으면 어느 값이 어디서
    # 왔는지 알 수 없다. 확인 시점도 함께 밝힌다.
    hardware = []
    if rec.get("gpuModel"):
        model = rec["gpuModel"]
        shown = model if isinstance(model, str) else " + ".join(model)
        count = rec.get("gpuCount")
        arch = rec.get("gpuArchitecture")
        arch_text = f", {arch}" if isinstance(arch, str) else ""
        hardware.append(f"  GPU: {shown}{f' ×{count}' if count else ''}{arch_text}")
    if rec.get("cpuModel"):
        detail = []
        if rec.get("cpuCores"):
            detail.append(f"{rec['cpuCores']} cores")
        if rec.get("cpuThreads"):
            detail.append(f"{rec['cpuThreads']} threads")
        suffix = f" ({' · '.join(detail)})" if detail else ""
        hardware.append(f"  CPU: {rec['cpuModel']}{suffix}")
    if rec.get("memorySpeedMHz"):
        hardware.append(f"  Memory speed: {rec['memorySpeedMHz']} MHz")
    if hardware:
        checked = rec.get("hardwareCheckedAt")
        lines.append(f"  — Hardware{f' (checked {checked})' if checked else ''}")
        lines.extend(hardware)
    return "\n".join(lines)


def instance_profile(
    provider: str, spec_name: str, output_dir: Path | str | None = None
) -> str:
    """한 스펙의 성능 프로파일을 텍스트로 반환한다.

    성능은 리전 불변이라 여러 리전에 같은 스펙이 있어도 하나만 설명한다.
    """
    found = find(provider=provider, spec_name=spec_name, output_dir=output_dir)
    if not found:
        return (
            f"No performance data for {provider} {spec_name}. "
            "The performance knowledge base may not be built "
            "(python -m perfkb build), or the provider may not be tracked for "
            "performance signals (only aws/azure/gcp are included)."
        )
    return f"{provider} {spec_name} performance profile:\n{_describe(found[0])}"


def compare(
    provider: str, spec_names: list[str], output_dir: Path | str | None = None
) -> str:
    """**같은 프로바이더** 스펙들을 축별로 나란히 놓는다. 승자를 선언하지 않는다.

    왜 승자를 안 뽑나: "더 빠르다"는 워크로드에 달렸다(CPU 바운드 vs IO 바운드).
    그래서 축별 수치만 나열하고 판단은 맡긴다 — 근거가 부족하면 판정을 보류한다.

    프로바이더 간 비교(AWS vs Azure)는 이 함수로 **할 수 없다**. 파라미터가 단일 provider라
    구조적으로 막혀 있고, 축 자체가 프로바이더 전용이라 비교 기준이 없다.

    축은 `perfkb.fields`에서 온다 — 프로파일과 **같은 목록**이다. 예전엔 여기만 별도
    목록을 갖고 있어서 100% 채워진 `currentGeneration`이 빠졌고, 그래서 `perf_compare`는
    m5.large가 구세대라는 걸 한 줄도 말하지 않는데 `cost_recommend_specs`는 경고하는
    **상충**이 났다.
    """
    provider = provider.lower()
    recs, missing = [], []
    for name in spec_names:
        found = find(provider=provider, spec_name=name, output_dir=output_dir)
        (recs.append(found[0]) if found else missing.append(name))

    if len(recs) < 2:
        got = [r["specName"] for r in recs]
        return (
            f"Comparing needs at least 2 {provider} specs. "
            f"Found: {got or 'none'} / not found: {missing or 'none'}. "
            "Either there is no performance data (build needed) or the providers "
            "differ, so they cannot be compared."
        )

    lines = [
        f"{provider} spec performance comparison "
        f"({', '.join(r['specName'] for r in recs)}):"
    ]

    def _row(label: str, values: list[str], incomplete: bool = False) -> str:
        cells = " / ".join(f"{r['specName']}={v}" for r, v in zip(recs, values))
        return f"  {label}: {cells}" + (
            " (incomplete — some values are missing)" if incomplete else ""
        )

    # 상시 CPU는 모든 프로바이더 공통 축이다.
    sustained = []
    for r in recs:
        s = r.get("sustainedCpu")
        sustained.append(
            "guaranteed" if s and s["value"] else "not guaranteed" if s else "unknown"
        )
    lines.append(_row("Sustained CPU performance", sustained))

    for field in COMPARE_FIELDS:
        vals = [r.get(field.key) for r in recs]
        if all(v is None for v in vals):
            continue  # 아무도 이 축 값이 없으면 행을 만들지 않는다 — 축은 저절로 갈린다
        cells = [field.render(v) if v is not None else "unknown" for v in vals]
        lines.append(_row(field.label, cells, incomplete=any(v is None for v in vals)))

    # **판단이 갈리는 것은 여기서 같은 문구로 말한다.** 축 값만 나열하면 "구세대"라는
    # 사실이 표에는 있어도 그게 무슨 뜻인지는 안 나온다 — 그 자리를 `cost_recommend_specs`의
    # 경고가 채우면서 두 도구가 다른 말을 하게 됐다. 판정 함수를 공유해 문구를 맞춘다.
    for rec in recs:
        warning = _warning_for(rec)
        if warning:
            lines.append(f"  ⚠ {rec['specName']}: {warning}")

    lines.append(
        "※ 'Faster' depends on the workload (CPU-bound vs IO-bound), so no winner "
        "is declared. Cross-provider comparison is impossible because the axes differ."
    )
    return "\n".join(lines)


def specs_meeting_ebs_baseline(
    min_mbps: float, limit: int = 10, output_dir: Path | str | None = None
) -> str:
    """AWS 스펙 중 **지속(baseline) EBS 대역폭**이 기준 이상인 것을 찾는다.

    함정 방지: 사람들이 보는 "최대 대역폭"은 버스트라 지속되지 않는다. 이 필터는 baseline만
    본다. **AWS 전용** — Azure는 IOPS로, GCP는 별도 디스크로 표현해 축이 다르다.
    """
    records = load_perf(output_dir) or []
    hits = {}  # specName → baseline (리전 불변이라 dedup)
    for r in records:
        if r["provider"] != "aws":
            continue
        base = r.get("ebsBaselineMbps")
        if base is not None and base >= min_mbps:
            hits[r["specName"]] = (base, r.get("ebsMaxMbps"))
    if not hits:
        return (
            f"Found no AWS spec with sustained EBS bandwidth of {min_mbps:g} Mbps or "
            "more. Check that the performance data is built and that the threshold "
            "is not too high."
        )
    ranked = sorted(hits.items(), key=lambda kv: -kv[1][0])[: max(1, limit)]
    lines = [
        f"AWS specs with sustained EBS bandwidth of {min_mbps:g} Mbps or more — "
        f"{len(hits)} found, top {len(ranked)}:"
    ]
    for name, (base, mx) in ranked:
        mx_text = f", burst max {mx:g}" if mx else ""
        lines.append(f"  - {name}: sustained {base:g} Mbps{mx_text}")
    lines.append(
        "※ Check price and size with cost_recommend_specs. The values here are "
        "sustained bandwidth."
    )
    return "\n".join(lines)


def hardware_facts(
    provider: str, spec_name: str, output_dir: Path | str | None = None
) -> str | None:
    """스펙 **하나**의 하드웨어 사실을 한 줄로. 없으면 None.

    성능 축에만 있는 것을 다른 축의 도구가 가져갈 수 있게 한다. 비용 축에도
    `acceleratorModel`이 있어서 "NVIDIA A100 8개"까지는 나오지만, **아키텍처
    (`Ampere`)와 정확한 SKU(`A100-SXM4-40GB`)는 여기에만 있다.**

    실측에서 드러났다 — 이름 조회 도구를 만들자 모델이 GPU 질문에도 그걸 부르기
    시작했고, 답 자체는 근거가 있었지만 `Turing` 같은 사실이 답에서 사라졌다.
    **어느 도구를 부르든 아는 것이 실려야 한다.**
    """
    found = find(provider=provider, spec_name=spec_name, output_dir=output_dir)
    if not found:
        return None
    rec = found[0]
    parts = []
    if rec.get("gpuModel"):
        model = rec["gpuModel"]
        shown = model if isinstance(model, str) else " + ".join(model)
        count = rec.get("gpuCount")
        arch = rec.get("gpuArchitecture")
        parts.append(
            f"GPU {shown}{f' ×{count}' if count else ''}"
            f"{f' ({arch})' if isinstance(arch, str) else ''}"
        )
    if rec.get("cpuModel"):
        parts.append(f"CPU {rec['cpuModel']}")
    if not parts:
        return None
    checked = rec.get("hardwareCheckedAt")
    return " · ".join(parts) + (f" (checked {checked})" if checked else "")


def hardware_summary(provider: str, output_dir: Path | str | None = None) -> str | None:
    """이 프로바이더에 **하드웨어 사실이 얼마나 있는지** 한 줄로. 없으면 None.

    용량 축에서 "이 인스턴스에 어떤 GPU가 달렸나"를 물었을 때, 그 답이 여기 있다는
    걸 알려주기 위한 것이다. 실측에서 모델이 `AWS::EC2::Instance.Gpu`라는 없는
    속성을 뒤지다 웹으로 갔다 — 우리는 그때 GPU 571건을 쥐고 있었다.

    판정은 하지 않고 **어디를 보라고만** 말한다. 조인은 도구 계층의 몫이다.
    """
    specs = load_perf(output_dir)
    if not specs:
        return None
    names, gpus = set(), set()
    for rec in specs:
        if rec.get("provider") != provider or not rec.get("hardwareEvidence"):
            continue
        names.add(rec.get("specName"))
        if rec.get("gpuModel"):
            gpus.add(rec.get("specName"))
    if not names:
        return None
    checked = sorted({r.get("hardwareCheckedAt") for r in specs
                      if r.get("provider") == provider and r.get("hardwareCheckedAt")})
    when = f", checked {checked[0]}~{checked[-1]}" if checked else ""
    return (
        f"{len(names):,} spec types with a known CPU/GPU model"
        f"{f' ({len(gpus)} of them with a GPU)' if gpus else ''}{when}"
    )
