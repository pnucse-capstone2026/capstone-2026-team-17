"""자연어 클라우드 제약을 검증된 ``RESOURCE_SPEC``으로 변환한다.

한 번의 구조화 LLM 호출이 proposal을 만들고 결정론 코드가 근거를 대조한 뒤 provider와
region을 정규화하고 계약을 검증한다. 필수값이 유효할 때만 ``resource_spec``을 내보내며,
질문 기반의 제한된 보정에 필요한 초안·근거·거절값은 ``resource_intake``에 항상 남긴다.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import cast

from app.cloudkb import regions
from app.requirements import prompts
from app.requirements.common.state_contract import contract
from app.requirements.config import settings
from app.requirements.contracts.state import AgentState
from app.requirements.resources import cloud_contract, input_registry
from app.requirements.resources.extraction import (
    ResourceConstraintProposalCall,
    propose_resource_constraints,
)
from app.requirements.resources.tools import LOOKUP_TOOLS, convert_to_usd
from app.requirements.runtime import telemetry
from app.requirements.schemas import CloudConstraintExtraction

ResourceStagePatch = dict[str, object]

#: 계약 판. 스키마가 `const`로 못 박아 둔 값이라 **옮겨 적지 않고 읽는다** —
#: 손으로 적어 두면 판을 올릴 때 여기가 조용히 낡는다(판 2에서 실제로 그럴 뻔했다).
SCHEMA_VERSION = cloud_contract.schema_version()

#: 질문의 종류. 빈 칸의 **이유**가 다르면 사용자가 할 일도 다르다.
MISSING = "missing"  # 계약이 요구하는데 아직 값이 없다
ASKED = "asked"  # 에이전트가 직접 물었다(모호·불명확·확인)
SUGGESTED = "suggested"  # 필수는 아닌데 **채우면 뒤 단계 판정이 하나 열린다**

#: `SUGGESTED`가 왜 별도인가 — 되묻기가 두 종류이기 때문이다. 못 채우면 나아갈 수
#: 없는 것과, 채우면 판정이 하나 열리는 것을 같은 얼굴로 물으면 사용자가 전부 필수로
#: 읽는다. 반대로 안 물으면 계획을 다 만든 뒤에야 "그걸 줬으면 판정이 섰다"를 알게
#: 되는데, 그 뒷북이 `verify._what_would_close_the_gaps`가 하던 일이다. 이 종류는
#: **받을 때** 같은 말을 한다.

#: `UNCONVERTIBLE`은 없앴다 — 환율 도구가 생겨서 "환산을 거부한다"는 종류의 질문이
#: 더 이상 존재하지 않는다. 환산이 실패하면 그건 도구 실패이고 `MISSING`으로 묻는다.


@dataclass(frozen=True)
class Candidate:
    """기록된 값 하나. **원문과 출처를 늘 들고 다닌다** — 틀렸을 때 되짚을 근거다."""

    field: str
    value: object
    as_written: str
    source: str  # "agent"
    how: str  # "user"(사용자가 쓴 것) 또는 "tool"(도구가 알아 온 것)
    #: 도구가 알아 온 값일 때, **그 도구가 돌려준 것 전체.** 인용 조각만으로는 근거가
    #: 반쪽이다 — 실측(2026-07-29): `700,000 KRW`를 환산한 `483.0`이 근거에 값만 남아
    #: 환율도 기준일도 사라졌다. 핀을 못 박는 소스는 **쓴 값과 시각을 남긴다**는 것이
    #: 이 저장소의 규율인데, 그 규율이 근거에서 증발한 자리다.
    via: str = ""

    def as_dict(self) -> dict[str, object]:
        """후보 값과 provenance를 저장 가능한 JSON shape로 바꾼다."""

        # **손으로 나열하지 않는다.** 칸을 하나 늘리면 감사 추적에서 조용히 빠지는데,
        # 그게 이 클래스가 존재하는 이유다.
        return asdict(self)


def _ground(fragment: str, seen: list[str]) -> bool:
    """인용한 조각이 **에이전트가 실제로 본 것 안에** 있는가.

    이것이 도구를 마음대로 쓰게 하면서도 지어냄을 막는 장치다. `claim_check`가 답변의
    숫자를 도구 출력에 대조하는 것과 같은 일을, 값 기록에 한다.

    본 것에는 사용자가 쓴 글만이 아니라 **이미 받은 도구 출력도 포함된다** — 리전
    코드는 사용자가 쓴 적이 없고 카탈로그가 답한 것이며, 환산된 USD 금액도 마찬가지다.
    원문만 대조하면 도구가 알아 온 것을 전부 지어냄으로 몰게 된다.

    공백만 헐겁게 본다. 대소문자·구두점까지 풀면 "비슷한 말"이 통과하고, 그러면
    대조가 하는 일이 없어진다.
    """
    squeeze = " ".join((fragment or "").split()).lower()
    if not squeeze:
        return False
    return any(squeeze in " ".join(text.split()).lower() for text in seen)


def _coerce_scalar(kind: str, allowed: tuple[str, ...], raw: object) -> tuple[object | None, str]:
    """타입 하나짜리 마샬링. `_coerce`와 그 object 하위 칸이 **같은 규칙을 쓴다.**

    떼어 낸 이유: `scale{value,unit}`이 생기면서 같은 판정("수여야 한다", "양수여야
    한다", enum 목록)이 두 곳에 필요해졌다. 두 벌로 두면 한쪽만 고쳐진다.
    """
    text = raw.strip() if isinstance(raw, str) else raw
    if kind == "enum":
        value = str(text).strip().lower()
        lowered = {a.lower(): a for a in allowed}
        if value not in lowered:
            return None, f"must be one of: {', '.join(allowed)}"
        return lowered[value], ""
    if kind == "boolean":
        if isinstance(text, bool):
            return text, ""
        value = str(text).strip().lower()
        if value in ("true", "false"):
            return value == "true", ""
        return None, "must be true or false"
    if kind in ("integer", "number"):
        if isinstance(text, bool):
            return None, "must be a number"
        try:
            number = float(str(text).replace(",", "").replace("_", "").strip())
        except (TypeError, ValueError):
            return None, "must be a number written without grouping separators"
        if number <= 0:
            return None, "must be greater than zero"
        return (int(number) if kind == "integer" else number), ""
    return str(text), ""


def _coerce(field_name: str, raw: object) -> tuple[object | None, str]:
    """계약이 선언한 타입으로 맞춘다. 못 맞추면 (None, 사유).

    **이건 자연어 파싱이 아니라 타입 마샬링이다.** 스키마가 `integer`라고 적어 둔 칸에
    문자열 `"3000"`이 오면 JSON Schema 검증이 스펙 전체를 무효로 만든다. 허용하는 정리는
    앞뒤 공백과 자릿수 구분자(`,` `_`)뿐이고, 그 이상은 손대지 않는다 — 표현을 읽어 내는
    일은 모델의 몫이고, 애매하면 모델이 되물어야 한다.
    """
    kind = cloud_contract.field_type(field_name)
    if not kind:
        return None, "is not a RESOURCE_SPEC field"
    text = raw.strip() if isinstance(raw, str) else raw

    if kind in ("enum", "boolean", "integer", "number"):
        return _coerce_scalar(kind, cloud_contract.field_enum(field_name), text)
    if kind == "array":
        # 목록 칸(`workloads`)은 도구가 문자열로 받는다. JSON 배열도, 쉼표로 나눈
        # 것도 받되 **거기까지다** — 무엇이 유효한 종류인지는 아래 도메인 검사가
        # 본다(레지스트리가 claims에서 뽑은 목록).
        if isinstance(text, list):
            items = [str(x).strip() for x in text]
        else:
            body = str(text).strip()
            try:
                parsed = json.loads(body)
            except ValueError:
                parsed = None
            items = (
                [str(x).strip() for x in parsed]
                if isinstance(parsed, list)
                else [p.strip() for p in body.replace("\n", ",").split(",")]
            )
        items = [i for i in items if i]
        if not items:
            return None, "must contain at least one item"
        # 순서는 뜻이 없고 중복은 스키마가 거부한다 — 여기서 정리해 준다.
        return list(dict.fromkeys(items)), ""
    if kind == "object":
        # **2026-08-01에 생겼다.** `scale{value,unit}`이 계약의 첫 object 칸이다.
        # 하위 칸의 이름·타입·필수는 스키마에서 읽는다 — 여기 옮겨 적으면 갈린다.
        if isinstance(text, dict):
            object_body = cast(dict[str, object], text)
        else:
            try:
                decoded = json.loads(str(text))
            except ValueError:
                decoded = None
            if not isinstance(decoded, dict):
                sub = ", ".join(n for n, _k, _e, _r in cloud_contract.field_object(field_name))
                return None, f"must be a JSON object with fields: {sub}"
            object_body = cast(dict[str, object], decoded)
        out: dict[str, object] = {}
        for name, sub_kind, allowed, required in cloud_contract.field_object(field_name):
            if name not in object_body:
                if required:
                    return None, f"is missing required field {name}"
                continue
            # 하위 칸도 같은 마샬링을 받는다 — 규칙이 갈라지지 않게.
            value, why = _coerce_scalar(sub_kind, allowed, object_body[name])
            if why:
                return None, f"{name}: {why}"
            out[name] = value
        extra = set(object_body) - {
            n for n, _k, _e, _r in cloud_contract.field_object(field_name)
        }
        if extra:
            return None, f"contains unknown fields: {', '.join(sorted(extra))}"
        return out, ""
    return str(text), ""


def _domain_error(field_name: str, value: object, draft: dict) -> str:
    """스키마가 못 잡는 **조인 축의 유효성**. 통과하면 빈 문자열.

    계약은 `provider`·`region`을 자유 문자열로 둔다(프로바이더가 늘어날 자리라서). 그래서
    "그런 프로바이더가 실재하는가"는 스키마가 아니라 여기서 본다 — 이 둘이 틀리면 뒤
    단계의 조인이 **오류 없이 빈 답**이 되고, 그게 가장 찾기 어려운 종류의 결함이다.
    """
    if field_name == "provider":
        known = regions.providers()
        if value not in known:
            return (
                f"unknown provider; choose one of {', '.join(known)} "
                "using list_cloud_providers"
            )
    if field_name == "region":
        if not regions.is_region_code(str(value), provider=draft.get("provider")):
            return (
                "must be a region code, not a place name; use resolve_region "
                "to obtain a code"
            )
    if field_name == "workloads":
        # 계획 전체가 이 값 위에 선다. **실측이 없는 종류를 받으면 그 부분 계획이
        # 통째로 비는데, 비었다는 사실이 값으로는 안 보인다** — 그래서 여기서 막는다.
        provider = draft.get("provider")
        if not provider:
            return (
                "record provider first because supported workload kinds differ by provider; "
                "use list_workload_kinds"
            )
        known = input_registry.anchors_for(str(provider))
        unknown = [v for v in cast(list[str], value or []) if v not in known]
        if unknown:
            return (
                f"unsupported workload kinds for {provider}: {', '.join(unknown)}; "
                f"choose from list_workload_kinds "
                f"({', '.join(known[:8])}…)"
            )
    return ""


class ResourceIntakeSession:
    """에이전트가 작용하는 환경. 행동의 결과를 **말로 돌려준다.**

    이 클래스가 도구를 겸하는 이유는 하나다 — 값 기록·계약 조회·되묻기는 전부 **이번
    실행의 상태를 바꾸는** 일이라 실행마다 새로 묶여야 한다. 조회 도구(`LOOKUP_TOOLS`)와
    갈라 둔 경계가 그것이다.
    """

    def __init__(self, seen: list[str]) -> None:
        self.draft: dict = {"schemaVersion": SCHEMA_VERSION, "workloads": ["vm"]}
        self.provenance: list[Candidate] = []
        self.rejected: list[dict] = []
        self.questions: list[dict] = []
        self.understanding = ""
        self.finished = False
        #: 에이전트가 지금까지 **본 것**. 인용 대조의 건초더미다.
        self.seen = list(seen)
        #: 그중 **사용자가 쓴 것이 몇 개까지인가.** 뒤는 전부 도구 출력이다(`saw`가 뒤에만
        #: 붙인다). 목록 자체에 표시를 섞으면 인용 대조가 그 표시까지 건초더미로 센다.
        self.user_seen = len(self.seen)
        #: 실제로 무엇을 했는지. 사람이 되짚는 자리이고, 데모가 그대로 찍는다.
        self.trace: list[dict] = [{"action": "system_scope", "field": "workloads", "value": ["vm"]}]

    # --- 관찰 ---------------------------------------------------------------
    def saw(self, text: str) -> None:
        """도구가 돌려준 것도 에이전트가 본 것이다 — 인용의 근거가 된다."""
        if text:
            self.seen.append(text)

    def contract_status(self) -> str:
        """현재 draft가 계약을 충족하는지와 남은 필수값을 설명한다."""

        missing = cloud_contract.missing_fields(self.draft)
        if not missing:
            return "The contract is satisfied. Say back what you understood, then call finish."
        lines = [f"- {name}: {cloud_contract.why(name)}" for name in missing]
        return (
            "Still required:\n"
            + "\n".join(lines)
            + "\n\nFilled so far: "
            + (
                json.dumps(self.draft, ensure_ascii=False)
                if len(self.draft) > 1
                else "(nothing yet)"
            )
        )

    # --- 행동 ---------------------------------------------------------------
    def record(self, field_name: str, raw: object, evidence: str) -> str:
        """근거가 확인된 field 후보만 현재 resource draft에 기록한다."""

        name = (field_name or "").strip()
        as_written = (evidence or "").strip()

        if not _ground(as_written, self.seen):
            return self._reject(
                name,
                as_written,
                raw,
                "the quoted evidence does not occur in the user input or a tool result",
            )
        value, why = _coerce(name, raw)
        if why:
            return self._reject(name, as_written, raw, why)
        why = _domain_error(name, value, self.draft)
        if why:
            return self._reject(name, as_written, raw, why)

        previous = self.draft.get(name)
        if previous is not None and previous != value:
            # **조용히 덮지 않는다.** 덮으면 밀려난 값이 사라지고, 사라진 값은 되짚을
            # 수 없다. 값이 갈리는 것은 정보가 아니라 질문이라는 것이 이 단계의 규율이다.
            return (
                f"Rejected: {name} is already {previous!r} from earlier evidence. "
                f"Two different values are a question for the user, not a value — "
                f"use ask_user, or explain which evidence supersedes the other."
            )

        origin, via = self._origin(as_written)
        self.draft[name] = value
        self.provenance.append(Candidate(name, value, as_written, "agent", origin, via))
        self.trace.append({"action": "record_field", "field": name, "value": value})
        return f"Accepted: {name} = {value!r}.\n\n{self.contract_status()}"

    def _origin(self, fragment: str) -> tuple[str, str]:
        """인용이 **어디서** 왔는가 — (갈래, 도구였다면 그 출력 전체).

        근거를 읽을 사람에게 이 구별이 가장 중요하다. `ap-northeast-2`는 사용자가 쓴 적이
        없고 카탈로그가 답한 것이며, 환산된 USD 금액도 마찬가지다. 둘을 같은 얼굴로 남기면
        "사용자가 그렇게 말했다"로 읽힌다.

        도구 쪽은 **출력 전체를 함께 남긴다.** 인용 조각은 대개 값 하나라서
        (`483.0`), 그것만으로는 환율도 기준일도 출처도 사라진다.
        """
        squeeze = " ".join(fragment.split()).lower()
        for index, text in enumerate(self.seen):
            if squeeze in " ".join(text.split()).lower():
                if index < self.user_seen:
                    return "user", ""
                return "tool", text[:400]
        return "tool", ""

    def _reject(self, name: str, as_written: str, raw: object, why: str) -> str:
        # 버린 이유를 남긴다 — 빈 칸이 **정보 부재**인지 **읽기 실패**인지 구별된다.
        self.rejected.append(
            {"field": name, "as_written": as_written, "value": raw, "source": "agent", "why": why}
        )
        self.trace.append({"action": "record_field", "field": name, "rejected": why})
        return f"Rejected ({name}): {why}"

    def _question_ui(self, field_name: str) -> dict[str, object]:
        """Return the small amount of UI guidance only cloud-coordinate questions need.

        The Workspace already retrieves the authoritative provider/region catalog.  A
        question therefore carries only its control type and the dependency needed to
        filter that catalog; copying choices here would create a second catalog.
        """

        if field_name == "provider":
            return {"ui": {"control": "cloudProvider"}}
        if field_name == "region":
            ui: dict[str, str] = {
                "control": "cloudRegion",
                "dependsOn": "provider",
            }
            provider = self.draft.get("provider")
            if isinstance(provider, str) and provider:
                ui["knownProvider"] = provider
            return {"ui": ui}
        return {}

    def ask(self, field_name: str, question: str) -> str:
        """계약이 아는 field에 대한 사용자 질문만 pending 목록에 기록한다."""

        name = (field_name or "").strip()
        text = (question or "").strip()
        if not text:
            return "Rejected: an empty question is not a question."
        # 계약이 아는 칸만 묻는다. 모르는 칸을 물으면 사용자가 답해도 갈 곳이 없다 —
        # 화면이 `field`를 키로 `resource_answers`를 만들기 때문이다.
        if name not in cloud_contract.schema_fields():
            return (
                f"Rejected: {name!r} is not a field of the contract, so an answer "
                "would have nowhere to go. Ask about a real field."
            )
        # A proposal may resolve a field and also conservatively list it as
        # ambiguous.  The normalized draft is the source of truth at this point;
        # asking again would make a completed provider/region look unresolved.
        # Rejected and unresolvable values never enter ``draft``, so their questions
        # remain intact.
        if self.draft.get(name) is not None:
            return f"Skipped: {name} already has a normalized value."
        self.questions.append(
            {
                "field": name,
                "kind": ASKED,
                "why": cloud_contract.why(name),
                "question": text,
                "seen": [r for r in self.rejected if r["field"] == name],
                **self._question_ui(name),
            }
        )
        self.trace.append({"action": "ask_user", "field": name, "question": text})
        return f"Recorded a question about {name}. The user will see it."

    def said(self, text: str) -> None:
        """도구 없이 산문으로 끝냈다 — 그 산문이 되읽기다.

        실측(2026-07-29): 진짜 모델은 칸을 다 채우고 나면 `finish`를 부르는 대신 요약을
        내놓고 멈추는 일이 있다. 그걸 버리면 **확인이 통째로 사라진다** — 되읽기는 이
        단계가 사용자에게 돌려주는 유일한 확인 수단이라 형식이 어긋났다고 버릴 것이 아니다.

        같은 실측에서 그 요약이 `{"understanding": "…", "finish": {}}`로 나온 판이 있었다.
        **도구 호출을 산문으로 흘린 것**이라 껍데기만 벗긴다 — 자연어를 뜯어보는 것이
        아니라 잘못 나온 호출을 되돌리는 것이고, JSON이 아니면 손대지 않는다.

        `finish`가 이미 채웠으면 덮지 않는다. 그쪽은 계약 검사를 통과한 되읽기다.
        """
        if self.understanding:
            return
        body = (text or "").strip()
        try:
            payload = json.loads(body)
        except ValueError:
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("understanding"), str):
            body = payload["understanding"].strip()
        self.understanding = body

    def finish(self, understanding: str) -> str:
        """모든 미충족 필수값을 질문했을 때만 resource 수집을 종료한다."""

        pending = [
            n
            for n in cloud_contract.missing_fields(self.draft)
            if n not in {q["field"] for q in self.questions}
        ]
        if pending:
            # **못 채웠으면서 묻지도 않은 채로 끝낼 수는 없다.** 그러면 빈 칸이 사용자에게
            # 보이지 않고, 뒤 단계는 그것이 왜 없는지 영영 모른다.
            return (
                "Not finished: "
                + ", ".join(pending)
                + " are required but neither filled nor asked about. "
                "Fill them or ask the user."
            )
        self.understanding = (understanding or "").strip()
        self.finished = True
        self.trace.append({"action": "finish"})
        return "Done."


# --- 지각 -------------------------------------------------------------------
def perceive_resource_inputs(state: AgentState) -> tuple[list[str], str]:
    """에이전트가 볼 것 — (인용 대조용 건초더미, 사람이 읽을 브리핑).

    제약 원문·요구사항 문장·앞선 되묻기의 답이 전부다. 셋을 **갈라서** 보여 주는 이유는
    실측이다: provider·region·예산은 요구사항 산문에 0건이고, 섞어 놓으면 모델이 없는
    곳을 뒤진다.

    Args:
        state: 자유문장, 구조화 intake, 분류 요구사항과 기존 답변을 담은 상태다.

    Returns:
        근거 대조용 원문 목록과 LLM에 전달할 구획화된 briefing이다.

    Notes:
        구조화 intake와 사용자 답변의 우선순위를 표시하되 값 자체를 정규화하지 않는다.
    """
    constraints = (state.get("resource_constraints_text") or "").strip()
    initial = dict(state.get("initial_cloud_constraints") or {})
    sentences = [
        f"[{item.get('id') or '?'}] {(item.get('text') or '').strip()}"
        for item in (state.get("classified") or [])
        if (item.get("text") or "").strip()
    ]
    answers = {
        k: str(v).strip()
        for k, v in (state.get("resource_answers") or {}).items()
        if str(v or "").strip()
    }
    free_text_answers = [
        {
            "expected_field": str(item.get("expected_field") or "").strip(),
            "text": str(item.get("text") or "").strip(),
        }
        for item in (state.get("resource_free_text_answers") or [])
        if isinstance(item, dict) and str(item.get("text") or "").strip()
    ]

    # 모델은 답변 블록을 읽고 `provider: azure`처럼 필드 이름까지 포함해 인용하는
    # 경향이 있다. 그 문자열은 실제로 모델에게 보여 준 사용자 답변 표현이므로 원문 값과
    # 함께 근거 후보에 넣는다. 값만 넣으면 정직한 인용도 환각으로 오판한다.
    rendered_answers = [f"{key}: {value}" for key, value in answers.items()]
    rendered_initial = [
        f"{key}: {value}" for key, value in initial.items() if str(value or "").strip()
    ]
    haystack = [
        constraints,
        *sentences,
        *(str(value) for value in initial.values()),
        *rendered_initial,
        *answers.values(),
        *rendered_answers,
        *(item["text"] for item in free_text_answers),
    ]
    parts = [
        "# Structured cloud constraints supplied at intake",
        "\n".join(rendered_initial) or "(none)",
        "These values were entered in dedicated fields and outrank free-form prose.",
        "",
        "# Cloud constraints the user wrote (separate from the requirements)",
        constraints or "(the user gave none — every required field will have to be asked)",
        "",
        "# The requirement sentences",
        "\n".join(sentences) or "(none)",
    ]
    if answers:
        parts += [
            "",
            "# Answers the user already gave to earlier questions",
            "\n".join(rendered_answers),
            "These are the user's own words about that field — they outrank the prose. "
            "They still have to be resolved and checked like anything else "
            '("Seoul" is still not a region code).',
        ]
    if free_text_answers:
        parts += [
            "",
            "# Free-form answers to resource questions",
            "\n".join(
                f"Answer to {item['expected_field']}: {item['text']}"
                for item in free_text_answers
            ),
            "Interpret these as user evidence for all relevant cloud constraints; "
            "the named question is context, not a single-field assignment.",
        ]
    return [h for h in haystack if h], "\n".join(parts)


# --- 루프 -------------------------------------------------------------------
def _control_tools(session: ResourceIntakeSession) -> list:
    """이번 실행의 상태를 바꾸는 행동들. 세션에 묶여 있어 실행마다 새로 만든다."""
    from langchain_core.tools import tool

    @tool
    def record_field(field: str, value: str, evidence: str) -> str:
        """Put one value into the draft RESOURCE_SPEC.

        Args:
            field: the contract field name (provider, region, regionAsWritten,
                monthlyBudgetUSD, minVCpu, minMemoryGiB, scale, trafficPattern,
                dataResidency, …). Call check_contract if unsure.
            value: the value, as a plain string. Numbers without separators.
                An object field takes a JSON object — `scale` is
                `{"value": 300, "unit": "concurrentUsers"}` (or
                `"requestsPerSecond"`). **Do not convert between the two units**:
                record whichever one the user actually stated.
            evidence: the fragment you read it from — verbatim from the user's text
                or from a tool result you already received. Paraphrases are rejected.
        """
        return session.record(field, value, evidence)

    @tool
    def check_contract() -> str:
        """Report what the contract still requires, and why each field is needed.

        The reasons come from the contract itself. Use them when you ask the user —
        a question without a reason gets answered with any old value.
        """
        return session.contract_status()

    @tool
    def ask_user(field: str, question: str) -> str:
        """Ask the user one question about one field. A normal action, not a failure.

        Use it when the value is absent, when two readings disagree, when a place name
        resolves to several regions, or when you want the user to confirm a reading.

        Args:
            field: the contract field the answer will fill.
            question: what to ask, in the user's vocabulary, including why it is needed.
        """
        return session.ask(field, question)

    @tool
    def finish(understanding: str) -> str:
        """End your turn.

        Args:
            understanding: one short paragraph saying back what you understood, in the
                user's own words, so a misreading can be caught. If you had to convert
                a currency or resolve a place name, say what it became and on what basis.
        """
        return session.finish(understanding)

    return [record_field, check_contract, ask_user, finish]


def _text_of(content: object) -> str:
    """`AIMessage.content`(문자열 또는 파트 리스트)를 평문으로."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part if isinstance(part, str) else str(part.get("text", ""))
            for part in content
            if isinstance(part, str | dict)
        )
    return ""


def _run(session: ResourceIntakeSession, briefing: str) -> None:
    """에이전트를 돌린다. 어느 도구를 언제 부를지는 **모델이 정한다.**

    여기서 코드가 정하는 것은 셋뿐이다: 언제 멈추는가(`finish`·도구 호출 없음·턴 상한),
    도구 결과를 어떻게 되먹이는가, 그리고 실패를 어떻게 기록하는가.
    """
    from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

    from app.requirements.runtime.structured_llm import build_llm

    tools = [*LOOKUP_TOOLS, *_control_tools(session)]
    by_name = {t.name: t for t in tools}
    llm = build_llm().bind_tools(tools)

    messages: list = [
        SystemMessage(content=prompts.RESOURCE_AGENT_SYSTEM),
        HumanMessage(content=briefing),
    ]
    for _turn in range(max(1, settings.resource_agent_max_turns)):
        with telemetry.record_llm_call("resource_agent") as call:
            reply = llm.invoke(messages)
            call.observe_usage(getattr(reply, "usage_metadata", None))
            call.observe_metadata(getattr(reply, "response_metadata", None))
        messages.append(reply)

        calls = getattr(reply, "tool_calls", None) or []
        if not calls:
            # 도구 없이 산문만 냈다. 그것도 정지 조건이다 — **말로 한 것은 기록이
            # 아니므로** 초안에 반영하지 않되, 산문 자체는 되읽기로 남긴다(`said`).
            # 계약이 못 채운 칸은 어차피 아래에서 질문이 된다.
            session.said(_text_of(reply.content))
            break

        for request in calls:
            name = request.get("name", "")
            tool_obj = by_name.get(name)
            if tool_obj is None:
                result = f"No such tool: {name!r}. Available: {', '.join(by_name)}."
            else:
                try:
                    result = str(tool_obj.invoke(request.get("args") or {}))
                except Exception as exc:  # noqa: BLE001 — 도구 실패는 관찰이지 종료가 아니다
                    result = f"Tool {name} failed: {type(exc).__name__}: {exc}"
                    telemetry.record_degradation(f"resource_agent.{name}", result)
                else:
                    session.saw(result)
            messages.append(ToolMessage(content=result, tool_call_id=request.get("id") or name))
        if session.finished:
            break


def extract_resource_constraints(
    state: AgentState,
    *,
    proposal_call: ResourceConstraintProposalCall | None = None,
) -> ResourceStagePatch:
    """자유문장 제약의 LLM 해석만 수행해 병렬 실행 가능한 중간 결과를 만든다.

    구조화된 CSP·리전·예산은 이 함수의 대상이 아니다. 이 중간 결과는 질문이나
    RESOURCE_SPEC을 확정하지 않으며, ``build_resource_spec``이 근거 대조와 계약 검증을
    수행한다. 단독 호출 경로에서는 기존처럼 ``build_resource_spec``이 직접 추출한다.

    Args:
        state: 분류 요구사항과 자유문장 클라우드 제약을 담은 단계 상태다.
        proposal_call: 테스트나 상위 adapter가 주입하는 한 번짜리 구조화 호출이다.

    Returns:
        성공·실패·비활성 상태와 typed proposal JSON을 담은 중간 state patch다.

    Notes:
        구조화 intake 필드는 이 호출에서 제외한다. LLM 비활성 시 0회, 활성 시 1회라는
        기존 호출 범위와 failure degradation 형식을 보존한다.
    """
    # 전용 필드로 받은 CSP·리전·예산은 결정론 경로가 이미 처리한다. LLM에는 자유문장과
    # 요구사항만 보여 같은 값을 다시 추출·확인하는 호출 낭비를 막는다.
    extraction_state = dict(state)
    extraction_state.pop("initial_cloud_constraints", None)
    _haystack, briefing = perceive_resource_inputs(extraction_state)  # type: ignore[arg-type]
    if not settings.resource_agent_llm:
        return {
            "resource_constraint_extraction": {
                "status": "disabled",
                "degraded": (
                    "The resource constraint LLM is disabled; no constraints were extracted."
                ),
            }
        }
    # 구조화 입력과 이전 질문의 답은 아래 build 단계가 field를 이미 알고 처리한다.
    # 자유문장과 요구사항이 모두 없으면 모델이 읽을 근거도 없으므로 빈 proposal을 바로 낸다.
    has_natural_input = bool(
        str(extraction_state.get("resource_constraints_text") or "").strip()
        or any(
            str(item.get("text") or "").strip()
            for item in extraction_state.get("classified") or []
        )
    )
    if not has_natural_input:
        return {
            "resource_constraint_extraction": {
                "status": "completed",
                "result": CloudConstraintExtraction().model_dump(mode="json"),
            }
        }
    try:
        found = propose_resource_constraints(briefing, proposal_call=proposal_call)
    except Exception as exc:  # noqa: BLE001 - 최종 단계가 질문으로 안전하게 강등한다
        return {
            "resource_constraint_extraction": {
                "status": "failed",
                "degraded": f"{type(exc).__name__}: {exc}",
            }
        }
    return {
        "resource_constraint_extraction": {
            "status": "completed",
            "result": found.model_dump(mode="json"),
        }
    }


def normalize_resource_extraction(
    session: ResourceIntakeSession,
    found: CloudConstraintExtraction,
    *,
    protected_fields: frozenset[str] = frozenset(),
) -> None:
    """LLM proposal을 근거 대조와 결정론 규칙으로 초안에 정규화한다.

    Args:
        session: 정규화 결과와 provenance를 누적할 현재 intake 세션이다.
        found: 아직 수락되지 않은 구조화 제약 proposal이다.
        protected_fields: 구조화 intake가 선점해 proposal로 덮을 수 없는 필드다.

    Returns:
        반환값 없이 세션의 초안·질문·근거·거절 내역을 갱신한다.

    Notes:
        이 함수는 계약 전체의 최종 유효성을 판정하지 않는다. ``build_resource_spec``이
        정규화 뒤 ``cloud_contract.validate``를 한 번 적용한다.
    """
    direct = (
        ("provider", found.provider, found.provider_evidence),
        ("minVCpu", found.min_vcpu, found.min_vcpu_evidence),
        ("minMemoryGiB", found.min_memory_gib, found.min_memory_evidence),
        ("trafficPattern", found.traffic_pattern, found.traffic_pattern_evidence),
        ("dataResidency", found.data_residency, found.data_residency_evidence),
    )
    for field, value, evidence in direct:
        if field not in protected_fields and value is not None:
            session.record(field, value, evidence)

    if "region" not in protected_fields and found.region_as_written is not None:
        session.record("regionAsWritten", found.region_as_written, found.region_evidence)
        matches = regions.resolve(
            found.region_as_written,
            provider=str(session.draft.get("provider") or "") or None,
        )
        if len(matches) == 1:
            match = matches[0]
            tool_result = f"{match.code} ({match.provider}, {match.display_name})"
            session.saw(tool_result)
            session.record("region", match.code, match.code)
        elif len(matches) != 1:
            session.ask("region", cloud_contract.question("region"))

    if (
        "monthlyBudgetUSD" not in protected_fields
        and found.monthly_budget_amount is not None
    ):
        currency = (found.monthly_budget_currency or "").upper()
        if currency == "USD":
            session.record(
                "monthlyBudgetUSD",
                found.monthly_budget_amount,
                found.monthly_budget_evidence,
            )
        elif currency:
            conversion = str(
                convert_to_usd.invoke(
                    {
                        "amount": found.monthly_budget_amount,
                        "currency": currency,
                    }
                )
            )
            session.saw(conversion)
            try:
                usd = json.loads(conversion)["usd"]
            except (json.JSONDecodeError, KeyError, TypeError):
                session.ask("monthlyBudgetUSD", cloud_contract.question("monthlyBudgetUSD"))
            else:
                session.record("monthlyBudgetUSD", usd, str(usd))

    if found.scale_value is not None and found.scale_unit is not None:
        session.record(
            "scale",
            {"value": found.scale_value, "unit": found.scale_unit},
            found.scale_evidence,
        )

    for field in found.ambiguous_fields:
        if (
            field not in protected_fields
            and field in cloud_contract.schema_fields()
            and field != "workloads"
        ):
            session.ask(field, cloud_contract.question(field) or f"Please confirm {field}.")
    session.understanding = found.understanding.strip()


def normalize_initial_cloud_constraints(
    session: ResourceIntakeSession, initial: dict
) -> None:
    """구조화 intake 필드를 LLM 없이 동일한 초안 규칙으로 정규화한다.

    Args:
        session: 정규화 결과를 누적할 현재 intake 세션이다.
        initial: HTTP 요청에서 받은 구조화 cloud input 사전이다.

    Returns:
        반환값 없이 세션의 초안과 provenance를 갱신한다.

    Notes:
        provider·region·budget은 LLM을 우회하지만 카탈로그 해소와 계약 검증을 우회하지
        않는다. 복수 deployment target의 입력 순서도 그대로 보존한다.
    """
    if not initial:
        return

    targets = [dict(item) for item in initial.get("targets") or [] if isinstance(item, dict)]
    primary = targets[0] if targets else {}
    provider = str(primary.get("provider") or initial.get("provider") or "").strip().lower()
    if provider:
        session.record("provider", provider, provider)

    region = str(primary.get("region") or initial.get("region") or "").strip()
    if region:
        session.record("regionAsWritten", region, region)
        matches = regions.resolve(region, provider=provider or None)
        if len(matches) == 1:
            match = matches[0]
            tool_result = f"{match.code} ({match.provider}, {match.display_name})"
            session.saw(tool_result)
            session.record("region", match.code, match.code)
        else:
            session.ask("region", cloud_contract.question("region"))

    if targets:
        normalized_targets = [
            {
                "provider": str(target.get("provider") or "").strip().lower(),
                "region": str(target.get("region") or "").strip(),
                "zones": list(
                    dict.fromkeys(
                        str(zone).strip() for zone in target.get("zones") or [] if str(zone).strip()
                    )
                ),
            }
            for target in targets
        ]
        session.draft["deploymentTargets"] = normalized_targets
        session.provenance.append(
            Candidate(
                "deploymentTargets",
                normalized_targets,
                json.dumps(normalized_targets, ensure_ascii=False),
                "agent",
                "user",
            )
        )
        session.trace.append(
            {
                "action": "record_structured_intake",
                "field": "deploymentTargets",
                "value": normalized_targets,
            }
        )

    amount = initial.get("monthly_budget_amount")
    currency = str(initial.get("monthly_budget_currency") or "USD").strip().upper()
    if amount is None:
        return
    if currency == "USD":
        session.record("monthlyBudgetUSD", amount, str(amount))
        return

    conversion = str(convert_to_usd.invoke({"amount": amount, "currency": currency}))
    session.saw(conversion)
    try:
        usd = json.loads(conversion)["usd"]
    except (json.JSONDecodeError, KeyError, TypeError):
        session.ask("monthlyBudgetUSD", cloud_contract.question("monthlyBudgetUSD"))
    else:
        session.record("monthlyBudgetUSD", usd, str(usd))


def normalize_resource_answers(
    session: ResourceIntakeSession, answers: dict[str, str]
) -> None:
    """화면이 질문의 ``field``와 함께 보낸 답변을 초안에 바로 적용한다.

    답변은 어떤 필드에 대한 값인지 이미 알고 있으므로 LLM이 다시 분류할 필요가 없다.
    특히 이전 자유문장 추출 결과를 재사용하는 재개 실행에서도 답변이 사라지지 않아야 한다.
    region은 사용자가 장소 이름을 답할 수도 있어 기존 region catalog로 정규화한다.
    """

    for field, raw_value in answers.items():
        value = str(raw_value or "").strip()
        if not value or field not in cloud_contract.schema_fields() or field == "workloads":
            continue
        if field == "provider":
            session.record(field, value.lower(), value)
            continue
        if field == "region":
            session.record("regionAsWritten", value, value)
            matches = regions.resolve(
                value,
                provider=str(session.draft.get("provider") or "") or None,
            )
            if len(matches) == 1:
                match = matches[0]
                session.saw(f"{match.code} ({match.provider}, {match.display_name})")
                session.record("region", match.code, match.code)
            else:
                session.ask("region", cloud_contract.question("region"))
            continue
        session.record(field, value, value)


@contract("build_resource_spec", requires=("classified",), produces=("resource_intake",))
def build_resource_spec(
    state: AgentState,
    *,
    proposal_call: ResourceConstraintProposalCall | None = None,
) -> ResourceStagePatch:
    """사용자의 클라우드 제약을 검증된 ``RESOURCE_SPEC``으로 가져온다.

    Args:
        state: 분류 요구사항, 구조화 intake, 자유문장과 기존 답변을 담은 단계 상태다.
        proposal_call: 자유문장을 읽을 구조화 proposal 호출을 선택적으로 주입한다.

    Returns:
        항상 ``resource_intake``를 포함하고 계약 통과 시 ``resource_spec``도 포함하는
        기존 graph-state patch다.

    Notes:
        proposal을 근거 대조·정규화한 뒤 결정론적 계약 검증을 수행한다. 누락·모호한
        값은 질문 목록으로만 보정하며 추가 LLM repair 호출을 만들지 않는다.
    """
    haystack, briefing = perceive_resource_inputs(state)
    session = ResourceIntakeSession(haystack)
    initial_cloud_constraints = dict(state.get("initial_cloud_constraints") or {})
    normalize_initial_cloud_constraints(session, initial_cloud_constraints)
    resource_answers = {
        str(field): str(value)
        for field, value in (state.get("resource_answers") or {}).items()
        if str(value or "").strip()
    }
    normalize_resource_answers(session, resource_answers)
    # 화면의 전용 입력 칸에서 받은 값은 자유문장을 읽은 LLM 제안보다 우선한다. 예전에는
    # 복수 target 형식만 보호해서, 단일 provider/region을 정확히 입력해도 LLM이 같은
    # 값을 ambiguous로 표시하면 이미 채운 region을 다시 묻는 문제가 있었다.
    protected = {
        field
        for field, source_key in (
            ("provider", "provider"),
            ("region", "region"),
            ("monthlyBudgetUSD", "monthly_budget_amount"),
        )
        if initial_cloud_constraints.get(source_key) not in (None, "")
    }
    if initial_cloud_constraints.get("targets"):
        protected.update({"provider", "region"})
    # A direct answer protects a field only after deterministic normalization
    # accepted it. For example, an unresolvable region response must remain
    # repairable by the contextual extraction rather than masking its evidence.
    protected.update(
        field
        for field in resource_answers
        if session.draft.get(field) not in (None, "")
    )
    protected_fields = frozenset(protected)

    degraded = ""
    cached = state.get("resource_constraint_extraction")
    if cached is not None:
        cached = dict(cached)
        if cached.get("status") == "completed":
            normalize_resource_extraction(
                session,
                CloudConstraintExtraction.model_validate(cached.get("result") or {}),
                protected_fields=protected_fields,
            )
        else:
            degraded = str(cached.get("degraded") or "Cloud constraint extraction failed.")
    elif not settings.resource_agent_llm:
        degraded = "The resource constraint LLM is disabled; no constraints were extracted."
    else:
        try:
            normalize_resource_extraction(
                session,
                propose_resource_constraints(
                    briefing, proposal_call=proposal_call
                ),
                protected_fields=protected_fields,
            )
        except Exception as exc:  # noqa: BLE001 — 못 읽었으면 못 읽었다고 남기고 되묻는다
            degraded = f"{type(exc).__name__}: {exc}"
    if degraded:
        telemetry.record_degradation("resource_intake.agent", degraded)

    # 못 채운 필수 칸은 **계약이 적어 둔 이유를 그대로** 되묻는다. 에이전트가 이미
    # 물었으면 겹치지 않는다. 에이전트가 죽거나 잊어도 이 질문은 나간다 — 되묻기가
    # 사용자에게 가느냐는 모델의 재량에 맡길 것이 아니다.
    asked = {q["field"] for q in session.questions}
    for name in cloud_contract.missing_fields(session.draft):
        if name in asked:
            continue
        session.questions.append(
            {
                "field": name,
                "kind": MISSING,
                "why": cloud_contract.why(name),
                # 사용자에게 하는 **말**과 그것이 필요한 **이유**는 다른 것이다.
                # 예전에는 이유만 있어서 영어 근거 문장이 그대로 화면에 나갔다.
                "question": cloud_contract.question(name)
                or f"A value for {name} is required: {cloud_contract.why(name)}",
                "seen": [r for r in session.rejected if r["field"] == name],
                **session._question_ui(name),
            }
        )
    # 권고 칸은 **막지 않는다.** 계약을 만족시키는 데는 필요 없고, 답하면 뒤 단계
    # 판정이 하나씩 열린다. 이것들이 없어도 `resource_spec`은 나간다.
    capacity_known = any(
        session.draft.get(name) is not None for name in ("minVCpu", "minMemoryGiB")
    )
    for name in cloud_contract.suggested_fields(session.draft):
        # 임시 용량 하한만 후속 입력으로 받는다. trafficPattern 같은 선택 맥락은
        # 사용자가 요구사항에 명시했을 때 추출하되, 모든 사용자에게 선제 질문하지 않는다.
        if name not in {"minVCpu", "minMemoryGiB"} or capacity_known:
            continue
        if name in asked:
            continue
        session.questions.append(
            {
                "field": name,
                "kind": SUGGESTED,
                "why": cloud_contract.why(name),
                "question": cloud_contract.question(name),
                "seen": [],
            }
        )

    errors = cloud_contract.validate(session.draft)
    intake = {
        "draft": session.draft,
        "valid": not errors,
        "errors": errors,
        "questions": session.questions,
        # 에이전트가 되읽은 이해. **확인은 질문과 다른 일이다** — 빈 칸을 채우는 것이
        # 아니라 채운 칸을 잘못 읽지 않았는지 사용자가 볼 수 있게 하는 것이다.
        "understanding": session.understanding,
        # 근거에 출처가 이미 붙어 있으므로 출처 목록을 따로 싣지 않는다.
        "provenance": [c.as_dict() for c in session.provenance],
        # 버린 값을 남긴다 — 빈 칸이 "정보가 없어서"인지 "우리가 버려서"인지 구별된다.
        "rejected": session.rejected,
        # 무엇을 했는지. 데모·디버깅이 읽는 자리다.
        "trace": session.trace,
    }
    if degraded:
        intake["degraded"] = degraded

    result: ResourceStagePatch = {
        "resource_intake": intake,
        "phase": "resource_spec",
    }
    # **계약을 만족할 때만 산출물이 존재한다.** 반쯤 채운 사양을 내보내면 뒤 단계가 그것을
    # 사양으로 알고 조인을 돌린다 — 값이 없는 것보다 나쁘다.
    if not errors:
        result["resource_spec"] = dict(session.draft)
    return result
