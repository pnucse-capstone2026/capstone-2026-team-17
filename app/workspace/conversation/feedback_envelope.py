"""Pure, version-pinned contracts for cross-stage user feedback.

``NORMALIZED`` means only that the typed question policy was satisfied.  It is
not execution approval, catalog membership, or ownership verification; the
next planner must rebuild those facts from its authoritative snapshot.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .contracts import RevisionTarget


class _EnvelopeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class DecisionMeaning(_EnvelopeModel):
    semantic_scope: Literal[
        "presentation", "contract", "behavior", "implementation", "test_expectation"
    ]
    requested_effect: str = Field(min_length=1, max_length=8_000)
    change_type: Literal["modify", "add", "rename", "remove"] = "modify"

    @model_validator(mode="after")
    def normalize(self) -> DecisionMeaning:
        value = self.requested_effect.strip()
        if not value:
            raise ValueError("requested_effect must not be blank")
        object.__setattr__(self, "requested_effect", value)
        return self


class DecisionPolicy(_EnvelopeModel):
    allowed_semantic_scopes: tuple[str, ...] = Field(min_length=1, max_length=5)
    allowed_change_types: tuple[str, ...] = Field(min_length=1, max_length=4)
    required_preserved_constraints: tuple[str, ...] = Field(default=(), max_length=20)

    @model_validator(mode="after")
    def normalize(self) -> DecisionPolicy:
        scopes = _unique_strings(self.allowed_semantic_scopes, "allowed_semantic_scopes")
        changes = _unique_strings(self.allowed_change_types, "allowed_change_types")
        constraints = _unique_strings(
            self.required_preserved_constraints, "required_preserved_constraints"
        )
        allowed_scopes = {
            "presentation",
            "contract",
            "behavior",
            "implementation",
            "test_expectation",
        }
        allowed_changes = {"modify", "add", "rename", "remove"}
        if not set(scopes).issubset(allowed_scopes) or not set(changes).issubset(allowed_changes):
            raise ValueError("policy contains unsupported scope or change type")
        object.__setattr__(self, "allowed_semantic_scopes", scopes)
        object.__setattr__(self, "allowed_change_types", changes)
        object.__setattr__(self, "required_preserved_constraints", constraints)
        return self


class DecisionPayload(_EnvelopeModel):
    normalized_meaning: DecisionMeaning
    authoritative_target_refs: tuple[str, ...] = Field(min_length=1, max_length=20)
    preserved_constraints: tuple[str, ...] = Field(default=(), max_length=20)

    @model_validator(mode="after")
    def normalize(self) -> DecisionPayload:
        object.__setattr__(
            self,
            "authoritative_target_refs",
            _unique_strings(self.authoritative_target_refs, "authoritative_target_refs"),
        )
        object.__setattr__(
            self,
            "preserved_constraints",
            _unique_strings(self.preserved_constraints, "preserved_constraints"),
        )
        return self


class QuestionOption(_EnvelopeModel):
    option_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    description: str = ""
    recommended: bool = False
    decision_payload: DecisionPayload

    @model_validator(mode="after")
    def normalize(self) -> QuestionOption:
        for field in ("option_id", "label", "description"):
            value = getattr(self, field).strip()
            if field != "description" and not value:
                raise ValueError(f"{field} must not be blank")
            object.__setattr__(self, field, value)
        return self


class DetectedLocation(_EnvelopeModel):
    stage: Literal["requirements", "design", "implementation", "testing"]
    artifact_ref: str = Field(min_length=1)
    element_ref: str | None = None

    @model_validator(mode="after")
    def normalize(self) -> DetectedLocation:
        for field in ("stage", "artifact_ref", "element_ref"):
            value = getattr(self, field)
            if value is not None:
                value = value.strip()
                if not value:
                    raise ValueError(f"{field} must not be blank")
                object.__setattr__(self, field, value)
        return self


class BindingSlot(_EnvelopeModel):
    use_case_id: str = Field(alias="useCaseId", min_length=1)
    actor_entry_index: int = Field(alias="actorEntryIndex", ge=0)
    call_index: int = Field(alias="callIndex", ge=0)
    parameter_index: int = Field(alias="parameterIndex", ge=0)
    receiver_operation_id: str | None = Field(default=None, alias="receiverOperationId")
    parameter_name: str | None = Field(default=None, alias="parameterName")


class QuestionTrigger(_EnvelopeModel):
    category: str = Field(min_length=1)
    finding_refs: tuple[str, ...] = Field(default=(), max_length=20)
    evidence_refs: tuple[str, ...] = Field(default=(), max_length=20)
    binding_slot: BindingSlot | None = None

    @model_validator(mode="after")
    def normalize(self) -> QuestionTrigger:
        category = self.category.strip()
        if not category:
            raise ValueError("trigger category must not be blank")
        object.__setattr__(self, "category", category)
        object.__setattr__(self, "finding_refs", _unique_strings(self.finding_refs, "finding_refs"))
        object.__setattr__(
            self, "evidence_refs", _unique_strings(self.evidence_refs, "evidence_refs")
        )
        return self


class BaseRevision(_EnvelopeModel):
    artifact_type: str = Field(min_length=1)
    version_id: int | str | None = None
    digest: str | None = None

    @model_validator(mode="after")
    def validate_identity(self) -> BaseRevision:
        artifact_type = self.artifact_type.strip()
        if not artifact_type:
            raise ValueError("artifact_type must not be blank")
        version = self.version_id
        if isinstance(version, int) and version < 1:
            raise ValueError("version_id must be positive")
        if isinstance(version, str):
            version = version.strip()
            if not version:
                raise ValueError("version_id must not be blank")
            object.__setattr__(self, "version_id", version)
        digest = self.digest.strip() if self.digest is not None else None
        if digest is not None:
            if not digest:
                raise ValueError("digest must not be blank")
            object.__setattr__(self, "digest", digest)
        if version is None and digest is None:
            raise ValueError("base revision requires a version_id or digest")
        object.__setattr__(self, "artifact_type", artifact_type)
        return self


QuestionStatus = Literal["OPEN", "ANSWERED", "SUPERSEDED", "DISMISSED", "STALE"]
DecisionOrigin = Literal["question_answer", "direct_feedback"]
AnswerMode = Literal["option", "free_text"]
DecisionStatus = Literal["RECEIVED", "NORMALIZED", "NEEDS_CLARIFICATION", "SUPERSEDED", "CANCELLED"]


class Question(_EnvelopeModel):
    question_id: str = Field(min_length=1)
    question_version: int = Field(ge=1)
    app_id: str = Field(min_length=1)
    source_execution_id: str | None = None
    draft_id: str | None = None
    detected_at: DetectedLocation
    base_revisions: tuple[BaseRevision, ...] = Field(min_length=1, max_length=20)
    trigger: QuestionTrigger
    authority_candidates: tuple[RevisionTarget, ...] = Field(min_length=1, max_length=20)
    prompt: str = Field(min_length=1)
    options: tuple[QuestionOption, ...] = Field(default=(), max_length=12)
    allow_free_text: bool = False
    blocking: bool = True
    decision_policy: DecisionPolicy
    status: QuestionStatus = "OPEN"

    @model_validator(mode="after")
    def validate_question(self) -> Question:
        for field in ("question_id", "app_id", "prompt"):
            value = getattr(self, field).strip()
            if not value:
                raise ValueError(f"{field} must not be blank")
            object.__setattr__(self, field, value)
        source_values = []
        for field in ("source_execution_id", "draft_id"):
            value = getattr(self, field)
            if value is not None:
                value = value.strip()
                if not value:
                    raise ValueError(f"{field} must not be blank")
                source_values.append(value)
                object.__setattr__(self, field, value)
        if len(source_values) != 1:
            raise ValueError("exactly one source_execution_id or draft_id is required")
        if not self.options and not self.allow_free_text:
            raise ValueError("question must allow options or free text")
        _unique_strings((option.option_id for option in self.options), "option_ids")
        candidate_refs = set(_unique_targets(self.authority_candidates, "authority target refs"))
        for option in self.options:
            if not set(option.decision_payload.authoritative_target_refs).issubset(candidate_refs):
                raise ValueError("option target is outside question authority candidates")
            _validate_payload_policy(option.decision_payload, self.decision_policy)
        _unique_revisions(self.base_revisions)
        _validate_target_revisions(self.authority_candidates, self.base_revisions)
        return self


class Decision(_EnvelopeModel):
    """A typed answer; NORMALIZED is not execution or ownership approval."""

    decision_id: str = Field(min_length=1)
    app_id: str = Field(min_length=1)
    origin: DecisionOrigin
    question_id: str | None = None
    question_version: int | None = Field(default=None, ge=1)
    source_user_message_id: str = Field(min_length=1)
    answer_mode: AnswerMode
    selected_option_id: str | None = None
    raw_answer: str = Field(min_length=1)
    normalized_meaning: DecisionMeaning | None = None
    authoritative_targets: tuple[RevisionTarget, ...] = Field(default=(), max_length=20)
    preserved_constraints: tuple[str, ...] = Field(default=(), max_length=20)
    base_revisions: tuple[BaseRevision, ...] = Field(default=(), max_length=20)
    status: DecisionStatus = "RECEIVED"
    supersedes_decision_id: str | None = None

    @model_validator(mode="after")
    def validate_decision(self) -> Decision:
        for field in ("decision_id", "app_id", "source_user_message_id"):
            value = getattr(self, field).strip()
            if not value:
                raise ValueError(f"{field} must not be blank")
            object.__setattr__(self, field, value)
        if not self.raw_answer.strip():
            raise ValueError("raw_answer must not be blank")
        for field in ("question_id", "selected_option_id", "supersedes_decision_id"):
            value = getattr(self, field)
            if value is not None:
                value = value.strip()
                if not value:
                    raise ValueError(f"{field} must not be blank")
                object.__setattr__(self, field, value)
        if self.supersedes_decision_id == self.decision_id:
            raise ValueError("decision cannot supersede itself")
        if self.origin == "question_answer":
            if not self.question_id or self.question_version is None:
                raise ValueError("question_answer requires question_id and question_version")
        elif self.question_id is not None or self.question_version is not None:
            raise ValueError("direct_feedback cannot identify a question")
        if self.answer_mode == "option" and not self.selected_option_id:
            raise ValueError("option answer requires selected_option_id")
        if self.answer_mode == "free_text" and self.selected_option_id is not None:
            raise ValueError("free_text answer cannot select an option")
        if self.origin == "direct_feedback" and self.answer_mode != "free_text":
            raise ValueError("direct_feedback only supports free_text")
        if self.answer_mode == "option" and self.origin != "question_answer":
            raise ValueError("option answers require question_answer origin")
        _unique_targets(self.authoritative_targets, "authoritative target refs")
        object.__setattr__(
            self,
            "preserved_constraints",
            _unique_strings(self.preserved_constraints, "preserved_constraints"),
        )
        _unique_revisions(self.base_revisions)
        if self.status == "NORMALIZED" and self.normalized_meaning is None:
            raise ValueError("NORMALIZED decision requires normalized_meaning")
        if self.status == "NORMALIZED" and (
            not self.authoritative_targets or not self.base_revisions
        ):
            raise ValueError("NORMALIZED decision requires targets and base revisions")
        if self.status == "NORMALIZED":
            _validate_target_revisions(self.authoritative_targets, self.base_revisions)
        if self.status == "NEEDS_CLARIFICATION" and (
            self.normalized_meaning is not None
            or self.authoritative_targets
            or self.preserved_constraints
        ):
            raise ValueError("clarification decision cannot carry executable meaning or targets")
        return self


def answer_option(
    question: Question, *, option_id: str, decision_id: str, source_user_message_id: str
) -> Decision:
    if question.status != "OPEN":
        raise ValueError("question is not open")
    if not question.options:
        raise ValueError("question does not allow option answers")
    option = next((item for item in question.options if item.option_id == option_id), None)
    if option is None:
        raise ValueError("unknown option_id")
    payload = option.decision_payload
    allowed = {target.ref: target for target in question.authority_candidates}
    if any(ref not in allowed for ref in payload.authoritative_target_refs):
        raise ValueError("option target is outside question authority candidates")
    return Decision(
        decision_id=decision_id,
        app_id=question.app_id,
        origin="question_answer",
        question_id=question.question_id,
        question_version=question.question_version,
        source_user_message_id=source_user_message_id,
        answer_mode="option",
        selected_option_id=option.option_id,
        raw_answer=option.option_id,
        normalized_meaning=payload.normalized_meaning,
        authoritative_targets=tuple(allowed[ref] for ref in payload.authoritative_target_refs),
        preserved_constraints=payload.preserved_constraints,
        base_revisions=question.base_revisions,
        status="NORMALIZED",
    )


def free_text_decision(
    question: Question,
    *,
    raw_answer: str,
    decision_id: str,
    source_user_message_id: str,
    normalization: object | None = None,
) -> Decision:
    if question.status != "OPEN":
        raise ValueError("question is not open")
    if not question.allow_free_text:
        raise ValueError("question does not allow free-text answers")
    raw = raw_answer.strip()
    if not raw:
        raise ValueError("raw_answer must not be blank")
    candidate: DecisionPayload | None = None
    if normalization is not None:
        try:
            candidate = DecisionPayload.model_validate(normalization)
        except (TypeError, ValueError):
            candidate = None
    allowed = {target.ref: target for target in question.authority_candidates}
    refs = candidate.authoritative_target_refs if candidate is not None else ()
    refs_valid = candidate is not None and all(ref in allowed for ref in refs)
    policy_valid = candidate is not None and _validate_payload_policy(
        candidate, question.decision_policy, raise_error=False
    )
    if candidate is None or not refs_valid or not policy_valid:
        return Decision(
            decision_id=decision_id,
            app_id=question.app_id,
            origin="question_answer",
            question_id=question.question_id,
            question_version=question.question_version,
            source_user_message_id=source_user_message_id,
            answer_mode="free_text",
            raw_answer=raw_answer,
            base_revisions=question.base_revisions,
            status="NEEDS_CLARIFICATION",
        )
    return Decision(
        decision_id=decision_id,
        app_id=question.app_id,
        origin="question_answer",
        question_id=question.question_id,
        question_version=question.question_version,
        source_user_message_id=source_user_message_id,
        answer_mode="free_text",
        raw_answer=raw_answer,
        normalized_meaning=candidate.normalized_meaning,
        authoritative_targets=tuple(allowed[ref] for ref in refs),
        preserved_constraints=candidate.preserved_constraints,
        base_revisions=question.base_revisions,
        status="NORMALIZED",
    )


def direct_feedback_decision(
    *,
    decision_id: str,
    app_id: str,
    source_user_message_id: str,
    raw_answer: str,
    normalization: object,
    authoritative_targets: Sequence[RevisionTarget],
    base_revisions: Sequence[BaseRevision],
    decision_policy: DecisionPolicy,
) -> Decision:
    payload = DecisionPayload.model_validate(normalization)
    _validate_payload_policy(payload, decision_policy)
    targets = tuple(authoritative_targets)
    _unique_targets(targets, "authoritative target refs")
    supplied = {target.ref: target for target in targets}
    if not targets or set(supplied) != set(payload.authoritative_target_refs):
        raise ValueError("direct feedback requires payload refs matching validated targets")
    ordered_targets = tuple(supplied[ref] for ref in payload.authoritative_target_refs)
    return Decision(
        decision_id=decision_id,
        app_id=app_id,
        origin="direct_feedback",
        source_user_message_id=source_user_message_id,
        answer_mode="free_text",
        raw_answer=raw_answer,
        normalized_meaning=payload.normalized_meaning,
        authoritative_targets=ordered_targets,
        preserved_constraints=payload.preserved_constraints,
        base_revisions=tuple(base_revisions),
        status="NORMALIZED",
    )


def base_revisions_match(expected: Sequence[BaseRevision], current: Sequence[BaseRevision]) -> bool:
    """Compare complete snapshots independent of order."""
    try:
        return _revision_map(expected) == _revision_map(current)
    except ValueError:
        return False


def question_is_stale(question: Question, current: Sequence[BaseRevision]) -> bool:
    return not base_revisions_match(question.base_revisions, current)


def _unique_strings(values: Iterable[object], field: str) -> tuple[str, ...]:
    normalized_values: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} must contain unique non-blank strings")
        normalized_values.append(value.strip())
    normalized = tuple(normalized_values)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{field} must contain unique non-blank strings")
    return normalized


def _unique_targets(targets: Sequence[RevisionTarget], field: str) -> tuple[str, ...]:
    return _unique_strings((target.ref for target in targets), field)


def _unique_revisions(revisions: Sequence[BaseRevision]) -> None:
    identities = tuple((item.artifact_type, item.version_id, item.digest) for item in revisions)
    artifacts = tuple(item.artifact_type for item in revisions)
    if len(set(identities)) != len(identities) or len(set(artifacts)) != len(artifacts):
        raise ValueError("base revision artifact identities must be unique")


def _validate_target_revisions(
    targets: Sequence[RevisionTarget], revisions: Sequence[BaseRevision]
) -> None:
    by_type = {revision.artifact_type: revision for revision in revisions}
    for target in targets:
        base = by_type.get(target.artifact_type)
        if base is None:
            raise ValueError("target artifact has no corresponding base revision")
        if target.artifact_version_id is not None:
            if base.version_id is None or base.version_id != target.artifact_version_id:
                raise ValueError("target and base revision versions do not match")
        # Digest-only bases are structurally allowed when the target has no
        # catalog version. Digest/catalog verification belongs to the planner.


def _validate_payload_policy(
    payload: DecisionPayload,
    policy: DecisionPolicy,
    *,
    raise_error: bool = True,
) -> bool:
    meaning = payload.normalized_meaning
    valid = (
        meaning.semantic_scope in policy.allowed_semantic_scopes
        and meaning.change_type in policy.allowed_change_types
        and set(policy.required_preserved_constraints).issubset(set(payload.preserved_constraints))
    )
    if not valid and raise_error:
        raise ValueError("decision payload violates question policy")
    return valid


def _revision_map(
    revisions: Sequence[BaseRevision],
) -> dict[str, tuple[int | str | None, str | None]]:
    result: dict[str, tuple[int | str | None, str | None]] = {}
    for revision in revisions:
        if revision.artifact_type in result:
            raise ValueError("duplicate base revision artifact")
        result[revision.artifact_type] = (revision.version_id, revision.digest)
    return result


__all__ = [
    "BaseRevision",
    "Decision",
    "DecisionMeaning",
    "DecisionPayload",
    "DecisionPolicy",
    "DetectedLocation",
    "Question",
    "QuestionOption",
    "QuestionStatus",
    "QuestionTrigger",
    "answer_option",
    "base_revisions_match",
    "direct_feedback_decision",
    "free_text_decision",
    "question_is_stale",
]
