"""Direct-model view: the unified Apigee LLM endpoint (/llm-stream/prompt).

Two dropdowns drive routing:
  publisher  (gemini|anthropic) selects the upstream
  complexity (low|high)         selects the model within that upstream
The proxy routes by header; this module just forwards the right headers + body
shape and extracts text/usage from the (Gemini- or Anthropic-shaped) SSE stream.

/llm-stream is the ONLY live unified surface: the buffered /llm/prompt proxy is
RETIRED at the org. `chat()` (the non-streaming /api/chat contract) aggregates
the SSE stream server-side, so both UI modes ride the same proxy — and the same
per-user analytics capture (x-user-email -> dc_user_id).
"""
import json
import os
import time

import httpx

try:
    from . import tracing
except ImportError:  # tests import these modules top-level (frontend/app on sys.path)
    import tracing

# nip.io / private hosts serve a cert that won't validate; skip when truthy.
_INSECURE = os.environ.get("APIGEE_INSECURE_TLS", "true").lower() in ("1", "true", "yes")

# The unified LLM proxy (unified-llm-endpoint-sse-apiproxy, BasePath /llm-stream).
# NO shared key: each end user brings their OWN key (Apigee app-per-user; the
# proxy binds key <-> IAP email and meters the per-key llmQuota). The key
# arrives per request from the UI.
STREAM_URL = os.environ.get("APIGEE_STREAM_URL", "").rstrip("/")
STREAM_PUBLISHERS = ["gemini", "anthropic"]

PUBLISHERS = ["gemini", "anthropic"]
COMPLEXITIES = ["low", "high"]
MODEL_MATRIX = {
    ("gemini", "low"): "gemini-3.1-flash-lite",
    ("gemini", "high"): "gemini-3.1-pro-preview",
    ("anthropic", "low"): "claude-haiku-4-5",
    ("anthropic", "high"): "claude-sonnet-4-6",
}
DEFAULT_PUBLISHER = "gemini"
DEFAULT_COMPLEXITY = "low"


def config() -> dict:
    return {
        "publishers": PUBLISHERS,
        "complexities": COMPLEXITIES,
        "default_publisher": DEFAULT_PUBLISHER,
        "default_complexity": DEFAULT_COMPLEXITY,
        "model_matrix": {f"{p}:{c}": m for (p, c), m in MODEL_MATRIX.items()},
        "streaming": bool(STREAM_URL),
        "streaming_publishers": STREAM_PUBLISHERS,
    }



def _collapse_history(history: list) -> str:
    if len(history) == 1:
        return history[0]["text"]
    transcript = "\n".join(
        f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['text']}"
        for m in history[:-1]
    )
    return f"Conversation so far:\n{transcript}\n\nUser: {history[-1]['text']}"


def _user_header(user_email: str | None) -> dict:
    """x-user-email for Apigee per-user analytics (captured into dc_user_id by
    the /llm proxies, then stripped before the upstream call). Value = the
    IAP-verified email the server resolved; absent -> proxy records 'anonymous'."""
    return {"x-user-email": user_email} if user_email else {}


async def chat(history: list, publisher: str, complexity: str, use_cache: bool,
               user_email: str | None = None, api_key: str | None = None) -> dict:
    """Buffered turn for /api/chat — aggregates the /llm-stream SSE stream
    server-side (the buffered /llm/prompt proxy is retired). Same return shape
    as before: {text, model, publisher, complexity, latency_ms, usage}."""
    publisher = (publisher or DEFAULT_PUBLISHER).lower()
    complexity = (complexity or DEFAULT_COMPLEXITY).lower()
    if publisher not in PUBLISHERS:
        raise ValueError(f"publisher must be one of {PUBLISHERS}")
    if complexity not in COMPLEXITIES:
        raise ValueError(f"complexity must be one of {COMPLEXITIES}")

    text_parts: list[str] = []
    done: dict = {}
    async for event in chat_stream(history, publisher, complexity, use_cache,
                                   user_email=user_email, api_key=api_key):
        kind = event.get("type")
        if kind == "text":
            text_parts.append(event["text"])
        elif kind == "error":
            raise RuntimeError(event.get("message", "stream failed"))
        elif kind == "done":
            done = event
    return {
        "text": "".join(text_parts),
        "model": done.get("model", MODEL_MATRIX[(publisher, complexity)]),
        "publisher": publisher,
        "complexity": complexity,
        "latency_ms": done.get("latency_ms"),
        "usage": done.get("usage"),
    }


# ── Streaming (Direct view testbed) ──────────────────────────────────────────
def _sse_data_object(frame: str):
    """Parse one SSE frame's `data:` payload into a JSON object (or None)."""
    datas = [
        ln.strip()[5:].strip()
        for ln in frame.split("\n")
        if ln.strip().startswith("data:")
    ]
    if not datas:
        return None
    payload = "\n".join(datas).strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None


def _gemini_text(obj: dict) -> str:
    out = []
    for cand in obj.get("candidates", []) or []:
        for part in cand.get("content", {}).get("parts", []) or []:
            if isinstance(part.get("text"), str):
                out.append(part["text"])
    return "".join(out)


def _gemini_usage(obj: dict):
    um = obj.get("usageMetadata") or {}
    if not um:
        return None
    return {
        "prompt": um.get("promptTokenCount"),
        "output": um.get("candidatesTokenCount"),
        "total": um.get("totalTokenCount"),
    }


def _extract_stream_piece(publisher: str, obj: dict, state: dict):
    """Return (text_delta_or_None, usage_or_None) for one parsed SSE data object.

    Gemini carries text + a full usage block per event. Anthropic is event-typed
    and splits usage across frames (input on message_start, output on
    message_delta), so we accumulate into `state` and finalize after the stream."""
    if publisher == "anthropic":
        t = obj.get("type")
        if t == "message_start":
            u = (obj.get("message") or {}).get("usage") or {}
            if u.get("input_tokens") is not None:
                state["in"] = u["input_tokens"]
            if u.get("output_tokens") is not None:
                state["out"] = u["output_tokens"]
        elif t == "content_block_delta":
            delta = obj.get("delta") or {}
            if delta.get("type") == "text_delta":
                return delta.get("text"), None
        elif t == "message_delta":
            u = obj.get("usage") or {}
            if u.get("output_tokens") is not None:
                state["out"] = u["output_tokens"]
        return None, None
    # Gemini
    return (_gemini_text(obj) or None), _gemini_usage(obj)


async def chat_stream(history: list, publisher: str, complexity: str, use_cache: bool,
                      debug: bool = False, user_email: str | None = None,
                      api_key: str | None = None):
    """Async generator of SSE-ready events for the Direct view streaming testbed:
      {type:'text',  text}
      {type:'raw',   data}      # the parsed upstream event (only when debug=True)
      {type:'done',  model, publisher, complexity, usage, latency_ms}
      {type:'error', message}"""
    publisher = (publisher or DEFAULT_PUBLISHER).lower()
    complexity = (complexity or DEFAULT_COMPLEXITY).lower()
    if not STREAM_URL:
        yield {"type": "error", "message": "streaming endpoint not configured (APIGEE_STREAM_URL)"}
        return
    if not api_key:
        yield {"type": "error", "message": "no LLM API key — add the key issued for your "
                                           "account in the Direct view's key field"}
        return
    if publisher not in STREAM_PUBLISHERS:
        yield {"type": "error", "message": f"streaming supports {STREAM_PUBLISHERS} for now"}
        return
    if complexity not in COMPLEXITIES:
        yield {"type": "error", "message": f"complexity must be one of {COMPLEXITIES}"}
        return

    model = MODEL_MATRIX[(publisher, complexity)]
    prompt = _collapse_history(history)
    if publisher == "anthropic":
        body = {
            "anthropic_version": "vertex-2023-10-16",
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
        }
    else:
        body = {"contents": {"role": "user", "parts": [{"text": prompt}]}}
    headers = {
        "Content-Type": "application/json",
        # Plain SSE, not gzip — httpx can't incrementally gunzip a chunked
        # stream (decompress "incorrect header check"), and compressing a token
        # stream defeats the point.
        "Accept-Encoding": "identity",
        "x-api-key": api_key,
        "publisher": publisher,
        "complexity": complexity,
        "invalidate-llm-cache": "False" if use_cache else "True",
        **_user_header(user_email),
        **tracing.outbound_headers(),
    }
    usage = None
    state = {"in": None, "out": None}  # Anthropic splits usage across frames
    started = time.monotonic()
    timeout = httpx.Timeout(connect=15.0, read=None, write=30.0, pool=15.0)
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=not _INSECURE) as client:
            async with client.stream("POST", STREAM_URL, headers=headers, json=body) as resp:
                if resp.status_code >= 400:
                    detail = (await resp.aread()).decode("utf-8", "replace")
                    yield {"type": "error", "message": f"upstream {resp.status_code}: {detail[:600]}"}
                    return
                buf = ""
                async for chunk in resp.aiter_text():
                    buf = (buf + chunk).replace("\r\n", "\n")
                    while "\n\n" in buf:
                        frame, buf = buf.split("\n\n", 1)
                        obj = _sse_data_object(frame)
                        if obj is None:
                            continue
                        if debug:
                            yield {"type": "raw", "data": obj}
                        txt, u = _extract_stream_piece(publisher, obj, state)
                        if txt:
                            yield {"type": "text", "text": txt}
                        if u:
                            usage = u
                obj = _sse_data_object(buf)  # trailing frame without terminator
                if obj is not None:
                    if debug:
                        yield {"type": "raw", "data": obj}
                    txt, u = _extract_stream_piece(publisher, obj, state)
                    if txt:
                        yield {"type": "text", "text": txt}
                    if u:
                        usage = u
    except Exception as e:  # noqa: BLE001
        yield {"type": "error", "message": f"stream failed: {e}"}
        return

    if publisher == "anthropic" and (state["in"] is not None or state["out"] is not None):
        p, o = state["in"], state["out"]
        usage = {
            "prompt": p,
            "output": o,
            "total": (p + o) if isinstance(p, int) and isinstance(o, int) else None,
        }

    yield {
        "type": "done",
        "model": model,
        "publisher": publisher,
        "complexity": complexity,
        "usage": usage,
        "latency_ms": int((time.monotonic() - started) * 1000),
    }
