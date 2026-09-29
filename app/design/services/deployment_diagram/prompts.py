"""배포 컴포넌트의 표시 이름만 제안받는 LLM prompt를 소유한다."""

from __future__ import annotations

import json
from typing import Any

DEPLOYMENT_LABEL_SYSTEM_PROMPT = """
Name the supplied deployment components for a software design diagram.

You may change only the human-readable `name` paired with each supplied `id`.
Copy every supplied id exactly once. Do not add or remove components. Do not
choose or describe workloads, virtual machines, replicas, networks, storage,
connections, cloud resources, providers, ports, exposure, or placement. Those
decisions have already been made by deterministic templates.

Use short English product or role names that help a beginner read the diagram.
Return only the response schema. Include no markdown or explanation.
"""


DEPLOYMENT_LABEL_REVISION_SYSTEM_PROMPT = """
Rename existing deployment diagram components from user feedback.

You may change only a component's human-readable name. Copy supplied ids
exactly and never add or remove an id. A request about topology, resources,
networking, storage, replicas, provider products, or placement is outside this
step; keep all names unchanged for such a request. Return only the response
schema with no markdown or explanation.
"""


def generation_messages(payload: dict[str, Any]) -> list[dict[str, str]]:
    """작은 이름 후보 목록과 이름을 정할 문맥만 LLM에 전달한다."""

    return [
        {"role": "system", "content": DEPLOYMENT_LABEL_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)},
    ]


def label_revision_messages(
    components: list[dict[str, str]],
    feedback: str,
    targets: set[str] | None,
) -> list[dict[str, str]]:
    """이름 수정에 실제로 쓰는 컴포넌트·피드백·대상 ID만 전달한다."""

    payload = {
        "components": components,
        "feedback": feedback,
        "targetIds": sorted(targets or ()),
    }
    return [
        {"role": "system", "content": DEPLOYMENT_LABEL_REVISION_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)},
    ]
