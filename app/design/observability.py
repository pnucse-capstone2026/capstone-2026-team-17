"""Low-volume, structured timing logs for design operations.

The design graph has a few potentially slow boundaries: an LLM repair call, a
PlantUML Docker invocation, and targeted sequence reassembly.  A single outer
"completed in …" event cannot tell which boundary delayed a workspace command.
This module keeps tracing dependency-free and attaches workspace context when
it is known.

Logs contain identifiers, counts, rule IDs, and durations only.  They never
include requirements, feedback, PlantUML source, or model contents.
"""
from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator


# The server configures ``easydep.agent`` at startup.  Use that hierarchy so
# timing records are visible in the normal application log rather than relying
# on a process-wide root logger configuration.
_logger = logging.getLogger("easydep.agent.design.timing")
_context: ContextVar[dict[str, Any]] = ContextVar("design_timing_context", default={})


@contextmanager
def design_timing_context(**values: Any) -> Iterator[None]:
    """Attach stable workspace identifiers to nested timing log records."""
    clean = {
        key: value
        for key, value in values.items()
        if value is not None and str(value) != ""
    }
    token = _context.set({**_context.get(), **clean})
    try:
        yield
    finally:
        _context.reset(token)


def log_design_timing(event: str, /, **values: Any) -> None:
    """Emit one grep-friendly JSON record for a design timing boundary."""
    record = {
        "event": event,
        **_context.get(),
        **{key: value for key, value in values.items() if value is not None},
    }
    _logger.info("design_timing=%s", json.dumps(record, ensure_ascii=False, sort_keys=True))
