"""`spec_infos` 행 → 성능 레코드 (순수 투영).

이 파일은 덤프도 파일도 모른다 — dict를 받아 dict를 준다. 그래서 34MB 덤프 없이
행 모양 fixture로 전부 테스트할 수 있다(`tests/test_perfkb_projection.py`).

## sustainedCpu — 세 프로바이더를 하나로 묶지 않는 이유

사용자가 실제로 묻는 건 "이거 상시 성능이 보장돼?"다. 그런데 그 답을 얻는 경로가
프로바이더마다 **다른 개념**이라, `burstable` 같은 필드 하나로 묶으면 거짓말이 된다:

| | 메커니즘 | 근거 | 신뢰도 |
|---|---|---|---|
| AWS t계열 | CPU 크레딧 소진 시 baseline 저하 | `BurstablePerformanceSupported` (명시 필드) | 1.0 |
| GCP 공유코어 | vCPU 자체를 공유 (크레딧 아님) | `IsSharedCpu` (명시 필드) | 1.0 |
| Azure B계열 | 크레딧 모델 (AWS와 유사) | `Family` 이름 패턴 **추론** | 0.8 |

그래서 값(`value`)과 함께 **왜 그렇게 판단했는지**(`note`)와 **어떻게 알았는지**
(`evidence`/`basis`)를 항상 같이 준다.
costkb가 evidence를 안 두는 것과 대조된다 — 거긴 출처가 파일 단위로 균일한 미러라
레코드별 evidence가 죽은 필드가 되지만, 여기선 같은 필드가 1.0에서도 0.8에서도 온다.

## Azure B계열 판정의 취약점

`Family`는 100% 채워져 있지만 표기가 섞여 있다(`standardBsv2Family` vs
`StandardFXmsv2Family`). 실측상 B계열은 전부 소문자로 시작했지만 그건 **운**이므로
대소문자를 무시한다. 또한 이 판정은 "B로 시작하면 버스트"라는 이름 규칙에 기대므로,
B가 아닌 새 버스트 패밀리가 생기면 놓친다 — 그래서 신뢰도가 0.8이다.
"""

from __future__ import annotations

import re

from app.cloudkb.kbcommon.basis import basis_of

from .details import (
    go_bool,
    go_field,
    go_number,
    is_burst_bandwidth,
    parse_details,
)

# costkb와 같은 값이지만 **의도적으로 복제한다** — KB끼리는 서로 import하지 않는다
# 한 줄 중복이 패키지 간 결합보다 싸다.
SYSTEM_NAMESPACE = "system"

#: azure NIC 수의 **미러 값**을 잠시 담는 칸. 스키마에 없는 이름이라 산출물에 남으면
#: 검증이 실패한다 — 그것이 의도다. 대조(`azure_sizes.reconcile_max_nics`)를 건너뛴
#: 빌드가 조용히 통과하면 결정 D3이 코드에서 증발한다.
MIRROR_MAX_NICS = "_mirrorMaxNics"

_AZURE_BURST_FAMILY = re.compile(r"^standardB", re.IGNORECASE)

_NOTE_AWS_BURST = (
    "Burstable instance — performance drops to baseline once the CPU credits run out."
)
_NOTE_GCP_SHARED = (
    "Shared core — the vCPU is shared with other instances, so performance is not steady."
)
_NOTE_AZURE_BURST = (
    "B-series (burstable) — a credit model, so performance drops under sustained load."
)
# 상시 보장은 "버스트로 분류되지 않았다"에서 끌어낸 **추론**이다. 원본이 그렇게
# 말한 게 아니므로 그 사실을 답변에도 남긴다.
_NOTE_AWS_NOT_BURST = (
    "A type AWS does not classify as burstable — sustained performance is inferred "
    "from that."
)
_NOTE_GCP_DEDICATED = (
    "A type GCP does not mark as shared-core — the dedicated vCPU is inferred from that."
)


def _sustained_cpu(provider: str, det: dict[str, str]) -> dict | None:
    """상시 CPU 성능이 보장되나. 판단 근거를 함께 반환한다. 모르면 None."""
    if provider == "aws":
        burst = go_bool(det.get("BurstablePerformanceSupported"))
        if burst is None:
            return None
        # **방향에 따라 근거가 다르다.** 필드가 참이면 "버스트다"를 직접 말한 것이고,
        # 거짓이면 "버스트로 분류하지 않는다"까지만 말한 것이다. 거기서 "상시 성능
        # 보장"을 끌어내는 건 추론이고, 그 추론은 실제로 깨진다 —
        # `t1.micro`는 AWS가 false를 주지만 T2 크레딧 모델보다 앞선 세대라서일 뿐,
        # 상시 성능이 보장되는 게 아니다(감사 결함 P1). 8건이 "확신에 찬 오답"이었다.
        evidence = "aws-burstable-field" if burst else "aws-non-burstable-inferred"
        return {
            "value": not burst,
            "note": _NOTE_AWS_BURST if burst else _NOTE_AWS_NOT_BURST,
            "evidence": evidence,
            "basis": basis_of(evidence),
        }
    if provider == "gcp":
        shared = go_bool(det.get("IsSharedCpu"))
        if shared is None:
            return None
        # AWS와 같은 비대칭 — 필드는 "공유 코어다"만 직접 말한다.
        evidence = "gcp-shared-cpu-field" if shared else "gcp-dedicated-cpu-inferred"
        return {
            "value": not shared,
            "note": _NOTE_GCP_SHARED if shared else _NOTE_GCP_DEDICATED,
            "evidence": evidence,
            "basis": basis_of(evidence),
        }
    if provider == "azure":
        family = det.get("Family")
        if not family:
            return None
        burst = bool(_AZURE_BURST_FAMILY.match(family))
        return {
            "value": not burst,
            "note": _NOTE_AZURE_BURST if burst else None,
            # 이름 규칙 추론이라 짐작이다 — B가 아닌 버스트 패밀리가 생기면 놓친다.
            "evidence": "azure-family-name",
            "basis": basis_of("azure-family-name"),
        }
    return None  # 나머지 7개 프로바이더는 신호를 추적하지 못했다 — 모른다고 둔다


def _aws_fields(det: dict[str, str]) -> dict:
    ebs = det.get("EbsInfo")
    net = det.get("NetworkInfo")
    perf = go_field(net, "NetworkPerformance")
    # **2026-07-29에 새로 담기 시작한 둘** — 새 소스를 받아온 것이 아니라 이미 갖고 있던
    # 미러에서 뽑는다. 둘 다 스키마에 칸이 **선언돼 있는데 aws는 0건**이었다
    # (`archive/perfkb-field-axis-plan-2026-07-29.md` §1-(2)). 세어 보고 나서야 보였다.
    #
    # **셋이 될 뻔했다**: `GpuInfo.TotalGpuMemoryInMiB`도 담으려다 뺐다. 스키마의
    # `gpuMemoryGB`는 **GPU 하나당** 메모리인데 원본은 **총합**이고, 나눌 개수는 중첩
    # 2단(`Gpus:[{Count:...}]`)이라 못 읽는다. 뜻이 다른 값을 같은 칸에 담으면 8장짜리
    # 인스턴스에서 8배 틀린다 — 이 저장소가 단위로 3,600배 틀린 적이 있어 여기서 멈춘다.
    nics = go_number(net, "MaximumNetworkInterfaces")
    local_ssd = go_number(det.get("InstanceStorageInfo"), "TotalSizeInGB")
    return {
        "currentGeneration": go_bool(det.get("CurrentGeneration")),
        "clockGHz": go_number(det.get("ProcessorInfo"), "SustainedClockSpeedInGhz"),
        "threadsPerCore": go_number(det.get("VCpuInfo"), "DefaultThreadsPerCore"),
        "networkPerformance": perf,
        "networkIsBurst": is_burst_bandwidth(perf),
        "ebsBaselineMbps": go_number(ebs, "BaselineBandwidthInMbps"),
        "ebsMaxMbps": go_number(ebs, "MaximumBandwidthInMbps"),
        "ebsBaselineIops": go_number(ebs, "BaselineIops"),
        "ebsMaxIops": go_number(ebs, "MaximumIops"),
        "bareMetal": go_bool(det.get("BareMetal")),
        "maxNics": int(nics) if nics else None,
        # 로컬 스토리지가 **없는** 타입은 이 블록 자체가 없다 — 0이 아니라 부재다.
        "localSsdGB": local_ssd or None,
    }


def _azure_fields(det: dict[str, str]) -> dict:
    def num(key: str) -> float | None:
        raw = det.get(key)
        try:
            return float(raw) if raw not in (None, "") else None
        except ValueError:
            return None

    return {
        # ACU는 37.7%만 채워져 있고 결측이 세대로 설명되지 않는다(로드맵 4-2절).
        # 없으면 "성능이 나쁘다"가 아니라 "모른다"다.
        "acu": num("ACUs"),
        "threadsPerCore": num("vCPUsPerCore"),
        "diskIops": num("UncachedDiskIOPS"),
        "cachedDiskIops": num("CombinedTempDiskAndCachedIOPS"),
        "acceleratedNetworking": go_bool(det.get("AcceleratedNetworkingEnabled")),
        "premiumIO": go_bool(det.get("PremiumIO")),
        "family": det.get("Family") or None,
        # **미러의 NIC 수는 `maxNics`에 바로 넣지 않는다.** 문서표와 두 소스가 있고,
        # 결정 D3이 "일치하는 것만 담는다"이기 때문이다(`aws-limits` 선례). 대조는
        # 빌드가 `azure_sizes.reconcile_max_nics`에서 하고, 그때 이 칸은 지워진다.
        MIRROR_MAX_NICS: num("MaxNetworkInterfaces"),
    }


def _gcp_fields(det: dict[str, str]) -> dict:
    def num(key: str) -> float | None:
        raw = det.get(key)
        try:
            return float(raw) if raw not in (None, "") else None
        except ValueError:
            return None

    return {
        "maxPersistentDisks": num("MaximumPersistentDisks"),
        "maxPersistentDiskGB": num("MaximumPersistentDisksSizeGb"),
        # GCP는 세대를 명시하지 않는다. 이름 접두(n1/c4…)로 추론할 수 있지만
        # 그건 추측이라 Phase 1에서는 하지 않는다 — currentGeneration은 null로 둔다.
        "vendorDescription": det.get("Description") or None,
    }


_BY_PROVIDER = {"aws": _aws_fields, "azure": _azure_fields, "gcp": _gcp_fields}


def project_row(row: dict, *, namespace: str | None = SYSTEM_NAMESPACE) -> dict | None:
    """`spec_infos` 행 → 성능 레코드. 성능 신호가 하나도 없으면 None.

    Args:
        row: spec_infos 행 dict.
        namespace: 이 namespace의 행만 쓴다. None이면 전체.
    """
    if namespace is not None and (row.get("namespace") or "").strip() != namespace:
        return None
    provider = (row.get("provider_name") or "").strip().lower()
    spec_id = (row.get("id") or "").strip()
    spec_name = (row.get("csp_spec_name") or "").strip()
    if not provider or not spec_id or not spec_name:
        return None

    det = parse_details(row.get("details"))
    if not det:
        return None

    record: dict = {"id": spec_id, "provider": provider, "specName": spec_name}
    sustained = _sustained_cpu(provider, det)
    if sustained is not None:
        record["sustainedCpu"] = sustained
    record.update(_BY_PROVIDER.get(provider, lambda _: {})(det))

    # None 필드는 버린다 — "모른다"를 저장할 필요는 없다(없는 키 = 모름).
    record = {k: v for k, v in record.items() if v is not None}
    # id/provider/specName 말고 실제 성능 신호가 하나도 없으면 레코드가 무의미하다.
    if len(record) <= 3:
        return None
    return record
