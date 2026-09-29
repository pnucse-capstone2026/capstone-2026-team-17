"""성능 필드를 **한 곳에서** 선언한다 — 라벨·단위·표시법·비교 가능 여부.

## 왜 따로 뺐나

같은 필드 목록이 세 군데에 있었다: `agent_api._describe`(프로파일), `agent_api._COMPARE_AXES`
(비교), `cli._describe`(사람용). 셋이 **따로 자라서 갈라졌고**, 그 결과가 실측으로 확인됐다.

    perf_compare(aws, [m5.large, c6a.large])  → 세대 이야기가 한 줄도 없음
    recommend_note(aws, m5.large)             → "구세대 인스턴스입니다"

`currentGeneration`은 aws에서 **100% 채워진** 칸인데 비교 축 목록에만 빠져 있었다. 그래서
성능 도구는 침묵하고 비용 도구는 경고하는, **같은 사실에 두 그림**이 나왔다. 반대로 채움률
37.7%짜리 Azure `acu`는 비교 축에 들어 있었다 — 선정 기준이 없었다는 뜻이다.

프로파일 쪽 손실은 더 컸다. 목록이 AWS 필드 위주로 자라서 다른 프로바이더의 **100% 채워진
칸이 통째로 빠졌다**:

    aws   m5.large         12줄
    azure Standard_D2s_v5   3줄   ← 캐시 IOPS·가속 네트워킹·프리미엄 IO·계열이 다 100%인데 없음
    gcp   n2-highmem-8      3줄   ← 최대 영구 디스크 수/GB·벤더 설명이 다 100%인데 없음

사람용 CLI는 `vendorDescription`을 출력하는데 에이전트용은 안 했다. **사람이 보는 것과
에이전트가 보는 것이 달랐다.**

## 규칙

- **축은 프로바이더 전용이다.** 목록은 하나지만 레코드에 없는 칸은 그냥 안 나온다. ACU는
  Azure에만, 클럭은 AWS에만 있으므로 프로바이더 간 비교는 여전히 불가능하다.
- **`compare=False`는 "비교 축이 아니다"라는 뜻이다** — 값이 없다는 뜻이 아니다. 긴 산문
  (`vendorDescription`)과 분류 문자열(`family`)이 그렇다. 프로파일에는 나온다.
- **버스트 값은 라벨에 버스트라고 적는다.** `ebsMaxMbps`를 숨기면 데이터를 버리는 것이고,
  그냥 "EBS 최대"로 적으면 지속 성능으로 읽힌다. 라벨에서 갈랐다.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Field:
    """표시 가능한 성능 칸 하나."""

    key: str
    label: str
    unit: str = ""
    kind: str = "number"  # number | bool | text
    compare: bool = True
    yes: str = "yes"
    no: str = "no"

    def render(self, value: object) -> str:
        """값 하나를 사람이 읽는 문자열로. **불리언을 날것으로 내보내지 않는다.**

        `최신 세대: False`는 파이썬을 읽는 사람에게만 뜻이 통한다. 숫자는 `:g`로 찍어
        `128.0개 디스크` 같은 걸 막는다 — 개수에 소수점이 붙으면 값이 의심스러워 보인다.
        """
        if self.kind == "bool":
            return self.yes if value else self.no
        if self.kind == "number" and isinstance(value, (int, float)):
            return f"{value:g}{self.unit}"
        return f"{value}{self.unit}"


# 표시 순서. 프로바이더별로 나누지 않는다 — 레코드에 있는 칸만 나오므로 저절로 갈린다.
FIELDS: tuple[Field, ...] = (
    # 공통 성격의 축 (실제로는 aws에만 있으나 개념은 프로바이더 공통)
    Field("currentGeneration", "generation", kind="bool",
          yes="current generation", no="previous generation"),
    # AWS
    Field("clockGHz", "clock speed", " GHz"),
    Field("threadsPerCore", "threads per core"),
    Field("bareMetal", "bare metal", kind="bool"),
    Field("networkPerformance", "network", kind="text"),
    Field("ebsBaselineMbps", "EBS sustained bandwidth", " Mbps"),
    Field("ebsMaxMbps", "EBS max bandwidth (burst)", " Mbps"),
    Field("ebsBaselineIops", "EBS sustained IOPS"),
    Field("ebsMaxIops", "EBS max IOPS (burst)"),
    # Azure
    Field("acu", "ACU (Azure-internal comparison)"),
    Field("diskIops", "disk IOPS"),
    Field("cachedDiskIops", "cached disk IOPS"),
    Field("acceleratedNetworking", "accelerated networking", kind="bool",
          yes="supported", no="not supported"),
    Field("premiumIO", "premium IO", kind="bool", yes="supported", no="not supported"),
    Field("family", "family", kind="text", compare=False),
    # GCP
    Field("maxPersistentDisks", "max persistent disks"),
    Field("maxPersistentDiskGB", "max persistent disk capacity", " GB"),
    Field("vendorDescription", "vendor description", kind="text", compare=False),
    # Azure 크기 문서와 GCP 계열 자료에서 제공하는 네트워크·토폴로지 필드.
    Field("networkBandwidthMbps", "network bandwidth", " Mbps"),
    Field("portSpeedMbps", "port speed", " Mbps"),
    Field("maxNics", "max network interfaces"),
    Field("numaCount", "NUMA nodes"),
    # CPU 계열 표시는 비교 점수로 사용하지 않고 설명에만 사용한다.
    Field("cpuVendor", "CPU vendor", kind="text", compare=False),
    Field("cpuFamily", "CPU family", kind="text", compare=False),
    # **`sustainedCpu`로 옮기지 않은 이유가 라벨에 있어야 한다.** dedicated를 보고
    # "상시 CPU 보장"이라 적으면 그건 우리 추론이지 원본이 한 말이 아니다.
    Field("vcpuTenancy", "vCPU tenancy (as the source states it)",
          kind="text", compare=False),
    Field("provisioningTimeoutSeconds", "provisioning timeout", " s", compare=False),
    Field("gpuMemoryGB", "GPU memory", " GB"),
    Field("localSsdGB", "local SSD", " GB"),
)

COMPARE_FIELDS: tuple[Field, ...] = tuple(f for f in FIELDS if f.compare)
