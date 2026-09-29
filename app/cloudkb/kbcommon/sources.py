"""VM 가격·성능 데이터 빌드에 사용하는 고정 원본 목록."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Source:
    key: str
    url: str
    pin_kind: str
    pin: str
    redistribution: str = ""
    license: str = ""
    note: str = ""


SOURCES: dict[str, Source] = {
    "tumblebug-dump": Source(
        "tumblebug-dump",
        "https://raw.githubusercontent.com/cloud-barista/cb-tumblebug/v0.12.25/assets/assets.dump.gz",
        "tag",
        "v0.12.25",
        license="Apache-2.0",
        note="AWS·Azure·GCP VM 사양·가격·성능의 공통 스냅샷.",
    ),
    "tumblebug-cloudinfo": Source(
        "tumblebug-cloudinfo",
        "https://raw.githubusercontent.com/cloud-barista/cb-tumblebug/v0.11.8/assets/cloudinfo.yaml",
        "tag",
        "v0.11.8",
        license="Apache-2.0",
        note="세 CSP의 리전 코드와 표시 이름.",
    ),
    "azure-retail-prices": Source(
        "azure-retail-prices",
        "https://prices.azure.com/api/retail/prices?api-version=2023-01-01-preview",
        "digest",
        "(고정 불가)",
        redistribution="not-stated",
        license="not-stated",
        note="Azure VM 스팟·예약·저축 플랜 가격. 응답 해시와 수집 시점을 기록한다.",
    ),
    "cyclenerd-gcp-pricing": Source(
        "cyclenerd-gcp-pricing",
        "https://raw.githubusercontent.com/Cyclenerd/google-cloud-pricing-cost-calculator/574d8fbb68fa/pricing.yml",
        "commit",
        "574d8fbb68fa",
        license="Apache-2.0",
        note="GCP VM 스팟·약정 가격 보강.",
    ),
    "azure-compute-docs": Source(
        "azure-compute-docs",
        "https://raw.githubusercontent.com/MicrosoftDocs/azure-compute-docs/9c18d88d498d09e897edde7e2fe8483067f2556a",
        "commit",
        "9c18d88d498d09e897edde7e2fe8483067f2556a",
        license="CC-BY-4.0",
        note="Azure VM 크기 표의 NIC·대역폭·세대 정보.",
    ),
    "ec2-hardware": Source(
        "ec2-hardware",
        "https://raw.githubusercontent.com/vantage-sh/ec2instances.info/4ef36cd2c9867c1076206dcb691412ae2de7e8dd/scraper/aws/ec2/extras/manually_fetched_data.json",
        "commit",
        "4ef36cd2c9867c1076206dcb691412ae2de7e8dd",
        license="MIT",
        note="AWS VM 하드웨어 특성 보강.",
    ),
    "gcloud-machine-types": Source(
        "gcloud-machine-types",
        "https://raw.githubusercontent.com/Cyclenerd/google-cloud-compute-machine-types/add204f16413d608d35141715aef4a122b59cb96",
        "commit",
        "add204f16413d608d35141715aef4a122b59cb96",
        license="Apache-2.0",
        note="GCP VM 시리즈의 CPU·네트워크·GPU 특성 보강.",
    ),
}


def unpinnable() -> list[Source]:
    return [source for source in SOURCES.values() if source.pin_kind == "digest"]


def unlicensed() -> list[Source]:
    return [source for source in SOURCES.values() if not source.license]
