"""제품 worker가 사용하는 구현 생성·계획·실행 명령을 제공한다."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..agents import execute_openhands_task
from ..agents.runtime import OWNER_TASK_TYPES
from ..agents.workspace import load_strict_task
from ..generation.orchestrator import PrototypeOrchestrator, load_job
from ..workflows.coordinator import plan_workflow, run_workflow
from ..workflows.repair import ReviewerProviderError


def main(argv: list[str] | None = None) -> int:
    """명령행 인자를 읽어 현재 제품 경로에 필요한 작업 하나를 실행한다."""
    arguments = sys.argv[1:] if argv is None else argv
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    if arguments and arguments[0] in {"plan-workflow", "run-workflow", "run-owner"}:
        parser = argparse.ArgumentParser(
            description="Plan, run, or diagnose the implementation workflow"
        )
        parser.add_argument(
            "command", choices=("plan-workflow", "run-workflow", "run-owner")
        )
        parser.add_argument("run", type=Path)
        parser.add_argument("job", type=Path)
        parser.add_argument("--retry-failed", action="store_true")
        parser.add_argument("task_id", nargs="?")
        args = parser.parse_args(arguments)
        spec = load_job(args.job.resolve())
        try:
            if args.command == "plan-workflow":
                if args.task_id:
                    parser.error("task_id is only valid for run-owner")
                result = plan_workflow(args.run.resolve(), spec)
            elif args.command == "run-workflow":
                if args.task_id:
                    parser.error("task_id is only valid for run-owner")
                result = run_workflow(
                    args.run.resolve(),
                    spec,
                    retry_failed=args.retry_failed,
                )
            else:
                if args.retry_failed:
                    parser.error("--retry-failed is only valid for run-workflow")
                if not args.task_id:
                    parser.error("run-owner requires task_id")
                run_root = args.run.resolve()
                try:
                    run_root.relative_to(spec.output_root.resolve())
                except ValueError:
                    parser.error("run must be inside the validated job output root")
                if not run_root.is_dir():
                    parser.error("run directory does not exist")
                # Load before execution so an arbitrary task ID can never enter the
                # owner runtime.  Do not call plan/run_workflow here: diagnostics
                # replay one explicitly selected owner task only.
                load_strict_task(run_root, args.task_id, allowed_task_types=OWNER_TASK_TYPES)
                result = execute_openhands_task(run_root, args.task_id)
        except ReviewerProviderError as error:
            if error.failure_kind == "provider_request_validation" and error.status_code == 400:
                # This is deliberately machine-readable and excludes provider
                # response text, headers, and request payloads.
                print(
                    json.dumps(
                        {
                            "failure": {
                                "kind": error.failure_kind,
                                "status_code": error.status_code,
                            }
                        }
                    )
                )
                return 1
            raise
        print(json.dumps(result, ensure_ascii=False))
        return 0

    parser = argparse.ArgumentParser(description="EasyDep implementation generator")
    parser.add_argument("job", type=Path, help="Path to a prototype job JSON file")
    args = parser.parse_args(arguments)
    output = PrototypeOrchestrator(load_job(args.job)).run()
    manifest = json.loads((output / "reports" / "run-manifest.json").read_text(encoding="utf-8"))
    print(
        json.dumps(
            {"status": manifest["status"], "output": str(output)},
            ensure_ascii=False,
        )
    )
    # 입력 보완이 필요한 상태도 생성기가 정상적으로 진단을 남긴 결과다.
    return 0 if manifest["status"] in {"SUCCEEDED", "NEEDS_INPUT"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
