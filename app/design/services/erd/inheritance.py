"""상속 관계의 **처리 순서와 옮길 수 있는지**를 정한다 — 그래프 문제이지 사상이 아니다.

`mapping.py`에서 떼어냈다. 나머지 사상은 관계 하나하나를 표와 컬럼으로 옮기는 일인데
여기는 **관계들 사이의 그래프**(위상 정렬·순환 탐지)를 본다. 성격이 달라서 경계를 긋는다.

**`mapping.py`를 import하지 않는다.** 여기 있는 코드는 표도 컬럼도 안 보고 관계 dict의
클래스 이름만 보므로, 의존이 `mapping → inheritance` 한 방향으로만 선다.

## 왜 순서가 문제인가

상속은 이 사상에서 **이미 만든 표를 뜯어고치는 유일한 연산**이다 — 자식의 기본키를 부모의
것으로 갈아 끼운다. 그래서 자식을 가리키는 외래키는 전부 그 뒤에 만들어져야 하고, 사슬
(`C → B → A`)에서 부모 쪽이 나중에 처리되면 `C`의 외래키가 없는 칸을 가리키게 된다.
"""
from __future__ import annotations

#: 부모가 둘 이상이다. **관계형에 다중 상속이 없다** — 하나를 골라 옮기면 그건 우리가
#: 조용히 정하는 것이므로, 하나도 옮기지 않고 전부 드러낸다.
UNMAPPED_MULTIPLE_INHERITANCE = "multiple-inheritance"
#: 상속이 순환한다. 어떤 전략으로도 첫 행을 만들 수 없다.
UNMAPPED_INHERITANCE_CYCLE = "inheritance-cycle"


def order_for_mapping(
    relationships: list[dict],
) -> tuple[list[dict], list[tuple[dict, str]]]:
    """관계를 `(사상할 것, [(못 옮길 관계, 사유)])`로 가른다.

    **상속이 먼저, 그중에서도 부모가 먼저.** 그 이유는 모듈 docstring에 있다.

    **옮길 수 없는 상속도 여기서 가려낸다** — 판정을 사상과 두 곳에 흩어 놓지 않는다.
    부모가 둘 이상이거나 순환하면 하나도 옮기지 않고 사유와 함께 돌려준다.
    """
    inheritance = [r for r in relationships if str(r.get("type")) == "Inheritance"]
    others = [r for r in relationships if str(r.get("type")) != "Inheritance"]

    #: 자식 → 부모들. **목록이다** — dict 하나면 부모가 둘일 때 조용히 하나만 남는다.
    parents: dict[str, list[str]] = {}
    for relationship in inheritance:
        parents.setdefault(str(relationship.get("source")), []).append(
            str(relationship.get("target"))
        )

    rejected: list[tuple[dict, str]] = []
    keep: list[dict] = []
    for relationship in inheritance:
        child = str(relationship.get("source"))
        if len(parents[child]) > 1:
            rejected.append((relationship, UNMAPPED_MULTIPLE_INHERITANCE))
        elif _reaches_itself(child, parents):
            rejected.append((relationship, UNMAPPED_INHERITANCE_CYCLE))
        else:
            keep.append(relationship)

    def depth(relationship: dict) -> int:
        """부모를 몇 번 거슬러 올라가야 뿌리인가. 깊이가 얕은 것부터 처리한다.

        **거쳐 온 곳을 표시하며 올라간다.** `parents`에는 거절된 상속도 그대로 남아
        있어서, 순환 **밖에서 순환 안으로** 들어가는 상속(`A↔B`가 있고 `C→A`)이 여기
        걸린다. `C→A`는 자기 자신을 다시 만나지 않으므로 `_reaches_itself`를 통과해
        `keep`에 남는데, 그 조상 사슬은 `A → B → A → …`로 끝이 없다. 표시가 없던 동안
        이 함수는 안 돌아왔고, 순수 함수라 타임아웃도 없어 `check_erd` 노드가 통째로
        멈췄다.

        멈춘 자리에서 깊이를 그냥 돌려준다. **`C→A`를 거절하지 않는 것은 일부러다** —
        순환은 `A↔B` 쪽에서 이미 지적되고, 거기 딸린 상속까지 싸잡아 거절하면 실수
        하나가 지적 여럿이 되어 재생성이 위반 수를 못 줄인다.
        """
        node, steps = str(relationship.get("target")), 0
        seen: set[str] = set()
        while parents.get(node) and len(parents[node]) == 1 and node not in seen:
            seen.add(node)
            node, steps = parents[node][0], steps + 1
        return steps

    return sorted(keep, key=depth) + others, rejected


def _reaches_itself(start: str, parents: dict[str, list[str]]) -> bool:
    """`start`에서 부모를 따라 올라가다 자기 자신을 다시 만나는가."""
    seen: set[str] = set()
    frontier = list(parents.get(start, ()))
    while frontier:
        node = frontier.pop()
        if node == start:
            return True
        if node in seen:
            continue
        seen.add(node)
        frontier.extend(parents.get(node, ()))
    return False
