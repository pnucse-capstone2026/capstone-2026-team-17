"""`RESOURCE_SPEC` 계약의 접근점 — 무엇을 받아야 하고 **왜** 받아야 하는가.

사실이 두 곳에 산다. 그 갈래가 이 모듈이 하는 일의 전부다:

    request.json           값의 **모양**과 필수 여부 (기계 검증)
    input_registry.py      **질문·근거·소비자·계층·선행조건** (사람에게 묻는 층)

**2026-08-01에 뒤쪽이 생겼다.** 그전에는 `REQUIRED_WHY`·`SUGGESTED_WHY`가
`appkb/contract.py`에 손으로 적혀 있었고, 그래서 (1) 어느 칸이 왜 있는지 근거를
댈 수 없었고 (2) 같은 종류의 질문을 하는 관심사 29건과 서로를 몰랐고 (3) 앵커가
정해진 뒤에만 물을 수 있는 것(depkb 결정)을 담을 자리가 없었다. 지금은
레지스트리가 그 셋을 한 항목의 칸으로 들고 있고, 이 모듈은 **그것을 요구사항
에이전트가 쓰던 이름으로 열어 준다.**

`schema_fields()`를 노출하는 이유는 별도다: 관심사의 소비자 선언이 실재하는
칸인지 테스트가 확인하는데, 그 목록을 손으로 적어 두면 스키마가 바뀔 때 검사가
거짓이 된다.
"""
from __future__ import annotations

from functools import lru_cache

from app.requirements.resources import input_registry
from app.requirements.resources import resource_contract as _contract


@lru_cache(maxsize=1)
def schema_fields() -> frozenset[str]:
    """`RESOURCE_SPEC`이 아는 칸 이름 전부(스키마에서 읽는다)."""
    return frozenset(_contract.request_schema().get("properties", {}))


@lru_cache(maxsize=1)
def _properties() -> dict[str, dict]:
    return dict(_contract.request_schema().get("properties", {}))


def schema_version() -> str:
    """계약 판 — 스키마의 `const`. **생산자가 옮겨 적지 않게 하려고 연다.**

    손으로 적으면 판을 올릴 때 생산자 쪽이 조용히 낡고, 그러면 산출물이 계약을
    통과하지 못하는 이유가 값이 아니라 판 표시가 된다.
    """
    return str(_properties().get("schemaVersion", {}).get("const", ""))


def field_type(field: str) -> str:
    """칸의 타입(`string`·`number`·`integer`·`boolean`·`array`). `enum` 칸이면 `"enum"`.

    **생산자가 스키마 사실을 옮겨 적지 않게 하려고 연다.** 이것이 없으면 부르는 쪽이
    "이 칸은 문자열", "이 칸은 steady|spiky"를 자기 목록으로 다시 적게 되고, 실제로 그
    목록이 스키마와 어긋났다(`regionAsWritten`이 빠져 있었다).
    """
    spec = _properties().get(field)
    if not spec:
        return ""
    if "enum" in spec:
        return "enum"
    kind = spec.get("type", "")
    return kind if isinstance(kind, str) else ""


def field_enum(field: str) -> tuple[str, ...]:
    """`enum` 칸이 허용하는 값들. 아니면 빈 튜플."""
    return tuple(_properties().get(field, {}).get("enum", ()))


def field_object(field: str) -> tuple[tuple[str, str, tuple[str, ...], bool], ...]:
    """`object` 칸의 하위 칸들 — `(이름, 타입, enum, 필수)`. object가 아니면 빈 튜플.

    `field_type`·`field_enum`과 같은 이유로 연다: 생산자가 스키마 사실을 옮겨
    적으면 조용히 갈린다. `scale{value,unit}`이 2026-08-01에 계약의 첫 object 칸이
    됐고, 그 하위 모양을 아는 곳이 하나여야 한다.
    """
    spec = _properties().get(field) or {}
    if spec.get("type") != "object":
        return ()
    required = set(spec.get("required", ()))
    out = []
    for name, sub in (spec.get("properties") or {}).items():
        kind = "enum" if "enum" in sub else str(sub.get("type", ""))
        out.append((name, kind, tuple(sub.get("enum", ())), name in required))
    return tuple(out)


def validate(spec: dict) -> list[str]:
    """계약 검증. 빈 목록이면 통과.

    문제를 예외가 아니라 **목록으로** 돌려주는 성질을 그대로 물려받는다 — 상류가
    에이전트라 첫 오류에서 멈추면 고치고 다시를 반복하게 된다.

    누락 메시지에 **왜**를 붙이는 것이 여기서 하는 일이다. 스키마 층은 모양만
    보므로 `[required] provider missing`까지만 말하고, 되묻기 문구가 될 이유는
    레지스트리가 안다 — 무엇이 왜 필요한지 없이 되물으면 사용자가 임의로 채운다.
    """
    problems = _contract.validate_request(spec)
    out: list[str] = []
    for line in problems:
        name = line.removeprefix("[required] ").removesuffix(" missing")
        why = why_of(name) if line.startswith("[required] ") else ""
        out.append(f"{line} — {why}" if why else line)
    return out


def missing_fields(spec: dict) -> tuple[str, ...]:
    """아직 못 채운 **필수** 칸 — 검증 메시지가 아니라 칸 이름으로.

    권고 칸은 `suggested_fields()`가 따로 준다 — 둘을 섞으면 사용자가 전부
    필수로 읽는다.
    """
    return tuple(a.spec_field
                 for a in input_registry.missing(spec, input_registry.REQUIRED))


def suggested_fields(spec: dict) -> tuple[str, ...]:
    """비어 있는 **권고** 칸 — 채우면 판정이 하나 열린다.

    쌍 처리(규모 신호 둘·하한 둘은 한쪽만 있으면 더 안 묻는다)는 레지스트리가 한다.
    """
    return tuple(a.spec_field
                 for a in input_registry.missing(spec, input_registry.SUGGESTED))


def context_fields(spec: dict) -> tuple[str, ...]:
    """비어 있는 **맥락** 칸 — 판정을 열진 않지만 계획에 실린다.

    2026-08-01에 생긴 갈래다. 그전에는 이 칸들이 아무 계층에도 없어서 *"물을 수
    있는데 안 묻는 것"*과 *"물 필요가 없는 것"*이 구별되지 않았다.
    """
    return tuple(a.spec_field
                 for a in input_registry.missing(spec, input_registry.CONTEXT))


def why(field: str) -> str:
    """그 칸이 왜 필요한가 — 레지스트리의 `opens`(무엇이 이 값으로 열리는가).

    **모르는 칸에 그럴듯한 이유를 지어내지 않는다** — 되묻기 문구는 레지스트리가
    실제로 적어 둔 소비자일 때만 값이 있다.
    """
    return why_of(field)


def why_of(field: str) -> str:
    """registry가 아는 field라면 그 값이 여는 후속 판단을 설명한다."""

    ask = input_registry.by_field().get(field)
    return ask.opens if ask else ""


def question(field: str) -> str:
    """그 칸을 사용자에게 물을 때의 **말**. 모르는 칸이면 빈 문자열.

    `why()`와 갈라 두는 이유: 하나는 사용자에게 하는 질문이고 하나는 그 질문이
    필요한 이유다. 예전에는 이유만 있어서 되묻기가 영어 근거 문장을 그대로
    사용자에게 내보냈다.
    """
    ask = input_registry.by_field().get(field)
    return ask.question if ask else ""


def choices(field: str, csp: str = "") -> tuple[str, ...]:
    """그 칸이 받는 값들. 스키마 enum이거나, CSP에 매인 목록(앵커)이다."""
    ask = input_registry.by_field().get(field)
    if ask is None:
        return field_enum(field)
    return input_registry.choices_for(ask, csp)
