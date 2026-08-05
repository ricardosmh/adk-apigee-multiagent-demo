"""Trace Explorer — reconstruct one end-to-end transaction from the four logs.

The demo threads a single W3C traceparent through every hop (BFF → Apigee →
Agent Engines → models → MCP → ecommerce proxies → ILB → services → Cloud SQL)
and each component writes a structured record — with an epoch-ms ``timing``
group — into one of four NAMED logs in the AI project (front-ai-logs,
apigee-ai-logs, agent-ai-logs, services-ai-logs). This module reads those logs
back (Cloud Logging entries.list) and turns a trace id into a drawable flow:
the full reference topology with the touched components lit, per-hop latency,
a chronological span timeline, and the raw records.

Design (mirrors admin.py):
  * The RECONSTRUCTION is a pure function over already-fetched log entries —
    unit-tested with plain dicts, no GCP. Only ``list_recent_traces`` /
    ``get_trace`` touch the client.
  * The client is injected (``store``-style) so tests substitute a fake.
  * The full 3-project TOPOLOGY (every component, even ones a given trace
    didn't touch) is defined HERE, once — the frontend just draws what it's
    handed. Colors match docs/OBSERVABILITY.md (blue BFF, purple engines,
    amber Apigee, green services).

Sync Cloud Logging calls — callers run these via asyncio.to_thread.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
import re

logger = logging.getLogger(__name__)

LOG_NAMES = ["front-ai-logs", "apigee-ai-logs", "agent-ai-logs", "services-ai-logs"]

_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_TRACE_ATTR_RE = re.compile(r"traces/([0-9a-f]{32})", re.I)

# Component color = the log it writes to (docs/OBSERVABILITY.md legend).
C_CLIENT = "#64748b"   # browser / infra (slate)
C_BFF = "#2563eb"      # front-ai-logs (blue)
C_AGENT = "#7c3aed"    # agent-ai-logs (purple)
C_APIGEE = "#f59e0b"   # apigee-ai-logs (amber)
C_SVC = "#16a34a"      # services-ai-logs (green)
C_MODEL = "#db2777"    # Vertex models (pink — AI project, no own log)
C_DATA = "#0891b2"     # Cloud SQL (cyan — no own log)


# ── The full reference topology (one home for the graph) ─────────────────────
# Each node: id, label, project column group, color, (col,row) layout hint, and
# `derived` = lights from a neighbour rather than its own log record.
# Node ids are keyed to the discriminator each record actually carries:
#   apigee → `apigee:<jsonPayload.proxy>` · services → `svc:<jsonPayload.service>`
#   agents → `agent:<role>` (role scanned from engine/agent) · BFF → `bff`.
_NODES = [
    {"id": "browser", "label": "Browser", "group": "client", "color": C_CLIENT, "col": 0, "row": 2},
    {"id": "bff", "label": "BFF", "sub": "Cloud Run", "group": "ai", "color": C_BFF, "col": 1, "row": 2},

    {"id": "apigee:unified-llm-endpoint-sse-apiproxy", "label": "/llm-stream",
     "sub": "Apigee", "group": "apigee", "color": C_APIGEE, "col": 2, "row": 0},
    {"id": "apigee:supervisor-agent-endpoint", "label": "/ai-agents",
     "sub": "Apigee", "group": "apigee", "color": C_APIGEE, "col": 2, "row": 2},
    {"id": "apigee:gemini-llm-apiproxy", "label": "/aiplatform",
     "sub": "Apigee", "group": "apigee", "color": C_APIGEE, "col": 4, "row": 0},

    {"id": "agent:supervisor", "label": "supervisor", "sub": "engine",
     "group": "ai", "color": C_AGENT, "col": 3, "row": 2},
    {"id": "agent:order", "label": "order_agent", "sub": "engine",
     "group": "ai", "color": C_AGENT, "col": 4, "row": 1},
    {"id": "agent:product", "label": "product_agent", "sub": "engine",
     "group": "ai", "color": C_AGENT, "col": 4, "row": 2},
    {"id": "agent:customer", "label": "customer_agent", "sub": "engine",
     "group": "ai", "color": C_AGENT, "col": 4, "row": 3},

    {"id": "model-vertex", "label": "Vertex models", "sub": "Gemini · Claude",
     "group": "ai", "color": C_MODEL, "col": 5, "row": 0, "derived": True},

    {"id": "apigee:mcp-server", "label": "/mcp", "sub": "Apigee",
     "group": "apigee", "color": C_APIGEE, "col": 5, "row": 2},
    {"id": "apigee:ecommerce-order-management", "label": "/order-mgmt",
     "sub": "Apigee", "group": "apigee", "color": C_APIGEE, "col": 6, "row": 1},
    {"id": "apigee:ecommerce-product-management", "label": "/product-mgmt",
     "sub": "Apigee", "group": "apigee", "color": C_APIGEE, "col": 6, "row": 2},
    {"id": "apigee:ecommerce-customer-management", "label": "/customer-mgmt",
     "sub": "Apigee", "group": "apigee", "color": C_APIGEE, "col": 6, "row": 3},

    {"id": "svc:orders-service", "label": "orders-service", "sub": "Cloud Run",
     "group": "backend", "color": C_SVC, "col": 7, "row": 1},
    {"id": "svc:products-service", "label": "products-service", "sub": "Cloud Run",
     "group": "backend", "color": C_SVC, "col": 7, "row": 2},
    {"id": "svc:customers-service", "label": "customers-service", "sub": "Cloud Run",
     "group": "backend", "color": C_SVC, "col": 7, "row": 3},

    {"id": "cloudsql", "label": "Cloud SQL", "sub": "MySQL · PSC",
     "group": "backend", "color": C_DATA, "col": 8, "row": 2, "derived": True},
]

# Reference edges (greyed background; the active timeline draws its own path).
_EDGES = [
    ("browser", "bff"),
    ("bff", "apigee:unified-llm-endpoint-sse-apiproxy"),
    ("bff", "apigee:supervisor-agent-endpoint"),
    ("apigee:supervisor-agent-endpoint", "agent:supervisor"),
    ("agent:supervisor", "agent:order"),
    ("agent:supervisor", "agent:product"),
    ("agent:supervisor", "agent:customer"),
    ("agent:supervisor", "apigee:gemini-llm-apiproxy"),
    ("agent:order", "apigee:gemini-llm-apiproxy"),
    ("agent:product", "apigee:gemini-llm-apiproxy"),
    ("agent:customer", "apigee:gemini-llm-apiproxy"),
    ("apigee:unified-llm-endpoint-sse-apiproxy", "model-vertex"),
    ("apigee:gemini-llm-apiproxy", "model-vertex"),
    ("agent:order", "apigee:mcp-server"),
    ("agent:product", "apigee:mcp-server"),
    ("agent:customer", "apigee:mcp-server"),
    ("apigee:mcp-server", "apigee:ecommerce-order-management"),
    ("apigee:mcp-server", "apigee:ecommerce-product-management"),
    ("apigee:mcp-server", "apigee:ecommerce-customer-management"),
    ("apigee:ecommerce-order-management", "svc:orders-service"),
    ("apigee:ecommerce-product-management", "svc:products-service"),
    ("apigee:ecommerce-customer-management", "svc:customers-service"),
    ("svc:orders-service", "cloudsql"),
    ("svc:products-service", "cloudsql"),
    ("svc:customers-service", "cloudsql"),
]


def topology() -> dict:
    """The static reference graph (nodes + edges), colors and layout hints."""
    return {"nodes": [dict(n) for n in _NODES], "edges": [{"from": a, "to": b} for a, b in _EDGES]}


# ── Entry access (works for real google entries AND test dicts) ──────────────
def _get(entry, name, default=None):
    if isinstance(entry, dict):
        return entry.get(name, default)
    return getattr(entry, name, default)


def _payload(entry) -> dict:
    p = _get(entry, "payload")
    if p is None and isinstance(entry, dict):
        p = entry.get("jsonPayload") or entry.get("json_payload")
    return p if isinstance(p, dict) else {}


def _message(entry) -> str | None:
    """The text of a NARRATIVE line — the engines route their Python log lines
    (ADK/genai framework chatter) to agent-ai-logs as a textPayload string, not
    a structured payload. Return it so the records panel shows the message
    instead of an empty object."""
    raw = _get(entry, "payload")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    if isinstance(entry, dict):
        tp = entry.get("textPayload") or entry.get("text_payload")
        if isinstance(tp, str) and tp.strip():
            return tp.strip()
    return None


def _log_short(entry) -> str:
    name = _get(entry, "log_name") or _get(entry, "logName") or ""
    return name.rsplit("/", 1)[-1] if name else ""


def _trace_id(entry) -> str | None:
    tr = _get(entry, "trace") or ""
    m = _TRACE_ATTR_RE.search(str(tr))
    if m:
        return m.group(1).lower()
    # fall back to a raw traceparent field in the payload (00-<32hex>-<16hex>-01)
    tp = _payload(entry).get("traceparent") or ""
    parts = str(tp).split("-")
    return parts[1].lower() if len(parts) >= 2 and _TRACE_ID_RE.match(parts[1] or "") else None


def _ts_ms(entry) -> int | None:
    ts = _get(entry, "timestamp")
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return int(ts)
    if isinstance(ts, _dt.datetime):
        return int(ts.timestamp() * 1000)
    try:  # ISO string
        return int(_dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def _num(v):
    """Epoch-ms values arrive as ints (BFF/services/agents) OR quoted strings
    (Apigee templates emit ``"{client.received.start.timestamp}"``)."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        s = v.strip()
        if s and s.lstrip("-").isdigit():
            return int(s)
    return None


# ── Classification + timing (pure) ───────────────────────────────────────────
def _agent_role(p: dict) -> str:
    """Which topology agent a model_call belongs to. Specialists carry their own
    name (order/product/customer); the coordinator logs `agent='root_agent'`
    with an empty `engine`, so anything that ISN'T a named specialist is the
    supervisor."""
    text = f"{p.get('agent') or ''} {p.get('engine') or ''}".lower()
    for role in ("order", "product", "customer"):
        if role in text:
            return role
    return "supervisor"


def classify(entry) -> str | None:
    """Map one log record to a topology node id (or None to skip it)."""
    log = _log_short(entry)
    p = _payload(entry)
    if log == "front-ai-logs":
        # The BFF writes BOTH the per-turn record (event=turn — user/view/session/
        # timing) AND plain middleware request-lines ("GET /api/… -> 200", no
        # fields). Only the turn is the BFF hop; the request-lines would clobber
        # the summary with empty user/view.
        return "bff" if p.get("event") == "turn" else None
    if log == "apigee-ai-logs":
        proxy = p.get("proxy")
        return f"apigee:{proxy}" if proxy else None
    if log == "agent-ai-logs":
        if p.get("event") != "model_call":
            return None  # agent_turn/tool_call/a2a_call/log_setup handled below
        return f"agent:{_agent_role(p)}"
    if log == "services-ai-logs":
        svc = p.get("service")
        return f"svc:{svc}" if svc else None
    return None


def _norm_lm(p: dict) -> dict:
    """Normalize a record's latency block to the CANONICAL shape
    ``{receivedAt, respondedAt, latencyMs, backendSentAt, backendReceivedAt}``.
    Prefers the new ``latency_metrics``; falls back to the legacy ``timing`` +
    flat ``latency_ms``/``latencyMs`` (so traces logged before the rollout still
    reconstruct). Every component now speaks the same names — one home."""
    lm = p.get("latency_metrics")
    if isinstance(lm, dict):
        return lm
    t = p.get("timing") or {}
    return {
        "receivedAt": t.get("receivedAt") or t.get("requestReceivedAt") or t.get("requestSentAt"),
        "respondedAt": t.get("respondedAt") or t.get("loggedAt"),
        "latencyMs": t.get("latencyMs") or p.get("latencyMs") or p.get("latency_ms"),
        "backendSentAt": t.get("backendSentAt") or t.get("targetSentAt"),
        "backendReceivedAt": t.get("backendReceivedAt") or t.get("targetReceivedAt"),
    }


def _span_times(node_id: str, p: dict, ts_fallback):
    """(start_ms, end_ms, latency_ms) — now uniform across every component:
    receivedAt → respondedAt, latencyMs. Ordering by the request side is what
    puts an outer hop before the inner hops it caused (they log at completion)."""
    lm = _norm_lm(p)
    start, end = _num(lm.get("receivedAt")), _num(lm.get("respondedAt"))
    lat = _num(lm.get("latencyMs"))
    if lat is None and start is not None and end is not None:
        lat = end - start
    if start is None:
        start = ts_fallback
    if end is None and start is not None and lat is not None:
        end = start + lat
    return start, end, lat


def _apigee_decomp(p: dict) -> dict | None:
    """Gateway latency decomposition: gateway overhead / backend-wait / streaming
    drain, from the canonical receivedAt · backendSentAt · backendReceivedAt ·
    respondedAt."""
    lm = _norm_lm(p)
    rr, bs, br, lg = (_num(lm.get(k)) for k in
                      ("receivedAt", "backendSentAt", "backendReceivedAt", "respondedAt"))
    if None in (rr, lg):
        return None
    out = {}
    if bs is not None:
        out["gatewayMs"] = bs - rr
    if bs is not None and br is not None:
        out["backendMs"] = br - bs
    if br is not None:
        out["drainMs"] = lg - br
    return out or None


_LLM_NODES = {"apigee:gemini-llm-apiproxy", "apigee:unified-llm-endpoint-sse-apiproxy"}
_ECOM_SVC = {"apigee:ecommerce-order-management": "svc:orders-service",
             "apigee:ecommerce-product-management": "svc:products-service",
             "apigee:ecommerce-customer-management": "svc:customers-service"}


def _self_latency(spans) -> tuple[dict, float]:
    """SELF (exclusive) latency per node — what each component ACTUALLY added,
    not the cumulative nested time. Uses the gateway decomposition:
      * a proxy's self = gatewayMs + drainMs (its own routing/streaming work);
        its backendMs is time it WAITED on something else, attributed to that:
          - LLM proxies → the model (their backendMs = inference time);
          - ecommerce proxies → the backend service call;
          - /ai-agents, /mcp → nested inner work, NOT re-attributed here.
      * an agent's self = its model-call latency minus the model inference that
        happened inside that call (matched by timestamp window).
    Returns (self_ms per node, total model inference time)."""
    self_ms: dict[str, float] = {}
    model_total = 0.0
    llm_spans = []
    for s in spans:
        node = s["node"]
        d = s.get("decomp") or {}
        if node.startswith("apigee:"):
            self_ms[node] = self_ms.get(node, 0) + (d.get("gatewayMs") or 0) + (d.get("drainMs") or 0)
            be = d.get("backendMs") or 0
            if node in _LLM_NODES:
                model_total += be
                llm_spans.append(s)
            elif node in _ECOM_SVC:
                svc = _ECOM_SVC[node]
                self_ms[svc] = self_ms.get(svc, 0) + be
    for s in spans:
        node = s["node"]
        if not node.startswith("agent:") or s.get("latency_ms") is None:
            continue
        inner = 0
        a0, a1 = s.get("start"), s.get("end")
        if a0 is not None and a1 is not None:
            for ls in llm_spans:
                if ls.get("start") is not None and a0 <= ls["start"] <= a1:
                    inner += (ls.get("decomp") or {}).get("backendMs") or 0
        self_ms[node] = self_ms.get(node, 0) + max(s["latency_ms"] - inner, 0)
    return self_ms, model_total


_ATTR_COLORS = {
    "model": C_MODEL, "backend": C_SVC, "gateway": C_APIGEE, "agent": C_AGENT,
    "a2a transport": "#6366f1", "startup": "#64748b", "streaming": "#94a3b8",
}


def _attribution(spans, ops, t0, t1) -> list[dict]:
    """Exclusive per-LAYER latency across the whole turn — every millisecond
    assigned to the innermost thing actually happening, so it sums to the total
    (no residual guessing). Measured LEAF work wins (model inference, backend
    call, gateway self); uncovered time is subdivided by the orchestration
    containers (model_call/tool_call → agent/backend, agent_turn → agent,
    a2a_call → A2A transport), with the pre-first-op gap = startup and the tail
    = streaming."""
    if t0 is None or t1 is None or t1 <= t0:
        return []
    leaf = []       # (start, end, bucket) — measured work, highest priority
    for s in spans:
        if not s["node"].startswith("apigee:"):
            continue
        st, en = s.get("start"), s.get("end")
        bs, br = (s.get("backend") or [None, None])
        if st is None or en is None:
            continue
        if bs is not None and br is not None:
            if bs > st:
                leaf.append((st, bs, "gateway"))
            if en > br:
                leaf.append((br, en, "gateway"))
            if s["node"] in _LLM_NODES:
                leaf.append((bs, br, "model"))
            elif s["node"] in _ECOM_SVC:
                leaf.append((bs, br, "backend"))
            # /ai-agents, /mcp backend = a container (nested work), not leaf
        else:
            leaf.append((st, en, "gateway"))
    containers = []  # (start, end, bucket) — subdivide the non-leaf time
    for o in ops:
        if o.get("start") is not None and o.get("end") is not None:
            cat = {"tool_call": "backend", "agent_turn": "agent",
                   "a2a_call": "a2a transport"}.get(o["event"], "agent")
            containers.append((o["start"], o["end"], cat))
    for s in spans:      # agent model_call windows → agent orchestration
        if s["node"].startswith("agent:") and s.get("start") is not None and s.get("end") is not None:
            containers.append((s["start"], s["end"], "agent"))
    acts = leaf + containers
    first = min((a for a, _, _ in acts), default=t0)
    last = max((b for _, b, _ in acts), default=t1)
    bounds = sorted({t0, t1} | {a for a, _, _ in acts} | {b for _, b, _ in acts})
    buckets: dict[str, float] = {}
    for a, b in zip(bounds, bounds[1:]):
        if b <= a:
            continue
        m = (a + b) / 2
        cat = next((lc for (ls, le, lc) in leaf if ls <= m < le), None)
        if cat is None:
            cov = sorted((ce - cs, cc) for (cs, ce, cc) in containers if cs <= m < ce)
            cat = cov[0][1] if cov else ("startup" if m < first else "streaming")
        buckets[cat] = buckets.get(cat, 0) + (b - a)
    out = [{"label": k, "ms": round(v), "color": _ATTR_COLORS.get(k, "#64748b")}
           for k, v in buckets.items() if v > 0]
    out.sort(key=lambda x: -x["ms"])
    return out


def reconstruct_flow(entries) -> dict:
    """Turn a trace's log entries into a drawable flow: the full topology with
    hit nodes lit + per-node latency, a chronological span timeline, and the
    raw records. PURE — no I/O; unit-tested with plain dicts."""
    spans, records, ops = [], [], []
    bff_payload = None
    seen_model_calls = set()   # (agent-node, receivedAt)
    for e in entries:
        node = classify(e)
        p = _payload(e)
        ts = _ts_ms(e)
        if node is None:
            ev = p.get("event") if isinstance(p, dict) else None
            if ev in ("agent_turn", "tool_call", "a2a_call"):
                # structured orchestration record — shown in the panel, and fed
                # to the self-latency attribution (not a topology hop, so it
                # doesn't double-count the model/tool spans nested inside it)
                st, en, lt = _span_times("op", p, ts)
                op = {"event": ev, "agent": _agent_role(p), "start": st, "end": en,
                      "latency_ms": lt, "payload": p, "ts": ts}
                ops.append(op)
                records.append({"node": None, "op": ev, "log": _log_short(e), "ts": ts,
                                "payload": p, **({"latency_ms": lt} if lt is not None else {})})
                continue
            # narrative line (framework log text) — keep as a record, not a hop
            records.append({"node": None, "log": _log_short(e), "ts": ts,
                            "payload": p, "message": _message(e)})
            continue
        if node.startswith("agent:"):
            # ADK logs each model call TWICE (same requestSentAt + tokens, a few
            # ms apart) — count it once, or per-agent latency doubles.
            sent = _num((p.get("timing") or {}).get("requestSentAt"))
            if sent is not None:
                key = (node, sent)
                if key in seen_model_calls:
                    continue
                seen_model_calls.add(key)
        start, end, lat = _span_times(node, p, ts)
        if node == "bff":
            bff_payload = p
        span = {"node": node, "start": start, "end": end, "latency_ms": lat, "ts": ts}
        if node.startswith("apigee:"):
            decomp = _apigee_decomp(p)
            if decomp:
                span["decomp"] = decomp
            lmn = _norm_lm(p)
            span["backend"] = [_num(lmn.get("backendSentAt")), _num(lmn.get("backendReceivedAt"))]
        spans.append(span)
        records.append({"node": node, "log": _log_short(e), "ts": ts, "payload": p, **(
            {"latency_ms": lat} if lat is not None else {})})

    # order key: request-side start, falling back to entry timestamp, then 0
    def _key(s):
        return (s.get("start") if s.get("start") is not None else s.get("ts")) or 0
    spans.sort(key=_key)
    ops.sort(key=lambda o: (o.get("start") if o.get("start") is not None else o.get("ts")) or 0)
    records.sort(key=lambda r: (r.get("ts") if r.get("ts") is not None else 0))
    for i, s in enumerate(spans):
        s["order"] = i

    # aggregate per node (a node can be hit several times)
    hit: dict[str, dict] = {}
    for s in spans:
        agg = hit.setdefault(s["node"], {"count": 0, "latency_ms": 0, "has_lat": False})
        agg["count"] += 1
        if s["latency_ms"] is not None:
            agg["latency_ms"] += s["latency_ms"]
            agg["has_lat"] = True

    # derived nodes: models light from an LLM proxy hit, Cloud SQL from a service
    llm_hit = any(n in hit for n in
                  ("apigee:gemini-llm-apiproxy", "apigee:unified-llm-endpoint-sse-apiproxy"))
    svc_hit = any(n.startswith("svc:") for n in hit)

    self_ms, model_total = _self_latency(spans)

    nodes = []
    for base in _NODES:
        n = dict(base)
        nid = n["id"]
        n["self_ms"] = round(self_ms[nid]) if nid in self_ms else None
        if nid == "browser":
            n["hit"] = bool(spans)  # origin lights whenever anything logged
            n["latency_ms"] = None
        elif nid == "model-vertex":
            n["hit"] = llm_hit
            # the model has no record of its own — its latency IS the inference
            # time the LLM proxies waited on (their backendMs)
            n["latency_ms"] = round(model_total) if model_total else None
            n["self_ms"] = n["latency_ms"]
        elif nid == "cloudsql":
            n["hit"] = svc_hit
            n["latency_ms"] = None
        elif nid in hit:
            agg = hit[nid]
            n["hit"] = True
            n["latency_ms"] = agg["latency_ms"] if agg["has_lat"] else None
            n["count"] = agg["count"]
        else:
            n["hit"] = False
            n["latency_ms"] = None
        nodes.append(n)

    total = _num((bff_payload or {}).get("latency_ms"))
    if total is None and spans:
        starts = [s["start"] for s in spans if s.get("start") is not None]
        ends = [s["end"] for s in spans if s.get("end") is not None]
        if starts and ends:
            total = max(ends) - min(starts)

    started = None
    starts = [s["start"] for s in spans if s.get("start") is not None]
    if starts:
        started = min(starts)

    # exclusive per-layer attribution (sums to the turn window) — the pie basis
    _starts = [x for x in ([s.get("start") for s in spans] + [o.get("start") for o in ops]) if x is not None]
    _ends = [x for x in ([s.get("end") for s in spans] + [o.get("end") for o in ops]) if x is not None]
    attribution = _attribution(spans, ops, min(_starts) if _starts else None,
                               max(_ends) if _ends else None)

    # sub-sessions: each specialist's OWN A2A session that took part in this
    # trace (from agentSessionId — see the a2a session propagation). These hang
    # off the trace in the Sessions navigator, under the user session.
    sub_sessions, seen_sub = [], set()
    for r in records:
        node = r.get("node") or ""
        asid = (r.get("payload") or {}).get("agentSessionId")
        if node.startswith("agent:") and asid and (node, asid) not in seen_sub:
            seen_sub.add((node, asid))
            sub_sessions.append({"agent": node.split(":", 1)[1], "node": node, "session": asid})

    summary = {
        "trace_id": None,  # filled by get_trace (entries may be test dicts)
        "user": (bff_payload or {}).get("user"),
        "view": (bff_payload or {}).get("view"),
        "session_id": (bff_payload or {}).get("session_id"),
        "model": (bff_payload or {}).get("model"),
        "delegations": (bff_payload or {}).get("delegations"),
        "total_ms": total,
        "started_at": started,
        "hops": sum(1 for n in nodes if n["hit"]),
        "sub_sessions": sub_sessions,
        # exclusive per-layer breakdown (model / backend / gateway / agent / A2A
        # transport / startup / streaming) — sums to the turn, drives the pie
        "attribution": attribution,
        "residual_ms": (max(total - sum(n["self_ms"] for n in nodes if n.get("self_ms")), 0)
                        if total else None),
    }
    return {"summary": summary, "nodes": nodes,
            "edges": [{"from": a, "to": b} for a, b in _EDGES],
            "spans": spans, "records": records, "ops": ops}


# ── Cloud Logging reads (thin; injected client for tests) ────────────────────
def client():
    """Lazy Cloud Logging read client (project from ADC / GOOGLE_CLOUD_PROJECT).
    Lazily imported so the module loads without google-cloud-logging (tests)."""
    import google.cloud.logging as gcl

    project = os.environ.get("GOOGLE_CLOUD_PROJECT") or None
    return gcl.Client(project=project) if project else gcl.Client()


def _esc(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"')


def _project(cl) -> str:
    return _get(cl, "project") or os.environ.get("GOOGLE_CLOUD_PROJECT") or ""


def _tokens(usage) -> int | None:
    """Total tokens from the BFF turn's usage ({prompt,total,output})."""
    if isinstance(usage, dict):
        for k in ("total", "totalTokens", "total_tokens"):
            v = _num(usage.get(k))
            if v is not None:
                return v
    return None


def _preview(text, n: int = 90) -> str | None:
    if not text:
        return None
    s = str(text).strip().replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"


def list_recent_traces(cl, *, limit: int = 50, user: str | None = None,
                       view: str | None = None) -> list[dict]:
    """Recent transactions, newest first, from the per-turn BFF anchor
    (front-ai-logs). The BFF record is the only one WITHOUT a raw traceparent
    field, so the trace id comes from its `trace` attribute."""
    proj = _project(cl)
    parts = [f'logName="projects/{proj}/logs/front-ai-logs"', 'jsonPayload.event="turn"']
    if user:
        parts.append(f'jsonPayload.user="{_esc(user)}"')
    if view:
        parts.append(f'jsonPayload.view="{_esc(view)}"')
    filt = " AND ".join(parts)
    out = []
    for e in cl.list_entries(filter_=filt, order_by="timestamp desc", max_results=limit):
        p = _payload(e)
        # defend against the BFF's middleware request-lines (same log, no fields)
        # even if the query filter didn't exclude them
        if p.get("event") != "turn":
            continue
        tid = _trace_id(e)
        if not tid:
            continue
        out.append({
            "trace_id": tid,
            "user": p.get("user"),
            "view": p.get("view"),
            "model": p.get("model"),
            "delegations": p.get("delegations"),
            "session_id": p.get("session_id"),
            "total_ms": _num(p.get("latency_ms")),
            "tokens": _tokens(p.get("usage")),
            "preview": _preview(p.get("userMessage")),
            "started_at": _ts_ms(e),
        })
    return out


def _sub_sessions_by_trace(cl, limit: int = 1000) -> dict:
    """{trace_id: {agentSessionId: {agent, model_calls, tokens}}} — the specialist
    A2A sessions per trace, from agent-ai-logs model_calls that carry an
    agentSessionId (the supervisor has none, so it's excluded — it IS the user
    session). Double-logged calls (same requestSentAt) counted once."""
    proj = _project(cl)
    filt = (f'logName="projects/{proj}/logs/agent-ai-logs" AND '
            f'jsonPayload.event="model_call"')
    out: dict = {}
    seen = set()
    for e in cl.list_entries(filter_=filt, order_by="timestamp desc", max_results=limit):
        p = _payload(e)
        asid = p.get("agentSessionId")
        if not asid:
            continue
        parts = str(p.get("traceparent") or "").split("-")
        tid = (parts[1].lower() if len(parts) >= 2 and _TRACE_ID_RE.match(parts[1] or "")
               else None)
        if not tid:
            continue
        sent = _num((p.get("timing") or {}).get("requestSentAt"))
        key = (tid, asid, sent)
        if sent is not None and key in seen:
            continue
        seen.add(key)
        sub = out.setdefault(tid, {}).setdefault(
            asid, {"agent": _agent_role(p), "model_calls": 0, "tokens": 0})
        sub["model_calls"] += 1
        sub["tokens"] += _num(p.get("totalTokens")) or 0
    return out


def _models_by_trace(cl, limit: int = 1000) -> dict:
    """{trace_id: [models]} from the gateway's llm.model — agent model calls
    don't log which model they hit, but the LLM proxies do (Gemini / Claude)."""
    proj = _project(cl)
    filt = f'logName="projects/{proj}/logs/apigee-ai-logs"'
    out: dict = {}
    for e in cl.list_entries(filter_=filt, order_by="timestamp desc", max_results=limit):
        p = _payload(e)
        model = (p.get("llm") or {}).get("model")
        if not model:
            continue
        parts = str(p.get("traceparent") or "").split("-")
        tid = (parts[1].lower() if len(parts) >= 2 and _TRACE_ID_RE.match(parts[1] or "")
               else None)
        if tid:
            out.setdefault(tid, set()).add(model)
    return {k: sorted(v) for k, v in out.items()}


def session_tree(traces: list[dict], sub_by_trace: dict | None = None,
                 models_by_trace: dict | None = None) -> list[dict]:
    """Group a flat list_recent_traces list into the navigator hierarchy
    user → session → trace, enriched with EAGER counts/metrics at every level
    (interactions, sub-sessions, tokens, time) — the dashboard. Pure, unit-tested.
    `sub_by_trace` (from _sub_sessions_by_trace) attaches each trace's specialist
    sub-sessions and rolls their counts up per session and per user. Direct turns
    have no session → grouped under a '' key the UI labels 'no session'."""
    sub_by_trace = sub_by_trace or {}
    models_by_trace = models_by_trace or {}
    users: dict[str, dict] = {}
    for t in traces:
        u = t.get("user") or "unknown"
        urec = users.setdefault(u, {"user": u, "sessions": {}})
        sid = t.get("session_id") or ""
        srec = urec["sessions"].setdefault(sid, {"session_id": sid, "traces": []})
        subs = sub_by_trace.get(t.get("trace_id"), {})
        tr = dict(t)
        tr["sub_sessions"] = [{"agent": v["agent"], "session": k,
                               "model_calls": v["model_calls"], "tokens": v["tokens"]}
                              for k, v in subs.items()]
        tr["sub_session_count"] = len(subs)
        # models: the direct turn's own model + any the gateway saw for this trace
        tr["models"] = sorted(set(([t["model"]] if t.get("model") else [])
                                  + models_by_trace.get(t.get("trace_id"), [])))
        srec["traces"].append(tr)
    out = []
    for urec in users.values():
        sessions = []
        for srec in urec["sessions"].values():
            trs = srec["traces"]
            starts = [x["started_at"] for x in trs if x.get("started_at") is not None]
            sess_subs = {}          # agentSessionId → agent (dedupe across turns)
            for x in trs:
                for s in x["sub_sessions"]:
                    sess_subs.setdefault(s["session"], s["agent"])
            sessions.append({
                "session_id": srec["session_id"],
                "traces": trs,
                "count": len(trs),
                "sub_session_count": len(sess_subs),
                "total_ms": sum(x.get("total_ms") or 0 for x in trs),
                "tokens": sum(x.get("tokens") or 0 for x in trs),
                "started_at": min(starts) if starts else None,
                "span_ms": (max(starts) - min(starts)) if len(starts) > 1 else 0,
                "agents": sorted(set(sess_subs.values())),
                "models": sorted({m for x in trs for m in x.get("models", [])}),
            })
        sessions.sort(key=lambda s: s["started_at"] or 0, reverse=True)
        u_starts = [s["started_at"] for s in sessions if s["started_at"] is not None]
        u_subs = {s["session"] for sess in sessions for x in sess["traces"]
                  for s in x["sub_sessions"]}
        out.append({
            "user": urec["user"],
            "sessions": sessions,
            "session_count": len(sessions),
            "trace_count": sum(s["count"] for s in sessions),
            "sub_session_count": len(u_subs),
            "total_ms": sum(s["total_ms"] for s in sessions),
            "tokens": sum(s["tokens"] for s in sessions),
            "started_at": max(u_starts) if u_starts else None,
        })
    out.sort(key=lambda u: u["started_at"] or 0, reverse=True)
    return out


def get_session_tree(cl, *, limit: int = 200, user: str | None = None) -> dict:
    """The user → session → trace dashboard + a top-level summary strip, built
    from recent BFF turns plus one sub-session query over agent-ai-logs."""
    traces = list_recent_traces(cl, limit=limit, user=user)
    subs = _sub_sessions_by_trace(cl, limit=max(limit * 8, 800))
    models = _models_by_trace(cl, limit=max(limit * 8, 800))
    users = session_tree(traces, subs, models)
    summary = {
        "users": len(users),
        "sessions": sum(u["session_count"] for u in users),
        "interactions": sum(u["trace_count"] for u in users),
        "sub_sessions": sum(u["sub_session_count"] for u in users),
        "total_ms": sum(u["total_ms"] for u in users),
    }
    return {"summary": summary, "users": users}


def get_trace(cl, trace_id: str) -> dict:
    """Every record on one trace across the four logs → reconstructed flow.
    The filter matches BOTH the `trace` attribute (all four logs set it) and a
    free-text trace id (the raw-traceparent field) so nothing is missed."""
    trace_id = (trace_id or "").strip().lower()
    if not _TRACE_ID_RE.match(trace_id):
        raise ValueError("trace_id must be a 32-hex W3C trace id")
    proj = _project(cl)
    logs = " OR ".join(f'logName="projects/{proj}/logs/{n}"' for n in LOG_NAMES)
    filt = (f'({logs}) AND '
            f'(trace="projects/{proj}/traces/{trace_id}" OR "{trace_id}")')
    entries = list(cl.list_entries(filter_=filt, order_by="timestamp asc", max_results=1000))
    flow = reconstruct_flow(entries)
    flow["summary"]["trace_id"] = trace_id
    return flow
