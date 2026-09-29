"""Cross-agent observability helpers.

The package deliberately keeps LangSmith optional at import time.  Observability
must never prevent an EasyDep workflow from running when credentials are absent
or the observability service is unavailable.
"""

from app.metrics.langsmith import TraceRun, trace_metadata, trace_scope
from app.metrics.llm_stall_probe import start_stall_probe

__all__ = ["TraceRun", "start_stall_probe", "trace_metadata", "trace_scope"]
