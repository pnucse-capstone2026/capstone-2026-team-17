"""스펙/가격 데이터셋 로드 및 필터 유틸.

**두 가지 소스**:
1. `output/tumblebug-cost.json` — `costkb build`가 cb-tumblebug 덤프에서 만든 미러(73k행).
   있으면 이쪽을 쓴다.
2. `specs.json` — 번들된 손 큐레이션 36건. 빌드 안 해도 서버·크레덴셜 없이 즉시 동작한다.

**왜 이 소스인가**: 열 개 CSP의 스펙·가격을 한 스키마로 모아 둔 공개 덤프라, 프로바이더
간 비교가 가능한 유일한 출처다. 각 CSP 공개 API를 따로 긁으면 필드가 제각각이라
"같은 조건으로 비교"가 안 된다.

**다만 우리는 미러의 노예가 아니다.** 이 덤프에는 상위 CB-Spider 버그가 실려 있어
GCP·Azure 메모리가 실제보다 2.4% 낮다(73,083건 중 46,468건). 우리는 가이드라인
지식베이스이므로 **빌드가 고쳐서 담는다** — `memGiB`가 곧 실제 값이다. 무엇을 어떻게
고쳤는지는 데이터셋의 `_corrections`에 규칙으로 남고, 그 식으로 원본을 되돌릴 수 있다.
(자세한 건 `parsers/tumblebug.py` 참고.)

`specs.json`은 손으로 편집하는 파일이라 오타(예: `memGib`)가 들어가기 쉽다. 예전에는
검증이 없어서 그런 오타가 로드 시점이 아니라 `filter_specs` 안에서 `KeyError`로 터졌다.
그래서 로드할 때 번들 스키마로 1회 검증한다 — `@lru_cache` 덕에 비용은 프로세스당 한 번이다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import jsonschema

from app.cloudkb.kbcommon import artifact
from app.cloudkb.kbcommon.artifact import REBUILD_HINT, read_dataset

_SPECS_PATH = Path(__file__).with_name("specs.json")
_SCHEMA_PATH = Path(__file__).with_name("schema.json")

DEFAULT_OUTPUT_DIR = Path("output")
BUILT_FILENAME = "tumblebug-cost.json"
#: GCP 스팟·약정 보강(Cyclenerd). 미러와 별개 파일이라 없을 수 있다.
SPOT_COMMIT_FILENAME = "gcp-spot-commit.json"
#: Azure 스팟·예약·저축 플랜 보강(Retail Prices API).
#: **`data/`에 커밋하지 않는 유일한 산출물이다** — 재배포 허가가 없어서다.
#: 그래서 "없을 수 있다"가 아니라 **없는 것이 기본**이다.
AZURE_DISCOUNT_FILENAME = "azure-discount-pricing.json"

# MCP의 recommend_vm_spec은 호출자가 architecture를 안 주면 x86_64를 끼워넣는다
# (tb-mcp.py). 이걸 미러하지 않으면 MCP가 감추는 arm64 스펙(덤프 기준 7,790건)이
# 우리에게만 보여 결과가 갈린다.
DEFAULT_ARCHITECTURE = "x86_64"

def _tiebreak(spec: dict) -> tuple[str, str, str]:
    """동점일 때의 순서. **없으면 파일 순서가 답을 정한다.**

    `sort_by='vcpu'`는 vCPU만 보므로 2 vCPU짜리 수천 건이 전부 동점이고, 그중 어느
    5개가 나오는지는 덤프의 행 순서가 정했다. 같은 핀에서는 재현되지만 **왜 그
    5개인지 설명할 수 없는 답**이고, 다음 빌드에서 조용히 바뀐다.

    id를 안 쓰는 이유: costkb 번들 36건에는 id가 없다(결함 C3의 원인이기도 하다).
    프로바이더·이름·리전은 항상 있고 사람이 읽어도 납득이 된다.
    """
    return (spec["provider"], spec["specName"], spec["region"])


_SORT_KEYS = {
    # 가격 미상(None)은 맨 뒤로 — Tumblebug의 `CASE WHEN cost_per_hour > 0 ... ELSE 999999`와 같은 취지.
    "cost": lambda s: (s["hourlyUSD"] is None, s["hourlyUSD"] or 0, *_tiebreak(s)),
    "vcpu": lambda s: (-s["vCPU"], *_tiebreak(s)),
    "memory": lambda s: (-s["memGiB"], *_tiebreak(s)),
}


@lru_cache(maxsize=1)
def _schema() -> dict:
    """번들된 JSON Schema를 로드한다."""
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def _load_validated(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    jsonschema.validate(data, _schema())
    return data


@dataclass(frozen=True)
class _Loaded:
    data: dict
    warning: str | None = None


def _resolve(output_dir: Path | str | None) -> str:
    """None이면 그때그때 DEFAULT_OUTPUT_DIR을 읽는다.

    기본 인자로 박아두면 def 시점에 바인딩돼 테스트에서 갈아끼울 수 없다.
    """
    return str(DEFAULT_OUTPUT_DIR if output_dir is None else output_dir)


@lru_cache(maxsize=4)
def _load_cached(output_dir: str) -> _Loaded:
    """빌드 산출물이 있으면 그것을, 없거나 **깨졌으면** 번들을 로드한다.

    깨진 산출물에 예외를 던지면 번들 폴백 경로가 통째로 사라진다 — 빌드 한 번
    실패하면 `output/`를 손으로 지우기 전까지 KB가 영구 정지했다(결함 C2).
    폴백하되 조용히 하지는 않는다: 사용자는 자기가 지금 36건짜리 번들을 보고
    있다는 걸 알아야 한다.
    """
    # output/ 이 먼저, 없으면 저장소에 커밋된 data/*.gz (kbcommon/artifact.py).
    found = artifact.resolve(output_dir, BUILT_FILENAME)
    data, error = read_dataset(found or Path(output_dir) / BUILT_FILENAME, _schema())
    if data is not None:
        return _Loaded(data)
    bundle = _load_validated(_SPECS_PATH)  # 번들이 깨졌으면 그건 진짜 버그다
    if error is None:
        return _Loaded(bundle)
    return _Loaded(
        bundle,
        f"The build artifact is unusable, so this answers from the bundled "
        f"{len(bundle['specs'])} specs ({error}). {REBUILD_HINT}: "
        "python -m costkb build",
    )


def schema() -> dict:
    """번들된 JSON Schema — 빌드가 쓰기 전 검증에 쓴다."""
    return _schema()


@lru_cache(maxsize=4)
def _load_spot_commit(output_dir: str) -> dict[tuple[str, str], dict]:
    """`(specName, region)` → 스팟·약정 레코드. 없으면 빈 맵.

    미러와 별개 파일이라 보강이 안 됐을 수 있다 — 그때는 빈 맵을 돌려주고, 부르는
    쪽이 "이 축은 안 붙었다"고 답한다. 미러 로드와 달리 번들 폴백이 없다(GCP 전용
    보강이라 폴백할 손 큐레이션이 없다).
    """
    return {
        (r["specName"], r["region"]): r
        for r in artifact.load_records(output_dir, SPOT_COMMIT_FILENAME)
    }


@lru_cache(maxsize=4)
def _load_azure_discount(output_dir: str) -> dict[tuple[str, str], dict]:
    """`(specName, region)` → Azure 스팟·예약·저축 플랜. 없으면 빈 맵.

    **이 파일은 `data/`에 없는 것이 정상이다.** Azure Retail Prices API는 재배포
    허가가 없어 산출물을 커밋하지 않으므로, 클론 직후에는 빈 맵이 정상 상태다.
    GCP 쪽과 달리 "빌드를 안 했다"가 예외가 아니라 기본이라, 부르는 쪽이 그 사실을
    **빈 답이 아니라 안내로** 말해야 한다.

    스펙 이름은 소문자로 색인한다 — API의 `armSkuName`은 `Standard_D2s_v5`이고
    미러도 같은 표기지만, 대소문자로 조인이 깨진 적이 세 번 있었다(kt·ncp·nhn 리전).
    """
    return {
        (r["specName"].lower(), r["region"]): r
        for r in artifact.load_records(output_dir, AZURE_DISCOUNT_FILENAME)
    }


def azure_discount_for(
    spec_name: str, region: str, output_dir: Path | str | None = None
) -> dict | None:
    """한 Azure 스펙·리전의 할인 레코드. 없으면 None."""
    return _load_azure_discount(_resolve(output_dir)).get(
        (spec_name.strip().lower(), region.strip())
    )


def azure_discount_regions(
    spec_name: str, output_dir: Path | str | None = None
) -> list[dict]:
    """한 Azure 스펙이 할인 값을 가진 모든 리전의 레코드."""
    wanted = spec_name.strip().lower()
    index = _load_azure_discount(_resolve(output_dir))
    return [r for (name, _), r in index.items() if name == wanted]


def azure_discount_built(output_dir: Path | str | None = None) -> bool:
    """할인 산출물이 있나. **없는 것이 기본**이라 부르는 쪽이 확인해야 한다."""
    return bool(_load_azure_discount(_resolve(output_dir)))


def spot_commit_for(
    spec_name: str, region: str, output_dir: Path | str | None = None
) -> dict | None:
    """한 스펙·리전의 스팟·약정 레코드. 없으면 None."""
    return _load_spot_commit(_resolve(output_dir)).get((spec_name, region))


def spot_commit_regions(
    spec_name: str, output_dir: Path | str | None = None
) -> list[dict]:
    """한 스펙이 스팟·약정을 가진 모든 리전의 레코드."""
    index = _load_spot_commit(_resolve(output_dir))
    return [r for (name, _), r in index.items() if name == spec_name]


def clear_caches() -> None:
    """로드·스키마 캐시를 비운다 (테스트가 output_dir을 갈아끼울 때)."""
    _load_cached.cache_clear()
    _load_spot_commit.cache_clear()
    _load_azure_discount.cache_clear()
    _schema.cache_clear()


def load_dataset(output_dir: Path | str | None = None) -> dict:
    """데이터셋 전체를 로드하고 스키마로 검증한다(캐시됨).

    산출물이 깨져 있으면 번들로 폴백한다 — 그 사실은 `load_warning()`으로 알 수 있다.
    """
    return _load_cached(_resolve(output_dir)).data


def load_warning(output_dir: Path | str | None = None) -> str | None:
    """산출물이 깨져 번들로 폴백했다면 그 설명. 정상이면 None.

    응답에 붙이는 용도 — 조용한 폴백은 "커버리지가 왜 갑자기 좁아졌지?"를 미궁으로 만든다.
    """
    return _load_cached(_resolve(output_dir)).warning


def load_specs(output_dir: Path | str | None = None) -> list[dict]:
    """검증된 스펙 목록을 반환한다."""
    return load_dataset(output_dir)["specs"]


def dataset_note(output_dir: Path | str | None = None) -> str:
    """데이터셋의 출처·한계 고지 (레코드별 evidence를 대신한다)."""
    return load_dataset(output_dir)["_note"]


def is_built(output_dir: Path | str | None = None) -> bool:
    """빌드 산출물을 **실제로 쓰고 있는지** (아니면 번들 36건 폴백).

    파일 존재만 보면 안 된다 — 깨진 파일이 있을 때 "빌드됨"이라고 답하면
    커버리지 안내가 73k건 기준으로 나가는데 정작 답은 번들 36건에서 나온다.
    """
    return _load_cached(_resolve(output_dir)).warning is None and (
        artifact.resolve(_resolve(output_dir), BUILT_FILENAME) is not None
    )


def resolve_region(
    query: str, output_dir: Path | str | None = None
) -> tuple[frozenset[str], str]:
    """리전 질의어 → (걸릴 리전 코드들, 방식). 방식은 `exact` | `partial` | `none`.

    **정확 일치가 있으면 그것만 쓴다.** 예전엔 무조건 부분 일치라, 실존 리전 15쌍이
    서로의 부분문자열인 탓에 질의가 오염됐다:

        region='centralus'  1,234건이어야 하는데 4,120건 (north/south/westcentralus)
        region='us-east'      190건이어야 하는데 4,349건 (us-east-1/2, us-east1/4/5)

    `us-east`는 **그 자체로 실존 리전**이면서 다섯 리전의 접두사다 — 그래서 "부분 일치를
    없앤다"도 "부분 일치를 유지한다"도 답이 아니었다. 정확 일치를 우선하면 둘 다 산다.

    부분 일치는 정확 일치가 없을 때만 쓴다(`ap-northeast` → -1/-2/-3). 그때는 **여러
    리전을 섞었다는 사실을 답에 밝혀야 한다** — 방식을 함께 돌려주는 이유다.

    Tumblebug은 정확 비교만 한다. 이제 정확 일치 질의에서는 두 경로의 답이 같고,
    갈리는 것은 부분 일치로 떨어질 때뿐이다.
    """
    wanted = query.strip().lower()
    known = {s["region"].lower() for s in load_specs(output_dir)}
    if wanted in known:
        return frozenset({wanted}), "exact"
    hit = frozenset(r for r in known if wanted in r)
    return (hit, "partial") if hit else (frozenset(), "none")


def filter_specs(
    vcpu_min: int = 0,
    mem_min_gib: float = 0,
    provider: str | None = None,
    region: str | None = None,
    sort_by: str = "cost",
    limit: int = 5,
    *,
    architecture: str | None = DEFAULT_ARCHITECTURE,
    priced_only: bool = True,
    fold_regions: bool = True,
    require_accelerator: bool = False,
    output_dir: Path | str | None = None,
) ->list[dict]:
    """요구사항 조건으로 스펙을 필터링·정렬해 상위 결과를 반환한다.

    `memGiB`는 **보정된 실제 값**이다(빌드가 상위 버그를 고쳐서 담는다). 예전에는
    원본의 버그값으로 필터링했는데 — 라이브 MCP와 답을 맞추려던 배포기의 이유였다 —
    "16GiB 이상"에서 **실제로는 만족하는 3,765건이 조용히 빠졌다.**

    Args:
        vcpu_min: 최소 vCPU. 0이면 바운드 없음(Tumblebug도 0값 범위는 무시한다).
        mem_min_gib: 최소 메모리(GiB). 보정된 실제 값 기준.
        provider: 'aws' | 'gcp' | 'azure' 등 (대소문자 무시). None이면 전체.
        region: 리전 필터. **정확 일치가 있으면 그것만**, 없으면 부분 일치
            (`resolve_region`). 대소문자 무시. None이면 전체.
        sort_by: 'cost'(저렴한 순) | 'vcpu'(큰 순) | 'memory'(큰 순).
            알 수 없는 값이면 'cost'로 폴백한다.
        limit: 반환 개수. **0 이하도 1로 올린다** — 빈 목록을 돌려주면 호출부가
            "조건을 만족하는 스펙이 데이터셋에 없습니다"라고 답해 **거짓을 말하게**
            된다. 데이터가 없는 것과 0개를 요청받은 것은 다른 일이다.
        architecture: 기본 'x86_64' — MCP가 주입하는 것과 동일. None이면 전체.
        require_accelerator: True면 `acceleratorCount`가 1 이상인 후보만. GPU를 포함한
            가속기 전반이다(Inferentia·TPU 등도 걸린다) — 필드 이름 그대로 쓴다.
            **정렬·접기보다 먼저** 건다. 나중에 걸면 상위 N을 고른 뒤라 가속기 후보가
            이미 잘려 나간 상태가 된다.
        priced_only: True(기본)면 가격이 있는 후보만. 비용 정렬·판정이 성립해야 하므로.
        fold_regions: True(기본)면 같은 (프로바이더, 스펙명)이 리전만 달리해 여러 칸을
            먹지 않게 접는다. 접힌 리전은 `_foldedRegions`에 남는다.
        output_dir: 빌드 산출물 위치.
    """
    prov = provider.lower() if provider else None
    # 정확 일치 우선. 부분 일치로만 걸러 `centralus`가 northcentralus를 함께 물던
    # 자리다 — 자세한 건 `resolve_region`.
    regs = resolve_region(region, output_dir)[0] if region else None
    arch = architecture.lower() if architecture else None

    matched = [
        s
        for s in load_specs(output_dir)
        if s["vCPU"] >= vcpu_min
        and s["memGiB"] >= mem_min_gib
        and (prov is None or s["provider"] == prov)
        and (regs is None or s["region"].lower() in regs)
        # architecture를 모르는 레코드(번들 36건)는 거르지 않는다 — 정보 부재가
        # 배제 사유가 되면 빌드 전 폴백이 통째로 사라진다.
        and (arch is None or not s.get("architecture") or s["architecture"].lower() == arch)
        and (not priced_only or s["hourlyUSD"] is not None)
        and (not require_accelerator or (s.get("acceleratorCount") or 0) > 0)
    ]

    key = _SORT_KEYS.get(sort_by, _SORT_KEYS["cost"])
    matched.sort(key=key)
    if fold_regions:
        matched = _fold_regions(matched)
    return matched[: max(1, limit)]


def find_by_name(
    name: str, provider: str | None = None, output_dir: Path | str | None = None
) -> list[dict]:
    """스펙 **이름**으로 찾는다. 리전별 레코드를 전부 돌려준다.

    조건 필터(`filter_specs`)만 있고 이름 조회가 없어서 "n2-highmem-8 메모리 몇
    GiB?"에 답할 길이 없었다 — 실측에서 0/3 실패하고 web_search로 3/3 샜다.
    **데이터는 처음부터 있었고 표면이 없었을 뿐이다.**

    이름은 대체로 유일하다(실측: 5,248종 중 프로바이더가 겹치는 것은 `m1.*` 4종뿐,
    aws와 openstack). 겹치면 둘 다 돌려주고 고르는 일은 부르는 쪽에 맡긴다.
    """
    wanted = name.strip().lower()
    prov = provider.strip().lower() if provider else None
    return [
        spec
        for spec in load_specs(output_dir)
        if spec["specName"].lower() == wanted
        and (prov is None or spec["provider"] == prov)
    ]


def name_suggestions(
    name: str, limit: int = 5, output_dir: Path | str | None = None
) -> list[str]:
    """비슷한 스펙 이름. 오타·기억 착오를 막다른 길로 만들지 않는다."""
    import difflib

    names = {spec["specName"] for spec in load_specs(output_dir)}
    return difflib.get_close_matches(name.strip(), names, n=limit, cutoff=0.6)


def _fold_regions(specs: list[dict]) -> list[dict]:
    """같은 스펙이 리전만 달리해 여러 칸을 먹지 않게 접는다.

    실측: `vcpu_min=2, mem_min_gib=4, limit=6`이 `S5.MEDIUM4`·`BF1.MEDIUM4`를
    ap-chengdu/ap-chongqing 두 벌씩 돌려줘 **6칸에 실제 스펙은 3종**이었다.
    리전을 지정하지 않은 사용자는 선택지를 **비교**하려는 것이므로 같은 것을
    여러 번 보여주면 그만큼 다른 후보를 못 본다.

    정렬이 끝난 뒤에 부르므로 **먼저 오는 것이 곧 그 스펙의 최선**이다(비용 정렬이면
    최저가). 값은 고쳐 쓰지 않는다 — 미러 원칙대로 고른 레코드를 그대로 두고,
    접혔다는 사실만 `_` 접두사 키로 덧붙인다(`_source`·`_coverage`와 같은 관례).
    """
    best: dict[tuple[str, str], dict] = {}
    order: list[tuple[str, str]] = []
    for spec in specs:
        key = (spec["provider"], spec["specName"])
        if key not in best:
            chosen = dict(spec)
            chosen["_foldedRegions"] = []
            best[key] = chosen
            order.append(key)
        elif spec["region"] != best[key]["region"]:
            best[key]["_foldedRegions"].append(spec["region"])
    return [best[k] for k in order]


def count_unpriced(
    vcpu_min: int = 0,
    mem_min_gib: float = 0,
    provider: str | None = None,
    region: str | None = None,
    *,
    architecture: str | None = DEFAULT_ARCHITECTURE,
    output_dir: Path | str | None = None,
) ->int:
    """조건에 맞지만 가격이 없는 후보 수 — "라이브 가격은 MCP로" 안내에 쓴다."""
    return len(
        filter_specs(
            vcpu_min,
            mem_min_gib,
            provider,
            region,
            limit=10**9,
            architecture=architecture,
            priced_only=False,
            output_dir=output_dir,
        )
    ) - len(
        filter_specs(
            vcpu_min,
            mem_min_gib,
            provider,
            region,
            limit=10**9,
            architecture=architecture,
            priced_only=True,
            output_dir=output_dir,
        )
    )


def coverage(output_dir: Path | str | None = None) -> list[dict]:
    """데이터셋 커버리지 요약 — (provider, region)별 개수와 vCPU·메모리 범위.

    "조건을 만족하는 스펙이 없습니다"가 나올 때 경계를 확인하는 용도.
    """
    groups: dict[tuple[str, str], list[dict]] = {}
    for spec in load_specs(output_dir):
        groups.setdefault((spec["provider"], spec["region"]), []).append(spec)
    return [
        {
            "provider": provider,
            "region": region,
            "count": len(rows),
            "priced": sum(1 for r in rows if r["hourlyUSD"] is not None),
            "vcpu_min": min(r["vCPU"] for r in rows),
            "vcpu_max": max(r["vCPU"] for r in rows),
            "mem_min_gib": min(r["memGiB"] for r in rows),
            "mem_max_gib": max(r["memGiB"] for r in rows),
        }
        for (provider, region), rows in sorted(groups.items())
    ]


def provider_summary(output_dir: Path | str | None = None) -> list[dict]:
    """프로바이더별 요약 — 리전이 수십 개로 늘어난 뒤 coverage()가 너무 길어져서."""
    groups: dict[str, list[dict]] = {}
    for spec in load_specs(output_dir):
        groups.setdefault(spec["provider"], []).append(spec)
    return [
        {
            "provider": provider,
            "count": len(rows),
            "priced": sum(1 for r in rows if r["hourlyUSD"] is not None),
            "regions": len({r["region"] for r in rows}),
            "vcpu_max": max(r["vCPU"] for r in rows),
            "mem_max_gib": max(r["memGiB"] for r in rows),
        }
        for provider, rows in sorted(groups.items(), key=lambda kv: -len(kv[1]))
    ]
