"""Azure Virtual Machines 카탈로그 원본 수집.

    python -m app.cloudkb.speckb.fetch_azure [--refresh] [--region koreacentral]

## 왜 엔드포인트가 두 개인가

한쪽만으로는 VM을 특정할 수 없다. 둘 다 받는다.

**Retail Prices API** (`prices.azure.com`) — 공식 가격 API. meterId, 스팟,
예약 인스턴스, Savings Plan, Windows/Linux 미터가 전부 들어온다. 인증이 필요
없다. 다만 **vCPU와 메모리가 없다** — 644,061건의 필드 23종을 전수 조사해
확인했다. `armSkuName`(예: `Standard_D14`)만 주고 몇 코어인지는 말하지 않는다.

**Resource SKUs API** (`management.azure.com`) — 포털의 VM 크기 선택 화면이
쓰는 소스. `vCPUs` `MemoryGB` `MaxDataDiskCount` `UncachedDiskIOPS`
`MaxResourceVolumeMB` `PremiumIO`를 준다. 대신 가격이 없다. 인증이 필요하다.

둘은 `Standard_D8ads_v5`라는 **같은 ARM 이름**으로 붙는다 — Retail의
`armSkuName`과 SKUs의 `name`이다.

## 가격 계산기를 왜 안 쓰는가

`azure.microsoft.com/api/v3/pricing/virtual-machines/calculator/`도 cores·ram을
주지만 두 가지가 부족해서 뺐다.

첫째, 데이터 디스크 수·IOPS·프리미엄 디스크 지원 여부가 없다(compute offer의
필드는 cores·ram·diskSize·gpu·series 등 11종이 전부).

둘째, **ARM SKU 이름을 주지 않는다.** 키가 `linux-d8adsv5-standard` 꼴인데
Terraform에는 `Standard_D8ads_v5`가 들어가야 한다. 슬러그→ARM 변환을 규칙으로
시도하면 1,435개 중 983개(68.5%)만 맞는다 — `d11s`의 실제 이름은 `Standard_DS11`로
s가 앞으로 가고 `dc128edsv6`는 `Standard_DC128eds_v6`로 DC가 대문자다. 나머지
452개는 손으로 예외 표를 만들어야 하는데 그건 지어낸 매핑이라 하지 않는다.

## 분량

Retail은 서울 리전 하나가 13페이지·12,335건·7.5MB다(실측). 68개 리전 661,563건,
gzip 후 32.3MB. AWS(~1.5MB)나 GCP(~3MB)보다 한 자릿수 크다.

Resource SKUs는 리전별 78개 파일에 72,550건, gzip 후 2.69MB다. **둘 다 리전별로
파일을 나눈다.** SKUs는 한 번에 받을 수도 있지만 그러면 파일 하나가 압축 풀어
123.8MB가 되어 열어볼 수가 없다 — 나누면 리전당 평균 1.59MB다. 대신 gzip이 리전 간
반복을 못 접어 디스크가 2.01MB에서 2.69MB로 는다.

## 주의 — 저장된 가격은 해석 전 값이다

Retail API의 예약 인스턴스 레코드는 `unitOfMeasure`가 "1 Hour"인데 실제
`retailPrice`는 기간 총액이다(1년이면 8,760시간분). speckb는 값을 해석하지
않고 원본만 저장하므로 이 함정에 걸리지 않지만, 이 데이터를 쓰는 쪽은 알아야
한다.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import urllib.parse
from pathlib import Path

try:
    from ._http import (
        already_have,
        enable_utf8_stdout,
        get,
        load_gz_json,
        raw_dir,
        save_gz,
        write_manifest,
    )
except ImportError:  # pragma: no cover - 단독 실행 경로
    from _http import (  # type: ignore[no-redef]
        already_have,
        enable_utf8_stdout,
        get,
        load_gz_json,
        raw_dir,
        save_gz,
        write_manifest,
    )

RETAIL_BASE = "https://prices.azure.com/api/retail/prices"
RETAIL_API_VERSION = "2023-01-01-preview"

ARM_RESOURCE = "https://management.azure.com/"
SKUS_URL = (
    "https://management.azure.com/subscriptions/{subscription}"
    "/providers/Microsoft.Compute/skus?api-version=2021-07-01"
)


def out_dir() -> Path:
    return raw_dir() / "azure"


def regions_path() -> Path:
    """armRegionName 목록.

    `aws_locations.json`과 같은 역할이다. Resource SKUs 응답의 `locations`에서
    만들며, 가격이 없는 리전 이름 목록이라 커밋해도 된다. 이 파일이 있으면
    Retail 수집은 az login 없이도 돌아간다.
    """
    return Path(__file__).resolve().parent / "azure_regions.json"


def load_regions() -> list[str]:
    path = regions_path()
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("regions", [])


def collected_regions() -> set[str]:
    """이미 Retail 데이터를 받아 둔 리전."""
    root = out_dir() / "retail-prices"
    if not root.exists():
        return set()
    return {entry.name for entry in root.iterdir() if entry.is_dir()}


def write_regions(regions: list[str], source: str) -> None:
    """Resource SKUs의 locations에 이미 받아 둔 리전을 합쳐 기록한다.

    합집합이어야 하는 이유: 일반 구독의 Resource SKUs 조회에는 US GovCloud가
    나오지 않는다(별도 클라우드다). 그런데 공개 Retail API에는 usgovarizona·
    usgovtexas·usgovvirginia 가격이 있고 이미 받아 뒀다. SKUs 결과만 쓰면 그 셋이
    목록에서 빠져 다음 실행부터 집계에서 사라진다 — 실제로 한 번 그렇게 돼서
    총계가 644,061에서 640,659로 줄었다.
    """
    regions_path().write_text(
        json.dumps(
            {
                "_note": (
                    "Retail Prices를 리전별로 받을 때 쓰는 armRegionName 목록. "
                    "Resource SKUs 응답의 locations를 소문자로 맞춘 값과, 이미 "
                    "Retail을 받아 둔 리전의 합집합이다. Azure가 locations를 "
                    "대소문자 뒤섞어 주기 때문에 소문자로 맞춘다. "
                    "가격은 들어 있지 않다."
                ),
                "_source": source,
                "regions": sorted(set(regions) | collected_regions()),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


# winget으로 az를 막 설치하면 이미 떠 있는 셸의 PATH에는 아직 안 잡힌다.
# 터미널을 다시 열게 하는 대신 표준 설치 경로도 같이 본다.
AZ_FALLBACK_PATHS = (
    r"C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd",
    r"C:\Program Files (x86)\Microsoft SDKs\Azure\CLI2\wbin\az.cmd",
)


def az_executable() -> str | None:
    found = shutil.which("az") or shutil.which("az.cmd")
    if found:
        return found
    for candidate in AZ_FALLBACK_PATHS:
        if Path(candidate).exists():
            return candidate
    return None


def _az(*args: str) -> str | None:
    executable = az_executable()
    if executable is None:
        return None
    try:
        completed = subprocess.run(
            [executable, *args], capture_output=True, text=True, timeout=180, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip() or None


def azure_credentials() -> tuple[str, str] | None:
    """(토큰, 구독 ID). 준비가 안 됐으면 이유를 찍고 None."""
    if az_executable() is None:
        print("[azure] az CLI가 없어 Resource SKUs를 건너뛴다.")
        print("        winget install -e --id Microsoft.AzureCLI  후  az login")
        return None
    raw = _az("account", "get-access-token", "--resource", ARM_RESOURCE)
    if not raw:
        print("[azure] Azure 로그인 상태가 아니라 Resource SKUs를 건너뛴다. `az login` 필요.")
        return None
    try:
        token = json.loads(raw)["accessToken"]
    except (ValueError, KeyError):
        print("[azure] 액세스 토큰을 해석하지 못해 Resource SKUs를 건너뛴다.")
        return None
    subscription = _az("account", "show", "--query", "id", "-o", "tsv")
    if not subscription:
        print("[azure] 구독을 확인하지 못해 Resource SKUs를 건너뛴다.")
        return None
    return token, subscription


def discover_sku_regions(token: str, subscription: str) -> dict[str, int]:
    """필터 없는 SKUs 호출로 리전 목록과 리전별 건수를 알아낸다. **저장하지 않는다.**

    리전별로 받으려면 리전 목록이 먼저 있어야 한다. 후보를 셋 다 실측으로 대조했고,
    **완전한 것은 이 호출뿐이었다.**

        필터 없는 SKUs 응답        78 / 78 리전
        Subscriptions - Locations  63 / 78   (api-version=2022-12-01)
        Providers - Microsoft.Compute  49 / 78

    Locations API가 놓치는 15개는 `eastus3` `taiwannorth` `saudiarabiaeast`
    `southcentralus2` 같은 미공개·예정 리전이다. SKU 카탈로그에는 있는데 리전
    목록에는 아직 없다. 그걸로 목록을 만들면 7,802건이 조용히 사라진다 —
    실제로 한 번 그렇게 돼서 63리전 64,686건만 받혔다.

    응답 본문을 저장하지 않는 이유는 그게 이 작업의 출발점이기 때문이다. 압축을
    풀면 123.8MB라 열어볼 수가 없어서 리전별로 나누기로 했다. 탐색에만 쓰고 버린다.
    같은 데이터를 리전별 파일로 다시 받으므로 잃는 것은 없고, 전체 실행마다 약
    124MB를 더 받는 것이 대가다.

    리전별 건수를 함께 돌려주는 이유는 검증용이다. 저장된 리전별 파일의 건수와
    맞춰 보면 분할이 손실을 냈는지 **같은 날 기준으로** 확인할 수 있다. Azure
    카탈로그는 날마다 조금씩 바뀌므로(하루 사이 brazilsouth -66, westeurope +4)
    어제의 총계와 비교하는 것은 검증이 되지 않는다.
    """
    url: str | None = SKUS_URL.format(subscription=subscription)
    headers = {"Authorization": f"Bearer {token}"}
    counts: dict[str, int] = {}
    while url:
        response = get(url, headers=headers)
        if not response.ok:
            raise RuntimeError(f"SKU 리전 탐색 실패: HTTP {response.status}")
        payload = json.loads(response.body.decode("utf-8"))
        for entry in payload.get("value", []):
            # Azure가 `locations`를 대소문자 뒤섞어 준다 — 같은 응답 안에
            # "AustraliaCentral"과 "australiaeast"가 함께 나온다. `$filter`는
            # 소문자 armRegionName을 받으므로 여기서 맞춘다. 저장되는 원본은
            # 손대지 않고 질의에 쓸 목록만 정규화한다.
            for location in entry.get("locations") or []:
                key = location.lower()
                counts[key] = counts.get(key, 0) + 1
        url = payload.get("nextLink")
    return counts


def fetch_skus_region(
    region: str, token: str, subscription: str, refresh: bool
) -> tuple[int, int]:
    """한 리전의 Compute SKU를 페이지별 파일로 저장한다.

    `$filter`는 location만 지원한다. 리전별로 나누는 이유는 용량이 아니라 **열람
    가능성**이다. 필터 없이 받으면 파일 하나가 압축 풀어 123.8MB라 열 수 없다.
    나누면 리전당 평균 1.59MB가 된다. 디스크는 2.01MB에서 2.69MB로 늘어난다 —
    gzip이 파일 하나일 때 리전 간 반복까지 접기 때문이고, 그 0.68MB가 대가다.

    나눠도 데이터는 같다. 필터 없는 응답도 항목마다 `locations` 길이가 정확히 1
    이라 이미 (SKU, 리전) 쌍 단위였다(72,550건 전수 확인). 실제로 대조했을 때
    koreacentral 932·eastus 1356·uksouth 1227로 양쪽이 일치했다.

    VM뿐 아니라 디스크·가용성 집합 등 Compute 전체 SKU가 온다. 거르지 않는다 —
    VM이 아닌 것은 건수로 10.4%지만 용량으로는 3.5%뿐이라, 무가공을 깨서 얻는
    이득이 80KB에 불과하다.

    첫 페이지가 비면 아무것도 저장하지 않고 (0, 0)을 돌려준다. 109개 리전 중 SKU가
    있는 곳은 78개뿐이고, 빈 파일을 남기면 나중에 "이 리전은 SKU가 없다"는 사실처럼
    읽히기 때문이다(`fetch_region`과 같은 규칙).
    """
    region_dir = out_dir() / "resource-skus" / region
    query = f"location eq '{region}'"
    url: str | None = (
        SKUS_URL.format(subscription=subscription)
        + f"&$filter={urllib.parse.quote(query)}"
    )
    headers = {"Authorization": f"Bearer {token}"}
    page = 0
    total = 0

    while url:
        page += 1
        destination = region_dir / f"page-{page:04d}.json.gz"
        if already_have(destination) and not refresh:
            payload = load_gz_json(destination)
        else:
            response = get(url, headers=headers)
            if not response.ok:
                raise RuntimeError(f"{region} {page}페이지 실패: HTTP {response.status}")
            payload = json.loads(response.body.decode("utf-8"))
            if page == 1 and not payload.get("value"):
                return 0, 0
            save_gz(destination, response.body, url, headers=response.headers)
        total += len(payload.get("value", []))
        # 지금은 리전당 1페이지로 끝나지만 Azure가 보장한 계약이 아니라 관찰일
        # 뿐이므로 루프는 남겨 둔다. Retail의 `NextPageLink`와 이름이 다르다.
        url = payload.get("nextLink")

    return page, total


def fetch_resource_skus(
    refresh: bool, only_region: str | None = None
) -> tuple[dict[str, dict[str, int]], int, list[str]]:
    """Compute Resource SKUs를 리전별로 받는다. (리전별 집계, 총건수, 데이터 있는 리전)"""
    credentials = azure_credentials()
    if credentials is None:
        return {}, 0, []
    token, subscription = credentials

    expected: dict[str, int] = {}
    if only_region:
        regions = [only_region]
    else:
        print("[azure] SKU 리전 탐색 중 (필터 없는 호출 1회, 저장하지 않음)")
        expected = discover_sku_regions(token, subscription)
        regions = sorted(expected)
        print(f"[azure] 탐색 결과 {len(regions)}개 리전, {sum(expected.values())}건")

    per_region: dict[str, dict[str, int]] = {}
    total = 0
    for position, region in enumerate(regions, start=1):
        try:
            pages, count = fetch_skus_region(region, token, subscription, refresh)
        except RuntimeError as error:
            # 리전 하나가 죽어도 나머지는 계속 받는다(Retail 루프와 같다).
            print(f"[azure] SKUs ({position}/{len(regions)}) {region}: 실패 — {error}")
            continue
        if pages == 0:
            continue
        per_region[region] = {"pages": pages, "records": count}
        total += count
        print(f"[azure] SKUs ({position}/{len(regions)}) {region}: {count}건")

    # 탐색 때 센 건수와 저장된 건수를 리전별로 대조한다. 어긋나면 분할이 데이터를
    # 잃었다는 뜻이므로 조용히 넘기지 않고 찍는다. 이미 받아 둔 파일을 건너뛴
    # 경우(`already_have`)에는 그 사이 카탈로그가 움직여 어긋날 수 있다.
    if expected:
        mismatched = {
            region: (count, per_region.get(region, {}).get("records", 0))
            for region, count in expected.items()
            if per_region.get(region, {}).get("records", 0) != count
        }
        if mismatched:
            print(f"[azure] 경고 — 탐색 건수와 다른 리전 {len(mismatched)}개")
            for region, (want, got) in sorted(mismatched.items()):
                print(f"        {region}: 탐색 {want} / 저장 {got}")
        else:
            print(f"[azure] 리전별 건수 대조 통과 — {len(expected)}개 리전 전부 일치")

    return per_region, total, sorted(per_region)


def retail_url(region: str) -> str:
    query = (
        f"serviceName eq 'Virtual Machines' and armRegionName eq '{region}'"
    )
    return (
        f"{RETAIL_BASE}?api-version={RETAIL_API_VERSION}"
        f"&$filter={urllib.parse.quote(query)}"
    )


def fetch_region(region: str, refresh: bool) -> tuple[int, int]:
    """한 리전의 모든 페이지를 페이지별 파일로 저장한다.

    페이지를 합쳐 하나로 만들지 않는다. 합치는 순간 그건 가공이다.

    첫 페이지가 비어 있으면 armRegionName 후보가 틀렸다는 뜻이므로 아무것도
    저장하지 않고 (0, 0)을 돌려준다. 빈 파일을 남기면 나중에 "이 리전은 VM이
    없다"는 사실처럼 읽히기 때문이다.
    """
    region_dir = out_dir() / "retail-prices" / region
    url: str | None = retail_url(region)
    page = 0
    items = 0

    while url:
        page += 1
        destination = region_dir / f"page-{page:04d}.json.gz"
        if already_have(destination) and not refresh:
            payload = load_gz_json(destination)
        else:
            response = get(url)
            if not response.ok:
                raise RuntimeError(f"{region} {page}페이지 요청 실패: HTTP {response.status}")
            payload = json.loads(response.body.decode("utf-8"))
            if page == 1 and not payload.get("Items"):
                return 0, 0
            save_gz(destination, response.body, url, headers=response.headers)
        items += len(payload.get("Items", []))
        url = payload.get("NextPageLink")

    return page, items


def main(argv: list[str] | None = None) -> int:
    enable_utf8_stdout()
    parser = argparse.ArgumentParser(description="Azure VM 카탈로그 원본 수집")
    parser.add_argument("--refresh", action="store_true", help="이미 받은 파일도 다시 받는다")
    parser.add_argument("--region", help="이 리전 하나만 받는다 (예: koreacentral)")
    parser.add_argument("--skus-only", action="store_true", help="Resource SKUs만 받는다")
    args = parser.parse_args(argv)

    sku_regions, sku_total, locations = fetch_resource_skus(args.refresh, args.region)
    if sku_regions:
        print(f"[azure] Resource SKUs 리전 {len(locations)}개, 총 {sku_total}건")
        # --region 으로 한 리전만 받았으면 목록을 다시 쓰지 않는다. write_regions는
        # 이미 Retail을 받아 둔 리전과만 합집합을 취하므로, 부분 결과로 덮으면
        # 나머지가 목록에서 사라진다(81 → 69).
        if not args.region:
            write_regions(locations, SKUS_URL.format(subscription="<subscription>"))

    if args.skus_only:
        print("[azure] --skus-only 이므로 Retail은 건너뛴다")
        return 0

    # 파일을 단일 출처로 삼는다. write_regions가 SKUs의 locations와 이미 받아 둔
    # 리전을 합쳐 써 두므로, locations를 직접 쓰면 GovCloud가 빠진다.
    regions = [args.region] if args.region else load_regions()
    if not regions:
        print(
            "[azure] 리전 목록이 없다. az login 후 한 번 실행하면 "
            "azure_regions.json이 만들어진다.",
            file=sys.stderr,
        )
        return 1
    print(f"[azure] Retail Prices 수집 대상 {len(regions)}개 리전")

    per_region: dict[str, dict[str, int]] = {}
    failed: list[str] = []
    empty: list[str] = []
    total_items = 0
    for position, region in enumerate(sorted(regions), start=1):
        try:
            pages, items = fetch_region(region, args.refresh)
        except RuntimeError as error:
            # 리전 하나가 죽어도 나머지는 계속 받는다.
            failed.append(region)
            print(f"[azure] ({position}/{len(regions)}) {region}: 실패 — {error}")
            continue
        if pages == 0:
            empty.append(region)
            print(f"[azure] ({position}/{len(regions)}) {region}: 결과 없음 — 리전명 불일치 추정")
            continue
        per_region[region] = {"pages": pages, "items": items}
        total_items += items
        print(f"[azure] ({position}/{len(regions)}) {region}: {pages}페이지 {items}건")

    write_manifest(
        out_dir() / "manifest.json",
        {
            "provider": "azure",
            "sources": [
                {
                    "key": "compute-resource-skus",
                    "url": SKUS_URL.format(subscription="<subscription>")
                    + "&$filter=location eq '<region>'",
                    "auth": "OAuth Bearer (az account get-access-token)",
                    "note": "vCPUs·MemoryGB·MaxDataDiskCount·UncachedDiskIOPS·"
                    "MaxResourceVolumeMB·PremiumIO 등 스펙. 가격 없음. "
                    "name 필드가 ARM SKU 이름(Standard_D8ads_v5). 리전별 한 파일",
                },
                {
                    "key": "compute-resource-skus-discovery",
                    "url": SKUS_URL.format(subscription="<subscription>"),
                    "auth": "OAuth Bearer (az account get-access-token)",
                    "note": "리전 목록을 알아내는 필터 없는 호출. 저장하지 않는다 — "
                    "압축 풀면 123.8MB라 열람이 안 돼 리전별로 다시 받는다. "
                    "Locations API(63/78)와 Providers API(49/78)는 미공개 리전을 "
                    "놓쳐서 이 호출만이 완전하다",
                },
                {
                    "key": "retail-prices",
                    "url": f"{RETAIL_BASE}?api-version={RETAIL_API_VERSION}"
                    "&$filter=serviceName eq 'Virtual Machines' and armRegionName eq '<region>'",
                    "auth": "none",
                    "note": "meterId·스팟·예약가 보유, vCPU·메모리 없음. 페이지별 한 파일. "
                    "armSkuName으로 Resource SKUs의 name과 붙는다",
                },
            ],
            "resource_skus": {
                "regions": len(sku_regions),
                "records": sku_total,
                "per_region": dict(sorted(sku_regions.items())),
            },
            "regions_attempted": len(regions),
            "regions_failed": sorted(failed),
            "regions_empty": sorted(empty),
            "retail_items_total": total_items,
            "per_region": dict(sorted(per_region.items())),
        },
    )
    print(
        f"[azure] 완료 — {len(per_region)}개 리전, 총 {total_items}건, "
        f"결과 없음 {len(empty)}개, 실패 {len(failed)}개"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
