"""OpenHands가 생성된 공개 Java 계약을 바꾸지 않았는지 확인한다.

실제 동작은 Gradle test와 HTTP 흐름 검사가 맡는다. 이 모듈은 그 검사를 흉내 내는
Java 정규식 검증기를 만들지 않고, 생성 직후 저장한 BCE·OpenAPI source와 최종 source의
공개 선언만 비교한다. BCE Entity는 빈 메서드 본문을 구현하면서 생성자나 상태 조회 메서드를
추가할 수 있다. 다만 설계가 만든 기존 공개 선언을 삭제하거나 타입을 바꿀 수는 없다.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

SCHEMA_VERSION = "source-design-conformance/v2"
SNAPSHOT_FILE = "reports/generated-source-contracts.json"
REPORT_FILE = "reports/source-design-conformance.json"


def capture_generated_contracts(run_root: Path, base_package: str) -> dict[str, object]:
    """에이전트 실행 전 BCE·OpenAPI Java source를 기준 자료로 저장한다."""
    package_root = (
        run_root
        / "application/src/main/java"
        / Path(base_package.replace(".", "/"))
    )
    files = [
        {
            "path": path.relative_to(run_root).as_posix(),
            "sha256": _sha256(content := path.read_text(encoding="utf-8")),
            "content": content,
            "entity": area == "bce" and _declares_public_class(content),
        }
        for area in ("bce", "api")
        for path in sorted((package_root / area).rglob("*.java"))
        if path.is_file()
    ]
    payload: dict[str, object] = {
        "schemaVersion": SCHEMA_VERSION,
        "basePackage": base_package,
        "files": files,
    }
    target = run_root / SNAPSHOT_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def verify_source_design_conformance(run_root: Path, spec: object) -> dict[str, object]:
    """최종 build 뒤 생성 계약을 한 번 확인하고 읽기 쉬운 보고서를 남긴다."""
    del spec  # 기준 snapshot에 package와 파일 목록이 모두 들어 있다.
    snapshot_path = run_root / SNAPSHOT_FILE
    if not snapshot_path.is_file():
        report = _report(
            [],
            [
                {
                    "code": "MISSING_CONTRACT_BASELINE",
                    "path": SNAPSHOT_FILE,
                    "message": "생성된 Java 계약 기준 자료가 없습니다.",
                }
            ],
        )
        _write_report(run_root, report)
        raise SourceDesignConformanceError(report)

    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    checks: list[dict[str, object]] = []
    violations: list[dict[str, str]] = []
    for item in snapshot.get("files", []):
        if not isinstance(item, dict):
            continue
        relative = str(item.get("path", ""))
        path = run_root / relative
        status = "PASSED"
        mode = "UNCHANGED"
        if not path.is_file():
            status = "FAILED"
            mode = "MISSING"
        else:
            current = path.read_text(encoding="utf-8")
            if _sha256(current) != item.get("sha256"):
                if bool(item.get("entity")) and _preserves_public_java_contract(
                    str(item.get("content", "")), current
                ):
                    mode = "ENTITY_IMPLEMENTED"
                else:
                    status = "FAILED"
                    mode = "PUBLIC_CONTRACT_CHANGED"
        checks.append({"path": relative, "status": status, "mode": mode})
        if status == "FAILED":
            violations.append(
                {
                    "code": "GENERATED_CONTRACT_CHANGED",
                    "path": relative,
                    "message": "생성된 BCE·OpenAPI 파일이 없거나 공개 선언이 변경되었습니다.",
                }
            )

    report = _report(checks, violations)
    _write_report(run_root, report)
    if violations:
        raise SourceDesignConformanceError(report)
    return report


def entity_public_signature_violations(
    run_root: Path, candidate_root: Path, relative_paths: list[str]
) -> list[str]:
    """Entity 구현이 설계에서 만든 공개 선언을 그대로 남겼는지 확인한다."""
    snapshot_path = run_root / SNAPSHOT_FILE
    if not snapshot_path.is_file():
        return [f"{SNAPSHOT_FILE}: generated Java contract baseline is missing"]
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    entities = {
        str(item.get("path", "")): str(item.get("content", ""))
        for item in snapshot.get("files", [])
        if isinstance(item, dict) and bool(item.get("entity"))
    }
    violations: list[str] = []
    for relative in relative_paths:
        normalized = relative.replace("\\", "/")
        candidate = candidate_root / normalized
        baseline = entities.get(normalized)
        if baseline is None or not candidate.is_file():
            violations.append(f"{normalized}: generated Entity source is missing")
        elif not _preserves_public_java_contract(
            baseline, candidate.read_text(encoding="utf-8")
        ):
            violations.append(
                f"{normalized}: preserve every generated public class and method signature"
            )
    return violations


class SourceDesignConformanceError(RuntimeError):
    """공개 Java 계약이 바뀌었을 때 보고서를 함께 전달하는 오류다."""

    def __init__(self, report: dict[str, object]):
        self.report = report
        super().__init__("Source/design conformance verification failed; see " + REPORT_FILE)


def _preserves_public_java_contract(before: str, after: str) -> bool:
    """설계가 만든 공개 선언을 모두 보존하면 구현용 선언 추가를 허용한다.

    Entity의 attribute는 private이므로 실제 구현에서는 생성자나 getter 같은 보조 메서드가
    필요할 수 있다. 그런 메서드까지 class diagram 계약으로 간주해 막으면 코딩 에이전트가
    정상적인 Java 객체를 만들 수 없다. 따라서 기존 선언은 하나도 빠지지 않아야 하지만,
    새로운 선언이 더 있는 것은 허용한다.
    """
    required = set(_public_java_signatures(before))
    current = set(_public_java_signatures(after))
    return required <= current


def _public_java_signatures(source: str) -> tuple[str, ...]:
    """주석·본문·공백을 제외하고 외부 호출자가 보는 공개 선언을 모은다."""
    clean = re.sub(r"/\*.*?\*/|//[^\n]*", "", source, flags=re.DOTALL)
    normalized = re.sub(r"\s*\n\s*", " ", clean)
    declarations = re.findall(
        r"\bpublic\s+(?:final\s+)?(?:class|interface|enum|record)\s+[A-Za-z_$]\w*|"
        r"\bpublic\s+(?:(?:abstract|final|static)\s+)*"
        r"[A-Za-z_$][\w$<>,.?\[\] ]*\s+[A-Za-z_$]\w*\s*\([^)]*\)"
        r"(?:\s+throws\s+[^{;]+)?(?=\s*[{;])|"
        r"\bpublic\s+[A-Za-z_$]\w*\s*\([^)]*\)(?=\s*\{)|"
        r"\bpublic\s+(?:(?:static|final)\s+)*"
        r"[A-Za-z_$][\w$<>,.?\[\] ]*\s+[A-Za-z_$]\w*\s*(?=[=;])",
        normalized,
    )
    return tuple(sorted(re.sub(r"\s+", "", value) for value in declarations))


def _declares_public_class(source: str) -> bool:
    return bool(re.search(r"\bpublic\s+(?:final\s+)?class\s+[A-Za-z_$]\w*", source))


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _report(
    checks: list[dict[str, object]], violations: list[dict[str, str]]
) -> dict[str, object]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "status": "FAILED" if violations else "PASSED",
        "checks": {"generatedContracts": checks},
        "violations": violations,
        "warnings": [],
    }


def _write_report(run_root: Path, report: dict[str, object]) -> None:
    target = run_root / REPORT_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
