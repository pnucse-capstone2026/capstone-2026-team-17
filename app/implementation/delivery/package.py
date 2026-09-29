"""선택된 ResourcePlan에서 사람이 실행할 deployment package를 만든다."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ..config import DEFAULT_CONTAINER_PORT
from ..domain.implementation_ir import remove_readonly


def _label(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", str(value or "workload")).upper()


def _tofu_label(value: Any) -> str:
    """renderer와 같은 규칙으로 OpenTofu 식별자를 만든다.

    Compose 환경 변수는 대문자를 쓰지만 OpenTofu 변수와 output 이름은 원래 대소문자를
    유지한다. 두 규칙을 섞으면 ``TF_VAR_image_digest_web`` 대신 존재하지 않는 대문자
    변수를 내보내므로 별도 함수로 분리한다.
    """

    text = re.sub(r"[^A-Za-z0-9_]", "_", str(value or "resource"))
    if text[:1].isdigit():
        text = f"r_{text}"
    return text or "resource"


def _write_text(path: Path, content: str) -> None:
    """Windows에서 생성해도 Linux가 실행할 수 있도록 LF 줄바꿈으로 저장한다."""
    path.write_text(content, encoding="utf-8", newline="\n")


def _compose(resource_plan: dict[str, Any]) -> tuple[str, list[tuple[str, str]]]:
    """runtimeUnits를 Compose와 비밀값 없는 env 변수 목록으로 옮긴다."""

    lines = ["services:"]
    envs: list[tuple[str, str]] = []
    networks: set[str] = set()
    for unit in resource_plan.get("runtimeUnits") or []:
        network = str(unit.get("containerNetwork") or "easydep")
        networks.add(network)
        for container in unit.get("containers") or []:
            workload_id = str(container.get("workloadRef") or "workload")
            service = re.sub(r"[^a-z0-9-]", "-", workload_id.lower()).strip("-") or "workload"
            image_env = f"{_label(workload_id)}_IMAGE"
            raw_image = container.get("image")
            image = raw_image if isinstance(raw_image, str) else ""
            if (container.get("artifact") or {}).get("kind") == "generatedApplication" or not image:
                image = "${" + image_env + "}"
                envs.append((image_env, f"Immutable container image for {workload_id}."))
            lines.extend([f"  {service}:", f"    image: {image}", "    restart: unless-stopped"])
            ports: list[str] = []
            for interface in container.get("interfaces") or []:
                port = interface.get("port")
                if isinstance(port, int) and interface.get("exposure") in {"public", "internal"}:
                    ports.append(f'      - "{port}:{port}"')
            if ports:
                lines.append("    ports:")
                lines.extend(ports)
            environment: list[str] = []
            for binding in container.get("runtimeBindings") or []:
                name = str(binding.get("environmentName") or "")
                if name:
                    environment.append(f"      - {name}")
            for configuration in container.get("configuration") or []:
                name = str(configuration.get("name") or "")
                if name:
                    environment.append(f"      - {name}")
                    kind = str(configuration.get("kind") or "")
                    # resource binding과 Secret은 cloud-init이 공급한다. 이를 로컬
                    # ``.env.example``에 다시 노출하면 사용자가 필수 입력으로 오해하거나
                    # 비밀값을 평문 파일에 넣을 수 있으므로, 일반 설정만 예시에 싣는다.
                    if not configuration.get("sensitive") and kind not in {
                        "secret",
                        "secretBinding",
                    }:
                        envs.append((name, f"Optional runtime setting for {workload_id}."))
            if environment:
                lines.append("    environment:")
                lines.extend(dict.fromkeys(environment))
            volumes = [
                f'      - "/mnt/easydep/{mount.get("storageRef") or "data"!s}'
                f'/data:{mount.get("mountPath")!s}"'
                for mount in container.get("mounts") or []
                if isinstance(mount.get("mountPath"), str)
            ]
            if volumes:
                lines.append("    volumes:")
                lines.extend(volumes)
            lines.extend(["    networks:", f"      - {network}"])
    if networks:
        lines.append("networks:")
        for network in sorted(networks):
            lines.extend([f"  {network}:", f'    name: "{network}"', "    external: true"])
    return "\n".join(lines) + "\n", list(dict.fromkeys(envs))


def _tfvars_example(resource_plan: dict[str, Any]) -> str:
    lines = [
        "# Copy this file to terraform.tfvars and fill deployment inputs locally.",
        "# Use a unique prefix for disposable deployments so concurrent runs do not collide.",
        'resource_prefix = "easydep"',
    ]
    provider = str(resource_plan.get("provider") or "")
    if provider == "aws":
        lines.extend(['boot_image_id = "ami-REPLACE_ME"', 'ssh_public_key = ""'])
    elif provider == "azure":
        lines.extend(['subscription_id = ""', 'ssh_public_key = ""'])
    elif provider == "gcp":
        lines.append('project_id = ""')
    for workload in resource_plan.get("workloads") or []:
        workload_label = _tofu_label(workload.get("id"))
        if (workload.get("artifact") or {}).get("kind") == "generatedApplication":
            lines.append(
                f"# image_digest_{workload_label} is written to "
                "runtime/image-digests.env by easydep.ps1."
            )
        for interface in workload.get("interfaces") or []:
            if isinstance(interface.get("port"), int):
                continue
            interface_label = _tofu_label(interface.get("id"))
            lines.append(
                f"container_port_{workload_label}_{interface_label} = "
                f"{DEFAULT_CONTAINER_PORT} # Replace when the app uses another port."
            )
    for slot in resource_plan.get("bindingSlots") or []:
        if slot.get("kind") in {"secretReference", "externalEndpoint"}:
            lines.append(f'{_tofu_label(slot.get("id"))} = ""')
    return "\n".join(lines) + "\n"


def _registry_bootstrap_targets(
    resource_plan: dict[str, Any],
) -> list[tuple[str, str, str]]:
    """생성 앱별 ``(workload, resource address, registry output)``을 돌려준다."""

    nodes = {
        str(node.get("id") or ""): node for node in resource_plan.get("nodes") or []
    }
    targets: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for unit in resource_plan.get("runtimeUnits") or []:
        for container in unit.get("containers") or []:
            registry_ref = str(container.get("registryRef") or "")
            workload_ref = str(container.get("workloadRef") or "")
            if not registry_ref or not workload_ref:
                continue
            # Compose environment variables are uppercase, but the renderer keeps
            # the workload label's case in OpenTofu variable and output names.
            workload_label = _tofu_label(workload_ref)
            node = nodes.get(registry_ref) or {}
            terraform_types = list(node.get("terraformTypes") or [])
            if node.get("handling") != "create" or not terraform_types:
                raise ValueError(
                    f"Generated workload {workload_label} has no creatable registry: {registry_ref}"
                )
            address = f"{terraform_types[0]}.{_tofu_label(registry_ref)}"
            key = (workload_label, address)
            if key not in seen:
                targets.append(
                    (workload_label, address, f"registry_{workload_label}_url")
                )
                seen.add(key)
    return targets


def _health_outputs(rendered_tofu: dict[str, str]) -> list[str]:
    """실제 outputs.tf에 존재하는 공개 health URL 이름만 고른다."""

    return re.findall(
        r'^output\s+"(health_url_[A-Za-z0-9_]+)"\s*\{',
        rendered_tofu.get("outputs.tf", ""),
        re.MULTILINE,
    )


def _retained_disk_outputs(rendered_tofu: dict[str, str]) -> list[str]:
    """Return renderer-provided retained replica disk output names."""
    return re.findall(
        r'^output\s+"(retained_replica_disk_[A-Za-z0-9_]+)"\s*\{',
        rendered_tofu.get("outputs.tf", ""),
        re.MULTILINE,
    )


def _interactive_powershell_script(
    resource_plan: dict[str, Any], rendered_tofu: dict[str, str]
) -> str:
    """인자 없이 실행하는 단일 대화형 배포 스크립트를 만든다.

    기존에는 내부 단계를 각각 별도 파일로 노출했다. 사용자는 어느 파일부터 실행하고
    어디서 재개할지 직접 판단해야 했으므로, 여기서는 같은 단계를 함수로 감추고
    ``배포/재개``와 ``삭제``만 선택하게 한다.
    """

    config = {
        "provider": str(resource_plan.get("provider") or ""),
        "region": str(resource_plan.get("region") or ""),
        "registryTargets": [
            {"workload": workload, "address": address, "output": output}
            for workload, address, output in _registry_bootstrap_targets(resource_plan)
        ],
        "healthOutputs": _health_outputs(rendered_tofu),
        "retainedResources": [
            (
                f"{next(iter(node.get('terraformTypes') or []), '')}."
                f"{_tofu_label(node.get('id'))}"
            )
            for node in resource_plan.get("nodes") or []
            if isinstance(node, dict)
            and (node.get("attributes") or {}).get("deletionPolicy") == "retain"
            and next(iter(node.get("terraformTypes") or []), "")
        ],
        "retainedDiskOutputs": _retained_disk_outputs(rendered_tofu),
    }
    config_json = json.dumps(config, ensure_ascii=True, separators=(",", ":"))
    script = r'''$ErrorActionPreference = 'Stop'
$Config = ConvertFrom-Json @'
__CONFIG__
'@
$Root = $PSScriptRoot
$TofuRoot = Join-Path $Root 'tofu'
$RuntimeRoot = Join-Path $Root 'runtime'
$TfvarsPath = Join-Path $TofuRoot 'terraform.tfvars'
$DigestPath = Join-Path $RuntimeRoot 'image-digests.env'
$DigestMetaPath = Join-Path $RuntimeRoot 'image-digests.meta.json'
$IdentityPath = Join-Path $RuntimeRoot 'cloud-identity.json'
$CreatedSecretsPath = Join-Path $RuntimeRoot 'created-secret-resources.json'
$PlanPath = Join-Path $TofuRoot 'easydep.tfplan'

function Invoke-Checked([string]$Program, [string[]]$Arguments) {
  & $Program @Arguments
  if ($LASTEXITCODE -ne 0) { throw "$Program failed with exit code $LASTEXITCODE." }
}

function Invoke-NativeCapture([scriptblock]$Action) {
  # Windows PowerShell turns native stderr redirected with 2>&1 into ErrorRecord
  # objects. Under the script-wide Stop preference, expected not-found responses
  # would terminate before the provider-specific branch can inspect them.
  $previousPreference = $ErrorActionPreference
  try {
    $ErrorActionPreference = 'Continue'
    $output = (& $Action 2>&1 | Out-String)
    $exitCode = $LASTEXITCODE
    return [PSCustomObject]@{ output = $output; exitCode = $exitCode }
  } finally { $ErrorActionPreference = $previousPreference }
}

$DeploymentActivity = 'EasyDep deployment'

function Show-DeploymentProgress([int]$Percent, [string]$Status) {
  $bounded = [Math]::Max(0, [Math]::Min(100, $Percent))
  Write-Progress -Activity $DeploymentActivity -Status $Status -PercentComplete $bounded
  Write-Host ("[{0}%] {1}" -f $bounded, $Status) -ForegroundColor Cyan
}

function Complete-DeploymentProgress {
  Write-Progress -Activity $DeploymentActivity -Completed
}

function Initialize-ProviderCache {
  if ($env:TF_PLUGIN_CACHE_DIR) {
    $cacheRoot = $env:TF_PLUGIN_CACHE_DIR
  } else {
    $localData = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
    if (-not $localData) { $localData = $env:LOCALAPPDATA }
    if (-not $localData) { throw 'Cannot determine a persistent OpenTofu provider cache directory.' }
    $cacheRoot = Join-Path $localData 'EasyDep\opentofu-plugin-cache'
    $env:TF_PLUGIN_CACHE_DIR = $cacheRoot
  }
  New-Item -ItemType Directory -Force -Path $cacheRoot | Out-Null
  return (Resolve-Path -LiteralPath $cacheRoot).Path
}

function Invoke-TofuInitWithHeartbeat {
  $startedAt = Get-Date
  $lastLogSecond = 0
  $startInfo = New-Object Diagnostics.ProcessStartInfo
  $startInfo.FileName = (Get-Command tofu -ErrorAction Stop).Source
  $startInfo.Arguments = 'init -input=false'
  $startInfo.WorkingDirectory = $TofuRoot
  $startInfo.UseShellExecute = $false
  $startInfo.CreateNoWindow = $true
  $process = New-Object Diagnostics.Process
  $process.StartInfo = $startInfo
  if (-not $process.Start()) { throw 'Unable to start OpenTofu initialization.' }
  try {
    while (-not $process.HasExited) {
      Start-Sleep -Seconds 1
      $process.Refresh()
      $elapsed = [int]((Get-Date) - $startedAt).TotalSeconds
      $minutes = [int][Math]::Floor($elapsed / 60)
      $seconds = $elapsed % 60
      $elapsedText = '{0:D2}:{1:D2}' -f $minutes, $seconds
      $spinner = @('|', '/', '-', '\')[$elapsed % 4]
      Write-Progress -Activity $DeploymentActivity `
        -Status "[15%] OpenTofu initialization is running $spinner  elapsed $elapsedText" `
        -PercentComplete 15
      if (($elapsed - $lastLogSecond) -ge 15) {
        Write-Host "[15%] OpenTofu initialization is still running (elapsed $elapsedText)." -ForegroundColor DarkGray
        $lastLogSecond = $elapsed
      }
    }
    $process.WaitForExit()
    $exitCode = $process.ExitCode
  } finally {
    $process.Dispose()
  }
  if ($exitCode -ne 0) { throw "tofu failed with exit code $exitCode." }
}

function New-AwsDockerConfig([string]$Region, [string]$RegistryHost) {
  # 대화형 Windows PowerShell에서는 긴 ECR token을 `docker login`의 표준입력으로
  # 넘기는 과정이 불안정할 수 있다. Docker가 기본으로 이해하는 임시 config.json을
  # 만들면 shell pipeline에 의존하지 않고 같은 인증 정보를 사용할 수 있다.
  if ($Region -notmatch '^[a-z0-9-]+$') { throw 'Invalid AWS region.' }
  if ($RegistryHost -notmatch '^[a-z0-9.-]+$') { throw 'Invalid AWS registry host.' }
  $password = (& aws ecr get-login-password --region $Region | Out-String).Trim()
  if ($LASTEXITCODE -ne 0 -or -not $password) { throw 'AWS registry login token failed.' }
  $auth = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("AWS:$password"))
  $auths = @{}
  $auths[$RegistryHost] = @{ auth = $auth }
  $json = @{ auths = $auths } | ConvertTo-Json -Depth 4 -Compress
  $configRoot = Join-Path ([IO.Path]::GetTempPath()) ('easydep-docker-' + [Guid]::NewGuid().ToString('N'))
  New-Item -ItemType Directory -Path $configRoot | Out-Null
  $utf8 = New-Object Text.UTF8Encoding($false)
  [IO.File]::WriteAllText((Join-Path $configRoot 'config.json'), $json, $utf8)
  return $configRoot
}

function Read-Required([string]$Prompt, [string]$Default = '') {
  while ($true) {
    $suffix = if ($Default) { " [$Default]" } else { '' }
    $value = Read-Host "$Prompt$suffix"
    if (-not $value) { $value = $Default }
    if ($value) { return $value }
    Write-Host 'A value is required.' -ForegroundColor Yellow
  }
}

function Set-TfValue([string]$Name, [string]$Value) {
  $escaped = $Value.Replace('\', '\\').Replace('"', '\"')
  if ($null -eq $script:TfvarsContent) {
    $script:TfvarsContent = Get-Content -Raw -Encoding UTF8 $TfvarsPath
  }
  $content = $script:TfvarsContent
  $pattern = '(?m)^' + [regex]::Escape($Name) + '\s*=.*$'
  $replacement = $Name + ' = "' + $escaped + '"'
  if ([regex]::IsMatch($content, $pattern)) {
    $content = [regex]::Replace($content, $pattern, $replacement)
  } else {
    $content = $content.TrimEnd() + [Environment]::NewLine + $replacement + [Environment]::NewLine
  }
  $script:TfvarsContent = $content
}

function Write-TfvarsAtomic {
  $directory = Split-Path -Parent $TfvarsPath
  $temporary = Join-Path $directory ('.terraform.tfvars.' + [Guid]::NewGuid().ToString('N') + '.tmp')
  try {
    $utf8 = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($temporary, $script:TfvarsContent.TrimEnd() + [Environment]::NewLine, $utf8)
    Move-Item -LiteralPath $temporary -Destination $TfvarsPath -Force
  } finally {
    if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue }
  }
}

function Get-TfValue([string]$Name) {
  if ($null -eq $script:TfvarsContent) { return '' }
  $match = [regex]::Match($script:TfvarsContent, '(?m)^' + [regex]::Escape($Name) + '\s*=\s*(.*?)(?:\s+#.*)?$')
  if (-not $match.Success) { return '' }
  $value = $match.Groups[1].Value.Trim()
  if ($value.Length -ge 2 -and $value.StartsWith('"') -and $value.EndsWith('"')) {
    return $value.Substring(1, $value.Length - 2).Replace('\\"', '"').Replace('\\\\', '\')
  }
  return $value
}

function Test-RequiredTfValue([string]$Name, [string]$Value) {
  if ([string]::IsNullOrWhiteSpace($Value) -or $Value -match '(?i)REPLACE_ME|CHANGE_ME|YOUR_') { return $false }
  if ($Name -eq 'resource_prefix') { return $Value -match '^[A-Za-z][A-Za-z0-9-]{0,31}$' }
  if ($Name -eq 'boot_image_id') { return $Value -match '^ami-[A-Za-z0-9]+$' }
  if ($Name -like 'secret_reference_*') {
    switch ($Config.provider) {
      'aws' { return $Value -match '^arn:aws[a-z-]*:secretsmanager:[a-z0-9-]+:\d{12}:secret:[A-Za-z0-9/_+=.@-]+$' }
      'azure' { return $Value -match '(?i)^/subscriptions/[0-9a-f-]{36}/resourceGroups/[^/]+/providers/Microsoft\.KeyVault/vaults/[^/]+/secrets/[^/]+$' }
      'gcp' { return $Value -match '^projects/[A-Za-z0-9-]+/secrets/[A-Za-z0-9_-]+$' }
    }
  }
  return $true
}

function Test-CloudLogin {
  switch ($Config.provider) {
    'aws' {
      $identityJson = & aws sts get-caller-identity --region $Config.region --output json
      if ($LASTEXITCODE -ne 0) {
        throw 'AWS authentication failed. Run aws configure or aws sso login, then run this script again.'
      }
      $identity = $identityJson | ConvertFrom-Json
      if ($identity.Arn -like '*:root') {
        throw 'Refusing to deploy with AWS root credentials. Configure a short-lived IAM or SSO identity.'
      }
    }
    'azure' {
      & az account show --output none
      if ($LASTEXITCODE -ne 0) {
        throw 'Azure authentication failed. Run az login and select a subscription, then run this script again.'
      }
    }
    'gcp' {
      # Terraform은 명시적으로 전달한 access token, 일반 gcloud 로그인, ADC를 모두
      # 사용할 수 있다. 사전 점검도 같은 선택지를 허용해야 정상 로그인을 막지 않는다.
      if (-not $env:GOOGLE_OAUTH_ACCESS_TOKEN) {
        $userToken = (& gcloud auth print-access-token --quiet 2>$null | Out-String).Trim()
        $userTokenExit = $LASTEXITCODE
        if ($userTokenExit -eq 0 -and $userToken) {
          $env:GOOGLE_OAUTH_ACCESS_TOKEN = $userToken
        } else {
          & gcloud auth application-default print-access-token --quiet | Out-Null
          if ($LASTEXITCODE -ne 0) {
            throw 'GCP credentials are unavailable. Run gcloud auth login or gcloud auth application-default login, then run this script again.'
          }
        }
      }
    }
    default { throw "Unsupported cloud provider: $($Config.provider)" }
  }
}

function Test-Prerequisites([bool]$NeedsDocker) {
  Get-Command tofu -ErrorAction Stop | Out-Null
  $cloudCommand = @{ aws = 'aws'; azure = 'az'; gcp = 'gcloud' }[$Config.provider]
  Get-Command $cloudCommand -ErrorAction Stop | Out-Null
  if ($NeedsDocker) {
    Get-Command docker -ErrorAction Stop | Out-Null
    & docker info | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Docker is installed but its daemon is not running.' }
  }
  Test-CloudLogin
}

function Initialize-Inputs {
  $examplePath = Join-Path $TofuRoot 'terraform.tfvars.example'
  $example = Get-Content -Raw -Encoding UTF8 $examplePath
  $script:TfvarsContent = if (Test-Path $TfvarsPath) { Get-Content -Raw -Encoding UTF8 $TfvarsPath } else { $example }
  # Merge newly required keys from the example without discarding a user's existing values.
  foreach ($exampleMatch in [regex]::Matches($example, '(?m)^([A-Za-z_][A-Za-z0-9_]*)\s*=.*$')) {
    $name = $exampleMatch.Groups[1].Value
    if (-not [regex]::IsMatch($script:TfvarsContent, '(?m)^' + [regex]::Escape($name) + '\s*=')) {
      $script:TfvarsContent = $script:TfvarsContent.TrimEnd() + [Environment]::NewLine + $exampleMatch.Value + [Environment]::NewLine
    }
  }
  Write-Host 'Enter the deployment values. They remain only in this extracted folder.' -ForegroundColor Cyan
  $resourcePrefix = Get-TfValue 'resource_prefix'
  if (-not (Test-RequiredTfValue 'resource_prefix' $resourcePrefix)) {
    Set-TfValue 'resource_prefix' (Read-Required 'Unique resource prefix' 'easydep')
  }

  switch ($Config.provider) {
    'aws' {
      if (-not (Test-RequiredTfValue 'boot_image_id' (Get-TfValue 'boot_image_id'))) {
        $ami = ''
        try { $ami = (& aws ssm get-parameter --region $Config.region --name '/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64' --query 'Parameter.Value' --output text 2>$null).Trim() } catch { $ami = '' }
        if (-not $ami.StartsWith('ami-')) { $ami = '' }
        Set-TfValue 'boot_image_id' (Read-Required "x86_64 Linux AMI in $($Config.region)" $ami)
      }
    }
    'azure' {
      if (-not (Test-RequiredTfValue 'subscription_id' (Get-TfValue 'subscription_id'))) {
        $subscription = (& az account show --query id --output tsv).Trim()
        Set-TfValue 'subscription_id' (Read-Required 'Azure subscription ID' $subscription)
      }
      if (-not (Test-RequiredTfValue 'ssh_public_key' (Get-TfValue 'ssh_public_key'))) {
        $keyPath = Read-Required 'Path to an OpenSSH public key'
        Set-TfValue 'ssh_public_key' (Get-Content -Raw -Encoding UTF8 $keyPath).Trim()
      }
    }
    'gcp' {
      if (-not (Test-RequiredTfValue 'project_id' (Get-TfValue 'project_id'))) {
        $project = (& gcloud config get-value project 2>$null).Trim()
        Set-TfValue 'project_id' (Read-Required 'GCP project ID' $project)
      }
    }
  }

  # Prompt only missing or invalid values from both an old partial file and new keys.
  $requiredNames = [regex]::Matches($script:TfvarsContent, '(?m)^([A-Za-z_][A-Za-z0-9_]*)\s*=') | ForEach-Object { $_.Groups[1].Value } | Select-Object -Unique
  foreach ($name in $requiredNames) {
    if ($Config.provider -eq 'aws' -and $name -eq 'ssh_public_key') { continue }
    # Secret references have a separate flow that can either accept an existing
    # reference or create a provider-managed Secret without exposing its value.
    if ($name -like 'secret_reference_*') { continue }
    $value = Get-TfValue $name
    if (-not (Test-RequiredTfValue $name $value)) {
      Set-TfValue $name (Read-Required "Value for $name")
    }
  }
  Write-TfvarsAtomic
}

function Get-CloudIdentity {
  switch ($Config.provider) {
    'aws' {
      $value = (& aws sts get-caller-identity --query Account --output text --region $Config.region 2>$null).Trim()
      if ($LASTEXITCODE -ne 0 -or $value -notmatch '^\d{12}$') { throw 'Unable to resolve the AWS caller identity.' }
      return $value
    }
    'azure' {
      $value = (& az account show --query id --output tsv 2>$null).Trim()
      if ($LASTEXITCODE -ne 0 -or $value -notmatch '^[0-9a-f-]{36}$') { throw 'Unable to resolve the Azure subscription identity.' }
      return $value.ToLowerInvariant()
    }
    'gcp' {
      $project = (Get-TfValue 'project_id').Trim()
      $account = (& gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>$null | Select-Object -First 1).Trim()
      if ($project -notmatch '^[a-z][a-z0-9-]{4,28}[a-z0-9]$' -or -not $account) { throw 'Unable to resolve the GCP caller identity.' }
      return ($project + '|' + $account)
    }
    default { throw "Unsupported cloud provider: $($Config.provider)" }
  }
}

function Get-StableSuffix([string]$Value) {
  $hasher = [Security.Cryptography.SHA256]::Create()
  try {
    $bytes = $hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($Value))
    return ([BitConverter]::ToString($bytes).Replace('-','').Substring(0, 8)).ToLowerInvariant()
  } finally { $hasher.Dispose() }
}

function Get-ManagedSecretName([string]$Slot, [int]$MaximumLength = 63) {
  $prefix = (Get-TfValue 'resource_prefix').ToLowerInvariant() -replace '[^a-z0-9-]', '-'
  $shortSlot = ($Slot -replace '^secret_reference_', '').ToLowerInvariant() -replace '[^a-z0-9-]', '-'
  $base = ($prefix.Trim('-') + '-' + $shortSlot.Trim('-')).Trim('-')
  if (-not $base -or $base[0] -notmatch '[a-z]') { $base = 'easydep-' + $base }
  $suffix = Get-StableSuffix ($Config.provider + '|' + (Get-CloudIdentity) + '|' + $Config.region + '|' + $Slot)
  $baseLimit = $MaximumLength - $suffix.Length - 1
  if ($base.Length -gt $baseLimit) { $base = $base.Substring(0, $baseLimit).Trim('-') }
  return ($base + '-' + $suffix)
}

function Convert-SecureValueToPlainText([Security.SecureString]$Value) {
  $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Value)
  try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) }
  finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
}

function Read-ConfirmedSecretValue([string]$Slot) {
  while ($true) {
    $first = Read-Host "Secret value for $Slot" -AsSecureString
    $second = Read-Host 'Enter the same Secret value again' -AsSecureString
    $firstText = Convert-SecureValueToPlainText $first
    $secondText = Convert-SecureValueToPlainText $second
    try {
      if ([string]::IsNullOrEmpty($firstText)) {
        Write-Host 'The Secret value cannot be empty.' -ForegroundColor Yellow
        continue
      }
      if ($firstText -cne $secondText) {
        Write-Host 'The two Secret values do not match.' -ForegroundColor Yellow
        continue
      }
      return $first
    } finally {
      $firstText = $null
      $secondText = $null
    }
  }
}

function Invoke-WithSecretFile([Security.SecureString]$SecretValue, [scriptblock]$Action) {
  $temporaryRoot = Join-Path ([IO.Path]::GetTempPath()) ('easydep-secret-' + [Guid]::NewGuid().ToString('N'))
  $secretPath = Join-Path $temporaryRoot 'value'
  $plainText = $null
  try {
    New-Item -ItemType Directory -Path $temporaryRoot | Out-Null
    if ([Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT) {
      $currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
      & icacls $temporaryRoot '/inheritance:r' '/grant:r' ($currentIdentity + ':(OI)(CI)(F)') *> $null
      if ($LASTEXITCODE -ne 0) { throw 'Unable to restrict the temporary Secret directory permissions.' }
    } else {
      & chmod 700 $temporaryRoot
      if ($LASTEXITCODE -ne 0) { throw 'Unable to restrict the temporary Secret directory permissions.' }
    }
    $plainText = Convert-SecureValueToPlainText $SecretValue
    $utf8 = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($secretPath, $plainText, $utf8)
    if ([Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT) {
      & icacls $secretPath '/inheritance:r' '/grant:r' ($currentIdentity + ':(R,W)') *> $null
      if ($LASTEXITCODE -ne 0) { throw 'Unable to restrict the temporary Secret file permissions.' }
    } else {
      & chmod 600 $secretPath
      if ($LASTEXITCODE -ne 0) { throw 'Unable to restrict the temporary Secret file permissions.' }
    }
    return (& $Action $secretPath)
  } finally {
    $plainText = $null
    Remove-Item -LiteralPath $secretPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $temporaryRoot -Force -ErrorAction SilentlyContinue
  }
}

function Get-AwsSecretFileArgument([string]$Path) {
  $normalized = [IO.Path]::GetFullPath($Path).Replace('\', '/')
  return 'file://' + $normalized
}

function Get-CreatedSecretState([bool]$CreateIfMissing) {
  $identity = Get-CloudIdentity
  if (-not (Test-Path -LiteralPath $CreatedSecretsPath)) {
    if (-not $CreateIfMissing) { return $null }
    return [PSCustomObject]@{
      schemaVersion = 1
      provider = $Config.provider
      region = $Config.region
      cloudIdentity = $identity
      resources = @()
    }
  }
  try { $state = Get-Content -Raw -Encoding UTF8 -LiteralPath $CreatedSecretsPath | ConvertFrom-Json }
  catch { throw 'The local script-created Secret record is unreadable.' }
  if ([int]$state.schemaVersion -ne 1 -or $state.provider -ne $Config.provider -or $state.region -ne $Config.region -or $state.cloudIdentity -ne $identity) {
    throw 'The script-created Secret record belongs to a different provider, region, or cloud identity.'
  }
  return $state
}

function Write-CreatedSecretState([object]$State) {
  $temporary = $CreatedSecretsPath + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
  try {
    $json = $State | ConvertTo-Json -Depth 8
    $utf8 = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($temporary, $json + [Environment]::NewLine, $utf8)
    Move-Item -LiteralPath $temporary -Destination $CreatedSecretsPath -Force
  } finally { Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue }
}

function Save-CreatedSecretRecord([object]$Record) {
  $state = Get-CreatedSecretState $true
  $resources = @($state.resources | Where-Object { $_.slot -ne $Record.slot })
  $resources += $Record
  $state.resources = $resources
  Write-CreatedSecretState $state
}

function New-AwsManagedSecret([string]$Slot, [Security.SecureString]$SecretValue) {
  $secretName = Get-ManagedSecretName $Slot 120
  $prefix = Get-TfValue 'resource_prefix'
  $existingResult = Invoke-NativeCapture { aws secretsmanager describe-secret --region $Config.region --secret-id $secretName --output json }
  $existingJson = [string]$existingResult.output
  $existingExit = [int]$existingResult.exitCode
  $reference = ''
  if ($existingExit -eq 0 -and $existingJson.Trim()) {
    try { $existing = $existingJson | ConvertFrom-Json } catch { throw "AWS Secret metadata for $secretName is unreadable." }
    $tags = @{}
    foreach ($tag in @($existing.Tags)) { $tags[[string]$tag.Key] = [string]$tag.Value }
    if ($tags.EasyDepManaged -ne 'true' -or $tags.EasyDepSlot -ne $Slot -or $tags.EasyDepPrefix -ne $prefix) {
      throw "AWS Secret name $secretName already exists and is not owned by this deployment. Use an existing reference or choose another resource prefix."
    }
    $reference = [string]$existing.ARN
    Save-CreatedSecretRecord ([PSCustomObject]@{
      slot = $Slot; reference = $reference; secretName = $secretName
      createdSecret = $true; createdVault = $false; createdResourceGroup = $false; ready = $false
    })
    Invoke-WithSecretFile $SecretValue {
      param($secretPath)
      $putResult = Invoke-NativeCapture { aws secretsmanager put-secret-value --region $Config.region --secret-id $reference --secret-string (Get-AwsSecretFileArgument $secretPath) --output json }
      if ($putResult.exitCode -ne 0) { throw "Unable to add a value to AWS Secret $secretName." }
    } | Out-Null
  } else {
    if ($existingJson -notmatch 'ResourceNotFoundException') { throw "Unable to determine whether AWS Secret $secretName exists." }
    Save-CreatedSecretRecord ([PSCustomObject]@{
      slot = $Slot; reference = ''; secretName = $secretName
      createdSecret = $true; createdVault = $false; createdResourceGroup = $false; ready = $false
    })
    $reference = (Invoke-WithSecretFile $SecretValue {
      param($secretPath)
      $createResult = Invoke-NativeCapture { aws secretsmanager create-secret --region $Config.region --name $secretName --description 'Created by the EasyDep deployment script.' --secret-string (Get-AwsSecretFileArgument $secretPath) --tags ('Key=EasyDepManaged,Value=true') ('Key=EasyDepSlot,Value=' + $Slot) ('Key=EasyDepPrefix,Value=' + $prefix) --query ARN --output text }
      $result = ([string]$createResult.output).Trim()
      if ($createResult.exitCode -ne 0 -or -not $result) { throw "Unable to create AWS Secret $secretName." }
      return $result
    })
  }
  if ($reference -notmatch '^arn:') { throw "AWS did not return a Secret ARN for $secretName." }
  return [PSCustomObject]@{
    slot = $Slot; reference = $reference; secretName = $secretName
    createdSecret = $true; createdVault = $false; createdResourceGroup = $false; ready = $true
  }
}

function New-GcpManagedSecret([string]$Slot, [Security.SecureString]$SecretValue) {
  $project = (Get-TfValue 'project_id').Trim()
  $secretName = Get-ManagedSecretName $Slot 120
  $prefix = Get-TfValue 'resource_prefix'
  $labelSlot = (($Slot -replace '^secret_reference_', '').ToLowerInvariant() -replace '[^a-z0-9_-]', '_')
  if ($labelSlot.Length -gt 63) { $labelSlot = $labelSlot.Substring(0, 54).Trim('_-') + '-' + (Get-StableSuffix $Slot) }
  $labelPrefix = ($prefix.ToLowerInvariant() -replace '[^a-z0-9_-]', '_')
  & gcloud services enable secretmanager.googleapis.com --project $project --quiet *> $null
  if ($LASTEXITCODE -ne 0) { throw 'Unable to enable the GCP Secret Manager API.' }
  $existingResult = Invoke-NativeCapture { gcloud secrets describe $secretName --project $project --format=json }
  $existingJson = [string]$existingResult.output
  $existingExit = [int]$existingResult.exitCode
  if ($existingExit -eq 0 -and $existingJson.Trim()) {
    try { $existing = $existingJson | ConvertFrom-Json } catch { throw "GCP Secret metadata for $secretName is unreadable." }
    if ($existing.labels.easydep_managed -ne 'true' -or $existing.labels.easydep_slot -ne $labelSlot -or $existing.labels.easydep_prefix -ne $labelPrefix) {
      throw "GCP Secret name $secretName already exists and is not owned by this deployment. Use an existing reference or choose another resource prefix."
    }
  } else {
    if ($existingJson -notmatch '(?i)NOT_FOUND|not found') { throw "Unable to determine whether GCP Secret $secretName exists." }
    & gcloud secrets create $secretName --project $project --replication-policy=automatic --labels ('easydep_managed=true,easydep_slot=' + $labelSlot + ',easydep_prefix=' + $labelPrefix) --quiet *> $null
    if ($LASTEXITCODE -ne 0) { throw "Unable to create GCP Secret $secretName." }
  }
  $record = [PSCustomObject]@{
    slot = $Slot; reference = ('projects/' + $project + '/secrets/' + $secretName); secretName = $secretName
    createdSecret = $true; createdVault = $false; createdResourceGroup = $false; ready = $false
  }
  Save-CreatedSecretRecord $record
  Invoke-WithSecretFile $SecretValue {
    param($secretPath)
    & gcloud secrets versions add $secretName --project $project --data-file=$secretPath --quiet *> $null
    if ($LASTEXITCODE -ne 0) { throw "Unable to add a value to GCP Secret $secretName." }
  } | Out-Null
  $record.ready = $true
  return $record
}

function Get-AzureCallerPrincipal {
  $accountJson = (& az account show --output json 2>$null | Out-String)
  if ($LASTEXITCODE -ne 0) { throw 'Unable to inspect the active Azure identity.' }
  $account = $accountJson | ConvertFrom-Json
  if ($account.user.type -eq 'servicePrincipal') {
    $objectId = (& az ad sp show --id $account.user.name --query id --output tsv 2>$null | Out-String).Trim()
    $principalType = 'ServicePrincipal'
  } else {
    $objectId = (& az ad signed-in-user show --query id --output tsv 2>$null | Out-String).Trim()
    $principalType = 'User'
  }
  if ($LASTEXITCODE -ne 0 -or $objectId -notmatch '^[0-9a-fA-F-]{36}$') { throw 'Unable to resolve the active Azure principal object ID.' }
  return [PSCustomObject]@{ objectId = $objectId; principalType = $principalType }
}

function New-AzureManagedSecret([string]$Slot, [Security.SecureString]$SecretValue) {
  $subscription = (Get-TfValue 'subscription_id').Trim().ToLowerInvariant()
  $prefix = Get-TfValue 'resource_prefix'
  $secretName = Get-ManagedSecretName $Slot 100
  $vaultName = (Read-Host 'Existing Azure Key Vault name (leave empty to create a dedicated vault)').Trim()
  $createdVault = $false
  $createdResourceGroup = $false
  $resourceGroup = ''
  $roleAssignmentId = ''
  $createdRoleAssignment = $false
  if ($vaultName) {
    if ($vaultName -notmatch '^[A-Za-z][A-Za-z0-9-]{1,22}[A-Za-z0-9]$') { throw 'Invalid Azure Key Vault name.' }
    $vaultJson = (& az keyvault show --name $vaultName --subscription $subscription --output json 2>$null | Out-String)
    if ($LASTEXITCODE -ne 0 -or -not $vaultJson.Trim()) { throw "Azure Key Vault $vaultName was not found." }
    $vault = $vaultJson | ConvertFrom-Json
    if ($vault.properties.enableRbacAuthorization -ne $true) {
      throw "Azure Key Vault $vaultName does not use Azure RBAC. Choose an RBAC-enabled vault or create a dedicated vault."
    }
    $resourceGroup = [string]$vault.resourceGroup
    $vaultId = [string]$vault.id
  } else {
    $resourceGroup = Get-ManagedSecretName 'secret-resource-group' 63
    $groupJson = (& az group show --name $resourceGroup --subscription $subscription --output json 2>$null | Out-String)
    if ($LASTEXITCODE -ne 0) {
      & az group create --name $resourceGroup --location $Config.region --subscription $subscription --tags EasyDepManaged=true ('EasyDepPrefix=' + $prefix) --output none
      if ($LASTEXITCODE -ne 0) { throw "Unable to create Azure resource group $resourceGroup." }
      $createdResourceGroup = $true
    } else {
      $group = $groupJson | ConvertFrom-Json
      if ($group.tags.EasyDepManaged -ne 'true' -or $group.tags.EasyDepPrefix -ne $prefix) {
        throw "Azure resource group $resourceGroup already exists and is not owned by this deployment."
      }
      $createdResourceGroup = $true
    }
    $vaultName = Get-ManagedSecretName 'secret-vault' 24
    $vaultJson = (& az keyvault show --name $vaultName --subscription $subscription --output json 2>$null | Out-String)
    if ($LASTEXITCODE -eq 0 -and $vaultJson.Trim()) {
      $vault = $vaultJson | ConvertFrom-Json
      if ($vault.tags.EasyDepManaged -ne 'true' -or $vault.tags.EasyDepPrefix -ne $prefix -or $vault.resourceGroup -ne $resourceGroup) {
        throw "Azure Key Vault name $vaultName already exists and is not owned by this deployment."
      }
    } else {
      $vaultJson = (& az keyvault create --name $vaultName --resource-group $resourceGroup --location $Config.region --subscription $subscription --enable-rbac-authorization true --tags EasyDepManaged=true ('EasyDepPrefix=' + $prefix) --output json 2>$null | Out-String)
      if ($LASTEXITCODE -ne 0 -or -not $vaultJson.Trim()) { throw "Unable to create Azure Key Vault $vaultName." }
      $vault = $vaultJson | ConvertFrom-Json
    }
    $createdVault = $true
    $vaultId = [string]$vault.id
    $principal = Get-AzureCallerPrincipal
    $roleAssignmentId = (& az role assignment list --assignee-object-id $principal.objectId --role 'Key Vault Secrets Officer' --scope $vaultId --query '[0].id' --output tsv 2>$null | Out-String).Trim()
    if (-not $roleAssignmentId) {
      $roleAssignmentId = (& az role assignment create --assignee-object-id $principal.objectId --assignee-principal-type $principal.principalType --role 'Key Vault Secrets Officer' --scope $vaultId --query id --output tsv 2>$null | Out-String).Trim()
      if ($LASTEXITCODE -ne 0 -or -not $roleAssignmentId) { throw "Unable to grant Secret write access on Azure Key Vault $vaultName." }
      $createdRoleAssignment = $true
    }
  }

  $existingSecretJson = (& az keyvault secret show --vault-name $vaultName --name $secretName --subscription $subscription --query '{id:id,tags:tags}' --output json 2>$null | Out-String)
  if ($LASTEXITCODE -eq 0 -and $existingSecretJson.Trim()) {
    $existingSecret = $existingSecretJson | ConvertFrom-Json
    if ($existingSecret.tags.EasyDepManaged -ne 'true' -or $existingSecret.tags.EasyDepSlot -ne $Slot -or $existingSecret.tags.EasyDepPrefix -ne $prefix) {
      throw "Azure Secret name $secretName already exists and is not owned by this deployment."
    }
  }
  $record = [PSCustomObject]@{
    slot = $Slot; reference = ($vaultId.TrimEnd('/') + '/secrets/' + $secretName); secretName = $secretName
    createdSecret = $true; vaultName = $vaultName; resourceGroup = $resourceGroup
    createdVault = $createdVault; createdResourceGroup = $createdResourceGroup
    roleAssignmentId = $roleAssignmentId; createdRoleAssignment = $createdRoleAssignment
    location = $Config.region; ready = $false
  }
  Save-CreatedSecretRecord $record
  $attempts = if ($createdVault) { 12 } else { 1 }
  $secretSet = $false
  for ($attempt = 1; $attempt -le $attempts; $attempt++) {
    Invoke-WithSecretFile $SecretValue {
      param($secretPath)
      & az keyvault secret set --vault-name $vaultName --name $secretName --file $secretPath --encoding utf-8 --subscription $subscription --tags EasyDepManaged=true ('EasyDepSlot=' + $Slot) ('EasyDepPrefix=' + $prefix) --output none 2>$null
    } | Out-Null
    if ($LASTEXITCODE -eq 0) { $secretSet = $true; break }
    if ($attempt -lt $attempts) { Start-Sleep -Seconds 5 }
  }
  if (-not $secretSet) { throw "Unable to store the value in Azure Key Vault $vaultName. Check the Secrets Officer role and retry." }
  $record.ready = $true
  return $record
}

function Initialize-Secrets {
  $names = @([regex]::Matches($script:TfvarsContent, '(?m)^(secret_reference_[A-Za-z0-9_]+)\s*=') | ForEach-Object { $_.Groups[1].Value } | Select-Object -Unique)
  if ($names.Count -eq 0) { return }
  $state = Get-CreatedSecretState $true
  foreach ($name in $names) {
    $reference = (Get-TfValue $name).Trim()
    if (Test-RequiredTfValue $name $reference) { continue }
    $recorded = @($state.resources | Where-Object { $_.slot -eq $name } | Select-Object -First 1)
    if ($recorded.Count -eq 1 -and $recorded[0].ready -eq $true -and (Test-RequiredTfValue $name ([string]$recorded[0].reference))) {
      Set-TfValue $name ([string]$recorded[0].reference)
      continue
    }
    while ($true) {
      $defaultChoice = if ($recorded.Count -eq 1) { 'C' } else { 'E' }
      $choice = (Read-Host "Secret $name is missing. Use an [E]xisting reference or [C]reate a new managed Secret? [$defaultChoice]").Trim().ToUpperInvariant()
      if (-not $choice) { $choice = $defaultChoice }
      if ($choice -eq 'E') {
        $candidate = Read-Required "Existing $($Config.provider) Secret reference for $name"
        if (Test-RequiredTfValue $name $candidate) { Set-TfValue $name $candidate; break }
        Write-Host "The reference is not a canonical $($Config.provider) Secret resource name." -ForegroundColor Yellow
        continue
      }
      if ($choice -eq 'C') {
        Write-Host 'Creating a cloud Secret can incur provider charges. Only its reference and ownership metadata will be saved locally.' -ForegroundColor Yellow
        $confirm = Read-Host 'Type CREATE to continue'
        if ($confirm -cne 'CREATE') { Write-Host 'Secret creation cancelled.'; continue }
        $secretValue = Read-ConfirmedSecretValue $name
        switch ($Config.provider) {
          'aws' { $record = New-AwsManagedSecret $name $secretValue }
          'azure' { $record = New-AzureManagedSecret $name $secretValue }
          'gcp' { $record = New-GcpManagedSecret $name $secretValue }
          default { throw "Unsupported cloud provider: $($Config.provider)" }
        }
        Save-CreatedSecretRecord $record
        $state = Get-CreatedSecretState $true
        Set-TfValue $name ([string]$record.reference)
        break
      }
      Write-Host 'Enter E or C.' -ForegroundColor Yellow
    }
  }
  Write-TfvarsAtomic
}

function Remove-CreatedSecrets {
  if (-not (Test-Path -LiteralPath $CreatedSecretsPath)) { return }
  $state = Get-CreatedSecretState $false
  $resources = @($state.resources)
  if ($resources.Count -eq 0) {
    Remove-Item -LiteralPath $CreatedSecretsPath -Force
    return
  }
  Write-Host 'Only Secrets recorded as created by this script are eligible for cleanup. Existing references are never deleted.' -ForegroundColor Yellow
  $answer = Read-Host 'Delete script-created Secret resources? Type DELETE-SECRETS to continue'
  if ($answer -cne 'DELETE-SECRETS') { Write-Host 'Secret cleanup skipped.'; return }
  $allSucceeded = $true
  $failedResources = @()
  $removedAzureVaults = @{}
  foreach ($record in $resources) {
    try {
      switch ($Config.provider) {
        'aws' {
          $secretId = if ($record.reference) { [string]$record.reference } else { [string]$record.secretName }
          $metadataResult = Invoke-NativeCapture { aws secretsmanager describe-secret --region $Config.region --secret-id $secretId --output json }
          $metadataJson = [string]$metadataResult.output
          $metadataExit = [int]$metadataResult.exitCode
          if ($metadataExit -eq 0 -and $metadataJson.Trim()) {
            $metadata = $metadataJson | ConvertFrom-Json
            $tags = @{}
            foreach ($tag in @($metadata.Tags)) { $tags[[string]$tag.Key] = [string]$tag.Value }
            if ($tags.EasyDepManaged -ne 'true' -or $tags.EasyDepSlot -ne $record.slot -or $tags.EasyDepPrefix -ne (Get-TfValue 'resource_prefix')) {
              throw 'AWS Secret ownership tags do not match the local record.'
            }
            if (-not $metadata.DeletedDate) {
              $deleteResult = Invoke-NativeCapture { aws secretsmanager delete-secret --region $Config.region --secret-id $secretId --recovery-window-in-days 7 --output json }
              if ($deleteResult.exitCode -ne 0) { throw 'AWS Secret deletion scheduling failed.' }
            }
          } elseif ($metadataJson -notmatch 'ResourceNotFoundException') {
            throw 'AWS Secret metadata lookup failed; the local cleanup record was kept.'
          }
        }
        'gcp' {
          $project = (Get-TfValue 'project_id').Trim()
          $metadataResult = Invoke-NativeCapture { gcloud secrets describe $record.reference --project $project --format=json }
          $metadataJson = [string]$metadataResult.output
          $metadataExit = [int]$metadataResult.exitCode
          if ($metadataExit -eq 0 -and $metadataJson.Trim()) {
            $metadata = $metadataJson | ConvertFrom-Json
            $expectedSlot = (([string]$record.slot -replace '^secret_reference_', '').ToLowerInvariant() -replace '[^a-z0-9_-]', '_')
            if ($expectedSlot.Length -gt 63) { $expectedSlot = $expectedSlot.Substring(0, 54).Trim('_-') + '-' + (Get-StableSuffix ([string]$record.slot)) }
            $expectedPrefix = ((Get-TfValue 'resource_prefix').ToLowerInvariant() -replace '[^a-z0-9_-]', '_')
            if ($metadata.labels.easydep_managed -ne 'true' -or $metadata.labels.easydep_slot -ne $expectedSlot -or $metadata.labels.easydep_prefix -ne $expectedPrefix) { throw 'GCP Secret ownership labels do not match the local record.' }
            & gcloud secrets delete $record.reference --project $project --quiet *> $null
            if ($LASTEXITCODE -ne 0) { throw 'GCP Secret deletion failed.' }
          } elseif ($metadataJson -notmatch '(?i)NOT_FOUND|not found') {
            throw 'GCP Secret metadata lookup failed; the local cleanup record was kept.'
          }
        }
        'azure' {
          $subscription = (Get-TfValue 'subscription_id').Trim()
          if ($record.createdVault -eq $true) {
            if (-not $removedAzureVaults.ContainsKey([string]$record.vaultName)) {
              if ($record.createdRoleAssignment -eq $true -and $record.roleAssignmentId) { & az role assignment delete --ids $record.roleAssignmentId --subscription $subscription *> $null }
              $vaultMetadataJson = (& az keyvault show --name $record.vaultName --subscription $subscription --query '{id:id,tags:tags}' --output json 2>$null | Out-String)
              if ($LASTEXITCODE -eq 0 -and $vaultMetadataJson.Trim()) {
                $vaultMetadata = $vaultMetadataJson | ConvertFrom-Json
                if ($vaultMetadata.tags.EasyDepManaged -ne 'true' -or $vaultMetadata.tags.EasyDepPrefix -ne (Get-TfValue 'resource_prefix')) { throw 'Azure Key Vault ownership tags do not match the local record.' }
                & az keyvault delete --name $record.vaultName --resource-group $record.resourceGroup --subscription $subscription --output none
                if ($LASTEXITCODE -ne 0) { throw 'Azure Key Vault deletion failed.' }
              }
              $deletedVaultJson = (& az keyvault show-deleted --name $record.vaultName --location $record.location --subscription $subscription --query '{id:id,tags:tags}' --output json 2>$null | Out-String)
              if ($LASTEXITCODE -eq 0 -and $deletedVaultJson.Trim()) {
                $deletedVault = $deletedVaultJson | ConvertFrom-Json
                if ($deletedVault.tags.EasyDepManaged -ne 'true' -or $deletedVault.tags.EasyDepPrefix -ne (Get-TfValue 'resource_prefix')) { throw 'Deleted Azure Key Vault ownership tags do not match the local record.' }
                & az keyvault purge --name $record.vaultName --location $record.location --subscription $subscription --no-wait *> $null
                if ($LASTEXITCODE -ne 0) { throw 'Azure Key Vault purge failed.' }
              }
              if ($record.createdResourceGroup -eq $true) {
                $groupJson = (& az group show --name $record.resourceGroup --subscription $subscription --query '{id:id,tags:tags}' --output json 2>$null | Out-String)
                if ($LASTEXITCODE -eq 0 -and $groupJson.Trim()) {
                  $group = $groupJson | ConvertFrom-Json
                  if ($group.tags.EasyDepManaged -ne 'true' -or $group.tags.EasyDepPrefix -ne (Get-TfValue 'resource_prefix')) { throw 'Azure Secret resource group ownership tags do not match the local record.' }
                  & az group delete --name $record.resourceGroup --subscription $subscription --yes --no-wait *> $null
                  if ($LASTEXITCODE -ne 0) { throw 'Azure Secret resource group deletion failed.' }
                }
              }
              $removedAzureVaults[[string]$record.vaultName] = $true
            }
          } else {
            $secretMetadataJson = (& az keyvault secret show --vault-name $record.vaultName --name $record.secretName --subscription $subscription --query '{id:id,tags:tags}' --output json 2>$null | Out-String)
            if ($LASTEXITCODE -eq 0 -and $secretMetadataJson.Trim()) {
              $secretMetadata = $secretMetadataJson | ConvertFrom-Json
              if ($secretMetadata.tags.EasyDepManaged -ne 'true' -or $secretMetadata.tags.EasyDepSlot -ne $record.slot -or $secretMetadata.tags.EasyDepPrefix -ne (Get-TfValue 'resource_prefix')) { throw 'Azure Secret ownership tags do not match the local record.' }
              & az keyvault secret delete --vault-name $record.vaultName --name $record.secretName --subscription $subscription --output none
              if ($LASTEXITCODE -ne 0) { throw 'Azure Secret deletion failed.' }
            }
            $deletedSecretJson = (& az keyvault secret show-deleted --vault-name $record.vaultName --name $record.secretName --subscription $subscription --query '{recoveryId:recoveryId,tags:tags}' --output json 2>$null | Out-String)
            if ($LASTEXITCODE -eq 0 -and $deletedSecretJson.Trim()) {
              $deletedSecret = $deletedSecretJson | ConvertFrom-Json
              if ($deletedSecret.tags.EasyDepManaged -ne 'true' -or $deletedSecret.tags.EasyDepSlot -ne $record.slot -or $deletedSecret.tags.EasyDepPrefix -ne (Get-TfValue 'resource_prefix')) { throw 'Deleted Azure Secret ownership tags do not match the local record.' }
              & az keyvault secret purge --vault-name $record.vaultName --name $record.secretName --subscription $subscription *> $null
              if ($LASTEXITCODE -ne 0) { throw 'Azure Secret purge failed.' }
            }
          }
        }
      }
      Write-Host "Cleaned up the script-created Secret for $($record.slot)." -ForegroundColor Green
    } catch {
      $allSucceeded = $false
      $failedResources += $record
      Write-Host "Secret cleanup failed for $($record.slot): $($_.Exception.Message)" -ForegroundColor Red
    }
  }
  if ($allSucceeded) {
    Remove-Item -LiteralPath $CreatedSecretsPath -Force
    Write-Host 'The local script-created Secret record was removed.' -ForegroundColor Green
  } else {
    $state.resources = $failedResources
    Write-CreatedSecretState $state
    Write-Host 'The local Secret record was kept so cleanup can be retried.' -ForegroundColor Yellow
  }
}

function Get-SourceHash([string]$ApplicationRoot) {
  $deploymentPrefix = [IO.Path]::GetFullPath($Root) + [IO.Path]::DirectorySeparatorChar
  $files = @(Get-ChildItem -LiteralPath $ApplicationRoot -File -Recurse | Where-Object {
      -not ($_.FullName.StartsWith($deploymentPrefix, [StringComparison]::OrdinalIgnoreCase)) -and
      $_.Name -notin @('terraform.tfstate','terraform.tfstate.backup')
    } | Sort-Object FullName)
  $signature = ($files | ForEach-Object {
      $relative = $_.FullName.Substring($ApplicationRoot.Length).TrimStart('\','/')
      $digest = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
      "$relative=$digest"
    }) -join "`n"
  $hasher = [Security.Cryptography.SHA256]::Create()
  try { return ([BitConverter]::ToString($hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($signature))).Replace('-','')).ToLowerInvariant() }
  finally { $hasher.Dispose() }
}

function Test-DeploymentIdentity([bool]$RecordIfMissing) {
  $identity = Get-CloudIdentity
  if (Test-Path -LiteralPath $IdentityPath) {
    try { $record = Get-Content -Raw -Encoding UTF8 -LiteralPath $IdentityPath | ConvertFrom-Json } catch { throw 'The deployment identity checkpoint is unreadable.' }
    if ($record.provider -ne $Config.provider -or $record.region -ne $Config.region -or $record.cloudIdentity -ne $identity) {
      throw 'The active cloud identity does not match this deployment state; refusing to mix accounts.'
    }
    return
  }
  if (-not $RecordIfMissing) { throw 'No cloud identity checkpoint exists for this deployment state.' }
  $temporary = $IdentityPath + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
  try {
    @{ provider = $Config.provider; region = $Config.region; cloudIdentity = $identity } | ConvertTo-Json -Compress | Set-Content -Encoding UTF8 -LiteralPath $temporary
    Move-Item -LiteralPath $temporary -Destination $IdentityPath -Force
  } finally { Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue }
}

function Test-SecretReferences {
  $names = @([regex]::Matches($script:TfvarsContent, '(?m)^(secret_reference_[A-Za-z0-9_]+)\s*=') | ForEach-Object { $_.Groups[1].Value })
  foreach ($name in $names) {
    $reference = (Get-TfValue $name).Trim()
    if (-not (Test-RequiredTfValue $name $reference)) { throw "Invalid $($Config.provider) Secret reference format for $name." }
    switch ($Config.provider) {
      'aws' {
        & aws secretsmanager describe-secret --region $Config.region --secret-id $reference --query ARN --output text *> $null
        if ($LASTEXITCODE -ne 0) { throw "AWS Secret metadata lookup failed for $name." }
        # Supported runtime payload is one SecretString. Capture it only in
        # memory, reject structured JSON, and never include it in diagnostics.
        $payload = (& aws secretsmanager get-secret-value --region $Config.region --secret-id $reference --query SecretString --output text 2>$null | Out-String).Trim()
        if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($payload)) { throw "AWS Secret payload lookup failed for $name." }
        if ($payload -match '^\s*[\[{]') { throw "AWS Secret payload for $name must be a single string." }
      }
      'azure' {
        # Validate through the Key Vault data plane. Azure Resource Manager does
        # not consistently expose Secret children through `az resource show`.
        if ($reference -notmatch '(?i)/vaults/([^/]+)/secrets/([^/]+)$') { throw "Invalid Azure Key Vault Secret reference for $name." }
        $vaultName = $matches[1]
        $secretName = $matches[2]
        & az keyvault secret show --vault-name $vaultName --name $secretName --query id --output tsv *> $null
        if ($LASTEXITCODE -ne 0) { throw "Azure Key Vault Secret metadata lookup failed for $name." }
      }
      'gcp' {
        $project = (Get-TfValue 'project_id').Trim()
        & gcloud secrets describe $reference --project $project --format='value(name)' *> $null
        if ($LASTEXITCODE -ne 0) { throw "GCP Secret metadata lookup failed for $name." }
        $version = (& gcloud secrets versions list $reference --project $project --filter=state:ENABLED --limit=1 --format='value(name)' 2>$null | Out-String).Trim()
        if ($LASTEXITCODE -ne 0 -or -not $version) { throw "GCP Secret has no enabled value version for $name." }
      }
    }
  }
}

function Test-DigestCheckpoint {
  $targets = @($Config.registryTargets)
  if ($targets.Count -eq 0) { return $true }
  if (-not (Test-Path $DigestPath) -or -not (Test-Path $DigestMetaPath)) { return $false }
  $lines = @(Get-Content -Encoding UTF8 -LiteralPath $DigestPath)
  $digests = @{}
  foreach ($line in $lines) {
    if ($line -match '^TF_VAR_(image_digest_[A-Za-z0-9_]+)=(sha256:[0-9a-f]{64})$') { $digests[$matches[1]] = $matches[2] }
  }
  foreach ($target in $targets) {
    $key = 'image_digest_' + $target.workload
    if (-not $digests.ContainsKey($key)) { return $false }
  }
  try { $metadata = Get-Content -Raw -Encoding UTF8 -LiteralPath $DigestMetaPath | ConvertFrom-Json } catch { return $false }
  if ($metadata.provider -ne $Config.provider -or $metadata.region -ne $Config.region) { return $false }
  $applicationRoot = Resolve-Path (Join-Path $Root '..')
  if ($metadata.sourceHash -ne (Get-SourceHash $applicationRoot)) { return $false }
  if ($metadata.cloudIdentity -ne (Get-CloudIdentity)) { return $false }
  $recorded = @($metadata.workloads | ForEach-Object { [string]$_ })
  $recordedKey = (@($recorded | Sort-Object) -join '|')
  $targetKey = (@($targets | ForEach-Object { [string]$_.workload } | Sort-Object) -join '|')
  if ($recordedKey -ne $targetKey) { return $false }
  # A digest file alone is not proof that the image still exists in this registry.
  Initialize-Tofu
  foreach ($target in $targets) {
    $registryUrl = (& tofu output -raw $target.output 2>$null).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $registryUrl) { return $false }
    $digest = $digests['image_digest_' + $target.workload]
    switch ($Config.provider) {
      'aws' {
        $repository = $registryUrl.Substring($registryUrl.LastIndexOf('/') + 1)
        & aws ecr describe-images --region $Config.region --repository-name $repository --image-ids imageDigest=$digest --query 'imageDetails[0].imageDigest' --output text *> $null
      }
      'azure' {
        $parts = $registryUrl.Split('/')
        $registry = $parts[0].Split('.')[0]
        $repository = $parts[1]
        & az acr repository show --name $registry --image "$repository@$digest" --query name --output tsv *> $null
      }
      'gcp' { & gcloud artifacts docker images describe "$registryUrl@$digest" --format='value(image_summary.digest)' *> $null }
    }
    if ($LASTEXITCODE -ne 0) { return $false }
  }
  return $true
}

function Import-RuntimeValues {
  $envPath = Join-Path $RuntimeRoot '.env'
  if (Test-Path $envPath) {
    $env:TF_VAR_runtime_env = [IO.File]::ReadAllText($envPath)
  }
  if (Test-Path $DigestPath) {
    Get-Content -Encoding UTF8 $DigestPath | ForEach-Object {
      if ($_ -match '^(TF_VAR_[A-Za-z0-9_]+)=(.+)$') {
        Set-Item -Path ('Env:' + $matches[1]) -Value $matches[2]
      }
    }
  }
}

function Initialize-Tofu {
  Set-Location $TofuRoot
  $providerCache = Initialize-ProviderCache
  Write-Host "OpenTofu provider cache: $providerCache" -ForegroundColor DarkGray
  Write-Host 'Initializing OpenTofu. The first provider download can take several minutes.' -ForegroundColor Cyan
  Invoke-TofuInitWithHeartbeat
  Write-Host 'OpenTofu initialization completed.' -ForegroundColor Green
}

function Initialize-Images {
  if (Test-DigestCheckpoint) {
    Show-DeploymentProgress 65 'Reusing the prepared application image.'
    Write-Host 'Reusing the recorded image digests after registry, source, and cloud identity checks.' -ForegroundColor Green
    return $true
  }
  if (Test-Path $DigestPath) {
    Write-Host 'The recorded image checkpoint is incomplete or stale; it will not be reused.' -ForegroundColor Yellow
    Remove-Item -LiteralPath $DigestPath,$DigestMetaPath -Force -ErrorAction SilentlyContinue
  }
  $answer = Read-Host 'Container registries will now be created and may incur cloud charges. Continue? [y/N]'
  if ($answer -notmatch '^(?i)y(?:es)?$') {
    Write-Host 'Deployment cancelled before creating cloud resources.' -ForegroundColor Yellow
    Complete-DeploymentProgress
    return $false
  }

  Show-DeploymentProgress 15 'Initializing OpenTofu providers.'
  Initialize-Tofu
  Show-DeploymentProgress 20 'OpenTofu providers are ready.'
  $placeholder = 'sha256:' + ('0' * 64)
  $targetArgs = @()
  foreach ($target in @($Config.registryTargets)) {
    $targetArgs += "-target=$($target.address)"
    $targetArgs += "-var=image_digest_$($target.workload)=$placeholder"
  }
  if ($targetArgs.Count -gt 0) {
    Show-DeploymentProgress 25 'Preparing the container registry.'
    Invoke-Checked 'tofu' (@('apply', '-auto-approve') + $targetArgs)
  }
  Show-DeploymentProgress 30 'Container registry preparation completed.'

  $applicationRoot = Resolve-Path (Join-Path $Root '..')
  $digestLines = @()
  $targets = @($Config.registryTargets)
  $targetCount = [Math]::Max(1, $targets.Count)
  for ($targetIndex = 0; $targetIndex -lt $targets.Count; $targetIndex++) {
    $target = $targets[$targetIndex]
    $registryUrl = (& tofu output -raw $target.output).Trim()
    if ($LASTEXITCODE -ne 0) { throw "Cannot read registry output $($target.output)." }
    $registryHost = $registryUrl.Split('/')[0]
    $previousDockerConfig = [Environment]::GetEnvironmentVariable('DOCKER_CONFIG', 'Process')
    $temporaryDockerConfig = $null
    switch ($Config.provider) {
      'aws' {
        $temporaryDockerConfig = New-AwsDockerConfig $Config.region $registryHost
        $env:DOCKER_CONFIG = $temporaryDockerConfig
      }
      'azure' { az acr login --name $registryHost.Split('.')[0] }
      'gcp' { gcloud auth configure-docker $registryHost --quiet }
    }
    if ($LASTEXITCODE -ne 0) { throw 'Container registry login failed.' }

    try {
      $tag = 'easydep-' + $target.workload + '-' + (Get-Date -Format 'yyyyMMddHHmmss')
      $imageTag = $registryUrl + ':' + $tag
      $buildPercent = 35 + [int][Math]::Floor((25.0 * $targetIndex) / $targetCount)
      $pushPercent = 35 + [int][Math]::Floor((25.0 * ($targetIndex + 0.5)) / $targetCount)
      Show-DeploymentProgress $buildPercent "Building image for $($target.workload)."
      Invoke-Checked 'docker' @('build', '-t', $imageTag, $applicationRoot)
      Show-DeploymentProgress $pushPercent "Pushing image for $($target.workload)."
      $pushLines = [Collections.Generic.List[string]]::new()
      docker push $imageTag 2>&1 | ForEach-Object {
        $line = $_.ToString()
        $pushLines.Add($line)
        Write-Host $line
      }
      $pushExitCode = $LASTEXITCODE
      $pushOutput = $pushLines -join [Environment]::NewLine
      if ($pushExitCode -ne 0) { throw 'Docker image push failed.' }
      $digest = [regex]::Match($pushOutput, 'digest: (sha256:[0-9a-f]{64})')
      if (-not $digest.Success) { throw 'The registry did not report an immutable image digest.' }
      $digestLines += "TF_VAR_image_digest_$($target.workload)=$($digest.Groups[1].Value)"
    } finally {
      if ($temporaryDockerConfig) {
        Remove-Item -LiteralPath $temporaryDockerConfig -Recurse -Force -ErrorAction SilentlyContinue
        if ($previousDockerConfig) {
          $env:DOCKER_CONFIG = $previousDockerConfig
        } else {
          Remove-Item Env:DOCKER_CONFIG -ErrorAction SilentlyContinue
        }
      }
    }
  }
  $applicationRoot = Resolve-Path (Join-Path $Root '..')
  $metadata = @{
    provider = $Config.provider
    region = $Config.region
    cloudIdentity = Get-CloudIdentity
    sourceHash = Get-SourceHash $applicationRoot
    workloads = @($targets | ForEach-Object { [string]$_.workload })
  } | ConvertTo-Json -Depth 4 -Compress
  $digestTemporary = $DigestPath + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
  $metadataTemporary = $DigestMetaPath + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
  try {
    $digestLines | Set-Content -Encoding UTF8 -LiteralPath $digestTemporary
    Set-Content -Encoding UTF8 -LiteralPath $metadataTemporary -Value $metadata
    Move-Item -LiteralPath $digestTemporary -Destination $DigestPath -Force
    Move-Item -LiteralPath $metadataTemporary -Destination $DigestMetaPath -Force
  } finally {
    Remove-Item -LiteralPath $digestTemporary,$metadataTemporary -Force -ErrorAction SilentlyContinue
  }
  Show-DeploymentProgress 65 'Application images are ready.'
  return $true
}

function New-AndApplyPlan {
  Set-Location $TofuRoot
  Test-DeploymentIdentity $true
  Import-RuntimeValues
  Show-DeploymentProgress 70 'Validating the OpenTofu configuration.'
  Invoke-Checked 'tofu' @('validate', '-no-color')
  Show-DeploymentProgress 75 'Creating the deployment plan.'
  Invoke-Checked 'tofu' @('plan', '-input=false', '-out=easydep.tfplan')
  Show-DeploymentProgress 80 'Review the deployment plan shown below.'
  Invoke-Checked 'tofu' @('show', '-no-color', 'easydep.tfplan')
  $answer = Read-Host 'Apply the plan shown above? [y/N]'
  if ($answer -notmatch '^(?i)y(?:es)?$') {
    Write-Host 'The plan was saved but not applied.' -ForegroundColor Yellow
    Complete-DeploymentProgress
    return $false
  }
  Show-DeploymentProgress 85 'Applying the deployment plan.'
  Invoke-Checked 'tofu' @('apply', 'easydep.tfplan')
  Show-DeploymentProgress 90 'Cloud resources are ready; starting health verification.'
  return $true
}

function Test-DeployedApplication {
  if (@($Config.healthOutputs).Count -eq 0) {
    Write-Host 'health=UNVERIFIED. This private application has no public health URL.' -ForegroundColor Yellow
    switch ($Config.provider) {
      'aws' { Write-Host 'Provider-native check: use AWS Systems Manager Run Command to curl the private health path from the VM/VPC.' -ForegroundColor Yellow }
      'azure' { Write-Host 'Provider-native check: use az vm run-command invoke to curl the private health path from the VM/VNet.' -ForegroundColor Yellow }
      'gcp' { Write-Host 'Provider-native check: use gcloud compute ssh with --command to curl the private health path from the VM.' -ForegroundColor Yellow }
    }
    return
  }
  foreach ($outputName in @($Config.healthOutputs)) {
    $healthUrl = (& tofu output -raw $outputName).Trim()
    $healthy = $false
    Show-DeploymentProgress 92 "Waiting for application health at $healthUrl"
    Write-Host "Waiting for $healthUrl"
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
      $healthPercent = 92 + [int][Math]::Floor((7.0 * $attempt) / 60)
      Write-Progress -Activity $DeploymentActivity `
        -Status ("Health check attempt {0}/60" -f ($attempt + 1)) `
        -PercentComplete $healthPercent
      try {
        Invoke-WebRequest -UseBasicParsing -TimeoutSec 10 -Uri $healthUrl | Out-Null
        $healthy = $true
        break
      } catch { Start-Sleep -Seconds 10 }
    }
    if (-not $healthy) { throw "Health check timed out after ten minutes: $healthUrl" }
  }
  Show-DeploymentProgress 100 'Deployment and health verification completed.'
  Complete-DeploymentProgress
  Write-Host 'health=VERIFIED. Deployment and health verification completed.' -ForegroundColor Green
}

function Get-RetainedDiskIds([object]$Descriptor, [bool]$AfterDestroy = $false) {
  $owner = [string]($Descriptor.vmss_resource_id | ForEach-Object { $_ })
  if (-not $owner) { $owner = [string]$Descriptor.owner_resource_id }
  $storage = [string]$Descriptor.storage_ref
  $raw = $null
  switch ($Config.provider) {
    'aws' {
      $asg = [string]$Descriptor.autoscaling_group_name
      $launchTemplate = [string]$Descriptor.launch_template_id
      $device = [string]$Descriptor.block_device_name
      if (-not $asg -or -not $launchTemplate -or -not $device) { throw 'Renderer retained disk output has no AWS ASG lookup coordinates.' }
      if ([string]$Descriptor.lookup_strategy -ne 'asg-instance-block-device-volume-id') { throw 'Renderer retained disk output has no AWS lookup contract.' }
      if ($AfterDestroy) { return @() }
      $instanceIds = @(& aws autoscaling describe-auto-scaling-groups --region $Config.region --auto-scaling-group-names $asg --query 'AutoScalingGroups[0].Instances[].InstanceId' --output json 2>$null | ConvertFrom-Json)
      if ($LASTEXITCODE -ne 0) { throw 'AWS ASG instance lookup for retained disks failed.' }
      $ids = @()
      foreach ($instanceId in $instanceIds) {
        $mappings = (& aws ec2 describe-instances --region $Config.region --instance-ids $instanceId --query 'Reservations[0].Instances[0].BlockDeviceMappings' --output json 2>$null | Out-String)
        if ($LASTEXITCODE -ne 0) { throw 'AWS instance disk lookup for retained disks failed.' }
        try { $blocks = @($mappings | ConvertFrom-Json) } catch { throw 'AWS instance disk response was unreadable.' }
        $ids += @($blocks | Where-Object { $_.DeviceName -eq $device } | ForEach-Object { [string]$_.Ebs.VolumeId })
      }
      return @($ids | Where-Object { $_ })
    }
    'azure' {
      if ($owner -notmatch '(?i)^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft.Compute/virtualMachineScaleSets/[^/]+$') { throw 'Renderer retained disk output has no Azure VMSS owner resource ID.' }
      $lun = [int]$Descriptor.vmss_data_disk_lun
      if ([string]$Descriptor.lookup_strategy -ne 'vmss-instance-lun-managed-disk-id' -or -not [string]$Descriptor.data_disk_profile_name) { throw 'Renderer retained disk output has no Azure lookup contract.' }
      if ($AfterDestroy) { return @() }
      $instanceIds = @(& az vmss list-instances --ids $owner --query '[].instanceId' --output json 2>$null | ConvertFrom-Json)
      if ($LASTEXITCODE -ne 0) { throw 'Azure VMSS instance lookup for retained disks failed.' }
      $ids = @()
      foreach ($instanceId in $instanceIds) {
        $profile = (& az vmss show --ids $owner --instance-id $instanceId --query 'storageProfile.dataDisks' --output json 2>$null | Out-String)
        if ($LASTEXITCODE -ne 0) { throw 'Azure VMSS data disk lookup for retained disks failed.' }
        try { $disks = @($profile | ConvertFrom-Json) } catch { throw 'Azure VMSS data disk response was unreadable.' }
        $ids += @($disks | Where-Object { [int]$_.lun -eq $lun } | ForEach-Object { [string]$_.managedDisk.id })
      }
      return @($ids | Where-Object { $_ })
    }
    'gcp' {
      $device = [string]$Descriptor.device_name
      $mig = [string]$Descriptor.mig_resource_id
      if (-not $mig) { $mig = $owner }
      if (-not $mig -or -not $device) { throw 'Renderer retained disk output has no GCP MIG/device coordinates.' }
      if ([string]$Descriptor.lookup_strategy -ne 'mig-instance-device-source' -or -not [string]$Descriptor.mig_region) { throw 'Renderer retained disk output has no GCP lookup contract.' }
      if ($AfterDestroy) { return @() }
      $migName = $mig.TrimEnd('/').Split('/')[-1]
      $region = [string]$Descriptor.mig_region
      $instances = @(& gcloud compute instance-groups managed list-instances $migName --region=$region --format=json 2>$null | ConvertFrom-Json)
      if ($LASTEXITCODE -ne 0) { throw 'GCP MIG instance lookup for retained disks failed.' }
      $sources = @()
      foreach ($instance in $instances) {
        $instanceUri = [string]$instance.instance
        if (-not $instanceUri) { continue }
        $instanceJson = (& gcloud compute instances describe $instanceUri --format=json 2>$null | Out-String)
        if ($LASTEXITCODE -ne 0) { throw 'GCP instance disk lookup for retained disks failed.' }
        try { $details = $instanceJson | ConvertFrom-Json } catch { throw 'GCP instance response was unreadable.' }
        $sources += @($details.disks | Where-Object { $_.deviceName -eq $device } | ForEach-Object { [string]$_.source })
      }
      return @($sources | Where-Object { $_ })
    }
  }
  if ($LASTEXITCODE -ne 0) { throw 'Provider retained disk metadata lookup failed.' }
  try {
    $items = @($raw | ConvertFrom-Json)
    return @($items | ForEach-Object {
      if ($_ -is [string]) { [string]$_ } elseif ($_.id) { [string]$_.id } elseif ($_.selfLink) { [string]$_.selfLink } elseif ($_.name) { [string]$_.name }
    } | Where-Object { $_ })
  } catch { throw 'Provider retained disk metadata response was unreadable.' }
}

function Get-RetainedDiskSnapshot([object[]]$Previous = @(), [bool]$AfterDestroy = $false) {
  $snapshot = @()
  $descriptors = @()
  if ($Previous.Count -gt 0) {
    $descriptors = @($Previous | Group-Object output | ForEach-Object { $_.Group[0] })
  } else {
    foreach ($outputName in @($Config.retainedDiskOutputs)) {
      $raw = (& tofu output -json $outputName 2>$null | Out-String)
      if ($LASTEXITCODE -ne 0 -or -not $raw.Trim()) { throw "Cannot read retained disk output $outputName." }
      try { $descriptors += [PSCustomObject]@{ output = $outputName; descriptor = ($raw | ConvertFrom-Json) } } catch { throw "Retained disk output $outputName is unreadable." }
    }
  }
  foreach ($entry in $descriptors) {
    $outputName = [string]$entry.output
    $descriptor = $entry.descriptor
    foreach ($id in @(Get-RetainedDiskIds $descriptor $AfterDestroy)) {
      $snapshot += [PSCustomObject]@{ output = $outputName; descriptor = $descriptor; id = [string]$id }
    }
  }
  if ($AfterDestroy -and $Previous.Count -gt 0) {
    if ($Config.provider -in @('aws','azure','gcp')) {
      $snapshot = @()
      foreach ($prior in $Previous) {
        $exists = $false
        switch ($Config.provider) {
          'aws' {
            & aws ec2 describe-volumes --region $Config.region --volume-ids $prior.id --query 'Volumes[0].VolumeId' --output text *> $null
            $exists = $LASTEXITCODE -eq 0
          }
          'azure' {
            & az disk show --ids $prior.id --query id --output tsv *> $null
            $exists = $LASTEXITCODE -eq 0
          }
          'gcp' {
            & gcloud compute disks describe $prior.id --format='value(selfLink)' *> $null
            $exists = $LASTEXITCODE -eq 0
          }
        }
        if ($exists) { $snapshot += [PSCustomObject]@{ output = $prior.output; descriptor = $prior.descriptor; id = [string]$prior.id } }
      }
    }
  }
  return $snapshot
}

function Start-OrContinueDeployment {
  Show-DeploymentProgress 5 'Checking local tools and cloud login.'
  Test-Prerequisites $true
  Show-DeploymentProgress 10 'Collecting missing deployment inputs.'
  Initialize-Inputs
  Test-DeploymentIdentity $true
  Show-DeploymentProgress 11 'Resolving or creating Secret references.'
  Initialize-Secrets
  Show-DeploymentProgress 12 'Checking Secret references before billable resources.'
  Test-SecretReferences
  if (-not (Initialize-Images)) { return }
  if (New-AndApplyPlan) { Test-DeployedApplication }
}

function Remove-Deployment {
  if (Test-Path -LiteralPath $TfvarsPath) {
    $script:TfvarsContent = Get-Content -Raw -Encoding UTF8 -LiteralPath $TfvarsPath
  }
  Test-Prerequisites $false
  $hasTofuState = Test-Path (Join-Path $TofuRoot 'terraform.tfstate')
  $hasCreatedSecrets = Test-Path -LiteralPath $CreatedSecretsPath
  if (-not $hasTofuState -and -not $hasCreatedSecrets) {
    Write-Host 'No local OpenTofu state or script-created Secret record was found.' -ForegroundColor Yellow
    return
  }
  if (-not $hasTofuState) {
    Write-Host 'No local OpenTofu state was found. Only script-created Secrets can be cleaned up.' -ForegroundColor Yellow
    Remove-CreatedSecrets
    return
  }
  $answer = Read-Host 'Destroy resources managed by this deployment state? Type DESTROY to continue'
  if ($answer -cne 'DESTROY') { Write-Host 'Destroy cancelled.'; return }
  Test-DeploymentIdentity $false
  Initialize-Tofu
  Import-RuntimeValues
  if (-not (Test-Path $DigestPath)) {
    # registry 생성 뒤 image push 전에 실패한 경우에도 destroy는 입력을 다시 묻지
    # 않아야 한다. 아직 실제 image가 없으므로 유효한 형식의 임시 digest만 전달한다.
    $placeholder = 'sha256:' + ('0' * 64)
    foreach ($target in @($Config.registryTargets)) {
      $name = 'TF_VAR_image_digest_' + $target.workload
      if (-not (Test-Path ('Env:' + $name))) {
        Set-Item -Path ('Env:' + $name) -Value $placeholder
      }
    }
  }
  $retainedLog = Join-Path $Root 'retained-resources.txt'
  $retainedBefore = @(Get-RetainedDiskSnapshot)
  foreach ($disk in $retainedBefore) {
    Add-Content -Encoding UTF8 -LiteralPath $retainedLog -Value "provider=$($Config.provider) output=$($disk.output) provider_id=$($disk.id) status=present-before-destroy"
  }
  foreach ($address in @($Config.retainedResources)) {
    $stateAddresses = @(& tofu state list)
    if ($stateAddresses -contains $address) {
      $stateText = (& tofu state show -no-color $address | Out-String)
      $idMatch = [regex]::Match($stateText, '(?m)^\s*id\s*=\s*"?([^"\r\n]+)')
      $cloudId = if ($idMatch.Success) { $idMatch.Groups[1].Value.Trim() } else { 'unknown' }
      Add-Content -Encoding UTF8 -LiteralPath $retainedLog -Value "$address cloud_id=$cloudId"
      Invoke-Checked 'tofu' @('state', 'rm', $address)
      Write-Host "Retained $address. Its cloud ID was written to retained-resources.txt." -ForegroundColor Yellow
    }
  }
  Invoke-Checked 'tofu' @('destroy', '-auto-approve')
  $retainedAfter = @(Get-RetainedDiskSnapshot $retainedBefore $true)
  foreach ($disk in $retainedBefore) {
    $stillPresent = @($retainedAfter | Where-Object { $_.id -eq $disk.id }).Count -gt 0
    $status = if ($stillPresent) { 'retained-after-destroy' } else { 'missing-after-destroy' }
    Add-Content -Encoding UTF8 -LiteralPath $retainedLog -Value "provider=$($Config.provider) output=$($disk.output) provider_id=$($disk.id) status=$status"
  }
  foreach ($disk in $retainedAfter) {
    if (@($retainedBefore | Where-Object { $_.id -eq $disk.id }).Count -eq 0) {
      Add-Content -Encoding UTF8 -LiteralPath $retainedLog -Value "provider=$($Config.provider) output=$($disk.output) provider_id=$($disk.id) status=unexpected-after-destroy"
    }
  }
  Remove-Item -LiteralPath $DigestPath -ErrorAction SilentlyContinue
  Remove-Item -LiteralPath $DigestMetaPath -ErrorAction SilentlyContinue
  Remove-Item -LiteralPath $PlanPath -ErrorAction SilentlyContinue
  Write-Host 'Managed resources were destroyed.' -ForegroundColor Green
  Remove-CreatedSecrets
}

function Show-Status {
  $image = if (Test-Path $DigestPath) { 'ready' } else { 'not prepared' }
  $state = if (Test-Path (Join-Path $TofuRoot 'terraform.tfstate')) { 'present' } else { 'not created' }
  $secrets = if (Test-Path -LiteralPath $CreatedSecretsPath) { 'script-created resources recorded' } else { 'none recorded' }
  Write-Host "Provider: $($Config.provider.ToUpper())  Region: $($Config.region)"
  Write-Host "Local state: $state  Application image: $image"
  Write-Host "Secret cleanup record: $secrets"
}

while ($true) {
  Write-Host ''
  Write-Host 'EasyDep Deployment' -ForegroundColor Cyan
  Show-Status
  Write-Host ''
  Write-Host '1. Start or continue deployment'
  Write-Host '2. Destroy deployed resources'
  Write-Host '0. Exit'
  $choice = Read-Host 'Select'
  try {
    switch ($choice) {
      '1' { Start-OrContinueDeployment }
      '2' { Remove-Deployment }
      '0' { return }
      default { Write-Host 'Select 1, 2, or 0.' -ForegroundColor Yellow }
    }
  } catch {
    Complete-DeploymentProgress
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host 'Fix the reported cause and choose Start or continue deployment again.' -ForegroundColor Yellow
  }
}
'''
    return script.replace("__CONFIG__", config_json)


def _format_open_tofu(directory: Path) -> None:
    """설치된 formatter가 있으면 package의 HCL을 검사와 같은 형식으로 고정한다."""

    executable = shutil.which("tofu") or shutil.which("terraform")
    if executable is None:
        return
    completed = subprocess.run(
        [executable, "fmt", "-recursive"],
        cwd=directory,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        output = ((completed.stderr or "") + (completed.stdout or ""))[-1000:]
        raise RuntimeError(f"OpenTofu formatting failed: {output}")


def _readme(resource_plan: dict[str, Any]) -> str:
    """패키지만 받은 사용자도 시작할 수 있는 짧은 공급자별 안내를 만든다."""

    provider = str(resource_plan.get("provider") or "")
    region = str(resource_plan.get("region") or "")
    authentication = {
        "aws": (
            "Run `aws configure`, or run `aws sso login --profile <profile>` and set "
            "`AWS_PROFILE`. The script verifies the identity with "
            f"`aws sts get-caller-identity --region {region}`."
        ),
        "azure": (
            "Run `az login`, then `az account set --subscription <subscription-id>`."
        ),
        "gcp": (
            "Run `gcloud auth login` and `gcloud config set project <project-id>`. "
            "Service-account or ADC users may instead run "
            "`gcloud auth application-default login`."
        ),
    }.get(provider, "Authenticate with the cloud provider CLI.")
    return f"""# EasyDep deployment package

This package targets **{provider.upper()}** in **{region}**. Keep `Dockerfile`, `build.gradle`, `frontend/`, `src/`, and `deployment/` together after extracting the ZIP.

## Prerequisites

Install PowerShell, OpenTofu 1.8 or newer, Docker with a running daemon, and the {provider.upper()} CLI. {authentication}

Use a short-lived, least-privilege cloud identity. Do not use a cloud account's root or owner identity. The first OpenTofu provider download can take several minutes. Providers are cached under the current user's local application-data directory and reused by other EasyDep deployment packages. Set `TF_PLUGIN_CACHE_DIR` before starting the script to use a different persistent cache.

## Deploy or resume

Open PowerShell in this `deployment` directory and run:

```powershell
.\\easydep.ps1
```

Choose **Start or continue deployment**. The script shows coarse 5–100% phase progress while it checks the environment and login, asks only for missing deployment values, prepares and uploads the image, displays the OpenTofu plan, asks before applying it, and verifies the public health URL. For each missing Secret binding, you can enter an existing canonical Secret reference or explicitly create a new Secret in AWS Secrets Manager, Azure Key Vault, or Google Secret Manager. During a long `tofu init`, a live elapsed-time heartbeat remains visible because OpenTofu does not expose provider download byte progress. The script detects the local state and image digest when you run it again after a failure.

The script clearly warns before it creates the first billable cloud resource. A newly entered Secret value is hidden, passed to the provider CLI through a restricted temporary file, and removed immediately; it is never written to `terraform.tfvars`, OpenTofu state, command arguments, or the local ownership record. OpenTofu state, `terraform.tfvars`, image digests, and `runtime/created-secret-resources.json` stay in this extracted folder; do not commit or share them. The JSON record contains only references and ownership metadata for Secrets created by this script. Passwords, API keys, and private keys belong in the selected cloud secret service, not in these files or VM metadata.

Choose **Destroy deployed resources** from the same menu when finished. Data marked for retention is removed from OpenTofu management before the other resources are destroyed. Its cloud ID is written to `retained-resources.txt`; you remain responsible for that resource and its charges. Secret cleanup has a separate typed confirmation and only targets resources in the local script-created ownership record. Existing Secret references are never deleted. AWS Secrets are scheduled with a seven-day recovery window; GCP Secrets and eligible Azure Secrets or dedicated vaults are deleted using their provider workflows.
"""


def render_deployment_package(
    application: Path, resource_plan: dict[str, Any], rendered_tofu: dict[str, str]
) -> Path:
    """`application/deployment`에 관리되는 사용자 배포 package를 원자적으로 쓴다."""

    destination = application / "deployment"
    marker = destination / ".easydep-managed"
    # 시스템 임시 폴더에서 만든 디렉터리를 Windows 앱 폴더로 옮기면 원래 앱의 ACL을
    # 상속하지 않아 Docker가 생성물을 읽지 못할 수 있다. 대상 앱 안에서 staging을
    # 만들면 같은 파일시스템에서 원자적으로 옮길 수 있고 접근 권한도 앱과 같게 유지된다.
    staging = Path(tempfile.mkdtemp(prefix=".easydep-deployment-", dir=application))
    try:
        package = staging / "deployment"
        tofu = package / "tofu"
        runtime = package / "runtime"
        tofu.mkdir(parents=True)
        runtime.mkdir()
        _write_text(package / ".easydep-managed", "easydep deployment package\n")
        # Keep every renderer-owned .tf/.tftpl file. main/variables/outputs are
        # the stable human entry points; auxiliary templates are referenced by it.
        for name, content in rendered_tofu.items():
            if name.endswith((".tf", ".tftpl")):
                _write_text(tofu / name, content)
        cloud_init = next(
            (
                content
                for name, content in rendered_tofu.items()
                if name.startswith("cloud-init_") and name.endswith(".yaml.tftpl")
            ),
            None,
        )
        if not isinstance(cloud_init, str):
            raise TypeError("OpenTofu rendering did not produce an active cloud-init template")
        compose, envs = _compose(resource_plan)
        _write_text(runtime / "compose.yaml", compose)
        env_example = (
            "# Optional non-secret overrides only. Resource bindings are supplied by cloud-init.\n"
            + "\n".join(f"# {description}\n{name}=" for name, description in envs)
            + "\n"
        )
        _write_text(runtime / ".env.example", env_example)
        _write_text(tofu / "terraform.tfvars.example", _tfvars_example(resource_plan))
        # This stable filename is a copy of the exact per-compute template that
        # main.tf passes as user_data/custom_data/instance metadata.
        _write_text(tofu / "cloud-init.yaml.tftpl", cloud_init)
        _format_open_tofu(tofu)
        _write_text(
            package / "easydep.ps1",
            _interactive_powershell_script(resource_plan, rendered_tofu),
        )
        _write_text(package / "README.md", _readme(resource_plan))
        if destination.exists():
            if not marker.is_file():
                raise ValueError(f"Refusing to replace unmanaged deployment package: {destination}")
            shutil.rmtree(destination, onerror=remove_readonly)
        shutil.move(str(package), str(destination))
        return destination
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


__all__ = ["render_deployment_package"]
