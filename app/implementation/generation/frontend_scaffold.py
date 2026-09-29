from __future__ import annotations

import json
import hashlib
import os
import re
from pathlib import Path
from typing import Any


class FrontendScaffoldError(ValueError):
    pass


HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
OPENAPI_GENERATOR_VERSION = "7.24.0"
OPENAPI_GENERATOR_NAME = "typescript-fetch"
OPENAPI_GENERATOR_IMAGE = (
    f"openapitools/openapi-generator-cli:v{OPENAPI_GENERATOR_VERSION}"
)


def installed_openapi_generator() -> Path | None:
    """공용 툴체인에 설치된 OpenAPI Generator JAR를 찾는다."""
    configured = os.getenv("EASYDEP_OPENAPI_GENERATOR_JAR", "").strip()
    candidates = [
        Path(configured) if configured else None,
        Path(f"/opt/easydep/openapi-generator-{OPENAPI_GENERATOR_VERSION}.jar"),
    ]
    return next(
        (candidate for candidate in candidates if candidate and candidate.is_file()),
        None,
    )
# The React scaffold pins every dependency to an exact version and the only
# per-application value in package.json is `name`, so the resolved lock is
# identical for every job.  Resolving it through the registry cost 7-46s per
# run; the committed template makes it a file write.
#
# To refresh after changing `react_scaffold_files`: write that function's
# package.json into an empty directory, run
# `npm install --package-lock-only --ignore-scripts --no-audit --no-fund`,
# and copy the resulting package-lock.json over the template.  Until then the
# drift guard below routes generation back through npm, so a stale template
# degrades speed rather than correctness.
PACKAGE_LOCK_TEMPLATE = Path(__file__).resolve().parents[1] / "tools" / "frontend" / "package-lock.json"


def _declared_dependencies(package_json: dict[str, Any]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for section in ("dependencies", "devDependencies"):
        value = package_json.get(section)
        if isinstance(value, dict):
            merged.update({str(k): str(v) for k, v in value.items()})
    return merged


def render_package_lock(package_json_text: str) -> str | None:
    """template lock의 앱 이름을 바꿔 반환하며, 내용이 맞지 않으면 ``None``을 반환한다.

    ``None``은 오류가 아니라 실제 npm dependency 해석을 실행하라는 신호다. template은
    속도를 높이기 위한 cache일 뿐이며 ``package.json``보다 우선하지 않는다.
    """
    try:
        package_json = json.loads(package_json_text)
        template = json.loads(PACKAGE_LOCK_TEMPLATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    root = template.get("packages", {}).get("")
    if not isinstance(root, dict):
        return None
    if _declared_dependencies(root) != _declared_dependencies(package_json):
        return None
    name = package_json.get("name")
    if not isinstance(name, str) or not name:
        return None
    template["name"] = name
    root["name"] = name
    return json.dumps(template, ensure_ascii=False, indent=2) + "\n"


def openapi_typescript_fetch_command(
    workspace_root: Path, openapi_path: Path, output_path: Path
) -> list[str]:
    root = workspace_root.resolve()
    source = openapi_path.resolve()
    target = output_path.resolve()
    if root not in source.parents or root not in target.parents:
        raise FrontendScaffoldError(
            "OpenAPI input and frontend output must stay in workspaceRoot"
        )
    jar = installed_openapi_generator()
    arguments = [
        "generate",
        "-g",
        OPENAPI_GENERATOR_NAME,
        "-i",
        str(source) if jar else "",
        "-o",
        str(target) if jar else "",
        "--additional-properties="
        "supportsES6=true,typescriptThreePlus=true,withInterfaces=true,"
        "npmName=@easydep/generated-api,npmVersion=0.1.0",
    ]
    if jar:
        return ["java", "-jar", str(jar), *arguments]

    container_root = Path("/workspace")
    container_source = container_root / source.relative_to(root)
    container_target = container_root / target.relative_to(root)
    arguments[arguments.index("-i") + 1] = container_source.as_posix()
    arguments[arguments.index("-o") + 1] = container_target.as_posix()
    return [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{root}:/workspace",
        OPENAPI_GENERATOR_IMAGE,
        *arguments,
    ]


def write_react_scaffold(
    frontend_root: Path,
    api_spec: dict[str, Any],
    *,
    application_name: str,
    api_base_url: str | None = None,
) -> dict[str, str]:
    files = react_scaffold_files(
        application_name, resolve_api_base_url(api_spec, api_base_url), api_spec
    )
    for relative, content in files.items():
        target = frontend_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return files


def render_frontend_typescript_config() -> str:
    """Render the production app TypeScript project, excluding focused test sources."""
    return """{
  "compilerOptions":{"target":"ES2022","useDefineForClassFields":true,"lib":["ES2022","DOM","DOM.Iterable"],"skipLibCheck":true,"esModuleInterop":true,"allowSyntheticDefaultImports":true,"strict":true,"module":"ESNext","moduleResolution":"Bundler","resolveJsonModule":true,"isolatedModules":true,"noEmit":true,"jsx":"react-jsx"},
  "include":["src"],
  "exclude":["src/**/*.test.ts","src/**/*.test.tsx","src/**/*.spec.ts","src/**/*.spec.tsx","src/**/__tests__/**","src/test/**","src/tests/**"]
}
"""


def write_frontend_typescript_config(frontend_root: Path) -> Path | None:
    """Refresh only the generated frontend tsconfig when a frontend scaffold exists."""
    if not frontend_root.is_dir():
        return None
    target = frontend_root / "tsconfig.json"
    target.write_text(render_frontend_typescript_config(), encoding="utf-8")
    return target


def resolve_api_base_url(
    api_spec: dict[str, Any], override: str | None = None
) -> str:
    """API prefix를 임의로 붙이지 않고 생성할 client의 base URL을 결정한다."""
    if override is not None and override.strip():
        return override.strip().rstrip("/")
    servers = api_spec.get("servers", [])
    if not isinstance(servers, list) or not servers:
        return ""
    server = servers[0]
    if not isinstance(server, dict) or not isinstance(server.get("url"), str):
        return ""
    url = server["url"].strip()
    variables = server.get("variables", {})
    if isinstance(variables, dict):
        for name, definition in variables.items():
            if isinstance(definition, dict) and "default" in definition:
                url = url.replace("{" + str(name) + "}", str(definition["default"]))
    if re.search(r"\{[^{}]+\}", url):
        raise FrontendScaffoldError(
            "OpenAPI server URL contains a variable without a default value"
        )
    return url.rstrip("/")


def react_scaffold_files(
    application_name: str, api_base_url: str, api_spec: dict[str, Any] | None = None
) -> dict[str, str]:
    package_name = re.sub(r"[^a-z0-9]+", "-", application_name.lower()).strip("-")
    package_name = package_name or "easydep-frontend"
    title = json.dumps(application_name.strip() or "EasyDep Application", ensure_ascii=False)
    base_url = json.dumps(api_base_url.rstrip("/"), ensure_ascii=False)
    operations = frontend_feature_operations(api_spec or {})
    return {
        ".gitignore": "node_modules\ndist\n.env.local\n",
        ".env.example": f"VITE_API_BASE_URL={api_base_url.rstrip('/')}\n",
        "package.json": json.dumps(
            {
                "name": package_name,
                "private": True,
                "version": "0.1.0",
                "type": "module",
                "scripts": {
                    "dev": "vite",
                    "build": "tsc -b && vite build",
                    "preview": "vite preview",
                    "test:unit": "vitest run",
                },
                "dependencies": {
                    "react": "18.3.1",
                    "react-dom": "18.3.1",
                    "react-router-dom": "7.18.2",
                },
                "devDependencies": {
                    "@types/react": "18.3.18",
                    "@types/react-dom": "18.3.5",
                    "@vitejs/plugin-react": "4.3.4",
                    "@testing-library/dom": "10.4.2",
                    "@testing-library/jest-dom": "7.0.1",
                    "@testing-library/react": "16.3.3",
                    "@testing-library/user-event": "14.6.7",
                    "jsdom": "26.1.0",
                    "typescript": "5.7.3",
                    "vite": "6.4.3",
                    "vitest": "4.0.18",
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        "index.html": """<!doctype html>
<html lang="en"><head><meta charset="UTF-8" /><meta name="viewport" content="width=device-width, initial-scale=1.0" /><title>Generated application</title></head>
<body><div id="root"></div><script type="module" src="/src/main.tsx"></script></body></html>
""",
        "tsconfig.json": render_frontend_typescript_config(),
        "vite.config.ts": """import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
export default defineConfig({ plugins: [react()], test: { environment: 'jsdom', setupFiles: ['./src/test/setup.ts'] } });
""",
        "src/test/setup.ts": """import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';

afterEach(cleanup);
""",
        "src/vite-env.d.ts": "/// <reference types=\"vite/client\" />\n",
        "src/main.tsx": """import React from 'react';
import ReactDOM from 'react-dom/client';
import { HashRouter } from 'react-router-dom';
import App from './App';
import './styles.css';
ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><HashRouter><App /></HashRouter></React.StrictMode>);
""",
        "src/config.ts": f"export const API_BASE_URL=(import.meta.env.VITE_API_BASE_URL??{base_url}).replace(/\\/$/,'');\n",
        "src/App.tsx": _render_app(title, operations),
        "src/styles.css": "body{margin:0;font-family:Inter,ui-sans-serif,system-ui,sans-serif;background:#f4f7fb;color:#172033}*{box-sizing:border-box}\n@media (max-width:40rem){table{display:block;max-width:100%;overflow-x:auto}}\n",
        "README.md": f"""# {application_name.strip() or 'EasyDep Application'} frontend

`src/generated/` is generated by OpenAPI Generator (`typescript-fetch`). Pages and
components are owned by the EasyDep frontend implementation agent and verified with
`npm run build`; focused behavior tests run with `npm run test:unit`.
""",
    } | {
        f"src/features/{slug}.tsx": _render_feature_component(operation)
        for slug, operation in operations
    }


def frontend_feature_operations(
    api_spec: dict[str, Any],
) -> list[tuple[str, dict[str, str]]]:
    operations: list[dict[str, str]] = []
    for path, path_item in api_spec.get("paths", {}).items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.lower() not in HTTP_METHODS or not isinstance(operation, dict):
                continue
            operation_id = str(operation.get("operationId") or "").strip()
            identity = f"{method.lower()} {path} {operation_id}"
            base = re.sub(r"[^a-z0-9]+", "-", operation_id.lower()).strip("-")
            if not base:
                base = re.sub(r"[^a-z0-9]+", "-", f"{method}-{path}".lower()).strip("-")
            digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
            label = str(operation.get("summary") or operation_id or f"{method.upper()} {path}")
            operations.append({"slug": f"{base}-{digest}", "method": method.upper(), "path": str(path), "id": operation_id or f"{method.upper()} {path}", "label": label})
    return [(item["slug"], item) for item in sorted(operations, key=lambda item: (item["method"], item["path"], item["id"]))]


def _feature_component_name(slug: str) -> str:
    return "Feature" + "".join(part.capitalize() for part in slug.split("-"))


def _render_app(title: str, operations: list[tuple[str, dict[str, str]]]) -> str:
    imports = "\n".join(
        f"import {_feature_component_name(slug)} from './features/{slug}';"
        for slug, _operation in operations
    )
    navigation = "\n".join(
        f"<button type=\"button\" key=\"{slug}\" aria-current={{active === {index} ? 'page' : undefined}} onClick={{() => setActive({index})}}>{{{json.dumps(operation['label'], ensure_ascii=False)}}}</button>"
        for index, (slug, operation) in enumerate(operations)
    )
    views = "\n".join(
        f"{{active === {index} && <{_feature_component_name(slug)} />}}"
        for index, (slug, _operation) in enumerate(operations)
    )
    state_import = "import { useState } from 'react';\n" if operations else ""
    selection_state = "  const [active, setActive] = useState(0);\n" if operations else ""
    content = (
        f"<nav aria-label=\"Features\">{navigation}</nav><section aria-live=\"polite\">{views}</section>"
        if operations else "<p>No API operations are available.</p>"
    )
    return f"{state_import}{imports}\n\nexport default function App() {{\n{selection_state}  return <main><h1>{{{title}}}</h1>{content}</main>;\n}}\n"


def _render_feature_component(operation: dict[str, str]) -> str:
    marker = f"{operation['method']} {operation['path']} ({operation['id']})"
    label = json.dumps(operation["label"], ensure_ascii=False)
    return (
        f"// EASYDEP-IMPLEMENT: {marker}\n"
        f"export default function {_feature_component_name(operation['slug'])}() {{\n"
        f"  return <article><h2>{label}</h2><p>Implementation pending.</p></article>;\n"
        "}\n"
    )


def operation_ids(api_spec: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for path, path_item in api_spec.get("paths", {}).items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method in HTTP_METHODS and isinstance(operation, dict):
                result.append(str(operation.get("operationId") or f"{method.upper()} {path}"))
    return sorted(result)
