"""Cyclenerd GCP 가격표 → 스팟·약정 가격 (미러와 별개 산출물).

**무엇을 답하나.** "이 GCP 인스턴스를 스팟으로 / 1년·3년 약정으로 쓰면 얼마?"
우리 미러(cb-tumblebug)엔 **온디맨드 정가 하나뿐**이라 이 질문에 답할 수 없었다.

## 미러를 대체하지 않는다 — 보강만 한다

costkb의 본체는 cb-tumblebug 미러이고, 그건 라이브 MCP 경로(`recommend_vm_spec`)와
같은 세계를 봐야 한다(`costkb/parsers/tumblebug.py` 참조). 그래서 이 파서는:

- **온디맨드는 건드리지 않는다.** tumblebug의 `hourlyUSD`가 그대로 남는다.
- 스팟·약정만 **별도 산출물**(`gcp-spot-commit.json`)에 담는다. 미러 파일은 읽기만
  하고 쓰지 않는다.

## 두 소스는 다른 스냅샷이다 — 그래서 괴리를 기록한다

Cyclenerd의 온디맨드(`hour`)는 tumblebug와 **리전×패밀리 단위로 어긋난다**(실측:
`n2d`/`asia-south1` 전 크기가 정확히 2.405배, `g4`/`asia-south2`가 0.385배). 크기마다
배율이 같으므로 이건 **가격 스냅샷 시점 차이**이지 다른 스펙을 가리키는 게 아니다
(다른 스펙이면 크기마다 배율이 제각각일 것이다). Cyclenerd는 2026-07-16 생성이고
tumblebug 덤프는 다른 시점이다.

Cyclenerd **자기 안에서는** 완전히 정합적이다(실측: 스팟이 온디맨드의 32%, 1년 63%,
3년 45% — GCP 공식 할인율과 일치, 스팟>온디맨드 이상치 0건). 그래서 스팟·약정 값
자체는 믿을 수 있다.

문제는 두 스냅샷을 **섞으면** 안 된다는 것이다. tumblebug 온디맨드에 Cyclenerd 스팟을
나란히 놓으면 "온디맨드 $0.17인데 스팟 $0.044(=26%)"처럼 보이지만, 그 스팟은
Cyclenerd 온디맨드 $0.147 기준이다. 그래서 레코드에 **Cyclenerd 온디맨드(`hourRefUSD`)를
함께 담고**, tumblebug와의 비율(`mirrorRatio`)을 기록해 스냅샷이 어긋난 곳을 밝힌다.

## 조인되는 것만 담는다

미러의 gcp 스펙 `(specName, region)`에 붙는 것만 담는다. 미러에 없는 스펙·리전은
답할 길이 없으므로 세기만 한다.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from app.cloudkb.kbcommon.artifact import write_dataset
from app.cloudkb.kbcommon.fetch import describe_source, fetch_cached
from app.cloudkb.kbcommon.sources import SOURCES

try:  # C 로더가 3.8MB YAML을 10배 빠르게 읽는다
    _Loader = yaml.CSafeLoader
except AttributeError:  # pragma: no cover
    _Loader = yaml.SafeLoader

#: 미러와 이보다 더 어긋나면 "다른 스냅샷"으로 표시한다. Cyclenerd 자기정합성은
#: 그대로이므로 값을 버리진 않고, 스냅샷이 다르다는 사실만 레코드에 남긴다.
_MIRROR_TOLERANCE = 0.05

SCHEMA = {
    "type": "object",
    "required": ["records", "_source"],
    "properties": {
        "_note": {"type": "string"},
        "_source": {"type": "array"},
        "records": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["provider", "specName", "region"],
                "additionalProperties": False,
                "properties": {
                    "provider": {"const": "gcp"},
                    "specName": {"type": "string", "minLength": 1},
                    "region": {"type": "string", "minLength": 1},
                    "hourSpotUSD": {"type": ["number", "null"]},
                    "month1yUSD": {"type": ["number", "null"]},
                    "month3yUSD": {"type": ["number", "null"]},
                    # Cyclenerd 자신의 온디맨드. 스팟·약정이 어느 기준인지 밝힌다.
                    "hourRefUSD": {"type": ["number", "null"]},
                    # tumblebug 온디맨드 ÷ Cyclenerd 온디맨드. 1.0에서 멀면 스냅샷이 다르다.
                    "mirrorRatio": {"type": ["number", "null"]},
                    "snapshotMatchesMirror": {"type": "boolean"},
                },
            },
        },
    },
}


def _load_pricing(path: Path) -> dict:
    with open(path, encoding="utf-8") as handle:
        return yaml.load(handle, Loader=_Loader)


def build_records(pricing: dict, mirror_specs: list[dict]) -> tuple[list[dict], dict]:
    """Cyclenerd 가격을 미러의 gcp 스펙에 조인해 레코드를 만든다.

    미러는 **읽기만** 한다. 반환값은 (레코드, 통계)다.
    """
    instances = (pricing.get("compute") or {}).get("instance") or {}
    stats = {
        "mirror_gcp": 0, "joined": 0, "missing_spec": 0, "missing_region": 0,
        "snapshot_diverged": 0, "spot": 0, "commit1y": 0, "commit3y": 0,
    }
    records: list[dict] = []

    for spec in mirror_specs:
        if spec.get("provider") != "gcp":
            continue
        stats["mirror_gcp"] += 1
        name, region = spec["specName"], spec["region"]
        entry = instances.get(name)
        if not entry:
            stats["missing_spec"] += 1
            continue
        cost = (entry.get("cost") or {}).get(region)
        if not cost:
            stats["missing_region"] += 1
            continue
        stats["joined"] += 1

        ref = cost.get("hour")
        tb_hour = spec.get("hourlyUSD")
        ratio = None
        matches = True
        if ref and tb_hour:
            ratio = round(tb_hour / ref, 4)
            matches = (1 - _MIRROR_TOLERANCE) <= ratio <= (1 + _MIRROR_TOLERANCE)
            if not matches:
                stats["snapshot_diverged"] += 1

        spot = cost.get("hour_spot")
        m1 = cost.get("month_1y")
        m3 = cost.get("month_3y")
        stats["spot"] += bool(spot)
        stats["commit1y"] += bool(m1)
        stats["commit3y"] += bool(m3)

        records.append({
            "provider": "gcp",
            "specName": name,
            "region": region,
            "hourSpotUSD": spot,
            "month1yUSD": m1,
            "month3yUSD": m3,
            "hourRefUSD": ref,
            "mirrorRatio": ratio,
            "snapshotMatchesMirror": matches,
        })
    return records, stats


def build(output: Path, *, refresh: bool = False) -> dict:
    from app.cloudkb.costkb.dataset import load_specs

    source = SOURCES["cyclenerd-gcp-pricing"]
    path = fetch_cached(
        source.url, f"cyclenerd-gcp-pricing-{source.pin}.yml", refresh=refresh
    )
    pricing = _load_pricing(path)

    mirror = load_specs()  # 미러를 읽기만 한다
    records, stats = build_records(pricing, mirror)

    dataset = {
        "_note": (
            "GCP spot and commitment pricing (Cyclenerd, Apache-2.0). A supplement "
            "to the costkb mirror; on-demand comes from tumblebug. These values are "
            "a different snapshot, so wherever on-demand diverges from the mirror "
            "(snapshotMatchesMirror=false) spot and commitment are based on a "
            "different price world too."
        ),
        "_source": [describe_source(path, source.key)],
        "records": records,
    }
    write_dataset(output, dataset, SCHEMA)

    print(
        f"gcp-pricing: 스팟·약정 {len(records):,}건 "
        f"(미러 gcp {stats['mirror_gcp']:,}종 중 조인 {stats['joined']:,})"
    )
    print(
        f"  채움: 스팟 {stats['spot']:,} · 1년 {stats['commit1y']:,} · "
        f"3년 {stats['commit3y']:,}"
    )
    if stats["missing_spec"] or stats["missing_region"]:
        print(
            f"  못 붙음: 이름 없음 {stats['missing_spec']:,} · "
            f"리전 없음 {stats['missing_region']:,}"
        )
    if stats["snapshot_diverged"]:
        pct = stats["snapshot_diverged"] / stats["joined"] * 100
        print(
            f"  ⚠ 온디맨드가 미러와 5% 넘게 어긋난 곳 {stats['snapshot_diverged']:,}건 "
            f"({pct:.0f}%) — 스냅샷 시점 차이. 스팟·약정은 Cyclenerd 온디맨드 기준이다."
        )
    return dataset
