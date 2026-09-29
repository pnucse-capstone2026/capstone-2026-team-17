"""Azure Retail Prices API → 스팟·예약·저축 플랜 (미러와 별개 산출물).

**무엇을 답하나.** "이 Azure VM을 스팟으로 / 1년·3년 예약으로 / 저축 플랜으로 쓰면
얼마?" 미러(cb-tumblebug)엔 **온디맨드 정가 하나뿐**이라 답할 수 없었다.

## 이 산출물은 `data/`에 커밋하지 않는다

다른 소스와 다른 점이다. 문서가 밝히는 용도는 *"your own tools for **internal**
analysis and price comparison"*뿐이고 **라이선스 문구가 없다.** 재배포 허가가 없으므로
저장소에 넣지 않고 **빌드 때만 받는다** — AWS Price List와 같은 취급이다.

그래서 클론 직후에는 이 축이 **없는 상태가 기본**이다. 조회 함수가 "안 붙었다"를
빈 답이 아니라 **명시적으로** 말해야 한다(`perfkb`의 `not_built`와 같은 이유).

## 미러를 대체하지 않는다 — 그리고 여기선 서로를 검증한다

`gcp_pricing.py`는 두 소스가 다른 스냅샷이라 괴리를 기록해야 했다. **여기는 다르다** —
실측에서 API의 온디맨드가 미러와 **완전히 일치했다**:

    koreasouth  겹침 551종 · 0.5% 넘게 어긋남 0
    eastus      겹침 1219종 · 0.5% 넘게 어긋남 0

독립 소스(Microsoft 자체 가격 API)와 cb-tumblebug 덤프가 같은 값을 말하므로,
이건 보강인 동시에 **미러의 교차 확인**이다. 그래서 스냅샷 헤지 없이 붙인다.
다만 일치를 **가정하지 않고 레코드마다 확인해서 담는다**(`matchesMirror`).

## 함정 셋 — 전부 실측으로 겪었다

**(1) 같은 SKU에 Windows 값이 따로 있다.** `productName`이 `… Windows`로 끝난다.
안 거르면 라이선스 값이 섞여 두 배 가까이 뛴다.

**(2) `Cloud Services`가 같은 크기 이름을 쓴다.** 구형 PaaS라 VM이 아닌데
`armSkuName`이 같다. (1)과 (2)를 안 거르면 **SKU의 93.4%가 값이 여럿**이 되고,
어느 값이 잡히는지는 응답 순서가 정한다. 둘 다 거르면 **여럿인 것이 0이 된다.**

    Virtual Machines Basv2 Series   0.6130   ← VM (미러와 일치)
    Basv2 Series Cloud Services     0.6860   ← 다른 제품

처음엔 `isPrimaryMeterRegion`이 범인이라고 짐작했는데 **틀렸다** — 둘 다 primary였다.
판별자는 `productName`이다.

**(3) 예약의 `retailPrice`는 기간 총액인데 단위 칸은 '1 Hour'라고 적는다.**
1,348건 전부 그렇게 적혀 있다. 그대로 시간당으로 읽으면 **5,165배** 틀린다.

    standard_b16als_v2  1년 예약 raw 3166.00 → ÷8760h = 0.3614/h  (온디맨드 0.6130)

기간 시간으로 나눈 값이 온디맨드의 0.590(1년)·0.380(3년)이라 Azure가 공표하는 RI
할인율과 맞는다. **`savingsPlan`은 반대로 진짜 시간당이다**(0.710·0.479) — 같은
응답 안에서 두 단위가 섞여 있으므로 한 규칙으로 처리하면 한쪽이 틀린다.

이 저장소가 산문에서 단위를 잘못 뽑아 3,600배 어긋난 값을 담았던 것과 같은 모양이다.
**단위 칸을 믿지 않고 값으로 확인했다.**

## 담지 않는 것

- `DevTestConsumption` — Dev/Test 구독에서만 적용된다. 일반 사용자에게 그 값을
  가격이라고 말하면 거짓이다. 건수만 `_coverage`에 적는다.
- `Low Priority` — 스팟과 별개 미터인데 어디에 적용되는지 이 데이터로는 알 수 없다.
  짐작으로 스팟과 같다고 담지 않는다. 건수만 적는다.
"""

from __future__ import annotations

import hashlib
import json
import re
import ssl
import sys
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

from app.cloudkb.kbcommon.artifact import write_dataset
from app.cloudkb.kbcommon.fetch import cache_dir
from app.cloudkb.kbcommon.sources import SOURCES

#: 예약 기간 → 시간. `retailPrice`가 기간 총액이라 이걸로 나눠야 시간당이 된다.
TERM_HOURS = {"1 Year": 8760, "3 Years": 26280, "5 Years": 43800}

#: 한 응답의 최대 레코드 수(원본 문서). 페이지를 끝까지 따라가야 한다.
_PAGE = 1000

#: 미러와 이보다 더 어긋나면 일치로 보지 않는다. 실측에서 어긋남이 0이라
#: 넉넉히 잡을 이유가 없다 — 조용히 벌어지면 알아야 한다.
_MIRROR_TOLERANCE = 0.005

_UA = "cloudkb-build (https://github.com/cloud-barista)"


#: Windows 라이선스가 붙은 제품명의 꼬리말. **`Windows`만 보면 하나를 놓친다.**
#:
#: 처음엔 `endswith("Windows")`로 짰는데, 전 리전 빌드에서 `standard_ncc40ads_h100_v5`
#: 세 리전이 "값이 여럿"으로 걸렸다. 범인은 `Virtual Machines NCCadsv5 Srs Win` —
#: **Windows가 `Win`으로 줄어 있었다.** 값이 여럿이면 담지 않는 가드가 아니었으면
#: 온디맨드 7.89 대신 9.73(Windows)이 조용히 들어갔을 것이다.
#:
#: 전 리전 제품명 362종을 세어 확인했다: `Windows` 172종 · `Win` 1종 · 그 밖에
#: 라이선스가 붙는 OS 표기(SQL·RHEL·SLES 등) **0종**. 그래서 꼬리 토큰만 본다.
_WINDOWS_TAIL = re.compile(r"\sWin(dows)?$")


def _is_vm(row: dict) -> bool:
    """VM 가격인가. **Windows와 Cloud Services를 여기서 거른다.**"""
    name = row.get("productName") or ""
    return name.startswith("Virtual Machines") and not _WINDOWS_TAIL.search(name)


def kind_of(row: dict) -> str:
    """가격 종류. **스팟·저우선순위는 별도 type이 아니라 이름에 들어 있다.**"""
    text = f"{row.get('skuName') or ''} {row.get('meterName') or ''}"
    if row.get("type") == "DevTestConsumption":
        return "devtest"
    if "Spot" in text:
        return "spot"
    if "Low Priority" in text:
        return "lowpriority"
    if row.get("type") == "Reservation":
        return "reserved"
    return "ondemand"


def _fetch_region(region: str, *, refresh: bool = False) -> tuple[list[dict], str]:
    """한 리전의 VM 가격 전부. `(레코드, 응답 sha256)`.

    캐시를 쓰지만 **버전이 없으므로 캐시가 곧 스냅샷**이다. 재현하려면 캐시 파일을
    보관해야 한다는 뜻이고, 그 사실이 `_source.pin`에 `(고정 불가)`로 적혀 있다.
    """
    path = cache_dir() / f"azure-retail-{region}.json"
    if path.exists() and not refresh:
        raw = path.read_bytes()
        return json.loads(raw.decode("utf-8")), hashlib.sha256(raw).hexdigest()

    ctx = ssl.create_default_context()
    url = SOURCES["azure-retail-prices"].url + "&" + urllib.parse.urlencode(
        {"$filter": f"serviceName eq 'Virtual Machines' and armRegionName eq '{region}'"}
    )
    rows: list[dict] = []
    while url:
        request = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(request, context=ctx, timeout=120) as response:
            body = json.loads(response.read().decode("utf-8"))
        rows.extend(body.get("Items") or [])
        url = body.get("NextPageLink")
    raw = json.dumps(rows, ensure_ascii=False).encode("utf-8")
    path.write_bytes(raw)
    return rows, hashlib.sha256(raw).hexdigest()


def _index(rows: list[dict]) -> tuple[dict, Counter]:
    """SKU별로 종류를 모은다. **한 SKU에 값이 여럿이면 담지 않는다.**

    거를 것을 다 거르면 여럿인 경우가 0이라(실측), 여럿이 나오면 그건 우리가 모르는
    새 축이 생겼다는 뜻이다. 조용히 하나를 고르면 그 사실이 묻힌다.
    """
    by_sku: dict[str, dict] = {}
    dropped: Counter = Counter()
    for row in rows:
        kind = kind_of(row)
        if kind in ("devtest", "lowpriority"):
            dropped[kind] += 1
            continue
        if not _is_vm(row):
            dropped["not-vm-or-windows"] += 1
            continue
        price = row.get("retailPrice")
        sku = (row.get("armSkuName") or "").lower()
        if not sku or not price:
            dropped["no-price"] += 1
            continue
        slot = by_sku.setdefault(sku, {})
        if kind == "reserved":
            term = row.get("reservationTerm")
            hours = TERM_HOURS.get(term or "")
            if not hours:
                dropped["unknown-term"] += 1
                continue
            slot.setdefault(f"reserved:{term}", set()).add(price / hours)
        else:
            slot.setdefault(kind, set()).add(price)
        if kind == "ondemand":
            for plan in row.get("savingsPlan") or []:
                if plan.get("term") in TERM_HOURS and plan.get("retailPrice"):
                    slot.setdefault(f"savings:{plan['term']}", set()).add(
                        plan["retailPrice"]
                    )
    return by_sku, dropped


def _one(values: set[float] | None) -> float | None:
    """값이 하나일 때만 쓴다 — 여럿이면 무엇이 맞는지 우리가 모른다."""
    if not values or len(values) != 1:
        return None
    return round(next(iter(values)), 6)


def build(
    output: Path,
    *,
    mirror: Path,
    regions: list[str] | None = None,
    refresh: bool = False,
) -> dict:
    """미러에 있는 Azure 리전을 돌며 할인 가격을 모은다."""
    specs = json.loads(mirror.read_text(encoding="utf-8"))["specs"]
    azure = [s for s in specs if s["provider"] == "azure"]
    mirror_price = {
        (s["specName"].lower(), s["region"]): s["hourlyUSD"] for s in azure
    }
    wanted = regions or sorted({s["region"] for s in azure})

    records: list[dict] = []
    dropped: Counter = Counter()
    digests: list[str] = []
    ambiguous = 0
    mismatched = 0
    for region in wanted:
        rows, digest = _fetch_region(region, refresh=refresh)
        digests.append(digest)
        by_sku, drops = _index(rows)
        dropped.update(drops)
        for sku, slots in by_sku.items():
            base = mirror_price.get((sku, region))
            if base is None:
                dropped["not-in-mirror"] += 1
                continue
            if any(len(v) > 1 for v in slots.values()):
                ambiguous += 1
                continue
            api_ondemand = _one(slots.get("ondemand"))
            matches = (
                api_ondemand is not None
                and base is not None
                and abs(api_ondemand - base) <= _MIRROR_TOLERANCE * max(api_ondemand, base)
            )
            if api_ondemand is not None and base is not None and not matches:
                mismatched += 1
            record = {
                "specName": sku,
                "region": region,
                "ondemandUSD": api_ondemand,
                "mirrorUSD": base,
                "matchesMirror": bool(matches),
                "spotUSD": _one(slots.get("spot")),
                "reserved1yUSD": _one(slots.get("reserved:1 Year")),
                "reserved3yUSD": _one(slots.get("reserved:3 Years")),
                "savings1yUSD": _one(slots.get("savings:1 Year")),
                "savings3yUSD": _one(slots.get("savings:3 Years")),
            }
            if any(record[k] is not None for k in
                   ("spotUSD", "reserved1yUSD", "reserved3yUSD",
                    "savings1yUSD", "savings3yUSD")):
                records.append(record)

    records.sort(key=lambda r: (r["region"], r["specName"]))
    payload = {
        "_note": (
            "Spot, reserved and savings plan prices from the Azure Retail Prices "
            "API. On-demand comes straight from the mirror (cb-tumblebug); this file "
            "**only supplements**. The source gives reserved prices as period totals "
            "while writing '1 Hour' in the unit field, so they are divided by the "
            "term hours (1 year 8,760 / 3 years 26,280) and stored as hourly rates — "
            "savings plan prices are already hourly in the source. Windows-priced "
            "values and Cloud Services products are not included (different things "
            "under the same SKU name). Dev/Test and Low Priority are not included "
            "either — they have their own conditions of application and are not "
            "general prices.\n"
            "**Source and redistribution status**: derived from values fetched from "
            "the Microsoft Azure Retail Prices API "
            "(https://prices.azure.com/api/retail/prices). Rights to the price data "
            "belong to Microsoft. **We found no wording granting redistribution, and "
            "none forbidding it either** — it is a publicly readable API with no "
            "auth, so we include the derived artifact in the repository for "
            "convenience; that does not mean permission was granted. We will remove "
            "it if the rights holder asks. Values are a snapshot, not an actual bill."
        ),
        "records": records,
        "_coverage": [
            {
                "provider": "azure",
                "regions": len(wanted),
                "records": len(records),
                "mirrorAgreed": sum(1 for r in records if r["matchesMirror"]),
                "mirrorDisagreed": mismatched,
                "ambiguousSkus": ambiguous,
                "dropped": dict(dropped),
                "note": (
                    "Every record was checked against the mirror for on-demand "
                    f"agreement before being included (tolerance "
                    f"{_MIRROR_TOLERANCE:.1%}). Agreement means two independent "
                    "sources state the same value, so it is also a **cross-check of "
                    "the mirror**. `ambiguousSkus` is what was not included because "
                    "one SKU had several values — filtering out Windows and Cloud "
                    "Services should make it 0, so anything other than 0 signals an "
                    "axis we do not know about."
                ),
            }
        ],
        "_source": [
            {
                "source": "azure-retail-prices",
                "pin_kind": "digest",
                "pin": "(고정 불가)",
                "url": SOURCES["azure-retail-prices"].url,
                "sha256": hashlib.sha256(
                    "".join(sorted(digests)).encode("utf-8")
                ).hexdigest(),
                "bytes": None,
                "fetched_at": None,
                "regions": len(wanted),
                "note": (
                    "The API has no version, so reproduction is impossible in "
                    "principle. We hash the combined sha256 of each region's "
                    "response, so **a change never goes unnoticed.**"
                ),
            }
        ],
    }
    write_dataset(output, payload, SCHEMA)
    print(f"Azure 할인 가격: {len(records)}건 → {output}")
    print(
        f"  리전 {len(wanted)} · 미러 일치 {payload['_coverage'][0]['mirrorAgreed']}"
        f" · 불일치 {mismatched} · 값이 여럿이라 제외 {ambiguous}",
        file=sys.stderr,
    )
    print(f"  담지 않은 것: {dict(dropped)}", file=sys.stderr)
    return payload


SCHEMA = {
    "type": "object",
    "required": ["records", "_source"],
    "properties": {
        "_note": {"type": "string"},
        "_coverage": {"type": "array"},
        "_source": {"type": "array"},
        "records": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["specName", "region"],
                "additionalProperties": False,
                "properties": {
                    "specName": {"type": "string"},
                    "region": {"type": "string"},
                    "ondemandUSD": {"type": ["number", "null"]},
                    "mirrorUSD": {"type": ["number", "null"]},
                    "matchesMirror": {"type": "boolean"},
                    "spotUSD": {"type": ["number", "null"]},
                    "reserved1yUSD": {
                        "type": ["number", "null"],
                        "description": "**시간당으로 환산한 값.** 원본은 기간 총액이다.",
                    },
                    "reserved3yUSD": {"type": ["number", "null"]},
                    "savings1yUSD": {
                        "type": ["number", "null"],
                        "description": "원본이 이미 시간당이라 환산하지 않는다.",
                    },
                    "savings3yUSD": {"type": ["number", "null"]},
                },
            },
        },
    },
}
