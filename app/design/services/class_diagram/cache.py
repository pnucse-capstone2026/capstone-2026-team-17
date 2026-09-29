"""수락된 클래스 설계 단위만 재사용하는 프로세스 로컬 캐시다.

LLM의 원시 응답, validation finding, repair 중간 후보는 이 모듈에 넣지 않는다. 호출자는
Pydantic과 결정론 검사를 통과한 값만 ``get_or_compute``에 반환해야 하며, cache hit에서도
같은 검사를 다시 수행한다. 따라서 캐시는 호출 수를 줄일 뿐 수락 경계를 바꾸지 않는다.
"""
from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import asdict, dataclass, is_dataclass
from threading import Event, RLock
from typing import Any, Generic, Literal, Protocol, TypeVar

T = TypeVar("T")
CacheStatus = Literal["hit", "miss", "coalesced"]
MAX_ACCEPTED_UNIT_CACHE_ENTRIES = 256


class AcceptedUnitCacheMiss(LookupError):
    """sealed cache에서 provider 계산이 필요함을 나타낸다."""

    def __init__(self, key: str) -> None:
        super().__init__(f"accepted unit cache miss while sealed: {key}")
        self.key = key


def _canonical_value(value: Any) -> Any:
    """digest 입력을 순서와 구현 객체에 영향받지 않는 JSON 값으로 바꾼다."""

    if isinstance(value, Mapping):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized = [_canonical_value(item) for item in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(
                item, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ),
        )
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return _canonical_value(dump(by_alias=True, mode="json"))
    if is_dataclass(value) and not isinstance(value, type):
        return _canonical_value(asdict(value))
    return value


def canonical_digest_key(namespace: str, value: Any) -> str:
    """JSON canonical form의 SHA-256 digest를 안정적인 cache key로 만든다."""

    encoded = json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"{namespace}:{hashlib.sha256(encoded).hexdigest()}"


def configured_provider_identity(base_url: str | None) -> str:
    """OpenAI-compatible endpoint identity를 원문 노출 없는 digest로 만든다."""

    return canonical_digest_key(
        "easydep.class-diagram.provider",
        {"kind": "openai-compatible", "baseUrl": base_url or "provider-default"},
    )


CACHE_VERSION_DIGEST = canonical_digest_key(
    "easydep.class-diagram.cache-version", {"version": 1},
)


def accepted_unit_key(
    unit: str,
    *,
    unit_slice: Any,
    inventory: Any,
    feedback: Any,
    prompt: str,
    schema: Any,
    provider: str,
    model: str,
    seed: int | None,
    temperature: float | int | None,
    reasoning_effort: str | None,
    max_completion_tokens: int | None,
    extra: Mapping[str, Any] | None = None,
) -> str:
    """수락 단위의 LLM·검사 입력 전체를 담은 digest key를 만든다.

    prompt와 schema는 원문 대신 각 digest를 넣어 key를 작게 유지한다. ``unit_slice``와
    ``inventory``는 이미 caller가 해당 unit 범위로 투영한 값이므로 서로 다른 reservation,
    feedback 또는 execution slice가 같은 결과를 잘못 공유하지 않는다.
    """

    schema_payload = (
        schema.model_json_schema() if callable(getattr(schema, "model_json_schema", None))
        else schema
    )
    return canonical_digest_key(
        "easydep.class-diagram.accepted-unit",
        {
            "cacheVersionDigest": CACHE_VERSION_DIGEST,
            "unit": unit,
            "normalizedSlice": unit_slice,
            "normalizedInventory": inventory,
            "normalizedFeedback": feedback,
            "promptDigest": canonical_digest_key(
                "easydep.class-diagram.prompt", prompt,
            ),
            "schemaDigest": canonical_digest_key(
                "easydep.class-diagram.schema", schema_payload,
            ),
            "provider": provider,
            "model": model,
            "seed": seed,
            "temperature": temperature,
            "reasoningEffort": reasoning_effort,
            "maxCompletionTokens": max_completion_tokens,
            "extra": dict(extra or {}),
        },
    )


@dataclass(frozen=True)
class CacheResult(Generic[T]):
    """방어적으로 복사한 값과 해당 호출의 cache 상태다."""

    value: T
    status: CacheStatus
    key: str


class AcceptedUnitCache(Protocol):
    """수락된 단위의 중복 계산을 합칠 수 있는 최소 cache 경계다."""

    def get_or_compute(
        self, key: str, compute: Callable[[], T],
    ) -> CacheResult[T]:
        """key의 수락 결과를 읽거나 현재 호출 한 번만 계산한다."""


@dataclass
class _InFlight:
    """동일 key를 기다리는 호출자가 공유하는 완료 신호다."""

    event: Event
    value: Any = None
    error: BaseException | None = None


class ProcessLocalAcceptedUnitCache:
    """최대 256개 accepted value를 보관하는 thread-safe LRU cache다."""

    def __init__(self, capacity: int = MAX_ACCEPTED_UNIT_CACHE_ENTRIES) -> None:
        if not 1 <= capacity <= MAX_ACCEPTED_UNIT_CACHE_ENTRIES:
            raise ValueError(
                "accepted unit cache capacity must be between 1 and "
                f"{MAX_ACCEPTED_UNIT_CACHE_ENTRIES}"
            )
        self._capacity = capacity
        self._values: OrderedDict[str, Any] = OrderedDict()
        self._inflight: dict[str, _InFlight] = {}
        self._lock = RLock()
        self._sealed = False

    @property
    def capacity(self) -> int:
        """현재 LRU의 고정 최대 항목 수다."""

        return self._capacity

    def clear(self) -> None:
        """완료된 accepted value만 비운다. 진행 중 계산은 취소하지 않는다."""

        with self._lock:
            self._values.clear()

    def seal(self) -> None:
        """새 계산을 금지해 warm 검증이 provider를 호출하기 전에 실패하게 한다."""

        with self._lock:
            if self._inflight:
                raise RuntimeError("cannot seal accepted unit cache with in-flight work")
            self._sealed = True

    def get_or_compute(
        self, key: str, compute: Callable[[], T],
    ) -> CacheResult[T]:
        """동일 key의 계산을 single-flight로 합치고 성공한 값만 LRU에 저장한다."""

        with self._lock:
            if key in self._values:
                value = self._values.pop(key)
                self._values[key] = value
                return CacheResult(deepcopy(value), "hit", key)
            if self._sealed:
                raise AcceptedUnitCacheMiss(key)
            flight = self._inflight.get(key)
            owner = flight is None
            if owner:
                flight = _InFlight(Event())
                self._inflight[key] = flight

        assert flight is not None
        if not owner:
            flight.event.wait()
            if flight.error is not None:
                raise flight.error
            return CacheResult(deepcopy(flight.value), "coalesced", key)

        try:
            # ``compute``가 예외를 내면 value를 넣지 않는다. 따라서 failure나 partial
            # repair 후보는 cache에 남지 않는다.
            computed = compute()
            stored = deepcopy(computed)
        except BaseException as error:
            with self._lock:
                current = self._inflight.pop(key, flight)
                current.error = error
                current.event.set()
            raise

        with self._lock:
            self._values[key] = stored
            self._values.move_to_end(key)
            while len(self._values) > self._capacity:
                self._values.popitem(last=False)
            current = self._inflight.pop(key, flight)
            current.value = deepcopy(stored)
            current.event.set()
        return CacheResult(deepcopy(stored), "miss", key)


def record_cache_outcome(
    result: CacheResult[Any] | None,
    *,
    operation: str,
    unit: str,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    """공개 timing hook이 있으면 cache 상태를 관측하고 없으면 조용히 넘긴다."""

    try:
        from app.design.services.common import structured

        recorder = getattr(structured, "record_llm_timing", None)
        if not callable(recorder):
            return
        details = {
            "cacheUnit": unit,
            "cacheKey": result.key if result is not None else None,
            "cacheStatus": result.status if result is not None else "bypass",
        } | dict(metadata or {})
        recorder(
            operation,
            status=f"cache_{details['cacheStatus']}",
            metadata=details,
        )
    except Exception:
        # 측정은 생성/repair의 결과를 바꾸거나 새 실패를 만들 수 없다.
        return


__all__ = [
    "CACHE_VERSION_DIGEST",
    "MAX_ACCEPTED_UNIT_CACHE_ENTRIES",
    "AcceptedUnitCache",
    "AcceptedUnitCacheMiss",
    "CacheResult",
    "ProcessLocalAcceptedUnitCache",
    "accepted_unit_key",
    "canonical_digest_key",
    "configured_provider_identity",
    "record_cache_outcome",
]
