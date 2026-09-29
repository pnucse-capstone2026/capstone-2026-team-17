"""실행 중인 workspace command의 중간 다이어그램을 메모리에 보관한다.

live preview는 정식 artifact가 아니다. 생성 도중 브라우저에 보여 주기 위한 값이므로 DB에
버전을 만들지 않고 현재 process 안에만 저장한다. 서버가 재시작되면 사라지는 것이 정상이며,
완료된 artifact의 복구나 이력 조회에 사용하면 안 된다.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from threading import RLock
from time import monotonic

TERMINAL_TTL_SECONDS = 600.0


@dataclass(frozen=True)
class LivePreview:
    """한 command가 특정 stage에서 만든 최신 preview 한 건이다.

    frozen dataclass를 사용해 조회자가 값을 직접 바꾸지 못하게 한다. SVG cache나 만료 시각을
    갱신할 때는 `replace()`로 새 객체를 만들어 store 안에서 교체한다.
    """
    app_id: str
    command_id: str
    stage: str
    revision: int
    puml: str
    phase: str
    unit: str
    completed: int
    total: int
    image_svg: bytes | None = None
    expires_at: float | None = None


class LivePreviewStore:
    """여러 worker thread가 함께 사용할 수 있는 process-local preview 저장소다."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str, str], LivePreview] = {}
        # 클래스 생성 callback과 HTTP 요청 thread가 동시에 접근할 수 있으므로 dict의 조회와
        # 변경을 같은 reentrant lock으로 보호한다.
        self._lock = RLock()

    @staticmethod
    def _key(app_id: str, command_id: str, stage: str) -> tuple[str, str, str]:
        """서로 다른 앱·명령·단계의 preview가 덮어써지지 않게 하는 key를 만든다."""
        return app_id, command_id, stage

    def _remove_expired(self) -> None:
        """완료 후 보존 시간이 지난 preview를 다음 접근 시점에 정리한다."""
        now = monotonic()
        for key, item in list(self._items.items()):
            if item.expires_at is not None and item.expires_at <= now:
                self._items.pop(key, None)

    def publish(
        self,
        *,
        app_id: str,
        command_id: str,
        stage: str,
        puml: str,
        phase: str,
        unit: str = "",
        completed: int = 0,
        total: int = 0,
    ) -> LivePreview:
        """새 PlantUML preview를 저장하고 revision을 하나 증가시킨다.

        빈 문자열은 UI에서 “아직 없음”과 구분할 수 없으므로 저장하지 않는다. 같은 key의
        preview가 새로 오면 이전 SVG cache는 버리고 새 PlantUML에 맞는 revision을 부여한다.
        """
        if not puml.strip():
            raise ValueError("A live preview requires non-empty PlantUML.")
        key = self._key(app_id, command_id, stage)
        with self._lock:
            self._remove_expired()
            current = self._items.get(key)
            item = LivePreview(
                app_id=app_id,
                command_id=command_id,
                stage=stage,
                revision=(current.revision + 1 if current else 1),
                puml=puml,
                phase=phase,
                unit=unit,
                completed=max(0, completed),
                total=max(0, total),
            )
            self._items[key] = item
            return item

    def get(self, app_id: str, command_id: str, stage: str) -> LivePreview | None:
        """만료된 항목을 정리한 뒤 요청한 최신 preview를 반환한다."""
        with self._lock:
            self._remove_expired()
            return self._items.get(self._key(app_id, command_id, stage))

    def cache_svg(
        self, app_id: str, command_id: str, stage: str, revision: int, image: bytes,
    ) -> None:
        """PlantUML을 렌더링한 SVG를 같은 revision에만 연결한다.

        렌더링 도중 더 새 preview가 publish될 수 있다. revision 확인 없이 저장하면 새
        PlantUML에 이전 SVG가 붙는 race condition이 생기므로 일치할 때만 갱신한다.
        """
        key = self._key(app_id, command_id, stage)
        with self._lock:
            current = self._items.get(key)
            if current is not None and current.revision == revision:
                self._items[key] = replace(current, image_svg=image)

    def mark_terminal(self, app_id: str, command_id: str) -> None:
        """command 종료 후에도 UI가 마지막 preview를 잠시 읽을 수 있게 만료 시각을 건다."""
        with self._lock:
            deadline = monotonic() + TERMINAL_TTL_SECONDS
            for key, item in list(self._items.items()):
                if item.app_id == app_id and item.command_id == command_id:
                    self._items[key] = replace(item, expires_at=deadline)

    def clear(self) -> None:
        """테스트나 process 종료 준비에서 메모리 preview를 모두 비운다."""
        with self._lock:
            self._items.clear()


live_previews = LivePreviewStore()
