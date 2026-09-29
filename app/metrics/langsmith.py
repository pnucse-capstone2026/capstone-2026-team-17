"""Minimal LangSmith tracing shared by every EasyDep agent.

This module deliberately uses only LangSmith's built-in observability model:
trace/run count, latency, error rate, LLM call count, token usage, and cost.
It does not create EasyDep-specific feedback scores or derived metrics.
"""

from __future__ import annotations

import contextvars
import functools
import logging
import os
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

# Settings in the rest of EasyDep are read from `.env` by Pydantic without
# exporting values to `os.environ`.  Load it here as well so this independent
# module honours the same opt-in configuration without changing `app.core`.
try:
    from dotenv import load_dotenv

    load_dotenv(override=False)
except ImportError:  # pragma: no cover - normal deployments install python-dotenv
    pass

_LOG = logging.getLogger("easydep.metrics.langsmith")
_INITIALIZATION_ERROR_REPORTED = False
_TRACE_METADATA: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "easydep_langsmith_trace_metadata", default={}
)


def _normalized_metadata(metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return metadata with EasyDep's application-level LangSmith thread key.

    A Workspace app is created from one initial requirements submission and all
    later requirements, design, implementation, and testing turns keep that
    ``app_id``.  LangSmith groups traces into a Thread only when every run has a
    special thread metadata key, so use the durable ``app_id`` as ``thread_id``
    unless a caller deliberately supplied another thread identifier.
    """

    normalized = dict(metadata or {})
    app_id = normalized.get("app_id")
    if app_id and not any(
        normalized.get(key) for key in ("thread_id", "session_id", "conversation_id")
    ):
        normalized["thread_id"] = str(app_id)
    return normalized


def _env_flag(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def tracing_enabled() -> bool:
    return _env_flag("LANGSMITH_TRACING", default=False) and bool(
        os.getenv("LANGSMITH_API_KEY")
    )


@contextmanager
def trace_metadata(metadata: Mapping[str, Any] | None = None) -> Iterator[None]:
    """Attach shared, non-sensitive metadata to nested agent traces."""

    combined_metadata = _normalized_metadata(
        {**_TRACE_METADATA.get(), **dict(metadata or {})}
    )
    metadata_token = _TRACE_METADATA.set(combined_metadata)
    try:
        yield
    finally:
        _TRACE_METADATA.reset(metadata_token)


def _client() -> Any:
    from langsmith import Client

    kwargs: dict[str, Any] = {
        "api_key": os.getenv("LANGSMITH_API_KEY"),
        "hide_inputs": _env_flag("LANGSMITH_HIDE_INPUTS", default=True),
        "hide_outputs": _env_flag("LANGSMITH_HIDE_OUTPUTS", default=True),
        "hide_metadata": _env_flag("LANGSMITH_HIDE_METADATA", default=False),
    }
    if endpoint := os.getenv("LANGSMITH_ENDPOINT"):
        kwargs["api_url"] = endpoint
    if workspace_id := os.getenv("LANGSMITH_WORKSPACE_ID"):
        kwargs["workspace_id"] = workspace_id
    return Client(**kwargs)


@dataclass
class TraceRun:
    """Optional active trace with standard LLM usage metadata only."""

    run: Any | None = None
    _usage: dict[str, int] = field(default_factory=dict)

    def set_usage(self, *, input_tokens: int = 0, output_tokens: int = 0) -> None:
        """Attach provider-reported token counts to an LLM run.

        LangSmith then supplies token and cost charts itself. Costs require a
        matching model-price entry in the LangSmith workspace.
        """

        self._usage = {
            "input_tokens": max(0, int(input_tokens)),
            "output_tokens": max(0, int(output_tokens)),
            "total_tokens": max(0, int(input_tokens)) + max(0, int(output_tokens)),
        }

    def _flush(self) -> None:
        if self.run is not None and self._usage:
            try:
                self.run.set(usage_metadata=self._usage)
            except Exception:  # noqa: BLE001 - tracing must never fail the agent
                _LOG.warning("LangSmith usage submission failed", exc_info=True)


@contextmanager
def trace_scope(
    name: str,
    *,
    run_type: str = "chain",
    metadata: Mapping[str, Any] | None = None,
) -> Iterator[TraceRun]:
    """Trace an operation using the same contract in every agent.

    No input or output payload is supplied here. With the default privacy
    settings, LangSmith receives trace structure, timing, errors, model usage,
    and metadata only. The context degrades to a no-op when unconfigured.
    """

    global _INITIALIZATION_ERROR_REPORTED
    if not tracing_enabled():
        yield TraceRun()
        return

    try:
        from langsmith import trace, tracing_context

        client = _client()
    except Exception:  # noqa: BLE001 - observability is optional
        if not _INITIALIZATION_ERROR_REPORTED:
            _LOG.warning("LangSmith tracing could not be initialized", exc_info=True)
            _INITIALIZATION_ERROR_REPORTED = True
        yield TraceRun()
        return

    span = TraceRun()
    body_error: BaseException | None = None
    try:
        with trace_metadata(metadata), tracing_context(
            enabled=True,
            client=client,
            project_name=os.getenv("LANGSMITH_PROJECT", "easydep"),
            # LangChain/LangGraph create their own descendant runs.  Supplying
            # the shared metadata on the tracing context ensures those runs,
            # not only EasyDep's manual spans, participate in the same Thread.
            metadata={"service": "easydep", **_TRACE_METADATA.get()},
        ), trace(
            name=name,
            run_type=run_type,
            metadata={"service": "easydep", **_TRACE_METADATA.get()},
            project_name=os.getenv("LANGSMITH_PROJECT", "easydep"),
            client=client,
        ) as run:
            span.run = run
            try:
                yield span
            except BaseException as error:
                body_error = error
                raise
            finally:
                span._flush()
    except BaseException:
        if body_error is not None:
            raise
        _LOG.warning("LangSmith trace submission failed", exc_info=True)


def bind_context(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Bind a callable to a fresh copy of the current tracing context.

    ``contextvars`` do not cross ``ThreadPoolExecutor`` or ``threading.Thread``
    boundaries automatically.  Create this wrapper once per submitted task so
    concurrent tasks receive the same parent trace and Thread metadata without
    attempting to enter one ``Context`` more than once.
    """

    context = contextvars.copy_context()

    @functools.wraps(fn)
    def runner(*args: Any, **kwargs: Any) -> Any:
        return context.run(fn, *args, **kwargs)

    return runner
