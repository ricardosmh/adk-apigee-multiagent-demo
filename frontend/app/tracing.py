"""End-to-end request correlation for the BFF — W3C traceparent + Cloud Logging.

Every request into Cloud Run already carries a trace context (GFE injects
`traceparent` / `X-Cloud-Trace-Context`). This module:

  * captures it per request (contextvar — safe under asyncio concurrency),
  * offers `outbound_headers()` so httpx calls to Apigee / Agent Engine carry
    the SAME trace (Apigee distributed tracing + the engines' OTEL then join
    it — one waterfall in Cloud Trace),
  * emits STRUCTURED logs to stdout with `logging.googleapis.com/trace`, so
    Logs Explorer correlates BFF lines with everything else on the trace id.

No OTEL SDK dependency on purpose: the BFF only needs to *propagate* and
*log* — it doesn't create spans. Review tools: Cloud Trace (waterfall),
Logs Explorer filtered by trace (all components, one view).
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import sys

_TRACEPARENT_RE = re.compile(r"^[0-9a-f]{2}-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$")
_XCTC_RE = re.compile(r"^([0-9a-f]{32})(?:/(\d+))?", re.I)

_current: contextvars.ContextVar[dict | None] = contextvars.ContextVar("trace_ctx", default=None)


def capture(headers) -> None:
    """Record this request's trace context (call once per request).
    Accepts a case-insensitive mapping (FastAPI request.headers)."""
    ctx = None
    tp = headers.get("traceparent")
    if tp:
        m = _TRACEPARENT_RE.match(tp.strip())
        if m:
            ctx = {"trace_id": m.group(1), "traceparent": tp.strip()}
    if ctx is None:
        xctc = headers.get("x-cloud-trace-context")
        if xctc:
            m = _XCTC_RE.match(xctc.strip())
            if m:
                trace_id = m.group(1).lower()
                span = int(m.group(2) or 0)
                ctx = {"trace_id": trace_id,
                       "traceparent": f"00-{trace_id}-{span or 1:016x}-01"}
    _current.set(ctx)


def outbound_headers() -> dict:
    """Headers that make the downstream hop join this request's trace."""
    ctx = _current.get()
    return {"traceparent": ctx["traceparent"]} if ctx else {}


def trace_id() -> str | None:
    ctx = _current.get()
    return ctx["trace_id"] if ctx else None


# ── Structured logging (Cloud Logging via stdout JSON) ───────────────────────
_SEVERITY = {logging.DEBUG: "DEBUG", logging.INFO: "INFO", logging.WARNING: "WARNING",
             logging.ERROR: "ERROR", logging.CRITICAL: "CRITICAL"}


class StructuredFormatter(logging.Formatter):
    """One JSON object per line — Cloud Run ingests it as structured logging.
    The logging.googleapis.com/trace field is what links a line to its trace."""

    def __init__(self, project: str | None):
        super().__init__()
        self._project = project

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "severity": _SEVERITY.get(record.levelno, "DEFAULT"),
            "message": record.getMessage(),
            "logger": record.name,
        }
        if record.exc_info:
            entry["message"] += "\n" + self.formatException(record.exc_info)
        jf = getattr(record, "json_fields", None)   # parity with CloudLoggingHandler
        if isinstance(jf, dict):
            entry.update(jf)
        tid = trace_id()
        if tid and self._project:
            entry["logging.googleapis.com/trace"] = f"projects/{self._project}/traces/{tid}"
        return json.dumps(entry, ensure_ascii=False)


_configured = False


def setup_logging() -> None:
    """Route BFF logging to the NAMED Cloud Logging log ``front-ai-logs`` with
    the request trace attached — one log name per component (apigee-ai-logs /
    front-ai-logs / agent-ai-logs), one trace filter per turn. Falls back to
    structured stdout (platform-captured) when the client library or ADC is
    unavailable (local dev, tests). Idempotent."""
    global _configured
    if _configured:
        return
    _configured = True
    root = logging.getLogger()
    handler: logging.Handler
    try:
        import google.cloud.logging as gcl
        from google.cloud.logging_v2.handlers import CloudLoggingHandler

        client = gcl.Client()
        project = client.project

        class _TraceNamedHandler(CloudLoggingHandler):
            def handle(self, record):
                tid = trace_id()
                if tid and not getattr(record, "trace", None):
                    record.trace = f"projects/{project}/traces/{tid}"
                return super().handle(record)

        handler = _TraceNamedHandler(client, name="front-ai-logs")
    except Exception:  # noqa: BLE001 — library/creds absent -> stdout fallback
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(StructuredFormatter(os.environ.get("GOOGLE_CLOUD_PROJECT")))
    root.handlers = [handler]
    root.setLevel(logging.INFO)
