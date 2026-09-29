"""배포 bundle의 provisioning dependency PlantUML을 렌더링한다."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, cast

from app.design.services.deployment_diagram.renderer_support import (
    _FOLDED_ASSOCIATION_LABELS,
    _FOLDED_RELATION_KINDS,
    _compact_reference_role,
    _fallback,
    _id,
    _primary,
    _provider_label,
    _render_context,
    _text,
)


def render_provisioning_dependencies(bundle: dict[str, Any]) -> str:
    """Runtime traffic을 제외한 IaC dependency를 PlantUML로 렌더링한다.

    Args:
        bundle: 단일 provider projection을 포함한 deployment bundle이다.

    Returns:
        기존 line 순서와 개행을 유지한 provisioning PlantUML 문자열이다.

    Notes:
        ResourcePlan reference와 association만 표시하며 생성 순서를 새로 추론하지 않는다.
    """

    projection = _primary(bundle)
    if projection is None or projection.get("status") not in {
        "completed",
        "needsInput",
    }:
        return _fallback(bundle, "Provisioning dependencies require one resolved provider target.")
    context = _render_context(bundle)
    plan = dict(context["plan"])
    render_settings = dict(context["settings"])
    provider = str(projection.get("provider") or "")
    region = str(projection.get("region") or "")
    display_caption = str(render_settings.get("displayCaption") or "")
    placement_constraints = dict(plan.get("placementConstraints") or {})
    ingress_zones = list(
        placement_constraints.get("selectedIngressZones")
        or render_settings.get("selectedIngressZones")
        or render_settings.get("selectedZones")
        or []
    )
    projected_nodes = {
        str(node.get("id") or ""): node
        for node in plan.get("nodes") or []
        if node.get("entityClass")
        in {
            "providerResource",
            "providerComponent",
            "externalArtifact",
            "sharedValue",
            "embeddedBlock",
        }
    }
    folded = {
        node_id: node
        for node_id, node in projected_nodes.items()
        if str(node.get("providerKind") or "") in _FOLDED_RELATION_KINDS.get(provider, set())
    }
    included = {node_id: node for node_id, node in projected_nodes.items() if node_id not in folded}
    embedded_by_owner: dict[str, list[dict[str, Any]]] = {}
    for node in included.values():
        if node.get("entityClass") == "embeddedBlock":
            embedded_by_owner.setdefault(str(node.get("ownerRef") or ""), []).append(node)
    lines = [
        "@startuml",
        "!theme plain",
        "top to bottom direction",
        "skinparam shadowing false",
        "skinparam linetype polyline",
        "skinparam nodesep 12",
        (
            f"title Provisioning dependencies - {_provider_label(provider)} / {_text(region)}"
            + (f"\\n{_text(display_caption)}" if display_caption else "")
        ),
    ]
    provision_aliases: dict[str, list[str]] = {}
    display_name_counts: dict[str, int] = {}
    for node in included.values():
        name = _text(node.get("name"))
        display_name_counts[name] = display_name_counts.get(name, 0) + 1
    for node_id, node in sorted(included.items()):
        if node.get("entityClass") == "embeddedBlock":
            continue
        handling = str(node.get("handling") or "create")
        stereotype = "reference" if handling == "referenceExisting" else handling
        node_name = _text(node.get("name") or node_id)
        if node_id == "compute-group":
            replica_count = int(render_settings.get("replicaCount") or 1)
            zones = list(render_settings.get("selectedZones") or [])
            placement = ", ".join(zones) if zones else "selected zone"
            node_name = (
                f"{node_name}\\ndesired capacity: {replica_count}\\nplacement: {_text(placement)}"
            )
        if display_name_counts.get(node_name, 0) > 1:
            role = _text(node.get("displayRole") or node_id)
            node_name = f"{node_name}\\n{role}"
        minimum_count = int(node.get("minimumCount") or 1)
        if minimum_count > 1:
            aliases: list[str] = []
            node_zones = (
                ingress_zones
                if node_id in {"ingress-subnet", "ingress-route-association"}
                else list(render_settings.get("selectedZones") or [])
            )
            for index in range(minimum_count):
                alias = f"provision_{_id(node_id)}_{index + 1}"
                zone = node_zones[index] if index < len(node_zones) else f"distinct AZ {index + 1}"
                aliases.append(alias)
                lines.append(
                    f'node "{node_name} {index + 1}\\n{_text(zone)}" as {alias} <<{_text(stereotype)}>>'
                )
            provision_aliases[node_id] = aliases
        else:
            alias = f"provision_{_id(node_id)}"
            provision_aliases[node_id] = [alias]
            if node.get("entityClass") == "sharedValue":
                lines.append(f'rectangle "{node_name}" as {alias} <<shared value>>')
            elif embedded_by_owner.get(node_id):
                lines.append(f'node "{node_name}" as {alias} <<{_text(stereotype)}>> {{')
                for block in sorted(
                    embedded_by_owner[node_id], key=lambda item: str(item.get("id") or "")
                ):
                    block_id = str(block.get("id") or "")
                    block_alias = f"provision_{_id(block_id)}"
                    provision_aliases[block_id] = [block_alias]
                    lines.append(
                        f'  rectangle "{_text(block.get("name") or block_id)}" '
                        f"as {block_alias} <<inline block>>"
                    )
                lines.append("}")
            else:
                lines.append(f'node "{node_name}" as {alias} <<{_text(stereotype)}>>')
    visible_edges = [
        edge
        for edge in plan.get("edges") or []
        if str(edge.get("from") or "") in included and str(edge.get("to") or "") in included
    ]
    endpoint_counts: dict[tuple[str, str], int] = {}
    for edge in visible_edges:
        endpoints = (str(edge.get("from") or ""), str(edge.get("to") or ""))
        endpoint_counts[endpoints] = endpoint_counts.get(endpoints, 0) + 1
    for edge in visible_edges:
        source = str(edge.get("from") or "")
        target = str(edge.get("to") or "")
        label = str(edge.get("label") or "depends on")
        relationship = (
            _compact_reference_role(edge.get("consumerPath"))
            if endpoint_counts[(source, target)] > 1
            else ""
        )
        dependent_aliases = provision_aliases[source]
        prerequisite_aliases = provision_aliases[target]
        if target == "ingress-subnet" and source == "nat-gateway":
            prerequisite_aliases = prerequisite_aliases[:1]
        pairs: list[tuple[str, str]]
        if (
            label == "binds"
            and len(prerequisite_aliases) == len(dependent_aliases)
            and len(prerequisite_aliases) > 1
        ):
            pairs = list(zip(dependent_aliases, prerequisite_aliases, strict=True))
        else:
            pairs = [
                (dependent_alias, prerequisite_alias)
                for dependent_alias in dependent_aliases
                for prerequisite_alias in prerequisite_aliases
            ]
        for dependent_alias, prerequisite_alias in pairs:
            suffix = f" : {_text(relationship)}" if relationship else ""
            lines.append(f"{dependent_alias} -[#6f7780,dashed]-> {prerequisite_alias}{suffix}")
    folded_lines: set[tuple[str, str, str]] = set()
    for relation_id, relation_node in sorted(folded.items()):
        neighbors: list[str] = []
        principal = ""
        for edge in plan.get("edges") or []:
            source = str(edge.get("from") or "")
            target = str(edge.get("to") or "")
            label = str(edge.get("label") or "")
            if source == relation_id and target in included:
                if label == "is deployed in":
                    continue
                neighbors.append(target)
                if label in {"grants pull access to", "grants secret read to"}:
                    principal = target
            elif target == relation_id and source in included:
                neighbors.append(source)
        neighbors = list(dict.fromkeys(neighbors))
        if len(neighbors) < 2:
            continue
        anchor = principal if principal in neighbors else neighbors[0]
        relation_label = _FOLDED_ASSOCIATION_LABELS.get(
            str(relation_node.get("providerKind") or ""), "associated"
        )
        for other in neighbors:
            if other == anchor:
                continue
            anchor_aliases = provision_aliases.get(anchor, [])
            other_aliases = provision_aliases.get(other, [])
            if len(anchor_aliases) == len(other_aliases) and len(anchor_aliases) > 1:
                relation_pairs: Iterable[tuple[str, str]] = zip(
                    anchor_aliases, other_aliases, strict=True
                )
            else:
                relation_pairs = (
                    (anchor_alias, other_alias)
                    for anchor_alias in anchor_aliases
                    for other_alias in other_aliases
                )
            for anchor_alias, other_alias in relation_pairs:
                key = cast(
                    tuple[str, str, str],
                    tuple(sorted((anchor_alias, other_alias))) + (relation_label,),
                )
                if key in folded_lines:
                    continue
                folded_lines.add(key)
                lines.append(
                    f"{anchor_alias} -[#c47713,dashed]- {other_alias} : {_text(relation_label)}"
                )
    if plan.get("unresolved"):
        lines.extend(
            [
                "note bottom",
                "  Deployment inputs remain unresolved; IaC promotion is blocked.",
                "end note",
            ]
        )
    lines.extend(
        [
            "legend bottom",
            "  Arrow: dependent -> prerequisite.",
            "  Arrow labels appear only when duplicate references need disambiguation.",
            "  Undirected line: Terraform association, attachment, permission, or route.",
            "  Shared value: one Terraform local consumed by multiple fields.",
            "  Runtime traffic is intentionally omitted.",
            "endlegend",
            "@enduml",
        ]
    )
    return "\n".join(lines)
