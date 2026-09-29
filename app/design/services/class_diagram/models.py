"""클래스 설계 단계 사이에서만 교환하는 수락된 불변 작업 단위다.

``AcceptedInventory``와 ``AcceptedFragment``는 LLM 제안을 정규화·검증한 뒤 다음 단계로
넘기는 경계다. frozen dataclass와 방어적 복사로 worker가 형제 작업의 입력을 바꾸지 못하게
한다.

이 타입들은 영속 JSON schema가 아니다. 외부 저장 계약은 ``schemas.class_model.BCEModel``이
소유하며, 여기에는 repair 횟수·prompt·telemetry 같은 실행 정보가 들어가지 않는다.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any


class GenerationStalled(RuntimeError):
    """A bounded repair loop could not produce an accepted design unit."""

    def __init__(self, unit_id: str, finding: str) -> None:
        self.unit_id = unit_id
        self.finding = finding
        self.repair_context: dict[str, Any] | None = None
        super().__init__(
            f"valid class-design candidate was not produced for {unit_id}; "
            f"last finding: {finding}"
        )


class ClassBindingStalled(GenerationStalled):
    """A use-case binding stayed unavailable after bounded owning-unit repair."""

    def __init__(
        self, unit_id: str, finding: str, repair_context: Mapping[str, Any],
    ) -> None:
        super().__init__(unit_id, finding)
        self.repair_context = deepcopy(dict(repair_context))


@dataclass
class RepairBudget:
    """Shared finite allowance for semantic repairs of one design unit."""

    unit_id: str
    attempts: int = 0
    limit: int = 4

    def consume(self, finding: str) -> None:
        if self.attempts >= self.limit:
            raise GenerationStalled(self.unit_id, finding)
        self.attempts += 1


@dataclass(frozen=True)
class Collision(Exception):
    """이미 수락된 연산과 다른 서명이 충돌했음을 나타낸다."""

    class_name: str
    operation_name: str

    def __str__(self) -> str:
        return f"operation signature collision: {self.class_name}.{self.operation_name}"


@dataclass(frozen=True)
class DataTypeCollision(Exception):
    """같은 이름의 로컬 타입 정의가 달라졌음을 나타낸다."""

    type_name: str

    def __str__(self) -> str:
        return f"DataType definition collision: {self.type_name}"


@dataclass(frozen=True)
class AcceptedInventory:
    """구조 단계가 연산 worker 모두에 넘기는 고정 BCE inventory다.

    ``Mapping`` tuple로 보관하고 입출력마다 깊은 복사하여 병렬 fragment 생성 중 구조
    기준이 변하지 않게 한다.
    """

    classes: tuple[Mapping[str, Any], ...]
    data_types: tuple[Mapping[str, Any], ...]
    relationships: tuple[Mapping[str, Any], ...]

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> AcceptedInventory:
        return cls(
            classes=tuple(deepcopy(item) for item in payload.get("Classes") or []),
            data_types=tuple(deepcopy(item) for item in payload.get("DataTypes") or []),
            relationships=tuple(
                deepcopy(item) for item in payload.get("Relationships") or []
            ),
        )

    def as_payload(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "Classes": [deepcopy(dict(item)) for item in self.classes],
            "DataTypes": [deepcopy(dict(item)) for item in self.data_types],
            "Relationships": [deepcopy(dict(item)) for item in self.relationships],
        }


@dataclass(frozen=True)
class AcceptedFragment:
    """한 유스케이스만 교체 권한을 가진 수락 operation·local type 조각이다."""

    use_case_id: str
    payload: Mapping[str, Any]

    def as_payload(self) -> dict[str, Any]:
        return deepcopy(dict(self.payload))


@dataclass(frozen=True)
class CallDependency:
    """호출 트리에서 class PlantUML에만 파생하는 한 클래스 간 의존선이다.

    저장 ``Relationships``에 추가하지 않으므로 renderer용 정보가 도메인 구조 계약을
    오염시키지 않는다.
    """

    source: str
    target: str
    type: str = "Dependency"

    def as_payload(self) -> dict[str, str]:
        return {"source": self.source, "target": self.target, "type": self.type}

    def get(self, key: str, default: str | None = None) -> str | None:
        """기존 UML 투영기가 읽는 최소 매핑 접근을 제공한다."""
        return self.as_payload().get(key, default)
