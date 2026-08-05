"""Unit tests for the Trace Explorer reconstruction (pure, no GCP).

Feeds fake log entries — plain dicts shaped like google Cloud Logging entries
(log_name / payload / trace / timestamp) — through reconstruct_flow and the
list/get helpers (with a fake client), asserting classification, request-side
ordering, latency math, and the hit/topology contract the frontend relies on.
"""
import datetime as dt

import trace_explorer as tx

TRACE = "d99a4896db171a8d4330ef08aa2f65fd"
PROJ = "demo-ai"


def _entry(log, payload, *, trace=TRACE, ts_ms=0):
    return {
        "log_name": f"projects/{PROJ}/logs/{log}",
        "payload": payload,
        "trace": f"projects/{PROJ}/traces/{trace}" if trace else "",
        "timestamp": dt.datetime.fromtimestamp(ts_ms / 1000, dt.timezone.utc),
    }


# ── Fixtures: a Direct trace and an agent fan-out trace ──────────────────────
def direct_entries():
    """Browser → BFF → /llm-stream → Vertex model. Logs arrive completion-order
    (inner proxy logs before the outer BFF turn) on purpose."""
    return [
        _entry("apigee-ai-logs", {
            "proxy": "unified-llm-endpoint-sse-apiproxy", "traceparent": f"00-{TRACE}-a-01",
            "llm": {"model": "gemini-2.5-flash"},
            "timing": {"requestReceivedAt": "1000", "targetSentAt": "1050",
                       "targetReceivedAt": "1700", "loggedAt": "1750"}}, ts_ms=1750),
        _entry("front-ai-logs", {
            "event": "turn", "view": "direct", "user": "alex@x.com",
            "model": "gemini-2.5-flash", "latency_ms": 820,
            "timing": {"respondedAt": 1820, "requestReceivedAt": 1000}}, ts_ms=1820),
    ]


def agent_entries():
    """Browser → BFF → /ai-agents → supervisor → (/aiplatform model) → order_agent
    → /mcp → /order-mgmt → orders-service → Cloud SQL."""
    return [
        _entry("services-ai-logs", {
            "service": "orders-service", "verb": "GET", "path": "/orders",
            "status": 200, "latencyMs": 40, "traceparent": f"00-{TRACE}-b-01",
            "timing": {"requestReceivedAt": 1500, "respondedAt": 1540}}, ts_ms=1540),
        _entry("apigee-ai-logs", {
            "proxy": "ecommerce-order-management", "traceparent": f"00-{TRACE}-c-01",
            "timing": {"requestReceivedAt": "1470", "targetSentAt": "1480",
                       "targetReceivedAt": "1560", "loggedAt": "1570"}}, ts_ms=1570),
        _entry("apigee-ai-logs", {
            "proxy": "mcp-server", "traceparent": f"00-{TRACE}-d-01",
            "timing": {"requestReceivedAt": "1450", "targetSentAt": "1455",
                       "targetReceivedAt": "1590", "loggedAt": "1600"}}, ts_ms=1600),
        _entry("agent-ai-logs", {
            "event": "model_call", "engine": "a2a-order-agent", "agent": "order_agent",
            "totalTokens": 120, "traceparent": f"00-{TRACE}-e-01",
            "timing": {"requestSentAt": 1300, "respondedAt": 1420, "latencyMs": 120}}, ts_ms=1420),
        _entry("agent-ai-logs", {
            "event": "log_setup", "engine": "a2a-order-agent"}, ts_ms=1200),
        _entry("apigee-ai-logs", {
            "proxy": "gemini-llm-apiproxy", "traceparent": f"00-{TRACE}-f-01",
            "llm": {"model": "gemini-2.5-pro"},
            "timing": {"requestReceivedAt": "1150", "targetSentAt": "1160",
                       "targetReceivedAt": "1250", "loggedAt": "1260"}}, ts_ms=1260),
        _entry("agent-ai-logs", {
            "event": "model_call", "engine": "a2a-supervisor-agent", "agent": "supervisor",
            "totalTokens": 90, "traceparent": f"00-{TRACE}-g-01",
            "timing": {"requestSentAt": 1100, "respondedAt": 1290, "latencyMs": 190}}, ts_ms=1290),
        _entry("apigee-ai-logs", {
            "proxy": "supervisor-agent-endpoint", "traceparent": f"00-{TRACE}-h-01",
            "timing": {"requestReceivedAt": "1050", "targetSentAt": "1060",
                       "targetReceivedAt": "1650", "loggedAt": "1660"}}, ts_ms=1660),
        _entry("front-ai-logs", {
            "event": "turn", "view": "agent", "user": "alex@x.com",
            "session_id": "s-1", "delegations": ["order_agent"], "latency_ms": 700,
            "timing": {"respondedAt": 1750, "requestReceivedAt": 1050}}, ts_ms=1750),
    ]


# ── Classification ───────────────────────────────────────────────────────────
def test_classify_each_log():
    assert tx.classify(_entry("front-ai-logs", {"event": "turn"})) == "bff"
    assert tx.classify(_entry("apigee-ai-logs", {"proxy": "mcp-server"})) == "apigee:mcp-server"
    assert tx.classify(_entry("services-ai-logs", {"service": "orders-service"})) == "svc:orders-service"
    assert tx.classify(_entry("agent-ai-logs", {"event": "model_call", "agent": "order_agent"})) == "agent:order"
    # non-model_call agent lines are not hops
    assert tx.classify(_entry("agent-ai-logs", {"event": "log_setup", "engine": "a2a-order-agent"})) is None
    # unknown proxy / missing discriminator → skip
    assert tx.classify(_entry("apigee-ai-logs", {})) is None
    # front-ai-logs middleware request-lines (no event) are NOT the BFF turn hop
    assert tx.classify(_entry("front-ai-logs", {})) is None
    assert tx.classify(_entry("front-ai-logs", {"message": "GET /api/agent/stream -> 200"})) is None


def test_agent_role_supervisor_is_the_non_specialist():
    # the coordinator logs agent='root_agent' with an empty engine — anything
    # that isn't a named specialist is the supervisor
    assert tx.classify(_entry("agent-ai-logs", {"event": "model_call", "agent": "root_agent"})) == "agent:supervisor"
    assert tx.classify(_entry("agent-ai-logs", {"event": "model_call", "engine": "a2a-supervisor-agent"})) == "agent:supervisor"
    assert tx.classify(_entry("agent-ai-logs", {"event": "model_call", "agent": "customer_agent"})) == "agent:customer"


def test_bff_middleware_line_does_not_clobber_summary():
    """A fieldless front-ai-logs request-line must not overwrite the turn's
    user/view/session in the summary (the live 'view=?, user=UNKNOWN' bug)."""
    entries = [
        _entry("front-ai-logs", {"event": "turn", "view": "agent", "user": "a@b.com",
            "session_id": "s-9", "latency_ms": 500,
            "timing": {"respondedAt": 1500, "requestReceivedAt": 1000}}, ts_ms=1500),
        _entry("front-ai-logs", {"message": "GET /api/agent/stream -> 200"}, ts_ms=1600),
    ]
    s = tx.reconstruct_flow(entries)["summary"]
    assert s["user"] == "a@b.com" and s["view"] == "agent" and s["session_id"] == "s-9"
    assert s["total_ms"] == 500


def test_double_logged_model_calls_counted_once():
    """ADK logs each model call twice (same requestSentAt) — the node latency
    must not double."""
    dup = {"event": "model_call", "agent": "product_agent",
           "timing": {"requestSentAt": 1000, "respondedAt": 1200, "latencyMs": 200}}
    dup2 = {**dup, "timing": {"requestSentAt": 1000, "respondedAt": 1240, "latencyMs": 240}}
    flow = tx.reconstruct_flow([_entry("agent-ai-logs", dup, ts_ms=1200),
                                _entry("agent-ai-logs", dup2, ts_ms=1240)])
    node = next(n for n in flow["nodes"] if n["id"] == "agent:product")
    assert node["count"] == 1 and node["latency_ms"] == 200   # first kept, not 440


# ── Direct trace ─────────────────────────────────────────────────────────────
def test_direct_flow_hits_and_totals():
    flow = tx.reconstruct_flow(direct_entries())
    hit = {n["id"]: n for n in flow["nodes"] if n["hit"]}
    assert set(hit) >= {"browser", "bff", "apigee:unified-llm-endpoint-sse-apiproxy", "model-vertex"}
    # the agent side stayed dark
    assert "apigee:supervisor-agent-endpoint" not in hit
    assert "svc:orders-service" not in hit
    # total = the BFF turn latency
    assert flow["summary"]["total_ms"] == 820
    assert flow["summary"]["view"] == "direct"
    assert flow["summary"]["user"] == "alex@x.com"
    # every topology node is present (lit or dark) so the frontend can grey them
    assert len(flow["nodes"]) == len(tx._NODES)


def test_direct_llm_proxy_latency_and_decomp():
    flow = tx.reconstruct_flow(direct_entries())
    llm = next(n for n in flow["nodes"] if n["id"] == "apigee:unified-llm-endpoint-sse-apiproxy")
    assert llm["latency_ms"] == 750           # loggedAt 1750 − requestReceivedAt 1000
    span = next(s for s in flow["spans"] if s["node"] == llm["id"])
    assert span["decomp"] == {"gatewayMs": 50, "backendMs": 650, "drainMs": 50}


# ── Agent fan-out trace ──────────────────────────────────────────────────────
def test_agent_flow_orders_by_request_side_not_emission():
    flow = tx.reconstruct_flow(agent_entries())
    order = [s["node"] for s in flow["spans"]]
    # request-side ordering: the outer supervisor proxy precedes the model call
    # it fronts, which precedes the inner ecommerce proxy — even though the
    # inner records were EMITTED (logged) first.
    assert order.index("apigee:supervisor-agent-endpoint") < order.index("agent:supervisor")
    assert order.index("agent:supervisor") < order.index("apigee:gemini-llm-apiproxy")
    assert order.index("agent:order") < order.index("apigee:mcp-server")
    assert order.index("apigee:mcp-server") < order.index("apigee:ecommerce-order-management")
    assert order.index("apigee:ecommerce-order-management") < order.index("svc:orders-service")


def test_agent_flow_lights_full_path():
    flow = tx.reconstruct_flow(agent_entries())
    hit = {n["id"] for n in flow["nodes"] if n["hit"]}
    assert {"browser", "bff", "apigee:supervisor-agent-endpoint", "agent:supervisor",
            "apigee:gemini-llm-apiproxy", "model-vertex", "agent:order",
            "apigee:mcp-server", "apigee:ecommerce-order-management",
            "svc:orders-service", "cloudsql"} <= hit
    # untouched specialists stay dark
    assert "agent:product" not in hit
    assert "svc:customers-service" not in hit


def test_agent_flow_latencies_and_summary():
    flow = tx.reconstruct_flow(agent_entries())
    lat = {n["id"]: n["latency_ms"] for n in flow["nodes"]}
    assert lat["svc:orders-service"] == 40
    assert lat["agent:supervisor"] == 190
    assert lat["agent:order"] == 120
    assert lat["apigee:mcp-server"] == 150      # 1600 − 1450
    assert flow["summary"]["total_ms"] == 700
    assert flow["summary"]["delegations"] == ["order_agent"]
    assert flow["summary"]["hops"] >= 10
    # the log_setup line is retained as an unclassified record, not a hop
    assert any(r["node"] is None for r in flow["records"])


def test_repeated_node_aggregates_latency():
    """A node hit twice (e.g. the LLM proxy for two model calls) sums latency."""
    entries = [
        _entry("apigee-ai-logs", {"proxy": "gemini-llm-apiproxy",
            "timing": {"requestReceivedAt": "100", "loggedAt": "200"}}, ts_ms=200),
        _entry("apigee-ai-logs", {"proxy": "gemini-llm-apiproxy",
            "timing": {"requestReceivedAt": "300", "loggedAt": "450"}}, ts_ms=450),
    ]
    flow = tx.reconstruct_flow(entries)
    node = next(n for n in flow["nodes"] if n["id"] == "apigee:gemini-llm-apiproxy")
    assert node["latency_ms"] == 250            # 100 + 150
    assert node["count"] == 2


# ── Client-backed helpers (fake client) ──────────────────────────────────────
class FakeClient:
    def __init__(self, entries, project=PROJ):
        self.project = project
        self._entries = entries
        self.last_filter = None
        self.last_order = None

    def list_entries(self, filter_=None, order_by=None, max_results=None):
        self.last_filter = filter_
        self.last_order = order_by
        return list(self._entries)[: max_results or None]


def test_list_recent_traces_shapes_and_filters():
    turns = [
        _entry("front-ai-logs", {"event": "turn", "view": "agent", "user": "alex@x.com",
            "delegations": ["order_agent"], "latency_ms": 700}, ts_ms=1750),
        _entry("front-ai-logs", {"event": "turn", "view": "direct", "user": "admin@x.com",
            "model": "gemini", "latency_ms": 300}, trace="a" * 32, ts_ms=900),
    ]
    cl = FakeClient(turns)
    rows = tx.list_recent_traces(cl, limit=10, user="alex@x.com", view="agent")
    assert 'jsonPayload.user="alex@x.com"' in cl.last_filter
    assert 'jsonPayload.view="agent"' in cl.last_filter
    assert 'logName="projects/demo-ai/logs/front-ai-logs"' in cl.last_filter
    assert cl.last_order == "timestamp desc"
    assert rows[0]["trace_id"] == TRACE
    assert rows[0]["total_ms"] == 700
    assert rows[1]["trace_id"] == "a" * 32


def test_list_recent_traces_skips_untraced():
    cl = FakeClient([_entry("front-ai-logs", {"event": "turn"}, trace="")])
    assert tx.list_recent_traces(cl) == []


def test_get_trace_builds_filter_and_fills_id():
    cl = FakeClient(agent_entries())
    flow = tx.get_trace(cl, TRACE.upper())          # normalized to lowercase
    assert flow["summary"]["trace_id"] == TRACE
    assert f'trace="projects/{PROJ}/traces/{TRACE}"' in cl.last_filter
    assert f'"{TRACE}"' in cl.last_filter            # free-text arm too
    for name in tx.LOG_NAMES:
        assert f'logName="projects/{PROJ}/logs/{name}"' in cl.last_filter


def test_get_trace_rejects_bad_id():
    cl = FakeClient([])
    try:
        tx.get_trace(cl, "not-a-trace")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_sub_sessions_from_agent_session_ids():
    entries = agent_entries() + [
        _entry("agent-ai-logs", {"event": "model_call", "agent": "product_agent",
            "agentSessionId": "SUB-1",
            "timing": {"requestSentAt": 1300, "respondedAt": 1420, "latencyMs": 120}}, ts_ms=1420),
    ]
    flow = tx.reconstruct_flow(entries)
    subs = flow["summary"]["sub_sessions"]
    assert {"agent": "product", "node": "agent:product", "session": "SUB-1"} in subs
    # deduped: the same (node, session) appears once even if logged twice
    assert sum(1 for s in subs if s["session"] == "SUB-1") == 1


def test_session_tree_groups_user_session_trace():
    traces = [
        {"trace_id": "a", "user": "alex@x.com", "session_id": "S1", "total_ms": 700, "started_at": 300, "view": "agent"},
        {"trace_id": "b", "user": "alex@x.com", "session_id": "S1", "total_ms": 500, "started_at": 400, "view": "agent"},
        {"trace_id": "c", "user": "alex@x.com", "session_id": "S2", "total_ms": 200, "started_at": 100, "view": "agent"},
        {"trace_id": "d", "user": "admin@x.com", "session_id": "", "total_ms": 90, "started_at": 500, "view": "direct"},
    ]
    tree = tx.session_tree(traces)
    # newest-user first (admin's trace d started_at 500 is newest)
    assert tree[0]["user"] == "admin@x.com"
    alex = next(u for u in tree if u["user"] == "alex@x.com")
    assert alex["session_count"] == 2 and alex["trace_count"] == 3
    s1 = next(s for s in alex["sessions"] if s["session_id"] == "S1")
    assert s1["count"] == 2 and s1["total_ms"] == 1200
    assert [t["trace_id"] for t in s1["traces"]] == ["a", "b"]
    # the direct turn groups under the empty-session key
    assert tree[0]["sessions"][0]["session_id"] == ""


def test_session_tree_rolls_up_sub_sessions_and_tokens():
    traces = [
        {"trace_id": "a", "user": "alex@x.com", "session_id": "S1", "total_ms": 700,
         "tokens": 1000, "started_at": 300, "view": "agent"},
        {"trace_id": "b", "user": "alex@x.com", "session_id": "S1", "total_ms": 500,
         "tokens": 400, "started_at": 400, "view": "agent"},
    ]
    sub_by_trace = {
        "a": {"sub-1": {"agent": "product", "model_calls": 2, "tokens": 300}},
        "b": {"sub-2": {"agent": "order", "model_calls": 1, "tokens": 150},
              "sub-3": {"agent": "customer", "model_calls": 3, "tokens": 90}},
    }
    tree = tx.session_tree(traces, sub_by_trace)
    u = tree[0]
    assert u["trace_count"] == 2 and u["sub_session_count"] == 3 and u["tokens"] == 1400
    s1 = u["sessions"][0]
    assert s1["count"] == 2                         # interactions per session
    assert s1["sub_session_count"] == 3             # sub-sessions rolled up per session
    assert sorted(s1["agents"]) == ["customer", "order", "product"]
    ta = next(t for t in s1["traces"] if t["trace_id"] == "a")
    tb = next(t for t in s1["traces"] if t["trace_id"] == "b")
    assert ta["sub_session_count"] == 1 and tb["sub_session_count"] == 2   # per trace
    assert ta["sub_sessions"][0] == {"agent": "product", "session": "sub-1",
                                     "model_calls": 2, "tokens": 300}


def test_session_tree_rolls_up_models():
    traces = [
        {"trace_id": "a", "user": "u@x.com", "session_id": "S1", "view": "agent", "started_at": 1},
        {"trace_id": "b", "user": "u@x.com", "session_id": "S1", "view": "direct",
         "model": "gemini-3.1-flash-lite", "started_at": 2},
    ]
    models = {"a": ["claude-sonnet-4-6", "gemini-3.1-flash-lite"]}   # gateway-seen
    tree = tx.session_tree(traces, None, models)
    s1 = tree[0]["sessions"][0]
    assert s1["models"] == ["claude-sonnet-4-6", "gemini-3.1-flash-lite"]
    ta = next(t for t in s1["traces"] if t["trace_id"] == "a")
    tb = next(t for t in s1["traces"] if t["trace_id"] == "b")
    assert ta["models"] == ["claude-sonnet-4-6", "gemini-3.1-flash-lite"]   # from gateway
    assert tb["models"] == ["gemini-3.1-flash-lite"]                        # direct turn's own


def test_list_recent_traces_carries_preview_and_tokens():
    turns = [_entry("front-ai-logs", {"event": "turn", "view": "agent", "user": "u@x.com",
        "session_id": "S1", "latency_ms": 700, "userMessage": "where is my order 1234?",
        "usage": {"prompt": 20, "total": 55, "output": 35}}, ts_ms=1000)]
    row = tx.list_recent_traces(FakeClient(turns))[0]
    assert row["preview"] == "where is my order 1234?" and row["tokens"] == 55


def test_get_session_tree_shape_summary_and_users():
    turns = [_entry("front-ai-logs", {"event": "turn", "view": "agent",
        "user": "alex@x.com", "session_id": "S1", "latency_ms": 700}, ts_ms=1000)]
    out = tx.get_session_tree(FakeClient(turns), limit=50)
    assert set(out) == {"summary", "users"}
    assert out["summary"]["users"] == 1 and out["summary"]["interactions"] == 1
    assert out["users"][0]["user"] == "alex@x.com"
    assert out["users"][0]["sessions"][0]["traces"][0]["trace_id"] == TRACE


def test_narrative_text_lines_keep_their_message():
    """The engines route framework log lines to agent-ai-logs as a textPayload
    STRING (not a structured payload) — the record must carry that message, not
    render empty."""
    txt = {"log_name": f"projects/{PROJ}/logs/agent-ai-logs",
           "payload": "Successfully resolved remote A2A agent: order_agent",
           "trace": f"projects/{PROJ}/traces/{TRACE}", "timestamp": None}
    export = {"logName": f"projects/{PROJ}/logs/agent-ai-logs",
              "textPayload": "Sending out request, model: gemini",
              "trace": f"projects/{PROJ}/traces/{TRACE}"}
    flow = tx.reconstruct_flow([txt, export])
    narr = [r for r in flow["records"] if r["node"] is None]
    msgs = {r["message"] for r in narr}
    assert "Successfully resolved remote A2A agent: order_agent" in msgs
    assert "Sending out request, model: gemini" in msgs
    # they're records, not hops (no classified spans at all here)
    assert flow["summary"]["hops"] == 0


def test_self_latency_attributes_backend_to_model_not_gateway():
    """The proxy's cumulative time is mostly backend WAIT — self latency strips
    that out: gateway+drain stays on the proxy, the model inference goes to the
    Vertex node, and the agent keeps only its own overhead."""
    entries = [
        _entry("apigee-ai-logs", {"proxy": "gemini-llm-apiproxy",
            "timing": {"requestReceivedAt": 1000, "targetSentAt": 1010,
                       "targetReceivedAt": 1210, "loggedAt": 1215}}, ts_ms=1215),
        _entry("agent-ai-logs", {"event": "model_call", "agent": "product_agent",
            "timing": {"requestSentAt": 990, "respondedAt": 1230, "latencyMs": 240}}, ts_ms=1230),
        _entry("front-ai-logs", {"event": "turn", "view": "agent", "user": "u@x.com",
            "session_id": "S", "latency_ms": 300,
            "timing": {"respondedAt": 1300, "requestReceivedAt": 1000}}, ts_ms=1300),
    ]
    flow = tx.reconstruct_flow(entries)
    byid = {n["id"]: n for n in flow["nodes"]}
    assert byid["apigee:gemini-llm-apiproxy"]["latency_ms"] == 215   # cumulative
    assert byid["apigee:gemini-llm-apiproxy"]["self_ms"] == 15       # gateway 10 + drain 5
    assert byid["model-vertex"]["self_ms"] == 200                    # backend = inference
    assert byid["agent:product"]["self_ms"] == 40                    # 240 − 200 model inside
    assert flow["summary"]["residual_ms"] == 45                      # 300 − (15+200+40)


def test_canonical_latency_metrics_schema():
    """Every component now speaks latency_metrics {receivedAt, respondedAt,
    latencyMs, backendSentAt, backendReceivedAt}. The gateway omits latencyMs
    (reader derives it)."""
    entries = [
        _entry("apigee-ai-logs", {"event": "gateway", "component": "gateway",
            "proxy": "gemini-llm-apiproxy",
            "latency_metrics": {"receivedAt": 1000, "backendSentAt": 1010,
                                "backendReceivedAt": 1200, "respondedAt": 1210}}, ts_ms=1210),
        _entry("front-ai-logs", {"event": "turn", "component": "bff", "view": "agent",
            "user": "u@x.com", "session_id": "S",
            "latency_metrics": {"receivedAt": 900, "respondedAt": 1300, "latencyMs": 400}}, ts_ms=1300),
    ]
    flow = tx.reconstruct_flow(entries)
    llm = next(n for n in flow["nodes"] if n["id"] == "apigee:gemini-llm-apiproxy")
    assert llm["latency_ms"] == 210     # derived respondedAt − receivedAt
    span = next(s for s in flow["spans"] if s["node"] == llm["id"])
    assert span["decomp"] == {"gatewayMs": 10, "backendMs": 190, "drainMs": 10}
    assert flow["summary"]["total_ms"] == 400 and flow["summary"]["user"] == "u@x.com"


def test_new_orchestration_records_captured_as_ops():
    entries = [
        _entry("agent-ai-logs", {"event": "agent_turn", "component": "engine", "agent": "product_agent",
            "latency_metrics": {"receivedAt": 100, "respondedAt": 900, "latencyMs": 800}}, ts_ms=900),
        _entry("agent-ai-logs", {"event": "tool_call", "component": "engine", "agent": "product_agent",
            "tool": "get_products",
            "latency_metrics": {"receivedAt": 300, "respondedAt": 600, "latencyMs": 300}}, ts_ms=600),
        _entry("agent-ai-logs", {"event": "a2a_call", "component": "engine", "agent": "order_agent",
            "targetAgent": "order_agent",
            "latency_metrics": {"receivedAt": 50, "respondedAt": 950, "latencyMs": 900}}, ts_ms=950),
    ]
    flow = tx.reconstruct_flow(entries)
    assert {o["event"] for o in flow["ops"]} == {"agent_turn", "tool_call", "a2a_call"}
    tc = next(o for o in flow["ops"] if o["event"] == "tool_call")
    assert tc["latency_ms"] == 300 and tc["agent"] == "product"
    assert not flow["spans"]   # ops are NOT topology hops (no double-count)


def test_attribution_uses_ops_and_sums_to_total():
    """Exclusive per-layer attribution: model inference (LLM backend), gateway
    self, the a2a_call transport (delegation − specialist work), and the
    uncovered tail as streaming — summing to the turn window."""
    entries = [
        # /aiplatform: gateway 10+5, model backend 200  → total window 1000..1215
        _entry("apigee-ai-logs", {"event": "gateway", "component": "gateway",
            "proxy": "gemini-llm-apiproxy",
            "latency_metrics": {"receivedAt": 1000, "backendSentAt": 1010,
                                "backendReceivedAt": 1210, "respondedAt": 1215}}, ts_ms=1215),
        # the agent model_call around it (1000..1230) → 15ms agent overhead after
        _entry("agent-ai-logs", {"event": "model_call", "agent": "order_agent",
            "latency_metrics": {"receivedAt": 1000, "respondedAt": 1230, "latencyMs": 230}}, ts_ms=1230),
        # an a2a_call wrapping it: 990..1300 → transport = 1300−1230(agent end)
        _entry("agent-ai-logs", {"event": "a2a_call", "agent": "order_agent", "targetAgent": "order_agent",
            "latency_metrics": {"receivedAt": 990, "respondedAt": 1300, "latencyMs": 310}}, ts_ms=1300),
        _entry("front-ai-logs", {"event": "turn", "component": "bff", "view": "agent",
            "user": "u@x.com", "session_id": "S",
            "latency_metrics": {"receivedAt": 990, "respondedAt": 1300, "latencyMs": 310}}, ts_ms=1300),
    ]
    flow = tx.reconstruct_flow(entries)
    attr = {a["label"]: a["ms"] for a in flow["summary"]["attribution"]}
    assert attr["model"] == 200                      # LLM backend
    assert attr["gateway"] == 15                      # 10 + 5
    assert attr["agent"] == 15                        # model_call tail 1215..1230
    # a2a transport = the a2a_call time outside the model_call: 990..1000 +
    # 1230..1300 = 10 + 70 = 80; everything sums to the 310ms window
    assert attr["a2a transport"] == 80
    assert sum(attr.values()) == 310


def test_topology_is_self_consistent():
    topo = tx.topology()
    ids = {n["id"] for n in topo["nodes"]}
    for e in topo["edges"]:
        assert e["from"] in ids and e["to"] in ids
