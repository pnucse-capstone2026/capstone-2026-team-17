"""Testing gate vocabulary and deterministic aggregation.

Legacy reports in the repository use ``PASSED``, ``FAILED`` and ``SKIPPED``.  The
testing API keeps those strings for clients that already consume them, while the
``gateStatus`` field and these helpers expose the unambiguous four-state contract.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Literal

GateStatus = Literal["PASS", "FAIL", "INCONCLUSIVE", "NOT_APPLICABLE"]
GATE_STATUSES = frozenset({"PASS", "FAIL", "INCONCLUSIVE", "NOT_APPLICABLE"})


def normalize_gate_status(
    value: Any, *, required: bool = True, default: GateStatus = "INCONCLUSIVE"
) -> GateStatus:
    """Normalize report/status values without treating unavailable as success."""
    if isinstance(value, Mapping):
        if "gateStatus" in value:
            value = value.get("gateStatus")
        elif "gate_status" in value:
            value = value.get("gate_status")
        else:
            value = value.get("status")
    text = str(value or "").strip().upper()
    if text in GATE_STATUSES:
        if text == "INCONCLUSIVE" and not required:
            return "NOT_APPLICABLE"
        return text  # type: ignore[return-value]
    if text in {"PASSED", "PASS", "OK", "COMPLETED", "SUCCESS"}:
        return "PASS"
    if text in {"FAILED", "FAIL", "ERROR", "INVALID"}:
        return "FAIL"
    if text in {"SKIPPED", "NOT_APPLICABLE", "NOT APPLICABLE", "N/A", "NA"}:
        return "NOT_APPLICABLE"
    if text in {"UNAVAILABLE", "TIMEOUT", "INCONCLUSIVE", "UNKNOWN", "PENDING"}:
        return "INCONCLUSIVE" if required else "NOT_APPLICABLE"
    return default if required else "NOT_APPLICABLE"


def gate_status(report: Mapping[str, Any] | Any, *, required: bool = True) -> GateStatus:
    """Return the canonical gate status for a report."""
    return normalize_gate_status(report, required=required)


def aggregate_gate_status(
    reports: Mapping[str, Any] | Iterable[Any],
    *,
    required: Mapping[str, bool] | Iterable[str] | None = None,
) -> GateStatus:
    """Aggregate gates with ``INCONCLUSIVE`` taking precedence over failure.

    A mandatory tool that was not run makes the complete gate inconclusive. This
    is intentionally stricter than checking only a ``passed`` boolean.
    """
    if isinstance(reports, Mapping):
        items = list(reports.items())
    else:
        items = [(str(index), value) for index, value in enumerate(reports)]
    required_map: dict[str, bool] = {}
    if isinstance(required, Mapping):
        required_map.update({str(key): bool(value) for key, value in required.items()})
    elif required is not None:
        required_map.update({str(key): True for key in required})

    statuses: list[GateStatus] = []
    for key, report in items:
        is_required = required_map.get(str(key), True)
        statuses.append(gate_status(report, required=is_required))
    if any(status == "INCONCLUSIVE" for status in statuses):
        return "INCONCLUSIVE"
    if any(status == "FAIL" for status in statuses):
        return "FAIL"
    if statuses and all(status == "NOT_APPLICABLE" for status in statuses):
        return "NOT_APPLICABLE"
    return "PASS" if statuses else "INCONCLUSIVE"


def aggregate_gate_report(
    reports: Mapping[str, Any], *, required: Mapping[str, bool] | None = None
) -> dict[str, Any]:
    """Build a JSON-safe aggregate report used by verification and HTTP callers."""
    required_map = dict(required or {})
    normalized = {
        str(key): gate_status(value, required=required_map.get(str(key), True))
        for key, value in reports.items()
    }
    status = aggregate_gate_status(reports, required=required_map)
    counts = {name: sum(value == name for value in normalized.values()) for name in GATE_STATUSES}
    return {
        "status": status,
        "gateStatus": status,
        "passed": status == "PASS",
        "gates": normalized,
        "counts": counts,
    }


aggregate_gates = aggregate_gate_status
aggregate_results = aggregate_gate_report
