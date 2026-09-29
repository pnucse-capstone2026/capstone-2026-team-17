"""단계가 읽어야 하는 상태 키를 선언하고, 없으면 크게 실패시킨다.

파이프라인 단계들이 하나의 평면 상태를 공유하면서 전부 `state.get("x") or []` 로
읽는다. 그래서 **"키가 아예 없다"와 "비어 있다"가 같은 값이 된다.**

  - 키 부재  = 상류 단계가 돌지 않았다 (배선 오류)
  - 빈 목록  = 상류가 돌았고 결과가 없었다 (정상일 수 있다)

앞엣것을 조용히 빈 산출물로 넘기면 cascade에서 한 단계가 통째로 빠져도 아무도 모른다.
결과물은 "유스케이스가 0개인 분석"처럼 보이고, 그건 진짜로 0개인 실행과 구별되지 않는다.

그래서 부재는 예외로 만든다. 이건 런타임 조건이 아니라 배선 오류이고, 배선 오류는
조용히 넘어가면 안 된다. 빈 목록은 그대로 통과시킨다 — 그건 판단할 문제가 아니다.

타입(TypedDict)으로는 이걸 못 잡는다. TypedDict는 런타임에 아무것도 검사하지 않고,
단계들은 어차피 같은 dict를 서로 넘긴다. 계약은 실행돼야 계약이다.
"""
from __future__ import annotations

import functools
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any


class MissingUpstreamState(RuntimeError):
    """단계가 요구하는 상태 키가 없다 — 상류가 돌지 않았다는 뜻."""

    def __init__(self, stage: str, missing: list[str]) -> None:
        self.stage = stage
        self.missing = missing
        super().__init__(
            f"{stage}: 상류 산출물이 없다 {missing}. "
            "빈 값이 아니라 키 자체가 없다 — 앞 단계가 돌지 않았거나 상태 배선이 끊겼다."
        )


def require(state: Mapping[str, Any], *keys: str, stage: str) -> None:
    """`keys`가 상태에 **존재하는지** 본다(값이 비었는지는 보지 않는다)."""
    missing = [k for k in keys if k not in state]
    if missing:
        raise MissingUpstreamState(stage, missing)


def require_any(state: Mapping[str, Any], *keys: str, stage: str) -> None:
    """`keys` 중 하나라도 있으면 통과. 대체 가능한 입력이 있는 단계용.

    예: 분류는 구체화된 요구(refined_requirements)를 쓰되 없으면 원문(raw_requirements)을
    쓴다. 둘 다 없을 때만 배선이 끊긴 것이다.
    """
    if not any(k in state for k in keys):
        raise MissingUpstreamState(stage, list(keys))


class BrokenStageOutput(RuntimeError):
    """단계가 내겠다고 선언한 키를 내지 않았다 — 그 단계 안의 결함이다."""

    def __init__(self, stage: str, missing: list[str]) -> None:
        self.stage = stage
        self.missing = missing
        super().__init__(
            f"{stage}: 내겠다고 선언한 산출물이 없다 {missing}. "
            "하류가 이 키를 요구하므로, 지금 실패하지 않으면 상류 배선 오류로 잘못 보고된다."
        )


@dataclass(frozen=True)
class StateContract:
    """한 단계가 상태에서 무엇을 **읽고** 무엇을 **내는지**에 대한 선언."""

    stage: str
    requires: tuple[str, ...] = ()
    #: 이 중 하나만 있으면 되는 대체 입력.
    requires_any: tuple[str, ...] = field(default_factory=tuple)
    #: 이 단계가 상태에 내놓는 산출물 키. 파이프라인 배선을 정적으로 검사하는 근거다(§15).
    #:
    #: 선언은 **하한**이다 — 기록용 키(`phase`)를 더 내는 것은 괜찮다. 정확히 일치를
    #: 요구하면 선언이 산출물의 사본이 되고, 사본은 갈린다.
    produces: tuple[str, ...] = field(default_factory=tuple)

    def check(self, state: Mapping[str, Any]) -> None:
        if self.requires:
            require(state, *self.requires, stage=self.stage)
        if self.requires_any:
            require_any(state, *self.requires_any, stage=self.stage)

    def check_output(self, result: Any) -> None:
        """단계가 선언한 산출물을 실제로 냈는지."""
        if not self.produces or not isinstance(result, Mapping):
            return
        missing = [k for k in self.produces if k not in result]
        if missing:
            raise BrokenStageOutput(self.stage, missing)


def contract(
    stage: str,
    *,
    requires: Sequence[str] = (),
    requires_any: Sequence[str] = (),
    produces: Sequence[str] = (),
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """단계 함수에 상태 계약을 **선언부에** 붙인다.

    본문에 `require(...)`를 적어도 동작은 같지만, 그러면 "이 단계가 무엇을 읽는가"가
    11개 함수 본문에 흩어진다. 파이프라인을 재편할 때 — 단계를 쪼개거나 합치거나 다른
    오케스트레이터로 옮길 때 — 본문을 전부 뒤져야 한다는 뜻이다.

    선언으로 두면 두 가지가 생긴다:
      1. 사람이 함수 시그니처만 보고 입력 의존을 안다.
      2. **기계가 읽을 수 있다** — `state_contract_of(fn)`으로 꺼내므로, 그래프 조립이나
         문서 생성이 같은 사실을 두 번째로 적지 않아도 된다.

    계약은 함수에 붙으므로 그래프를 거치든 직접 부르든 항상 적용된다. 피드백 cascade는
    단계 함수를 직접 부르는데, 거기서 빠지면 계약의 의미가 없다.
    """
    spec = StateContract(
        stage=stage,
        requires=tuple(requires),
        requires_any=tuple(requires_any),
        produces=tuple(produces),
    )

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def guarded(state: Mapping[str, Any], *args: Any, **kwargs: Any) -> Any:
            spec.check(state)
            result = fn(state, *args, **kwargs)
            # 들어올 때만 보고 나갈 때는 안 보면, 아무것도 안 낸 단계의 잘못이
            # **하류 단계의 이름으로** 보고된다.
            spec.check_output(result)
            return result

        guarded.state_contract = spec  # type: ignore[attr-defined]
        return guarded

    return decorate


def state_contract_of(fn: Callable[..., Any]) -> StateContract | None:
    """함수에 붙은 계약을 꺼낸다(없으면 None)."""
    return getattr(fn, "state_contract", None)
