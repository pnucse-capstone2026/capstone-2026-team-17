"""에이전트용 사전 정의 질의 API (costkb).

다른 cloudkb 질의 API와 같은 관례: 예외 대신 에이전트가 그대로 읽을 수 있는
한국어 텍스트를 반환한다.

다른 두 KB와 달리 `_MISSING_MESSAGE`("먼저 build 하세요")가 없다 — 번들 36건이 항상
폴백으로 있어 산출물이 없을 수가 없다. `costkb build`는 커버리지를 73k건으로 넓힐 뿐이다.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from app.cloudkb.costkb.dataset import (
    DEFAULT_ARCHITECTURE,
    _load_azure_discount,
    _resolve,
    azure_discount_built,
    azure_discount_for,
    azure_discount_regions,
    count_unpriced,
    coverage,
    filter_specs,
    find_by_name,
    is_built,
    load_warning,
    name_suggestions,
    provider_summary,
    resolve_region,
    spot_commit_for,
    spot_commit_regions,
)

# 한 달 가동 시간 기준: 24h * 365d / 12 ≈ 730시간(상시 가동 가정).
HOURS_PER_MONTH = 730

_COST_DISCLAIMER = (
    "An estimate based on on-demand list price and a representative region, not an "
    "actual bill (storage / egress / managed services / commitment discounts not "
    "included)."
)


def coverage_text(output_dir: Path | str | None = None) -> str:
    """데이터셋이 어디까지 커버하는지 — 조건 불만족 시 안내에 쓴다."""
    if is_built(output_dir):
        return "\n".join(
            f"  - {row['provider']}: {row['count']:,} specs, {row['regions']} regions, "
            f"max vCPU {row['vcpu_max']}, max memory {row['mem_max_gib']:g} GiB"
            for row in provider_summary(output_dir)
        )
    return "\n".join(
        f"  - {row['provider']} {row['region']}: {row['count']} specs, "
        f"vCPU {row['vcpu_min']}~{row['vcpu_max']}, "
        f"memory {row['mem_min_gib']}~{row['mem_max_gib']} GiB"
        for row in coverage(output_dir)
    )


def _describe(spec: dict) -> str:
    hourly = spec["hourlyUSD"]
    price = f"${hourly:.4f}/h" if hourly is not None else "price unknown"
    # `memGiB`가 곧 실제 값이다 — 빌드가 상위 버그를 고쳐서 담는다(_corrections).
    # 예전에는 표시와 필터가 서로 다른 칸을 봐서 답이 자리마다 갈렸다.
    mem_text = f"{spec['memGiB']:g} GiB"
    # 리전을 접었으면 몇 곳이 더 있는지 밝힌다. 숨기면 "이 스펙은 이 리전에만 있나?"로
    # 오해되고, 그건 접기가 만들어낸 거짓 정보다.
    folded = spec.get("_foldedRegions") or []
    where = spec["region"]
    if folded:
        where += f" and {len(folded)} more regions"
    return (
        f"- {spec['provider'].upper()} {spec['specName']} ({where}): "
        f"{spec['vCPU']} vCPU / {mem_text}, {price}"
    )


def _region_fallback_note(
    region: str | None, output_dir: Path | str | None = None
) -> str | None:
    """리전 질의가 **여러 리전으로 번졌으면** 그 사실을 밝힌다. 아니면 None.

    정확 일치는 아무 말도 하지 않는다 — 사용자가 물은 그대로이기 때문이다. 부분
    일치일 때만 밝히는데, 이때 단가를 리전 하나의 값으로 읽으면 틀린 비교가 된다.
    """
    if not region:
        return None
    regions, how = resolve_region(region, output_dir)
    if how == "none":
        return (
            f"※ No region in the dataset matches the name '{region}', so no "
            "candidate meets the region condition."
        )
    if how == "exact":
        return None
    shown = ", ".join(sorted(regions)[:6]) + (" …" if len(regions) > 6 else "")
    return (
        f"※ No region is named exactly '{region}', so this answer **groups "
        f"{len(regions)} regions** ({shown}). Unit price differs by region — "
        "do not read it as one."
    )


def recommend_specs(
    vcpu_min: int = 2,
    mem_min_gib: float = 4,
    provider: str | None = None,
    region: str | None = None,
    sort_by: str = "cost",
    limit: int = 5,
    *,
    architecture: str | None = DEFAULT_ARCHITECTURE,
    require_accelerator: bool = False,
    output_dir: Path | str | None = None,
    annotate: Callable[[dict], str | None] | None = None,
    footer: Callable[[list[dict]], str | None] | None = None,
) -> str:
    """요구사항을 만족하는 VM 스펙 후보를 **시간당 단가까지만** 텍스트로 반환한다.

    ⚠️ **월 비용을 여기에 넣지 말 것.** 예전에는 후보마다 `≈ $121.47/월`을 함께 줬는데,
    그러면 모델이 월 비용을 이미 손에 쥔 상태가 되어 `estimate_monthly_cost` 도구가
    불필요해 보인다 — 실측으로 5회 중 5회 도구를 건너뛰고 직접 암산했다. 제거 후 5/5 호출.
    (사람이 읽는 `costkb/cli.py`의 표는 월 비용을 계속 보여준다 — 거기엔 다음 도구가 없다.)

    Args:
        annotate: 각 후보 스펙 dict를 받아 한 줄 주석을 반환하는 선택적 콜백.
            None을 반환하면 주석을 안 붙인다. **costkb는 이 주석이 무엇인지 모른다** —
            성능 경고(perfkb) 조인을 도구 계층에서 끼워넣기 위한 확장점일 뿐이다.
            KB끼리 import하지 않는 규약을 지키면서 축을 잇는 방법이다.
            **기호(`⚠`/`·`)도 콜백이 붙인다** — 예전엔 여기서 `⚠`를 하드코딩했는데,
            그건 "모든 주석은 경고"라는 의미 가정이라 "정보 없음" 같은 비경고 주석을
            달 수 없었다(결함 C4).
        footer: 후보 목록 전체를 받아 블록 끝 한 줄을 반환하는 선택적 콜백.
            annotate와 대칭이며 마찬가지로 costkb는 내용을 모른다. 후보마다 상태를
            적으면 노이즈라, "주석 없는 후보가 무슨 뜻인지"를 한 번만 밝히는 데 쓴다.
    """
    results = filter_specs(
        vcpu_min,
        mem_min_gib,
        provider,
        region,
        sort_by,
        limit,
        architecture=architecture,
        require_accelerator=require_accelerator,
        output_dir=output_dir,
    )
    if not results:
        # **무엇 때문에 비었는지 밝힌다.** 가속기 조건으로 걸러 비었는데 "조건을
        # 조정하세요"만 말하면 사용자는 vCPU·메모리만 낮추다가 끝난다.
        extra = (
            "\n(Searching only for specs with an accelerator — drop that condition "
            "and there are more candidates.)"
            if require_accelerator else ""
        )
        # 리전 이름이 원인이면 그걸 먼저 말한다. 안 그러면 사용자는 vCPU·메모리만
        # 낮추다가 끝난다 — 가속기 조건에서 이미 겪은 실패 모양이다.
        if region and resolve_region(region, output_dir)[1] == "none":
            extra = (
                f"\n(No region in the dataset matches the name '{region}' — "
                "check the region name first.)"
            ) + extra
        return (
            "No spec in the dataset meets these conditions. This dataset's coverage "
            f"is below, so adjust the conditions:{extra}\n{coverage_text(output_dir)}"
        )

    lines = []
    for spec in results:
        line = _describe(spec)
        note = annotate(spec) if annotate is not None else None
        if note:
            line += f"\n    {note}"
        lines.append(line)
    text = "Recommended candidates (on-demand list price, hourly rate):\n" + "\n".join(lines)

    mixed = _region_fallback_note(region, output_dir)
    if mixed:
        text += f"\n\n{mixed}"

    unpriced = count_unpriced(
        vcpu_min, mem_min_gib, provider, region,
        architecture=architecture, output_dir=output_dir,
    )
    if unpriced:
        text += (
            f"\n\n※ {unpriced} more candidates meet the conditions but have no "
            "price data. Check live prices with the cb-tumblebug MCP."
        )

    degraded = load_warning(output_dir)
    if degraded:
        # 조용한 폴백은 "커버리지가 왜 갑자기 좁아졌지?"를 미궁으로 만든다.
        text += f"\n\n⚠ {degraded}"

    tail = footer(results) if footer is not None else None
    if tail:
        text += f"\n\n※ {tail}"
    return (
        text
        + "\n\nCompute monthly cost with the estimate_monthly_cost tool "
        "(it accounts for node count and hours, and attaches the limitation "
        "notice). Do not multiply it out yourself."
    )


def describe_spec(
    spec_name: str,
    provider: str | None = None,
    region: str | None = None,
    *,
    output_dir: Path | str | None = None,
) -> str:
    """스펙 **하나**를 이름으로 조회한다. "n2-highmem-8 메모리 몇 GiB?"에 답한다.

    조건 필터만 있고 이름 조회가 없어서 이 질문이 실측에서 0/3 실패했다 — 데이터는
    처음부터 있었고 표면이 없었을 뿐이다.

    리전을 안 주면 **가격 범위**를 보여준다. 리전마다 단가가 달라서 하나만 고르면
    그게 대표값인 양 읽히기 때문이다(n2-highmem-8은 40개 리전에서 $0.52~$0.84다).
    """
    rows = find_by_name(spec_name, provider, output_dir)
    if not rows:
        near = name_suggestions(spec_name, output_dir=output_dir)
        hint = f"\n  Similar names: {', '.join(near)}" if near else ""
        return (
            f"Spec '{spec_name}' was not found in the catalog — that does not mean "
            f"no such spec exists, it means **it is not in this dataset**.{hint}"
        )

    lines = []
    for prov in sorted({r["provider"] for r in rows}):
        group = [r for r in rows if r["provider"] == prov]
        head = group[0]
        priced = [r for r in group if r["hourlyUSD"] is not None]
        regions = sorted({r["region"] for r in group})

        lines.append(f"{prov.upper()} {head['specName']}")
        lines.append(f"  vCPU {head['vCPU']} · memory {head['memGiB']:g} GiB")
        if head.get("architecture"):
            lines[-1] += f" · {head['architecture']}"
        accel = head.get("acceleratorCount") or 0
        if accel:
            model = (
                head.get("acceleratorModel") or head.get("acceleratorType") or "accelerator"
            )
            lines.append(f"  Accelerator {model} ×{accel}")

        if region is not None:
            here = [r for r in group if r["region"].lower() == region.strip().lower()]
            if not here:
                lines.append(
                    f"  Not present in region '{region}'. Regions it is in: "
                    + ", ".join(regions[:8])
                )
            elif here[0]["hourlyUSD"] is None:
                lines.append(f"  {here[0]['region']}: no price data")
            else:
                lines.append(f"  {here[0]['region']}: ${here[0]['hourlyUSD']:.4f}/h")
        elif not priced:
            lines.append(f"  {len(regions)} regions · no price data")
        else:
            low = min(r["hourlyUSD"] for r in priced)
            high = max(r["hourlyUSD"] for r in priced)
            span = f"${low:.4f}/h" if low == high else f"${low:.4f} ~ ${high:.4f}/h"
            lines.append(f"  {len(regions)} regions · hourly {span} (varies by region)")
        lines.append(f"  Regions: {', '.join(regions[:10])}" + (" …" if len(regions) > 10 else ""))

    degraded = load_warning(output_dir)
    if degraded:
        lines.append(f"\n⚠ {degraded}")
    lines.append(
        "\n※ An on-demand list-price snapshot. " + _COST_DISCLAIMER
    )
    return "\n".join(lines)


def _discount_line(rec: dict) -> str:
    """스팟·약정 한 리전의 값을 시간당으로 나란히 보여준다.

    약정은 원본이 월 단위라 그대로 두고 시간당(÷730)을 병기한다 — 다른 가격과
    비교하려면 시간당이 필요한데, 원본을 버리지 않도록 둘 다 적는다.
    """
    parts = []
    spot = rec.get("hourSpotUSD")
    if spot is not None:
        parts.append(f"spot ${spot:.4f}/h")
    for label, key in (
        ("1-year commitment", "month1yUSD"),
        ("3-year commitment", "month3yUSD"),
    ):
        month = rec.get(key)
        if month is not None:
            parts.append(
                f"{label} ${month / HOURS_PER_MONTH:.4f}/h (${month:,.2f}/month)"
            )
    return " · ".join(parts) if parts else "no value"


def discount_pricing(
    spec_name: str,
    region: str | None = None,
    *,
    output_dir: Path | str | None = None,
) -> str:
    """GCP 인스턴스의 스팟·약정 가격. 미러엔 없는 축이라 Cyclenerd에서 보강한다.

    미러의 온디맨드와 **다른 스냅샷**이므로, 온디맨드가 어긋난 리전에서는 그 사실을
    함께 밝힌다 — 스팟·약정은 Cyclenerd 자신의 온디맨드 기준이다.
    """
    if region is not None:
        rec = spot_commit_for(spec_name, region, output_dir)
        if rec is None:
            rows = spot_commit_regions(spec_name, output_dir)
            if not rows:
                return _no_discount(spec_name)
            # 리전 오타를 잡는다. 모델이 `asia-northheast3`(h 하나 더)처럼 한 글자
            # 틀리면, "정보 없음"을 "그 리전엔 스팟이 없다"로 오해해 **틀린 답**을
            # 냈다(실측). 가까운 리전이 확실하면 그걸로 답하되 교정 사실을 밝힌다.
            import difflib

            available = sorted(r["region"] for r in rows)
            near = difflib.get_close_matches(region, available, n=1, cutoff=0.8)
            if near:
                rec = spot_commit_for(spec_name, near[0], output_dir)
                note = (
                    f"(no region named '{region}' — answering with the closest, "
                    f"'{near[0]}') "
                )
                lines = [f"{spec_name} spot & commitment pricing (GCP) {note}:"]
                lines.append(f"  - {rec['region']}: {_discount_line(rec)}")
                extra = ""
                if not rec.get("snapshotMatchesMirror", True):
                    ref = rec.get("hourRefUSD")
                    lines[-1] += f"  ※ baseline on-demand ${ref:.4f}/h" if ref else ""
                    extra = (
                        "\n※ This source's on-demand differs from the mirror by more "
                        "than 5% — a snapshot timing difference, so spot and "
                        "commitment are based on that on-demand."
                    )
                return (
                    "\n".join(lines)
                    + "\n\n※ Source: Cyclenerd GCP price table "
                    "(2026-07-16 snapshot, Apache-2.0)."
                    + extra
                )
            return (
                f"{spec_name}: no spot or commitment data for region '{region}'. "
                f"Regions this spec has data for: "
                + ", ".join(available[:8])
            )
        recs = [rec]
    else:
        recs = spot_commit_regions(spec_name, output_dir)
        if not recs:
            return _no_discount(spec_name)
        recs = sorted(recs, key=lambda r: r["region"])[:6]

    lines = [f"{spec_name} spot & commitment pricing (GCP):"]
    diverged = False
    for rec in recs:
        line = f"  - {rec['region']}: {_discount_line(rec)}"
        if not rec.get("snapshotMatchesMirror", True):
            diverged = True
            ref = rec.get("hourRefUSD")
            line += f"  ※ baseline on-demand ${ref:.4f}/h" if ref else ""
        lines.append(line)
    footer = (
        "\n\n※ Source: Cyclenerd GCP price table (2026-07-16 snapshot, Apache-2.0). "
        "On-demand comes from the costkb mirror (cb-tumblebug); only spot and "
        "commitment are supplemented from this source."
    )
    if diverged:
        footer += (
            "\n※ In regions marked 'baseline on-demand', this source's on-demand "
            "differs from our mirror by more than 5% — a price snapshot timing "
            "difference, so spot and commitment are based on that on-demand. "
            "Check the exact current price with the cb-tumblebug MCP."
        )
    return "\n".join(lines) + footer


def _azure_line(rec: dict) -> str:
    """할인 한 줄. **온디맨드 대비 비율을 함께 적는다** — 값만 주면 얼마나 싼지
    사람이 암산해야 하고, 그 암산은 우리가 보증하지 않는다.
    """
    base = rec.get("mirrorUSD")
    parts = []
    for key, label in (
        ("spotUSD", "spot"),
        ("reserved1yUSD", "1-year reserved"),
        ("reserved3yUSD", "3-year reserved"),
        ("savings1yUSD", "1-year savings plan"),
        ("savings3yUSD", "3-year savings plan"),
    ):
        value = rec.get(key)
        if value is None:
            continue
        share = f" ({value / base:.0%})" if base else ""
        parts.append(f"{label} ${value:.4f}/h{share}")
    return " · ".join(parts) if parts else "no discount data"


AZURE_DISCOUNT_MISSING = (
    "Azure discount pricing (spot / reserved / savings plan) is not in this "
    "environment. **That means we did not include the data, not that there are no "
    "discounts.** This axis comes from the Azure Retail Prices API, and we have no "
    "redistribution permission so it is not kept in the repository — to use it, "
    "fetch it yourself: `python -m costkb build-azure-pricing`"
)


def azure_discount_pricing(
    spec_name: str,
    region: str | None = None,
    *,
    output_dir: Path | str | None = None,
) -> str:
    """Azure VM의 스팟·예약·저축 플랜 가격.

    GCP 쪽(`discount_pricing`)과 두 가지가 다르다.

    1. **온디맨드가 미러와 일치한다.** 실측에서 어긋남이 0건이라 스냅샷 헤지가
       필요 없다. 오히려 독립 소스 둘이 같은 값을 말하는 **교차 확인**이다.
       그래도 가정하지 않고 레코드의 `matchesMirror`를 보고, 어긋난 것이 있으면
       그것만 밝힌다.
    2. **산출물이 없는 것이 기본이다.** 재배포 허가가 없어 커밋하지 않으므로,
       빈 답 대신 받는 방법을 알려준다.
    """
    if not azure_discount_built(output_dir):
        return AZURE_DISCOUNT_MISSING

    if region is not None:
        rec = azure_discount_for(spec_name, region, output_dir)
        if rec is None:
            rows = azure_discount_regions(spec_name, output_dir)
            if not rows:
                return _no_azure_discount(spec_name, output_dir)
            import difflib

            available = sorted(r["region"] for r in rows)
            near = difflib.get_close_matches(region, available, n=1, cutoff=0.8)
            if not near:
                return (
                    f"{spec_name}: no discount data for region '{region}'. "
                    "Regions this spec has data for: " + ", ".join(available[:8])
                )
            # GCP 쪽에서 실측으로 겪은 실패다 — 모델이 리전을 한 글자 틀리면
            # "정보 없음"을 "그 리전엔 스팟이 없다"로 오해해 틀린 답을 냈다.
            rec = azure_discount_for(spec_name, near[0], output_dir)
            prefix = (
                f"(no region named '{region}' — answering with the closest, "
                f"'{near[0]}') "
            )
        else:
            prefix = ""
        recs = [rec]
    else:
        prefix = ""
        recs = sorted(azure_discount_regions(spec_name, output_dir),
                      key=lambda r: r["region"])[:6]
        if not recs:
            return _no_azure_discount(spec_name, output_dir)

    lines = [f"{spec_name} discount pricing (Azure) {prefix}:".replace(" :", ":")]
    diverged = False
    for rec in recs:
        base = rec.get("mirrorUSD")
        head = (
            f"  - {rec['region']}: on-demand ${base:.4f}/h"
            if base else f"  - {rec['region']}:"
        )
        lines.append(f"{head} · {_azure_line(rec)}")
        if not rec.get("matchesMirror", True):
            diverged = True
    footer = (
        "\n\n※ Source: Azure Retail Prices API (no auth). On-demand comes from the "
        "costkb mirror (cb-tumblebug); only discounts are supplemented from this "
        "source. **Reserved prices are period totals in the source, converted here "
        "to hourly** (1 year 8,760h · 3 years 26,280h). Prices that bundle a Windows "
        "license and Dev/Test-only prices are not included."
    )
    if diverged:
        footer += (
            "\n※ In some regions this source's on-demand differs from our mirror — "
            "it may be a snapshot timing difference, so the discount prices may be "
            "off by as much."
        )
    return "\n".join(lines) + footer


def _no_azure_discount(spec_name: str, output_dir: Path | str | None) -> str:
    """**제안은 이 데이터셋 안에서만 한다.**

    처음엔 미러 전체에서 비슷한 이름을 찾았는데, `m5.large`를 물으면 AWS 스펙을
    줄줄이 제안했다 — Azure 전용 축에서 AWS 이름을 권하면 그게 Azure에 있는 것처럼
    읽힌다. 이 KB가 줄곧 좁혀 온 '없음'의 뜻이 흐려지는 자리다.
    """
    import difflib

    known = sorted({name for name, _ in _load_azure_discount(_resolve(output_dir))})
    near = difflib.get_close_matches(spec_name.strip().lower(), known, n=5, cutoff=0.6)
    hint = f" Similar names on the Azure side: {', '.join(near)}" if near else ""
    return (
        f"No Azure discount data for '{spec_name}'. **That means it is not in this "
        f"dataset, not that it does not exist** (only Azure VMs are included).{hint}"
    )


def _no_discount(spec_name: str) -> str:
    """스팟·약정을 못 찾았을 때. '없음'과 '이 축 미수록'을 구분한다."""
    if not spec_name.startswith(("n1", "n2", "n4", "e2", "c2", "c3", "c4", "t2",
                                 "a2", "a3", "g2", "m1", "m2", "m3", "z3")):
        return (
            f"No spot or commitment data for '{spec_name}'. This supplement is "
            "**GCP-only** (Cyclenerd). AWS and Azure spot are not included here."
        )
    return (
        f"Could not find spot or commitment data for {spec_name}. The GCP spot and "
        "commitment supplement may not be built "
        "(python -m costkb build-gcp-pricing), or the spec may not be in the mirror."
    )


def estimate_monthly_cost(
    hourly_usd: float,
    count: int = 1,
    hours_per_month: float = HOURS_PER_MONTH,
    running_total_usd: float = 0.0,
) -> str:
    """시간당 단가로 월 비용을 계산해 텍스트로 반환한다.

    `running_total_usd`는 **앞서 계산한 다른 구성요소의 월 비용 합**이다. 주면
    이번 구성요소를 더한 누적 합계까지 함께 낸다.

    **왜 합계를 여기서 내나.** 이 도구는 구성요소 하나만 계산했고 합계를 내는
    도구는 없었다. 그런데 지시문은 "구성요소별·합계를 도구로 계산하라"와 "직접
    암산하지 마라"를 동시에 요구했다 — 모델은 불가능한 요구를 받고 더할 수밖에
    없었고, 그 합계는 어떤 도구 출력에도 없는 값이 됐다(실측 RS2: $285.28).
    **사용자가 예산을 대는 바로 그 숫자**라 근거가 없으면 안 된다.
    """
    per_node = hourly_usd * hours_per_month
    total = per_node * count
    line = (
        f"Estimated monthly cost: ${total:,.2f} "
        f"(${per_node:,.2f} per node × {count} nodes, at {hours_per_month:.0f}h/month). "
    )
    if running_total_usd:
        line += (
            f"Running total across components: "
            f"${running_total_usd + total:,.2f} "
            f"(${running_total_usd:,.2f} so far + ${total:,.2f} for this one). "
        )
    return line + _COST_DISCLAIMER
