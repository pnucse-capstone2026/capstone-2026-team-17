"""Workspace 메시지를 해석하는 LLM 기반 대화 경계다.

모델은 발화를 분류하고 유한한 ref 중 하나를 고른다. 실행 단계와 영향 범위를 결정하거나 전문
서비스를 호출하지 않으며, 그 결정은 action registry와 project tool이 맡는다.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Literal, TypeVar

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.requirements.runtime.structured_llm import invoke_structured

from .cloud_guidance import CloudTopic, cloud_guidance_evidence
from .context import ConversationContext
from .contracts import (
    Clarification,
    CommandIntent,
    ConversationIntent,
    Reply,
    RevisionInterpretation,
)
from .project_tools import ProjectTools

T = TypeVar("T", bound=BaseModel)
ProposalCall = Callable[[type[T], list], T]
CloudGuidanceCall = Callable[[str, CloudTopic, str], dict[str, object]]
ConversationResult = Reply | Clarification | CommandIntent


class _ConversationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["reply", "project_question", "command", "clarification"]
    intent: Literal[
        "",
        "advance",
        "answer",
        "revise",
        "branch",
        "rerun",
        "confirm_revision",
        "dismiss_revision",
    ] = ""
    query: str = ""
    reply: str = ""
    question: str = ""
    stage: Literal["", "requirements", "design", "implementation", "testing"] = ""
    cloud_topic: Literal["", "provider_region", "sku", "free_tier", "topology"] = ""
    search_queries: list[str] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def validate_kind_fields(self) -> _ConversationPlan:
        self.search_queries = list(
            dict.fromkeys(item.strip() for item in self.search_queries if item.strip())
        )
        if self.kind == "command" and not self.intent:
            raise ValueError("command plans require an intent")
        if self.kind == "command" and self.intent in {
            ConversationIntent.BRANCH,
            ConversationIntent.RERUN,
        } and not self.stage:
            raise ValueError("branch and rerun commands require a stage")
        if self.kind == "project_question" and not self.query.strip():
            raise ValueError("project questions require a search query")
        if self.kind == "reply" and not self.reply.strip():
            raise ValueError("replies require text")
        if self.kind == "clarification" and not self.question.strip():
            raise ValueError("clarifications require a question")
        return self


class _GroundedReply(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)


_PLAN_SYSTEM = """You are the conversational boundary of a software delivery workspace.
Classify the user's utterance without inventing state or artifact references.
- reply: ordinary social conversation that needs no project data or workflow execution.
- project_question: a question about this project's current state or artifacts. Supply a concise
  search query, not an answer from memory. Set cloud_topic only when the question concerns the
  current cloud provider or region, VM SKU, cost or performance, VM Free Tier, or deployment resource topology;
  otherwise leave it empty.
- command: an explicit request to advance, answer a pending question, revise project content,
  review a pending revision plan, create a checkpoint
  branch, or rerun a delivery stage. Select confirm_revision or dismiss_revision only when the
  corresponding pending-plan action is present in the supplied workspace context. Branch supports
  requirements, design, and implementation; rerun also supports testing. Choose only one stage.
- clarification: the utterance is ambiguous between those categories.
For a revise command, provide two to five short search_queries that let read-only project tools
find both the named artifact and behaviorally related elements. Keep an explicitly named class as
one query. Add ordinary domain synonyms for implied flows: for example, a missing update when a
registration is removed should search for distinct flow mechanisms. In the specific
registration-removal/enrollment-decrease example, you MUST emit separate queries for
"registration removal", "drop registration", and "swap registration" (in addition to a named
artifact query when one is present). Do not spend the limited slots on morphological or redundant
variants such as "remove registration", "decrease enrollment", or "enrollment count decrement"
when they describe the same flow. More generally, use each slot for a different business-flow
synonym or mechanism rather than a word-form variation. These are search terms, not target refs,
and must not assume that a matching artifact exists.
An artifact selection in workspace context identifies what the user is viewing; it is not itself an
instruction to revise it. Classify the user's utterance first.
Never infer a file, impact scope, or target reference. A stage may be selected only for an explicit
branch or rerun request. Buttons and explicit action payloads do not pass through this classifier."""


class ConversationAgent:
    def __init__(
        self,
        proposal_call: ProposalCall | None = None,
        cloud_guidance_call: CloudGuidanceCall | None = None,
    ) -> None:
        self._propose = proposal_call or invoke_structured
        self._cloud_guidance = cloud_guidance_call or cloud_guidance_evidence

    def respond(
        self,
        app_id: str,
        text: str,
        context: ConversationContext,
        *,
        tools: ProjectTools | None = None,
    ) -> ConversationResult:
        """발화 한 건을 분류해 아직 실행하지 않은 공개 결과로 반환한다."""

        project_tools = tools or ProjectTools(app_id)
        utterance = _bounded_text(text.strip(), 8_000)
        # 분류기는 앱 ID나 오래된 command ID를 사용하지 않는다. 현재 상태와 대기 질문을
        # 먼저 주고, 지시 대상을 이어 말할 때 필요한 최근 대화만 남긴다. 실제 project
        # 정보와 수정 대상은 분류 뒤 전용 tool이 다시 읽으므로 여기서 산출물을 복사하지 않는다.
        planning_context = {
            "workspace": context.workspace,
            "pendingQuestion": context.pending_question,
            "actions": context.actions,
            "recentTurns": [
                {"role": turn.role, "text": turn.text}
                for turn in context.turns[-4:]
            ],
            "recentDecisions": context.decisions[-3:],
            "recentTargetRemap": context.target_remap,
        }
        context_json = json.dumps(
            planning_context,
            ensure_ascii=False,
            default=str,
            separators=(",", ":"),
        )
        context_json = _bounded_text(context_json, 24_000)
        plan = self._propose(
            _ConversationPlan,
            [
                SystemMessage(content=_PLAN_SYSTEM),
                HumanMessage(
                    content=(
                        f"Workspace context:\n{context_json}\n\n"
                        f"User utterance:\n{utterance}"
                    )
                ),
            ],
        )
        if plan.kind == "reply":
            return Reply(text=plan.reply.strip())
        if plan.kind == "clarification":
            return Clarification(question=plan.question.strip(), candidates=[])
        if plan.kind == "project_question":
            return self._answer_project_question(
                utterance,
                plan.query,
                project_tools,
                app_id=app_id,
                cloud_topic=plan.cloud_topic,
                context=context,
            )

        assert plan.intent
        if plan.intent == ConversationIntent.REVISE:
            return self._resolve_revision(
                utterance,
                plan.query or utterance,
                project_tools,
                search_queries=plan.search_queries,
                recent_refs=list(dict.fromkeys(context.target_remap.values())),
                context=context,
            )
        return CommandIntent(
            intent=plan.intent,
            instruction=utterance,
            stage=plan.stage,
        )

    def _resolve_revision(
        self,
        text: str,
        query: str,
        tools: ProjectTools,
        *,
        search_queries: list[str] | None = None,
        recent_refs: list[str] | None = None,
        context: ConversationContext | None = None,
    ) -> CommandIntent | Clarification:
        selected_candidates = self._selected_artifact_candidates(context, tools)
        exact_candidates = self._exact_catalog_candidates(text, tools)
        queries = list(dict.fromkeys(
            item.strip()
            for item in [*(search_queries or []), query]
            if item and item.strip()
        ))[:5]
        search_context = self._revision_search_context(
            queries,
            exact_candidates,
            tools,
            context=context,
        )
        candidates = _merge_candidates(
            exact_candidates,
            list(search_context.get("candidates") or []),
            self._selected_scope_candidates(context, selected_candidates),
        )[:20]
        if not candidates and query.strip() != text.strip():
            candidates = _merge_candidates(candidates, tools.search_elements(text))
        if recent_refs:
            validation = tools.validate_revision_selections(recent_refs[:12])
            known_refs = {str(item.get("ref") or "") for item in candidates}
            for ref in validation.get("valid_refs") or []:
                normalized_ref = str(ref)
                if normalized_ref in known_refs:
                    continue
                try:
                    candidates.append(tools.read_element(normalized_ref))
                    known_refs.add(normalized_ref)
                except KeyError:
                    continue
        return self._select_revision(
            text,
            candidates,
            tools,
            context=context,
            evidence=list(search_context.get("evidence") or []),
            exact_refs=[str(item.get("ref") or "") for item in exact_candidates],
        )

    def interpret_revision(
        self,
        text: str,
        target_refs: list[str],
        *,
        tools: ProjectTools,
        context: ConversationContext | None = None,
        sealed_targets: bool = False,
    ) -> CommandIntent | Clarification:
        """Interpret semantics for UI-selected targets without reclassifying the command."""

        validation = tools.validate_revision_selections(target_refs)
        exact_candidates: list[dict] = []
        describe = getattr(tools, "describe_element", None)
        for ref in validation.get("valid_refs") or []:
            try:
                exact_candidates.append(
                    describe(str(ref)) if callable(describe) else tools.read_element(str(ref))
                )
            except KeyError:
                continue
        if sealed_targets:
            return self._select_revision(
                text,
                exact_candidates,
                tools,
                context=context,
                evidence=[],
                exact_refs=[],
            )
        named_candidates = self._exact_catalog_candidates(text, tools)
        search_context = self._revision_search_context(
            [text],
            _merge_candidates(named_candidates, exact_candidates),
            tools,
            context=context,
        )
        # A selected card is locality, not necessarily the leaf that owns the
        # requested change. Include finite current-project matches so a named
        # operation inside a selected sequence can become the exact authority.
        candidates = _merge_candidates(
            named_candidates,
            list(search_context.get("candidates") or []),
            exact_candidates,
            self._selected_scope_candidates(
                context, self._selected_artifact_candidates(context, tools)
            ),
        )[:12]
        return self._select_revision(
            text,
            candidates,
            tools,
            context=context,
            evidence=list(search_context.get("evidence") or []),
            exact_refs=[str(item.get("ref") or "") for item in named_candidates],
        )

    def _select_revision(
        self,
        text: str,
        candidates: list[dict],
        tools: ProjectTools,
        *,
        context: ConversationContext | None = None,
        evidence: list[dict] | None = None,
        exact_refs: list[str] | None = None,
    ) -> CommandIntent | Clarification:
        """Use one structured call for target selection and revision semantics."""

        if not candidates:
            return Clarification(
                question="I could not find the artifact element to revise. Please specify the target.",
                candidates=[],
            )
        ambiguous = _ambiguous_exact_display_candidates(text, candidates)
        if ambiguous:
            return _duplicate_label_clarification(ambiguous)
        selection = self._propose(
            RevisionInterpretation,
            [
                SystemMessage(
                    content=(
                        "Select only refs from the supplied finite candidate list that the user "
                        "directly requests to change; trace-linked or generated downstream projections "
                        "are impact, not separate requested authority, unless the user explicitly asks "
                        "to edit that projection. A single request may contain multiple dependent "
                        "changes: select every candidate that owns one of those changes and return one "
                        "target_instructions entry per selected target. For example, adding a class "
                        "operation and invoking it in named use cases selects the owning class plus "
                        "the collaboration candidate for each named use case. Select a collaboration, "
                        "not a call argument, when adding, removing, or reordering calls. Do not force "
                        "Select an existing Boundary or Control operation only when the user asks to "
                        "change that operation's own signature or contract; do not select it merely "
                        "because a revised collaboration passes through it. "
                        "Do not force "
                        "the user to split one coherent request into separate messages. If the target "
                        "is ambiguous, return no targets and "
                        "ask one concise clarification question. Never invent or rewrite a ref. "
                        "For quantified wording such as all, every, each, or all relevant flows, treat "
                        "the supplied candidate list and read-only evidence as the complete search "
                        "universe. Inspect each collaboration's full content and select every relevant "
                        "collaboration that contains the same trigger operation or equivalent behavior; "
                        "do not stop after selecting the first named example. If multiple collaborations "
                        "share the requested removal/update trigger in the evidence, all of them must be "
                        "selected. This expands only to evidence-backed candidates and never invents refs. "
                        "Classify only the user's semantic scope as presentation, contract, behavior, "
                        "implementation, test_expectation, or unknown. Use implementation for a "
                        "testing finding that asks to repair trace-linked production code; use "
                        "test_expectation only when the expected external behavior itself changes. "
                        "Use presentation only for visual formatting or labels that do not change "
                        "a described system response. Scenario steps, emitted messages, conditions, "
                        "and outcomes are behavior. Use contract for designed interfaces such as "
                        "operation names, parameters, return types, API shapes, and schema fields. "
                        "Use implementation only for source files, implementation tasks, or code "
                        "details behind those contracts. "
                        "requested_effect is a short "
                        "resolved description of what the user asked for. When the supplied "
                        "conversation contains an original request and a follow-up, preserve the "
                        "specific details from both; do not invent details. Never name an executable stage, file, "
                        "owner, impact list, or inferred upstream target. Also classify change_type "
                        "as modify, add, rename, remove, or unknown. A selected artifact is context, "
                        "not a requested mutation. When the user explicitly asks to revise, "
                        "regenerate, or replace an entire diagram, select only the matching "
                        "design_stage candidate. Names of classes, operations, calls, or use cases "
                        "inside that request describe the desired stage contents and must not narrow "
                        "the explicit whole-diagram scope. Use unknown when the wording "
                        "does not distinguish those meanings. For an atomic, target-specific edit, also "
                        "emit patch_intents using only these operations: add_operation (target is the "
                        "owning class, with name, parameters, returnType, and stepRefs), "
                        "insert_call_before or insert_call_after (target is the owning collaboration, "
                        "with the exact existing receiverOperationId in anchor, the new exact "
                        "receiverOperationId, stepRefs, argumentBindings, and anchorOccurrence only "
                        "when the anchor occurs more than once), remove_call (target is the owning "
                        "collaboration, with the exact receiverOperationId), "
                        "rename_operation (target is the operation, with new_name), and "
                        "preserve_existing_order (target is the collaboration whose existing calls must "
                        "remain ordered). For operation declarations, preserve parameters as objects with "
                        "name and type, plus return_type, and use step_refs when the request names flow "
                        "steps. When one added class operation is inserted into multiple selected flows, "
                        "its step_refs must be the union of those selected insertion patches' step_refs. "
                        "For calls, preserve receiver_operation_id, anchor_occurrence, step_refs, "
                        "and argument_bindings when present; camelCase spellings are accepted on input. "
                        "Patch targets must be selected candidates; never invent refs. "
                        "Use patch_intents to describe the minimal edit and retain the natural-language "
                        "instruction as the user-facing fallback."
                    )
                ),
                HumanMessage(
                    content=(
                        f"Revision conversation:\n{self._recent_turns_text(context)}\n\n"
                        f"Revision request:\n{text}\n\nCandidates:\n"
                        + json.dumps(candidates, ensure_ascii=False, default=str)
                        + "\n\nRead-only search evidence:\n"
                        + json.dumps(evidence or [], ensure_ascii=False, default=str)
                    )
                ),
            ],
        )
        available = {str(item.get("ref") or "") for item in candidates}
        selected = list(dict.fromkeys(ref for ref in selection.targets if ref in available))
        decomposed_refs = {
            item.target
            for item in selection.target_instructions
            if item.target in available
        }
        complete_decomposition = (
            len(decomposed_refs) > 1 and decomposed_refs == set(selected)
        )
        # The catalog resolver supplies authoritative identity matches. When a
        # test double or bounded search result omits that method, compare the
        # canonical refs and display identities in this finite candidate set.
        exact_refs = set(exact_refs or []) | set(_exact_candidate_refs(text, candidates))
        exact_refs.intersection_update(available)
        # A uniquely named qualified identity is direct user authority even
        # when the model decomposes it into its container and a projection.
        qualified_exact = _qualified_exact_candidate_refs(text, candidates)
        if len(qualified_exact) == 1:
            selected = qualified_exact
            complete_decomposition = False
        if not complete_decomposition:
            explicit_refs = _explicit_ref_candidate_refs(text, candidates)
            if len(explicit_refs) == 1:
                selected = explicit_refs
            selected_exact = [ref for ref in selected if ref in exact_refs]
            if len(selected_exact) == 1:
                selected = selected_exact
        validation = tools.validate_revision_selections(selected)
        valid = list(validation.get("valid_refs") or [])
        if not valid:
            labels = [
                str(item.get("label") or item.get("ref") or "")
                for item in candidates[:5]
            ]
            return Clarification(
                question=(
                    selection.clarification.strip()
                    or "Please select the artifact element to revise."
                ),
                candidates=[item for item in labels if item],
            )
        # A follow-up after a clarification needs the original request as well
        # as the new constraint. The structured interpreter receives that small
        # dialogue and resolves the user-authored request without guessing refs.
        requested_effect = (
            selection.requested_effect.strip()
            if context is not None and context.turns and selection.requested_effect.strip()
            else text.strip()
        )
        selected_set = set(valid)
        interpretation = selection.model_copy(
            update={
                "targets": valid,
                "requested_effect": requested_effect,
                "target_instructions": [
                    item
                    for item in selection.target_instructions
                    if item.target in selected_set
                ],
                "patch_intents": [
                    item
                    for item in selection.patch_intents
                    if item.target in selected_set
                ],
            }
        )
        return CommandIntent(
            intent=ConversationIntent.REVISE,
            targets=valid,
            instruction=interpretation.requested_effect,
            revision=interpretation,
        )

    def _answer_project_question(
        self,
        text: str,
        query: str,
        tools: ProjectTools,
        *,
        app_id: str,
        cloud_topic: CloudTopic | Literal[""],
        context: ConversationContext | None = None,
    ) -> Reply | Clarification:
        workspace = tools.read_workspace()
        selected_candidates = self._selected_artifact_candidates(context, tools)
        candidates = _merge_candidates(
            self._explicit_selection_candidates(context, selected_candidates),
            tools.search_elements(query),
            selected_candidates,
        )
        if not candidates and query.strip() != text.strip():
            candidates = _merge_candidates(candidates, tools.search_elements(text))
        evidence = {
            "workspace": workspace,
            "selection": self._selection(context),
            "matches": candidates,
        }
        if cloud_topic:
            evidence["cloud"] = self._cloud_guidance(
                app_id,
                cloud_topic,
                f"{query}\n{text}",
            )
        if candidates:
            refs = [str(item.get("ref") or "") for item in candidates[:5]]
            validation = tools.validate_targets(refs)
            readable = list(validation.get("existing_refs") or refs)
            evidence["elements"] = [
                tools.read_element(ref) for ref in readable[:3]
            ]
        reply = self._propose(
            _GroundedReply,
            [
                SystemMessage(
                    content=(
                        "Answer the project question using only the supplied tool evidence. "
                        "Say explicitly when the evidence is insufficient. Do not claim that a "
                        "workflow action ran and do not recommend values or artifact refs absent "
                        "from the evidence. Treat cloud catalog data as guidance, not a selection "
                        "or a guarantee of availability, performance, Free Tier eligibility, or "
                        "zero cost. The user chooses provider, region, and SKU. Answer in English."
                    )
                ),
                HumanMessage(
                    content=(
                        f"Conversation:\n{self._recent_turns_text(context)}\n\n"
                        f"Question:\n{text}\n\nTool evidence:\n"
                        + json.dumps(evidence, ensure_ascii=False, default=str)
                    )
                ),
            ],
        )
        if not reply.text.strip():
            return Clarification(
                question="Please be more specific about what to inspect in the project.",
                candidates=[],
            )
        return Reply(text=reply.text.strip())

    @staticmethod
    def _selection(context: ConversationContext | None) -> dict:
        if context is None:
            return {}
        selection = context.workspace.get("selection")
        return dict(selection) if isinstance(selection, dict) else {}

    def _selected_artifact_candidates(
        self, context: ConversationContext | None, tools: ProjectTools
    ) -> list[dict]:
        """Read the finite catalog behind a UI artifact selection when present."""

        stage = str(self._selection(context).get("artifact_stage") or "").strip()
        artifact_candidates = getattr(tools, "artifact_candidates", None)
        if not stage or not callable(artifact_candidates):
            return []
        return list(artifact_candidates(stage))

    def _selected_scope_candidates(
        self, context: ConversationContext | None, candidates: list[dict]
    ) -> list[dict]:
        """Keep only the explicit selection and its stage row as revision context."""

        selection = self._selection(context)
        selected_ref = str(selection.get("element_ref") or "").strip()
        artifact_stage = str(selection.get("artifact_stage") or "").strip()
        stage_refs = {
            f"design_stage:{artifact_stage}",
            f"requirements_stage:{artifact_stage}",
        }
        return [
            item
            for item in candidates
            if str(item.get("ref") or "") == selected_ref
            or str(item.get("ref") or "") in stage_refs
        ]

    @staticmethod
    def _revision_search_context(
        queries: list[str],
        exact_candidates: list[dict],
        tools: ProjectTools,
        *,
        context: ConversationContext | None = None,
    ) -> dict[str, list[dict]]:
        """Ask the project tool for bounded, cross-stage trace evidence."""

        anchors = [
            str(item.get("ref") or "")
            for item in exact_candidates
            if item.get("ref")
        ]
        search = getattr(tools, "search_change_context", None)
        if callable(search):
            result = search(
                queries,
                anchor_refs=anchors,
                artifact_stage=str(
                    ConversationAgent._selection(context).get("artifact_stage") or ""
                ).strip()
                or None,
            )
            if isinstance(result, dict):
                return {
                    "candidates": list(result.get("candidates") or []),
                    "evidence": list(result.get("evidence") or []),
                }
        matches = _merge_candidates(*(tools.search_elements(query) for query in queries))
        return {"candidates": matches, "evidence": matches}

    @staticmethod
    def _exact_catalog_candidates(text: str, tools: ProjectTools) -> list[dict]:
        resolver = getattr(tools, "resolve_exact_elements", None)
        return list(resolver(text)) if callable(resolver) else []

    def _explicit_selection_candidates(
        self, context: ConversationContext | None, candidates: list[dict]
    ) -> list[dict]:
        """Place the exact UI element first so a grounded answer reads it."""

        selected_ref = str(self._selection(context).get("element_ref") or "").strip()
        if not selected_ref:
            return []
        return [item for item in candidates if str(item.get("ref") or "") == selected_ref]

    @staticmethod
    def _recent_turns_text(context: ConversationContext | None) -> str:
        if context is None:
            return ""
        return _bounded_text(
            "\n".join(
                f"{turn.role}: {turn.text}"
                for turn in context.turns[-4:]
            ),
            16_000,
        )

def _bounded_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    marker = "\n...[content truncated]...\n"
    available = limit - len(marker)
    if available <= 1:
        return text[:limit]
    tail = max(1, available // 3)
    return f"{text[: available - tail]}{marker}{text[-tail:]}"


def _merge_candidates(*groups: list[dict]) -> list[dict]:
    """Preserve the first catalog candidate for every finite public ref."""

    merged: list[dict] = []
    refs: set[str] = set()
    for group in groups:
        for item in group:
            ref = str(item.get("ref") or "")
            if not ref or ref in refs:
                continue
            refs.add(ref)
            merged.append(item)
    return merged


def _exact_candidate_refs(text: str, candidates: list[dict]) -> list[str]:
    """Resolve exact candidate identities without guessing from partial words.

    Labels and names are useful for retrieval, but are not execution identity:
    a free-text display-name match must still be resolved by the bounded target
    selector and validated by the catalog below.
    """

    text_tokens = re.findall(r"[A-Za-z0-9_]+", text.casefold())
    matches: list[tuple[int, str]] = []
    for item in candidates:
        ref = str(item.get("ref") or "").strip()
        if not ref:
            continue
        canonical = str(item.get("canonical_ref") or ref).strip()
        for form in (canonical, canonical.split(":", 1)[-1]):
            if not form:
                continue
            literal = re.search(
                rf"(?<![A-Za-z0-9_]){re.escape(form)}(?![A-Za-z0-9_])",
                text,
                re.IGNORECASE,
            )
            # Separator-insensitive comparison supports canonical catalog
            # identifiers in ordinary prose (for example, ``Owner.method``).
            # Compare whole token sequences so a shorter identifier cannot
            # match a word fragment such as ``Incident`` inside ``Incidental``.
            form_tokens = re.findall(r"[A-Za-z0-9_]+", form.casefold())
            token_match = bool(form_tokens) and any(
                text_tokens[index : index + len(form_tokens)] == form_tokens
                for index in range(len(text_tokens) - len(form_tokens) + 1)
            )
            if literal or token_match:
                matches.append((len(form_tokens), ref))
                break
        else:
            # Names and labels are useful only when they identify one candidate;
            # duplicate display labels are rejected before selection.
            for field in ("name", "label"):
                display = str(item.get(field) or "").strip()
                display_tokens = re.findall(r"[A-Za-z0-9_]+", display.casefold())
                if display_tokens and any(
                    text_tokens[index : index + len(display_tokens)] == display_tokens
                    for index in range(len(text_tokens) - len(display_tokens) + 1)
                ):
                    matches.append((len(display_tokens), ref))
                    break
    if not matches:
        return []
    longest = max(score for score, _ref in matches)
    most_specific = [ref for score, ref in matches if score == longest]
    return list(dict.fromkeys(most_specific))


def _explicit_ref_candidate_refs(text: str, candidates: list[dict]) -> list[str]:
    """Return candidates whose full catalog ref the user wrote literally."""

    refs = []
    for item in candidates:
        candidate_ref = str(item.get("canonical_ref") or item.get("ref") or "").strip()
        if candidate_ref and re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(candidate_ref)}(?![A-Za-z0-9_])",
            text,
            re.IGNORECASE,
        ):
            ref = str(item.get("ref") or "").strip()
            if ref:
                refs.append(ref)
    return list(dict.fromkeys(refs))


def _qualified_exact_candidate_refs(
    text: str, candidates: list[dict]
) -> list[str]:
    """Return uniquely matched candidates named by a qualified identity."""

    matched_refs = []
    for item in candidates:
        ref = str(item.get("ref") or "").strip()
        if not ref:
            continue
        canonical = str(item.get("canonical_ref") or ref).split(":", 1)[-1]
        parts = [part for part in canonical.split("::") if part]
        if len(parts) < 2:
            continue
        # User prose commonly writes a catalog-qualified operation as
        # Owner.method even when its canonical ref uses Owner::method(...).
        pattern = r"\s*[.:/]+\s*".join(
            re.escape(part.split("(", 1)[0]) for part in parts
        )
        if re.search(rf"(?<![A-Za-z0-9_]){pattern}(?![A-Za-z0-9_])", text, re.IGNORECASE):
            matched_refs.append(ref)
    if len(matched_refs) == 1:
        return matched_refs
    return []


def _ambiguous_exact_display_candidates(
    text: str, candidates: list[dict]
) -> list[dict]:
    """Find literal duplicate display names lacking user-supplied context."""

    exact_refs = set(_exact_candidate_refs(text, candidates))

    groups: dict[str, list[dict]] = {}
    for item in candidates:
        ref = str(item.get("canonical_ref") or item.get("ref") or "").strip()
        if not ref:
            continue
        for key in ("name", "label"):
            display = str(item.get(key) or "").strip()
            if display:
                groups.setdefault(display.casefold(), []).append(item)

    normalized = " ".join(text.casefold().split())
    for key, group in groups.items():
        unique = {
            str(item.get("canonical_ref") or item.get("ref") or "").strip(): item
            for item in group
        }
        if len(unique) < 2:
            continue
        # A longer matched identity outside this duplicate-label group (such
        # as Owner.method) names a leaf rather than either shorter container.
        if exact_refs - set(unique):
            continue
        display = str(group[0].get("label") or group[0].get("name") or key).strip()
        if not re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(display)}(?![A-Za-z0-9_])",
            text,
            re.IGNORECASE,
        ):
            continue
        # An exact canonical identifier is unambiguous even when its display
        # name is shared.
        if any(
            re.search(
                rf"(?<![A-Za-z0-9_]){re.escape(str(item.get(field) or '').strip())}(?![A-Za-z0-9_])",
                text,
                re.IGNORECASE,
            )
            for item in unique.values()
            for field in ("ref", "canonical_ref")
            if str(item.get(field) or "").strip()
        ):
            continue
        # Respect explicit type/owner qualifiers in ordinary prose, such as
        # "the use case Incident" or "requirements Incident".
        qualified = set()
        for ref, item in unique.items():
            discriminator_values = [
                ref.split(":", 1)[0].replace("_", " "),
                *(str(item.get(field) or "").replace("_", " ") for field in (
                    "owner", "artifact_type", "type", "kind", "stage"
                )),
            ]
            if any(
                value.strip()
                and re.search(
                    rf"(?<![A-Za-z0-9_]){re.escape(value.strip())}(?![A-Za-z0-9_])",
                    normalized,
                    re.IGNORECASE,
                )
                for value in discriminator_values
            ):
                qualified.add(ref)
        if len(qualified) == 1:
            continue
        return list(unique.values())
    return []


def _duplicate_label_clarification(candidates: list[dict]) -> Clarification:
    choices = [
        f"{item.get('label') or item.get('name') or item.get('ref')} ({item.get('ref')})"
        for item in candidates
    ]
    return Clarification(
        question="Which of these matching elements do you mean?",
        candidates=choices,
    )


conversation_agent = ConversationAgent()


__all__ = ["ConversationAgent", "ConversationResult", "conversation_agent"]
