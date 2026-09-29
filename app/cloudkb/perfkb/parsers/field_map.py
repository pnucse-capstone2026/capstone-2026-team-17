"""`spec_infos.details`의 **키마다 판정 하나** — 전수 분류표.

## 왜 코드에 있나

예전에는 "뽑을 키를 고른다"가 파서 안에 흩어진 선택이었다. 그래서 셋이 동시에 벌어졌다
(`archive/perfkb-field-axis-plan-2026-07-29.md` §1):

  1. 같은 규율이 소스마다 다르게 적용됐다(`ibm-perf`만 버린 칸을 기록했다).
  2. **스키마에 칸이 있는데 비어 있었다** — `maxNics`(aws 0/18,564)·`localSsdGB`
     (aws 0/18,564)·`gpuMemoryGB`(전부 0). 미러가 답을 갖고 있는데도 그랬다.
  3. 미러가 어디까지 답하는지 세지 않고 외부 소스를 추가했다.

문서에 표로 적으면 다음 스냅샷에서 조용히 거짓이 된다(질의집·도구 목록에서 이미 겪었다).
그래서 분류표는 **코드 상수**이고, 인벤토리에 있는 키가 여기 없으면 빌드가 죽는다.

> 판단이 바뀌는 것은 괜찮다. **판단하지 않은 칸이 있는 것**이 안 된다.

## 판정 여섯

    adopt            KB 필드로 담는다 (`field`에 스키마 필드명)
    constant         값 종류가 1 — 변별력이 0이다
    duplicate        다른 산출물이 이미 담는 사실 (`against`에 어디인지)
    out-of-axis      사실이지만 성능 축에 자리가 없다
    unmapped-axis    사실이고 담을 만한데 스키마에 칸이 없다 — **후보로만 올린다**
    blocked          담고 싶은데 못 읽는다 (중첩 2단 등)

`unmapped-axis`를 자꾸 채택하면 가이드라인 KB가 스펙 카탈로그가 된다(위협 T5). 그래서
후보로만 올리고 **채택은 별도 결정**이다(결정 D2).

## 실측 기준

인벤토리는 `tools/tumblebug_inventory.py`가 잰다 —
`report/tumblebug-inventory-2026-07-29/inventory.json` (cb-tumblebug v0.12.25,
namespace=system, 2026-07-29). 채움률은 **행 단위와 타입 단위 둘 다** 잰다: aws는
18,564행이지만 타입은 1,349종이라 리전 편중이 행 단위 채움률을 왜곡할 수 있다(위협 T4).
"""

from __future__ import annotations

from dataclasses import dataclass

ADOPT = "adopt"
CONSTANT = "constant"
DUPLICATE = "duplicate"
OUT_OF_AXIS = "out-of-axis"
UNMAPPED = "unmapped-axis"
BLOCKED = "blocked"

CLASSES = (ADOPT, CONSTANT, DUPLICATE, OUT_OF_AXIS, UNMAPPED, BLOCKED)


@dataclass(frozen=True)
class Decision:
    """키 하나에 대한 판정. **사유가 없으면 분류가 아니라 방치다**(위협 T6)."""

    cls: str
    why: str
    field: str = ""
    """`adopt`일 때 매핑할 `perfkb/schema.json`의 스펙 필드."""
    against: str = ""
    """`duplicate`일 때 어느 산출물의 어느 필드와 겹치는가."""
    verify: str = ""
    """값 대조가 필요한데 아직 안 한 것(T2). 비어 있으면 확인이 끝났거나 불필요하다."""

    def __post_init__(self) -> None:
        if self.cls not in CLASSES:
            raise ValueError(f"unknown class: {self.cls!r}")
        if not self.why.strip():
            raise ValueError("사유 없는 판정은 분류가 아니다")
        if self.cls == ADOPT and not self.field:
            raise ValueError("adopt에는 매핑할 스키마 필드가 있어야 한다")
        if self.cls == DUPLICATE and not self.against:
            raise ValueError("duplicate에는 겹치는 대상이 있어야 한다")


def _d(cls: str, why: str, **kw: str) -> Decision:
    return Decision(cls, why, **kw)


#: 미러 컬럼과 겹치는 것들의 공통 사유 — `spec_infos`의 42컬럼은 costkb·envkb가 이미 쓴다.
_MIRROR = "미러 컬럼이 같은 사실을 담고 costkb가 그것을 쓴다"

AWS: dict[str, Decision] = {
    # --- 담는다 -------------------------------------------------------------
    "BareMetal": _d(ADOPT, "베어메탈 여부는 성능 해석의 전제다", field="bareMetal"),
    "BurstablePerformanceSupported": _d(
        ADOPT, "지속 CPU 판정의 유일한 aws 신호", field="sustainedCpu"),
    "CurrentGeneration": _d(ADOPT, "구세대 경고의 근거", field="currentGeneration"),
    "ProcessorInfo": _d(ADOPT, "중첩 1단에서 클럭을 뽑는다", field="clockGHz"),
    "ProcessorInfo.SustainedClockSpeedInGhz": _d(
        ADOPT, "지속 클럭 — aws의 유일한 CPU 속도 신호", field="clockGHz"),
    "VCpuInfo": _d(ADOPT, "중첩 1단에서 코어당 스레드를 뽑는다", field="threadsPerCore"),
    "VCpuInfo.DefaultThreadsPerCore": _d(
        ADOPT, "vCPU가 물리 코어인지 스레드인지 가른다", field="threadsPerCore"),
    "NetworkInfo": _d(ADOPT, "중첩 1단에서 대역폭·NIC를 뽑는다", field="networkPerformance"),
    "NetworkInfo.NetworkPerformance": _d(
        ADOPT, "'Up to N Gigabit'이면 버스트다 — 이 구분이 축의 핵심",
        field="networkPerformance"),
    "NetworkInfo.MaximumNetworkInterfaces": _d(
        ADOPT,
        "**2026-07-29 신규.** 스키마에 `maxNics`가 있는데 aws는 0/18,564였다. "
        "미러가 100% 답하고 있었고, 새 소스 없이 채워진다",
        field="maxNics"),
    "EbsInfo": _d(ADOPT, "중첩 1단에서 EBS 대역폭·IOPS를 뽑는다", field="ebsBaselineMbps"),
    "InstanceStorageInfo": _d(
        ADOPT, "중첩 1단에서 로컬 SSD 용량을 뽑는다", field="localSsdGB"),
    "InstanceStorageInfo.TotalSizeInGB": _d(
        ADOPT,
        "**2026-07-29 신규.** `localSsdGB`가 gcp에만 있었다(aws 0/18,564). 미러가 "
        "40%(7,426행) 답한다 — 없는 것은 그 타입에 로컬 스토리지가 없는 것이다",
        field="localSsdGB"),
    "GpuInfo": _d(
        UNMAPPED,
        "GPU 메모리 총합이 여기 있지만 우리 칸(`gpuMemoryGB`)은 **GPU 하나당**이다 — "
        "아래 참조"),
    "GpuInfo.TotalGpuMemoryInMiB": _d(
        UNMAPPED,
        "**담으려다 뺐다(2026-07-29).** 스키마의 `gpuMemoryGB`는 GPU 하나당 메모리인데 "
        "원본은 **총합**이고, 나눌 GPU 개수는 중첩 2단(`Gpus:[{Count:…}]`)이라 못 읽는다"
        "(blocked). 뜻이 다른 값을 같은 칸에 담으면 8장짜리에서 8배 틀린다 — 이 "
        "저장소가 단위로 3,600배 틀린 전력이 있어 여기서 멈춘다. 총합용 칸을 새로 열지, "
        "개수를 읽을 길을 찾을지는 별도 결정이다"),

    # --- 중복 ---------------------------------------------------------------
    "InstanceType": _d(DUPLICATE, _MIRROR, against="tumblebug-cost.specName"),
    "MemoryInfo": _d(DUPLICATE, _MIRROR, against="tumblebug-cost.memGiB",
                     verify="보정(×1.024)이 걸린 축이라 값 대조가 필요하다"),
    "MemoryInfo.SizeInMiB": _d(DUPLICATE, _MIRROR, against="tumblebug-cost.memGiB",
                               verify="같은 이유"),
    "VCpuInfo.DefaultVCpus": _d(DUPLICATE, _MIRROR, against="tumblebug-cost.vcpu"),
    "VCpuInfo.DefaultCores": _d(
        DUPLICATE, "코어 수는 `ec2-hardware`가 `cpuCores`로 담는다",
        against="tumblebug-perf.cpuCores"),
    "InstanceStorageSupported": _d(
        DUPLICATE, "`InstanceStorageInfo`의 존재 여부와 같은 사실",
        against="details.InstanceStorageInfo"),
    "EbsInfo.EbsOptimizedSupport": _d(
        DUPLICATE, "EBS 대역폭 값이 이미 최적화 전제를 담는다",
        against="tumblebug-perf.ebsBaselineMbps"),

    # --- 값 종류 1 ----------------------------------------------------------
    "EbsInfo.EncryptionSupport": _d(CONSTANT, "전부 supported — 변별력 0"),
    "EbsInfo.EbsOptimizedInfo": _d(CONSTANT, "값이 전부 null"),
    "NetworkInfo.DefaultNetworkCardIndex": _d(CONSTANT, "전부 0"),
    "NetworkInfo.EfaInfo": _d(CONSTANT, "값이 전부 null — 존재 여부는 EfaSupported가 답한다"),
    "VCpuInfo.ValidCores": _d(CONSTANT, "값이 전부 null(중첩 배열이 눌린 자리)"),
    "VCpuInfo.ValidThreadsPerCore": _d(CONSTANT, "값이 전부 null"),

    # --- 축이 없다 ----------------------------------------------------------
    "AutoRecoverySupported": _d(OUT_OF_AXIS, "장애 복구 옵션 — 성능 사실이 아니다"),
    "DedicatedHostsSupported": _d(OUT_OF_AXIS, "테넌시 선택 — 배포 옵션이다"),
    "FreeTierEligible": _d(OUT_OF_AXIS, "가격 정책 — 성능 축이 아니다"),
    "HibernationSupported": _d(OUT_OF_AXIS, "전원 상태 기능 — 성능 사실이 아니다"),
    "PlacementGroupInfo": _d(OUT_OF_AXIS, "배치 전략은 성능 사실이 아니라 배포 옵션이다"),
    "SupportedBootModes": _d(OUT_OF_AXIS, "부팅 모드 — 이미지 축"),
    "SupportedRootDeviceTypes": _d(OUT_OF_AXIS, "루트 장치 종류 — 이미지 축"),
    "SupportedVirtualizationTypes": _d(OUT_OF_AXIS, "가상화 방식 — 이미지 축"),
    "EbsInfo.NvmeSupport": _d(OUT_OF_AXIS, "장치 인터페이스 요구 — 이미지·드라이버 축"),
    "InstanceStorageInfo.NvmeSupport": _d(OUT_OF_AXIS, "같은 이유"),
    "NetworkInfo.EnaSupport": _d(OUT_OF_AXIS, "드라이버 요구 — 이미지 축"),

    # --- 사실인데 칸이 없다 (후보) -------------------------------------------
    "SupportedUsageClasses": _d(UNMAPPED, "스팟 가능 여부 — 가격 축에 자리가 없다"),
    "Hypervisor": _d(UNMAPPED, "nitro/xen — 세대 신호로 쓸 수 있으나 칸이 없다"),
    "FpgaInfo": _d(UNMAPPED, "FPGA 축 자체가 없다(채움 0.1%)"),
    "FpgaInfo.TotalFpgaMemoryInMiB": _d(UNMAPPED, "같은 축"),
    "InferenceAcceleratorInfo": _d(UNMAPPED, "추론 가속기 축이 없다(채움 0.6%)"),
    "NetworkInfo.EfaSupported": _d(UNMAPPED, "HPC 신호 — 칸이 없다"),
    "NetworkInfo.MaximumNetworkCards": _d(UNMAPPED, "NIC **카드** 수 — maxNics와 다른 사실"),
    "NetworkInfo.Ipv4AddressesPerInterface": _d(UNMAPPED, "주소 밀도 — 네트워크 설계 축"),
    "NetworkInfo.Ipv6AddressesPerInterface": _d(UNMAPPED, "같은 축"),
    "NetworkInfo.Ipv6Supported": _d(UNMAPPED, "같은 축"),
}

AZURE: dict[str, Decision] = {
    # --- 담는다 -------------------------------------------------------------
    "ACUs": _d(ADOPT, "azure의 유일한 상대 CPU 지표", field="acu"),
    "vCPUsPerCore": _d(ADOPT, "vCPU가 스레드인지 코어인지", field="threadsPerCore"),
    "UncachedDiskIOPS": _d(ADOPT, "디스크 성능의 기준값", field="diskIops"),
    "CombinedTempDiskAndCachedIOPS": _d(ADOPT, "캐시 포함 IOPS", field="cachedDiskIops"),
    "AcceleratedNetworkingEnabled": _d(
        ADOPT, "네트워크 가속 지원", field="acceleratedNetworking"),
    "PremiumIO": _d(ADOPT, "프리미엄 디스크 지원", field="premiumIO"),
    "Family": _d(ADOPT, "버스트 패밀리 판정의 입력(B 계열)", field="family"),
    "MaxNetworkInterfaces": _d(
        ADOPT,
        "**2026-07-29 신규 · 조건부.** 미러가 100% 답하는데 우리는 문서표(72.8%)만 "
        "쓰고 있었다. 결정 D3에 따라 **두 소스가 일치하는 것만** 담고 불일치는 담지 "
        "않고 보고한다(`aws-limits` 선례)",
        field="maxNics"),

    # --- 중복 ---------------------------------------------------------------
    "Name": _d(DUPLICATE, _MIRROR, against="tumblebug-cost.specName"),
    "Size": _d(DUPLICATE, "`Name`에서 접두만 뗀 값", against="tumblebug-cost.specName"),
    "MemoryInMB": _d(DUPLICATE, _MIRROR, against="tumblebug-cost.memGiB",
                     verify="보정(×1.024) 때문에 값 대조 필요"),
    "MemoryGB": _d(DUPLICATE, _MIRROR, against="tumblebug-cost.memGiB",
                   verify="같은 이유"),
    "NumberOfCores": _d(DUPLICATE, _MIRROR, against="tumblebug-cost.vcpu"),
    "vCPUs": _d(DUPLICATE, _MIRROR, against="tumblebug-cost.vcpu"),
    "CpuArchitectureType": _d(DUPLICATE, _MIRROR, against="tumblebug-cost.architecture"),
    "LocationInfo_0_Location": _d(DUPLICATE, _MIRROR, against="tumblebug-cost.region"),
    "LocationInfo_0_Zone_0": _d(DUPLICATE, "존 목록은 envkb가 담는다",
                                against="cloud-regions.zones"),
    "LocationInfo_0_Zone_1": _d(DUPLICATE, "같은 이유", against="cloud-regions.zones"),
    "LocationInfo_0_Zone_2": _d(DUPLICATE, "같은 이유", against="cloud-regions.zones"),
    "MaxResourceVolumeMB": _d(
        DUPLICATE, "`ResourceDiskSizeInMB`와 같은 값으로 관측된다",
        against="details.ResourceDiskSizeInMB",
        verify="두 칸의 값이 항상 같은지 대조 필요"),

    # --- 값 종류 1 ----------------------------------------------------------
    "OSDiskSizeInMB": _d(CONSTANT, "전부 1047552"),
    "OSVhdSizeMB": _d(CONSTANT, "전부 1047552"),
    "Tier": _d(CONSTANT, "전부 Standard"),
    "ResourceType": _d(CONSTANT, "전부 virtualMachines"),
    "TrustedLaunchDisabled": _d(CONSTANT, "관측된 값이 True 하나뿐 — 부재와 구별 안 된다"),
    "HibernationSupported": _d(CONSTANT, "관측된 값이 True 하나뿐"),
    "SupportedVirtualizationTypes": _d(CONSTANT, "관측된 값이 하나뿐(채움 1%)"),
    "LocationInfo_0_ZoneDetail_0_Capability_0_UltraSSDAvailable": _d(
        CONSTANT, "관측된 값이 True 하나뿐 — `UltraSSDAvailable`가 두 값을 갖는다"),

    # --- 축이 없다 ----------------------------------------------------------
    "CapacityReservationSupported": _d(OUT_OF_AXIS, "예약 기능 — 배포·가격 옵션"),
    "SupportedCapacityReservationTypes": _d(OUT_OF_AXIS, "같은 축"),
    "ConfidentialComputingType": _d(OUT_OF_AXIS, "보안 실행 환경 — 성능 축이 아니다"),
    "DiskControllerTypes": _d(OUT_OF_AXIS, "컨트롤러 종류 — 이미지·드라이버 축"),
    "EncryptionAtHostSupported": _d(OUT_OF_AXIS, "암호화 옵션 — 보안 축"),
    "EphemeralOSDiskSupported": _d(OUT_OF_AXIS, "OS 디스크 배치 옵션"),
    "SupportedEphemeralOSDiskPlacements": _d(OUT_OF_AXIS, "같은 축"),
    "HyperVGenerations": _d(OUT_OF_AXIS, "하이퍼바이저 세대 — 이미지 축"),
    "MemoryPreservingMaintenanceSupported": _d(OUT_OF_AXIS, "유지보수 동작"),
    "VMDeploymentTypes": _d(OUT_OF_AXIS, "IaaS/PaaS 구분 — 배포 축"),
    "ParentSize": _d(OUT_OF_AXIS, "제약 크기의 부모 — 크기 족보이지 성능 사실이 아니다"),
    "vCPUsAvailable": _d(OUT_OF_AXIS, "제약 크기용 값 — 카탈로그 축"),
    "vCPUsConstraintsAllowed": _d(OUT_OF_AXIS, "같은 축"),
    "MaxDataDiskCount": _d(OUT_OF_AXIS, "데이터 디스크 **개수** 한도 — VM 성능 축 밖"),

    # --- 사실인데 칸이 없다 (후보) -------------------------------------------
    "RetirementDateUtc": _d(UNMAPPED, "수명주기 축이 없다 — envkb 후보"),
    "UncachedDiskBytesPerSecond": _d(UNMAPPED, "디스크 **대역폭** — 우리는 IOPS만 담는다"),
    "CachedDiskBytes": _d(UNMAPPED, "같은 축"),
    "CombinedTempDiskAndCachedReadBytesPerSecond": _d(UNMAPPED, "같은 축"),
    "CombinedTempDiskAndCachedWriteBytesPerSecond": _d(UNMAPPED, "같은 축"),
    "NvmeMaxReadIops": _d(UNMAPPED, "NVMe 로컬 디스크 축이 없다"),
    "NvmeMaxWriteIops": _d(UNMAPPED, "같은 축"),
    "NvmeMaxReadBytesPerSecond": _d(UNMAPPED, "같은 축"),
    "NvmeMaxWriteBytesPerSecond": _d(UNMAPPED, "같은 축"),
    "NvmeDiskSizeInMiB": _d(UNMAPPED, "같은 축 — `localSsdGB`와 뜻이 다르다"),
    "NvmeSizePerDiskInMiB": _d(UNMAPPED, "같은 축"),
    "GPUs": _d(UNMAPPED, "azure GPU 개수 — `gpuCount`가 있으나 azure 소스를 안 대조했다"),
    "RdmaEnabled": _d(UNMAPPED, "HPC 신호 — 칸이 없다"),
    "UltraSSDAvailable": _d(UNMAPPED, "디스크 티어 가용성 — 리전·존 축에 걸린다"),
    "MaxWriteAcceleratorDisksAllowed": _d(UNMAPPED, "쓰기 가속 디스크 수 — 칸이 없다"),
    "LowPriorityCapable": _d(UNMAPPED, "스팟 가능 여부 — aws `SupportedUsageClasses`와 같은 성격"),
    "ResourceDiskSizeInMB": _d(UNMAPPED, "임시 디스크 용량 — `localSsdGB`와 뜻이 다르다"),
}

GCP: dict[str, Decision] = {
    # --- 담는다 -------------------------------------------------------------
    "IsSharedCpu": _d(ADOPT, "공유 코어 여부 = gcp의 지속 CPU 신호", field="sustainedCpu"),
    "MaximumPersistentDisks": _d(ADOPT, "디스크 부착 한도", field="maxPersistentDisks"),
    "MaximumPersistentDisksSizeGb": _d(ADOPT, "디스크 용량 한도", field="maxPersistentDiskGB"),
    "Description": _d(ADOPT, "벤더 설명 원문 — 스펙 해석의 근거", field="vendorDescription"),

    # --- 중복 ---------------------------------------------------------------
    "Name": _d(DUPLICATE, _MIRROR, against="tumblebug-cost.specName"),
    "Id": _d(DUPLICATE, "행 식별자 — `id`가 이미 있다", against="tumblebug-cost.id"),
    "GuestCpus": _d(DUPLICATE, _MIRROR, against="tumblebug-cost.vcpu"),
    "MemoryMb": _d(DUPLICATE, _MIRROR, against="tumblebug-cost.memGiB",
                   verify="보정(×1.024) 때문에 값 대조 필요"),
    "Architecture": _d(DUPLICATE, _MIRROR, against="tumblebug-cost.architecture"),
    "Zone": _d(DUPLICATE, "존은 envkb가 담는다", against="cloud-regions.zones"),
    "BundledLocalSsds": _d(
        DUPLICATE, "로컬 SSD 용량은 `cyclenerd-gcp-catalog`가 `localSsdGB`로 담는다",
        against="tumblebug-perf.localSsdGB",
        verify="파티션 수 × 크기가 우리 값과 같은지 대조 필요"),
    "BundledLocalSsds.partitionCount": _d(
        DUPLICATE, "같은 사실의 조각", against="tumblebug-perf.localSsdGB"),
    "Accelerators": _d(DUPLICATE, "GPU 개수·모델은 `gcloud-machine-types`가 담는다",
                       against="tumblebug-perf.gpuCount"),
    "Accelerators.guestAcceleratorCount": _d(
        DUPLICATE, "같은 사실", against="tumblebug-perf.gpuCount"),
    "Accelerators.guestAcceleratorType": _d(
        DUPLICATE, "같은 사실", against="tumblebug-perf.gpuModel"),

    # --- 값 종류 1 ----------------------------------------------------------
    "CreationTimestamp": _d(CONSTANT, "전부 1969-12-31 — 값이 무의미하다"),
    "Kind": _d(CONSTANT, "전부 compute#machineType"),
    "SelfLink": _d(CONSTANT, "수집 프로젝트 URL 하나 — 우리 것이 아닌 맥락"),
    "BundledLocalSsds.defaultInterface": _d(CONSTANT, "전부 NVME"),

    # --- 축이 없다 ----------------------------------------------------------
    "ImageSpaceGb": _d(OUT_OF_AXIS, "이미지 공간 — 이미지 축"),

    # --- 사실인데 칸이 없다 (후보) -------------------------------------------
    "Deprecated": _d(
        UNMAPPED,
        "**세대 신호다.** `project.py`의 주석 '`GCP는 세대를 명시하지 않는다`'는 "
        "엄밀히는 과장이었다 — 1%(타입 1.3%)에 DEPRECATED가 실제로 찍혀 있다. "
        "`currentGeneration`에 담을 수 있으나 채움률이 낮아 **부재를 최신으로 읽는 "
        "오독**을 부를 수 있어 이번 라운드 채택 밖(D2)"),
    "Deprecated.state": _d(UNMAPPED, "같은 후보의 실제 값 자리"),
    "Deprecated.replacement": _d(UNMAPPED, "대체 스펙 URL — 이전 경로 축이 없다"),
}

FIELD_MAP: dict[str, dict[str, Decision]] = {"aws": AWS, "azure": AZURE, "gcp": GCP}

#: 분류표를 두지 않은 프로바이더. **성능 신호를 추적하지 않는다는 사실 자체가 판정이다.**
#: 인벤토리(2026-07-29)의 키 수를 함께 적는다 — "왜 이 다섯은 성능 데이터가 없나"에
#: "추적하지 않는다"가 아니라 근거로 답하기 위해서다.
UNTRACKED_PROVIDERS = {
    "ibm": "키 86 · 행 2,002 — `ibm-catalog`가 별도 축으로 담는다(details는 안 본다)",
    "tencent": "키 41 · 행 2,865 — 성능 축 미개설",
    "alibaba": "키 1 · 행 2,494 — details에 키가 하나뿐이라 담을 것이 없다",
    "ncp": "키 19 · 행 393 — 성능 축 미개설",
    "kt": "키 8 · 행 220 — 성능 축 미개설",
    "nhn": "키 15 · 행 71 — 성능 축 미개설",
    "openstack": "키 8 · 행 6 — 표본이 6행이라 통계가 무의미하다",
}


def decision_for(provider: str, key: str) -> Decision | None:
    return FIELD_MAP.get(provider, {}).get(key)


def unknown_keys(provider: str, keys) -> list[str]:
    """분류표에 없는 키 — **빌드를 죽이는 데 쓴다**(위협 T1).

    `details`는 Go `%v`라 계약이 없다. 상위가 키를 하나 추가하면 우리는 조용히 모르게
    되는데, 그 조용함이 이 저장소가 계속 막아 온 것이다.
    """
    known = FIELD_MAP.get(provider, {})
    return sorted(k for k in keys if k not in known)


def summarize(provider: str) -> dict[str, int]:
    """판정별 개수 — 산출물 `_coverage.fields`에 그대로 실린다."""
    counts: dict[str, int] = dict.fromkeys(CLASSES, 0)
    for decision in FIELD_MAP.get(provider, {}).values():
        counts[decision.cls] += 1
    return counts


def not_adopted(provider: str) -> list[dict]:
    """담지 않은 키와 **사유** — 산출물에 남는다.

    `ibm-perf`의 `droppedConstantFields`를 일반화한 것이다. 그쪽만 버린 칸을 적고
    미러 `details`는 안 적어서, 무엇을 왜 안 담았는지 아무도 몰랐다.
    """
    out = []
    for key, decision in sorted(FIELD_MAP.get(provider, {}).items()):
        if decision.cls == ADOPT:
            continue
        row = {"key": key, "class": decision.cls, "reason": decision.why}
        if decision.against:
            row["against"] = decision.against
        if decision.verify:
            row["needsValueCheck"] = decision.verify
        out.append(row)
    return out
