"""Public syntax contract shared by accepted sequence artifacts and consumers."""

from __future__ import annotations

import re

from .type_system import DesignTypeError, parse_type_expression

_METHOD_CALL = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\([^()\r\n]*\)$")
_METHOD_NAME = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)")


def is_complete_method_call(label: str) -> bool:
    return bool(_METHOD_CALL.fullmatch(label.strip()))


def method_call_signature(raw: str) -> str:
    raw = re.sub(r"^[+\-#~]\s*", "", raw.strip())
    match = re.match(r"([A-Za-z_][A-Za-z0-9_]*\([^()\r\n]*\))", raw)
    return re.sub(r"\s+", "", match.group(1)) if match else ""


def method_name(raw: str) -> str:
    raw = re.sub(r"^[+\-#~]\s*", "", raw.strip())
    match = _METHOD_NAME.match(raw)
    return match.group(1).lower() if match else ""


def method_return_type(raw: str) -> str | None:
    raw = re.sub(r"^[+\-#~]\s*", "", raw.strip())
    match = re.match(r"[A-Za-z_][A-Za-z0-9_]*\([^()]*\)\s*:\s*(.+)$", raw)
    return match.group(1).strip() if match else None


def is_return_value_label(label: str) -> bool:
    try:
        parse_type_expression(label)
    except DesignTypeError:
        return False
    return True


def normalize_return_type(raw: str) -> str:
    return re.sub(r"\s+", "", raw).lower()
