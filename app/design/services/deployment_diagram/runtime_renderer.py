"""배포 bundle의 runtime placement와 traffic PlantUML을 렌더링한다."""

from __future__ import annotations

from typing import Any

from app.design.services.deployment_diagram.renderer_support import (
    _display_image_reference,
    _fallback,
    _id,
    _instance_label,
    _node_by_kind,
    _persistent_workload,
    _primary,
    _provider_label,
    _render_context,
    _replica_alias,
    _runtime_workload_label,
    _text,
    _workload_alias,
)


def render_runtime_deployment(bundle: dict[str, Any]) -> str:
    """Runtime boundary, placement와 traffic을 PlantUML로 렌더링한다.

    Args:
        bundle: 단일 provider projection을 포함한 deployment bundle이다.

    Returns:
        기존 line 순서와 개행을 유지한 runtime PlantUML 문자열이다.

    Notes:
        provider 대안이 복수이거나 미해결이면 기존 fallback diagram을 반환한다.
    """

    projections = list(bundle.get("projections") or [])
    if not projections:
        return _fallback(bundle)
    projection = _primary(bundle)
    if projection is None:
        return _fallback(
            bundle,
            "Multiple provider alternatives exist. Select one target before rendering a provider-native view.",
        )
    if projection.get("status") not in {"completed", "needsInput"}:
        reasons = "; ".join(
            str(item.get("reason") or "") for item in projection.get("issues") or []
        )
        return _fallback(bundle, reasons or "Provider-native deployment target is unresolved.")

    context = _render_context(bundle)
    plan = dict(context["plan"])
    render_settings = dict(context["settings"])
    provider = str(projection.get("provider") or "")
    region = str(projection.get("region") or "")
    nodes = _node_by_kind(plan)
    workloads = list(plan.get("workloads") or [])
    allocations = {
        str(item.get("workloadRef") or ""): item for item in plan.get("allocations") or []
    }
    selected_zones = list(render_settings.get("selectedZones") or [])
    zone_label = (
        ", ".join(selected_zones)
        if provider == "aws" and selected_zones
        else f"regional: {region}"
        if provider in {"azure", "gcp"}
        else "one selected zone"
    )
    placement_constraints = dict(plan.get("placementConstraints") or {})
    minimum_subnets = int(placement_constraints.get("minimumSubnets") or 1)
    ingress_zones = list(
        placement_constraints.get("selectedIngressZones")
        or render_settings.get("selectedIngressZones")
        or selected_zones
    )
    load_balanced = render_settings.get("publicIngress") == "loadBalanced"
    ingress_name = _text(
        (nodes.get("load-balancer") or nodes.get("forwarding-rule") or {}).get("name")
        or "Public load balancer"
    )

    compute_kind = (
        "compute-group"
        if render_settings.get("computeManagement") == "managedGroup"
        else "compute-instance"
    )
    primary_compute_id = str(plan.get("computeNodeId") or "")
    compute = next(
        (
            node
            for node in plan.get("nodes") or []
            if str(node.get("id") or "") == primary_compute_id
        ),
        nodes.get(compute_kind, {}),
    )
    network = nodes.get("network", {})
    subnet = nodes.get("subnet", {})
    traffic_filters = [
        node
        for node in plan.get("nodes") or []
        if node.get("providerKind")
        in {
            "application-ingress-rule",
            "firewall",
            "load-balancer-security-group",
            "security-group",
        }
    ]
    display_caption = str(render_settings.get("displayCaption") or "")
    has_public_endpoint = any(node.get("group") == "endpoint" for node in plan.get("nodes") or [])

    lines = [
        "@startuml",
        "!theme plain",
        "left to right direction",
        "skinparam shadowing false",
        "skinparam linetype ortho",
        "skinparam nodesep 55",
        "skinparam ranksep 65",
        "skinparam ArrowColor #2f6b50",
        "skinparam node {",
        "  BackgroundColor #ffffff",
        "  BorderColor #68736b",
        "}",
        "skinparam cloud {",
        "  BackgroundColor #f5f7f4",
        "  BorderColor #7f8d83",
        "}",
        (
            f"title Runtime deployment - {_provider_label(provider)} / {_text(region)}"
            + (f"\\n{_text(display_caption)}" if display_caption else "")
        ),
        'actor "Internet client" as internet_client',
        f'cloud "{_provider_label(provider)}" as provider_boundary {{',
    ]
    lines[lines.index("skinparam cloud {") : lines.index("skinparam cloud {")] = [
        "skinparam rectangle {",
        "  BackgroundColor #f8faf8",
        "  BorderColor #68736b",
        "}",
    ]
    if not has_public_endpoint:
        lines.remove('actor "Internet client" as internet_client')
    if provider == "gcp":
        lines.extend(
            [
                f'  node "{_text(network.get("name") or "VPC Network")}\\n<<global>>" as provider_network',
                f'  frame "Region: {_text(region)}" as region_boundary {{',
            ]
        )
    else:
        lines.extend(
            [
                f'  frame "Region: {_text(region)}" as region_boundary {{',
                f'    node "{_text(network.get("name") or "Network")}" as provider_network {{',
            ]
        )
    subnet_aliases: list[str] = []
    if minimum_subnets > 1:
        for index in range(minimum_subnets):
            alias = f"provider_subnet_{index + 1}"
            zone = (
                ingress_zones[index] if index < len(ingress_zones) else f"distinct AZ {index + 1}"
            )
            subnet_aliases.append(alias)
            lines.append(
                f'      node "{_text(subnet.get("name") or "Subnet")} {index + 1}\\n{_text(zone)}" as {alias}'
            )
    else:
        subnet_aliases.append("provider_subnet")
        lines.append(
            f'      node "{_text(subnet.get("name") or "Subnet")}\\n{_text(zone_label)}" as provider_subnet {{'
        )

    compute_name = _text(compute.get("name") or "Compute")
    replicas = int(render_settings.get("replicaCount") or 1)
    managed_group = render_settings.get("computeManagement") == "managedGroup"
    compute_suffix = (
        f"\\nfixed capacity: {replicas}" if managed_group else "\\n1 standalone instance"
    )
    lines.append(f'        node "{compute_name}{compute_suffix}" as primary_compute {{')
    primary_workload_ids = [
        ref
        for ref, allocation in allocations.items()
        if str(allocation.get("computeRef") or "") == primary_compute_id
    ]
    runtime_aliases: dict[str, list[str]] = {}
    if managed_group:
        for replica in range(1, replicas + 1):
            if selected_zones:
                zone = (
                    selected_zones[(replica - 1) % len(selected_zones)]
                    if render_settings.get("zoneLayout") == "multiZoneSpread"
                    else selected_zones[0]
                )
            else:
                zone = "selected zone"
            lines.append(
                f'          node "{_instance_label(provider)} {replica}\\n{_text(zone)}" as primary_instance_{replica} {{'
            )
            for workload in workloads:
                workload_id = str(workload.get("id") or "")
                if workload_id not in primary_workload_ids:
                    continue
                alias = _replica_alias(workload, replica)
                runtime_aliases.setdefault(workload_id, []).append(alias)
                shape = "component"
                label = _runtime_workload_label(
                    plan, workload, fallback="Application", replica=replica
                )
                lines.append(f'            {shape} "{label}" as {alias}')
            lines.append("          }")
    else:
        for workload in workloads:
            workload_id = str(workload.get("id") or "")
            if workload_id not in primary_workload_ids:
                continue
            alias = _workload_alias(workload)
            runtime_aliases.setdefault(workload_id, []).append(alias)
            shape = "component"
            label = _runtime_workload_label(plan, workload, fallback="Application")
            lines.append(f'          {shape} "{label}" as {alias}')
    lines.append("        }")
    external_compute_refs = sorted(
        {
            str(allocation.get("computeRef") or "")
            for allocation in allocations.values()
            if str(allocation.get("computeRef") or "") != primary_compute_id
        }
    )
    primary_subnet_closed = False
    if minimum_subnets == 1 and external_compute_refs:
        lines.append("      }")
        primary_subnet_closed = True
    for compute_ref in external_compute_refs:
        compute_node: dict[str, Any] = next(
            (node for node in plan.get("nodes") or [] if node.get("id") == compute_ref),
            {},
        )
        compute_alias = "compute_" + _id(compute_ref)
        external_policy: dict[str, Any] = next(
            (
                item
                for item in (projection.get("deploymentPlan") or {}).get("computeUnits", [])
                if item.get("id") == compute_ref
            ),
            {},
        )
        external_replicas = int(external_policy.get("replicaCount") or 1)
        external_subnet: dict[str, Any] = next(
            (
                node
                for node in plan.get("nodes") or []
                if node.get("providerKind") == "subnet"
                and str(node.get("id") or "").startswith(f"subnet-{compute_ref}-")
            ),
            {},
        )
        if external_subnet:
            external_zone = str((external_subnet.get("attributes") or {}).get("zone") or "")
            lines.append(
                f'      node "{_text(external_subnet.get("name") or "Application Subnet")}\\n{_text(external_zone or "selected zone")}" as subnet_{_id(compute_ref)} {{'
            )
        external_name = compute_node.get("name") or "Compute"
        external_suffix = (
            f"fixed capacity: {external_replicas}"
            if external_policy.get("kind") == "managedVmGroup"
            else "1 standalone instance"
        )
        lines.append(
            f'        node "{_text(external_name)}\\n{_text(external_suffix)}" as {compute_alias} {{'
        )
        for workload in workloads:
            allocation = allocations.get(str(workload.get("id") or ""), {})
            if str(allocation.get("computeRef") or "") != compute_ref:
                continue
            workload_id = str(workload.get("id") or "")
            alias = _workload_alias(workload)
            runtime_aliases.setdefault(workload_id, []).append(alias)
            shape = "component"
            label = _runtime_workload_label(plan, workload, fallback="State workload")
            lines.append(f'          {shape} "{label}" as {alias}')
        lines.append("        }")
        if external_subnet:
            lines.append("      }")
    if minimum_subnets == 1 and not primary_subnet_closed:
        lines.append("      }")
    disks = [node for node in plan.get("nodes") or [] if node.get("providerKind") == "disk"]
    disk = disks[0] if disks else None
    disk_aliases = {
        str((item.get("attributes") or {}).get("storageRef") or item.get("id")): (
            "persistent_disk"
            if index == 0
            else f"persistent_disk_{_id(str(item.get('id') or index))}"
        )
        for index, item in enumerate(disks)
    }
    public_ip = nodes.get("public-ip")
    if provider == "gcp":
        if public_ip and not load_balanced:
            lines.append(f'    node "{_text(public_ip.get("name"))}" as public_address')
        for item in disks:
            storage_ref = str((item.get("attributes") or {}).get("storageRef") or item.get("id"))
            lines.append(
                f'    database "{_text(item.get("name"))}" as {disk_aliases[storage_ref]} <<zonal; retained>>'
            )
        if load_balanced and nodes.get("backend-group"):
            backend_shape = "rectangle"
            backend_role = " <<backend selection>>"
            lines.append(
                f'    {backend_shape} "{_text(nodes["backend-group"].get("name"))}\\n<<zonal>>" as ingress_backend_group{backend_role}'
            )
        lines.append("  }")
    for traffic_filter in traffic_filters:
        filter_id = str(traffic_filter.get("id") or "policy")
        filter_role = "PostgreSQL" if "postgresql" in filter_id else "Application"
        filter_name = _text(traffic_filter.get("name"))
        display_role = _text(traffic_filter.get("displayRole") or filter_role)
        display_name = (
            filter_name
            if display_role.lower() in filter_name.lower()
            else f"{filter_name}\\n{display_role}"
        )
        policy_shape = "rectangle"
        lines.append(
            f'      {policy_shape} "{display_name}" as traffic_filter_{_id(filter_id)} <<traffic policy>>'
        )
    if load_balanced and provider == "aws":
        routing_shape = "rectangle"
        backend_shape = "rectangle"
        health_shape = "rectangle"
        routing_role = " <<routing policy>>"
        backend_role = " <<backend selection>>"
        minimum_ingress_subnets = int(placement_constraints.get("minimumIngressSubnets") or 1)
        for index in range(minimum_ingress_subnets):
            zone = (
                ingress_zones[index] if index < len(ingress_zones) else f"distinct AZ {index + 1}"
            )
            lines.append(
                f'      node "Ingress Subnet {index + 1}\\n{_text(zone)}" as ingress_subnet_{index + 1}'
            )
        lines.extend(
            [
                f'      node "{ingress_name}" as public_ingress <<TCP ingress>>',
                f'      {routing_shape} "{_text(nodes.get("listener", {}).get("name"))}" as ingress_listener{routing_role}',
                f'      {backend_shape} "{_text(nodes.get("backend-group", {}).get("name"))}" as ingress_backend_group{backend_role}',
                f'      {health_shape} "{_text(nodes.get("health-check", {}).get("name"))}" as ingress_health_check <<health policy>>',
            ]
        )
    elif load_balanced and provider == "azure":
        configuration_shape = "rectangle"
        backend_shape = "rectangle"
        frontend_role = " <<frontend configuration>>"
        routing_role = " <<routing policy>>"
        backend_role = " <<backend selection>>"
        lines.extend(
            [
                f'      node "{ingress_name}" as public_ingress <<TCP ingress>> {{',
                f'        {configuration_shape} "{_text(nodes.get("frontend-ip-config", {}).get("name"))}" as ingress_frontend_ip_config{frontend_role}',
                "      }",
                f'      {configuration_shape} "{_text(nodes.get("routing-rule", {}).get("name"))}" as ingress_routing_rule{routing_role}',
                f'      {backend_shape} "{_text(nodes.get("backend-group", {}).get("name"))}" as ingress_backend_group{backend_role}',
                f'      {configuration_shape} "{_text(nodes.get("health-check", {}).get("name"))}" as ingress_health_check <<health policy>>',
            ]
        )
    if provider != "gcp":
        lines.append("    }")
        if public_ip:
            lines.append(f'    node "{_text(public_ip.get("name"))}" as public_address')
        for item in disks:
            storage_ref = str((item.get("attributes") or {}).get("storageRef") or item.get("id"))
            lines.append(
                f'    database "{_text(item.get("name"))}" as {disk_aliases[storage_ref]} <<retained>>'
            )
        lines.append("  }")
    elif load_balanced:
        managed_shape = "rectangle"
        backend_role = " <<backend selection>>"
        lines.extend(
            [
                f'  node "{_text(public_ip.get("name") if public_ip else "Regional External IP Address")}\\n<<regional>>" as public_address',
                f'  node "{ingress_name}\\n<<regional>>" as public_ingress <<TCP ingress>>',
                f'  {managed_shape} "{_text(nodes.get("backend-service", {}).get("name"))}\\n<<regional>>" as ingress_backend_service{backend_role}',
                f'  {managed_shape} "{_text(nodes.get("health-check", {}).get("name"))}\\n<<regional>>" as ingress_health_check <<health policy>>',
            ]
        )
    lines.extend(["}", ""])
    generated_image_aliases: dict[str, str] = {}
    prebuilt_image_aliases: dict[str, str] = {}
    secret_binding_aliases: dict[str, list[str]] = {}
    secret_identity_aliases: dict[str, str] = {}
    if workloads:
        generated_workloads = [
            workload
            for workload in workloads
            if (workload.get("artifact") or {}).get("kind") == "generatedApplication"
        ]
        prebuilt_workloads = [
            workload
            for workload in workloads
            if (workload.get("artifact") or {}).get("kind") == "prebuiltImage"
        ]
        if generated_workloads:
            lines.extend(
                [
                    'actor "User-run easydep.ps1\\n<<local CSP authentication>>" as user_deploy',
                    'frame "Application delivery dependencies" as delivery_dependencies {',
                    '  cloud "VM outbound path\\n(public address or managed NAT)" as workload_outbound_dependency',
                ]
            )
            for workload in generated_workloads:
                workload_id = str(workload.get("id") or "application")
                alias_id = _id(workload_id)
                registry_node: dict[str, Any] = next(
                    (
                        node
                        for node in plan.get("nodes") or []
                        if node.get("id") == f"registry-{workload_id}"
                    ),
                    {},
                )
                compute_ref = str(allocations.get(workload_id, {}).get("computeRef") or "")
                identity_node: dict[str, Any] = next(
                    (
                        node
                        for node in plan.get("nodes") or []
                        if node.get("id") == f"registry-pull-identity-{compute_ref}"
                    ),
                    {},
                )
                source_alias = f"application_source_artifact_{alias_id}"
                registry_alias = f"application_registry_dependency_{alias_id}"
                identity_alias = f"application_identity_dependency_{alias_id}"
                if identity_node.get("id"):
                    secret_identity_aliases[str(identity_node.get("id"))] = identity_alias
                image_alias = f"application_image_dependency_{alias_id}"
                generated_image_aliases[workload_id] = image_alias
                lines.extend(
                    [
                        f'  node "{_text(registry_node.get("name") or "Container Registry")}" as {registry_alias}',
                        f'  node "{_text(identity_node.get("name") or "VM Registry Pull Identity")}" as {identity_alias}',
                        f'  artifact "{_text(workload.get("name") or workload_id)} image source" as {source_alias}',
                        f'  artifact "{_text(workload.get("name") or workload_id)} image@sha256\\n<<immutable digest>>" as {image_alias}',
                    ]
                )
            lines.append("}")
            for workload_id, image_alias in generated_image_aliases.items():
                alias_id = _id(workload_id)
                lines.extend(
                    [
                        f"user_deploy -[#2f6b50]-> application_registry_dependency_{alias_id} : create",
                        f"user_deploy -[#2f6b50]-> {image_alias} : push",
                        f"application_registry_dependency_{alias_id} -[#2f6b50]-> {image_alias}",
                        f"application_identity_dependency_{alias_id} ..[#8a6d3b]> {image_alias} : pull",
                        f"application_source_artifact_{alias_id} -[#2f6b50]-> {image_alias} : build",
                    ]
                )
        if prebuilt_workloads:
            lines.append('frame "Prebuilt image dependencies" as prebuilt_dependencies {')
            for workload in prebuilt_workloads:
                workload_id = str(workload.get("id") or "workload")
                image_alias = f"prebuilt_image_dependency_{_id(workload_id)}"
                prebuilt_image_aliases[workload_id] = image_alias
                image = _display_image_reference((workload.get("artifact") or {}).get("image"))
                lines.append(
                    f'  artifact "{_text(image)}\\n<<explicit prebuilt image; immutable digest>>" as {image_alias}'
                )
            lines.append("}")
        secret_nodes = [
            node for node in plan.get("nodes") or [] if node.get("providerKind") == "secret-ref"
        ]
        binding_nodes = [
            node
            for node in plan.get("nodes") or []
            if node.get("providerKind") in {"secret-access-binding", "state-secret-access-binding"}
        ]
        identity_nodes = [
            node
            for node in plan.get("nodes") or []
            if node.get("providerKind") in {"state-secret-identity", "secret-identity"}
        ]
        if secret_nodes or binding_nodes or identity_nodes:
            lines.append('frame "Secret runtime dependencies" as secret_dependencies {')
            for node in secret_nodes:
                lines.append(
                    f'  artifact "{_text(node.get("name") or "Existing provider Secret")}" as secret_{_id(str(node.get("id") or "secret"))}'
                )
            for node in binding_nodes:
                node_id = str(node.get("id") or "binding")
                alias = f"secret_binding_{_id(node_id)}"
                lines.append(
                    f'  node "{_text(node.get("name") or "Secret read binding")}" as {alias}'
                )
                for workload in workloads:
                    workload_id = str(workload.get("id") or "")
                    if f"-{workload_id}-" in node_id:
                        secret_binding_aliases.setdefault(workload_id, []).append(alias)
            for node in identity_nodes:
                node_id = str(node.get("id") or "identity")
                alias = f"secret_identity_{_id(node_id)}"
                secret_identity_aliases[node_id] = alias
                lines.append(
                    f'  node "{_text(node.get("name") or "Runtime Secret identity")}" as {alias}'
                )
            lines.append("}")
    if provider == "gcp":
        lines.append("provider_network ..[#6f7c73]> provider_subnet : contains regional subnetwork")

    if minimum_subnets > 1:
        for replica in range(1, replicas + 1):
            subnet_index = (
                (replica - 1) % len(subnet_aliases)
                if render_settings.get("zoneLayout") == "multiZoneSpread"
                else 0
            )
            target_alias = f"primary_instance_{replica}" if managed_group else "primary_compute"
            lines.append(f"{subnet_aliases[subnet_index]} ..[#6f7c73]> {target_alias}")
            if not managed_group:
                break
    if load_balanced and provider == "aws":
        minimum_ingress_subnets = int(placement_constraints.get("minimumIngressSubnets") or 1)
        for index in range(minimum_ingress_subnets):
            lines.append(f"ingress_subnet_{index + 1} ..[#6f7c73]> public_ingress")

    endpoint = next(
        (node for node in plan.get("nodes") or [] if node.get("group") == "endpoint"),
        None,
    )
    endpoint_alias = "public_endpoint"
    if endpoint:
        lines.append(
            f'interface "{_text(endpoint.get("name") or "Public HTTP endpoint")}" as {endpoint_alias}'
        )
        lines.append(f"internet_client -[#2f6b50]-> {endpoint_alias} : HTTP")
    if endpoint and public_ip:
        lines.append(f"{endpoint_alias} -[#2f6b50]-> public_address")
    elif endpoint and load_balanced:
        lines.append(f"{endpoint_alias} -[#2f6b50]-> public_ingress")
    if load_balanced and public_ip and provider != "azure":
        lines.append("public_address -[#2f6b50]-> public_ingress")
    primary_application = next(
        (
            item
            for item in workloads
            if str(item.get("id") or "") in primary_workload_ids
            and not _persistent_workload(item)
        ),
        None,
    )
    if primary_application:
        application_aliases = runtime_aliases.get(str(primary_application.get("id") or ""), [])
        if load_balanced:
            if provider == "aws":
                lines.extend(
                    [
                        "public_ingress -[#2f6b50]-> ingress_listener : TCP :80",
                        "ingress_listener -[#2f6b50]-> ingress_backend_group",
                    ]
                )
                backend_alias = "ingress_backend_group"
            elif provider == "azure":
                lines.extend(
                    [
                        "public_address -[#2f6b50]-> ingress_frontend_ip_config : assigned frontend address",
                        "ingress_frontend_ip_config -[#2f6b50]-> ingress_routing_rule : TCP :80",
                        "ingress_routing_rule -[#2f6b50]-> ingress_backend_group",
                    ]
                )
                backend_alias = "ingress_backend_group"
            else:
                lines.extend(
                    [
                        "public_ingress -[#2f6b50]-> ingress_backend_service : TCP :80",
                    ]
                )
                backend_alias = "primary_compute" if managed_group else "ingress_backend_group"
                lines.append(f"ingress_backend_service -[#2f6b50]-> {backend_alias}")
            for alias in application_aliases:
                lines.append(f"{backend_alias} -[#2f6b50]-> {alias}")
                lines.append(f"ingress_health_check ..[#8a6d3b]> {alias}")
        elif public_ip and application_aliases:
            lines.append(f"public_address -[#2f6b50]-> {application_aliases[0]} : HTTP")
    for edge in plan.get("edges") or []:
        if "design-connection" not in edge.get("evidence", []):
            continue
        source = next((item for item in workloads if item.get("id") == edge.get("from")), None)
        target = next((item for item in workloads if item.get("id") == edge.get("to")), None)
        if source and target:
            connection_label = str(edge.get("label") or "connects").upper()
            source_aliases = runtime_aliases.get(str(source.get("id") or ""), [])
            target_aliases = runtime_aliases.get(str(target.get("id") or ""), [])
            for source_index, source_alias in enumerate(source_aliases):
                for target_index, target_alias in enumerate(target_aliases):
                    label = (
                        f" : {_text(connection_label)}"
                        if source_index == 0 and target_index == 0
                        else ""
                    )
                    lines.append(f"{source_alias} -[#2f6b50]-> {target_alias}{label}")
    if workloads:
        for workload in workloads:
            workload_id = str(workload.get("id") or "")
            aliases = runtime_aliases.get(workload_id, [])
            generated_image_alias = generated_image_aliases.get(workload_id)
            prebuilt_alias = prebuilt_image_aliases.get(workload_id)
            for alias_index, alias in enumerate(aliases):
                if generated_image_alias:
                    lines.append(f"{generated_image_alias} -[#2f6b50]-> {alias}")
                    if alias_index == 0:
                        lines.append(f"workload_outbound_dependency ..[#8a6d3b]> {alias}")
                if prebuilt_alias:
                    lines.append(f"{prebuilt_alias} -[#2f6b50]-> {alias}")
                for binding in plan.get("runtimeBindings") or []:
                    if (
                        binding.get("kind") != "secretEnvironment"
                        or str(binding.get("workloadRef") or "") != workload_id
                    ):
                        continue
                    grant_id = (
                        f"secret-access-binding-{workload_id}-{binding.get('configurationRef')}"
                    )
                    identity_ref = next(
                        (
                            str(reference.get("producerRef") or "")
                            for reference in plan.get("references") or []
                            if reference.get("consumerRef") == grant_id
                            and str(reference.get("producerRef") or "") in secret_identity_aliases
                        ),
                        "",
                    )
                    secret_identity_alias = secret_identity_aliases.get(identity_ref)
                    secret_node_id = f"secret-ref-{workload_id}-{binding.get('configurationRef')}"
                    secret_alias = f"secret_{_id(secret_node_id)}"
                    if secret_identity_alias:
                        lines.append(
                            f"{secret_alias} --[#8a6d3b] {secret_identity_alias} : read permission"
                        )
                        lines.append(f"{secret_identity_alias} ..[#8a6d3b]> {alias} : inject")
    for traffic_filter in traffic_filters:
        filter_id = str(traffic_filter.get("id") or "policy")
        provider_kind = str(traffic_filter.get("providerKind") or "")
        if provider == "aws" and provider_kind == "load-balancer-security-group":
            lines.append(f"traffic_filter_{_id(filter_id)} ..[#8a6d3b]> public_ingress")
            continue
        if provider == "aws" and provider_kind == "application-ingress-rule":
            lines.append(
                "traffic_filter_load_balancer_security_group ..[#8a6d3b]> "
                f"traffic_filter_{_id(filter_id)}"
            )
            lines.append(
                f"traffic_filter_{_id(filter_id)} ..[#8a6d3b]> "
                "traffic_filter_security_group"
            )
            continue
        logical_ref = str(traffic_filter.get("logicalRef") or "")
        target_workload_ids = (
            [logical_ref]
            if logical_ref in runtime_aliases
            else [
                workload_id
                for workload_id, allocation in allocations.items()
                if allocation.get("computeRef") == logical_ref
            ]
        )
        targets = [
            alias
            for workload_id in target_workload_ids
            for alias in runtime_aliases.get(workload_id, [])
        ]
        for target_alias in targets:
            lines.append(f"traffic_filter_{_id(filter_id)} ..[#8a6d3b]> {target_alias}")
    if disk:
        for storage_binding in plan.get("storageBindings") or []:
            storage_ref = str(storage_binding.get("storageRef") or "")
            disk_alias = disk_aliases.get(storage_ref)
            if not disk_alias:
                continue
            owner_workload = str(storage_binding.get("workloadRef") or "")
            for owner_alias in runtime_aliases.get(owner_workload, []):
                lines.append(f"{disk_alias} ..[#6f7c73]> {owner_alias}")
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
            "  |= Line |= Meaning |",
            "  | <color:#2f6b50>solid arrow</color> | runtime request or data flow |",
            "  | <color:#8a6d3b>dotted arrow</color> | health or traffic policy configuration |",
            "  | <color:#6f7c73>dotted arrow</color> | placement or attachment |",
            "endlegend",
            "@enduml",
        ]
    )
    return "\n".join(lines)
