"""Deterministic OpenTofu rendering for ResourcePlan.

The renderer is intentionally boring: it has no inference and no LLM boundary.
Every provider resource block is emitted from a typed ResourcePlan node, while
the provider-specific body only realizes the node's already-selected primitive.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from app.cloudkb.depkb.provider_cache import PINNED_PROVIDERS
from app.implementation.config import DEFAULT_CONTAINER_PORT

RESOURCE_PLAN_SCHEMA = "easydep-resource-plan"


def _label(value: Any) -> str:
    text = re.sub(r"[^a-zA-Z0-9_]", "_", str(value or "resource"))
    if text[:1].isdigit():
        text = f"r_{text}"
    return text or "resource"


def _cloud_label(value: Any) -> str:
    return re.sub(r"[^a-z0-9-]", "-", str(value or "resource").lower()).strip("-")


def _gcp_name(node_id: str, cloud_label: str, *, limit: int = 63) -> str:
    """GCP 이름 제한을 지키면서 긴 이름끼리 충돌하지 않는 HCL 표현식을 만든다."""

    candidate = f'"${{var.resource_prefix}}-{cloud_label}"'
    readable_length = limit - 9
    return (
        f"length({candidate}) <= {limit} ? {candidate} : "
        f'format("%s-%s", substr({candidate}, 0, {readable_length}), '
        f"substr(sha1({_quoted(node_id)}), 0, 8))"
    )


def _quoted(value: Any) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _shell_literal(value: Any) -> str:
    """설정값을 Bash가 변수나 명령으로 다시 해석하지 않도록 감싼다."""

    # POSIX 셸의 단일 인용부호 안에서는 $, 역따옴표, 역슬래시가 모두 문자로
    # 유지된다. 값 안의 단일 인용부호만 잠깐 인용을 닫아 안전하게 이어 붙인다.
    return "'" + str(value).replace("'", "'\"'\"'") + "'"


def _attrs(node: dict[str, Any]) -> dict[str, Any]:
    return dict(node.get("attributes") or {})


def _vm_sku(node: dict[str, Any]) -> str:
    """선택된 SKU는 ResourcePlan 값으로, 미선택 값은 기존 input variable로 낸다."""

    value = _attrs(node).get("vmSku")
    return _quoted(value) if isinstance(value, str) and value.strip() else "var.vm_sku"


def _port_expression(plan: dict[str, Any], owner: Any, field: str = "port") -> str:
    """고정 포트 또는 같은 workload interface를 가리키는 입력 변수를 돌려준다."""

    if isinstance(owner, int):
        return str(owner)
    if not isinstance(owner, dict):
        raise TypeError(
            f"Port owner must be an object or integer, got {type(owner).__name__}"
        )
    value = owner.get(field)
    if isinstance(value, int):
        return str(value)
    workload_id = str(owner.get("targetWorkloadRef") or owner.get("logicalRef") or "")
    workload: dict[str, Any] = next(
        (item for item in plan.get("workloads") or [] if item.get("id") == workload_id),
        {},
    )
    interfaces = list(workload.get("interfaces") or [])
    interface_id = str(owner.get("targetInterfaceRef") or "")
    if not interface_id and len(interfaces) == 1:
        interface_id = str(interfaces[0].get("id") or "")
    interface: dict[str, Any] = next(
        (item for item in interfaces if item.get("id") == interface_id),
        {},
    )
    interface_port = interface.get("port")
    if isinstance(interface_port, int):
        return str(interface_port)
    if workload_id and interface_id:
        return f"var.container_port_{_label(workload_id)}_{_label(interface_id)}"
    return str(DEFAULT_CONTAINER_PORT)


def _health_path(plan: dict[str, Any], health_owner: dict[str, Any]) -> str:
    """health resource가 가리키는 workload의 실제 경로를 읽는다.

    provider template의 late-binding 표식을 그대로 렌더링하면 생성 앱의 ``/healthz``와
    무관한 경로가 생긴다. logicalRef로 workload를 찾되 서로 다른 경로가 여러 개면
    임의로 하나를 고르지 않고 중단한다.
    """

    workload_id = str(health_owner.get("logicalRef") or "")
    workload: dict[str, Any] = next(
        (item for item in plan.get("workloads") or [] if str(item.get("id") or "") == workload_id),
        {},
    )
    paths = {
        str(interface.get("healthPath"))
        for interface in workload.get("interfaces") or []
        if isinstance(interface, dict) and interface.get("healthPath")
    }
    if len(paths) > 1:
        raise ValueError(f"Workload {workload_id} declares multiple health paths")
    return next(iter(paths), "/actuator/health")


def _block(kind: str, label: str, body: str) -> str:
    body = body.replace("; ", "\n").replace("{ ", "{\n").replace(" }", "\n}")
    indented = "\n".join(f"  {line}" if line else "" for line in body.splitlines())
    return f'resource "{kind}" "{label}" {{\n{indented}\n}}\n'


def _expression_map(values: dict[str, str]) -> str:
    return "{ " + ", ".join(f"{key} = {value}" for key, value in values.items()) + " }"


def _bootstrap_expression(
    node_id: str, vars_by_compute: dict[str, dict[str, str]]
) -> tuple[str, str]:
    compute_id = node_id.removeprefix("compute-template-")
    return (
        f"cloud-init_{_label(compute_id)}.yaml.tftpl",
        _expression_map(vars_by_compute.get(compute_id, {})),
    )


def _registry_expression(
    provider: str,
    plan: dict[str, Any],
    context: _Context,
    registry_ref: str,
    workload_label: str,
) -> str:
    return {
        "aws": f"{context.address(registry_ref)}.repository_url",
        "azure": (f'format("%s/{workload_label}", {context.address(registry_ref)}.login_server)'),
        "gcp": (
            f'"{plan.get("region")}-docker.pkg.dev/${{var.project_id}}/'
            f'${{{context.address(registry_ref)}.repository_id}}/{workload_label}"'
        ),
    }[provider]


@dataclass
class _Context:
    plan: dict[str, Any]

    def __post_init__(self) -> None:
        self.nodes = {str(item.get("id") or ""): item for item in self.plan.get("nodes") or []}
        self.addresses: dict[str, str] = {}
        for node_id, node in self.nodes.items():
            types = list(node.get("terraformTypes") or [])
            if node.get("handling") == "create" and types:
                self.addresses[node_id] = f"{types[0]}.{_label(node_id)}"
        self.references = list(self.plan.get("references") or [])
        self.consumed_reference_ids: set[str] = set()
        self.shared_values = {
            str(item.get("id") or ""): item for item in self.plan.get("sharedValues") or []
        }
        self.embedded_blocks = {
            str(item.get("id") or ""): item for item in self.plan.get("embeddedBlocks") or []
        }
        self.binding_slots = {
            str(item.get("id") or ""): item for item in self.plan.get("bindingSlots") or []
        }

    def address(self, node_id: str) -> str:
        try:
            return self.addresses[node_id]
        except KeyError as error:
            raise ValueError(f"No Terraform address for ResourcePlan node: {node_id}") from error

    def target(self, node_id: str, path: str) -> str | None:
        reference = next(
            (
                item
                for item in self.references
                if item.get("consumerRef") == node_id and item.get("consumerPath") == path
            ),
            None,
        )
        if reference is None:
            return None
        self.consumed_reference_ids.add(str(reference.get("id") or ""))
        return str(reference.get("producerRef") or "")

    def targets(self, node_id: str, path: str) -> list[str]:
        matches = [
            reference
            for reference in self.references
            if reference.get("consumerRef") == node_id and reference.get("consumerPath") == path
        ]
        self.consumed_reference_ids.update(str(item.get("id") or "") for item in matches)
        return [str(reference.get("producerRef") or "") for reference in matches]

    def ref(self, node_id: str, attribute: str = "id") -> str:
        return f"{self.address(node_id)}.{attribute}"

    def producer_expression(self, producer_ref: str, attribute: str) -> str:
        if producer_ref in self.shared_values:
            return f"local.{_label(producer_ref)}"
        if producer_ref in self.binding_slots:
            return f"var.{_label(producer_ref)}"
        if producer_ref == "boot-image":
            provider = str(self.plan.get("provider") or "")
            return {
                "aws": "var.boot_image_id",
                "azure": '"Canonical:0001-com-ubuntu-server-jammy:22_04-lts-gen2:latest"',
                "gcp": '"projects/ubuntu-os-cloud/global/images/family/ubuntu-2204-lts"',
            }[provider]
        return self.ref(producer_ref, attribute)

    def dependency_ref(self, node_id: str, path: str, *, required: bool = True) -> str:
        reference = next(
            (
                reference
                for reference in self.references
                if reference.get("consumerRef") == node_id and reference.get("consumerPath") == path
            ),
            None,
        )
        if not reference:
            if required:
                raise ValueError(f"ResourcePlan node {node_id} has no {path} dependency")
            return ""
        target = str(reference.get("producerRef") or "")
        attribute = str(reference.get("producerAttribute") or "id")
        self.consumed_reference_ids.add(str(reference.get("id") or ""))
        return self.producer_expression(target, attribute)

    def dependency_refs(self, node_id: str, path: str) -> list[str]:
        matches = [
            reference
            for reference in self.references
            if reference.get("consumerRef") == node_id and reference.get("consumerPath") == path
        ]
        self.consumed_reference_ids.update(str(item.get("id") or "") for item in matches)
        return [
            self.producer_expression(
                str(reference.get("producerRef") or ""),
                str(reference.get("producerAttribute") or "id"),
            )
            for reference in matches
        ]


def _locals_file(plan: dict[str, Any]) -> str:
    values = list(plan.get("sharedValues") or [])
    if not values:
        return ""
    assignments = "\n".join(
        f"  {_label(item.get('id'))} = {json.dumps(item.get('value'), ensure_ascii=False)}"
        for item in values
    )
    return f"locals {{\n{assignments}\n}}\n"


def _provider_file(provider: str, region: str) -> str:
    contract = PINNED_PROVIDERS[provider]
    local_name = {"azure": "azurerm", "gcp": "google"}.get(provider, provider)
    configuration = {
        "aws": (
            'provider "aws" {\n'
            f"  region                      = {_quoted(region)}\n"
            "  skip_credentials_validation = var.offline_validation\n"
            "  skip_requesting_account_id  = var.offline_validation\n"
            "  skip_metadata_api_check     = var.offline_validation\n"
            "  skip_region_validation      = var.offline_validation\n"
            "}"
        ),
        "azure": (
            'provider "azurerm" {\n  features {}\n  subscription_id = var.subscription_id\n}'
        ),
        "gcp": (
            f'provider "google" {{\n  project = var.project_id\n  region  = {_quoted(region)}\n}}'
        ),
    }[provider]
    return (
        "terraform {\n"
        '  required_version = ">= 1.8.0"\n'
        "  required_providers {\n"
        f"    {local_name} = {{\n"
        f"      source  = {_quoted(contract['source'])}\n"
        f"      version = {_quoted('= ' + contract['version'])}\n"
        "    }\n"
        "  }\n"
        "}\n\n"
        f"{configuration}\n"
    )


def _variable_file(plan: dict[str, Any]) -> str:
    provider = str(plan.get("provider") or "")
    lines = [
        'variable "resource_prefix" {',
        "  type    = string",
        '  default = "easydep"',
        "}",
        "",
        'variable "runtime_env" {',
        "  type      = string",
        '  default   = ""',
        "  sensitive = true",
        "}",
        "",
        'variable "vm_sku" {',
        "  type    = string",
        f'  default = "{ {"aws": "t3.small", "azure": "Standard_B2s", "gcp": "e2-small"}[provider] }"',
        "}",
    ]
    if provider == "azure":
        lines.extend(
            [
                "",
                'variable "subscription_id" {',
                "  type = string",
                "}",
                "",
                'variable "ssh_public_key" {',
                "  type      = string",
                "  sensitive = true",
                "}",
            ]
        )
    elif provider == "gcp":
        lines.extend(["", 'variable "project_id" {', "  type = string", "}"])
    else:
        lines.extend(
            [
                "",
                'variable "offline_validation" {',
                "  type        = bool",
                "  default     = false",
                '  description = "Internal switch used only by the offline template test."',
                "}",
                "",
                'variable "ssh_public_key" {',
                "  type      = string",
                "  sensitive = true",
                '  default   = ""',
                "}",
                "",
                'variable "boot_image_id" {',
                "  type = string",
                "  validation {",
                '    condition     = startswith(var.boot_image_id, "ami-")',
                '    error_message = "boot_image_id must be an explicit AMI id."',
                "  }",
                "}",
            ]
        )
    for workload in plan.get("workloads") or []:
        workload_id = _label(workload.get("id"))
        artifact = workload.get("artifact") or {}
        if artifact.get("kind") == "generatedApplication":
            lines.extend(
                [
                    "",
                    f'variable "image_digest_{workload_id}" {{',
                    "  type = string",
                    "  validation {",
                    f'    condition     = startswith(var.image_digest_{workload_id}, "sha256:")',
                    f'    error_message = "image_digest_{workload_id} must be an immutable sha256 digest."',
                    "  }",
                    "}",
                ]
            )
        for interface in workload.get("interfaces") or []:
            if isinstance(interface.get("port"), int):
                continue
            interface_id = _label(interface.get("id"))
            lines.extend(
                [
                    "",
                    f'variable "container_port_{workload_id}_{interface_id}" {{',
                    "  type = number",
                    "}",
                ]
            )
    secret_slots = [
        item for item in plan.get("bindingSlots") or [] if item.get("kind") == "secretReference"
    ]
    for item in secret_slots:
        variable_name = _label(item.get("id"))
        reference_validation = {
            "aws": (
                f'    condition     = can(regex("^arn:(aws|aws-us-gov|aws-cn):secretsmanager:[a-z0-9-]+:[0-9]{{12}}:secret:[A-Za-z0-9/_+=.@-]+$", var.{variable_name}))',
                "must be a Secrets Manager ARN",
            ),
            "azure": (
                f'    condition     = can(regex("^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft\\\\.KeyVault/vaults/[^/]+/secrets/[^/]+$", var.{variable_name}))',
                "must be an Azure Key Vault secret resource ID",
            ),
            "gcp": (
                f'    condition     = can(regex("^projects/[^/]+/secrets/[^/]+$", var.{variable_name}))',
                "must be a Secret Manager resource name",
            ),
        }[provider]
        lines.extend(
            [
                "",
                f'variable "{variable_name}" {{',
                "  type      = string",
                "  sensitive = true",
                "  validation {",
                reference_validation[0],
                f'    error_message = "{variable_name} {reference_validation[1]}; secret values are never accepted here."',
                "  }",
                "}",
            ]
        )
    external_endpoint_slots = [
        item for item in plan.get("bindingSlots") or [] if item.get("kind") == "externalEndpoint"
    ]
    for item in external_endpoint_slots:
        lines.extend(
            [
                "",
                f'variable "{_label(item.get("id"))}" {{',
                "  type = string",
                "}",
            ]
        )
    return "\n".join(lines) + "\n"


def _storage_setup_lines(
    plan: dict[str, Any],
    context: _Context,
    *,
    provider: str,
    compute_id: str,
    template_vars: dict[str, str],
) -> list[str]:
    """한 compute가 소유한 block device를 안전한 애플리케이션 경로로 준비한다.

    filesystem root를 container에 직접 보이면 ``lost+found`` 같은 시스템 항목이 앱의
    데이터 디렉터리에 섞인다. 따라서 상위 경로에 filesystem을 mount하고, 그 안의
    ``data/``만 고정 UID/GID 10001 애플리케이션에 제공한다.
    """

    bindings = [
        item
        for item in plan.get("storageBindings") or []
        if item.get("computeUnitRef") == compute_id
    ]
    lines: list[str] = []
    if provider == "aws" and bindings:
        # 여러 볼륨이 있는 경우 fallback이 같은 장치를 두 번 고르지 않게 기록한다.
        lines.append('CLAIMED_DISKS=""')
    for fallback_index, binding in enumerate(bindings):
        storage_id = str(binding.get("storageRef") or f"storage-{fallback_index}")
        storage_label = _label(storage_id)
        disk_node = context.nodes.get(f"data-disk-{storage_id}") or {}
        attributes = _attrs(disk_node)
        index = int(attributes.get("attachmentIndex", fallback_index))
        conventional_device = chr(ord("f") + index)
        if provider == "aws":
            lines.append('DISK_DEVICE=""')
            if disk_node.get("handling") == "create":
                disk_key = f"disk_id_{storage_label}"
                template_vars[disk_key] = context.ref(f"data-disk-{storage_id}")
                lines.extend(
                    [
                        f'EXPECTED_VOLUME="${{{disk_key}}}"',
                        'EXPECTED_VOLUME_NO_DASH=$(printf "%s" "$EXPECTED_VOLUME" | tr -d "-")',
                    ]
                )
            lines.append("for ATTEMPT in $(seq 1 60); do")
            if disk_node.get("handling") == "create":
                # 단독 VM의 EBS ID를 아는 경우에는 정확히 그 장치를 먼저 찾는다.
                lines.extend(
                    [
                    '  for CANDIDATE in /dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_"$EXPECTED_VOLUME_NO_DASH"*; do',
                    '    [ ! -b "$CANDIDATE" ] || DISK_DEVICE=$(readlink -f "$CANDIDATE")',
                    "  done",
                    "  for CANDIDATE in /dev/nvme*n1; do",
                    '    [ -b "$CANDIDATE" ] || continue',
                    '    CANDIDATE_NAME=$(basename "$CANDIDATE")',
                    '    CANDIDATE_SERIAL=$(tr -d "-[:space:]" < "/sys/block/$CANDIDATE_NAME/device/serial" 2>/dev/null || true)',
                    '    if [ "$CANDIDATE_SERIAL" = "$EXPECTED_VOLUME_NO_DASH" ]; then DISK_DEVICE="$CANDIDATE"; break; fi',
                    '    if command -v ebsnvme-id >/dev/null 2>&1 && ebsnvme-id "$CANDIDATE" 2>/dev/null | tr -d "-" | grep -q "$EXPECTED_VOLUME_NO_DASH"; then DISK_DEVICE="$CANDIDATE"; break; fi',
                    "  done",
                    ]
                )
            # Launch Template이 replica별로 만든 EBS는 plan 시점에 volume ID를 알 수
            # 없다. 요청 장치명과, root를 제외한 유일한 미사용 디스크 순서로 찾는다.
            lines.extend(
                [
                    f'  [ -n "$DISK_DEVICE" ] || [ ! -b "/dev/xvd{conventional_device}" ] || DISK_DEVICE="/dev/xvd{conventional_device}"',
                    f'  [ -n "$DISK_DEVICE" ] || [ ! -b "/dev/sd{conventional_device}" ] || DISK_DEVICE="/dev/sd{conventional_device}"',
                    '  if [ -z "$DISK_DEVICE" ]; then',
                    '    ROOT_SOURCE=$(readlink -f "$(findmnt -n -o SOURCE /)")',
                    '    ROOT_DISK=$(lsblk -nro PKNAME "$ROOT_SOURCE" 2>/dev/null | head -n 1)',
                    '    [ -n "$ROOT_DISK" ] || ROOT_DISK=$(basename "$ROOT_SOURCE")',
                    '    UNCLAIMED_DISKS=""',
                    '    for CANDIDATE in /dev/nvme*n1 /dev/xvd? /dev/sd?; do',
                    '      [ -b "$CANDIDATE" ] || continue',
                    '      DEVICE_NAME=$(basename "$CANDIDATE")',
                    '      [ "$DEVICE_NAME" != "$ROOT_DISK" ] || continue',
                    '      case " $CLAIMED_DISKS " in *" /dev/$DEVICE_NAME "*) continue ;; esac',
                    '      UNCLAIMED_DISKS="$UNCLAIMED_DISKS $CANDIDATE"',
                    '    done',
                    '    set -- $UNCLAIMED_DISKS',
                    '    [ "$#" -ne 1 ] || DISK_DEVICE="$1"',
                    '  fi',
                    '  [ -n "$DISK_DEVICE" ] && break',
                    "  sleep 2",
                    "done",
                ]
            )
        else:
            expected = {
                "azure": f"/dev/disk/azure/scsi1/lun{10 + index}",
                "gcp": f"/dev/disk/by-id/google-easydep-{_cloud_label(storage_id)}",
            }[provider]
            lines.extend(
                [
                    'DISK_DEVICE=""',
                    "for ATTEMPT in $(seq 1 60); do",
                    f"  [ ! -b {_quoted(expected)} ] || DISK_DEVICE={_quoted(expected)}",
                    '  [ -n "$DISK_DEVICE" ] && break',
                    "  sleep 2",
                    "done",
                ]
            )
        filesystem_path = f"/mnt/easydep/{storage_label}"
        guest_path = f"{filesystem_path}/data"
        lines.append(
            f'[ -n "$DISK_DEVICE" ] || {{ echo "disk {storage_id} did not attach; root=$ROOT_DISK devices=$(lsblk -dn -o NAME,TYPE | tr \'\\n\' \',\')" >&2; exit 1; }}'
            if provider == "aws"
            else f'[ -n "$DISK_DEVICE" ] || {{ echo "disk {storage_id} did not attach" >&2; exit 1; }}'
        )
        if provider == "aws":
            lines.append('CLAIMED_DISKS="$CLAIMED_DISKS $DISK_DEVICE"')
        lines.extend(
            [
                'if ! blkid "$DISK_DEVICE" >/dev/null 2>&1; then mkfs.ext4 "$DISK_DEVICE"; fi',
                'DISK_UUID=$(blkid -s UUID -o value "$DISK_DEVICE")',
                f"mkdir -p {_quoted(filesystem_path)}",
                f'grep -q "UUID=$DISK_UUID {_quoted(filesystem_path)} " /etc/fstab || printf "UUID=%s {filesystem_path} ext4 defaults,nofail 0 2\\n" "$DISK_UUID" >> /etc/fstab',
                f"mountpoint -q {_quoted(filesystem_path)} || mount {_quoted(filesystem_path)}",
                f"mkdir -p {_quoted(guest_path)}",
                f"chown 10001:10001 {_quoted(guest_path)}",
                f"chmod 0750 {_quoted(guest_path)}",
            ]
        )
    return lines


def _runtime_files(
    plan: dict[str, Any], context: _Context
) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    provider = str(plan.get("provider") or "")
    # 컨테이너 레지스트리에 로그인할 때 필요한 명령만 VM에 함께 설치한다.
    # Ubuntu 24.04에는 awscli APT 후보가 없으므로 AWS 공식 설치 파일을 사용한다.
    files: dict[str, str] = {}
    vars_by_compute: dict[str, dict[str, str]] = {}
    # 같은 Compose 네트워크의 호출은 container DNS로 직접 들어가므로 host port가
    # 필요 없다. 다른 VM이나 내부 LB가 호출하는 대상만 사설 host port를 연다.
    host_port_targets = {
        str(binding.get("targetWorkloadRef") or "")
        for binding in plan.get("runtimeBindings") or []
        if binding.get("kind") == "endpointEnvironment"
        and binding.get("strategy") != "containerDns"
    }
    for unit in plan.get("runtimeUnits") or []:
        compute_id = str(unit.get("computeUnitRef") or "")
        template_vars: dict[str, str] = {"runtime_env_b64": "base64encode(var.runtime_env)"}
        lines = [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "",
            "if command -v dnf >/dev/null 2>&1; then",
            # Amazon Linux 2023에는 curl-minimal이 이미 설치되어 있다. curl 패키지를
            # 다시 요청하면 두 패키지가 충돌하므로 기존 curl 명령을 그대로 사용한다.
            "  dnf install -y docker nvme-cli jq unzip",
            "else",
            "  apt-get update",
            "  apt-get install -y docker.io curl jq unzip",
            "  apt-get install -y docker-compose-v2 || apt-get install -y docker-compose-plugin || apt-get install -y docker-compose || true",
            "fi",
        ]
        if provider == "aws":
            lines.extend(
                [
                    "if ! command -v aws >/dev/null 2>&1; then",
                    '  case "$(uname -m)" in x86_64) AWS_ARCH=x86_64 ;; aarch64|arm64) AWS_ARCH=aarch64 ;; *) echo "unsupported AWS CLI architecture" >&2; exit 1 ;; esac',
                    '  curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-$${AWS_ARCH}.zip" -o /tmp/easydep-awscliv2.zip',
                    "  unzip -q /tmp/easydep-awscliv2.zip -d /tmp/easydep-aws-cli",
                    "  /tmp/easydep-aws-cli/aws/install",
                    "  rm -rf /tmp/easydep-aws-cli /tmp/easydep-awscliv2.zip",
                    "fi",
                ]
            )
        lines.extend(
            [
                "systemctl enable --now docker",
                "if ! docker compose version >/dev/null 2>&1 && ! command -v docker-compose >/dev/null 2>&1; then",
                '  case "$(uname -m)" in x86_64) COMPOSE_ARCH=x86_64; COMPOSE_SHA=db1889184726840f75c4f9c001048430d4f25b3be3cb084d3ddd762bc0aed576 ;; aarch64|arm64) COMPOSE_ARCH=aarch64; COMPOSE_SHA=732e3a84c1a0f67256ce80bc2598a24546b10ca05f9faa97efceb1171ece2ef7 ;; *) echo "unsupported Docker Compose architecture" >&2; exit 1 ;; esac',
                "  COMPOSE_PLUGIN=/usr/local/lib/docker/cli-plugins/docker-compose",
                '  mkdir -p "$(dirname "$COMPOSE_PLUGIN")"',
                '  curl -fsSL "https://github.com/docker/compose/releases/download/v5.5.1/docker-compose-linux-$${COMPOSE_ARCH}" -o "$COMPOSE_PLUGIN"',
                '  printf "%s  %s\\n" "$COMPOSE_SHA" "$COMPOSE_PLUGIN" | sha256sum -c -',
                '  chmod +x "$COMPOSE_PLUGIN"',
                "fi",
                'compose() { if docker compose version >/dev/null 2>&1; then docker compose "$@"; else docker-compose "$@"; fi; }',
            ]
        )
        lines.extend(
            _storage_setup_lines(
                plan,
                context,
                provider=provider,
                compute_id=compute_id,
                template_vars=template_vars,
            )
        )
        network = str(unit.get("containerNetwork") or "easydep")
        lines.append(
            f"docker network inspect {_quoted(network)} >/dev/null 2>&1 || docker network create {_quoted(network)}"
        )
        compose_lines = ["services:"]
        authenticated_registries: set[str] = set()
        azure_identity_ready = False
        for container in unit.get("containers") or []:
            workload_id = str(container.get("workloadRef") or "")
            workload_label = _label(workload_id)
            service_name = re.sub(r"[^a-z0-9-]", "-", workload_id.lower()).strip("-") or "workload"
            registry_ref = str(container.get("registryRef") or "")
            if registry_ref:
                registry_key = f"registry_{workload_label}"
                digest_key = f"image_digest_{workload_label}"
                template_vars[registry_key] = _registry_expression(
                    provider, plan, context, registry_ref, workload_label
                )
                template_vars[digest_key] = f"var.image_digest_{workload_label}"
                image = f"${{{registry_key}}}@${{{digest_key}}}"
                if registry_ref not in authenticated_registries:
                    authenticated_registries.add(registry_ref)
                    if provider == "aws":
                        lines.append(
                            f'aws ecr get-login-password --region {_quoted(plan.get("region"))} | docker login --username AWS --password-stdin "$(printf %s "${{{registry_key}}}" | cut -d/ -f1)"'
                        )
                    elif provider == "azure":
                        lines.extend(
                            [
                                "command -v az >/dev/null 2>&1 || curl -sL https://aka.ms/InstallAzureCLIDeb | bash",
                                "az login --identity --allow-no-subscriptions >/dev/null",
                                f'az acr login --name "$(printf %s "${{{registry_key}}}" | cut -d. -f1)"',
                            ]
                        )
                        azure_identity_ready = True
                    else:
                        lines.extend(
                            [
                                'REGISTRY_TOKEN=$(curl -fsS -H "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token" | jq -r .access_token)',
                                f'printf %s "$REGISTRY_TOKEN" | docker login -u oauth2accesstoken --password-stdin "$(printf %s "${{{registry_key}}}" | cut -d/ -f1)"',
                            ]
                        )
            else:
                image = str(container.get("image") or "")
            compose_ports: list[str] = []
            published_ports: set[str] = set()
            for interface in container.get("interfaces") or []:
                port = interface.get("port")
                if isinstance(port, int):
                    rendered_port = str(port)
                else:
                    key = f"port_{workload_label}_{_label(interface.get('id'))}"
                    template_vars[key] = (
                        f"var.container_port_{workload_label}_{_label(interface.get('id'))}"
                    )
                    rendered_port = f"${{{key}}}"
                # 다른 VM과 내부 LB는 VM의 사설 주소로 host port에 접속한다. public
                # interface만 publish하면 방화벽이 허용해도 container에 도달할 수 없다.
                if (
                    (
                        interface.get("exposure") == "public"
                        or (
                            interface.get("exposure") == "internal"
                            and workload_id in host_port_targets
                        )
                    )
                    and rendered_port not in published_ports
                ):
                    compose_ports.append(f'      - "{rendered_port}:{rendered_port}"')
                    published_ports.add(rendered_port)
            mount_args = [
                f"-v /mnt/easydep/{_label(item.get('storageRef'))}/data:{item.get('mountPath')}"
                for item in container.get("mounts") or []
                if isinstance(item.get("mountPath"), str)
            ]
            environment_names: list[str] = []
            endpoint_configuration_ids = {
                str(item.get("configurationRef") or "")
                for item in container.get("runtimeBindings") or []
                if item.get("kind") == "endpointEnvironment"
            }
            for binding in container.get("runtimeBindings") or []:
                if binding.get("kind") != "endpointEnvironment":
                    continue
                env_name = str(binding.get("environmentName") or "")
                host = binding.get("endpointHost")
                if binding.get("endpointProducerRef"):
                    endpoint_key = (
                        f"endpoint_{workload_label}_{_label(binding.get('configurationRef'))}"
                    )
                    owner = str(unit.get("bootstrapOwnerRef") or compute_id)
                    template_vars[endpoint_key] = context.dependency_ref(
                        owner, f"bootstrap.environment.{env_name}"
                    )
                    host = f"${{{endpoint_key}}}"
                elif binding.get("endpointValueBindingRef"):
                    endpoint_key = (
                        f"endpoint_{workload_label}_{_label(binding.get('configurationRef'))}"
                    )
                    template_vars[endpoint_key] = context.producer_expression(
                        str(binding.get("endpointValueBindingRef")), "value"
                    )
                    host = f"${{{endpoint_key}}}"
                    lines.append(f"export {env_name}={_quoted(host)}")
                    environment_names.append(env_name)
                    continue
                port = binding.get("port")
                if not isinstance(port, int):
                    target_label = _label(binding.get("targetWorkloadRef"))
                    interface_label = _label(binding.get("targetInterfaceRef"))
                    port_key = (
                        f"endpoint_port_{workload_label}_{_label(binding.get('configurationRef'))}"
                    )
                    template_vars[port_key] = f"var.container_port_{target_label}_{interface_label}"
                    port = f"${{{port_key}}}"
                projection = str(binding.get("projection") or "url")
                protocol = str(binding.get("protocol") or "http")
                if projection == "port":
                    value = str(port)
                elif projection == "host":
                    value = str(host or "")
                else:
                    value = f"{protocol}://{host}:{port}"
                lines.append(f"export {env_name}={_quoted(value)}")
                environment_names.append(env_name)
            for config in container.get("configuration") or []:
                config_id = str(config.get("id") or config.get("name") or "secret")
                if config_id in endpoint_configuration_ids:
                    continue
                env_name = str(config.get("name") or _label(config_id).upper())
                is_secret = config.get("sensitive") is True or str(config.get("kind") or "") in {
                    "secret",
                    "secretBinding",
                }
                if is_secret:
                    secret_key = f"secret_ref_{workload_label}_{_label(config_id)}"
                    variable_name = _label(f"secret-reference-{workload_id}-{config_id}")
                    template_vars[secret_key] = f"var.{variable_name}"
                    if provider == "aws":
                        lines.append(
                            f'export {env_name}="$(aws secretsmanager get-secret-value --region {_quoted(plan.get("region"))} --secret-id "${{{secret_key}}}" --query SecretString --output text)"'
                        )
                    elif provider == "azure":
                        if not azure_identity_ready:
                            lines.extend(
                                [
                                    "command -v az >/dev/null 2>&1 || curl -sL https://aka.ms/InstallAzureCLIDeb | bash",
                                    "az login --identity --allow-no-subscriptions >/dev/null",
                                ]
                            )
                            azure_identity_ready = True
                        lines.extend(
                            [
                                f'AZURE_SECRET_RESOURCE="${{{secret_key}}}"',
                                'case "$AZURE_SECRET_RESOURCE" in',
                                "  /subscriptions/*/resourceGroups/*/providers/Microsoft.KeyVault/vaults/*/secrets/*) AZURE_VAULT_NAME=$(printf '%s' \"$AZURE_SECRET_RESOURCE\" | sed -n 's#^.*/vaults/\\([^/]*\\)/secrets/.*$#\\1#p'); AZURE_SECRET_NAME=$(printf '%s' \"$AZURE_SECRET_RESOURCE\" | sed -n 's#^.*/secrets/\\([^/]*\\).*$#\\1#p'); SECRET_VALUE=$(az keyvault secret show --vault-name \"$AZURE_VAULT_NAME\" --name \"$AZURE_SECRET_NAME\" --query value -o tsv) ;;",
                                "  /subscriptions/*/vaults/*/secrets/*) echo \"Azure secret must use a full resource ID including resourceGroups and providers/Microsoft.KeyVault\" >&2; exit 1 ;;",
                                '  *) echo "Unsupported Azure Key Vault secret reference: $AZURE_SECRET_RESOURCE" >&2; exit 1 ;;',
                                "esac",
                                f'export {env_name}="$SECRET_VALUE"',
                            ]
                        )
                    else:
                        lines.extend(
                            [
                                f'SECRET_RESOURCE="${{{secret_key}}}"',
                                'SECRET_TOKEN=$(curl -fsS -H "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token" | jq -r .access_token)',
                                f'export {env_name}="$(curl -fsS -H "Authorization: Bearer $SECRET_TOKEN" "https://secretmanager.googleapis.com/v1/$SECRET_RESOURCE/versions/latest:access" | jq -r .payload.data | tr "_-" "/+" | base64 -d)"',
                            ]
                        )
                elif config.get("value") is not None:
                    lines.append(
                        f"export {env_name}={_shell_literal(config.get('value'))}"
                    )
                environment_names.append(env_name)
            compose_lines.extend(
                [
                    f"  {service_name}:",
                    f"    container_name: {_quoted(workload_id)}",
                    f"    image: {_quoted(image)}",
                    "    restart: unless-stopped",
                    "    env_file:",
                    "      - /opt/easydep/runtime/.env",
                ]
            )
            if compose_ports:
                compose_lines.append("    ports:")
                compose_lines.extend(compose_ports)
            if mount_args:
                compose_lines.append("    volumes:")
                compose_lines.extend(
                    f"      - {_quoted(item.removeprefix('-v '))}" for item in mount_args
                )
            if environment_names:
                compose_lines.append("    environment:")
                compose_lines.extend(f"      - {name}" for name in dict.fromkeys(environment_names))
            compose_lines.extend(["    networks:", "      - easydep"])
            # Keep the former Docker-run mapping as trace evidence. The command
            # is intentionally a comment: cloud-init now executes Compose only.
            lines.append(
                "# Compose service replaces: "
                + " ".join(["docker run", f"--name {_quoted(workload_id)}", *mount_args])
            )
        compose_lines.extend(
            [
                "networks:",
                "  easydep:",
                f"    name: {_quoted(network)}",
                "    external: true",
            ]
        )
        lines.extend(
            [
                "mkdir -p /opt/easydep/runtime",
                "cat > /opt/easydep/runtime/compose.yaml <<'EASYDEP_COMPOSE'",
                *compose_lines,
                "EASYDEP_COMPOSE",
                "compose --env-file /opt/easydep/runtime/.env -f /opt/easydep/runtime/compose.yaml up -d --remove-orphans",
                "sleep 2",
                *[
                    f"test \"$(docker inspect --format '{{{{.State.Running}}}}' {_quoted(str(container.get('workloadRef') or 'workload'))})\" = true"
                    for container in unit.get("containers") or []
                ],
                # 직렬 콘솔에서 이 표식을 확인하면 공개 주소가 없는 VM도 부팅 성공 여부를
                # 배포 도구가 확인할 수 있다. 비밀값이나 애플리케이션 출력은 포함하지 않는다.
                "printf '%s\\n' EASYDEP_BOOTSTRAP_OK > /dev/console",
            ]
        )
        template_name = f"bootstrap_{_label(compute_id)}.sh.tftpl"
        files[template_name] = "\n".join(lines) + "\n"
        vars_by_compute[compute_id] = template_vars
    return files, vars_by_compute


def _cloud_init_files(bootstrap_files: dict[str, str]) -> dict[str, str]:
    """기존 검증된 bootstrap을 실제 provider cloud-init input으로 감싼다."""

    files: dict[str, str] = {}
    for bootstrap_name, bootstrap in bootstrap_files.items():
        compute_label = bootstrap_name.removeprefix("bootstrap_").removesuffix(".sh.tftpl")
        cloud_name = f"cloud-init_{compute_label}.yaml.tftpl"
        files[cloud_name] = (
            "#cloud-config\n"
            "write_files:\n"
            "  - path: /opt/easydep/runtime/.env\n"
            "    permissions: '0600'\n"
            "    encoding: b64\n"
            '    content: "${runtime_env_b64}"\n'
            "  - path: /opt/easydep/bootstrap.sh\n"
            "    permissions: '0755'\n"
            "    content: |\n"
            + "\n".join(
                f"      {line}" if line else "      " for line in bootstrap.rstrip().splitlines()
            )
            + "\nruncmd:\n"
            "  - [ /opt/easydep/bootstrap.sh ]\n"
        )
    return files


def _aws_resources(
    plan: dict[str, Any],
    context: _Context,
    vars_by_compute: dict[str, dict[str, str]],
) -> str:
    def private_route_for(compute_id: str) -> str | None:
        """사설 compute의 인터넷 경로가 있으면 해당 Terraform 주소를 돌려준다."""

        route_id = f"private-default-route-{compute_id}"
        route = context.nodes.get(route_id) or {}
        if (
            route.get("handling") == "create"
            and (route.get("terraformTypes") or [""])[0] == "aws_route"
        ):
            return context.address(route_id)
        return None

    blocks: list[str] = []
    for node_id, node in context.nodes.items():
        if node.get("handling") != "create":
            continue
        kind = (node.get("terraformTypes") or [""])[0]
        label = _label(node_id)
        attributes = _attrs(node)
        if kind == "aws_ecr_repository":
            # 생성 이미지는 소스에서 다시 만들 수 있으므로 사용자가 destroy를 실행했을 때
            # 비어 있지 않은 ECR도 함께 정리되어야 한다. 영구 데이터 디스크의 retain 정책과
            # 달리 registry 자체를 남기면 smoke test와 일반 정리가 모두 실패한다.
            body = (
                f'name = "${{var.resource_prefix}}-{label}"\n'
                'image_tag_mutability = "IMMUTABLE"\n'
                "force_delete = true\n"
                "image_scanning_configuration { scan_on_push = true }"
            )
        elif kind == "aws_iam_role":
            body = (
                f'name = "${{var.resource_prefix}}-{label}"\n'
                'assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }] })'
            )
        elif kind == "aws_iam_instance_profile":
            body = f'name = "${{var.resource_prefix}}-{label}"\nrole = {context.dependency_ref(node_id, "role")}'
        elif kind == "aws_iam_role_policy_attachment":
            body = f'role = {context.dependency_ref(node_id, "role")}\npolicy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"'
        elif kind == "aws_iam_role_policy":
            body = (
                f'name = "${{var.resource_prefix}}-{label}"\n'
                f"role = {context.dependency_ref(node_id, 'role')}\n"
                'policy = jsonencode({ Version = "2012-10-17", Statement = [{ '
                'Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], '
                f"Resource = {context.dependency_ref(node_id, 'scope')} }}] }})"
            )
        elif kind == "aws_vpc":
            body = 'cidr_block = "10.80.0.0/16"\nenable_dns_support = true\nenable_dns_hostnames = true'
        elif kind in {"aws_internet_gateway", "aws_route_table"}:
            body = f"vpc_id = {context.dependency_ref(node_id, 'vpc_id')}"
        elif kind == "aws_route":
            route_table = context.dependency_ref(node_id, "route_table_id")
            gateway = context.target(node_id, "gateway_id")
            nat = context.target(node_id, "nat_gateway_id")
            body = f'route_table_id = {route_table}\ndestination_cidr_block = "0.0.0.0/0"'
            if gateway:
                body += f"\ngateway_id = {context.ref(gateway)}"
            elif nat:
                body += f"\nnat_gateway_id = {context.ref(nat)}"
        elif kind == "aws_subnet":
            # 공개 진입점도 별도 EIP 또는 Load Balancer가 담당한다. subnet 전체에
            # public IP를 자동 배정하면 같은 subnet에 나중에 추가된 VM까지 의도치 않게
            # 외부에 노출되므로, ResourcePlan의 명시적 public-address node만 사용한다.
            body = (
                f"vpc_id = {context.dependency_ref(node_id, 'vpc_id')}\n"
                f"cidr_block = {_quoted(attributes.get('cidr'))}\n"
                "map_public_ip_on_launch = false"
            )
            if attributes.get("zone"):
                body += f"\navailability_zone = {_quoted(attributes.get('zone'))}"
        elif kind == "aws_route_table_association":
            body = (
                f"subnet_id = {context.dependency_ref(node_id, 'subnet_id')}\n"
                f"route_table_id = {context.dependency_ref(node_id, 'route_table_id')}"
            )
        elif kind == "aws_eip":
            body = 'domain = "vpc"'
            if node_id.startswith("public-ip-"):
                body += f"\ninstance = {context.dependency_ref(node_id, 'instance')}"
        elif kind == "aws_nat_gateway":
            body = (
                f"allocation_id = {context.dependency_ref(node_id, 'allocation_id')}\n"
                f"subnet_id = {context.dependency_ref(node_id, 'subnet_id')}"
            )
        elif kind == "aws_security_group":
            public_paths = attributes.get("publicInterfaces") or []
            ingress = ""
            if public_paths:
                path = public_paths[0]
                port = _port_expression(plan, path)
                ingress = f'\ningress {{ from_port = {port}; to_port = {port}; protocol = "tcp"; cidr_blocks = ["0.0.0.0/0"] }}'
            source_groups = context.dependency_refs(node_id, "ingress.security_groups[]")
            if source_groups:
                allowed_ports = list(attributes.get("allowedPorts") or [])
                if not allowed_ports:
                    raise ValueError(
                        f"AWS source security-group ingress has no target port: {node_id}"
                    )
                for allowed_port in allowed_ports:
                    port = _port_expression(plan, allowed_port)
                    ingress += (
                        f'\ningress {{ from_port = {port}; to_port = {port}; protocol = "tcp"; '
                        f"security_groups = [{', '.join(source_groups)}] }}"
                    )
            for health_check in attributes.get("loadBalancerHealthChecks") or []:
                port = _port_expression(plan, health_check)
                ingress += (
                    f'\ningress {{ from_port = {port}; to_port = {port}; protocol = "tcp"; '
                    f"cidr_blocks = [{context.ref('network', 'cidr_block')}] }}"
                )
            # cloud-init의 package 설치와 container image pull에는 외부 HTTP/HTTPS가
            # 필요하다. 모든 protocol을 여는 대신 이 두 TCP port만 허용한다. workload
            # 사이 통신과 DNS는 VPC 내부 경로를 별도 규칙으로 표현한다.
            egress = (
                '\negress { from_port = 80; to_port = 80; protocol = "tcp"; '
                'cidr_blocks = ["0.0.0.0/0"] }'
                '\negress { from_port = 443; to_port = 443; protocol = "tcp"; '
                'cidr_blocks = ["0.0.0.0/0"] }'
                f'\negress {{ from_port = 0; to_port = 0; protocol = "-1"; '
                f'cidr_blocks = [{context.ref("network", "cidr_block")}] }}'
            )
            body = (
                f'name_prefix = "${{var.resource_prefix}}-{label}-"\n'
                f'vpc_id = {context.dependency_ref(node_id, "vpc_id")}'
                f"{egress}{ingress}"
            )
        elif kind == "aws_instance":
            profile = context.target(node_id, "iam_instance_profile")
            private_route = private_route_for(node_id)
            bootstrap_file, bootstrap_vars = _bootstrap_expression(node_id, vars_by_compute)
            body = (
                f"ami = {context.dependency_ref(node_id, 'ami')}\n"
                f"instance_type = {_vm_sku(node)}\n"
                f"subnet_id = {context.dependency_ref(node_id, 'subnet_id')}\n"
                f"vpc_security_group_ids = [{', '.join(context.dependency_refs(node_id, 'vpc_security_group_ids[]'))}]\n"
                f'user_data = templatefile("${{path.module}}/{bootstrap_file}", {bootstrap_vars})\n'
                'metadata_options { http_endpoint = "enabled"; http_tokens = "required" }\n'
                'root_block_device { encrypted = true; volume_type = "gp3" }'
            )
            if attributes.get("privateIp"):
                body += f"\nprivate_ip = {_quoted(attributes.get('privateIp'))}"
            if profile:
                body += f"\niam_instance_profile = {context.ref(profile, 'name')}"
            # 사설 VM은 cloud-init이 패키지를 받기 전에 NAT 경로가 준비되어야 한다.
            if private_route:
                body += f"\ndepends_on = [{private_route}]"
        elif kind == "aws_launch_template":
            profile = context.target(node_id, "iam_instance_profile")
            bootstrap_file, bootstrap_vars = _bootstrap_expression(node_id, vars_by_compute)
            body = (
                f"image_id = {context.dependency_ref(node_id, 'image_id')}\n"
                f"instance_type = {_vm_sku(node)}\n"
                f"vpc_security_group_ids = [{', '.join(context.dependency_refs(node_id, 'vpc_security_group_ids[]'))}]\n"
                f'user_data = base64encode(templatefile("${{path.module}}/{bootstrap_file}", {bootstrap_vars}))\n'
                'metadata_options { http_endpoint = "enabled"; http_tokens = "required" }\n'
                'block_device_mappings { device_name = "/dev/sda1"; '
                'ebs { encrypted = true; volume_type = "gp3" } }'
            )
            for child_id, child in context.embedded_blocks.items():
                if (
                    child.get("ownerRef") != node_id
                    or child.get("blockPath") != "block_device_mappings"
                ):
                    continue
                child_attrs = _attrs(child)
                index = int(child_attrs.get("attachmentIndex") or 0)
                device = chr(ord("f") + index)
                body += (
                    f'\nblock_device_mappings {{ device_name = "/dev/sd{device}"; '
                    f"ebs {{ volume_size = {int(child_attrs.get('capacityGiB') or 10)}; "
                    'volume_type = "gp3"; encrypted = true; delete_on_termination = '
                    f"{str(child_attrs.get('deletionPolicy') != 'retain').lower()} }} }}"
                )
            if profile:
                body += f"\niam_instance_profile {{ name = {context.ref(profile, 'name')} }}"
        elif kind == "aws_autoscaling_group":
            replica = int(attributes.get("replicaCount") or 1)
            subnets = context.dependency_refs(node_id, "vpc_zone_identifier[]")
            target_groups = context.targets(node_id, "target_group_arns[]")
            private_route = private_route_for(node_id)
            body = (
                f"min_size = {replica}\nmax_size = {replica}\ndesired_capacity = {replica}\n"
                f"vpc_zone_identifier = [{', '.join(subnets)}]\n"
                f'launch_template {{ id = {context.dependency_ref(node_id, "launch_template.id")}; version = "$Latest" }}'
            )
            # 사설 서브넷의 VM은 cloud-init 시작 전에 외부 통신 경로가 필요하다.
            # 서브넷 참조만으로는 Terraform이 이 생성 순서를 추론할 수 없다.
            if private_route:
                body += f"\ndepends_on = [{private_route}]"
            if target_groups:
                body += (
                    "\ntarget_group_arns = ["
                    + ", ".join(context.ref(item, "arn") for item in target_groups)
                    + "]"
                )
        elif kind == "aws_lb":
            subnets = context.targets(node_id, "subnets[]")
            internal = str(attributes.get("scheme") or "public") == "internal"
            body = f'name = substr("${{var.resource_prefix}}-lb-${{substr(sha1({_quoted(node_id)}), 0, 8)}}", 0, 32)\ninternal = {str(internal).lower()}\nload_balancer_type = "network"\nsubnets = [{", ".join(context.ref(item) for item in subnets)}]'
        elif kind == "aws_lb_target_group":
            port = _port_expression(plan, {**attributes, "logicalRef": node.get("logicalRef")})
            health: dict[str, Any] = next(
                (
                    block
                    for block in context.embedded_blocks.values()
                    if block.get("ownerRef") == node_id and block.get("blockPath") == "health_check"
                ),
                {},
            )
            path = _health_path(plan, health)
            body = f'name = substr("${{var.resource_prefix}}-tg-${{substr(sha1({_quoted(node_id)}), 0, 8)}}", 0, 32)\nport = {port}\nprotocol = "TCP"\nvpc_id = {context.dependency_ref(node_id, "vpc_id")}\nhealth_check {{ protocol = "HTTP"; path = {_quoted(path)}; port = "traffic-port" }}'
        elif kind == "aws_lb_listener":
            port = _port_expression(plan, {**attributes, "logicalRef": node.get("logicalRef")})
            body = f'load_balancer_arn = {context.dependency_ref(node_id, "load_balancer_arn")}\nport = {port}\nprotocol = "TCP"\ndefault_action {{ type = "forward"; target_group_arn = {context.dependency_ref(node_id, "default_action.target_group_arn")} }}'
        elif kind == "aws_ebs_volume":
            body = (
                "availability_zone = "
                f"{context.dependency_ref(node_id, 'availability_zone')}\n"
                f"size = {int(attributes.get('capacityGiB') or 10)}\n"
                'type = "gp3"\n'
                "encrypted = true"
            )
            if attributes.get("deletionPolicy") == "retain":
                body += "\nlifecycle { prevent_destroy = true }"
        elif kind == "aws_volume_attachment":
            index = int(attributes.get("attachmentIndex") or 0)
            device = chr(ord("f") + index)
            body = f'device_name = "/dev/sd{device}"\nvolume_id = {context.dependency_ref(node_id, "volume_id")}\ninstance_id = {context.dependency_ref(node_id, "instance_id")}'
        else:
            raise ValueError(f"Unsupported AWS ResourcePlan primitive: {node_id}/{kind}")
        blocks.append(_block(kind, label, body))
    return "\n".join(blocks)


def _azure_resources(
    plan: dict[str, Any],
    context: _Context,
    vars_by_compute: dict[str, dict[str, str]],
) -> str:
    def nat_dependencies_for(compute_id: str) -> list[str]:
        """해당 compute subnet을 NAT Gateway에 연결하는 Terraform 주소를 찾는다."""

        prefix = f"nat-association-{compute_id}-"
        return [
            context.address(node_id)
            for node_id, node in context.nodes.items()
            if node_id.startswith(prefix)
            and node.get("handling") == "create"
            and (node.get("terraformTypes") or [""])[0]
            == "azurerm_subnet_nat_gateway_association"
        ]

    blocks: list[str] = []
    region = str(plan.get("region") or "")
    for node_id, node in context.nodes.items():
        if node.get("handling") != "create":
            continue
        kind = (node.get("terraformTypes") or [""])[0]
        label = _label(node_id)
        cloud_label = _cloud_label(node_id)
        attributes = _attrs(node)
        rg_target = context.target(node_id, "resource_group_name")
        rg_name = (
            context.ref(rg_target, "name")
            if rg_target
            else "azurerm_resource_group.resource_group.name"
        )
        if kind == "azurerm_resource_group":
            body = f'name = "${{var.resource_prefix}}-rg"\nlocation = {_quoted(region)}'
        elif kind == "azurerm_container_registry":
            body = f'name = replace("${{var.resource_prefix}}{cloud_label}", "-", "")\nresource_group_name = {rg_name}\nlocation = {_quoted(region)}\nsku = "Basic"\nadmin_enabled = false'
        elif kind == "azurerm_user_assigned_identity":
            body = f'name = "${{var.resource_prefix}}-{cloud_label}"\nresource_group_name = {rg_name}\nlocation = {_quoted(region)}'
        elif kind == "azurerm_role_assignment":
            scope_target = context.target(node_id, "scope")
            principal_target = context.target(node_id, "principal_id")
            if not principal_target:
                raise ValueError(f"Azure role assignment has no principal: {node_id}")
            scope = (
                context.ref(scope_target)
                if scope_target in context.addresses
                else f"var.{_label('secret-reference-' + node_id.removeprefix('secret-access-binding-'))}"
            )
            role = (
                "AcrPull"
                if str(node.get("providerPrimitiveKind") or "") == "registry-pull-binding"
                else "Key Vault Secrets User"
            )
            body = f"scope = {scope}\nrole_definition_name = {_quoted(role)}\nprincipal_id = {context.ref(principal_target, 'principal_id')}"
        elif kind == "azurerm_virtual_network":
            body = f'name = "${{var.resource_prefix}}-vnet"\naddress_space = ["10.80.0.0/16"]\nlocation = {_quoted(region)}\nresource_group_name = {rg_name}'
        elif kind == "azurerm_subnet":
            network_target = context.target(node_id, "virtual_network_name")
            body = (
                f'name = "${{var.resource_prefix}}-{cloud_label}"\n'
                f"resource_group_name = {context.ref('resource-group', 'name')}\n"
                f"virtual_network_name = {context.ref(network_target or 'network', 'name')}\n"
                f"address_prefixes = [{_quoted(attributes.get('cidr'))}]"
            )
        elif kind == "azurerm_public_ip":
            body = f'name = "${{var.resource_prefix}}-{cloud_label}"\nlocation = {_quoted(region)}\nresource_group_name = {rg_name}\nallocation_method = "Static"\nsku = "Standard"'
        elif kind == "azurerm_nat_gateway":
            body = f'name = "${{var.resource_prefix}}-{cloud_label}"\nlocation = {_quoted(region)}\nresource_group_name = {rg_name}\nsku_name = "Standard"'
        elif kind == "azurerm_nat_gateway_public_ip_association":
            body = f"nat_gateway_id = {context.dependency_ref(node_id, 'nat_gateway_id')}\npublic_ip_address_id = {context.dependency_ref(node_id, 'public_ip_address_id')}"
        elif kind == "azurerm_subnet_nat_gateway_association":
            body = f"subnet_id = {context.dependency_ref(node_id, 'subnet_id')}\nnat_gateway_id = {context.dependency_ref(node_id, 'nat_gateway_id')}"
        elif kind == "azurerm_network_security_group":
            public_paths = attributes.get("publicInterfaces") or []
            rule = ""
            if public_paths:
                port = _port_expression(plan, public_paths[0])
                rule = (
                    '\nsecurity_rule { name = "public-http"; priority = 100; direction = "Inbound"; access = "Allow"; protocol = "Tcp"; '
                    f'source_port_range = "*"; destination_port_range = tostring({port}); source_address_prefix = "Internet"; destination_address_prefix = "*" }}'
                )
            for index, internal_rule in enumerate(attributes.get("internalRules") or [], start=1):
                connection_ref = str(internal_rule.get("connectionRef") or "internal")
                port = _port_expression(plan, internal_rule)
                source_prefix = context.dependency_ref(
                    node_id,
                    f"security_rule[{connection_ref}].source_address_prefix",
                )
                rule += (
                    f'\nsecurity_rule {{ name = "internal-{index}"; priority = {100 + index}; '
                    'direction = "Inbound"; access = "Allow"; protocol = "Tcp"; '
                    f'source_port_range = "*"; destination_port_range = tostring({port}); '
                    f'source_address_prefix = {source_prefix}; destination_address_prefix = "*" }}'
                )
            body = f'name = "${{var.resource_prefix}}-{cloud_label}"\nlocation = {_quoted(region)}\nresource_group_name = {rg_name}{rule}'
        elif kind == "azurerm_network_interface":
            subnet_target = context.target(node_id, "subnet_id")
            public_target = context.target(node_id, "public_ip_address_id")
            subnet_ref = context.ref(subnet_target or "")
            allocation = attributes.get("privateAddressAllocation") or "Dynamic"
            body = f'name = "${{var.resource_prefix}}-{cloud_label}"\nlocation = {_quoted(region)}\nresource_group_name = {rg_name}\nip_configuration {{ name = "primary"; subnet_id = {subnet_ref}; private_ip_address_allocation = {_quoted(allocation)}'
            if attributes.get("privateIp"):
                body += f"; private_ip_address = {_quoted(attributes.get('privateIp'))}"
            if public_target:
                body += f"; public_ip_address_id = {context.ref(public_target)}"
            body += " }"
        elif kind == "azurerm_network_interface_security_group_association":
            body = f"network_interface_id = {context.dependency_ref(node_id, 'network_interface_id')}\nnetwork_security_group_id = {context.dependency_ref(node_id, 'network_security_group_id')}"
        elif kind == "azurerm_linux_virtual_machine":
            nic_target = context.target(node_id, "network_interface_ids[]")
            identity_target = context.target(node_id, "identity.identity_ids[]")
            nic_ref = context.ref(nic_target or "")
            bootstrap_file, bootstrap_vars = _bootstrap_expression(node_id, vars_by_compute)
            body = (
                f'name = "${{var.resource_prefix}}-{cloud_label}"\nresource_group_name = {rg_name}\nlocation = {_quoted(region)}\n'
                f'size = {_vm_sku(node)}\nadmin_username = "easydep"\nnetwork_interface_ids = [{nic_ref}]\n'
                'disable_password_authentication = true\nadmin_ssh_key { username = "easydep"; public_key = var.ssh_public_key }\n'
                'os_disk { caching = "ReadWrite"; storage_account_type = "Standard_LRS" }\n'
                'source_image_reference { publisher = "Canonical"; offer = "0001-com-ubuntu-server-jammy"; sku = "22_04-lts-gen2"; version = "latest" }\n'
                f'custom_data = base64encode(templatefile("${{path.module}}/{bootstrap_file}", {bootstrap_vars}))'
            )
            if identity_target:
                body += f'\nidentity {{ type = "UserAssigned"; identity_ids = [{context.ref(identity_target)}] }}'
            if attributes.get("zone"):
                body += f"\nzone = {_quoted(attributes.get('zone'))}"
            nat_dependencies = nat_dependencies_for(node_id)
            if nat_dependencies:
                body += f"\ndepends_on = [{', '.join(nat_dependencies)}]"
        elif kind == "azurerm_linux_virtual_machine_scale_set":
            subnet_target = context.target(node_id, "network_interface.ip_configuration.subnet_id")
            filter_target = context.target(node_id, "network_interface.network_security_group_id")
            identity_target = context.target(node_id, "identity.identity_ids[]")
            replica = int(attributes.get("replicaCount") or 1)
            zones = attributes.get("zones") or ["1"]
            bootstrap_file, bootstrap_vars = _bootstrap_expression(node_id, vars_by_compute)
            body = (
                f'name = "${{var.resource_prefix}}-{cloud_label}"\nresource_group_name = {rg_name}\nlocation = {_quoted(region)}\n'
                f'sku = {_vm_sku(node)}\ninstances = {replica}\nzones = {json.dumps(zones)}\nadmin_username = "easydep"\n'
                'disable_password_authentication = true\nadmin_ssh_key { username = "easydep"; public_key = var.ssh_public_key }\n'
                'os_disk { caching = "ReadWrite"; storage_account_type = "Standard_LRS" }\n'
                'source_image_reference { publisher = "Canonical"; offer = "0001-com-ubuntu-server-jammy"; sku = "22_04-lts-gen2"; version = "latest" }\n'
                f'network_interface {{ name = "primary"; primary = true; network_security_group_id = {context.ref(filter_target or "")}\n'
                f'  ip_configuration {{ name = "primary"; primary = true; subnet_id = {context.ref(subnet_target or "")}'
            )
            backend_targets = context.targets(
                node_id,
                "network_interface.ip_configuration.load_balancer_backend_address_pool_ids[]",
            )
            if backend_targets:
                body += (
                    "; load_balancer_backend_address_pool_ids = ["
                    + ", ".join(context.ref(item) for item in backend_targets)
                    + "]"
                )
            body += " }\n}\n"
            body += f'custom_data = base64encode(templatefile("${{path.module}}/{bootstrap_file}", {bootstrap_vars}))'
            for child_id, child in context.embedded_blocks.items():
                if child.get("ownerRef") != node_id or child.get("blockPath") != "data_disk":
                    continue
                child_attrs = _attrs(child)
                index = int(child_attrs.get("attachmentIndex") or 0)
                storage_ref = _cloud_label(child_attrs.get("storageRef") or child_id)
                body += (
                    f'\ndata_disk {{ lun = {10 + index}; caching = "ReadWrite"; '
                    # ``name`` is an actual VMSS data-disk profile coordinate,
                    # not a tag on the scale set.  AzureRM 5.0.1 exposes no
                    # data_disk.delete_option argument, so do not emit an
                    # unsupported pseudo-retention field; the descriptor below
                    # tells the deployment package to record managed-disk IDs
                    # by VMSS instance and LUN before destroy.
                    f'name = "${{var.resource_prefix}}-{storage_ref}"; '
                    'create_option = "Empty"; '
                    f'storage_account_type = "Standard_LRS"; disk_size_gb = {int(child_attrs.get("capacityGiB") or 10)} }}'
                )
            retained_storage = [
                str(_attrs(child).get("storageRef") or child_id)
                for child_id, child in context.embedded_blocks.items()
                if child.get("ownerRef") == node_id
                and child.get("blockPath") == "data_disk"
                and _attrs(child).get("deletionPolicy") == "retain"
            ]
            if retained_storage:
                body += (
                    "\ntags = { EasyDepRetainedReplicaDisks = "
                    f"{_quoted(','.join(sorted(retained_storage)))} }}"
                )
            if identity_target:
                body += f'\nidentity {{ type = "UserAssigned"; identity_ids = [{context.ref(identity_target)}] }}'
            nat_dependencies = nat_dependencies_for(node_id)
            if nat_dependencies:
                body += f"\ndepends_on = [{', '.join(nat_dependencies)}]"
        elif kind == "azurerm_lb":
            frontend_child = next(
                (
                    child_id
                    for child_id, child in context.embedded_blocks.items()
                    if child.get("blockPath") == "frontend_ip_configuration"
                    and child.get("ownerRef") == node_id
                ),
                None,
            )
            if not frontend_child:
                raise ValueError(f"Azure load balancer has no frontend: {node_id}")
            internal = str(attributes.get("scheme") or "public") == "internal"
            if internal:
                subnet_target = context.target(frontend_child, "subnet_id")
                frontend_name = context.dependency_ref(frontend_child, "name")
                frontend = (
                    f"frontend_ip_configuration {{ name = {frontend_name}; "
                    'private_ip_address_allocation = "Dynamic"; '
                    f"subnet_id = {context.ref(subnet_target or '')} }}"
                )
            else:
                public_target = context.target(frontend_child, "public_ip_address_id")
                frontend_name = context.dependency_ref(frontend_child, "name")
                frontend = (
                    f"frontend_ip_configuration {{ name = {frontend_name}; "
                    f"public_ip_address_id = {context.ref(public_target or '')} }}"
                )
            body = f'name = "${{var.resource_prefix}}-{cloud_label}"\nlocation = {_quoted(region)}\nresource_group_name = {rg_name}\nsku = "Standard"\n{frontend}'
        elif kind == "azurerm_lb_backend_address_pool":
            body = f'name = "backend"\nloadbalancer_id = {context.dependency_ref(node_id, "loadbalancer_id")} '
        elif kind == "azurerm_lb_probe":
            port = _port_expression(plan, {**attributes, "logicalRef": node.get("logicalRef")})
            body = f'name = "health"\nloadbalancer_id = {context.dependency_ref(node_id, "loadbalancer_id")}\nprotocol = "Http"\nport = {port}\nrequest_path = {_quoted(_health_path(plan, node))}'
        elif kind == "azurerm_lb_rule":
            port = _port_expression(
                plan,
                {**attributes, "logicalRef": node.get("logicalRef")},
                "frontendPort",
            )
            frontend_name = context.dependency_ref(node_id, "frontend_ip_configuration_name")
            body = f'name = "http"\nloadbalancer_id = {context.dependency_ref(node_id, "loadbalancer_id")}\nprotocol = "Tcp"\nfrontend_port = {port}\nbackend_port = {port}\nfrontend_ip_configuration_name = {frontend_name}\nbackend_address_pool_ids = [{context.dependency_ref(node_id, "backend_address_pool_ids[]")}]\nprobe_id = {context.dependency_ref(node_id, "probe_id")} '
        elif kind == "azurerm_managed_disk":
            body = f'name = "${{var.resource_prefix}}-{cloud_label}"\nlocation = {_quoted(region)}\nresource_group_name = {rg_name}\nstorage_account_type = "Standard_LRS"\ncreate_option = "Empty"\ndisk_size_gb = {int(attributes.get("capacityGiB") or 10)}\nzone = {context.dependency_ref(node_id, "zone")}'
            if attributes.get("deletionPolicy") == "retain":
                body += "\nlifecycle { prevent_destroy = true }"
        elif kind == "azurerm_virtual_machine_data_disk_attachment":
            index = int(attributes.get("attachmentIndex") or 0)
            body = f'managed_disk_id = {context.dependency_ref(node_id, "managed_disk_id")}\nvirtual_machine_id = {context.dependency_ref(node_id, "virtual_machine_id")}\nlun = {10 + index}\ncaching = "ReadWrite"'
        elif kind == "azurerm_network_interface_backend_address_pool_association":
            body = f'network_interface_id = {context.dependency_ref(node_id, "network_interface_id")}\nip_configuration_name = "primary"\nbackend_address_pool_id = {context.dependency_ref(node_id, "backend_address_pool_id")}'
        else:
            raise ValueError(f"Unsupported Azure ResourcePlan primitive: {node_id}/{kind}")
        blocks.append(_block(kind, label, body))
    return "\n".join(blocks)


def _gcp_resources(
    plan: dict[str, Any],
    context: _Context,
    vars_by_compute: dict[str, dict[str, str]],
) -> str:
    blocks: list[str] = []
    region = str(plan.get("region") or "")
    # 사설 VM은 cloud-init에서 이미지와 패키지를 내려받는다. 리소스 참조만으로는
    # Compute와 Cloud NAT가 동시에 만들어질 수 있으므로, NAT가 있는 계획에서는
    # 사설 Compute가 NAT 준비를 명시적으로 기다리게 한다.
    cloud_nat_dependencies = [
        context.address(node_id)
        for node_id, node in context.nodes.items()
        if node.get("handling") == "create"
        and (node.get("terraformTypes") or [""])[0] == "google_compute_router_nat"
    ]
    for node_id, node in context.nodes.items():
        if node.get("handling") != "create":
            continue
        kind = (node.get("terraformTypes") or [""])[0]
        label = _label(node_id)
        cloud_label = _cloud_label(node_id)
        attributes = _attrs(node)
        if kind == "google_artifact_registry_repository":
            body = f"location = {_quoted(region)}\nrepository_id = {_gcp_name(node_id, cloud_label)}\nformat = \"DOCKER\""
        elif kind == "google_service_account":
            body = f'account_id = substr("${{var.resource_prefix}}-id-${{substr(sha1({_quoted(node_id)}), 0, 6)}}", 0, 30)\ndisplay_name = "EasyDep {cloud_label}"'
        elif kind == "google_artifact_registry_repository_iam_member":
            scope = context.target(node_id, "scope")
            principal = context.target(node_id, "member")
            body = (
                f"project = var.project_id\nlocation = {context.ref(scope or '', 'location')}\n"
                f"repository = {context.ref(scope or '', 'repository_id')}\n"
                'role = "roles/artifactregistry.reader"\n'
                f'member = "serviceAccount:${{{context.address(principal or "")}.email}}"'
            )
        elif kind == "google_secret_manager_secret_iam_member":
            principal = context.target(node_id, "member")
            body = f'project = var.project_id\nsecret_id = {context.dependency_ref(node_id, "scope")}\nrole = "roles/secretmanager.secretAccessor"\nmember = "serviceAccount:${{{context.address(principal or "")}.email}}"'
        elif kind == "google_compute_network":
            body = f'name = {_gcp_name(node_id, cloud_label)}\nauto_create_subnetworks = false\nrouting_mode = "REGIONAL"'
        elif kind == "google_compute_subnetwork":
            body = f'name = {_gcp_name(node_id, cloud_label)}\nregion = {_quoted(region)}\nnetwork = {context.dependency_ref(node_id, "network")}\nip_cidr_range = {_quoted(attributes.get("cidr"))}\nprivate_ip_google_access = true'
        elif kind == "google_compute_router":
            body = f'name = {_gcp_name(node_id, cloud_label)}\nregion = {_quoted(region)}\nnetwork = {context.dependency_ref(node_id, "network")} '
        elif kind == "google_compute_router_nat":
            body = f'name = {_gcp_name(node_id, cloud_label)}\nregion = {_quoted(region)}\nrouter = {context.dependency_ref(node_id, "router")}\nnat_ip_allocate_option = "AUTO_ONLY"\nsource_subnetwork_ip_ranges_to_nat = "LIST_OF_SUBNETWORKS"'
            subnet_targets = context.targets(node_id, "subnetwork[].name")
            for subnet in subnet_targets:
                body += f'\nsubnetwork {{ name = {context.ref(subnet)}; source_ip_ranges_to_nat = ["ALL_IP_RANGES"] }}'
        elif kind == "google_compute_firewall":
            public_paths = attributes.get("publicInterfaces") or []
            allowed_ports = public_paths or attributes.get("allowedPorts") or []
            if not allowed_ports:
                raise ValueError(f"GCP firewall has no explicit allowed port: {node_id}")
            ports = [_port_expression(plan, path) for path in allowed_ports]
            target_tags = context.dependency_refs(node_id, "target_tags[]")
            source_tags = context.dependency_refs(node_id, "source_tags[]")
            port_values = "[" + ", ".join(f"tostring({port})" for port in ports) + "]"
            body = f'name = {_gcp_name(node_id, cloud_label)}\nnetwork = {context.dependency_ref(node_id, "network")}\ndirection = "INGRESS"\ntarget_tags = [{", ".join(target_tags)}]\nallow {{ protocol = "tcp"; ports = {port_values} }}'
            if source_tags:
                body += f"\nsource_tags = [{', '.join(source_tags)}]"
            else:
                body += '\nsource_ranges = ["0.0.0.0/0"]'
        elif kind == "google_compute_instance":
            gcp_subnet = context.target(node_id, "network_interface.subnetwork")
            tag_values = context.dependency_refs(node_id, "tags[]")
            gcp_identity = context.target(node_id, "service_account.email")
            traffic_label = context.dependency_ref(
                node_id, "labels.easydep_traffic_tag", required=False
            )
            public_address = context.target(node_id, "network_interface.access_config.nat_ip")
            zone = attributes.get("zone") or f"{region}-a"
            bootstrap_file, bootstrap_vars = _bootstrap_expression(node_id, vars_by_compute)
            body = (
                f'name = {_gcp_name(node_id, cloud_label)}\nzone = {_quoted(zone)}\nmachine_type = {_vm_sku(node)}\n'
                f"boot_disk {{ initialize_params {{ image = {context.dependency_ref(node_id, 'boot_disk.initialize_params.image')} }} }}\n"
                f"network_interface {{ subnetwork = {context.ref(gcp_subnet or '')}"
            )
            if attributes.get("privateIp"):
                body += f"; network_ip = {_quoted(attributes.get('privateIp'))}"
            if public_address:
                body += f"; access_config {{ nat_ip = {context.dependency_ref(node_id, 'network_interface.access_config.nat_ip')} }}"
            body += " }\n"
            if gcp_identity:
                body += f'service_account {{ email = {context.ref(gcp_identity, "email")}; scopes = ["cloud-platform"] }}\n'
            body += f'metadata = {{ user-data = templatefile("${{path.module}}/{bootstrap_file}", {bootstrap_vars}) }}\ntags = [{", ".join(tag_values)}]'
            if traffic_label:
                body += f'\nlabels = {{ easydep_traffic_tag = {traffic_label} }}'
            if not public_address and cloud_nat_dependencies:
                body += f"\ndepends_on = [{', '.join(cloud_nat_dependencies)}]"
        elif kind == "google_compute_instance_template":
            gcp_template_subnet = context.target(node_id, "network_interface.subnetwork")
            gcp_template_identity = context.target(node_id, "service_account.email")
            tag_values = context.dependency_refs(node_id, "tags[]")
            traffic_label = context.dependency_ref(
                node_id, "labels.easydep_traffic_tag", required=False
            )
            bootstrap_file, bootstrap_vars = _bootstrap_expression(node_id, vars_by_compute)
            body = (
                f'name_prefix = substr("${{var.resource_prefix}}-{cloud_label}-", 0, 37)\nmachine_type = {_vm_sku(node)}\n'
                f"disk {{ source_image = {context.dependency_ref(node_id, 'boot_disk.initialize_params.image')}; auto_delete = true; boot = true }}\n"
                f"network_interface {{ subnetwork = {context.ref(gcp_template_subnet or '')} }}\n"
            )
            if gcp_template_identity:
                body += f'service_account {{ email = {context.ref(gcp_template_identity, "email")}; scopes = ["cloud-platform"] }}\n'
            for child_id, child in context.embedded_blocks.items():
                if child.get("ownerRef") != node_id or child.get("blockPath") != "disk":
                    continue
                child_attrs = _attrs(child)
                storage_ref = _cloud_label(child_attrs.get("storageRef") or child_id)
                body += (
                    f'\ndisk {{ device_name = "easydep-{storage_ref}"; type = "PERSISTENT"; '
                    f"disk_size_gb = {int(child_attrs.get('capacityGiB') or 10)}; "
                    'disk_type = "pd-balanced"; auto_delete = '
                    f"{str(child_attrs.get('deletionPolicy') != 'retain').lower()}; boot = false }}"
                )
            body += f'\ntags = [{", ".join(tag_values)}]'
            if traffic_label:
                body += f'\nlabels = {{ easydep_traffic_tag = {traffic_label} }}'
            body += f'\nmetadata = {{ user-data = templatefile("${{path.module}}/{bootstrap_file}", {bootstrap_vars}) }}\nlifecycle {{ create_before_destroy = true }}'
        elif kind in {
            "google_compute_region_instance_group_manager",
            "google_compute_instance_group_manager",
        }:
            template = context.target(node_id, "version.instance_template")
            template_ref = context.ref(template or "", "self_link")
            replica = int(attributes.get("replicaCount") or 1)
            zones = attributes.get("zones") or []
            body = f'name = {_gcp_name(node_id, cloud_label)}\nbase_instance_name = {_gcp_name(node_id, cloud_label, limit=58)}\ntarget_size = {replica}\nversion {{ instance_template = {template_ref} }}'
            if kind == "google_compute_region_instance_group_manager":
                body += f"\nregion = {_quoted(region)}"
                if zones:
                    body += f"\ndistribution_policy_zones = {json.dumps(zones)}"
            else:
                body += f"\nzone = {_quoted(zones[0] if zones else region + '-a')}"
            # An instance-template disk with auto_delete=false alone is not a
            # stateful MIG contract.  Mark each retained per-replica device as
            # stateful so the manager detaches it (rather than deleting it)
            # when an instance or the group is removed.
            for child_id, child in context.embedded_blocks.items():
                if child.get("ownerRef") != (template or "") or child.get("blockPath") != "disk":
                    continue
                child_attrs = _attrs(child)
                if child_attrs.get("deletionPolicy") != "retain":
                    continue
                storage_ref = _cloud_label(child_attrs.get("storageRef") or child_id)
                body += (
                    f'\nstateful_disk {{ device_name = "easydep-{storage_ref}"; '
                    'delete_rule = "NEVER" }'
                )
            if cloud_nat_dependencies:
                body += f"\ndepends_on = [{', '.join(cloud_nat_dependencies)}]"
        elif kind == "google_compute_address":
            body = f'name = {_gcp_name(node_id, cloud_label)}\nregion = {_quoted(region)}\naddress_type = "EXTERNAL"'
        elif kind == "google_compute_region_health_check":
            port = _port_expression(plan, {**attributes, "logicalRef": node.get("logicalRef")})
            body = f'name = {_gcp_name(node_id, cloud_label)}\nregion = {_quoted(region)}\nhttp_health_check {{ port = {port}; request_path = {_quoted(_health_path(plan, node))} }}'
        elif kind == "google_compute_region_backend_service":
            health = context.target(node_id, "health_checks[]")
            health_ref = context.ref(health or "")
            backend_block = next(
                (
                    block_id
                    for block_id, block in context.embedded_blocks.items()
                    if block.get("ownerRef") == node_id and block.get("blockPath") == "backend"
                ),
                None,
            )
            if not backend_block:
                raise ValueError(f"GCP backend service has no backend block: {node_id}")
            group_ref = context.dependency_ref(backend_block, "group")
            scheme = "INTERNAL" if attributes.get("scheme") == "internal" else "EXTERNAL"
            body = f'name = {_gcp_name(node_id, cloud_label)}\nregion = {_quoted(region)}\nprotocol = "TCP"\nload_balancing_scheme = "{scheme}"\nhealth_checks = [{health_ref}]\nbackend {{ group = {group_ref} }}'
        elif kind == "google_compute_forwarding_rule":
            backend_ref = context.dependency_ref(node_id, "backend_service")
            internal = attributes.get("scheme") == "internal"
            scheme = "INTERNAL" if internal else "EXTERNAL"
            port = _port_expression(plan, {**attributes, "logicalRef": node.get("logicalRef")})
            body = f'name = {_gcp_name(node_id, cloud_label)}\nregion = {_quoted(region)}\nload_balancing_scheme = "{scheme}"\nip_protocol = "TCP"\nports = [tostring({port})]\nbackend_service = {backend_ref}'
            if internal:
                body += (
                    f"\nnetwork = {context.dependency_ref(node_id, 'network')}"
                    f"\nsubnetwork = {context.dependency_ref(node_id, 'subnetwork')}"
                )
        elif kind == "google_compute_disk":
            body = f'name = {_gcp_name(node_id, cloud_label)}\nzone = {context.dependency_ref(node_id, "zone")}\nsize = {int(attributes.get("capacityGiB") or 10)}\ntype = "pd-balanced"'
            if attributes.get("deletionPolicy") == "retain":
                body += "\nlifecycle { prevent_destroy = true }"
        elif kind == "google_compute_attached_disk":
            storage_ref = _cloud_label(attributes.get("storageRef") or node_id)
            body = f'device_name = "easydep-{storage_ref}"\ndisk = {context.dependency_ref(node_id, "disk")}\ninstance = {context.dependency_ref(node_id, "instance")}\nzone = {context.dependency_ref(node_id, "zone")}'
        else:
            raise ValueError(f"Unsupported GCP ResourcePlan primitive: {node_id}/{kind}")
        blocks.append(_block(kind, label, body))
    return "\n".join(blocks)


def _output_file(plan: dict[str, Any], context: _Context) -> str:
    provider = str(plan.get("provider") or "")
    outputs: list[str] = []
    for unit in plan.get("runtimeUnits") or []:
        for container in unit.get("containers") or []:
            registry_ref = str(container.get("registryRef") or "")
            if not registry_ref:
                continue
            workload_id = str(container.get("workloadRef") or "")
            workload_label = _label(workload_id)
            expression = _registry_expression(provider, plan, context, registry_ref, workload_label)
            output_name = f"registry_{workload_label}_url"
            outputs.extend(
                [
                    f'output "{output_name}" {{',
                    f"  value = {expression}",
                    "}",
                    "",
                ]
            )
    for path in plan.get("networkPaths") or []:
        if not isinstance(path, dict) or path.get("kind") != "publicIngress":
            continue
        compute_id = str(path.get("computeUnitRef") or "")
        ingress_kind = str(path.get("ingressKind") or "")
        node_id = (
            f"public-ip-{compute_id}"
            if ingress_kind == "directPublicIp" or provider == "azure"
            else f"load-balancer-{compute_id}"
        )
        if node_id not in context.addresses:
            continue
        workload_id = str(path.get("targetWorkloadRef") or "")
        interface_id = str(path.get("targetInterfaceRef") or "")
        endpoint_attribute = {
            "aws": "public_ip" if ingress_kind == "directPublicIp" else "dns_name",
            "azure": "ip_address",
            "gcp": "address" if ingress_kind == "directPublicIp" else "ip_address",
        }[provider]
        endpoint = context.ref(node_id, endpoint_attribute)
        endpoint_name = f"public_endpoint_{_label(compute_id)}_{_label(interface_id)}"
        outputs.extend([f'output "{endpoint_name}" {{', f"  value = {endpoint}", "}", ""])
        port = path.get("port")
        if not isinstance(port, int):
            port = f"var.container_port_{_label(workload_id)}_{_label(interface_id)}"
        workload: dict[str, Any] = next(
            (item for item in plan.get("workloads") or [] if item.get("id") == workload_id),
            {},
        )
        interface: dict[str, Any] = next(
            (item for item in workload.get("interfaces") or [] if item.get("id") == interface_id),
            {},
        )
        health = str(interface.get("healthPath") or "/actuator/health")
        health_name = f"health_url_{_label(compute_id)}_{_label(interface_id)}"
        outputs.extend(
            [
                f'output "{health_name}" {{',
                f'  value = format("http://%s:%s%s", {endpoint}, {port}, {_quoted(health)})',
                "}",
                "",
            ]
        )
        if ingress_kind == "directPublicIp" and provider == "azure":
            outputs.extend(
                [
                    f'output "ssh_command_{_label(compute_id)}" {{',
                    f'  value = format("ssh easydep@%s", {endpoint})',
                    "}",
                    "",
                ]
            )
    for block_id, block in context.embedded_blocks.items():
        attributes = _attrs(block)
        if not (
            attributes.get("perReplica") is True
            and attributes.get("deletionPolicy") == "retain"
        ):
            continue
        owner = str(block.get("ownerRef") or "")
        if owner not in context.addresses:
            continue
        storage_ref = str(attributes.get("storageRef") or block_id)
        output_name = f"retained_replica_disk_{_label(storage_ref)}"
        aws_asg_owner = (
            owner.removeprefix("compute-template-")
            if provider == "aws" and owner.removeprefix("compute-template-") in context.addresses
            else owner
        )
        gcp_mig_owner = (
            owner.removeprefix("compute-template-")
            if provider == "gcp" and owner.removeprefix("compute-template-") in context.addresses
            else owner
        )
        details = [
            f"provider = {_quoted(provider)}",
            f"storage_ref = {_quoted(storage_ref)}",
            f"owner_resource_id = {context.ref(aws_asg_owner if provider == 'aws' else gcp_mig_owner)}",
        ]
        if provider == "aws":
            device = chr(ord("f") + int(attributes.get("attachmentIndex") or 0))
            details.extend(
                [
                    f"autoscaling_group_name = {context.ref(aws_asg_owner, 'name')}",
                    f"launch_template_id = {context.ref(owner)}",
                    f"block_device_name = {_quoted('/dev/sd' + device)}",
                    'lookup_strategy = "asg-instance-block-device-volume-id"',
                ]
            )
        elif provider == "azure":
            disk_lun = 10 + int(attributes.get("attachmentIndex") or 0)
            details.append(
                'vmss_retained_disk_tag_key = "EasyDepRetainedReplicaDisks"'
            )
            details.extend(
                [
                    f"vmss_data_disk_lun = {disk_lun}",
                    f'data_disk_profile_name = format("%s-{_cloud_label(storage_ref)}", var.resource_prefix)',
                    'lookup_strategy = "vmss-instance-lun-managed-disk-id"',
                ]
            )
        else:
            details.extend(
                [
                    f"mig_resource_id = {context.ref(gcp_mig_owner)}",
                    f"device_name = {_quoted('easydep-' + _cloud_label(storage_ref))}",
                    f"mig_region = {_quoted(plan.get('region'))}",
                    'lookup_strategy = "mig-instance-device-source"',
                ]
            )
        outputs.extend(
            [
                f'output "{output_name}" {{',
                "  value = { " + ", ".join(details) + " }",
                "}",
                "",
            ]
        )
    for compute_id in sorted(
        {str(item.get("computeUnitRef") or "") for item in plan.get("placements") or []}
    ):
        if compute_id in context.addresses:
            outputs.extend(
                [
                    f'output "resource_id_{_label(compute_id)}" {{',
                    f"  value = {context.ref(compute_id)}",
                    "}",
                    "",
                ]
            )
    return "\n".join(outputs)


def rendered_resource_types(files: dict[str, str]) -> list[str]:
    pattern = re.compile(r'^resource\s+"([^"]+)"\s+"[^"]+"\s*\{', re.MULTILINE)
    return sorted(
        match.group(1)
        for name, content in files.items()
        if name.endswith(".tf")
        for match in pattern.finditer(content)
    )


def render_open_tofu(resource_plan: dict[str, Any]) -> dict[str, str]:
    """Render one complete, provider-selected OpenTofu module."""

    if resource_plan.get("schemaVersion") != RESOURCE_PLAN_SCHEMA:
        raise ValueError("Deterministic rendering requires ResourcePlan")
    if resource_plan.get("unresolved"):
        raise ValueError("ResourcePlan has unresolved structural decisions")
    provider = str(resource_plan.get("provider") or "")
    if provider not in PINNED_PROVIDERS:
        raise ValueError(f"Unsupported provider: {provider}")
    context = _Context(resource_plan)
    bootstrap_files, vars_by_compute = _runtime_files(resource_plan, context)
    cloud_init_files = _cloud_init_files(bootstrap_files)
    outputs = _output_file(resource_plan, context)
    renderer = {
        "aws": _aws_resources,
        "azure": _azure_resources,
        "gcp": _gcp_resources,
    }[provider]
    locals_content = _locals_file(resource_plan)
    files = {
        "easydep-provider.tf": _provider_file(provider, str(resource_plan.get("region") or "")),
        "variables.tf": _variable_file(resource_plan),
        "main.tf": renderer(resource_plan, context, vars_by_compute),
        "outputs.tf": outputs,
        **bootstrap_files,
        **cloud_init_files,
        **({"easydep-locals.tf": locals_content} if locals_content else {}),
    }
    expected_types = sorted(
        item
        for node in resource_plan.get("nodes") or []
        if node.get("handling") == "create"
        for item in node.get("terraformTypes") or []
    )
    actual_types = rendered_resource_types(files)
    if actual_types != expected_types:
        raise ValueError(
            f"Rendered Terraform diverges from ResourcePlan: expected={expected_types}, actual={actual_types}"
        )
    unconsumed = sorted(
        str(item.get("id") or "")
        for item in resource_plan.get("references") or []
        if str(item.get("id") or "") not in context.consumed_reference_ids
    )
    if unconsumed:
        raise ValueError(f"ResourcePlan references were not rendered: {unconsumed}")
    return {name: content.rstrip() + "\n" for name, content in files.items()}


__all__ = ["render_open_tofu", "rendered_resource_types"]
