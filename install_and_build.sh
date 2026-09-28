#!/usr/bin/env bash
set -euo pipefail

# 전체 프로젝트 파일과 같은 루트에서 설치·빌드를 수행한다.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
SOURCE_ROOT="$SCRIPT_DIR"
RUN_SERVER=0
SKIP_INSTALL=0
DATABASE_CONTAINER="easydep-mysql-dev"

fail() {
    printf '오류: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<'USAGE'
사용법: bash install_and_build.sh [옵션]

기본 실행: Python/npm 의존성 설치, 프론트엔드 빌드, 공용 도구 이미지 빌드
  --run              설치·빌드 후 MySQL과 서버 실행
  --skip-install     기존 가상환경·프론트엔드 빌드·도구 이미지 재사용
  --help             사용법 표시

서버 실행: bash install_and_build.sh --skip-install --run
서버 종료: Ctrl+C
MySQL 종료: docker stop easydep-mysql-dev (데이터 볼륨 유지)
USAGE
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --run) RUN_SERVER=1 ;;
        --skip-install) SKIP_INSTALL=1 ;;
        --help|-h) usage; exit 0 ;;
        *) fail "알 수 없는 옵션: $1" ;;
    esac
    shift
done

require_command() {
    command -v "$1" >/dev/null 2>&1 || fail "$1 명령을 설치하고 PATH에 추가하세요."
}

docker_command() {
    # Git Bash가 컨테이너 내부 경로를 Windows 경로로 바꾸지 않게 한다.
    MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' docker "$@"
}

is_project() {
    [ -d "$1/app" ] && [ -f "$1/server.py" ] && [ -f "$1/requirements.txt" ] &&
        [ -f "$1/.env.example" ] && [ -f "$1/frontend/package.json" ] &&
        [ -f "$1/frontend/package-lock.json" ] && [ -f "$1/docker/Dockerfile.toolchain" ]
}

is_project "$SOURCE_ROOT" ||
    fail "전체 프로젝트 파일과 install_and_build.sh를 같은 루트에 두세요."
cd -- "$SOURCE_ROOT"

case "$(uname -s)" in
    Darwin) export DOCKER_DEFAULT_PLATFORM=linux/amd64 ;;
    Linux)
        case "$(uname -m)" in
            x86_64|amd64) ;;
            *) fail "현재 Linux 툴체인은 x86_64 환경을 사용합니다." ;;
        esac
        ;;
    MINGW*|MSYS*|CYGWIN*) export DOCKER_DEFAULT_PLATFORM=linux/amd64 ;;
    *) fail "macOS, Linux 또는 Windows Git Bash에서 실행하세요." ;;
esac

HOST_PYTHON=""
for candidate in python3.13 python3.12 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 &&
        "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 12))' 2>/dev/null; then
        HOST_PYTHON="$candidate"
        break
    fi
done
[ -n "$HOST_PYTHON" ] || fail "Python 3.12 이상이 필요합니다."
require_command node
require_command npm
require_command docker
node -e 'const [major, minor] = process.versions.node.split(".").map(Number); process.exit(major > 22 || (major === 22 && minor >= 12) ? 0 : 1)' ||
    fail "Node.js 22.12 이상이 필요합니다."
docker_command info >/dev/null 2>&1 || fail "Docker를 시작하고 현재 계정의 접근 권한을 확인하세요."
docker_command compose version >/dev/null 2>&1 || fail "Docker Compose 플러그인이 필요합니다."

[ -f .env ] || cp .env.example .env
printf '프로젝트 소스: %s\n환경 설정: %s/.env\n' "$SOURCE_ROOT" "$SOURCE_ROOT"

if [ "$SKIP_INSTALL" -eq 0 ] && [ ! -d .venv ]; then
    "$HOST_PYTHON" -m venv .venv
fi
if [ -x .venv/bin/python ]; then
    PROJECT_PYTHON=".venv/bin/python"
elif [ -f .venv/Scripts/python.exe ]; then
    PROJECT_PYTHON=".venv/Scripts/python.exe"
else
    fail "가상환경이 없습니다. --skip-install 없이 설치를 먼저 실행하세요."
fi
"$PROJECT_PYTHON" -c 'import sys; raise SystemExit(sys.version_info < (3, 12))' ||
    fail "가상환경의 Python도 3.12 이상이어야 합니다."

if [ "$SKIP_INSTALL" -eq 0 ]; then
    "$PROJECT_PYTHON" -m pip install --disable-pip-version-check "uv==0.8.22"
    "$PROJECT_PYTHON" -m uv pip install --python "$PROJECT_PYTHON" \
        --index-strategy unsafe-best-match --requirements requirements.txt
    npm --prefix frontend ci --no-audit --no-fund
    npm --prefix frontend run build
fi

[ -f frontend/build/index.html ] || fail "frontend/build/index.html이 없습니다. 설치·빌드를 먼저 실행하세요."
"$PROJECT_PYTHON" -c 'import dotenv, pymysql, uvicorn' ||
    fail "가상환경의 실행 의존성이 없습니다. --skip-install 없이 설치하세요."

setting() {
    "$PROJECT_PYTHON" -X utf8 -c '
import os, sys
from dotenv import dotenv_values
values = dotenv_values(".env")
name, default = sys.argv[1:3]
value = os.environ.get(name, values.get(name))
print(default if value is None else value)
' "$1" "$2"
}

TOOLCHAIN_IMAGE="$(setting EASYDEP_TOOLCHAIN_IMAGE easydep-toolchain:local)"
TOOLCHAIN_IMAGE="${TOOLCHAIN_IMAGE:-easydep-toolchain:local}"
export EASYDEP_TOOLCHAIN_IMAGE="$TOOLCHAIN_IMAGE"
if [ "$SKIP_INSTALL" -eq 0 ]; then
    docker_command build --platform linux/amd64 \
        --file docker/Dockerfile.toolchain --target toolchain \
        --tag "$TOOLCHAIN_IMAGE" .
else
    docker_command image inspect "$TOOLCHAIN_IMAGE" >/dev/null 2>&1 ||
        fail "도구 이미지가 없습니다. 설치·빌드를 먼저 실행하세요."
fi

printf '설치·빌드 준비 완료. LLM_PROVIDER, API_KEY, BASE_URL, MODEL을 .env에 설정하세요.\n'
if [ "$RUN_SERVER" -eq 0 ]; then
    printf '실행 명령: bash "%s/install_and_build.sh" --skip-install --run\n' "$SCRIPT_DIR"
    exit 0
fi

"$PROJECT_PYTHON" -X utf8 -c '
import os, sys
from dotenv import dotenv_values
values = dotenv_values(".env")
for name in ("LLM_PROVIDER", "API_KEY", "BASE_URL", "MODEL"):
    value = str(os.environ.get(name, values.get(name) or "")).strip()
    if not value or (value.startswith("<") and value.endswith(">")):
        print(f".env의 {name} 값을 설정하세요.", file=sys.stderr)
        raise SystemExit(1)
'

"$PROJECT_PYTHON" -X utf8 -c '
import socket, sys
with socket.socket() as listener:
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind(("127.0.0.1", 8100))
    except OSError:
        print("8100 포트를 사용하는 서버를 먼저 중지하세요.", file=sys.stderr)
        raise SystemExit(1)
'

DB_HOST="$(setting DB_HOST 127.0.0.1)"
DB_PORT="$(setting DB_PORT 33060)"
DB_USER="$(setting DB_USER root)"
DB_PASSWORD="$(setting DB_PASSWORD "")"
DB_NAME="$(setting DB_NAME easydep)"
[ -n "$DB_HOST" ] || fail ".env에 DB_HOST를 설정하세요."
[ -n "$DB_USER" ] || fail ".env에 DB_USER를 설정하세요."
[ -n "$DB_NAME" ] || fail ".env에 DB_NAME을 설정하세요."
"$PROJECT_PYTHON" -c 'import sys; raise SystemExit(not sys.argv[1].isdigit() or not 1 <= int(sys.argv[1]) <= 65535)' "$DB_PORT" ||
    fail "DB_PORT는 1~65535 사이의 정수여야 합니다."

if { [ "$DB_HOST" = "127.0.0.1" ] || [ "$DB_HOST" = "localhost" ]; } && [ "$DB_USER" = "root" ]; then
    [ -n "$DB_PASSWORD" ] || fail "개발용 MySQL을 준비하려면 .env에 DB_PASSWORD를 설정하세요."
    if DATABASE_INFO="$(docker_command container inspect "$DATABASE_CONTAINER" 2>/dev/null)"; then
        # 다른 설정의 기존 컨테이너를 삭제하거나 바꾸지 않는다.
        printf '%s' "$DATABASE_INFO" |
            EXPECTED_DB_PORT="$DB_PORT" EXPECTED_DB_PASSWORD="$DB_PASSWORD" EXPECTED_DB_NAME="$DB_NAME" \
            "$PROJECT_PYTHON" -c '
import json, os, sys
info = json.load(sys.stdin)[0]
values = dict(item.split("=", 1) for item in info["Config"]["Env"] if "=" in item)
ports = (info["HostConfig"].get("PortBindings") or {}).get("3306/tcp") or []
valid = (
    any(item["HostPort"] == os.environ["EXPECTED_DB_PORT"] for item in ports)
    and values.get("MYSQL_ROOT_PASSWORD") == os.environ["EXPECTED_DB_PASSWORD"]
    and values.get("MYSQL_DATABASE") == os.environ["EXPECTED_DB_NAME"]
)
if not valid:
    print("기존 MySQL 컨테이너의 포트·DB·암호 설정이 .env와 다릅니다.", file=sys.stderr)
    raise SystemExit(1)
'
        docker_command start "$DATABASE_CONTAINER" >/dev/null
    else
        MYSQL_ROOT_PASSWORD="$DB_PASSWORD" MYSQL_DATABASE="$DB_NAME" \
            docker_command run -d --name "$DATABASE_CONTAINER" \
            -p "127.0.0.1:$DB_PORT:3306" \
            -e MYSQL_ROOT_PASSWORD -e MYSQL_DATABASE \
            -v easydep-mysql-dev-data:/var/lib/mysql mysql:8.4 >/dev/null
    fi
    deadline=$((SECONDS + 180))
    while true; do
        # 서버와 같은 호스트 주소·인증값·DB로 실제 쿼리를 실행한다.
        if DB_CHECK_HOST="$DB_HOST" DB_CHECK_PORT="$DB_PORT" DB_CHECK_USER="$DB_USER" \
            DB_CHECK_PASSWORD="$DB_PASSWORD" DB_CHECK_NAME="$DB_NAME" \
            "$PROJECT_PYTHON" -X utf8 -c '
import os, sys
import pymysql
try:
    with pymysql.connect(
        host=os.environ["DB_CHECK_HOST"],
        port=int(os.environ["DB_CHECK_PORT"]),
        user=os.environ["DB_CHECK_USER"],
        password=os.environ["DB_CHECK_PASSWORD"],
        database=os.environ["DB_CHECK_NAME"],
        charset="utf8mb4",
        connect_timeout=3,
        read_timeout=3,
        write_timeout=3,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
except pymysql.MySQLError as error:
    code = error.args[0] if error.args else None
    raise SystemExit(2 if code in (1044, 1045, 1049) else 1)
'; then
            break
        else
            database_status=$?
        fi
        [ "$database_status" -ne 2 ] ||
            fail "MySQL 인증 또는 DB 선택에 실패했습니다. .env의 DB 설정을 확인하세요. 기존 데이터는 유지됩니다."
        [ "$SECONDS" -lt "$deadline" ] ||
            fail "MySQL 연결 시간이 초과되었습니다. docker logs $DATABASE_CONTAINER 명령으로 확인하세요."
        sleep 2
    done
else
    printf '기존 DB 연결 설정을 사용합니다. 해당 DB를 먼저 준비하세요.\n'
fi

printf '서버 실행: http://127.0.0.1:8100/\nAPI 문서: http://127.0.0.1:8100/docs\n종료: Ctrl+C\n'
exec "$PROJECT_PYTHON" -X utf8 -m uvicorn server:app --host 127.0.0.1 --port 8100
