"""Apigee-fronted Gemini model + host-scoped TLS skip.

Shared verbatim by the supervisor and all three specialists — this is the block
that was previously copy-pasted (and drifted) across five ``agent.py`` files.
See package docstring for the vendoring contract.
"""
import contextvars
import time
import logging
import os
import re
from urllib.parse import urlparse

import google.auth
from google.adk.models.apigee_llm import ApigeeLlm

# A2A request-metadata key carrying the END USER's identity from the supervisor
# to its specialists (set by client.user_email_meta_provider, read by
# specialist.force_sse_request_converter). Namespaced to avoid colliding with
# ADK's own a2a metadata prefix.
USER_EMAIL_META_KEY = "adk_user_email"
# ...and the W3C trace context, threaded the same way (BFF -> streamQuery input
# -> contextvar -> model-call header + A2A metadata -> specialist contextvar).
# Engines can't read HTTP headers, so the value travels IN-BAND like the email.
TRACEPARENT_META_KEY = "adk_traceparent"
# ...and the FRONTEND (Agent Engine) session id — the conversation the user is in.
# The supervisor's own ADK session IS the frontend session, but an A2A specialist
# spins up its OWN session, so without propagation a specialist logs a divergent
# id and can't be tied back to the conversation. Threaded like the email/trace.
SESSION_META_KEY = "adk_session_id"

_TRACEPARENT_RE = re.compile(r"^[0-9a-f]{2}-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")
# Model-call start time (epoch ms), stamped by the before_model callback and
# read by the after_model usage log — both run in the same async task, so a
# contextvar pairs them without cross-request bleed (parallel calls in OTHER
# tasks each see their own value).
_model_call_started: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "a2a_model_call_started", default=None)

_incoming_traceparent: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "incoming_traceparent", default=None)


def set_incoming_traceparent(tp) -> None:
    """Adopt the caller's W3C trace context for this request's task tree
    (contextvar: asyncio-safe, no cross-user bleed). Invalid values ignored."""
    if isinstance(tp, str) and _TRACEPARENT_RE.match(tp.strip()):
        _incoming_traceparent.set(tp.strip())


def incoming_traceparent() -> str | None:
    return _incoming_traceparent.get()


_incoming_session: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "incoming_session", default=None)


def set_incoming_session(sid) -> None:
    """Adopt the caller's (frontend) session id for this request's task tree —
    contextvar, so parallel A2A calls in other tasks keep their own."""
    if isinstance(sid, str) and sid.strip():
        _incoming_session.set(sid.strip())


def incoming_session() -> str | None:
    return _incoming_session.get()


_log_setup_attempted = False


def _ensure_engine_logging() -> None:
    """Attach the named-log handler from WHEREVER provably executes. The
    A2aAgent template does NOT call set_up() the way AdkApp does (verified
    live: only the supervisor's workers emitted log_setup markers), so
    specialists attach lazily on their first model call. Once per process."""
    global _log_setup_attempted
    if _log_setup_attempted:
        return
    _log_setup_attempted = True
    setup_engine_cloud_logging()


def _latency_metrics(started: int | None) -> dict:
    """CANONICAL latency block (shared by every component): receivedAt (when the
    operation started — for a client-side call, when we sent it), respondedAt
    (now), latencyMs. `started` is None when the paired before-hook didn't run."""
    responded = int(time.time() * 1000)
    out = {"respondedAt": responded}
    if started:
        out["receivedAt"] = started
        out["latencyMs"] = responded - started
    return out


def usage_logging_after_model_callback(callback_context, llm_response):
    """after_model_callback: one structured agent-ai-logs entry PER MODEL CALL
    with the token usage — trace-linked (the named-log handler attaches the
    threaded traceparent), so a turn's trace filter shows exactly which agent
    burned which tokens, next to the gateway's view of the same calls. Bodies
    are NOT logged here (ADK's span content capture covers prompt/response
    detail when the console toggle is on)."""
    _ensure_engine_logging()   # specialists: A2aAgent has no set_up hook (see above)
    um = getattr(llm_response, "usage_metadata", None)
    if um is None or getattr(um, "total_token_count", None) is None:
        return None   # SSE partials carry no usage — logging them was pure noise
    ic = getattr(callback_context, "_invocation_context", None)
    uid = getattr(getattr(ic, "session", None), "user_id", None)
    # session_id = the USER/frontend session (the correlation key for grouping);
    # agentSessionId = THIS agent's own ADK session, logged when it differs so a
    # specialist's own A2A session is tracked AS PART OF the user session. For the
    # supervisor the two coincide, so only session_id is written.
    local_sid = getattr(getattr(ic, "session", None), "id", None)
    user_sid = incoming_session() or local_sid
    logging.getLogger("agent").info(
        "model call", extra={"json_fields": {
            "event": "model_call",
            "component": "engine",
            "engine": os.environ.get("DISPLAY_NAME", ""),   # API-written entries carry no engine resource label
            "agent": getattr(callback_context, "agent_name", None),
            "user": uid,
            "session_id": user_sid,
            **({"agentSessionId": local_sid} if local_sid and local_sid != user_sid else {}),
            "promptTokens": getattr(um, "prompt_token_count", None),
            "outputTokens": getattr(um, "candidates_token_count", None),
            "totalTokens": getattr(um, "total_token_count", None),
            "latency_metrics": _latency_metrics(_model_call_started.get()),
            # raw traceparent in the payload like every other component's
            # record (the entry's trace ATTRIBUTE is set too, by the handler)
            "traceparent": incoming_traceparent(),
        }})
    return None


# ── Orchestration spans: agent_turn / tool_call / a2a_call ───────────────────
# The model_call record only covers model time; these three cover the rest of
# an agent's wall-clock (its own run, each tool call, each A2A delegation) so
# the trace's latency has no unattributed gaps. Same envelope + latency_metrics
# as every other component. before-hook stamps a contextvar; after-hook logs.
_agent_started: contextvars.ContextVar[int | None] = contextvars.ContextVar("agent_started", default=None)
_a2a_started: contextvars.ContextVar[int | None] = contextvars.ContextVar("a2a_started", default=None)
_tool_started: contextvars.ContextVar[int | None] = contextvars.ContextVar("tool_started", default=None)


def _engine_identity(ctx) -> dict:
    ic = getattr(ctx, "_invocation_context", None)
    sess = getattr(ic, "session", None)
    local = getattr(sess, "id", None)
    user_sid = incoming_session() or local
    out = {"engine": os.environ.get("DISPLAY_NAME", ""),
           "agent": getattr(ctx, "agent_name", None),
           "user": getattr(sess, "user_id", None),
           "session_id": user_sid}
    if local and local != user_sid:
        out["agentSessionId"] = local
    return out


def _emit_engine(event: str, fields: dict, started: int | None) -> None:
    _ensure_engine_logging()
    logging.getLogger("agent").info(event, extra={"json_fields": {
        "event": event, "component": "engine", **fields,
        "latency_metrics": _latency_metrics(started),
        "traceparent": incoming_traceparent()}})


def before_agent_log(callback_context):
    """before_agent_callback: stamp the agent's run start."""
    _agent_started.set(int(time.time() * 1000))
    return None


def after_agent_log(callback_context):
    """after_agent_callback: one agent_turn record — this agent's whole run."""
    _emit_engine("agent_turn", _engine_identity(callback_context), _agent_started.get())
    return None


def before_a2a_log(callback_context):
    """before_agent_callback on a RemoteA2aAgent — stamp the delegation start."""
    _a2a_started.set(int(time.time() * 1000))
    return None


def after_a2a_log(callback_context):
    """after_agent_callback on a RemoteA2aAgent — one a2a_call record (the
    delegation round trip, from the caller's side)."""
    ident = _engine_identity(callback_context)
    ident["targetAgent"] = ident.get("agent")
    _emit_engine("a2a_call", ident, _a2a_started.get())
    return None


def before_tool_log(tool, args, tool_context):
    """before_tool_callback: stamp the tool call start."""
    _tool_started.set(int(time.time() * 1000))
    return None


def after_tool_log(tool, args, tool_context, tool_response):
    """after_tool_callback: one tool_call record (e.g. an MCP invocation)."""
    ident = _engine_identity(tool_context)
    ident["tool"] = getattr(tool, "name", None)
    _emit_engine("tool_call", ident, _tool_started.get())
    return None


def setup_engine_cloud_logging(log_name: str = "agent-ai-logs") -> None:
    """Route this engine's Python logging to a NAMED Cloud Logging log with the
    request's trace ATTACHED (from the threaded traceparent) — engine lines
    then join the per-turn one-filter view natively, alongside front-ai-logs
    and apigee-ai-logs. Platform stdout capture keeps existing as a fallback
    (its logName is fixed by the platform and can't be renamed).

    Best-effort: missing library or credentials (local dev, tests) = no-op.
    Idempotent per process. Requires roles/logging.logWriter on the runner SA
    (granted in agents/runtime-manifest.yaml)."""
    try:
        import google.cloud.logging as gcl
        from google.cloud.logging_v2.handlers import CloudLoggingHandler
    except ImportError:
        return
    global _log_setup_attempted
    _log_setup_attempted = True
    root = logging.getLogger()
    if any(isinstance(h, CloudLoggingHandler) for h in root.handlers):
        return
    try:
        client = gcl.Client()
        project = client.project
    except Exception:  # noqa: BLE001 — no ADC etc.
        return

    class _TraceNamedHandler(CloudLoggingHandler):
        # handle() runs BEFORE the handler's own CloudLoggingFilter computes
        # its trace fields, so setting record.trace here is honored.
        def handle(self, record):
            # Curated log: drop routed Python warnings (ADK's [EXPERIMENTAL]
            # UserWarning spam — 641 entries in one live window). They remain
            # in the platform stderr log for anyone who wants them.
            if record.name == "py.warnings":
                return False
            tp = incoming_traceparent()
            if tp and not getattr(record, "trace", None):
                record.trace = f"projects/{project}/traces/{tp.split('-')[1]}"
            return super().handle(record)

    root.addHandler(_TraceNamedHandler(client, name=log_name))
    if root.level in (logging.NOTSET, logging.WARNING):
        root.setLevel(logging.INFO)
    # Startup marker: proves from the log itself WHICH engines attached the
    # named-log handler (specialist attachment was unverifiable otherwise).
    logging.getLogger("agent").info(
        "named log attached", extra={"json_fields": {
            "event": "log_setup", "engine": os.environ.get("DISPLAY_NAME", "")}})


def user_attribution_model_callback(callback_context, llm_request):
    """before_model_callback: stamp x-user-email on this agent's model calls so
    the Apigee LLM proxy attributes tokens per USER (dc_user_id), not just per
    agent app. Works wherever session.user_id is the user's email — the
    supervisor gets it from the BFF; specialists get it via the A2A request
    metadata (USER_EMAIL_META_KEY -> AgentRunRequest.user_id).

    Safe per-request: genai merges request-level http_options.headers OVER the
    client-level ones ({**client, **request} in patch_http_options), so the
    x-api-key survives and nothing shared is mutated. The proxy captures the
    header and strips it before the upstream call. Non-email user_ids (e.g. the
    A2A_USER_<ctx> fallback when no email was propagated) are NOT sent."""
    _model_call_started.set(int(time.time() * 1000))   # paired in the after hook
    if llm_request.config is None:
        return None
    extra: dict[str, str] = {}
    ic = getattr(callback_context, "_invocation_context", None)
    uid = getattr(getattr(ic, "session", None), "user_id", None)
    if isinstance(uid, str) and "@" in uid:
        extra["x-user-email"] = uid.strip().lower()
    tp = incoming_traceparent()
    if tp:
        # The gateway (traceConfig) parents its spans under this trace and the
        # interaction log carries it — the whole turn shares ONE trace id.
        extra["traceparent"] = tp
    if not extra:
        return None
    from google.genai import types as genai_types

    ho = llm_request.config.http_options
    if ho is None:
        llm_request.config.http_options = genai_types.HttpOptions(headers=extra)
    else:
        ho.headers = {**(ho.headers or {}), **extra}
    return None



def configure_genai_env() -> None:
    """Set the GenAI / GEAP env defaults every engine needs.

    Uses ``setdefault`` so an explicit deploy-time env var always wins.

      * GOOGLE_CLOUD_PROJECT  — from ADC if unset.
      * GOOGLE_CLOUD_LOCATION — ``global`` (Apigee owns upstream region).
      * GOOGLE_GENAI_USE_VERTEXAI — kept True for parity; a no-op for the model
        because ``apigee/gemini/`` forces Gemini-provider mode regardless.
      * GEMINI_API_KEY — dummy. Gemini-mode client requires *a* key but never
        uses it (Apigee owns upstream auth). MUST be GEMINI_API_KEY, not
        GOOGLE_API_KEY: the latter is global and the GEAP session-service
        client would pick it up and 401.
    """
    _, project_id = google.auth.default()
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", project_id or "")
    os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")
    os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "True")
    os.environ.setdefault("GEMINI_API_KEY", "unused-dummy-key")


def install_scoped_tls_skip() -> None:
    """Disable TLS verification ONLY for the Apigee gateway host(s).

    The Apigee gateway terminates TLS with a cert that won't validate for our
    private hostname (no public CA). When ``APIGEE_LLM_INSECURE_TLS`` is truthy
    we skip verification — but only for the hosts derived from
    ``APIGEE_LLM_PROXY_URL`` / ``APIGEE_MCP_URL``, never process-wide. All other
    egress (GEAP session service, A2A, Agent Registry) keeps full validation.
    """
    if os.environ.get("APIGEE_LLM_INSECURE_TLS", "").lower() not in ("1", "true", "yes"):
        return

    import httpx

    insecure_hosts = {
        urlparse(u).hostname
        for u in (
            os.environ.get("APIGEE_LLM_PROXY_URL"),
            os.environ.get("APIGEE_MCP_URL"),
        )
        if u
    }
    insecure_hosts.discard(None)
    if not insecure_hosts:
        return

    class _ScopedAsyncTransport(httpx.AsyncHTTPTransport):
        def __init__(self) -> None:
            super().__init__()
            self._insecure = httpx.AsyncHTTPTransport(verify=False)

        async def handle_async_request(self, request):
            if request.url.host in insecure_hosts:
                return await self._insecure.handle_async_request(request)
            return await super().handle_async_request(request)

    class _ScopedSyncTransport(httpx.HTTPTransport):
        def __init__(self) -> None:
            super().__init__()
            self._insecure = httpx.HTTPTransport(verify=False)

        def handle_request(self, request):
            if request.url.host in insecure_hosts:
                return self._insecure.handle_request(request)
            return super().handle_request(request)

    _orig_async_init = httpx.AsyncClient.__init__
    _orig_sync_init = httpx.Client.__init__

    def _async_init(self, *args, **kwargs):
        kwargs.setdefault("transport", _ScopedAsyncTransport())
        _orig_async_init(self, *args, **kwargs)

    def _sync_init(self, *args, **kwargs):
        kwargs.setdefault("transport", _ScopedSyncTransport())
        _orig_sync_init(self, *args, **kwargs)

    httpx.AsyncClient.__init__ = _async_init
    httpx.Client.__init__ = _sync_init

    # google-genai (the client behind ApigeeLlm) makes its async model calls
    # over aiohttp, not httpx, so the transport patch above never sees them.
    # Patch aiohttp too — host-scoped ssl=False for the Apigee gateway only.
    try:
        import aiohttp
        from yarl import URL as _AioURL
    except ImportError:
        return

    _orig_aiohttp_request = aiohttp.ClientSession._request

    async def _scoped_aiohttp_request(self, method, str_or_url, **kwargs):
        try:
            _host = _AioURL(str_or_url).host
        except Exception:
            _host = None
        if _host in insecure_hosts and kwargs.get("ssl") is None:
            kwargs["ssl"] = False
        return await _orig_aiohttp_request(self, method, str_or_url, **kwargs)

    aiohttp.ClientSession._request = _scoped_aiohttp_request


def _rebuild_model() -> "EnvKeyApigeeLlm":
    """Unpickle hook — reconstructs the model from the CURRENT env."""
    return build_apigee_model()


class EnvKeyApigeeLlm(ApigeeLlm):
    """ApigeeLlm that pickles as a RECIPE, not a snapshot.

    Agent Engine runs the UNPICKLED object graph — module-level code does not
    feed it — so a plain ApigeeLlm freezes whatever x-api-key existed on the
    deploy host into the pickle (verified live: engines sent the deploy-time
    placeholder and Apigee 401'd InvalidApiKey). Overriding ``__reduce__`` makes
    unpickling call build_apigee_model() ON THE ENGINE, where Agent Engine has
    injected the real APIGEE_API_KEY from the SecretRef — and it means no key
    material (or placeholder) is ever serialized into the staging-bucket pickle.
    """

    def __reduce__(self):
        return (_rebuild_model, ())


def build_apigee_model() -> EnvKeyApigeeLlm:
    """Construct the ApigeeLlm. Runs env config + TLS skip as a side effect.

    Explicit ``apigee/gemini/`` forces Gemini-provider mode regardless of
    GOOGLE_GENAI_USE_VERTEXAI, so the client attaches no ADC/bearer — Apigee
    owns upstream GEAP auth and the agent only presents its own x-api-key.
    """
    configure_genai_env()
    install_scoped_tls_skip()

    proxy_url = os.environ.get("APIGEE_LLM_PROXY_URL")
    # One key per agent: APIGEE_API_KEY is this agent's single Apigee key (its app
    # is authorized for the LLM gateway AND the MCP gateway). Falls back to the
    # legacy APIGEE_LLM_API_KEY for a transition window.
    api_key = os.environ.get("APIGEE_API_KEY") or os.environ.get("APIGEE_LLM_API_KEY")
    if not api_key and os.environ.get("APIGEE_API_KEY_SECRET"):
        # DEPLOY-HOST import: the .env deliberately holds no key — the deploy tool
        # sends a SecretRef and Agent Engine injects the real APIGEE_API_KEY at
        # RUNTIME, where this module re-executes with it set. A placeholder lets
        # the deploy-time import/pickle proceed; on the engine the strict check
        # below still fails loudly if the injection is ever missing.
        api_key = "deploy-time-placeholder-see-APIGEE_API_KEY_SECRET"
    if not proxy_url or not api_key:
        raise RuntimeError(
            "APIGEE_LLM_PROXY_URL and APIGEE_API_KEY must be set (see .env) —"
            " or APIGEE_API_KEY_SECRET for SecretRef deploys."
        )
    return EnvKeyApigeeLlm(
        model=f"apigee/gemini/{os.environ.get('GEMINI_MODEL', 'gemini-3.1-flash-lite')}",
        proxy_url=proxy_url,
        custom_headers={"x-api-key": api_key},
    )
