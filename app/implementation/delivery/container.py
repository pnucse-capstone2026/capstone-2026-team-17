"""검토된 배포 입력에서 Docker 실행 파일과 배포 보고서를 생성한다."""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from app.implementation.config import (
    DEFAULT_CONTAINER_PORT,
    DEFAULT_DOCKER_GRADLE_IMAGE,
    DEFAULT_DOCKER_JRE_IMAGE,
)

SCHEMA_VERSION = "easydep-deployment-intent/v1alpha1"


def application_dockerfile(*, include_frontend: bool = True) -> str:
    """Spring Boot 애플리케이션용 다단계 Dockerfile을 반환한다."""
    frontend_stage = ""
    frontend_copy = ""
    if include_frontend:
        frontend_stage = """FROM node:20-alpine AS frontend-build
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund \\
    && test -x node_modules/.bin/tsc \\
    && test -x node_modules/.bin/vite
COPY frontend/ ./
ARG VITE_API_BASE_URL=""
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
RUN npm run build

"""
        frontend_copy = (
            "COPY --from=frontend-build /app/frontend/dist/ "
            "src/main/resources/static/\n"
        )
    return f"""{frontend_stage}FROM {DEFAULT_DOCKER_GRADLE_IMAGE} AS build
WORKDIR /app
COPY . .
{frontend_copy}RUN gradle bootJar --no-daemon \\
    && jar="$(find build/libs -maxdepth 1 -type f -name '*.jar' ! -name '*-plain.jar' -print | sort | head -n 1)" \\
    && test -n "$jar" \\
    && cp "$jar" /tmp/app.jar

FROM {DEFAULT_DOCKER_JRE_IMAGE}
WORKDIR /app
# Use the same numeric identity as the VM bootstrap when it owns persistent paths.
RUN addgroup -S -g 10001 app && adduser -S -D -H -u 10001 -G app app
COPY --chown=10001:10001 --from=build /tmp/app.jar app.jar
USER 10001:10001
# Keep the Spring Boot listen port aligned with the container's published port.
ENV SERVER_PORT={DEFAULT_CONTAINER_PORT}
EXPOSE {DEFAULT_CONTAINER_PORT}
ENTRYPOINT ["java", "-jar", "app.jar"]"""


def dockerignore() -> str:
    """Docker 빌드에 들어가면 안 되는 로컬 파일 목록을 반환한다."""
    # 다운로드 API가 만드는 manifest는 산출물 버전만 기록한다. 애플리케이션 소스가
    # 그대로여도 배포 파일을 새로 만들 때마다 바뀌므로 빌드 입력에서는 제외한다.
    return """.git
.gradle
build
reports
deployment-bundle
/deployment
/manifest.json
.env
.env.*
*.pem
*.key"""


def frontend_dockerfile() -> str:
    """프런트엔드를 별도 nginx 컨테이너로 실행하는 Dockerfile을 반환한다."""
    return """FROM node:20-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund \\
    && test -x node_modules/.bin/tsc \\
    && test -x node_modules/.bin/vite
COPY . .
ARG VITE_API_BASE_URL
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
RUN test -n "$VITE_API_BASE_URL" && npm run build

FROM nginxinc/nginx-unprivileged:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist/ /usr/share/nginx/html/
EXPOSE 8080"""


def frontend_nginx_config() -> str:
    """프런트엔드의 단일 페이지 라우팅을 지원하는 nginx 설정을 반환한다."""
    return """server {
  listen 8080;
  server_name _;
  root /usr/share/nginx/html;
  index index.html;
  location / { try_files $uri $uri/ /index.html; }
}"""


def render_local_container(run_root: Path) -> dict[str, object]:
    """클라우드 입력이 없어도 로컬에서 실행할 Docker 파일을 만든다."""
    application = run_root / "application"
    frontend = application / "frontend" / "package.json"
    application.mkdir(parents=True, exist_ok=True)
    (application / "Dockerfile").write_text(
        application_dockerfile(
            include_frontend=frontend.is_file(),
        )
        + "\n",
        encoding="utf-8",
    )
    (application / ".dockerignore").write_text(
        dockerignore() + "\n", encoding="utf-8"
    )
    return {
        "schemaVersion": "easydep-local-container/v1alpha1",
        "frontendMode": "integrated" if frontend.is_file() else "absent",
        "renderedFiles": ["application/.dockerignore", "application/Dockerfile"],
    }


def render_deployment(run_root: Path, spec: Any) -> dict[str, object]:
    """배포 입력을 읽어 Docker 파일과 두 개의 배포 보고서를 만든다."""
    cloud_path = spec.inputs.get("cloud")
    deployment_path = spec.inputs.get("deployment")
    intent_path = spec.inputs.get("deploymentIntent")
    has_intent = bool(intent_path and intent_path.is_file())
    if not has_intent and (cloud_path is None or not cloud_path.is_file()):
        raise ValueError(
            "Deployment intent inference requires a cloud resource specification"
        )
    cloud = (
        json.loads(cloud_path.read_text(encoding="utf-8"))
        if cloud_path and cloud_path.is_file()
        else {}
    )
    deployment = (
        deployment_path.read_text(encoding="utf-8")
        if deployment_path and deployment_path.is_file()
        else ""
    )
    intent = (
        json.loads(intent_path.read_text(encoding="utf-8"))
        if has_intent
        else infer_intent(spec.name, cloud, deployment)
    )
    validate_intent(intent)

    application = run_root / "application"
    reports = run_root / "reports"
    removed = remove_previous_render(application, reports)
    rendered: list[str] = []

    def write(relative: str, content: str) -> None:
        path = application / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content.rstrip() + "\n", encoding="utf-8")
        rendered_path = f"application/{relative}"
        if rendered_path not in rendered:
            rendered.append(rendered_path)

    separate_frontend = any(
        workload.get("artifact") == "frontend" for workload in intent["workloads"]
    )
    write(
        "Dockerfile",
        application_dockerfile(
            include_frontend=(application / "frontend/package.json").is_file()
            and not separate_frontend,
        ),
    )
    write(".dockerignore", dockerignore())
    if separate_frontend:
        write("frontend/Dockerfile", frontend_dockerfile())
        write("frontend/nginx.conf", frontend_nginx_config())
        write("frontend/.dockerignore", "node_modules\ndist\n.env*\n")

    # 예전 실행이 Kubernetes 파일을 남겼더라도 현재 Docker 배포에 섞이지 않게 한다.
    k8s_root = application / "k8s"
    if k8s_root.is_dir():
        shutil.rmtree(k8s_root)

    report = {
        "schemaVersion": "easydep-deployment-render/v1alpha1",
        "intent": intent,
        "renderedFiles": sorted(rendered),
        "removedFiles": sorted(removed),
        "renderer": "deterministic",
        "kubernetesManifests": False,
        "validation": {
            "status": "SKIPPED",
            "reason": "Kubernetes manifest generation is disabled for implementation releases.",
        },
        "sourceConformance": {
            "status": "SKIPPED",
            "reason": "Kubernetes manifest generation is disabled for implementation releases.",
        },
        "sourceEvidence": {
            "deploymentDiagram": bool(deployment),
            "cloudResourceSpecification": bool(cloud),
            "explicitIntent": has_intent,
        },
        "intentSource": (
            "explicit-input" if has_intent else "implementation-agent-inference"
        ),
        "externalPrerequisites": external_prerequisites(intent),
    }
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "deployment-intent.json").write_text(
        json.dumps(intent, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (reports / "deployment-render.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def infer_intent(
    name: str, cloud: dict[str, Any], deployment_diagram: str = ""
) -> dict[str, Any]:
    """기존 클라우드 명세에서 Docker 보고서에 넣을 배포 의도를 계산한다."""
    resources = cloud.get("resources", [])
    provider = cloud_provider(cloud)
    clusters = [
        item for item in resources if cloud_role(provider, item) == "cluster"
    ]
    if len(clusters) != 1:
        raise ValueError(
            "Automatic deployment intent inference supports exactly one Kubernetes cluster; "
            "provide one cluster or use a future multi-cluster deployment model"
        )
    cluster = clusters[0]
    registries = [
        item for item in resources if cloud_role(provider, item) == "registry"
    ]
    networking = cluster.get("networking", {})
    exposed_aliases = deployment_exposed_aliases(deployment_diagram)
    workloads = []
    for source in cluster.get("workloads", []) or [{"name": name}]:
        workload_name = dns_name(str(source.get("name", name)))
        replicas = source.get("replicas", {"min": 1, "max": 1})
        if not isinstance(replicas, dict):
            replicas = {
                "min": max(as_int(replicas, 1), 0),
                "max": max(as_int(replicas, 1), 1),
            }
        else:
            replicas = {
                "min": max(as_int(replicas.get("min"), 1), 0),
                "max": max(as_int(replicas.get("max"), 1), 1),
            }
        diagram_alias = dns_name(str(source.get("diagramAlias", "")))
        diagram_exposed = bool(diagram_alias and diagram_alias in exposed_aliases)
        role = str(source.get("role", "")).lower()
        artifact = "frontend" if role == "frontend" else "application"
        exposure = str(source.get("exposure", "")).lower()
        if exposure in {"none", "disabled"}:
            api_like = False
        elif exposure in {"public", "external"}:
            api_like = True
        elif role in {"worker", "consumer", "batch", "scheduler"}:
            api_like = diagram_exposed
        else:
            api_like = (
                diagram_exposed
                or role in {"api", "web", "frontend", "gateway"}
                or bool(source.get("probes"))
                or "api" in workload_name
                or "service" in workload_name
            )
        use_ingress = api_like and networking.get("ingressProtocol") == "HTTPS"
        metrics_path = source.get("monitoring", {}).get("metricsPath")
        external_secret = source.get("externalSecret")
        registry = infer_workload_registry(source, cluster, registries)
        workloads.append(
            {
                "name": workload_name,
                "kind": "Deployment",
                "image": registry_image(provider, registry, workload_name),
                "artifact": artifact,
                **(
                    {"registryRef": resource_reference(registry)}
                    if registry
                    else {}
                ),
                "clusterRef": resource_reference(cluster),
                "port": int(
                    source.get(
                        "port",
                        8080
                        if artifact == "frontend"
                        else networking.get("containerPort", DEFAULT_CONTAINER_PORT),
                    )
                ),
                "replicas": replicas,
                "resources": source.get("resources", {}),
                "health": {
                    "readinessPath": source.get("probes", {}).get(
                        "readiness", "/" if artifact == "frontend" else "/healthz"
                    ),
                    "livenessPath": source.get("probes", {}).get(
                        "liveness",
                        source.get("probes", {}).get(
                            "readiness",
                            "/" if artifact == "frontend" else "/healthz",
                        ),
                    ),
                },
                "service": {
                    "type": (
                        "ClusterIP"
                        if use_ingress
                        else networking.get("serviceExposure", "ClusterIP")
                    )
                },
                "ingress": (
                    {"className": networking.get("ingressClassName", "")}
                    if use_ingress and networking.get("ingressClassName")
                    else {}
                ),
                "monitoring": (
                    {"metricsPath": metrics_path} if metrics_path else {}
                ),
                "capabilities": {
                    "service": api_like,
                    "ingress": use_ingress,
                    "hpa": replicas.get("max", 1) > replicas.get("min", 1),
                    "pdb": replicas.get("min", 1) > 1,
                    "networkPolicy": True,
                    "serviceAccount": True,
                    "externalSecret": bool(external_secret),
                    "configMap": False,
                    "pvc": bool(source.get("persistentVolume")),
                    "serviceMonitor": bool(metrics_path),
                },
                **(
                    {"externalSecret": external_secret}
                    if external_secret
                    else {}
                ),
                **(
                    {"storage": source["persistentVolume"]}
                    if source.get("persistentVolume")
                    else {}
                ),
            }
        )
    return {
        "schemaVersion": SCHEMA_VERSION,
        "namespace": dns_name(name),
        "createNamespace": True,
        "frontend": {
            "mode": (
                "separate"
                if any(item.get("artifact") == "frontend" for item in workloads)
                else "integrated"
            )
        },
        "workloads": workloads,
    }


def validate_intent(intent: dict[str, Any]) -> None:
    """Docker 파일 선택에 필요한 배포 의도의 최소 구조를 검사한다."""
    errors: list[str] = []
    if intent.get("schemaVersion") != SCHEMA_VERSION:
        errors.append(f"schemaVersion must be {SCHEMA_VERSION}")
    workloads = intent.get("workloads")
    if not isinstance(workloads, list) or not workloads:
        errors.append("workloads must be a non-empty array")
        workloads = []
    frontend = intent.get("frontend", {"mode": "integrated"})
    frontend_mode = (
        frontend.get("mode", "integrated")
        if isinstance(frontend, dict)
        else "integrated"
    )
    if frontend_mode not in {"integrated", "separate"}:
        errors.append("frontend.mode must be integrated or separate")
    frontend_workloads = [
        item
        for item in workloads
        if isinstance(item, dict) and item.get("artifact") == "frontend"
    ]
    if frontend_mode == "separate" and len(frontend_workloads) != 1:
        errors.append("frontend.mode=separate requires exactly one frontend workload")
    if frontend_mode == "integrated" and frontend_workloads:
        errors.append("frontend.mode=integrated cannot declare a frontend workload")
    names: set[str] = set()
    for index, workload in enumerate(workloads):
        at = f"workloads[{index}]"
        if not isinstance(workload, dict):
            errors.append(f"{at} must be an object")
            continue
        name = workload.get("name")
        if not isinstance(name, str) or not is_dns1123_name(name):
            errors.append(f"{at}.name must be a DNS-1123 name")
        elif name in names:
            errors.append(f"{at}.name is duplicated: {name}")
        else:
            names.add(name)
        if workload.get("artifact") not in {None, "application", "frontend"}:
            errors.append(f"{at}.artifact must be application or frontend")
        if not isinstance(workload.get("capabilities"), dict):
            errors.append(f"{at}.capabilities must be an object")
        if workload.get("artifact") == "frontend" and as_int(
            workload.get("port"), 0
        ) != 8080:
            errors.append(f"{at}: frontend artifact must listen on port 8080")
    if errors:
        raise ValueError("Invalid deploymentIntent:\n- " + "\n- ".join(errors))


def cloud_provider(cloud: dict[str, Any]) -> str:
    provider = str(cloud.get("provider", "azure")).lower()
    return provider if provider in {"azure", "aws", "gcp"} else "azure"


def cloud_role(provider: str, item: dict[str, Any]) -> str | None:
    kind = str(item.get("type", "")).lower()
    if provider == "azure":
        if kind == "microsoft.containerservice/managedclusters":
            return "cluster"
        if kind == "microsoft.containerregistry/registries":
            return "registry"
    elif provider == "aws":
        if "eks" in kind and "cluster" in kind:
            return "cluster"
        if "ecr" in kind and "repository" in kind:
            return "registry"
    elif provider == "gcp":
        if "container" in kind and "cluster" in kind:
            return "cluster"
        if "artifact" in kind and "repository" in kind:
            return "registry"
    return None


def resource_reference(resource: dict[str, Any]) -> str:
    explicit = resource.get("id")
    if isinstance(explicit, str) and explicit.strip():
        return explicit
    return f"{resource.get('type')}:{resource.get('name')}"


def infer_workload_registry(
    workload: dict[str, Any],
    cluster: dict[str, Any],
    registries: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """명시된 레지스트리를 고르고, 후보가 여럿이면 입력 보완을 요구한다."""
    requested = workload.get("registryRef")
    if isinstance(requested, str) and requested:
        matches = [
            item
            for item in registries
            if requested in {str(item.get("name")), resource_reference(item)}
        ]
        if len(matches) != 1:
            raise ValueError(
                f"workload {workload.get('name')} registryRef does not identify "
                f"one registry: {requested}"
            )
        return matches[0]
    cluster_dependencies = (
        set(cluster.get("dependsOn", []))
        if isinstance(cluster.get("dependsOn"), list)
        else set()
    )
    connected = [
        item
        for item in registries
        if resource_reference(item) in cluster_dependencies
        or item.get("name") in cluster_dependencies
    ]
    candidates = connected or registries
    if len(candidates) > 1:
        raise ValueError(
            f"workload {workload.get('name')} requires registryRef because multiple "
            "registries are available"
        )
    return candidates[0] if candidates else None


def registry_image(
    provider: str, registry: dict[str, Any] | None, workload: str
) -> str:
    if registry is None:
        return f"{workload}:<tag>"
    marker = f"__EASYDEP_REGISTRY_{resource_reference(registry)}__"
    if provider == "aws":
        return f"{marker}:<tag>"
    return f"{marker}/{workload}:<tag>"


def dns_name(value: str) -> str:
    return re.sub(
        r"[^a-z0-9-]+", "-", value.lower().replace("_", "-")
    ).strip("-")[:63].rstrip("-")


def is_dns1123_name(value: str) -> bool:
    return bool(
        len(value) <= 63
        and re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", value)
    )


def as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def deployment_exposed_aliases(diagram: str) -> set[str]:
    """배포 그림에서 외부 요청을 받는 구성 요소 이름을 찾는다."""
    exposed: set[str] = set()
    external_sources = {"lb", "loadbalancer", "gateway", "ingress"}
    actors = {
        match.group(1)
        for match in re.finditer(
            r'(?im)^\s*actor\s+(?:"[^"]+"\s+as\s+)?(\w+)', diagram
        )
    }
    external_sources.update(item.lower() for item in actors)
    for match in re.finditer(
        r"(?im)^\s*(\w+)\s*(?:-+>|=+>)\s*(\w+)", diagram
    ):
        source, target = match.group(1).lower(), match.group(2).lower()
        if source in external_sources:
            exposed.add(dns_name(target))
    return exposed


def remove_previous_render(application: Path, reports: Path) -> list[str]:
    """이전 보고서가 EasyDep 생성물로 기록한 파일만 지운다."""
    report_path = reports / "deployment-render.json"
    if not report_path.is_file():
        return []
    try:
        previous = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    removed: list[str] = []
    root = application.parent.resolve()
    for relative in previous.get("renderedFiles", []):
        if not isinstance(relative, str) or not relative.startswith("application/"):
            continue
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(application.resolve())
        except ValueError:
            continue
        if candidate.is_file():
            candidate.unlink()
            removed.append(relative)
    return removed


def external_prerequisites(intent: dict[str, Any]) -> list[str]:
    """입력에 기록된 기능을 운영하려면 별도로 준비할 항목을 돌려준다."""
    required: set[str] = set()
    for workload in intent.get("workloads", []):
        capabilities = workload.get("capabilities", {})
        if capabilities.get("externalSecret"):
            required.update(
                {
                    "External Secrets Operator CRDs",
                    "Configured ClusterSecretStore and cloud workload identity",
                }
            )
        if capabilities.get("serviceMonitor"):
            required.add("Prometheus Operator ServiceMonitor CRD")
        if capabilities.get("ingress"):
            required.update(
                {
                    "Ingress controller",
                    "TLS Secret or certificate automation",
                }
            )
    return sorted(required)
