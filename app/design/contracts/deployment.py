"""Public deployment contracts for downstream stage consumers."""

from __future__ import annotations

from typing import Any

from app.design.services.deployment_diagram.planner import (
    RESOURCE_PLAN_SCHEMA,
    bind_runtime_contract,
    build_provider_resource_plan,
)
from app.design.services.deployment_diagram.provider_plantuml import (
    deployment_bundle_runtime_puml,
)
from app.design.services.deployment_diagram.provider_template import (
    validate_complete_provider_template,
)


def validate_provider_resource_plan(plan: dict[str, Any]) -> None:
    """Reject a provider ResourcePlan that violates the accepted design contract."""

    validate_complete_provider_template(plan)


__all__ = [
    "RESOURCE_PLAN_SCHEMA",
    "bind_runtime_contract",
    "build_provider_resource_plan",
    "deployment_bundle_runtime_puml",
    "validate_provider_resource_plan",
]
