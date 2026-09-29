"""Semantic admission for bounded implementation behavior capsules."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from pathlib import Path
from textwrap import shorten
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.demo_validation import demo_skip_validation_enabled
from app.design.services.common.structured import parse_structured
from app.llm_connection import build_admission_llm_connection

from .upstream_gap_tool import UpstreamGap, UpstreamGapOption

ADMISSION_CHECKPOINT_SCHEMA = "implementation-admission/v1alpha1"
INTEGRATION_ADMISSION_VALIDATOR_VERSION = "integration-admission/v2"

_GENERATED_API_BASE_EXPORT = re.compile(
    r'^exportconstAPI_BASE_URL=\(import\.meta\.env\.VITE_API_BASE_URL\?\?'
    r'(?P<fallback>"(?:\\.|[^"\\])*")\)\.replace\(/\\/\$/,(?:\'\'|"")\);$'
)
_GENERATED_API_CONFIGURATION_BINDING = re.compile(
    r"^\s*const\s+[A-Za-z_$][A-Za-z0-9_$]*\s*=\s*"
    r"new\s+[A-Za-z_$][A-Za-z0-9_$]*\s*\(\s*"
    r"new\s+Configuration\s*\(\s*\{\s*basePath\s*:\s*API_BASE_URL\s*\}\s*\)"
    r"\s*\)\s*;\s*$",
    re.MULTILINE,
)


class AdmissionOption(BaseModel):
    """The small choice payload returned with a semantic admission gap."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    label: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=300)
    requested_effect: str = Field(min_length=1, max_length=500)


class BehaviorAdmission(BaseModel):
    """The structured semantic admission decision returned by the proposer."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["IMPLEMENT", "NEEDS_INPUT"]
    summary: str = Field(min_length=1)
    source_ref: str
    options: list[AdmissionOption] = Field(default_factory=list, max_length=3)


_INTEGRATION_SYSTEM_PROMPT = """You are the semantic preflight for one bounded integration implementation task.
The supplied evidence boundary is complete. Trace required runtime meanings to backend
preconditions across that evidence. Choose NEEDS_INPUT when evidence contradicts the frozen
contract or omits or ambiguously defines required upstream product or runtime meaning, so
implementation or validation would require guessing. Never assume unseen configuration,
framework defaults, or naming conventions. Choose IMPLEMENT only when the path is semantically
coherent and any remaining work is connector mechanics. Before IMPLEMENT, for each backend
precondition on the representative traced path whose operand comes from runtime context or
configuration, identify an explicit compatible supplier in the supplied evidence: either an
effective compatible value/default or a runtime/deployment binding that supplies or forwards it.
Merely declaring a configurable property is insufficient when its effective value is absent or
incompatible and the supplied deployment neither supplies nor exposes a compatible value. If no
compatible supplier exists and satisfying the precondition would require guessing or an upstream
change, choose NEEDS_INPUT. A build or test pass is not semantic evidence. Return only the
requested structured decision. For NEEDS_INPUT, give one concise root gap using exactly one
allowed source reference and return an empty options list; downstream RTM authority is not part of
this evidence. For IMPLEMENT, source_ref must be empty.
When payload.deliveryContract is present, it is deterministic delivery evidence. In particular,
frontendMode=integrated, apiBaseMode=sameOriginRelative, and supplier=browserDocumentOrigin mean
that an empty or origin-relative VITE_API_BASE_URL is an effective value: browser requests use the
document origin and do not need a separate host supplier, frontend workload, connection, or CORS
binding. A null HTTP interface port with portBinding=runtime is expected late binding, not missing
upstream meaning; the implementation and final runtime-binding check determine the numeric port.
Do not extend this rule to a separate frontend, multiple generated application workloads, an
absolute or scheme-relative API base, a missing HTTP interface, or evidence that contradicts the
delivery contract.
The source_ref is an RTM routing key, not the path where evidence was observed. Copy exactly one
complete string verbatim from payload.sourceRefs; never return an evidenceFiles[].path. For a
runtime or deployment mapping ambiguity or contradiction, prefer an available workload:* source
reference.
"""


def _semantic_source_refs(source_refs: list[str]) -> list[str]:
    """Prefer a use-case specification when both forms identify the same case."""

    specified_use_cases = {
        ref.removeprefix("use_case_spec:")
        for ref in source_refs
        if ref.startswith("use_case_spec:")
    }
    return [
        ref
        for ref in source_refs
        if not (
            ref.startswith("use_case:")
            and ref.removeprefix("use_case:") in specified_use_cases
        )
    ]


def _sha256_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _admission_input_sha256(
    payload: dict[str, object],
    *,
    task_id: str,
    system_prompt: str,
    validator_version: str,
) -> str:
    """Fingerprint exactly the values that can change the admission decision."""

    return _sha256_json(
        {
            "taskId": task_id,
            "admissionModel": build_admission_llm_connection().model,
            "admissionPrompt": system_prompt,
            "validatorSchemaVersion": validator_version,
            "payload": payload,
        }
    )


def _checkpoint_gap(
    checkpoint: dict[str, object], source_refs: list[str]
) -> tuple[bool, UpstreamGap | None]:
    if checkpoint.get("decision") == "IMPLEMENT":
        return True, None
    raw_gap = checkpoint.get("upstreamGap")
    if checkpoint.get("decision") != "NEEDS_INPUT" or not isinstance(raw_gap, dict):
        return False, None
    summary = raw_gap.get("summary")
    source_ref = raw_gap.get("sourceRef")
    if (
        not isinstance(summary, str)
        or not summary
        or not isinstance(source_ref, str)
        or source_ref not in _semantic_source_refs(source_refs)
    ):
        return False, None
    raw_options = raw_gap.get("options", [])
    if not isinstance(raw_options, list):
        return False, None
    options: list[UpstreamGapOption] = []
    for raw_option in raw_options:
        if not isinstance(raw_option, dict):
            return False, None
        try:
            option = AdmissionOption.model_validate(
                {
                    "id": raw_option.get("id"),
                    "label": raw_option.get("label"),
                    "description": raw_option.get("description"),
                    "requested_effect": raw_option.get(
                        "requested_effect", raw_option.get("requestedEffect")
                    ),
                }
            )
        except ValueError:
            return False, None
        options.append(
            UpstreamGapOption(
                id=option.id,
                label=option.label,
                description=option.description,
                requested_effect=option.requested_effect,
            )
        )
    if len(options) < 2 or len(options) > 3 or len({option.id for option in options}) != len(options):
        options = []
    return True, UpstreamGap(
        summary=summary,
        source_ref=source_ref,
        options=tuple(options),
    )


def _clear_stale_need_input(run_root: Path, task_id: str) -> None:
    """Remove only the latest blocker superseded by a new admission decision."""

    path = run_root / "reports" / "agent-executions" / f"{task_id}.result.json"
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if result.get("status") == "NEEDS_INPUT":
        path.unlink()


def _preflight_admission(
    run_root: Path,
    task: dict[str, object],
    source_refs: list[str],
    *,
    payload: dict[str, object],
    system_prompt: str,
    validator_version: str,
    admission_call: Callable[[], UpstreamGap | None],
) -> UpstreamGap | None:
    """Reuse the existing checkpoint contract for one exact admission input."""

    task_id = str(task.get("task_id") or "")
    if not task_id:
        return None
    execution_dir = run_root / "reports" / "agent-executions"
    target = execution_dir / f"{task_id}.admission.json"
    input_sha256 = _admission_input_sha256(
        payload,
        task_id=task_id,
        system_prompt=system_prompt,
        validator_version=validator_version,
    )
    try:
        checkpoint = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        checkpoint = {}
    if not isinstance(checkpoint, dict):
        checkpoint = {}
    if (
        checkpoint.get("schemaVersion") == ADMISSION_CHECKPOINT_SCHEMA
        and checkpoint.get("inputSha256") == input_sha256
    ):
        reused, gap = _checkpoint_gap(checkpoint, source_refs)
        if reused:
            return gap

    gap = admission_call()
    _clear_stale_need_input(run_root, task_id)
    execution_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = {
        "schemaVersion": ADMISSION_CHECKPOINT_SCHEMA,
        "inputSha256": input_sha256,
        "decision": "NEEDS_INPUT" if gap is not None else "IMPLEMENT",
    }
    if gap is not None:
        checkpoint["upstreamGap"] = gap.as_result()
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(target)
    return gap


def _admit_payload(
    payload: dict[str, object],
    source_refs: list[str],
    *,
    system_prompt: str,
    operation: str,
    proposal_call: Callable[..., dict[str, Any]] = parse_structured,
) -> UpstreamGap | None:
    semantic_source_refs = _semantic_source_refs(source_refs)
    connection = build_admission_llm_connection()
    parsed = proposal_call(
        [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False, sort_keys=True),
            },
        ],
        BehaviorAdmission,
        reasoning_effort="low",
        max_completion_tokens=2048,
        operation=operation,
        connection=connection,
    )
    admission = BehaviorAdmission.model_validate(parsed)
    if admission.decision == "IMPLEMENT":
        if admission.source_ref:
            raise ValueError("IMPLEMENT admission must have an empty source_ref")
        return None

    if admission.source_ref not in semantic_source_refs:
        raise ValueError(
            "NEEDS_INPUT source_ref must match exactly one allowed source reference: "
            f"received {admission.source_ref!r}; allowed {semantic_source_refs!r}"
        )
    summary = shorten(admission.summary.strip(), width=500, placeholder="…")
    if not summary:
        raise ValueError("NEEDS_INPUT summary must not be blank")
    options = [
        UpstreamGapOption(
            id=option.id,
            label=option.label,
            description=option.description,
            requested_effect=option.requested_effect,
        )
        for option in admission.options
    ]
    if len(options) < 2 or len(options) > 3 or len({option.id for option in options}) != len(options):
        options = []
    return UpstreamGap(
        summary=summary,
        source_ref=admission.source_ref,
        options=tuple(options),
    )


def integration_evidence_paths(
    run_root: Path,
    task: dict[str, object],
    context: dict[str, object],
) -> list[str]:
    """Return the safe evidence boundary, including not-yet-created planned files."""

    raw_paths = context.get("readSourcePaths")
    if not isinstance(raw_paths, list):
        raise TypeError("Integration admission requires readSourcePaths")
    root = run_root.resolve()
    evidence_paths: set[str] = set()
    for value in raw_paths:
        if not isinstance(value, str) or not value:
            raise ValueError("Integration admission paths must be non-empty strings")
        relative = Path(value.replace("\\", "/"))
        target = (root / relative).resolve()
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or not target.is_relative_to(root)
        ):
            raise ValueError(f"Unsafe integration evidence path: {value}")
        evidence_paths.add(target.relative_to(root).as_posix())

    execution_dir = (root / "reports" / "agent-executions").resolve()
    application_root = (root / "application").resolve()
    for task_id in task.get("depends_on", []):
        result_path = (execution_dir / f"{task_id}.result.json").resolve()
        if not result_path.is_relative_to(execution_dir) or not result_path.is_file():
            continue
        result = json.loads(result_path.read_text(encoding="utf-8"))
        for value in result.get("changedFiles", []):
            if not isinstance(value, str):
                continue
            relative = Path(value.replace("\\", "/"))
            target = (root / relative).resolve()
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or not relative.parts
                or relative.parts[0] != "application"
                or "generated" in relative.parts
                or not target.is_relative_to(application_root)
            ):
                continue
            evidence_paths.add(target.relative_to(root).as_posix())

    return sorted(evidence_paths)


def prepare_integration_admission_payload(
    run_root: Path,
    task: dict[str, object],
    context: dict[str, object],
    source_refs: list[str],
) -> dict[str, object]:
    """Read every file in the admitted evidence boundary for the live decision."""

    root = run_root.resolve()
    paths = integration_evidence_paths(run_root, task, context)
    for path in paths:
        if not (root / path).is_file():
            raise ValueError(f"Missing integration admission evidence: {path}")
    evidence_files = [
        {
            "path": path,
            "content": (root / path).read_bytes().decode("utf-8"),
        }
        for path in paths
    ]
    payload: dict[str, object] = {
        "traceEvidence": context.get("traceEvidence"),
        "deployment": context.get("deployment"),
        "evidenceFiles": evidence_files,
        "sourceRefs": _semantic_source_refs(source_refs),
    }
    delivery_contract = _integrated_same_origin_delivery_contract(
        context.get("deployment"), evidence_files
    )
    if delivery_contract is not None:
        payload["deliveryContract"] = delivery_contract
    return payload


def _integrated_same_origin_delivery_contract(
    deployment: object,
    evidence_files: list[dict[str, str]],
) -> dict[str, object] | None:
    """Describe the one topology where an empty browser API base is a supplier.

    The fact is deliberately absent for separate or ambiguous delivery. The LLM still
    performs semantic admission; this only prevents it from treating an explicit browser
    document-origin route and a runtime-bound port as missing configuration.
    """

    if not isinstance(deployment, dict):
        return None
    if deployment.get("generatedApplicationCount") != 1:
        return None
    workloads = deployment.get("workloads")
    if not isinstance(workloads, list) or len(workloads) != 1:
        return None
    workload = workloads[0]
    if not isinstance(workload, dict):
        return None
    artifact = workload.get("artifact")
    if not isinstance(artifact, dict) or artifact.get("kind") != "generatedApplication":
        return None
    interfaces = workload.get("interfaces")
    if not isinstance(interfaces, list):
        return None
    http_interfaces = [
        item
        for item in interfaces
        if isinstance(item, dict)
        and str(item.get("protocol") or "").lower() in {"http", "https"}
        and item.get("exposure") == "public"
    ]
    if len(http_interfaces) != 1:
        return None

    evidence = {item["path"]: item["content"] for item in evidence_files}
    package_text = evidence.get("application/frontend/package.json")
    if package_text is None:
        return None
    try:
        package = json.loads(package_text)
    except json.JSONDecodeError:
        return None
    if not isinstance(package, dict):
        return None
    env_text = evidence.get("application/frontend/.env.example")
    config_text = evidence.get("application/frontend/src/config.ts", "")
    api_text = evidence.get("application/frontend/src/api.ts", "")
    if env_text is None:
        return None
    fallback = _generated_api_base_fallback(config_text)
    if fallback is None or not _is_origin_relative_base(fallback):
        return None
    if _GENERATED_API_CONFIGURATION_BINDING.search(api_text) is None:
        return None
    api_base = _dotenv_value(env_text, "VITE_API_BASE_URL")
    if api_base is None or not _is_origin_relative_base(api_base):
        return None

    interface = http_interfaces[0]
    workload_id = str(workload.get("id") or "").strip()
    interface_id = str(interface.get("id") or "").strip()
    if not workload_id or not interface_id:
        return None
    port = interface.get("port")
    contract: dict[str, object] = {
        "frontendMode": "integrated",
        "apiBaseMode": "sameOriginRelative",
        "supplier": "browserDocumentOrigin",
        "workloadRef": f"workload:{workload_id}",
        "httpInterfaceId": interface_id,
        "portBinding": "runtime" if port is None else "declared",
    }
    if port is not None:
        contract["port"] = port
    return contract


def _dotenv_value(content: str, name: str) -> str | None:
    prefix = f"{name}="
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line.startswith(prefix):
            continue
        value = line[len(prefix) :].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        return value
    return None


def _generated_api_base_fallback(content: str) -> str | None:
    """Read only the generated one-statement API base export; reject other TS."""

    normalized = re.sub(r"\s+", "", content)
    match = _GENERATED_API_BASE_EXPORT.fullmatch(normalized)
    if match is None:
        return None
    try:
        fallback = json.loads(match.group("fallback"))
    except json.JSONDecodeError:
        return None
    return fallback if isinstance(fallback, str) else None


def _is_origin_relative_base(value: str) -> bool:
    return value == "" or (value.startswith("/") and not value.startswith("//"))


def admit_integration_evidence(
    payload: dict[str, object],
    source_refs: list[str],
    *,
    proposal_call: Callable[..., dict[str, Any]] = parse_structured,
) -> UpstreamGap | None:
    """Admit the complete vertical-integration evidence boundary."""

    gap = _admit_payload(
        payload,
        source_refs,
        system_prompt=_INTEGRATION_SYSTEM_PROMPT,
        operation="implementation-integration-admission",
        proposal_call=proposal_call,
    )
    if gap is None:
        return None
    return UpstreamGap(summary=gap.summary, source_ref=gap.source_ref)


def preflight_semantic_integration(
    run_root: Path,
    task: dict[str, object],
    context: dict[str, object],
    source_refs: list[str],
    *,
    payload: dict[str, object] | None = None,
) -> UpstreamGap | None:
    """Return a cached or new integration gap before starting OpenHands."""

    if payload is None:
        payload = prepare_integration_admission_payload(
            run_root, task, context, source_refs
        )
    if demo_skip_validation_enabled():
        return None
    return _preflight_admission(
        run_root,
        task,
        source_refs,
        payload=payload,
        system_prompt=_INTEGRATION_SYSTEM_PROMPT,
        validator_version=INTEGRATION_ADMISSION_VALIDATOR_VERSION,
        admission_call=lambda: admit_integration_evidence(payload, source_refs),
    )
