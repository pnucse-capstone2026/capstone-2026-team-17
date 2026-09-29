"""AWS 인스턴스의 **하드웨어 사실** — CPU 모델과 GPU 모델.

**왜 필요한가.** 우리는 가속기(GPU) 정보를 **한 건도** 갖고 있지 않았다. 실측에서
"ap-northeast-2에서 쓸 수 있는 GPU 인스턴스 알려줘"에 모델이 GPU 개수·vCPU·메모리
표를 통째로 지어냈고(`g5g`를 AMD라고 했다 — NVIDIA다), 그것을 **우리 지식베이스에서
조회한 결과라고 명시**했다. 빈칸이 지어내기를 부른다.

**무엇을 담고 무엇을 안 담나.** 이 소스에는 벤치마크 점수(coremark·ffmpeg)도 있지만
**담지 않는다.** 그건 "누군가 한 번 잰 값"이라 우리 데이터의 다른 값들과 성격이
다르고, 비교에 쓰는 순간 위험하다:

- 원점수는 인스턴스 크기에 거의 정비례한다(실측: m7i 2vCPU 47k → 48vCPU 1,068k).
  그대로 보여주면 **큰 인스턴스가 항상 이기는** 답이 나온다.
- vCPU당으로 나누면 세대·아키텍처 차이가 잘 보이지만(Intel 18.7k · AMD 21.1k ·
  Graviton 25.7k), 어느 쪽을 보여주느냐가 **답을 뒤집는다.**
- coremark는 정수 연산이라 메모리·I/O 중심 작업에는 안 맞는다.

여기서 담는 것은 전부 **사실**이다 — `p4d.24xlarge`에 A100이 8장 달려 있다는 건
측정이 아니라 사양이다.

**단일 소스다.** 교차 검증할 짝이 없다. 대신 원본이 측정 시점(`ran_at`)을 함께
적어 두어 신선도를 우리가 판단할 수 있다.

실측(커밋 4ef36cd2): 1,093종 중 우리 aws 스펙과 매칭 1,069종(우리 aws 1,349종의
79%). GPU 정보가 있는 것은 50종, 모델 표기는 11종으로 이미 정규화돼 있다.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from app.cloudkb.kbcommon.fetch import fetch_cached
from app.cloudkb.kbcommon.sources import SOURCES

EVIDENCE = "ec2-hardware-probe"


class Report:
    def __init__(self) -> None:
        self.matched = 0
        self.unmatched: list[str] = []
        """소스에는 있는데 우리 스펙에 없는 타입. 담지 않고 센다."""
        self.gpu_models: Counter = Counter()
        self.measured_at: Counter = Counter()


def _cpu_fields(cpu: dict) -> dict:
    """CPU 사실만 고른다. 값이 없으면 칸을 만들지 않는다 — 0은 사실이 아니다."""
    out: dict = {}
    if cpu.get("vendor"):
        out["cpuVendor"] = cpu["vendor"]
    if cpu.get("model"):
        out["cpuModel"] = cpu["model"]
    for src, dst in (("speed", "cpuClockMHz"), ("cache", "cpuCacheKB"),
                     ("cores", "cpuCores"), ("threads", "cpuThreads")):
        value = cpu.get(src)
        if isinstance(value, (int, float)) and value > 0:
            out[dst] = value
    return out


def _gpu_fields(gpus: list) -> dict:
    """GPU 사실. **모델이 섞여 있으면 목록으로 남긴다** — 하나로 고르면 짐작이 된다."""
    names = [g.get("name") for g in gpus if isinstance(g, dict) and g.get("name")]
    if not names:
        return {}
    out: dict = {"gpuCount": len(gpus)}
    unique = sorted(set(names))
    out["gpuModel"] = unique[0] if len(unique) == 1 else unique
    arch = sorted({g.get("architecture") for g in gpus
                   if isinstance(g, dict) and g.get("architecture")})
    if arch:
        out["gpuArchitecture"] = arch[0] if len(arch) == 1 else arch
    return out


def enrich(specs: list[dict], hardware: dict) -> Report:
    """perfkb 레코드에 하드웨어 사실을 **덧붙인다**(제자리 수정).

    aws 레코드만 건드린다. 소스가 AWS 인스턴스만 담고 있어서다.
    """
    report = Report()
    by_name: dict[str, list[dict]] = {}
    for spec in specs:
        if spec.get("provider") == "aws" and spec.get("specName"):
            by_name.setdefault(spec["specName"], []).append(spec)

    for name, entry in hardware.items():
        targets = by_name.get(name)
        if not targets:
            report.unmatched.append(name)
            continue
        fields: dict = {}
        cpu = entry.get("cpu")
        if isinstance(cpu, dict):
            fields.update(_cpu_fields(cpu))
        memory = entry.get("memory")
        if isinstance(memory, dict) and memory.get("speed_mhz"):
            fields["memorySpeedMHz"] = memory["speed_mhz"]
        gpus = entry.get("nvidia_gpus")
        if isinstance(gpus, list) and gpus:
            fields.update(_gpu_fields(gpus))
            for g in gpus:
                if isinstance(g, dict) and g.get("name"):
                    report.gpu_models[g["name"]] += 1
        if not fields:
            continue
        # **측정 시점을 함께 남긴다.** 하드웨어 사실이라 잘 안 바뀌지만, 언제 확인된
        # 것인지 없으면 다음 사람이 신선도를 판단할 수 없다.
        if entry.get("ran_at"):
            fields["hardwareCheckedAt"] = str(entry["ran_at"])[:10]
            report.measured_at[str(entry["ran_at"])[:7]] += 1
        fields["hardwareEvidence"] = EVIDENCE
        for spec in targets:
            spec.update(fields)
        report.matched += 1
    return report


def fetch(refresh: bool = False) -> tuple[dict, Path]:
    from app.cloudkb.kbcommon.artifact import load_json

    source = SOURCES["ec2-hardware"]
    path = fetch_cached(source.url, f"ec2-hardware-{source.pin[:12]}.json", refresh=refresh)
    return load_json(path), path


def format_report(report: Report) -> str:
    lines = [
        f"하드웨어 사실: {report.matched:,}종에 덧붙임 "
        f"(GPU 있는 것 {sum(1 for _ in report.gpu_models.elements()) and len(report.gpu_models)}종 표기)"
    ]
    if report.gpu_models:
        top = ", ".join(f"{k}×{v}" for k, v in report.gpu_models.most_common(4))
        lines.append(f"  GPU 모델: {top}")
    if report.measured_at:
        lines.append(f"  측정 시점: {dict(report.measured_at)}")
    if report.unmatched:
        lines.append(
            f"  소스에는 있으나 우리 스펙에 없는 타입 {len(report.unmatched)}종은 "
            f"담지 않음: {sorted(report.unmatched)[:4]}"
        )
    return "\n".join(lines)
