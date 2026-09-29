"""영구 Workspace 대화는 command에, 실행 중 진행 이벤트는 메모리에 둔다.

이 모듈은 action이 무엇을 실행할지 판단하지 않는다. command의 동시 실행 방지, 상태 저장과
시간 직렬화처럼 데이터베이스에 가까운 규칙만 담당하며 HTTP status code도 결정하지 않는다.
"""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta, timezone
from threading import RLock
from typing import Any

from sqlalchemy import select

from app.db.models import App, WorkspaceCommand
from app.db.session import session_scope

ACTIVE_STATUSES = {"QUEUED", "RUNNING"}
REQUIREMENTS_ARTIFACT_STAGES = {
    "refined_requirements",
    "capability_contract",
    "resource_intake",
    "usecase_spec",
    "usecase_diagram",
    "resource_spec",
}
DESIGN_ARTIFACT_STAGES = {
    "class_diagram",
    "sequence_diagram",
    "api_spec",
    "erd",
    "deployment_diagram",
}

KST = timezone(timedelta(hours=9), name="KST")
_EVENT_LIMIT_PER_APP = 1_000
_event_lock = RLock()
_last_progress_event_id = 0
_events: dict[str, deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=_EVENT_LIMIT_PER_APP))
_TIMELINE_PROGRESS_KEY = "_timeline_progress_cards"


def now() -> datetime:
    """MySQL의 timezone 없는 DATETIME 열에 넣을 UTC 현재 시각을 반환한다."""
    return datetime.now(UTC).replace(tzinfo=None)


def _timestamp_in_kst(value: datetime | None) -> str | None:
    """UTC로 저장한 DATETIME을 timezone이 표시된 한국 시각 문자열로 바꾼다.

    DB 값에는 timezone 정보가 없지만 EasyDep는 UTC로 저장한다는 규칙을 사용한다. 먼저 UTC를
    명시한 뒤 KST로 변환해야 단순히 9시간을 더하면서 생길 수 있는 중복 변환을 피할 수 있다.
    """
    if value is None:
        return None
    return value.replace(tzinfo=UTC).astimezone(KST).isoformat()


def _timeline_event_id(value: datetime, slot: int) -> int:
    """Create sortable numeric IDs without sharing the SSE progress cursor."""

    return int(value.replace(tzinfo=UTC).timestamp() * 1_000_000) * 4 + slot


def workflow_stage(stage: str | None) -> str:
    """세부 artifact stage를 UI가 사용하는 네 개의 큰 stage로 묶는다."""
    if stage in REQUIREMENTS_ARTIFACT_STAGES:
        return "requirements"
    if stage in DESIGN_ARTIFACT_STAGES:
        return "design"
    if stage in {"requirements", "design", "implementation", "testing"}:
        return str(stage)
    return "requirements"


def command_dict(row: WorkspaceCommand) -> dict[str, Any]:
    """ORM command 행을 DB Session 밖에서도 안전하게 쓸 수 있는 dict로 복사한다."""
    return {
        "command_id": row.command_id,
        "app_id": row.app_id,
        "action": row.action,
        "stage": row.stage,
        "status": row.status,
        "payload": row.payload or {},
        "result": row.result,
        "error": row.error,
        "created_at": _timestamp_in_kst(row.created_at),
        "started_at": _timestamp_in_kst(row.started_at),
        "completed_at": _timestamp_in_kst(row.completed_at),
    }


def event_dict(row: Any, *, include_llm_timings: bool = True) -> dict[str, Any]:
    """이전 ORM 형식의 event 객체를 현재 API 형식으로 바꾼다.

    원격 DB 정리 이후 새 event는 메모리에 저장하지만, 기존 호출부와 단위 테스트가 넘기는
    event 모양도 간단히 변환할 수 있도록 이 작은 호환 함수는 유지한다.
    """
    metadata = _event_metadata(
        getattr(row, "event_data", None) or {},
        include_llm_timings=include_llm_timings,
    )
    return {
        "event_id": row.event_id,
        "app_id": row.app_id,
        "command_id": row.command_id,
        "stage": row.stage,
        "kind": row.kind,
        "actor": row.actor,
        "text": row.text,
        "metadata": metadata,
        "created_at": _timestamp_in_kst(row.created_at),
    }


def _event_metadata(metadata: dict[str, Any], *, include_llm_timings: bool) -> dict[str, Any]:
    """목록 응답에서는 큰 LLM 원문 대신 개수만 남긴다."""
    result = dict(metadata)
    if not include_llm_timings and result.get("progress_event") == "designLlmMetrics":
        timings = result.pop("llm_timing_events", [])
        result["llm_timing_count"] = len(timings) if isinstance(timings, list) else 0
    return result


def create_command(
    command_id: str,
    app_id: str,
    action: str,
    stage: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """앱에 활성 command가 없을 때만 새 QUEUED command를 만든다.

    한 앱에서 두 command가 동시에 단계 state를 수정하면 checkpoint와 artifact 버전이 서로
    섞일 수 있다. 따라서 QUEUED 또는 RUNNING command가 있으면 새 command를 거절한다.
    """
    with session_scope() as session:
        # SELECT 후 INSERT만 하면 두 요청이 동시에 "활성 command 없음"을 보고 둘 다
        # 저장할 수 있다. 항상 존재하는 부모 app 행을 잠가 앱별 생성 절차를 직렬화한다.
        app = session.scalar(select(App).where(App.app_id == app_id).with_for_update())
        if app is None:
            raise KeyError(app_id)
        active = session.scalar(
            select(WorkspaceCommand)
            .where(
                WorkspaceCommand.app_id == app_id,
                WorkspaceCommand.status.in_(ACTIVE_STATUSES),
            )
            .limit(1)
        )
        if active is not None:
            raise RuntimeError(f"An active workspace command already exists: {active.command_id}")
        row = WorkspaceCommand(
            command_id=command_id,
            app_id=app_id,
            action=action,
            stage=stage,
            status="QUEUED",
            payload=payload,
        )
        session.add(row)
        session.flush()
        return command_dict(row)


def get_command(command_id: str) -> dict[str, Any] | None:
    """command ID로 한 건을 조회하며 없으면 `None`을 반환한다."""
    with session_scope() as session:
        row = session.get(WorkspaceCommand, command_id)
        return command_dict(row) if row is not None else None


def latest_command(
    app_id: str,
    *,
    exclude_command_id: str | None = None,
    stage: str | None = None,
    status: str | None = None,
) -> dict[str, Any] | None:
    """앱의 가장 최근 command를 조회한다.

    ``stage``를 지정하면 이후 단계가 실행됐더라도 해당 단계의 마지막 결과를 찾는다.
    """
    with session_scope() as session:
        query = select(WorkspaceCommand).where(WorkspaceCommand.app_id == app_id)
        if exclude_command_id:
            query = query.where(WorkspaceCommand.command_id != exclude_command_id)
        if stage:
            query = query.where(WorkspaceCommand.stage == stage)
        if status:
            query = query.where(WorkspaceCommand.status == status)
        row = session.scalar(query.order_by(WorkspaceCommand.created_at.desc()).limit(1))
        return command_dict(row) if row is not None else None


def update_command(command_id: str, **changes: Any) -> dict[str, Any]:
    """command의 지정된 필드만 갱신하고 갱신 직후 snapshot을 반환한다."""
    with session_scope() as session:
        row = session.scalar(
            select(WorkspaceCommand)
            .where(WorkspaceCommand.command_id == command_id)
            .with_for_update()
        )
        if row is None:
            raise KeyError(command_id)
        for key, value in changes.items():
            # Long-running workers still checkpoint an entire payload from a
            # stale in-memory copy. The timeline namespace is server-owned.
            if key == "payload" and isinstance(value, dict):
                existing = row.payload if isinstance(row.payload, dict) else {}
                value = {
                    item_key: item_value
                    for item_key, item_value in value.items()
                    if item_key != _TIMELINE_PROGRESS_KEY
                }
                if _TIMELINE_PROGRESS_KEY in existing:
                    value[_TIMELINE_PROGRESS_KEY] = existing[_TIMELINE_PROGRESS_KEY]
            setattr(row, key, value)
        session.flush()
        return command_dict(row)


def request_stop(app_id: str, command_id: str) -> dict[str, Any]:
    """Durably ask one active command to stop at its next safe boundary.

    The worker owns the terminal transition.  Keeping the request in the
    existing JSON payload avoids a migration while allowing a sleeping retry
    loop (or a restarted reader) to observe it.
    """

    with session_scope() as session:
        row = session.scalar(
            select(WorkspaceCommand)
            .where(
                WorkspaceCommand.command_id == command_id,
                WorkspaceCommand.app_id == app_id,
            )
            .with_for_update()
        )
        if row is None:
            raise KeyError(command_id)
        if str(row.status or "") not in ACTIVE_STATUSES:
            raise RuntimeError("Only an active workspace command can be stopped.")
        payload = dict(row.payload or {})
        payload["_stop_requested"] = True
        row.payload = payload
        session.flush()
        return command_dict(row)


def finish_command_honoring_stop(
    command_id: str,
    *,
    status: str,
    result: dict[str, Any],
    error: str | None,
    cancelled_result: dict[str, Any],
) -> dict[str, Any]:
    """Apply a terminal outcome without allowing a durable stop to lose a race.

    Both this transition and :func:`request_stop` lock the same command row.  A
    stop committed before the terminal writer obtains the lock therefore always
    wins; a later stop sees a terminal command and is correctly rejected.
    """

    with session_scope() as session:
        row = session.scalar(
            select(WorkspaceCommand)
            .where(WorkspaceCommand.command_id == command_id)
            .with_for_update()
        )
        if row is None:
            raise KeyError(command_id)
        stopped = bool((row.payload or {}).get("_stop_requested"))
        row.status = "CANCELLED" if stopped else status
        row.result = cancelled_result if stopped else result
        row.error = None if stopped else error
        row.completed_at = now()
        session.flush()
        return command_dict(row)


def _progress_status(value: object) -> str:
    status = str(value or "").lower()
    if status == "running":
        return "running"
    if status in {"waiting", "pending", "queued"}:
        return "waiting"
    return "completed"


def _safe_progress_task(value: dict[str, Any], *, fallback_order: int = 0) -> dict[str, Any]:
    task_id = str(value.get("id") or "")
    task = {
        "id": task_id,
        "label": str(value.get("label") or task_id),
        "order": int(value.get("order") or fallback_order),
        "status": _progress_status(value.get("status") or "waiting"),
        "detail": str(value.get("detail") or "")[:500],
    }
    parent_id = str(value.get("parent_id") or "")
    if parent_id:
        task["parent_id"] = parent_id
    return task


def _card_status(tasks: list[dict[str, Any]], fallback: object = "waiting") -> str:
    if any(task["status"] == "running" for task in tasks):
        return "running"
    if tasks and all(task["status"] == "completed" for task in tasks):
        return "completed"
    return _progress_status(fallback)


def _safe_progress_card(value: dict[str, Any]) -> dict[str, Any]:
    card_id = str(value.get("id") or "")
    raw_tasks = value.get("tasks") if isinstance(value.get("tasks"), list) else []
    tasks = [
        _safe_progress_task(task, fallback_order=index)
        for index, task in enumerate(raw_tasks)
        if isinstance(task, dict) and str(task.get("id") or "")
    ]
    card = {
        "id": card_id,
        "stage": str(value.get("stage") or "requirements"),
        "order": int(value.get("order") or 0),
        "label": str(value.get("label") or card_id),
        "status": _card_status(tasks, value.get("status")),
        "tasks": tasks,
    }
    if isinstance(value.get("event_id"), int):
        card["event_id"] = value["event_id"]
    if isinstance(value.get("created_at"), str) and value["created_at"]:
        card["created_at"] = value["created_at"]
    return card


def _merge_progress_card(existing: dict[str, Any] | None, patch: dict[str, Any]) -> dict[str, Any]:
    base = _safe_progress_card(existing or patch)
    incoming = _safe_progress_card(patch)
    by_id = {task["id"]: task for task in base["tasks"]}
    for task in incoming["tasks"]:
        previous = by_id.get(task["id"])
        if previous and task["status"] == "waiting" and previous["status"] != "waiting":
            # Reconstructed cards start from waiting defaults. They must not
            # rewind already observed running or completed work.
            by_id[task["id"]] = {
                **previous,
                **task,
                "status": previous["status"],
                "detail": previous["detail"] or task["detail"],
            }
        else:
            by_id[task["id"]] = {**previous, **task} if previous else task
    tasks = sorted(by_id.values(), key=lambda task: (task["order"], task["id"]))
    return {
        **base,
        **{key: incoming[key] for key in ("stage", "order", "label")},
        "status": _card_status(tasks, incoming["status"]),
        "tasks": tasks,
    }


def publish_progress_cards(
    app_id: str,
    *,
    command_id: str,
    stage: str,
    cards: list[dict[str, Any]],
) -> dict[str, Any]:
    """Merge durable progress-card patches and publish their live counterpart."""

    with session_scope() as session:
        row = session.scalar(
            select(WorkspaceCommand)
            .where(WorkspaceCommand.command_id == command_id)
            .with_for_update()
        )
        if row is None or row.app_id != app_id:
            raise KeyError(command_id)
        if str(row.status or "") not in ACTIVE_STATUSES:
            # A late worker callback must not mutate a completed, failed, or
            # interrupted command's durable snapshot.
            return {}
        payload = dict(row.payload or {})
        stored = payload.get(_TIMELINE_PROGRESS_KEY)
        current_cards = stored.get("cards") if isinstance(stored, dict) else []
        by_id = {
            str(card.get("id")): _safe_progress_card(card)
            for card in current_cards
            if isinstance(card, dict) and str(card.get("id") or "")
        }
        changed: list[dict[str, Any]] = []
        first_seen_at = now()
        for card in cards:
            if not isinstance(card, dict) or not str(card.get("id") or ""):
                continue
            merged = _merge_progress_card(by_id.get(str(card["id"])), card)
            if "event_id" not in merged:
                merged["event_id"] = _timeline_event_id(first_seen_at, 3 + len(changed))
                merged["created_at"] = _timestamp_in_kst(first_seen_at)
            by_id[merged["id"]] = merged
            changed.append(merged)
        if not changed:
            return {}
        payload[_TIMELINE_PROGRESS_KEY] = {
            "version": 1,
            "revision": int(stored.get("revision") or 0) + 1 if isinstance(stored, dict) else 1,
            "cards": sorted(by_id.values(), key=lambda card: (card["order"], card["id"])),
        }
        row.payload = payload
        session.flush()

    return append_progress_event(
        app_id,
        command_id=command_id,
        stage=stage,
        text="",
        metadata={
            "progress_event": "progressCardPatch",
            "progress_cards": {"version": 1, "cards": changed},
        },
    )


def _stage_progress_card(
    stage: str, command_id: str, metadata: dict[str, Any]
) -> list[dict[str, Any]]:
    """Adapt existing stage emitters into stable durable card patches."""

    event = str(metadata.get("progress_event") or "")
    if event in {
        "progressCardPatch",
        "durableProgressCards",
        "commandStateChanged",
        "designLlmMetrics",
    }:
        return []
    resolved_stage = workflow_stage(stage)
    if resolved_stage not in {"requirements", "design", "implementation", "testing"}:
        return []
    step = str(metadata.get("step") or metadata.get("analysis_step") or "")
    if not step:
        return []
    status = _progress_status(metadata.get("progress_status") or "running")
    return [{
        "id": f"{resolved_stage}:{command_id}:progress",
        "stage": resolved_stage,
        "order": ("requirements", "design", "implementation", "testing").index(resolved_stage),
        "label": str(metadata.get("progress_card_label") or f"{resolved_stage.title()} progress"),
        "status": status,
        "tasks": [{
            "id": step,
            "label": str(metadata.get("progress_step_label") or step),
            "order": 0,
            "status": status,
            "detail": str(metadata.get("progress_detail") or ""),
        }],
    }]


def append_progress_event(
    app_id: str,
    *,
    stage: str,
    text: str,
    command_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """실시간 표시용 진행 이벤트만 bounded process memory에 추가한다."""

    safe_metadata = dict(metadata or {})
    with session_scope() as session:
        if session.get(App, app_id) is None:
            raise KeyError(app_id)
        if command_id is not None:
            command = session.get(WorkspaceCommand, command_id)
            if command is None or command.app_id != app_id:
                raise KeyError(command_id)
    # Existing workers keep their transient event contract. This central
    # adapter persists the corresponding card before publishing that event.
    if command_id is not None:
        cards = _stage_progress_card(stage, command_id, safe_metadata)
        if cards:
            publish_progress_cards(
                app_id,
                command_id=command_id,
                stage=workflow_stage(stage),
                cards=cards,
            )
    with _event_lock:
        global _last_progress_event_id
        created_at = now()
        _last_progress_event_id = max(
            _timeline_event_id(created_at, 1),
            _last_progress_event_id + 1,
        )
        event = {
            "event_id": _last_progress_event_id,
            "app_id": app_id,
            "command_id": command_id,
            "stage": stage,
            "kind": "progress",
            "actor": "system",
            "text": text,
            "metadata": safe_metadata,
            "created_at": _timestamp_in_kst(created_at),
        }
        _events[app_id].append(event)
        return dict(event)


def notify_command_changed(
    app_id: str,
    *,
    stage: str,
    command_id: str,
) -> dict[str, Any]:
    """Wake live clients without duplicating the command's durable cards."""

    return append_progress_event(
        app_id,
        command_id=command_id,
        stage=stage,
        text="Workspace state changed.",
        metadata={"progress_event": "commandStateChanged"},
    )


def list_progress_events(
    app_id: str,
    *,
    after: int = 0,
    limit: int = 500,
    include_llm_timings: bool = True,
) -> list[dict[str, Any]]:
    """현재 process가 보유한 ``after`` 이후 진행 이벤트를 반환한다.

    서버 재시작 전 진행 상황은 복원하지 않는다. 사용자 메시지와 최종 카드는
    ``list_timeline_events``가 MySQL command에서 복원한다.
    """
    with _event_lock:
        events = [
            dict(event) for event in _events.get(app_id, ()) if int(event["event_id"]) > after
        ][:limit]
    for event in events:
        event["metadata"] = _event_metadata(
            event.get("metadata", {}),
            include_llm_timings=include_llm_timings,
        )
    return events


def _command_timeline_events(row: WorkspaceCommand) -> list[dict[str, Any]]:
    """Project one persisted command into its durable user and terminal cards."""

    payload = row.payload if isinstance(row.payload, dict) else {}
    result = row.result if isinstance(row.result, dict) else {}
    events: list[dict[str, Any]] = []
    progress_cards = payload.get(_TIMELINE_PROGRESS_KEY)
    has_progress_cards = isinstance(progress_cards, dict) and isinstance(
        progress_cards.get("cards"), list
    ) and bool(progress_cards["cards"])
    user_text = str(payload.get("text") or payload.get("_timeline_text") or "").strip()
    if user_text:
        events.append(
            {
                "event_id": _timeline_event_id(row.created_at, 0),
                "app_id": row.app_id,
                "command_id": row.command_id,
                "stage": row.stage,
                "kind": "message",
                "actor": "user",
                "text": user_text,
                "metadata": {"context": payload.get("context")},
                "created_at": _timestamp_in_kst(row.created_at),
            }
        )

    status = str(row.status or "")
    if has_progress_cards:
        events.append(
            {
                # Keep restored progress before the terminal command card.
                "event_id": _timeline_event_id(row.created_at, 1),
                "app_id": row.app_id,
                "command_id": row.command_id,
                "stage": row.stage,
                "kind": "progress",
                "actor": "system",
                "text": "",
                "metadata": {
                    "progress_event": "durableProgressCards",
                    "progress_cards": progress_cards,
                },
                "created_at": _timestamp_in_kst(row.created_at),
            }
        )
    if status in ACTIVE_STATUSES:
        if has_progress_cards:
            return events
        return events
    result_kind = str(result.get("kind") or "")
    if status in {"FAILED", "INTERRUPTED"}:
        kind = "error"
        actor = "system"
        text = str(row.error or result.get("message") or "Workspace command failed.")
        metadata = {"status": status, "error": row.error}
    else:
        text = str(result.get("message") or "").strip()
        if not text:
            return events
        kind = (
            "message"
            if result_kind == "reply"
            else result_kind
            if result_kind in {"question", "action_required"}
            else "status"
        )
        actor = "assistant"
        metadata = {"status": status, **result}

    # Questions can be marked COMPLETED later when their answer arrives. Keep
    # the card beside its original command instead of moving it after the answer.
    conversational = result_kind in {"reply", "question", "action_required"}
    terminal_at = (
        row.started_at if conversational else row.completed_at
    ) or row.started_at or row.created_at
    # New snapshots already contain these stable terminal Testing tasks. Keep
    # the legacy projection only for commands created before durable cards.
    testing_completion = [] if has_progress_cards else _testing_completion_projection(row, terminal_at)
    events.extend(testing_completion)
    events.append(
        {
            "event_id": _timeline_event_id(terminal_at, 3 if testing_completion else 2),
            "app_id": row.app_id,
            "command_id": row.command_id,
            "stage": row.stage,
            "kind": kind,
            "actor": actor,
            "text": text,
            "metadata": metadata,
            "created_at": _timestamp_in_kst(terminal_at),
        }
    )
    return events


def _testing_completion_projection(
    row: WorkspaceCommand,
    terminal_at: datetime,
) -> list[dict[str, Any]]:
    """Rebuild terminal Testing steps from a completed command checkpoint."""

    if str(row.stage or "") != "testing" or str(row.status or "") != "COMPLETED":
        return []
    payload = row.payload if isinstance(row.payload, dict) else {}
    checkpoint = payload.get("testing_checkpoint")
    if not isinstance(checkpoint, dict) or checkpoint.get("current_node") != "verification_complete":
        return []
    result = row.result if isinstance(row.result, dict) else {}
    job = result.get("job") if isinstance(result.get("job"), dict) else {}
    reports = (checkpoint.get("result"), job.get("result"), result)
    if not any(_testing_report_passed(report) for report in reports):
        return []

    steps = (
        (
            "prepare-testing",
            "Prepare testing snapshot",
            "Testing inputs are ready.",
        ),
        (
            "run-verification",
            "Run application verification",
            "Verification gates finished.",
        ),
        (
            "finalize-testing",
            "Finalize testing results",
            "Test results are ready.",
        ),
    )
    return [
        {
            "event_id": _timeline_event_id(terminal_at, slot),
            "app_id": row.app_id,
            "command_id": row.command_id,
            "stage": "testing",
            "kind": "progress",
            "actor": "system",
            "text": detail,
            "metadata": {
                "progress_event": "testingStepUpdated",
                "step": step,
                "progress_step_label": label,
                "progress_card_label": "Testing progress",
                "progress_detail": detail,
                "progress_status": "completed",
            },
            "created_at": _timestamp_in_kst(terminal_at),
        }
        for slot, (step, label, detail) in enumerate(steps)
    ]


def _testing_report_passed(report: Any) -> bool:
    """Return whether a Testing report contains terminal passing evidence."""

    if not isinstance(report, dict):
        return False
    if report.get("passed") is True or str(report.get("gateStatus") or "").upper() == "PASS":
        return True
    verification = report.get("verification")
    return isinstance(verification, dict) and _testing_report_passed(verification)


def list_timeline_events(
    app_id: str,
    *,
    command_limit: int = 500,
    include_llm_timings: bool = False,
) -> list[dict[str, Any]]:
    """Combine durable command cards with this process's transient progress."""

    with session_scope() as session:
        rows = list(
            session.scalars(
                select(WorkspaceCommand)
                .where(WorkspaceCommand.app_id == app_id)
                .order_by(WorkspaceCommand.created_at.desc())
                .limit(command_limit)
            ).all()
        )
    rows.reverse()
    durable = [event for row in rows for event in _command_timeline_events(row)]
    progress = [
        event
        for event in list_progress_events(
            app_id,
            limit=_EVENT_LIMIT_PER_APP,
            include_llm_timings=include_llm_timings,
        )
        if event.get("metadata", {}).get("progress_event") != "commandStateChanged"
    ]

    # An awaiting command has no completed_at. During the current process, put
    # its terminal card after the last progress update for that command.
    last_progress = {
        command_id: max(
            int(event["event_id"])
            for event in progress
            if event.get("command_id") == command_id
        )
        for command_id in {
            str(event.get("command_id") or "") for event in progress
        }
        if command_id
    }
    next_terminal_event_id: dict[str, int] = {}
    for event in durable:
        if event.get("actor") == "user":
            continue
        command_id = str(event.get("command_id") or "")
        minimum = last_progress.get(command_id, -1) + 1
        event_id = max(int(event["event_id"]), minimum, next_terminal_event_id.get(command_id, -1))
        event["event_id"] = event_id
        next_terminal_event_id[command_id] = event_id + 1
    return sorted([*durable, *progress], key=lambda event: int(event["event_id"]))


def progress_cursor(app_id: str) -> int:
    """Return the SSE cursor independently from durable timeline card IDs."""

    with _event_lock:
        return max(
            (int(event["event_id"]) for event in _events.get(app_id, ())),
            default=0,
        )


def get_event_llm_timings(
    app_id: str, event_id: int, *, offset: int = 0, limit: int = 20
) -> dict[str, Any]:
    """메모리에 남아 있는 설계 LLM 원문을 작은 page 단위로 반환한다."""
    with _event_lock:
        event = next(
            (item for item in _events.get(app_id, ()) if int(item["event_id"]) == event_id),
            None,
        )
        if event is None:
            raise KeyError(event_id)
        metadata = event.get("metadata", {})
        timings = metadata.get("llm_timing_events")
        if metadata.get("progress_event") != "designLlmMetrics" or not isinstance(timings, list):
            raise ValueError("The event does not contain design LLM timings.")
        page = list(timings[offset : offset + limit])
        total = len(timings)
    return {
        "event_id": event_id,
        "total": total,
        "offset": offset,
        "timings": page,
    }


def get_app_summary(app_id: str) -> dict[str, Any]:
    """workspace 첫 화면에 필요한 앱 식별자·현재 단계·생성 시각만 조회한다."""
    with session_scope() as session:
        row = session.get(App, app_id)
        if row is None:
            raise KeyError(app_id)
        return {
            "app_id": row.app_id,
            "current_stage": workflow_stage(row.current_stage),
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }


def save_deployment_preferences(app_id: str, selection: dict[str, Any]) -> dict[str, Any]:
    """활성 command와 별개로 최신 배포 선택 초안을 insert 또는 update한다.

    사용자가 요구사항 분석 중에 지역을 바꿀 수 있으므로 이 값은 artifact 버전을 만들지 않는
    draft다. 다음 requirements command가 시작될 때 읽어 정식 resource 입력에 반영한다.
    """
    with session_scope() as session:
        app = session.get(App, app_id)
        if app is None:
            raise KeyError(app_id)
        app.deployment_preferences = selection
        session.flush()
        return dict(app.deployment_preferences or {})


def get_deployment_preferences(app_id: str) -> dict[str, Any] | None:
    """저장된 최신 배포 선택 초안을 반환한다."""
    with session_scope() as session:
        app = session.get(App, app_id)
        if app is None or app.deployment_preferences is None:
            return None
        return dict(app.deployment_preferences)


def list_workspace_apps(limit: int = 50) -> list[dict[str, Any]]:
    """사이드바에 표시할 최근 앱과 각 앱의 최신 command를 조회한다."""
    with session_scope() as session:
        latest_command_id = (
            select(WorkspaceCommand.command_id)
            .where(WorkspaceCommand.app_id == App.app_id)
            .order_by(
                WorkspaceCommand.created_at.desc(),
                WorkspaceCommand.command_id.desc(),
            )
            .limit(1)
            .correlate(App)
            .scalar_subquery()
        )
        rows = session.execute(
            select(
                App.app_id,
                App.requirements_text,
                App.current_stage,
                App.created_at,
                WorkspaceCommand.command_id,
                WorkspaceCommand.action,
                WorkspaceCommand.stage,
                WorkspaceCommand.status,
                WorkspaceCommand.created_at,
            )
            .outerjoin(
                WorkspaceCommand,
                WorkspaceCommand.command_id == latest_command_id,
            )
            .order_by(App.created_at.desc())
            .limit(limit)
        ).all()
        result: list[dict[str, Any]] = []
        for (
            app_id,
            requirements_text,
            current_stage,
            created_at,
            command_id,
            command_action,
            command_stage,
            command_status,
            command_created_at,
        ) in rows:
            first_line = next(
                (
                    line.strip()
                    for line in (requirements_text or "").splitlines()
                    if line.strip()
                ),
                "",
            )
            result.append(
                {
                    "app_id": app_id,
                    "title": first_line[:72] or f"EasyDep app {app_id[:8]}",
                    "current_stage": (
                        command_stage if command_id is not None else workflow_stage(current_stage)
                    ),
                    "created_at": created_at.isoformat() if created_at else None,
                    "command": (
                        {
                            "command_id": command_id,
                            "action": command_action,
                            "stage": command_stage,
                            "status": command_status,
                            "created_at": _timestamp_in_kst(command_created_at),
                        }
                        if command_id is not None
                        else None
                    ),
                }
            )
        return result


def interrupt_unfinished() -> int:
    """서버 재시작 전에 끝나지 않은 command를 INTERRUPTED로 표시한다.

    process-local worker는 재시작 후 존재하지 않으므로 QUEUED/RUNNING 상태를 그대로 두면 UI가
    영원히 진행 중으로 보인다. 성공으로 추정하지 않고, 검증된 checkpoint에서 재개하라는
    명시적인 오류를 남긴다.
    """
    changed = 0
    with session_scope() as session:
        rows = session.scalars(
            select(WorkspaceCommand).where(WorkspaceCommand.status.in_(ACTIVE_STATUSES))
        ).all()
        for row in rows:
            row.status = "INTERRUPTED"
            row.error = (
                "The server restarted and could not restore the in-flight command. "
                "Resume from a validated checkpoint."
            )
            row.completed_at = now()
            changed += 1
    return changed


def interrupted_testing_commands() -> list[dict[str, Any]]:
    """고정 입력이 저장되어 있어 안전하게 다시 실행할 수 있는 Testing 명령을 반환한다."""
    with session_scope() as session:
        rows = session.scalars(
            select(WorkspaceCommand).where(
                WorkspaceCommand.stage == "testing",
                WorkspaceCommand.status == "INTERRUPTED",
            )
        ).all()
        return [
            command_dict(row)
            for row in rows
            if isinstance((row.payload or {}).get("testing_checkpoint"), dict)
        ]


def interrupted_technical_retry_commands() -> list[dict[str, Any]]:
    """Return only marked technical retries interrupted by a server restart."""

    with session_scope() as session:
        rows = session.scalars(
            select(WorkspaceCommand).where(
                WorkspaceCommand.status.in_(("AWAITING_INPUT", "INTERRUPTED")),
                WorkspaceCommand.payload["_technical_repair_retry"].is_not(None),
            )
        ).all()
        return [command_dict(row) for row in rows]
