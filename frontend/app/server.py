"""FastAPI BFF for the two-view frontend.

  Direct model view  → POST /api/chat   → llm_client (unified /llm-stream)
  Agent view         → POST /api/agent  → agent_client (supervisor Agent Engine)

Static UI is served from this directory. Deployed to Cloud Run via
service.yaml / deploy.sh (see docs/PHASE3_OPS_RUNBOOK.md). Config is injected as
env vars + Secret Manager — service.yaml is the contract.
"""
import asyncio
import json
import os
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

import logging

from . import admin, agent_client, iap, llm_client, trace_explorer, tracing

tracing.setup_logging()
logger = logging.getLogger("bff")

HERE = Path(__file__).parent
app = FastAPI(title="Apigee LLM + Agent Frontend")


@app.middleware("http")
async def _trace_middleware(request: Request, call_next):
    """Capture the incoming trace context (GFE injects it on every Cloud Run
    request) so logs correlate and outbound calls propagate the SAME trace —
    then log one structured line per API interaction."""
    tracing.capture(request.headers)
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        logger.info("%s %s -> %s", request.method, request.url.path, response.status_code)
    return response


@app.exception_handler(iap.IdentityError)
async def _on_identity_error(request: Request, exc: iap.IdentityError):
    # IAP is enabled but the request had no valid verified identity → 401.
    return JSONResponse({"error": "unauthorized", "detail": str(exc)}, status_code=401)


# ── Static UI ────────────────────────────────────────────────────────────────
# Browser assets live in ../static/ (sibling of this app/ package); the served
# URLs (/, /app.js, /style.css) are unchanged, so index.html's refs still resolve.
STATIC = HERE.parent / "static"


# no-cache: the UI ships inside the container image and changes with deploys;
# without this, browsers serve a STALE app.js/style.css against a fresh
# index.html (seen live: the admin panel rendered unstyled inside the Direct
# view because the cached CSS predated the .admin rules). Assets are tiny —
# revalidation costs nothing and removes the whole failure class.
_NO_CACHE = {"Cache-Control": "no-cache, must-revalidate"}


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html", headers=_NO_CACHE)


@app.get("/app.js")
async def appjs():
    return FileResponse(STATIC / "app.js", media_type="application/javascript", headers=_NO_CACHE)


@app.get("/style.css")
async def stylecss():
    return FileResponse(STATIC / "style.css", media_type="text/css", headers=_NO_CACHE)


# ── Liveness (Cloud Run health check; unauthenticated-safe, no secrets) ───────
@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


# ── Direct model view ────────────────────────────────────────────────────────
@app.get("/api/config")
async def api_config():
    return {**llm_client.config(), "sub_agents": agent_client.sub_agent_legend()}


@app.post("/api/chat")
async def api_chat(request: Request):
    payload = await request.json()
    history = [m for m in (payload.get("history") or []) if m.get("text")]
    if not history or history[-1].get("role") != "user":
        return JSONResponse({"error": "history must end with a user turn"}, status_code=400)
    try:
        result = await llm_client.chat(
            history,
            payload.get("publisher"),
            payload.get("complexity"),
            payload.get("use_cache", True),
            user_email=await _user_id_from_iap(request) or os.environ.get("AGENT_DEV_USER_ID"),
            api_key=(payload.get("api_key") or "").strip(),
        )
        _log_turn("direct", user=await _user_id_from_iap(request),
                  model=result.get("model"), latency_ms=result.get("latency_ms"),
                  usage=result.get("usage"), userMessage=_trunc(history[-1]["text"]),
                  response=_trunc(result.get("text")))
        return result
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": "upstream call failed", "detail": str(e)}, status_code=502)


@app.post("/api/chat/stream")
async def api_chat_stream(request: Request):
    payload = await request.json()
    history = [m for m in (payload.get("history") or []) if m.get("text")]
    if not history or history[-1].get("role") != "user":
        return JSONResponse({"error": "history must end with a user turn"}, status_code=400)

    # resolve identity BEFORE streaming starts (the generator can't 401 mid-stream)
    user_email = await _user_id_from_iap(request) or os.environ.get("AGENT_DEV_USER_ID")

    async def gen():
        pieces, done, err = [], {}, None
        async for event in llm_client.chat_stream(
            history,
            payload.get("publisher"),
            payload.get("complexity"),
            payload.get("use_cache", True),
            debug=bool(payload.get("debug")),
            user_email=user_email,
            api_key=(payload.get("api_key") or "").strip(),
        ):
            kind = event.get("type")
            if kind == "text":
                pieces.append(event.get("text") or "")
            elif kind == "done":
                done = event
            elif kind == "error":
                err = event.get("message")
            yield _sse(event)
        _log_turn("direct", user=user_email, model=done.get("model"),
                  latency_ms=done.get("latency_ms"), usage=done.get("usage"),
                  userMessage=_trunc(history[-1]["text"]), response=_trunc("".join(pieces)),
                  error=_trunc(err))

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


# ── Agent view ───────────────────────────────────────────────────────────────
@app.post("/api/agent")
async def api_agent(request: Request):
    payload = await request.json()
    message = (payload.get("message") or "").strip()
    if not message:
        return JSONResponse({"error": "message is required"}, status_code=400)
    # Phase 3 will derive user_id from the verified IAP assertion; until then the
    # dev fallback in agent_client applies. (Header read here is forward-looking.)
    user_id = await _user_id_from_iap(request)
    try:
        result = await agent_client.agent_turn(
            message, payload.get("session_id"), user_id=user_id
        )
        return result
    except agent_client.AgentError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": "agent call failed", "detail": str(e)}, status_code=502)


@app.get("/api/sessions")
async def api_sessions(request: Request):
    user_id = await _user_id_from_iap(request)
    try:
        return {"sessions": await agent_client.list_sessions(user_id)}
    except agent_client.AgentError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": "list sessions failed", "detail": str(e)}, status_code=502)


@app.get("/api/sessions/{session_id}")
async def api_session(session_id: str, request: Request):
    user_id = await _user_id_from_iap(request)
    try:
        turns = await agent_client.get_session_history(session_id, user_id)
        return {"session_id": session_id, "turns": turns}
    except agent_client.AgentError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": "load session failed", "detail": str(e)}, status_code=502)


@app.delete("/api/sessions/{session_id}")
async def api_delete_session(session_id: str, request: Request):
    user_id = await _user_id_from_iap(request)
    try:
        await agent_client.delete_session(session_id, user_id)
        return {"deleted": session_id}
    except agent_client.AgentError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": "delete session failed", "detail": str(e)}, status_code=502)


def _sse(event: dict) -> str:
    return f"event: {event.get('type', 'message')}\ndata: {json.dumps(event)}\n\n"


@app.post("/api/agent/stream")
async def api_agent_stream(request: Request):
    payload = await request.json()
    message = (payload.get("message") or "").strip()
    if not message:
        return JSONResponse({"error": "message is required"}, status_code=400)
    user_id = await _user_id_from_iap(request)
    session_id = payload.get("session_id")

    async def gen():
        pieces, delegations, done, err, sid = [], [], {}, None, session_id
        async for event in agent_client.agent_turn_stream(
            message,
            session_id,
            user_id=user_id,
            debug=bool(payload.get("debug")),
            stream_tokens=bool(payload.get("stream_tokens")),
        ):
            kind = event.get("type")
            if kind == "text":
                pieces.append(event.get("text") or "")
            elif kind == "delegation":
                delegations.append(event.get("agent_name"))
            elif kind == "session":
                sid = event.get("session_id")
            elif kind == "done":
                done = event
            elif kind == "error":
                err = event.get("message")
            yield _sse(event)
        _log_turn("agent", user=user_id, session_id=sid,
                  delegations=delegations or None,
                  latency_ms=done.get("latency_ms"), usage=done.get("usage"),
                  userMessage=_trunc(message), response=_trunc("".join(pieces)),
                  error=_trunc(err))

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # don't let any intermediary buffer us
        },
    )


_BODY_TRUNC = 2000


def _trunc(text) -> str | None:
    """Bodies are logged HERE (the one layer that has the full turn: user
    message + final aggregated response), truncated — the gateway can't buffer
    streams and the engines log content via ADK span capture."""
    if not text:
        return None
    text = str(text)
    return text if len(text) <= _BODY_TRUNC else text[:_BODY_TRUNC] + f"…[+{len(text) - _BODY_TRUNC} chars]"


def _log_turn(kind: str, **fields) -> None:
    """One structured front-ai-logs entry per user turn, in the CANONICAL schema
    every component shares: event/component/traceparent + a `latency_metrics`
    block (receivedAt · respondedAt · latencyMs). receivedAt derives from
    respondedAt − the latency the caller already measured."""
    latency = fields.pop("latency_ms", None)
    responded = int(time.time() * 1000)
    payload = {"event": "turn", "component": "bff", "view": kind,
               **{k: v for k, v in fields.items() if v not in (None, "", [])}}
    tp = tracing.outbound_headers().get("traceparent")
    if tp:
        payload["traceparent"] = tp
    lm = {"respondedAt": responded}
    if isinstance(latency, (int, float)):
        lm["receivedAt"] = responded - int(latency)
        lm["latencyMs"] = int(latency)
    payload["latency_metrics"] = lm
    logger.info("%s turn", kind, extra={"json_fields": payload})


async def _user_id_from_iap(request: Request) -> str | None:
    """Agent Engine user_id for this request. IAP on → the verified EMAIL; IAP
    off → None (agent_client falls back to AGENT_DEV_USER_ID). Raises
    iap.IdentityError (→ 401 via the handler) when IAP is on but the request
    carries no valid assertion. See iap.py.

    Runs the verify OFF the event loop: an occasional cert refresh touches the
    network, and blocking the loop in-line previously starved /healthz and got
    the instance killed by the liveness probe (the agent-path 503s)."""
    return await asyncio.to_thread(iap.resolve_user_id, request.headers)


# ── Admin API (ACL management — backend of the admin view) ───────────────────
@app.exception_handler(admin.AdminError)
async def _on_admin_error(request: Request, exc: admin.AdminError):
    return JSONResponse({"error": "forbidden", "detail": str(exc)}, status_code=403)


@app.exception_handler(admin.ValidationError)
async def _on_admin_validation(request: Request, exc: admin.ValidationError):
    return JSONResponse({"error": "invalid", "detail": str(exc)}, status_code=400)


async def _admin_identity(request: Request) -> str:
    """The caller's email, verified admin (403 otherwise). IAP off (local dev)
    falls back to AGENT_DEV_USER_ID — is_admin still requires a real Firestore
    doc, so a dev identity has no power unless deliberately granted."""
    email = await _user_id_from_iap(request) or os.environ.get("AGENT_DEV_USER_ID")
    return await asyncio.to_thread(
        lambda: admin.require_admin(admin.store(), email))


@app.get("/api/me")
async def api_me(request: Request):
    email = await _user_id_from_iap(request) or os.environ.get("AGENT_DEV_USER_ID")
    if not email:
        return {"user_id": None, "is_admin": False}
    ok = await asyncio.to_thread(lambda: admin.is_admin(admin.store(), email))
    return {"user_id": email.strip().lower(), "is_admin": ok}


@app.get("/api/admin/acl")
async def api_admin_acl(request: Request):
    await _admin_identity(request)
    return await asyncio.to_thread(lambda: admin.list_acl(admin.store()))


@app.get("/api/admin/agents")
async def api_admin_agents(request: Request):
    await _admin_identity(request)
    return {"agents": await asyncio.to_thread(admin.list_registry_agents)}


@app.put("/api/admin/roles/{name}")
async def api_admin_put_role(name: str, request: Request):
    await _admin_identity(request)
    body = await request.json()
    await asyncio.to_thread(lambda: admin.put_role(
        admin.store(), name, body.get("agents") or [], bool(body.get("is_admin"))))
    return {"status": "ok"}


@app.delete("/api/admin/roles/{name}")
async def api_admin_delete_role(name: str, request: Request):
    await _admin_identity(request)
    await asyncio.to_thread(lambda: admin.delete_role(admin.store(), name))
    return {"status": "ok"}


@app.put("/api/admin/users/{email}")
async def api_admin_put_user(email: str, request: Request):
    await _admin_identity(request)
    body = await request.json()
    await asyncio.to_thread(lambda: admin.put_user(admin.store(), email, body.get("roles") or []))
    return {"status": "ok"}


@app.delete("/api/admin/users/{email}")
async def api_admin_delete_user(email: str, request: Request):
    await _admin_identity(request)
    await asyncio.to_thread(lambda: admin.delete_user(admin.store(), email))
    return {"status": "ok"}


# ── Trace Explorer (admin view — reads the four ai-logs, no data emitted) ─────
@app.get("/api/admin/traces")
async def api_admin_traces(request: Request):
    """Recent transactions (newest first) from the per-turn BFF anchor, with
    optional ?user= / ?view= / ?limit= filters."""
    await _admin_identity(request)
    user = request.query_params.get("user") or None
    view = request.query_params.get("view") or None
    try:
        limit = min(max(int(request.query_params.get("limit", "50")), 1), 200)
    except ValueError:
        limit = 50
    traces = await asyncio.to_thread(
        lambda: trace_explorer.list_recent_traces(
            trace_explorer.client(), limit=limit, user=user, view=view))
    return {"traces": traces}


@app.get("/api/admin/sessions")
async def api_admin_sessions(request: Request):
    """The user → session → trace navigator tree (admin)."""
    await _admin_identity(request)
    user = request.query_params.get("user") or None
    try:
        limit = min(max(int(request.query_params.get("limit", "200")), 1), 500)
    except ValueError:
        limit = 200
    return await asyncio.to_thread(
        lambda: trace_explorer.get_session_tree(
            trace_explorer.client(), limit=limit, user=user))


@app.get("/api/admin/traces/{trace_id}")
async def api_admin_trace(trace_id: str, request: Request):
    """One trace reconstructed across all four logs → drawable flow."""
    await _admin_identity(request)
    try:
        return await asyncio.to_thread(
            lambda: trace_explorer.get_trace(trace_explorer.client(), trace_id))
    except ValueError as exc:
        return JSONResponse({"error": "invalid", "detail": str(exc)}, status_code=400)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.server:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), reload=True)
