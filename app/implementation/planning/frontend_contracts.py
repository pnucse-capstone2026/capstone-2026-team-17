from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

DEFAULT_FRONTEND_CONTRACT_BUDGET = 100_000


class FrontendContractBudgetExceeded(ValueError):
    """Raised instead of silently transmitting an incomplete generated contract."""


@dataclass(frozen=True)
class GeneratedClientOperation:
    operation_id: str
    class_name: str
    request_type: str | None
    response_type: str
    source_path: Path


@dataclass(frozen=True)
class GeneratedClientContracts:
    generated_root: Path
    source_root: Path
    import_root: str
    files: tuple[Path, ...]

    @classmethod
    def discover(cls, generated_root: Path) -> GeneratedClientContracts:
        generated_root = generated_root.resolve()
        if not generated_root.is_dir():
            raise ValueError(
                f"OpenAPI Generator frontend client was not found: {generated_root}"
            )

        files = tuple(
            path
            for path in sorted(generated_root.rglob("*.ts"))
            if not _is_test_contract(path.relative_to(generated_root))
        )
        if not files:
            raise ValueError("OpenAPI Generator produced no TypeScript client contracts")

        source_root = _discover_source_root(generated_root, files)
        relative_source = source_root.relative_to(generated_root).as_posix()
        import_root = "src/generated"
        if relative_source != ".":
            import_root += f"/{relative_source}"
        return cls(
            generated_root=generated_root,
            source_root=source_root,
            import_root=import_root,
            files=files,
        )

    @property
    def page_import_root(self) -> str:
        relative = self.import_root.removeprefix("src/")
        return f"../{relative}"

    def render(
        self, max_chars: int = DEFAULT_FRONTEND_CONTRACT_BUDGET
    ) -> str:
        chunks = self._render_chunks(compact=False)
        total_chars = sum(len(chunk) for chunk in chunks)
        if total_chars > max_chars:
            compact_chunks = self._render_chunks(compact=True)
            compact_total = sum(len(chunk) for chunk in compact_chunks)
            if compact_total <= max_chars:
                return "\n".join(compact_chunks)
        if total_chars > max_chars:
            raise FrontendContractBudgetExceeded(
                "Generated TypeScript contracts exceed the frontend-agent context "
                f"budget: {total_chars} > {max_chars} characters across "
                f"{len(self.files)} files. Split frontend planning by OpenAPI tag/page "
                "before transmitting any partial contract."
            )
        return "\n".join(chunks)

    def _render_chunks(self, *, compact: bool) -> list[str]:
        chunks: list[str] = []
        for path in self.files:
            source = path.read_text(encoding="utf-8").strip()
            if compact:
                source = _agent_contract_surface(
                    _compact_typescript(source),
                    path.relative_to(self.generated_root),
                )
            chunks.append(
                f"// {path.relative_to(self.generated_root).as_posix()}\n"
                f"{source}\n"
            )
        return chunks

    def resolve_operations(
        self, operation_ids: list[str]
    ) -> dict[str, GeneratedClientOperation]:
        """Resolve exact generated methods without guessing from similar names."""

        resolved: dict[str, GeneratedClientOperation] = {}
        for operation_id in sorted(set(operation_ids)):
            matches: list[GeneratedClientOperation] = []
            for path in self.files:
                if path.parent.name != "apis" or path.name == "index.ts":
                    continue
                source = path.read_text(encoding="utf-8")
                class_match = re.search(r"\bexport class ([A-Za-z_][A-Za-z0-9_]*)\b", source)
                signature = re.search(
                    rf"\basync\s+{re.escape(operation_id)}\(([^\r\n]*)\)"
                    r"\s*:\s*Promise<([^\r\n]+)>\s*\{",
                    source,
                )
                if class_match is None or signature is None:
                    continue
                parameters = signature.group(1).strip()
                request_match = re.match(
                    r"requestParameters:\s*([A-Za-z_][A-Za-z0-9_]*)",
                    parameters,
                )
                request_type = request_match.group(1) if request_match else None
                matches.append(
                    GeneratedClientOperation(
                        operation_id=operation_id,
                        class_name=class_match.group(1),
                        request_type=request_type,
                        response_type=signature.group(2).strip(),
                        source_path=path,
                    )
                )
            if len(matches) == 1:
                resolved[operation_id] = matches[0]
        return resolved

    def render_call_skeleton(
        self,
        operation_ids: list[str],
        resolved: dict[str, GeneratedClientOperation] | None = None,
    ) -> tuple[str, list[str]]:
        """Render wrappers only for exact methods exported by the generated client."""

        if resolved is None:
            resolved = self.resolve_operations(operation_ids)
        calls = [resolved[operation_id] for operation_id in sorted(resolved)]

        imports = {"Configuration"}
        imports.update(call.class_name for call in calls)
        imports.update(call.request_type for call in calls if call.request_type)
        relative_source = self.source_root.relative_to(self.generated_root).as_posix()
        generated_import = (
            "./generated"
            if relative_source == "."
            else f"./generated/{relative_source}"
        )
        lines = [
            "import {",
            *(f"  {name}," for name in sorted(imports)),
            f"}} from '{generated_import}';",
            "import { API_BASE_URL } from './config';",
            "",
        ]
        class_names = sorted({call.class_name for call in calls})
        for class_name in class_names:
            lines.append(
                f"const {_client_name(class_name)} = new {class_name}("
                "new Configuration({ basePath: API_BASE_URL }));"
            )
        if class_names:
            lines.append("")
        unresolved = sorted(set(operation_ids) - set(resolved))
        for operation_id in unresolved:
            lines.append(
                "// EASYDEP-IMPLEMENT: inspect reports/implementation-tasks/"
                "frontend-generated-client-index.json for "
                f"api:{operation_id}; the generated client has no unique exact export"
            )
        if unresolved:
            lines.append("")
        lines.append("export const apiCalls = {")
        for call in calls:
            invocation = f"{_client_name(call.class_name)}.{call.operation_id}"
            if call.request_type:
                lines.append(
                    f"  {call.operation_id}: (request: {call.request_type}) => "
                    f"{invocation}(request),"
                )
            else:
                lines.append(f"  {call.operation_id}: () => {invocation}(),")
        lines.append("};")
        return "\n".join(lines) + "\n", [call.operation_id for call in calls]


def _discover_source_root(
    generated_root: Path, files: tuple[Path, ...]
) -> Path:
    candidates: set[Path] = set()
    for path in files:
        if path.name == "runtime.ts":
            candidates.add(path.parent)
        if path.parent.name in {"apis", "models"}:
            candidates.add(path.parent.parent)
    if not candidates:
        return generated_root
    return min(
        candidates,
        key=lambda path: (len(path.relative_to(generated_root).parts), path.as_posix()),
    )


def _compact_typescript(source: str) -> str:
    """Remove comments and blank lines without changing string literals."""
    output: list[str] = []
    index = 0
    line: list[str] = []
    state = "code"
    while index < len(source):
        char = source[index]
        next_char = source[index + 1] if index + 1 < len(source) else ""
        if state == "line_comment":
            if char == "\n":
                state = "code"
                if "".join(line).strip():
                    output.append("".join(line).rstrip())
                line = []
            index += 1
            continue
        if state == "block_comment":
            if char == "*" and next_char == "/":
                state = "code"
                index += 2
            else:
                index += 1
            continue
        if state in {"single", "double", "template"}:
            line.append(char)
            if char == "\\" and next_char:
                line.append(next_char)
                index += 2
                continue
            closing = {"single": "'", "double": '"', "template": "`"}[state]
            if char == closing:
                state = "code"
            index += 1
            continue
        if char == "/" and next_char == "/":
            state = "line_comment"
            index += 2
            continue
        if char == "/" and next_char == "*":
            state = "block_comment"
            index += 2
            continue
        if char in {"'", '"', "`"}:
            state = {"'": "single", '"': "double", "`": "template"}[char]
        if char == "\n":
            if "".join(line).strip():
                output.append("".join(line).rstrip())
            line = []
        else:
            line.append(char)
        index += 1
    if "".join(line).strip():
        output.append("".join(line).rstrip())
    return "\n".join(output)


def _agent_contract_surface(source: str, relative: Path) -> str:
    """프론트엔드가 호출할 공개 선언만 남긴다.

    OpenAPI Generator 파일에는 HTTP 요청을 만드는 내부 코드와 JSON 변환 함수가
    크게 반복된다. 프론트엔드 구현 에이전트가 알아야 하는 것은 API 메서드의 이름과
    인자, 모델 필드, ``Configuration`` 생성 방법이다. 원본 파일은 수정하지 않고 이
    공개 부분만 입력에 실어 큰 API도 한 작업으로 처리할 수 있게 한다.
    """
    normalized = relative.as_posix()
    if normalized.endswith("/runtime.ts") or normalized == "runtime.ts":
        return _through_braced_declaration(source, "export class Configuration")

    if "/models/" in f"/{normalized}":
        function_at = source.find("\nexport function ")
        return source[:function_at].rstrip() if function_at >= 0 else source

    if "/apis/" in f"/{normalized}" and relative.name != "index.ts":
        class_at = source.find("\nexport class ")
        if class_at < 0:
            return source
        declaration_start = class_at + 1
        brace_at = source.find("{", declaration_start)
        if brace_at < 0:
            return source[:class_at].rstrip()
        # 바로 위의 Interface가 실제 호출 시그니처를 모두 담는다. 구현 클래스는
        # 존재와 이름만 알려 주고 수십 KB의 요청 조립 본문은 보내지 않는다.
        declaration = source[declaration_start:brace_at].rstrip()
        return f"{source[:class_at].rstrip()}\n{declaration} {{}}"

    return source


def _through_braced_declaration(source: str, marker: str) -> str:
    """marker로 시작하는 선언의 닫는 중괄호까지 안전하게 잘라 낸다."""
    start = source.find(marker)
    if start < 0:
        return source
    brace_at = source.find("{", start)
    if brace_at < 0:
        return source
    depth = 0
    for index in range(brace_at, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[: index + 1]
    return source


def _is_test_contract(relative: Path) -> bool:
    lowered_parts = {part.lower() for part in relative.parts}
    return bool(
        lowered_parts & {"test", "tests", "__tests__"}
        or relative.name.lower().endswith(("test.ts", "spec.ts"))
    )


def _client_name(class_name: str) -> str:
    return class_name[:1].lower() + class_name[1:]
