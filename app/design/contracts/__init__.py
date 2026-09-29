"""Public typed contracts exposed by the design stage.

Deployment contracts depend on runtime topology, which in turn consumes the
class-model schema.  Keep their public exports lazy so importing a low-level
schema does not initialise the deployment stack.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "RESOURCE_PLAN_SCHEMA",
    "bind_runtime_contract",
    "build_provider_resource_plan",
    "deployment_bundle_runtime_puml",
    "validate_provider_resource_plan",
]


def __getattr__(name: str) -> Any:
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module("app.design.contracts.deployment"), name)
    globals()[name] = value
    return value
