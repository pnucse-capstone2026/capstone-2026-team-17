"""기능 작업이 약속한 파일을 모두 만들었는지 최종 확인한다.

Java source 문자열에서 설계 의미를 다시 추측하지 않는다. 파일이 준비되면 coordinator가 실제
backend/frontend build, test, HTTP, schema와 container 검증을 이어서 실행한다.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..generation.java_scaffold import CONTROLLER_BODY_REQUIRED
from ..generation.method_skeleton import IMPLEMENTATION_MARKER


def audit_run_completion(run_root: Path) -> dict[str, object]:
    """누락된 필수 산출물을 해당 기능 작업의 자동 수리 backlog로 만든다."""
    manifest = _read_json(run_root / "reports" / "run-manifest.json")
    tasks = [
        task
        for task in manifest.get("implementation_tasks", [])
        if isinstance(task, dict) and task.get("task_id")
    ]
    backlog: list[dict[str, object]] = []
    produced = 0
    expected = 0
    for task in tasks:
        required = [
            str(path)
            for path in task.get(
                "required_output_paths",
                task.get("allowed_write_paths", []),
            )
        ]
        missing = [path for path in required if not (run_root / path).is_file()]
        context_path = run_root / str(task.get("context_file") or "")
        context = _read_json(context_path) if context_path.is_file() else {}
        controller_paths = context.get("controllerPaths", [])
        unfinished = (
            [
                path
                for path in controller_paths
                if isinstance(path, str)
                and (run_root / path).is_file()
                and CONTROLLER_BODY_REQUIRED in (run_root / path).read_text(encoding="utf-8")
            ]
            if isinstance(controller_paths, list)
            else []
        )
        unfinished_markers = [
            path
            for path in required
            if (run_root / path).is_file()
            and IMPLEMENTATION_MARKER
            in (run_root / path).read_text(encoding="utf-8")
        ]
        expected += len(required)
        produced += len(required) - len(missing)
        if missing or unfinished or unfinished_markers:
            backlog.append(
                {
                    "task_id": str(task["task_id"]),
                    "task_type": str(task.get("task_type", "")),
                    "objective": "Create the required implementation files and pass the relevant build and tests.",
                    "missing_outputs": missing,
                    "evidence": [
                        *[f"Missing required output: {path}" for path in missing],
                        *[f"Unimplemented Controller body remains: {path}" for path in unfinished],
                        *[
                            f"Unresolved implementation marker remains: {path}"
                            for path in unfinished_markers
                        ],
                    ],
                }
            )

    report: dict[str, object] = {
        "schemaVersion": "implementation-completion-audit/v2",
        "run": run_root.name,
        "status": "INCOMPLETE" if backlog else "COMPLETE",
        "summary": {
            "workUnits": len(tasks),
            "expectedOutputs": expected,
            "producedOutputs": produced,
            "missingOutputs": expected - produced,
            "backlogTasks": len(backlog),
        },
        "completionCriteria": [
            "Every implementation owner produced its required files.",
            "The complete backend and frontend builds continue in Testing.",
            "Database schema, HTTP flows, and container runtime are verified with execution evidence.",
        ],
        "backlog": backlog,
    }
    reports = run_root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "implementation-completion-audit.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (reports / "implementation-backlog.json").write_text(
        json.dumps(backlog, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return report


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))
