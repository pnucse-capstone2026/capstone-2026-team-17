"""GCP Compute Engine 머신 타입·가격 원본 수집.

    python -m app.cloudkb.speckb.fetch_gcp [--refresh]

## 인증 — API 키는 필요 없다

두 엔드포인트 모두 `gcloud auth print-access-token`이 주는 OAuth 토큰을
받는다. Cloud Billing Catalog API 문서가 API 키를 안내하지만, 인증 없이 부르면
돌아오는 403 메시지가 "API Key **or other form of API consumer identity**"라고
적혀 있고 실제로 Bearer 토큰이 통한다(실측 200).

## 왜 엔드포인트가 두 개인가

GCP는 AWS·Azure와 달리 **사양과 가격이 아예 다른 API로 분리돼 있다**.

- `compute.googleapis.com` `aggregated/machineTypes` — `guestCpus`, `memoryMb`,
  `accelerators` 등 사양. 가격은 한 필드도 없다.
- `cloudbilling.googleapis.com` Compute Engine SKU — 가격. 머신 타입 이름이
  아니라 `description`("Spot Preemptible E2 Custom Instance Core running in
  Paris") 문자열로 표현되고, 코어와 램이 **별도 SKU로 쪼개져** 있다.

둘을 잇는 일은 speckb의 몫이 아니다. 원본만 받아 둔다.

## 프로젝트 ID

호출 경로에 프로젝트가 들어가지만 machineTypes는 프로젝트별 카탈로그가 아니다.
어느 프로젝트로 부르든 같은 목록이 나온다. 다만 `selfLink` 필드에는 호출에 쓴
프로젝트 ID가 박혀 나오므로, manifest에 어느 프로젝트로 받았는지 남긴다.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
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

COMPUTE_ENGINE_SERVICE = "6F81-5844-456A"
MACHINE_TYPES_URL = (
    "https://compute.googleapis.com/compute/v1/projects/{project}/aggregated/machineTypes"
)
SKUS_URL = f"https://cloudbilling.googleapis.com/v1/services/{COMPUTE_ENGINE_SERVICE}/skus"


def out_dir() -> Path:
    return raw_dir() / "gcp"


def _gcloud(*args: str) -> str | None:
    executable = shutil.which("gcloud")
    if executable is None:
        return None
    try:
        completed = subprocess.run(
            [executable, *args],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip()
    return value or None


def credentials() -> tuple[str, str]:
    """OAuth 토큰과 프로젝트 ID. 없으면 무엇을 해야 하는지 알려주고 끝낸다."""
    if shutil.which("gcloud") is None:
        raise SystemExit(
            "gcloud CLI를 찾지 못했다. Google Cloud SDK를 설치하고 PATH에 넣어야 한다."
        )
    token = _gcloud("auth", "print-access-token")
    if not token:
        raise SystemExit("액세스 토큰을 얻지 못했다. `gcloud auth login`을 먼저 실행해야 한다.")
    project = _gcloud("config", "get-value", "project")
    if not project or project == "(unset)":
        raise SystemExit(
            "프로젝트가 설정돼 있지 않다. `gcloud config set project <PROJECT_ID>`가 필요하다."
        )
    return token, project


def fetch_pages(
    name: str,
    base_url: str,
    token: str,
    refresh: bool,
    *,
    page_param: str = "pageToken",
) -> tuple[int, list[dict]]:
    """nextPageToken을 따라가며 페이지마다 한 파일로 저장한다."""
    page_dir = out_dir() / name
    headers = {"Authorization": f"Bearer {token}"}
    token_value: str | None = None
    page = 0
    payloads: list[dict] = []

    while True:
        page += 1
        destination = page_dir / f"page-{page:04d}.json.gz"
        if already_have(destination) and not refresh:
            payload = load_gz_json(destination)
        else:
            separator = "&" if "?" in base_url else "?"
            url = base_url
            if token_value:
                url = f"{base_url}{separator}{page_param}={token_value}"
            response = get(url, headers=headers)
            if not response.ok:
                raise RuntimeError(f"{name} {page}페이지 요청 실패: HTTP {response.status}")
            save_gz(destination, response.body, url, headers=response.headers)
            payload = json.loads(response.body.decode("utf-8"))
        payloads.append(payload)
        token_value = payload.get("nextPageToken")
        if not token_value:
            break

    return page, payloads


def count_machine_types(payloads: list[dict]) -> tuple[int, int]:
    zones = set()
    total = 0
    for payload in payloads:
        for zone_key, entry in payload.get("items", {}).items():
            machine_types = entry.get("machineTypes") or []
            if machine_types:
                zones.add(zone_key)
                total += len(machine_types)
    return len(zones), total


def main(argv: list[str] | None = None) -> int:
    enable_utf8_stdout()
    parser = argparse.ArgumentParser(description="GCP Compute Engine 원본 수집")
    parser.add_argument("--refresh", action="store_true", help="이미 받은 파일도 다시 받는다")
    args = parser.parse_args(argv)

    try:
        token, project = credentials()
    except SystemExit as error:
        print(f"[gcp] {error}", file=sys.stderr)
        return 1

    print(f"[gcp] 프로젝트 {project}로 수집한다")

    machine_url = MACHINE_TYPES_URL.format(project=project)
    print("[gcp] machineTypes 수집 중 …")
    machine_pages, machine_payloads = fetch_pages(
        "machine-types-aggregated", machine_url, token, args.refresh
    )
    zone_count, machine_count = count_machine_types(machine_payloads)
    print(f"[gcp] machineTypes {machine_pages}페이지, 존 {zone_count}개, {machine_count}건")

    print("[gcp] Billing SKU 수집 중 …")
    sku_pages, sku_payloads = fetch_pages(
        "billing-skus-compute-engine", SKUS_URL, token, args.refresh
    )
    sku_count = sum(len(payload.get("skus", [])) for payload in sku_payloads)
    print(f"[gcp] SKU {sku_pages}페이지, {sku_count}건")

    write_manifest(
        out_dir() / "manifest.json",
        {
            "provider": "gcp",
            "project_used": project,
            "sources": [
                {
                    "key": "compute-machine-types-aggregated",
                    "url": MACHINE_TYPES_URL.format(project="<project>"),
                    "auth": "OAuth Bearer (gcloud auth print-access-token)",
                    "note": "사양만. 가격 필드 없음. 페이지별 한 파일",
                },
                {
                    "key": "billing-catalog-compute-engine-skus",
                    "url": SKUS_URL,
                    "auth": "OAuth Bearer (gcloud auth print-access-token)",
                    "note": f"Compute Engine 서비스 {COMPUTE_ENGINE_SERVICE}. 가격만. "
                    "머신 타입 이름이 아니라 description 문자열로 표현된다",
                },
            ],
            "machine_types": {
                "pages": machine_pages,
                "zones": zone_count,
                "records": machine_count,
            },
            "billing_skus": {"pages": sku_pages, "records": sku_count},
        },
    )
    print("[gcp] 완료")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
