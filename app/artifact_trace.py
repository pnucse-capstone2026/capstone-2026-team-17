"""단계와 저장 방식에 묶이지 않는 산출물 추적 projection.

처음 이 모듈을 읽는 사람은 ``TraceRef``를 산출물의 "주소", ``TraceNode``를
그 주소와 바로 앞 출처를 묶은 카드라고 생각하면 된다. ``ArtifactTrace``는 이
카드들을 읽기 전용으로 모아, 어느 산출물이 무엇을 직접 사용했고 어디까지
거슬러 올라가는지만 보여 준다.

기존 단계의 ID는 서로 우연히 같을 수 있다. 그래서 항상 ``kind:id`` 모양의
``TraceRef``로 감싼다. 예를 들어 ``file:src/api.py:18``과
``operation:GET:/users/{id}:detail``은 첫 번째 콜론만 구분자로 쓰므로 경로와
operation ID 안의 나머지 콜론을 잃지 않는다.

이것은 DB나 graph framework가 아니라, 호출자가 넘긴 작은 산출물 목록을
결정론적으로 읽는 순수 projection이다. 알 수 없는 출처도 오류로 지우지 않고
원래 참조를 보존해 나중에 누락을 확인할 수 있게 한다.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True, order=True)
class TraceRef:
    """종류를 붙여 충돌 없이 식별한 산출물 참조.

    ``id``에는 파일의 줄 위치나 HTTP operation처럼 콜론을 포함한 기존 ID를
    그대로 넣을 수 있다. ``kind``와 ``id``를 비워 두면 어느 산출물인지
    설명할 수 없으므로 생성 시 바로 막는다.
    """

    kind: str
    id: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind:
            raise ValueError("TraceRef.kind must be a non-empty string")
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("TraceRef.id must be a non-empty string")

    @classmethod
    def parse(cls, value: str) -> TraceRef:
        """``kind:id`` 문자열을 읽는다.

        첫 번째 콜론만 나누므로 ``file:src/a.py:12``의 ID는
        ``src/a.py:12``로, ``operation:GET:/items:by-id``의 ID는
        ``GET:/items:by-id``로 보존된다.
        """
        if not isinstance(value, str):
            raise TypeError("TraceRef text must be a string")
        kind, separator, identifier = value.partition(":")
        if not separator:
            raise ValueError("TraceRef text must have the form 'kind:id'")
        return cls(kind=kind, id=identifier)

    def format(self) -> str:
        """다른 단계에 전달하거나 화면에 보일 ``kind:id`` 문자열을 만든다."""
        return f"{self.kind}:{self.id}"

    def __str__(self) -> str:
        return self.format()


def parse_ref(value: str) -> TraceRef:
    """문자열 참조를 읽는 짧은 함수형 진입점이다."""
    return TraceRef.parse(value)


def format_ref(ref: TraceRef) -> str:
    """typed 참조를 안정적인 ``kind:id`` 문자열로 바꾼다."""
    if not isinstance(ref, TraceRef):
        raise TypeError("ref must be a TraceRef")
    return ref.format()


@dataclass(frozen=True)
class TraceNode:
    """한 산출물과 그 산출물이 직접 사용한 출처 참조들.

    이 구조에는 관계 이름이나 표시용 메타데이터를 넣지 않는다. 방향은 언제나
    ``ref -> direct_sources``이며, 그것만으로 소비자와 상·하류를 계산할 수 있다.
    """

    ref: TraceRef
    direct_sources: tuple[TraceRef, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.ref, TraceRef):
            raise TypeError("TraceNode.ref must be a TraceRef")
        if not all(isinstance(source, TraceRef) for source in self.direct_sources):
            raise TypeError("TraceNode.direct_sources must contain TraceRef values")

        # 입력 순서와 중복은 호출자마다 다를 수 있다. projection 결과는 언제나
        # 같아야 하므로 여기서 한 번만 정렬·중복 제거한다.
        object.__setattr__(self, "direct_sources", tuple(sorted(set(self.direct_sources))))


@dataclass(frozen=True, init=False)
class ArtifactTrace:
    """중복 노드를 합친 읽기 전용 산출물 추적표.

    ``nodes``에 같은 ``ref``가 여러 번 있어도 각각의 직접 출처를 합친 단일
    노드가 된다. 존재하지 않는 출처 참조는 노드로 꾸며 내지 않지만
    ``sources``와 ``upstream`` 결과에는 남고 ``unknown_source_refs``에서
    따로 확인할 수 있다.
    """

    nodes: tuple[TraceNode, ...]

    def __init__(self, nodes: Iterable[TraceNode] = ()) -> None:
        merged_sources: dict[TraceRef, set[TraceRef]] = {}
        for node in nodes:
            if not isinstance(node, TraceNode):
                raise TypeError("ArtifactTrace nodes must be TraceNode values")
            merged_sources.setdefault(node.ref, set()).update(node.direct_sources)

        # ``TraceRef``의 kind, id 순서는 한 곳에서 정의되어 있다. 모든 공개
        # 조회가 같은 기준을 쓰도록 저장 순서도 여기서 고정한다.
        normalized = tuple(
            TraceNode(ref=ref, direct_sources=tuple(sources))
            for ref, sources in sorted(merged_sources.items())
        )
        object.__setattr__(self, "nodes", normalized)

    @property
    def refs(self) -> tuple[TraceRef, ...]:
        """정의된 산출물 참조를 안정된 순서로 돌려준다."""
        return tuple(node.ref for node in self.nodes)

    @property
    def unknown_source_refs(self) -> tuple[TraceRef, ...]:
        """정의된 노드가 없는 직접 출처를 버리지 않고 보고한다."""
        known = frozenset(self.refs)
        return tuple(
            sorted(
                {
                    source
                    for node in self.nodes
                    for source in node.direct_sources
                    if source not in known
                }
            )
        )

    def sources(self, ref: TraceRef) -> tuple[TraceRef, ...]:
        """``ref``가 직접 사용한 출처들을 돌려준다."""
        self._require_ref(ref)
        for node in self.nodes:
            if node.ref == ref:
                return node.direct_sources
        return ()

    def consumers(self, ref: TraceRef) -> tuple[TraceRef, ...]:
        """``ref``를 직접 출처로 든 산출물들을 돌려준다."""
        self._require_ref(ref)
        return tuple(sorted(node.ref for node in self.nodes if ref in node.direct_sources))

    def upstream(self, ref: TraceRef) -> tuple[TraceRef, ...]:
        """직접·간접 출처를 모두 돌려준다. 알 수 없는 출처도 포함한다."""
        self._require_ref(ref)
        seen: set[TraceRef] = set()
        pending = list(self.sources(ref))
        while pending:
            current = pending.pop()
            if current == ref or current in seen:
                continue
            seen.add(current)
            pending.extend(self.sources(current))
        return tuple(sorted(seen))

    def downstream(self, ref: TraceRef) -> tuple[TraceRef, ...]:
        """``ref``를 직·간접으로 소비하는 모든 산출물을 돌려준다."""
        self._require_ref(ref)
        seen: set[TraceRef] = set()
        pending = list(self.consumers(ref))
        while pending:
            current = pending.pop()
            if current == ref or current in seen:
                continue
            seen.add(current)
            pending.extend(self.consumers(current))
        return tuple(sorted(seen))

    def files(self, ref: TraceRef | None = None) -> tuple[TraceRef, ...]:
        """전체 또는 한 항목과 연결된 구현 파일 참조를 돌려준다."""
        return self._refs_of_kinds({"file"}, ref)

    def evidence(self, ref: TraceRef | None = None) -> tuple[TraceRef, ...]:
        """전체 또는 한 항목과 연결된 테스트·finding·실행 증거를 돌려준다."""
        return self._refs_of_kinds({"test", "finding", "evidence"}, ref)

    def _refs_of_kinds(
        self, kinds: set[str], ref: TraceRef | None
    ) -> tuple[TraceRef, ...]:
        if ref is None:
            candidates = {
                candidate
                for node in self.nodes
                for candidate in (node.ref, *node.direct_sources)
            }
        else:
            self._require_ref(ref)
            # 요구사항에서 파일·테스트를 찾을 때는 downstream이 필요하고, 실패나
            # 파일에서 연관 대상을 찾을 때는 upstream도 필요하다. 둘을 함께 보아도
            # 이미 연결된 항목만 포함되므로 이름 유사성으로 범위를 넓히지 않는다.
            candidates = {ref, *self.upstream(ref), *self.downstream(ref)}
        return tuple(sorted(candidate for candidate in candidates if candidate.kind in kinds))

    @staticmethod
    def _require_ref(ref: TraceRef) -> None:
        if not isinstance(ref, TraceRef):
            raise TypeError("ref must be a TraceRef")
