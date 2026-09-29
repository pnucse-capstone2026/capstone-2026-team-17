"""멤버가 선언한 생성 도구 컨테이너 호출을 runner의 고정 도구로 치환한다."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path, PurePosixPath

from app.implementation.runtime.runner_compat import gradle_command

OPENAPI_JARS = {
    "openapitools/openapi-generator-cli": PurePosixPath("/opt/easydep/openapi-generator-7.24.0.jar"),
    "openapitools/openapi-generator-cli:latest": PurePosixPath(
        "/opt/easydep/openapi-generator-7.24.0.jar"
    ),
    "openapitools/openapi-generator-cli:v7.24.0": PurePosixPath(
        "/opt/easydep/openapi-generator-7.24.0.jar"
    ),
}
GRADLE_IMAGES = {"gradle:8.14.2-jdk21"}


def translate(arguments: list[str]) -> tuple[list[str], Path | None, dict[str, str]]:
    if not arguments or arguments[0] != "run":
        raise ValueError("runner에서는 허용된 docker run 생성 도구만 사용할 수 있습니다")
    index = 1
    working_directory: Path | None = None
    environment = os.environ.copy()
    volumes: list[tuple[str, str]] = []
    while index < len(arguments):
        argument = arguments[index]
        if argument in {"--rm", "--init"}:
            index += 1
            continue
        if argument in {"-v", "--volume"}:
            source, separator, target = arguments[index + 1].partition(":")
            if not separator or not source.startswith("/") or not target.startswith("/"):
                raise ValueError("runner bind 경로는 절대 Linux 경로여야 합니다")
            volumes.append((source.rstrip("/"), target.rstrip("/")))
            index += 2
            continue
        if argument in {"-w", "--workdir"}:
            working_directory = Path(arguments[index + 1])
            index += 2
            continue
        if argument in {"-e", "--env"}:
            name, _, value = arguments[index + 1].partition("=")
            if not name:
                raise ValueError("빈 환경 변수 이름은 허용하지 않습니다")
            environment[name] = value or os.environ.get(name, "")
            index += 2
            continue
        if argument.startswith("-"):
            raise ValueError(f"runner가 지원하지 않는 docker 옵션입니다: {argument}")
        break
    if index >= len(arguments):
        raise ValueError("docker run 이미지가 없습니다")
    image = arguments[index]
    command = arguments[index + 1 :]

    def local_path(value: str) -> str:
        for source, target in sorted(volumes, key=lambda item: len(item[1]), reverse=True):
            if value == target:
                return source
            if value.startswith(target + "/"):
                return source + value[len(target) :]
        return value

    command = [local_path(value) for value in command]
    if working_directory is not None:
        working_directory = Path(local_path(str(working_directory)))
    if image == "node:20":
        if not command or command[0] not in {"node", "npm"}:
            raise ValueError("node:20에서는 node 또는 npm만 허용합니다")
        return command, working_directory, environment
    if image in OPENAPI_JARS:
        return ["java", "-jar", str(OPENAPI_JARS[image]), *command], working_directory, environment
    if image in GRADLE_IMAGES:
        if command and command[0] == "gradle":
            command = command[1:]
        return [*gradle_command(), *command], working_directory, environment
    raise ValueError(f"runner에서 허용하지 않은 생성 도구 이미지입니다: {image}")


def main(argv: list[str] | None = None) -> int:
    try:
        command, cwd, environment = translate(argv or sys.argv[1:])
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 64
    return subprocess.run(command, cwd=cwd, env=environment, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
