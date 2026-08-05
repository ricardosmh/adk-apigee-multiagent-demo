"""Structured per-request logging into the AI project's single pane.

One JSON entry per request in the NAMED log ``services-ai-logs`` of the AI
project (env AI_LOG_PROJECT, injected by deploy_services.py from
demo-environment.yaml), carrying the inbound W3C ``traceparent`` — so one
trace-id filter in the AI project's Logs Explorer shows
BFF -> gateway -> agents -> MCP -> THIS SERVICE for a turn.

Requires roles/logging.logWriter for the runtime SA on the AI project
(granted by apigee/provision manifest telemetryGrants). Falls back to stdout
JSON (captured by Cloud Run into the BACKEND project) when the client library,
credentials, or AI_LOG_PROJECT are absent — logging never breaks the service.
"""
import json
import logging
import os
import sys
import time

_SERVICE = "orders-service"
_logger = None


def _init():
    global _logger
    project = os.environ.get("AI_LOG_PROJECT")
    try:
        if not project:
            raise RuntimeError("AI_LOG_PROJECT unset")
        import google.cloud.logging as gcl

        client = gcl.Client(project=project)
        _logger = client.logger("services-ai-logs")
    except Exception:  # noqa: BLE001 — fall back to stdout JSON
        _logger = None


def _emit(entry: dict, trace_id: str | None):
    project = os.environ.get("AI_LOG_PROJECT")
    if _logger is not None:
        kwargs = {}
        if trace_id and project:
            kwargs["trace"] = f"projects/{project}/traces/{trace_id}"
        try:
            _logger.log_struct(entry, **kwargs)
            return
        except Exception:  # noqa: BLE001
            pass
    if trace_id and project:
        entry["logging.googleapis.com/trace"] = f"projects/{project}/traces/{trace_id}"
    print(json.dumps(entry), file=sys.stdout, flush=True)


def install_ai_logging(app, service: str = _SERVICE) -> None:
    """FastAPI middleware: one structured entry per request (skips /health)."""
    _init()

    @app.middleware("http")
    async def _ai_log(request, call_next):  # noqa: ANN001
        if request.url.path.rstrip("/").endswith("/health"):
            return await call_next(request)
        t0 = time.monotonic()
        received = int(time.time() * 1000)
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            tp = request.headers.get("traceparent", "")
            parts = tp.split("-")
            trace_id = parts[1] if len(parts) == 4 else None
            responded = int(time.time() * 1000)
            _emit({"event": "service_request", "component": "service",
                   "service": service, "verb": request.method,
                   "path": request.url.path, "status": status,
                   "traceparent": tp,
                   "latency_metrics": {"receivedAt": received, "respondedAt": responded,
                                       "latencyMs": round((time.monotonic() - t0) * 1000)}},
                  trace_id)
