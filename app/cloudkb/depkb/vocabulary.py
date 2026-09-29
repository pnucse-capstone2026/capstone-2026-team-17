"""어휘 — 다루는 자원 타입의 우주와 CSP 결속.

## 타입 집합은 연구 범위의 정규화 계층이다

아래 이름은 표준의 완전한 자원 분류가 아니라 AWS·Azure·GCP의 Docker-on-VM
실험 결과를 비교하기 위한 연구 내 정규화 이름이다. 완전한 실행 가능 배포에 필요한
자원 전체라고 주장하지 않는다. 현재 누락 범위는 `document/resource-coverage.md`에 기록한다.

`TYPES`는 Azure 스키마 결속이 확인된 하위 집합이며 전체 어휘의 정의가 아니다.
Cloud-Barista 용어는 비교 대상으로만 사용하며 이 이름들의 출처라고 주장하지 않는다.

Kubernetes, VPN, 서버리스와 관리형 상품 서비스는 현재 Docker-on-VM 범위 밖이다.

## 결속(binding)은 원문으로 검증된다

각 타입의 azure 결속은 핀 박힌 스키마 캐시(`cache/azure/`)의 definition 실물과
1:1로 맞아야 하고, `test_depkb.py`가 그것을 강제한다. 결속이 깨지면(판 갱신 등)
어휘가 조용히 낡는 대신 테스트가 죽는다.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AzureBinding:
    """어휘 타입 하나의 azure 스키마 결속."""

    file: str  #: 캐시 키 (fetch_azure.FILES의 키)
    definition: str  #: 그 파일 `definitions/` 안의 이름


#: **다루지 않기로 한 것과 그 사유.** 미룬 것이 아니라 경계다(2026-08-01 결정).
#:
#: 이 선언이 없으면 대조기가 관리형 서비스를 볼 때마다 *"실측 어휘가 없다"*로
#: 세고, 그러면 **"아직 안 한 것"과 "안 하기로 한 것"이 섞인다** — 이 저장소가
#: 다른 축에서 계속 지켜 온 구분이다(관심사의 `out_of_scope`, 계약의 `NOT_ASKED`).
#:
#: 경계를 그은 근거는 개수다. 우리 축은 **(자원 × CSP × 질문)**이고 실측 한 칸이
#: 클라우드 왕복 수 분이라, 상품 목록만큼 자원을 늘리면 축이 발산한다. 지금
#: 24종·118주장에 이르는 데 라운드 52개가 들었다.
OUT_OF_SCOPE: dict[str, str] = {
    "managed-service": "관리형 상품 서비스(관계형 DB·큐·오브젝트 저장소·시크릿 "
                       "저장소·검색·캐시 …). 상품이 CSP마다 수십 종이고 계속 "
                       "늘어나 **실측으로는 따라갈 수 없다**. 계획은 이것들을 "
                       "노드로 그리지만 우리는 그 의존에 대해 아무 말도 하지 "
                       "않는다 — 침묵을 '문제없다'로 읽지 말 것",
    "external-system": "외부 시스템(결제 게이트웨이 등). 우리가 생성하지 않으므로 "
                       "프로비저닝 의존성 분석 대상이 아니다",
    "serverless": "서버리스 런타임. 배치를 프로바이더가 정해서 우리가 놓을 것이 "
                  "없고, 그래서 연쇄 의존 자원 계산의 시작점이 되지 않는다",
}

#: 타입 우주. 키가 주장(claim)의 주체·대상 어휘다.
TYPES: dict[str, AzureBinding] = {
    "network": AzureBinding("network-virtualNetwork", "Common.VirtualNetwork"),
    "subnet": AzureBinding("network-virtualNetwork", "Common.Subnet"),
    "firewall": AzureBinding("network-virtualNetwork", "Common.NetworkSecurityGroup"),
    "nic": AzureBinding("network-virtualNetwork", "Common.NetworkInterface"),
    "publicIp": AzureBinding("network-virtualNetwork", "Common.PublicIPAddress"),
    "loadBalancer": AzureBinding("network-loadBalancer", "Common.LoadBalancer"),
    "vm": AzureBinding("compute-ComputeRP", "VirtualMachine"),
    "disk": AzureBinding("compute-DiskRP", "Disk"),
    "sshKey": AzureBinding("compute-ComputeRP", "SshPublicKeyResource"),
}

#: aws 결속 — CloudFormation 리소스 타입 이름. 원문은 CFN 스펙(핀은
#: fetch_vendors.SOURCES). CFN은 azure와 달리 **Required 플래그를 실제로 쓴다.**
AWS_TYPES: dict[str, str] = {
    "network": "AWS::EC2::VPC",
    "subnet": "AWS::EC2::Subnet",
    "firewall": "AWS::EC2::SecurityGroup",
    "nic": "AWS::EC2::NetworkInterface",
    "publicIp": "AWS::EC2::EIP",
    "loadBalancer": "AWS::ElasticLoadBalancingV2::LoadBalancer",
    "vm": "AWS::EC2::Instance",
    "disk": "AWS::EC2::Volume",
    "sshKey": "AWS::EC2::KeyPair",
}

#: gcp 결속 — compute 디스커버리 문서의 schema 이름. 특기 둘을 결속에 박는다:
#: **sshKey는 대응하는 독립 자원 타입이 없고**(None) —
#: **"gcp가 SSH 키를 지원하지 않는다"는 뜻이 아니다.** 지원한다. 다만 키가
#: 프로젝트·인스턴스 **메타데이터**(`ssh-keys`)나 OS Login으로 다뤄져 컴퓨트
#: 디스커버리 문서에 독립 CRUD 스키마가 없을 뿐이다. 이 어휘의 선정 기준이
#: "독립 CRUD가 있는 것"이라 여기서 None이 된다(2026-08-01 정정 — 앞서 이것을
#: "자원이 없다"로 줄여 적어 지원 부재로 읽힐 소지가 있었다).
#: 근거: Compute Engine 문서 *Add SSH keys to VMs* — 키는 메타데이터에 들어간다.
#: **그리고 이건 결속 판단이지 실측이 아니다** — claims에 gcp sshKey 주장은 0건이다
#: **NIC는 독립 자원이 아니라 Instance 내장 스키마다**(gcp에 NIC CRUD가 없다).
GCP_TYPES: dict[str, str | None] = {
    "network": "Network",
    "subnet": "Subnetwork",
    "firewall": "Firewall",
    "nic": "NetworkInterface",  # 내장 — 독립 CRUD 없음
    "publicIp": "Address",
    "loadBalancer": "ForwardingRule",  # gcp LB는 성좌 — 진입점만 결속
    "vm": "Instance",
    "disk": "Disk",
    "sshKey": None,
}

#: aws 이름 기반 참조 휴리스틱 — CFN은 참조가 문자열이라 **속성 이름**으로
#: 겨눈다. **우리 구성**이고 과대·과소근사 둘 다 가능하다(계획 T1과 같은 지위) —
#: 후보마다 원문 인용이 붙어 틀리면 인용에서 드러난다.
AWS_NAME_REFS: dict[str, str] = {
    "SubnetId": "subnet", "SubnetIds": "subnet", "Subnets": "subnet",
    "SubnetMappings": "subnet",
    "VpcId": "network",
    "SecurityGroupIds": "firewall", "SecurityGroups": "firewall",
    "GroupSet": "firewall", "Groups": "firewall",
    "NetworkInterfaceId": "nic",
    "AllocationId": "publicIp",
    "VolumeId": "disk",
    "KeyName": "sshKey",
    # image 라운드(2026-07-31). CFN엔 EC2 이미지 자원 타입이 없어 image는
    # aws에서 대상으로만 나타난다. Required:False는 위치 플래그다(LaunchTemplate
    # 경로) — 서버 요구는 동적 층이 판정.
    "ImageId": "image",
}

#: gcp (스키마, 속성) 쌍 한정 참조 — 이름만으로는 'source' 같은 일반어가
#: 오탐하므로 쌍으로 좁힌다. **우리 구성**, 인용 동반.
GCP_PAIR_REFS: dict[tuple[str, str], str] = {
    ("NetworkInterface", "network"): "network",
    ("NetworkInterface", "subnetwork"): "subnet",
    ("AttachedDisk", "source"): "disk",
    ("Firewall", "network"): "network",
    ("Subnetwork", "network"): "network",
    ("ForwardingRule", "network"): "network",
    ("ForwardingRule", "subnetwork"): "subnet",
    # image 라운드(2026-07-31): 부트 디스크의 이미지 참조. 대안 슬롯
    # (sourceSnapshot·AttachedDisk.source)의 존재가 선언 술어 가설의 근거다.
    ("AttachedDiskInitializeParams", "sourceImage"): "image",
}

#: ARM id 참조 래퍼 — 다른 RP의 자원을 id로 가리키는 정의. **우리 구성**이되,
#: 후보마다 원문 인용이 붙으므로 대응이 틀리면 인용에서 드러난다.
REFERENCE_WRAPPERS: dict[str, str] = {
    "NetworkInterfaceReference": "nic",
    "ManagedDiskParameters": "disk",
    # image 라운드(2026-07-31): 참조 훅만 연다 — image를 TYPES 주체로 올리면
    # image→vm·image→disk 후보가 생겨 미판정이 쌓인다(customImage 대기열의 것).
    "ImageReference": "image",
}

#: ARM 경로 세그먼트 → 어휘 타입. 경로 중첩(부모/{}/자식/{}) 추출에 쓴다.
PATH_SEGMENTS: dict[str, str] = {
    "virtualNetworks": "network",
    "subnets": "subnet",
    "networkSecurityGroups": "firewall",
    "networkInterfaces": "nic",
    "publicIPAddresses": "publicIp",
    "loadBalancers": "loadBalancer",
    "virtualMachines": "vm",
    "disks": "disk",
    "sshPublicKeys": "sshKey",
}
