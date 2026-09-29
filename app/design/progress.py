"""Context-local progress reporting for design generation.

Design services do not know about HTTP, workspace commands, or persistence.
The workspace may bind a sink around a graph invocation; direct API and test
callers simply run without one.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

ProgressSink = Callable[[str, dict[str, Any]], None]

_progress_sink: ContextVar[ProgressSink | None] = ContextVar(
    "easydep_design_progress_sink", default=None,
)


def emit_progress(event: str, **fields: Any) -> None:
    sink = _progress_sink.get()
    if sink is not None:
        sink(event, fields)


@contextmanager
def progress_scope(sink: ProgressSink) -> Iterator[None]:
    token = _progress_sink.set(sink)
    try:
        yield
    finally:
        _progress_sink.reset(token)
