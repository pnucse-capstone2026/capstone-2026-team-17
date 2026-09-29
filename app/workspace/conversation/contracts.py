"""Workspace 대화형 에이전트가 반환할 수 있는 최소 결과 계약."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


class ConversationIntent(StrEnum):
    """대화 해석기가 제안할 수 있는 제한된 실행 의도."""

    ADVANCE = "advance"
    ANSWER = "answer"
    REVISE = "revise"
    BRANCH = "branch"
    RERUN = "rerun"
    CONFIRM_REVISION = "confirm_revision"
    DISMISS_REVISION = "dismiss_revision"


class RevisionTarget(BaseModel):
    """A catalog-owned, version-pinned revision target.

    Caller-provided owner, label, and version are never trusted. This public
    boundary contains only values rebuilt by ``ProjectTools`` from its catalog.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: Annotated[str, Field(min_length=3)]
    kind: Annotated[str, Field(min_length=1)]
    element_id: Annotated[str, Field(min_length=1)]
    owner: Literal["requirements", "design", "implementation", "testing"]
    artifact_type: Annotated[str, Field(min_length=1)]
    artifact_version_id: int | None = None
    display_label: Annotated[str, Field(min_length=1)]

    @model_validator(mode="after")
    def normalize_identity(self) -> RevisionTarget:
        normalized = {
            "ref": self.ref.strip(),
            "kind": self.kind.strip(),
            "element_id": self.element_id.strip(),
            "artifact_type": self.artifact_type.strip(),
            "display_label": self.display_label.strip(),
        }
        if any(not value for value in normalized.values()):
            raise ValueError("revision target fields must not be blank")
        for field, value in normalized.items():
            object.__setattr__(self, field, value)
        return self


class TargetedRevisionInstruction(BaseModel):
    """One model-proposed subchange grounded to a finite candidate ref."""

    model_config = ConfigDict(extra="forbid")

    target: Annotated[str, Field(min_length=3)]
    instruction: Annotated[str, Field(min_length=1, max_length=8_000)]

    @model_validator(mode="after")
    def normalize_instruction(self) -> TargetedRevisionInstruction:
        self.target = self.target.strip()
        self.instruction = self.instruction.strip()
        if not self.target or not self.instruction:
            raise ValueError("targeted revision fields must not be blank")
        return self


class RevisionPatchParameter(BaseModel):
    """A lossless name/type pair for a newly declared operation parameter."""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=500)]
    type: Annotated[str, Field(min_length=1, max_length=500)]

    @model_validator(mode="after")
    def normalize_parameter(self) -> RevisionPatchParameter:
        self.name = self.name.strip()
        self.type = self.type.strip()
        if not self.name or not self.type:
            raise ValueError("patch parameter name and type must not be blank")
        return self


class RevisionPatchArgumentBinding(BaseModel):
    """Closed argument-binding object matching persisted collaboration calls."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    parameter: Annotated[str, Field(min_length=1, max_length=500)]
    source_ref: Annotated[
        str,
        Field(
            min_length=1,
            max_length=2_000,
            validation_alias=AliasChoices("source_ref", "sourceRef"),
            serialization_alias="sourceRef",
        ),
    ]

    @model_validator(mode="after")
    def normalize_binding(self) -> RevisionPatchArgumentBinding:
        self.parameter = self.parameter.strip()
        self.source_ref = self.source_ref.strip()
        if not self.parameter or not self.source_ref:
            raise ValueError("patch argument binding requires parameter and sourceRef")
        return self


class RevisionPatchIntent(BaseModel):
    """One atomic, target-scoped edit proposed by the conversation model.

    The intent is deliberately a small vocabulary rather than an executable
    command.  The design/implementation stage owns applying it after checking
    the target against its catalog.  Optional fields describe the subject of
    the edit without embedding stage-specific IDs or database concerns.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    operation: Literal[
        "add_operation",
        "insert_call_before",
        "insert_call_after",
        "remove_call",
        "rename_operation",
        "preserve_existing_order",
    ] = Field(validation_alias=AliasChoices("operation", "kind"))
    target: Annotated[str, Field(min_length=3)]
    anchor: Annotated[str, Field(max_length=2_000)] = ""
    call: Annotated[str, Field(max_length=2_000)] = ""
    name: Annotated[str, Field(max_length=500)] = ""
    signature: Annotated[str, Field(max_length=2_000)] = ""
    new_name: Annotated[
        str,
        Field(
            max_length=500,
            validation_alias=AliasChoices("new_name", "newName"),
            serialization_alias="newName",
        ),
    ] = ""
    parameters: Annotated[list[RevisionPatchParameter], Field(max_length=20)] = Field(
        default_factory=list
    )
    return_type: Annotated[
        str,
        Field(
            max_length=500,
            validation_alias=AliasChoices("return_type", "returnType"),
            serialization_alias="returnType",
        ),
    ] = ""
    step_refs: Annotated[
        list[str],
        Field(
            max_length=20,
            validation_alias=AliasChoices("step_refs", "stepRefs"),
            serialization_alias="stepRefs",
        ),
    ] = Field(default_factory=list)
    receiver_operation_id: Annotated[
        str,
        Field(
            max_length=500,
            validation_alias=AliasChoices("receiver_operation_id", "receiverOperationId"),
            serialization_alias="receiverOperationId",
        ),
    ] = ""
    anchor_occurrence: int | None = Field(
        default=None,
        ge=1,
        validation_alias=AliasChoices("anchor_occurrence", "anchorOccurrence"),
        serialization_alias="anchorOccurrence",
    )
    argument_bindings: list[RevisionPatchArgumentBinding] = Field(
        default_factory=list,
        validation_alias=AliasChoices("argument_bindings", "argumentBindings"),
        serialization_alias="argumentBindings",
    )

    @model_validator(mode="after")
    def validate_patch_shape(self) -> RevisionPatchIntent:
        for field in (
            "target",
            "anchor",
            "call",
            "name",
            "signature",
            "new_name",
            "return_type",
            "receiver_operation_id",
        ):
            setattr(self, field, getattr(self, field).strip())
        self.step_refs = [item.strip() for item in self.step_refs]
        if any(not item for item in self.step_refs):
            raise ValueError("patch step refs must not be blank")
        if not self.target:
            raise ValueError("patch target must not be blank")
        required = {
            # Return type and flow provenance can be completed from the
            # insertion patches on the surrounding interpretation.  Requiring
            # them here made an otherwise useful structured response fail on
            # a harmless omission by the model.
            "add_operation": self.name or self.signature,
            "insert_call_before": self.anchor
            and (self.receiver_operation_id or self.call),
            "insert_call_after": self.anchor
            and (self.receiver_operation_id or self.call),
            "remove_call": self.call or self.receiver_operation_id,
            "rename_operation": self.new_name,
            "preserve_existing_order": True,
        }
        if not required[self.operation]:
            raise ValueError(
                f"patch operation {self.operation!r} is missing its required details"
            )
        return self


class RevisionInterpretation(BaseModel):
    """Small revision intent proposed by the model and checked by the planner."""

    model_config = ConfigDict(extra="forbid")

    targets: Annotated[list[str], Field(max_length=20)] = Field(default_factory=list)
    semantic_scope: Literal[
        "presentation",
        "contract",
        "behavior",
        "implementation",
        "test_expectation",
        "unknown",
    ]
    requested_effect: Annotated[str, Field(max_length=8_000)] = ""
    clarification: Annotated[str, Field(max_length=2_000)] = ""
    change_type: Literal["modify", "add", "rename", "remove", "unknown"] = "modify"
    target_instructions: Annotated[
        list[TargetedRevisionInstruction], Field(max_length=20)
    ] = Field(default_factory=list)
    patch_intents: Annotated[list[RevisionPatchIntent], Field(max_length=40)] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def normalize_interpretation(self) -> RevisionInterpretation:
        self.targets = [target.strip() for target in self.targets]
        self.requested_effect = self.requested_effect.strip()
        self.clarification = self.clarification.strip()
        if any(not target for target in self.targets):
            raise ValueError("revision targets must not be blank")
        if len(set(self.targets)) != len(self.targets):
            raise ValueError("revision targets must be unique")
        instruction_targets = [item.target for item in self.target_instructions]
        if len(set(instruction_targets)) != len(instruction_targets):
            raise ValueError("targeted revision instructions must be unique")
        if any(target not in self.targets for target in instruction_targets):
            raise ValueError("targeted revision instructions must reference selected targets")
        if any(item.target not in self.targets for item in self.patch_intents):
            raise ValueError("patch intents must reference selected targets")
        # A class operation shared by several flows should carry the union of
        # the insertion flow steps.  This also repairs a common structured
        # output omission without inventing any target or step reference.
        insertions = [
            item
            for item in self.patch_intents
            if item.operation in {"insert_call_before", "insert_call_after"}
            and item.step_refs
        ]
        additions = [
            item for item in self.patch_intents if item.operation == "add_operation"
        ]

        def operation_name(value: str) -> str:
            return value.rsplit("::", 1)[-1].split("(", 1)[0].strip()

        all_insertion_steps = list(
            dict.fromkeys(step for item in insertions for step in item.step_refs)
        )
        for addition in additions:
            matched_steps = list(
                dict.fromkeys(
                    step
                    for item in insertions
                    if any(
                        operation_name(value)
                        == operation_name(addition.name)
                        for value in (item.receiver_operation_id, item.call)
                        if value
                    )
                    for step in item.step_refs
                )
            )
            if not matched_steps and len(additions) == 1:
                matched_steps = all_insertion_steps
            if matched_steps:
                addition.step_refs = matched_steps
        return self


class RevisionPlan(BaseModel):
    """Deterministic plan that is never executed before required approval."""

    model_config = ConfigDict(extra="forbid")

    plan_digest: Annotated[str, Field(min_length=64, max_length=64)]
    status: Literal[
        "ready_local",
        "needs_confirmation",
        "needs_clarification",
        "unsupported",
    ]
    requested_targets: list[RevisionTarget] = Field(default_factory=list)
    authority_targets: list[RevisionTarget] = Field(default_factory=list)
    upstream_candidates: list[RevisionTarget] = Field(default_factory=list)
    downstream_targets: list[RevisionTarget] = Field(default_factory=list)
    execution_mode: Literal["targeted_revision", "stage_rewind", "none"]
    reason_codes: list[str] = Field(default_factory=list)
    explanation: Annotated[str, Field(min_length=1, max_length=4_000)]
    artifact_versions: dict[str, int] = Field(default_factory=dict)
    trace_digest: Annotated[str, Field(min_length=64, max_length=64)]

    @model_validator(mode="after")
    def normalize_plan_collections(self) -> RevisionPlan:
        self.reason_codes = sorted({code.strip() for code in self.reason_codes if code.strip()})
        if len({target.ref for target in self.requested_targets}) != len(self.requested_targets):
            raise ValueError("requested targets must be unique")
        for field in ("authority_targets", "upstream_candidates", "downstream_targets"):
            targets = getattr(self, field)
            if len({target.ref for target in targets}) != len(targets):
                raise ValueError(f"{field} must be unique")
        return self


class RevisionExecutionResult(BaseModel):
    """Result returned by an execution adapter, including a target remap."""

    model_config = ConfigDict(extra="forbid")

    changed_stages: list[str] = Field(default_factory=list)
    touched_targets: dict[str, list[str]] = Field(default_factory=dict)
    regenerated_targets: dict[str, list[str]] = Field(default_factory=dict)
    stale_targets: dict[str, list[str]] = Field(default_factory=dict)
    target_remap: dict[str, str] = Field(default_factory=dict)
    artifact_versions: dict[str, int] = Field(default_factory=dict)


class Reply(BaseModel):
    """상태를 바꾸지 않고 사용자에게 돌려주는 답변."""

    model_config = ConfigDict(extra="forbid")

    text: Annotated[str, Field(min_length=1, max_length=8_000)]


class Clarification(BaseModel):
    """서로 다른 변경을 뜻하는 유한한 후보를 사용자에게 되묻는 결과."""

    model_config = ConfigDict(extra="forbid")

    question: Annotated[str, Field(min_length=1, max_length=2_000)]
    candidates: Annotated[list[str], Field(max_length=12)] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_candidates(self) -> Clarification:
        normalized = [candidate.strip() for candidate in self.candidates]
        if any(not candidate for candidate in normalized):
            raise ValueError("clarification candidates must not be empty")
        if len(set(normalized)) != len(normalized):
            raise ValueError("clarification candidates must be unique")
        self.candidates = normalized
        return self


class CommandIntent(BaseModel):
    """결정론적 router가 검증한 뒤에만 실행할 사용자 의도."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    intent: ConversationIntent
    targets: Annotated[list[str], Field(max_length=20)] = Field(default_factory=list)
    instruction: Annotated[str, Field(max_length=8_000)] = ""
    # Preserve the legacy command payload; Wave 2 explicitly reads this field
    # while constructing an approval action payload.
    revision: RevisionInterpretation | None = Field(default=None, exclude=True)
    # 분기/재실행 routing에만 쓰며 기존 수정 명령의 저장 형식에는 추가하지 않는다.
    stage: str = Field(default="", exclude=True)

    @model_validator(mode="after")
    def validate_intent_payload(self) -> CommandIntent:
        self.targets = [target.strip() for target in self.targets]
        self.instruction = self.instruction.strip()
        self.stage = self.stage.strip()
        if self.revision is not None:
            if self.targets and self.targets != self.revision.targets:
                raise ValueError("command targets must match revision targets")
            if not self.targets:
                self.targets = list(self.revision.targets)
            if not self.instruction:
                self.instruction = self.revision.requested_effect
        if any(not target for target in self.targets):
            raise ValueError("command targets must not be empty")
        if len(set(self.targets)) != len(self.targets):
            raise ValueError("command targets must be unique")
        if self.intent == ConversationIntent.REVISE.value:
            if not self.targets:
                raise ValueError("revise intent requires at least one target")
            if not self.instruction:
                raise ValueError("revise intent requires an instruction")
        if self.intent == ConversationIntent.ANSWER.value and not self.instruction:
            raise ValueError("answer intent requires an instruction")
        allowed_stages = {
            ConversationIntent.BRANCH.value: {"requirements", "design", "implementation"},
            ConversationIntent.RERUN.value: {
                "requirements",
                "design",
                "implementation",
                "testing",
            },
        }
        if self.intent in allowed_stages and self.stage not in allowed_stages[self.intent]:
            raise ValueError(f"{self.intent} intent requires a supported stage")
        return self


ConversationOutcome = Reply | Clarification | CommandIntent

# The module uses postponed annotations so contracts can refer to one another.
# Rebuild after all model classes are present; this keeps direct construction in
# tests and adapters working (not only structured LLM deserialization).
RevisionPatchIntent.model_rebuild()
RevisionInterpretation.model_rebuild()
CommandIntent.model_rebuild()


__all__ = [
    "Clarification",
    "CommandIntent",
    "ConversationIntent",
    "ConversationOutcome",
    "Reply",
    "RevisionExecutionResult",
    "RevisionInterpretation",
    "RevisionPatchArgumentBinding",
    "RevisionPatchIntent",
    "RevisionPatchParameter",
    "RevisionPlan",
    "RevisionTarget",
    "TargetedRevisionInstruction",
]
