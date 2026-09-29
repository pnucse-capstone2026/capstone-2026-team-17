"""Requirements와 design 그래프가 공유하는 graph-scoped SQL 체크포인터.

LangGraph 규약에 따라 checkpoint 본문, 채널 blob, pending write는 세 표로 나누되,
단계마다 표를 복제하지 않고 ``graph_type``을 모든 기본키의 첫 열로 사용한다.
"""
from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_id,
    get_checkpoint_metadata,
)
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from sqlalchemy import delete, select, tuple_

from app.db.models import AgentCheckpoint, AgentCheckpointBlob, AgentCheckpointWrite
from app.db.session import session_scope

_serde = JsonPlusSerializer()

#: `dumps_typed`가 값 없음을 표시하는 타입. `loads_typed`는 이걸 모르므로 걸러내야 한다.
_EMPTY = "empty"


def _dump(value: Any) -> tuple[str, bytes]:
    """직렬화하고 **바이트로 굳힌다.**

    `dumps_typed`는 페이로드에 따라 `memoryview`를 돌려준다. 그걸 그대로 바인딩하면
    바탕 버퍼가 먼저 풀렸을 때 드라이버가 "bad parameter or other API misuse"로 죽는다.
    GC 압력이 있어야 나타나므로 단위 테스트로는 안 보이고 부하가 걸린 뒤에 터진다.
    """
    type_, payload = _serde.dumps_typed(value)
    return type_, bytes(payload)


class SqlCheckpointSaver(BaseCheckpointSaver):
    """산출물 저장소와 같은 MySQL에 체크포인트를 쓰는 LangGraph 체크포인터.

    `InMemorySaver`와 **같은 순서로** 넣고 꺼낸다 — 채널 값은 버전별 blob으로 쪼개고,
    체크포인트 뼈대는 `channel_versions`만 들고 있다가 읽을 때 blob에서 되살린다.
    이 규약이 어긋나면 재개가 조용히 어긋난다(예외가 아니라 잘못된 상태로 이어진다).

    ``graph_type``은 같은 테이블 안에서 requirements와 design의 키 공간을 분리한다.
    """

    def __init__(
        self,
        graph_type: str,
    ) -> None:
        super().__init__(serde=_serde)
        if not graph_type or len(graph_type) > 16:
            raise ValueError("graph_type must be between 1 and 16 characters")
        self.graph_type = graph_type
        self._checkpoint = AgentCheckpoint
        self._blob = AgentCheckpointBlob
        self._write = AgentCheckpointWrite

    # -- 쓰기 ---------------------------------------------------------------
    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        skeleton = checkpoint.copy()
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        values: dict[str, Any] = skeleton.pop("channel_values")  # type: ignore[misc]

        checkpoint_type, checkpoint_bytes = _dump(skeleton)
        metadata_type, metadata_bytes = _dump(get_checkpoint_metadata(config, metadata))

        with session_scope() as db:
            for channel, version in new_versions.items():
                # 값이 없는 채널도 자리를 남긴다 — 나중에 "버전은 있는데 blob이 없다"와
                # "값이 비어 있다"를 구별해야 한다.
                blob_type, blob = (
                    _dump(values[channel]) if channel in values else (_EMPTY, b"")
                )
                db.merge(
                    self._blob(
                        graph_type=self.graph_type,
                        thread_id=thread_id,
                        checkpoint_ns=checkpoint_ns,
                        channel=channel,
                        version=str(version),
                        blob_type=blob_type,
                        blob=blob,
                    )
                )
            db.merge(
                self._checkpoint(
                    graph_type=self.graph_type,
                    thread_id=thread_id,
                    checkpoint_ns=checkpoint_ns,
                    checkpoint_id=checkpoint["id"],
                    parent_checkpoint_id=config["configurable"].get("checkpoint_id"),
                    checkpoint_type=checkpoint_type,
                    checkpoint=checkpoint_bytes,
                    metadata_type=metadata_type,
                    checkpoint_metadata=metadata_bytes,
                )
            )
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint["id"],
            }
        }

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = config["configurable"]["checkpoint_id"]

        with session_scope() as db:
            for position, (channel, value) in enumerate(writes):
                idx = WRITES_IDX_MAP.get(channel, position)
                write_type, blob = _dump(value)
                row = self._write(
                    graph_type=self.graph_type,
                    thread_id=thread_id,
                    checkpoint_ns=checkpoint_ns,
                    checkpoint_id=checkpoint_id,
                    task_id=task_id,
                    idx=idx,
                    channel=channel,
                    write_type=write_type,
                    blob=blob,
                    task_path=task_path,
                )
                if idx >= 0:
                    # 일반 쓰기는 먼저 쓴 것이 이긴다(같은 태스크의 재시도가 덮지 않게).
                    # 음수 idx(에러·인터럽트 등 특수 채널)는 최신이 이긴다.
                    existing = db.get(
                        self._write,
                        (
                            self.graph_type,
                            thread_id,
                            checkpoint_ns,
                            checkpoint_id,
                            task_id,
                            idx,
                        ),
                    )
                    if existing is not None:
                        continue
                    db.add(row)
                else:
                    db.merge(row)

    # -- 읽기 ---------------------------------------------------------------
    def _load_blobs(
        self, db, thread_id: str, checkpoint_ns: str, versions: ChannelVersions
    ) -> dict[str, Any]:
        if not versions:
            return {}
        rows = db.scalars(
            select(self._blob).where(
                self._blob.graph_type == self.graph_type,
                self._blob.thread_id == thread_id,
                self._blob.checkpoint_ns == checkpoint_ns,
                tuple_(self._blob.channel, self._blob.version).in_(
                    [(channel, str(version)) for channel, version in versions.items()]
                ),
            )
        ).all()
        by_key = {(r.channel, r.version): r for r in rows}
        values: dict[str, Any] = {}
        for channel, version in versions.items():
            row = by_key.get((channel, str(version)))
            if row is None or row.blob_type == _EMPTY:
                continue
            values[channel] = self.serde.loads_typed((row.blob_type, row.blob or b""))
        return values

    def _pending_writes(
        self, db, thread_id: str, checkpoint_ns: str, checkpoint_id: str
    ) -> list[tuple[str, str, Any]]:
        rows = db.scalars(
            select(self._write)
            .where(
                self._write.graph_type == self.graph_type,
                self._write.thread_id == thread_id,
                self._write.checkpoint_ns == checkpoint_ns,
                self._write.checkpoint_id == checkpoint_id,
            )
            .order_by(self._write.task_id, self._write.idx)
        ).all()
        return [
            (r.task_id, r.channel, self.serde.loads_typed((r.write_type, r.blob)))
            for r in rows
        ]

    def _to_tuple(self, db, row) -> CheckpointTuple:
        skeleton: Checkpoint = self.serde.loads_typed(
            (row.checkpoint_type, row.checkpoint)
        )
        return CheckpointTuple(
            config={
                "configurable": {
                    "thread_id": row.thread_id,
                    "checkpoint_ns": row.checkpoint_ns,
                    "checkpoint_id": row.checkpoint_id,
                }
            },
            checkpoint={
                **skeleton,
                "channel_values": self._load_blobs(
                    db, row.thread_id, row.checkpoint_ns, skeleton["channel_versions"]
                ),
            },
            metadata=self.serde.loads_typed(
                (row.metadata_type, row.checkpoint_metadata)
            ),
            pending_writes=self._pending_writes(
                db, row.thread_id, row.checkpoint_ns, row.checkpoint_id
            ),
            parent_config=(
                {
                    "configurable": {
                        "thread_id": row.thread_id,
                        "checkpoint_ns": row.checkpoint_ns,
                        "checkpoint_id": row.parent_checkpoint_id,
                    }
                }
                if row.parent_checkpoint_id
                else None
            ),
        )

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = get_checkpoint_id(config)

        with session_scope() as db:
            query = select(self._checkpoint).where(
                self._checkpoint.graph_type == self.graph_type,
                self._checkpoint.thread_id == thread_id,
                self._checkpoint.checkpoint_ns == checkpoint_ns,
            )
            if checkpoint_id:
                query = query.where(self._checkpoint.checkpoint_id == checkpoint_id)
            else:
                # id를 안 주면 최신 체크포인트. id는 단조 증가하는 UUIDv6라 정렬이 곧 시간순이다.
                query = query.order_by(self._checkpoint.checkpoint_id.desc()).limit(1)
            row = db.scalars(query).first()
            return None if row is None else self._to_tuple(db, row)

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,  # noqa: A002 - 상위 인터페이스가 정한 이름
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        # 제너레이터로 만들지 않는다. `with session_scope()` 안에서 yield 하면 소비자가
        # 중간에 멈출 때(LangGraph는 흔히 그런다) 세션이 열린 채 남고, 그 커넥션 위에서
        # 다음 put이 "transaction within a transaction"으로 깨진다. 세션 안에서 다 만들고
        # 밖에서 넘긴다 — limit은 SQL에서 걸리므로 메모리는 여전히 유계다.
        with session_scope() as db:
            query = select(self._checkpoint).where(
                self._checkpoint.graph_type == self.graph_type
            )
            if config is not None:
                query = query.where(
                    self._checkpoint.thread_id == config["configurable"]["thread_id"]
                )
                checkpoint_ns = config["configurable"].get("checkpoint_ns")
                if checkpoint_ns is not None:
                    query = query.where(self._checkpoint.checkpoint_ns == checkpoint_ns)
            if before is not None and (bound := get_checkpoint_id(before)):
                query = query.where(self._checkpoint.checkpoint_id < bound)
            query = query.order_by(self._checkpoint.checkpoint_id.desc())
            if limit is not None:
                query = query.limit(limit)

            found = []
            for row in db.scalars(query).all():
                tup = self._to_tuple(db, row)
                if filter and not all(
                    tup.metadata.get(k) == v for k, v in filter.items()
                ):
                    continue
                found.append(tup)
        return iter(found)

    # -- 정리 ---------------------------------------------------------------
    def delete_thread(self, thread_id: str) -> None:
        with session_scope() as db:
            for model in (self._write, self._blob, self._checkpoint):
                db.execute(
                    delete(model).where(
                        model.graph_type == self.graph_type,
                        model.thread_id == thread_id,
                    )
                )
