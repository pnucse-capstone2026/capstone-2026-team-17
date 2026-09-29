#!/bin/sh
set -eu

# 기본 구현 도구가 준비됐는지 먼저 확인하고, Testing 전용 브라우저만 추가로 검사한다.
repository_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
sh "$repository_root/scripts/bootstrap-implementation-tools.sh"

command -v playwright >/dev/null 2>&1 || {
  echo "Required Testing command is not installed: playwright" >&2
  exit 1
}
playwright --version

# 실제 화면 비교는 하지 않지만 DOM, JavaScript, routing을 실제 브라우저 엔진에서
# 실행하려면 headless shell이 필요하다. 전체 Chromium은 설치하지 않는다.
python -m playwright install --list | grep -Eq "chromium[-_]headless[-_]shell"
echo "EasyDep browser testing tools are ready."
