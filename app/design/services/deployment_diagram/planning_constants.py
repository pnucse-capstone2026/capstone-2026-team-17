"""배포 계획 단계가 공유하는 schema와 검증 상수를 정의한다."""

from __future__ import annotations

import re

WORKLOAD_GRAPH_SCHEMA = "easydep-workload-graph"
DEPLOYMENT_PLAN_SCHEMA = "easydep-deployment-plan"
RESOURCE_PLAN_SCHEMA = "easydep-resource-plan"
RUNTIME_BINDING_SCHEMA = "easydep-runtime-binding"

SUPPORTED_PROVIDERS = frozenset({"aws", "azure", "gcp"})
SUPPORTED_PROTOCOLS = frozenset({"http", "tcp"})
SUPPORTED_PREBUILT_RUNTIME_CATALOG = frozenset({"docker-on-vm/prebuilt-image"})
BLOCKING_CLASSES = frozenset(
    {
        "invalid",
        "unsupported",
        "needsInput",
        "unjustified",
    }
)
ENVIRONMENT_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
