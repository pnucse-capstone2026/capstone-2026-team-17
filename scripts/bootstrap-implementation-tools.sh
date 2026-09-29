#!/bin/sh
set -eu

repository_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
export GRADLE_USER_HOME="$repository_root/.easydep/gradle-cache"

for command in node npm java gradle tofu trivy cloud-init shellcheck pwsh pytest; do
  command -v "$command" >/dev/null 2>&1 || {
    echo "Required command is not installed: $command" >&2
    exit 1
  }
done

# 공용 툴체인에는 Gradle 자체가 들어 있다. wrapper 배포본을 이미지 빌드 중 다시 받아
# 저장하지 않고, 설치된 고정 버전만 확인한다.
gradle --version --no-daemon
tofu version
# OpenTofu 자체만 있어서는 오프라인 validate를 할 수 없다. 이미지에 고정한 세 CSP
# provider가 모두 들어 있는지 빌드 단계에서 확인해 실행 중 다운로드를 막는다.
for provider in aws azurerm google; do
  test -d "/opt/easydep/provider-mirror/registry.opentofu.org/hashicorp/$provider"
done
trivy --version
docker compose version
cloud-init --version
shellcheck --version
pwsh -NoLogo -NoProfile -Command '$PSVersionTable.PSVersion.ToString()'
pytest --version
echo "EasyDep implementation tools are ready."
