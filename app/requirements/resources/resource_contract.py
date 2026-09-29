"""RESOURCE_SPEC JSON Schema를 읽고 current 요청을 검증한다."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import jsonschema

_SCHEMA_PATH = Path(__file__).with_name("resource_spec.schema.json")


@lru_cache(maxsize=1)
def request_schema() -> dict:
    """고정 RESOURCE_SPEC 요청 schema를 프로세스 cache에서 반환한다."""

    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def validate_request(spec: dict) -> list[str]:
    """요청 spec의 schema·필수 field 오류를 안정된 순서로 반환한다."""

    if not isinstance(spec, dict):
        return [f"[schema] the constraint must be an object (got {type(spec).__name__})"]

    problems: list[str] = []
    validator = jsonschema.Draft202012Validator(request_schema())
    for error in validator.iter_errors(spec):
        if not error.absolute_path and error.validator in ("required", "anyOf"):
            continue
        where = "/".join(str(part) for part in error.absolute_path) or "(top level)"
        problems.append(f"[schema] {where}: {error.message[:140]}")

    for field_name in request_schema().get("required", ()):
        if field_name not in spec:
            problems.append(f"[required] {field_name} missing")
    return problems
