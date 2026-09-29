"""AWS EC2 VM 카탈로그 원본 수집.

    python -m app.cloudkb.speckb.fetch_aws [--refresh] [--region ap-northeast-2]

## 어떤 엔드포인트를 쓰는가

콘솔 가격 피드(`b0.p.awsstatic.com`)를 쓴다. 리전당 1,322건이 57KB에 들어오고,
한 레코드에 `vCPU`·`Memory`·`price`가 같이 있다.

벌크 Price List(`pricing.us-east-1.amazonaws.com/offers/...`)는 같은 내용을 더
자세히 주지만 **리전 하나가 480MB**다(us-east-1 실측). 전 리전을 받으면 17GB라
쓰지 않는다 — 아래 한 가지 용도로만, 그것도 앞부분만 잘라 쓴다.

## 함정 — 콘솔 피드는 리전 코드가 아니라 표시명으로 키가 걸린다

경로에 `ap-northeast-2`가 아니라 `Asia Pacific (Seoul)`이 들어간다. 그리고 이
표시명은 규칙으로 유추할 수 없다. `eu-west-1`은 `Europe (Ireland)`가 아니라
`EU (Ireland)`이고, 전자로 요청하면 404가 돌아온다.

저장소의 `data/cloud-regions.json.gz`에도 리전명이 있지만 tumblebug 이름 체계라
값이 다르다(`ap-northeast-2`가 `South Korea (Seoul)`). 애초에 speckb는 저장소
데이터를 읽지 않으므로 선택지가 아니다.

그래서 리전마다 벌크 가격 파일에 Range 요청(`bytes=0-300000`)을 걸어 첫
`"location"` 값을 뽑는다. AWS가 직접 쓴 표시명을 AWS에서 받아오는 셈이고,
리전당 300KB면 끝난다.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
from pathlib import Path

# 패키지로 실행하든(`python -m app.cloudkb.speckb.fetch_aws`) 디렉터리를
# 통째로 떼어내 실행하든(`python fetch_aws.py`) 둘 다 동작해야 한다.
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

PRICING_HOST = "https://pricing.us-east-1.amazonaws.com"
REGION_INDEX_URL = f"{PRICING_HOST}/offers/v1.0/aws/AmazonEC2/current/region_index.json"
CONSOLE_FEED = (
    "https://b0.p.awsstatic.com/pricing/2.0/meteredUnitMaps/ec2/USD/current"
    "/ec2-ondemand-without-sec-sel/{location}/Linux/index.json"
)

# 벌크 파일에서 location 하나를 찾는 데 필요한 앞부분 크기. 300KB로 대부분
# 잡히고, 못 잡히면 1MB로 넓혀 한 번 더 본다.
LOCATION_PROBE_SIZES = (300_000, 1_000_000)
LOCATION_PATTERN = re.compile(r'"location"\s*:\s*"([^"]+)"')

# 두 AWS 소스가 같은 리전을 다르게 부른다 — 실측으로 확인한 어긋남이다.
#
# 벌크 Price List는 `eu-central-2`를 "Europe (Zurich)"라고 쓰는데 콘솔 피드는
# 그 이름에 404를 주고 "EU (Zurich)"에만 200을 준다. 오래된 유럽 리전은 양쪽
# 다 "EU (...)"라 문제가 없었고, 취리히·스페인처럼 나중에 생긴 리전에서만
# 갈린다. GovCloud도 벌크는 "AWS GovCloud (US-West)", 피드는 "AWS GovCloud (US)"다.
#
# 그래서 벌크에서 얻은 이름으로 404가 나면 아래 변환을 차례로 시도한다. 어떤
# 이름이 실제로 통했는지는 manifest에 남긴다.
LOCATION_FALLBACKS = (
    (re.compile(r"^Europe \("), "EU ("),
    (re.compile(r"^AWS GovCloud \(US-.*\)$"), "AWS GovCloud (US)"),
)


def location_candidates(location: str) -> list[str]:
    """벌크에서 얻은 이름과, 404일 때 시도할 대체 이름들."""
    candidates = [location]
    for pattern, replacement in LOCATION_FALLBACKS:
        if pattern.match(location):
            swapped = pattern.sub(replacement, location)
            if swapped not in candidates:
                candidates.append(swapped)
    return candidates


def out_dir() -> Path:
    return raw_dir() / "aws"


def locations_path() -> Path:
    """리전 코드 → AWS 표시명 매핑.

    이 파일만은 `raw/` 밖에 둔다. AWS 응답에서 뽑아낸 값이긴 하지만 파일 자체는
    우리가 조립한 것이라, 벤더 응답 본문만 들어가는 `raw/`에 섞으면 무가공
    보장이 흐려진다.
    """
    return Path(__file__).resolve().parent / "aws_locations.json"


def fetch_region_index(refresh: bool) -> dict:
    destination = out_dir() / "region_index.json.gz"
    if refresh or not already_have(destination):
        print("[aws] region_index.json 받는 중 …")
        response = get(REGION_INDEX_URL)
        if not response.ok:
            raise RuntimeError(f"region_index.json 요청 실패: HTTP {response.status}")
        save_gz(destination, response.body, REGION_INDEX_URL, headers=response.headers)
    return load_gz_json(destination)


def resolve_locations(index: dict, refresh: bool) -> tuple[dict[str, str], list[str]]:
    """리전마다 벌크 파일 앞부분을 잘라 AWS 표시명을 얻는다."""
    path = locations_path()
    known: dict[str, str] = {}
    if path.exists() and not refresh:
        known = json.loads(path.read_text(encoding="utf-8")).get("locations", {})

    regions = index.get("regions", {})
    unresolved: list[str] = []
    for position, (code, entry) in enumerate(sorted(regions.items()), start=1):
        if code in known:
            continue
        url = PRICING_HOST + entry["currentVersionUrl"]
        location = None
        for size in LOCATION_PROBE_SIZES:
            response = get(url, byte_range=(0, size))
            if not response.ok:
                continue
            match = LOCATION_PATTERN.search(response.body.decode("utf-8", "replace"))
            if match:
                location = match.group(1)
                break
        if location is None:
            unresolved.append(code)
            print(f"[aws] ({position}/{len(regions)}) {code}: 표시명을 찾지 못함")
            continue
        known[code] = location
        print(f"[aws] ({position}/{len(regions)}) {code} → {location}")

    path.write_text(
        json.dumps(
            {
                "_note": (
                    "AWS 콘솔 가격 피드는 리전 코드가 아니라 표시명으로 경로가 걸린다. "
                    "이 매핑은 리전별 벌크 Price List 파일 앞부분에 Range 요청을 걸어 "
                    "첫 'location' 값을 뽑아 만든 것이다. 벤더 응답 본문 자체는 아니므로 "
                    "raw/ 밖에 둔다."
                ),
                "_source": f"{PRICING_HOST}/offers/v1.0/aws/AmazonEC2/current/<region>/index.json",
                "_unresolved": unresolved,
                "locations": dict(sorted(known.items())),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return known, unresolved


def fetch_feeds(locations: dict[str, str], refresh: bool, only: str | None) -> dict:
    feed_dir = out_dir() / "ondemand-linux"
    saved: list[str] = []
    missing: list[str] = []
    used_fallback: dict[str, str] = {}
    targets = {only: locations[only]} if only else locations

    for position, (code, location) in enumerate(sorted(targets.items()), start=1):
        destination = feed_dir / f"{code}.json.gz"
        if already_have(destination) and not refresh:
            saved.append(code)
            continue

        response = None
        matched = None
        for candidate in location_candidates(location):
            url = CONSOLE_FEED.format(location=urllib.parse.quote(candidate))
            attempt = get(url)
            if attempt.status == 404:
                continue
            if not attempt.ok:
                raise RuntimeError(f"{code} 피드 요청 실패: HTTP {attempt.status}")
            response, matched = attempt, candidate
            break

        if response is None or matched is None:
            # 후보를 다 시도해도 404면 이 리전에는 온디맨드 Linux 피드가 없다.
            # 실패가 아니라 기록할 사실이다.
            missing.append(code)
            print(f"[aws] ({position}/{len(targets)}) {code}: 피드 없음 (404)")
            continue

        if matched != location:
            used_fallback[code] = matched

        url = CONSOLE_FEED.format(location=urllib.parse.quote(matched))
        save_gz(destination, response.body, url, headers=response.headers)
        saved.append(code)
        count = _count_items(response.body)
        suffix = f" [피드 이름 {matched!r}]" if matched != location else ""
        print(f"[aws] ({position}/{len(targets)}) {code}: {count}건 저장{suffix}")

    return {
        "saved": sorted(saved),
        "missing_feed": sorted(missing),
        "feed_name_differs_from_bulk": dict(sorted(used_fallback.items())),
    }


def _count_items(body: bytes) -> int:
    try:
        payload = json.loads(body.decode("utf-8"))
        return sum(len(entries) for entries in payload.get("regions", {}).values())
    except (ValueError, AttributeError):
        return -1


def main(argv: list[str] | None = None) -> int:
    enable_utf8_stdout()
    parser = argparse.ArgumentParser(description="AWS EC2 VM 카탈로그 원본 수집")
    parser.add_argument("--refresh", action="store_true", help="이미 받은 파일도 다시 받는다")
    parser.add_argument("--region", help="이 리전 하나만 받는다 (예: ap-northeast-2)")
    args = parser.parse_args(argv)

    index = fetch_region_index(args.refresh)
    print(f"[aws] 리전 {len(index.get('regions', {}))}개 확인")

    locations, unresolved = resolve_locations(index, args.refresh)
    print(f"[aws] 표시명 {len(locations)}개 확보, 미해결 {len(unresolved)}개")

    if args.region and args.region not in locations:
        print(f"[aws] {args.region}의 표시명을 확보하지 못해 중단한다", file=sys.stderr)
        return 1

    result = fetch_feeds(locations, args.refresh, args.region)

    write_manifest(
        out_dir() / "manifest.json",
        {
            "provider": "aws",
            "sources": [
                {"key": "region-index", "url": REGION_INDEX_URL, "auth": "none"},
                {
                    "key": "ec2-ondemand-linux",
                    "url": CONSOLE_FEED.format(location="<AWS location display name>"),
                    "auth": "none",
                    "note": "리전당 한 파일, 응답 본문 그대로",
                },
                {
                    "key": "bulk-price-list-header",
                    "url": f"{PRICING_HOST}/offers/v1.0/aws/AmazonEC2/current/<region>/index.json",
                    "auth": "none",
                    "note": "리전 표시명만 뽑으려고 Range 요청으로 앞부분만 읽는다",
                },
            ],
            "regions_in_index": len(index.get("regions", {})),
            "locations_resolved": len(locations),
            "locations_unresolved": unresolved,
            **result,
        },
    )
    print(f"[aws] 완료 — 저장 {len(result['saved'])}개, 피드 없음 {len(result['missing_feed'])}개")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
