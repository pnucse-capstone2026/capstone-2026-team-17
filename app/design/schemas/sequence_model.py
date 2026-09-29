"""Accepted, implementation-neutral sequence artifact schema."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.design.contracts.sequence import (
    is_complete_method_call,
    is_return_value_label,
)


class SequenceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SequenceParticipant(SequenceRecord):
    name: str = Field(min_length=1)
    alias: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    kind: Literal["actor", "boundary", "control", "entity", "database"]
    description: str = ""
    source_class: str = ""
    participant_ref: str = Field(min_length=1)


class SequenceFragment(SequenceRecord):
    id: str = Field(min_length=1)
    type: Literal["alt", "opt", "loop"]
    branch: Literal["main", "else"] = "main"
    condition: str = Field(min_length=1)
    # Structural provenance for conditions governed by accepted source data.
    # The condition prose remains presentation text and is never an identity.
    condition_ref: str | None = None


class SequenceArgument(SequenceRecord):
    parameter: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    type: str = Field(min_length=1)
    source_kind: Literal[
        "input", "precondition", "call_parameter", "call_result", "state", "literal",
    ]
    source_ref: str = Field(min_length=1)


class SequenceMessage(SequenceRecord):
    source: str
    target: str
    label: str
    type: Literal["sync", "async", "return", "self", "activate", "deactivate"]
    fragments: list[SequenceFragment] = Field(default_factory=list)
    use_case_ids: list[str] = Field(default_factory=list)
    step_ids: list[str] = Field(default_factory=list)
    call_id: str = ""
    reply_to: str = ""
    arguments: list[SequenceArgument] = Field(default_factory=list)
    # Stable accepted identities; presentation fields retain rendering syntax.
    call_ref: str | None = None
    operation_ref: str | None = None

    @model_validator(mode="after")
    def call_or_return_contract(self) -> SequenceMessage:
        if self.type in {"sync", "self", "async"}:
            if not is_complete_method_call(self.label):
                raise ValueError("call label must be a complete method signature")
            if not self.call_id or self.reply_to:
                raise ValueError("call requires call_id only")
            if not self.call_ref or not self.operation_ref:
                raise ValueError("call requires call_ref and operation_ref")
        if self.type == "return":
            if not is_return_value_label(self.label):
                raise ValueError("return label must be a type identifier")
            if self.call_id or not self.reply_to:
                raise ValueError("return requires reply_to only")
            if not self.call_ref or not self.operation_ref:
                raise ValueError("return requires call_ref and operation_ref")
        return self


class UseCaseSequence(SequenceRecord):
    use_case_id: str = Field(min_length=1)
    use_case_name: str = ""
    Participants: list[SequenceParticipant]
    Messages: list[SequenceMessage]
    UnresolvedSteps: list[dict[str, Any]] = Field(default_factory=list)
    NarrativeSteps: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def messages_reference_owner(self) -> UseCaseSequence:
        for message in self.Messages:
            if message.use_case_ids != [self.use_case_id]:
                raise ValueError("every message must reference its diagram use case")
        return self


class SequenceCollection(SequenceRecord):
    Diagrams: list[UseCaseSequence]
    class_diagram_hash: str = ""
    MethodProposals: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("Diagrams")
    @classmethod
    def diagram_ids_are_unique(
        cls, values: list[UseCaseSequence]
    ) -> list[UseCaseSequence]:
        identifiers = [diagram.use_case_id for diagram in values]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("sequence diagram use_case_ids must be unique")
        return values
