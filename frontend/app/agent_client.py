"""Talk to the supervisor Agent Engine (reasoning engine) from the BFF.

The agent is reached **only through Apigee** — the BFF posts to the `/ai-agents`
proxy with an `x-api-key`, and Apigee injects the Google SA token and rewrites to
the full Vertex reasoning-engine URL. The client sends just the bare engine id +
the slash verb (`/agents/<id>/<query|streamQuery>`).

Session state lives server-side in Agent Engine, keyed by user_id. v1 creates a
session on the first turn and the browser echoes the returned session_id back on
subsequent turns; resume-across-reload (list_sessions) is a later nicety.
"""
import asyncio
import json
import os
import time

import httpx

try:
    from . import tracing
except ImportError:  # tests import these modules top-level (frontend/app on sys.path)
    import tracing

# ── Config ───────────────────────────────────────────────────────────────────
# AGENT_BASE_URL = the Apigee /ai-agents base, e.g. https://<host>/ai-agents
BASE_URL = os.environ.get("AGENT_BASE_URL", "").rstrip("/")
# AGENT_ENGINE_ID may be a bare id, a full resource name, or an Agent Registry
# URN — Apigee only needs the trailing id (it holds project/location).
ENGINE_ID = os.environ.get("AGENT_ENGINE_ID", "").strip().rsplit(":", 1)[-1].rsplit("/", 1)[-1]
DEV_USER_ID = os.environ.get("AGENT_DEV_USER_ID", "local-dev")
APIGEE_AGENT_API_KEY = os.environ.get("APIGEE_AGENT_API_KEY", "")
# TLS skip for the private/nip.io Apigee host (no publicly trusted cert).
_INSECURE = os.environ.get("AGENT_INSECURE_TLS", "false").lower() in ("1", "true", "yes")

# supervisor sub-agent display name → friendly label for the delegation trace
DELEGATION_LABELS = {
    "order_agent": "Order Management",
    "product_agent": "Product Catalog",
    "customer_agent": "Customer Accounts",
    "Workspace_Agent": "Workspace",
}


class AgentError(Exception):
    """Raised with a human-readable message when an agent call fails."""


# ── Auth ─────────────────────────────────────────────────────────────────────
async def _auth_headers() -> dict:
    if not APIGEE_AGENT_API_KEY:
        raise AgentError("APIGEE_AGENT_API_KEY is required.")
    return {"x-api-key": APIGEE_AGENT_API_KEY}


def _user_header(agent_input: dict | None) -> dict:
    """x-user-email for Apigee per-user analytics (captured into dc_user_id by
    the /ai-agents proxy, then stripped before the upstream call). The value is
    the request's Agent Engine user_id — the IAP-VERIFIED email the BFF
    resolved, never anything client-supplied. Every agent_input already
    carries user_id, so this needs no extra plumbing at call sites."""
    uid = (agent_input or {}).get("user_id")
    return {"x-user-email": uid} if uid else {}


def _url(verb: str) -> str:
    """verb ∈ {query, streamQuery}. → {Apigee /ai-agents}/agents/<id>/<verb>."""
    if not BASE_URL:
        raise AgentError("AGENT_BASE_URL (the Apigee /ai-agents base) is required.")
    if not ENGINE_ID:
        raise AgentError("AGENT_ENGINE_ID is not set.")
    return f"{BASE_URL}/agents/{ENGINE_ID}/{verb}"


# ── Low-level calls ──────────────────────────────────────────────────────────
async def _post(verb: str, class_method: str, agent_input: dict) -> str:
    headers = {"Content-Type": "application/json", **(await _auth_headers()),
               **_user_header(agent_input), **tracing.outbound_headers()}
    body = {"class_method": class_method, "input": agent_input}
    async with httpx.AsyncClient(timeout=httpx.Timeout(180.0), verify=not _INSECURE) as client:
        resp = await client.post(_url(verb), headers=headers, json=body)
    if resp.status_code >= 400:
        raise AgentError(f"{class_method} -> HTTP {resp.status_code}: {resp.text[:600]}")
    return resp.text


def _parse_event_stream(text: str) -> list:
    """:streamQuery / :query bodies are application/json — either one object, a
    JSON array, or several concatenated objects. Handle all three."""
    text = text.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
        return data if isinstance(data, list) else [data]
    except json.JSONDecodeError:
        pass
    events, dec, idx, n = [], json.JSONDecoder(), 0, len(text)
    while idx < n:
        while idx < n and text[idx] in " \r\n\t":
            idx += 1
        if idx >= n:
            break
        obj, idx = dec.raw_decode(text, idx)
        events.append(obj)
    return events


def _session_id_from(output) -> str:
    """Pull a session id out of an async_create_session / session object."""
    if isinstance(output, dict):
        for key in ("id", "session_id", "sessionId"):
            if output.get(key):
                return str(output[key])
        name = output.get("name")
        if isinstance(name, str) and name:
            return name.rsplit("/", 1)[-1]
    raise AgentError(f"Could not find a session id in response: {str(output)[:300]}")


# ── Public API ───────────────────────────────────────────────────────────────
async def create_session(user_id: str) -> str:
    raw = await _post("query", "async_create_session", {"user_id": user_id})
    events = _parse_event_stream(raw)
    if not events:
        raise AgentError("Empty response from async_create_session.")
    ev = events[-1]
    if isinstance(ev, dict) and ev.get("error_code"):
        raise AgentError(f"{ev.get('error_code')}: {ev.get('error_message')}")
    output = ev.get("output", ev) if isinstance(ev, dict) else ev
    return _session_id_from(output)


def _aggregate(events: list) -> dict:
    text_parts, thought_parts = [], []
    delegations, usage = [], {"prompt": 0, "output": 0, "total": 0}
    for ev in events:
        if not isinstance(ev, dict):
            continue
        if ev.get("error_code") or ev.get("error_message"):
            # A2A hop failures carry error_message with NO error_code (seen
            # live: deleted-engine 403s rendered as '(no text returned)').
            raise AgentError(
                f"{ev.get('error_code') or 'A2A_ERROR'}: {ev.get('error_message')}")
        content = ev.get("content") or {}
        for part in content.get("parts") or []:
            txt = part.get("text")
            # Model "thought" parts (the specialist echoes injected "For
            # context:" scaffolding + prior turns as thoughts) go to a separate
            # "thinking" channel — never into the visible answer.
            if txt:
                (thought_parts if part.get("thought") else text_parts).append(txt)
            fc = part.get("function_call") or {}
            if fc.get("name") == "transfer_to_agent":
                name = (fc.get("args") or {}).get("agent_name")
                if name:
                    delegations.append(
                        {"agent_name": name, "label": DELEGATION_LABELS.get(name, name)}
                    )
        um = ev.get("usage_metadata") or {}
        if um:
            usage["prompt"] += um.get("prompt_token_count", 0) or 0
            usage["output"] += um.get("candidates_token_count", 0) or 0
            usage["total"] += um.get("total_token_count", 0) or 0
    return {
        "text": "".join(text_parts).strip(),
        "thoughts": "".join(thought_parts).strip() or None,
        "delegations": delegations,
        "usage": usage if usage["total"] else None,
    }


async def agent_turn(message: str, session_id: str | None, user_id: str | None = None) -> dict:
    """Run one supervisor turn. Returns
    {text, session_id, delegations, usage, latency_ms}."""
    user_id = user_id or DEV_USER_ID
    started = time.monotonic()
    if not session_id:
        session_id = await create_session(user_id)
        _cache_title(session_id, message)  # first message becomes the title
    raw = await _post(
        "streamQuery",
        "async_stream_query",
        {"message": message, "user_id": user_id, "session_id": session_id,
         **tracing.outbound_headers()},   # in-band: the engine can't read HTTP headers
    )
    result = _aggregate(_parse_event_stream(raw))
    result["session_id"] = session_id
    result["latency_ms"] = int((time.monotonic() - started) * 1000)
    return result


# ── Streaming turn (SSE source) ──────────────────────────────────────────────
def _drain_json(buf: str):
    """Pull complete JSON objects from the front of `buf`.

    The :streamQuery body is application/json arriving in chunks — either a JSON
    array ([{…},{…}]) or concatenated objects. Chunk boundaries don't align to
    objects, so we decode greedily and keep any trailing partial for next time.
    Returns (remaining_buf, [objects])."""
    objs, i, n, dec = [], 0, len(buf), json.JSONDecoder()
    while i < n:
        while i < n and buf[i] in " \r\n\t,[]":  # skip separators / array wrapper
            i += 1
        if i >= n:
            break
        try:
            obj, end = dec.raw_decode(buf, i)
        except json.JSONDecodeError:
            break  # incomplete object — wait for more bytes
        objs.append(obj)
        i = end
    return buf[i:], objs


async def _stream_events(verb: str, class_method: str, agent_input: dict):
    """Async-generate parsed ADK events from a streamed :streamQuery call."""
    headers = {
        "Content-Type": "application/json",
        "Accept-Encoding": "identity",  # plain SSE — don't gzip a streamed body
        **(await _auth_headers()),
        **_user_header(agent_input),
        **tracing.outbound_headers(),
    }
    body = {"class_method": class_method, "input": agent_input}
    timeout = httpx.Timeout(connect=15.0, read=None, write=30.0, pool=15.0)
    async with httpx.AsyncClient(timeout=timeout, verify=not _INSECURE) as client:
        async with client.stream("POST", _url(verb), headers=headers, json=body) as resp:
            if resp.status_code >= 400:
                detail = (await resp.aread()).decode("utf-8", "replace")
                raise AgentError(f"{class_method} -> HTTP {resp.status_code}: {detail[:600]}")
            buf = ""
            async for chunk in resp.aiter_text():
                buf += chunk
                buf, objs = _drain_json(buf)
                for obj in objs:
                    yield obj
            _, objs = _drain_json(buf)
            for obj in objs:
                yield obj


async def agent_turn_stream(
    message: str,
    session_id: str | None,
    user_id: str | None = None,
    debug: bool = False,
    stream_tokens: bool = False,
):
    """Async generator of UI events for one supervisor turn:
      {type:'session',   session_id}
      {type:'delegation', agent_name, label}   # emitted as the handover happens
      {type:'text',       text}                 # answer deltas
      {type:'raw',        data}                 # the parsed Agent Engine event (debug only)
      {type:'done',       session_id, usage, latency_ms}
      {type:'error',      message}

    stream_tokens=True asks Agent Engine to run with RunConfig streaming_mode=SSE
    so the model emits *partial* (token-level) events. We then stream the partial
    text deltas and skip the trailing consolidated event for that message (which
    repeats the full text). Falls back cleanly to message-level events otherwise.
    Errors are yielded (not raised) so the SSE endpoint can always close cleanly."""
    user_id = user_id or DEV_USER_ID
    started = time.monotonic()
    usage = {"prompt": 0, "output": 0, "total": 0}
    try:
        if not session_id:
            session_id = await create_session(user_id)
            _cache_title(session_id, message)
        yield {"type": "session", "session_id": session_id}
        agent_input = {"message": message, "user_id": user_id, "session_id": session_id,
                       **tracing.outbound_headers()}  # in-band traceparent (engines can't read headers)
        if stream_tokens:
            # ADK RunConfig.streaming_mode enum is lowercase: None | "sse" | "bidi".
            agent_input["run_config"] = {"streaming_mode": "sse"}
        saw_partial = False  # to skip the consolidated event that follows partials
        saw_thought_partial = False  # same dedup, independently, for thoughts
        async for ev in _stream_events("streamQuery", "async_stream_query", agent_input):
            if not isinstance(ev, dict):
                continue
            if debug:
                yield {"type": "raw", "data": ev}
            if ev.get("error_code") or ev.get("error_message"):
                yield {"type": "error", "message":
                       f"{ev.get('error_code') or 'A2A_ERROR'}: {ev.get('error_message')}"}
                return
            content = ev.get("content") or {}
            is_partial = ev.get("partial") is True
            for part in content.get("parts") or []:
                txt = part.get("text")
                # Model "thought" parts → separate "thought" event (collapsible
                # thinking panel), never the visible answer. Same partial/
                # consolidated dedup as text, tracked independently.
                if txt and part.get("thought"):
                    if is_partial:
                        saw_thought_partial = True
                        yield {"type": "thought", "text": txt}
                    elif saw_thought_partial:
                        saw_thought_partial = False  # consolidated repeat — skip
                    else:
                        yield {"type": "thought", "text": txt}
                elif txt:
                    if is_partial:
                        saw_partial = True
                        yield {"type": "text", "text": txt}
                    elif saw_partial:
                        saw_partial = False  # consolidated repeat of the streamed text — skip
                    else:
                        yield {"type": "text", "text": txt}  # message-level (non-streamed)
                fc = part.get("function_call") or {}
                # transfer_to_agent shows up in both the partial and the
                # consolidated event — emit the chip once (on the consolidated).
                if fc.get("name") == "transfer_to_agent" and not is_partial:
                    name = (fc.get("args") or {}).get("agent_name")
                    if name:
                        yield {
                            "type": "delegation",
                            "agent_name": name,
                            "label": DELEGATION_LABELS.get(name, name),
                        }
            # Count usage only from the consolidated (non-partial) event per model
            # call: the final *partial* event and the consolidated one both carry
            # the full usage_metadata, so summing every event double-counts.
            if not is_partial:
                um = ev.get("usage_metadata") or {}
                if um:
                    usage["prompt"] += um.get("prompt_token_count", 0) or 0
                    usage["output"] += um.get("candidates_token_count", 0) or 0
                    usage["total"] += um.get("total_token_count", 0) or 0
        yield {
            "type": "done",
            "session_id": session_id,
            "usage": usage if usage["total"] else None,
            "latency_ms": int((time.monotonic() - started) * 1000),
        }
    except AgentError as e:
        yield {"type": "error", "message": str(e)}
    except Exception as e:  # noqa: BLE001
        yield {"type": "error", "message": f"agent stream failed: {e}"}


# ── Session history ──────────────────────────────────────────────────────────
def sub_agent_legend() -> list[dict]:
    """The specialists the supervisor can hand off to (for the UI legend)."""
    return [{"agent_name": k, "label": v} for k, v in DELEGATION_LABELS.items()]


# Agent Engine sessions have no native title field, so we derive a label from
# the first user message and cache it (session_id -> title) to avoid re-fetching
# events on every sidebar refresh.
_TITLE_CACHE: dict[str, str] = {}


def _cache_title(session_id: str, text: str) -> None:
    if session_id and text and session_id not in _TITLE_CACHE:
        _TITLE_CACHE[session_id] = " ".join(text.split())[:80]


def _unwrap_output(events: list):
    """Pull the `output` payload from a :query class-method response."""
    if not events:
        return {}
    ev = events[-1]
    if isinstance(ev, dict) and ev.get("error_code"):
        raise AgentError(f"{ev.get('error_code')}: {ev.get('error_message')}")
    return ev.get("output", ev) if isinstance(ev, dict) else {}


def _first_user_text(events) -> str | None:
    for ev in events or []:
        content = (ev or {}).get("content") or {}
        if content.get("role") == "user":
            for part in content.get("parts") or []:
                if part.get("text"):
                    return part["text"]
    return None


def _events_to_turns(events) -> list:
    """Replay stored session events into chat turns:
    [{role:'user',text}, {role:'model',text,delegations}, ...].

    Heuristic: a user content with text starts a user turn; model/tool events
    accumulate into the following model turn. function_response contents (tool
    results, which ADK tags role='user') do not split the model turn."""
    turns: list = []
    pending = None

    def flush():
        nonlocal pending
        if pending and (pending["text"].strip() or pending["thoughts"].strip() or pending["delegations"]):
            pending["text"] = pending["text"].strip()
            pending["thoughts"] = pending["thoughts"].strip() or None
            turns.append(pending)
        pending = None

    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        content = ev.get("content") or {}
        role = content.get("role")
        parts = content.get("parts") or []
        texts, thoughts, delegations, has_func_response = [], [], [], False
        for part in parts:
            # Split thought parts from answer parts so replayed history matches
            # what was shown live (thoughts → collapsible panel).
            if part.get("text"):
                (thoughts if part.get("thought") else texts).append(part["text"])
            if part.get("function_response"):
                has_func_response = True
            fc = part.get("function_call") or {}
            if fc.get("name") == "transfer_to_agent":
                name = (fc.get("args") or {}).get("agent_name")
                if name:
                    delegations.append(
                        {"agent_name": name, "label": DELEGATION_LABELS.get(name, name)}
                    )
        if role == "user" and texts and not has_func_response:
            flush()
            turns.append({"role": "user", "text": "".join(texts).strip()})
        else:
            if pending is None:
                pending = {"role": "model", "text": "", "thoughts": "", "delegations": []}
            pending["text"] += "".join(texts)
            pending["thoughts"] += "".join(thoughts)
            pending["delegations"].extend(delegations)
    flush()
    return turns


async def list_sessions(user_id: str | None = None) -> list:
    """List the current user's sessions, newest first."""
    user_id = user_id or DEV_USER_ID
    raw = await _post("query", "async_list_sessions", {"user_id": user_id})
    out = _unwrap_output(_parse_event_stream(raw))
    raw_sessions = out.get("sessions") if isinstance(out, dict) else None
    sessions = []
    for s in raw_sessions or []:
        if not isinstance(s, dict):
            continue
        sid = s.get("id") or s.get("session_id") or s.get("sessionId")
        if not sid and isinstance(s.get("name"), str):
            sid = s["name"].rsplit("/", 1)[-1]
        if not sid:
            continue
        updated = (
            s.get("lastUpdateTime")
            or s.get("last_update_time")
            or s.get("updateTime")
            or s.get("update_time")
        )
        sid = str(sid)
        sessions.append(
            {
                "session_id": sid,
                "last_update_time": updated,
                "title": _TITLE_CACHE.get(sid) or _first_user_text(s.get("events")),
            }
        )
    sessions.sort(key=lambda x: x.get("last_update_time") or "", reverse=True)
    # ListSessions omits events, so most titles are still empty here. Backfill
    # them (bounded) by fetching the first user message of each, cached after.
    missing = [s for s in sessions if not s["title"]][:40]
    if missing:
        await asyncio.gather(*(_fill_title(s, user_id) for s in missing))
    return sessions


async def _fill_title(session: dict, user_id: str) -> None:
    session["title"] = await _title_for(session["session_id"], user_id)


async def _title_for(session_id: str, user_id: str) -> str | None:
    if session_id in _TITLE_CACHE:
        return _TITLE_CACHE[session_id]
    try:
        for turn in await get_session_history(session_id, user_id):
            if turn.get("role") == "user" and turn.get("text"):
                _cache_title(session_id, turn["text"])
                break
    except AgentError:
        pass
    return _TITLE_CACHE.get(session_id)


async def get_session_history(session_id: str, user_id: str | None = None) -> list:
    """Fetch a session and replay its events into chat turns for the UI."""
    user_id = user_id or DEV_USER_ID
    raw = await _post(
        "query", "async_get_session", {"user_id": user_id, "session_id": session_id}
    )
    out = _unwrap_output(_parse_event_stream(raw))
    events = out.get("events") if isinstance(out, dict) else None
    return _events_to_turns(events or [])


async def delete_session(session_id: str, user_id: str | None = None) -> None:
    """Delete a session (and drop its cached title)."""
    user_id = user_id or DEV_USER_ID
    raw = await _post(
        "query", "async_delete_session", {"user_id": user_id, "session_id": session_id}
    )
    events = _parse_event_stream(raw)
    if events:
        ev = events[-1]
        if isinstance(ev, dict) and ev.get("error_code"):
            raise AgentError(f"{ev.get('error_code')}: {ev.get('error_message')}")
    _TITLE_CACHE.pop(session_id, None)
