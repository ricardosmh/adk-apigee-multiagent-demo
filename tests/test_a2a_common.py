"""a2a_common tests — the scaffolding we hand-verified this cycle.

Skips if google-adk / a2a-sdk aren't importable (heavy agent deps).
"""
import pytest

pytest.importorskip("google.adk")
pytest.importorskip("a2a")


def test_build_apigee_model_string(monkeypatch):
    import google.auth

    monkeypatch.setattr(google.auth, "default", lambda *a, **k: (None, "proj"))
    monkeypatch.setenv("APIGEE_LLM_PROXY_URL", "https://gw.example/aiplatform/v1beta")
    monkeypatch.setenv("APIGEE_API_KEY", "k")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
    monkeypatch.delenv("APIGEE_LLM_INSECURE_TLS", raising=False)

    from a2a_common.model import build_apigee_model

    model = build_apigee_model()
    assert model.model == "apigee/gemini/gemini-3.1-flash-lite"


def test_build_apigee_model_secretref_deploy_host(monkeypatch):
    """No plaintext key + APIGEE_API_KEY_SECRET set = the deploy-host shape:
    import must succeed (placeholder header; the real key is injected at
    runtime by Agent Engine's SecretRef, where the module re-executes)."""
    import google.auth

    monkeypatch.setattr(google.auth, "default", lambda *a, **k: (None, "proj"))
    monkeypatch.setenv("APIGEE_LLM_PROXY_URL", "https://gw.example/aiplatform/v1beta")
    monkeypatch.delenv("APIGEE_API_KEY", raising=False)
    monkeypatch.delenv("APIGEE_LLM_API_KEY", raising=False)
    monkeypatch.setenv("APIGEE_API_KEY_SECRET", "apigee-key-order")
    monkeypatch.delenv("APIGEE_LLM_INSECURE_TLS", raising=False)

    from a2a_common.model import build_apigee_model

    model = build_apigee_model()  # must not raise
    assert model._custom_headers["x-api-key"].startswith("deploy-time-placeholder")


def test_model_pickle_rebuilds_from_runtime_env(monkeypatch):
    """The engine runs the UNPICKLED object — verified live that a snapshot
    pickle froze the deploy-host placeholder into x-api-key (Apigee 401).
    The recipe pickle must (a) carry no key material and (b) rebuild with the
    env present at UNPICKLE time (= the SecretRef-injected runtime key)."""
    import cloudpickle
    import google.auth

    monkeypatch.setattr(google.auth, "default", lambda *a, **k: (None, "proj"))
    monkeypatch.setenv("APIGEE_LLM_PROXY_URL", "https://gw.example/aiplatform/v1beta")
    monkeypatch.setenv("APIGEE_API_KEY", "deploy-host-key")
    monkeypatch.delenv("APIGEE_LLM_INSECURE_TLS", raising=False)

    from a2a_common.model import build_apigee_model

    blob = cloudpickle.dumps(build_apigee_model())
    assert b"deploy-host-key" not in blob          # no key material in the pickle

    monkeypatch.setenv("APIGEE_API_KEY", "runtime-injected-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.1-pro-preview")
    rebuilt = cloudpickle.loads(blob)
    assert rebuilt._custom_headers["x-api-key"] == "runtime-injected-key"
    assert rebuilt.model.endswith("gemini-3.1-pro-preview")   # model also from runtime env


def _find_headers(obj, depth=0):
    """Locate the connection headers dict inside a toolset instance."""
    if depth > 6:
        return None
    if isinstance(obj, dict) and "x-api-key" in obj:
        return obj
    for v in vars(obj).values() if hasattr(obj, "__dict__") else []:
        if isinstance(v, dict) and "x-api-key" in v:
            return v
        found = _find_headers(v, depth + 1) if hasattr(v, "__dict__") or isinstance(v, dict) else None
        if found:
            return found
    return None


def test_mcp_toolset_pickle_rebuilds_from_runtime_env(monkeypatch):
    import cloudpickle

    monkeypatch.setenv("APIGEE_MCP_URL", "https://gw.example/mcp")
    monkeypatch.setenv("APIGEE_API_KEY", "deploy-host-key")
    monkeypatch.delenv("APIGEE_MCP_TOKEN", raising=False)

    from a2a_common.specialist import build_mcp_toolset

    blob = cloudpickle.dumps(build_mcp_toolset(["get_order"]))
    assert b"deploy-host-key" not in blob

    monkeypatch.setenv("APIGEE_API_KEY", "runtime-injected-key")
    rebuilt = cloudpickle.loads(blob)
    headers = _find_headers(rebuilt)
    assert headers and headers["x-api-key"] == "runtime-injected-key"


def test_agent_card_url_shapes():
    """The URI-healing refresh compares proxies by card URL — cover the three
    places a RemoteA2aAgent can carry it (resolved card, URL source, card source)."""
    import types

    from a2a_common.client import agent_card_url

    card = types.SimpleNamespace(url="https://x/v1beta1/reasoningEngines/111/a2a/v1")
    assert agent_card_url(types.SimpleNamespace(_agent_card=card)) == card.url
    assert agent_card_url(types.SimpleNamespace(
        _agent_card=None, _agent_card_source="https://y/card")) == "https://y/card"
    assert agent_card_url(types.SimpleNamespace(
        _agent_card=None, _agent_card_source=card)) == card.url
    assert agent_card_url(types.SimpleNamespace(_agent_card=None, _agent_card_source=None)) is None


def test_build_apigee_model_requires_env(monkeypatch):
    import google.auth

    monkeypatch.setattr(google.auth, "default", lambda *a, **k: (None, "proj"))
    monkeypatch.delenv("APIGEE_LLM_PROXY_URL", raising=False)
    monkeypatch.delenv("APIGEE_API_KEY", raising=False)
    monkeypatch.delenv("APIGEE_LLM_API_KEY", raising=False)
    monkeypatch.delenv("APIGEE_API_KEY_SECRET", raising=False)

    from a2a_common.model import build_apigee_model

    with pytest.raises(RuntimeError):
        build_apigee_model()


def test_force_sse_request_converter(monkeypatch):
    from a2a_common import specialist
    from google.adk.a2a.converters.request_converter import AgentRunRequest
    from google.adk.agents.run_config import RunConfig, StreamingMode

    # Stub the inner default converter so we don't need a real RequestContext.
    monkeypatch.setattr(
        specialist,
        "convert_a2a_request_to_agent_run_request",
        lambda *a, **k: AgentRunRequest(run_config=RunConfig()),
    )
    out = specialist.force_sse_request_converter("ctx", "partconv")
    assert out.run_config.streaming_mode == StreamingMode.SSE


def _card(streaming: bool):
    from a2a.types import AgentCapabilities, AgentCard, TransportProtocol

    return AgentCard(
        name="order_agent",
        description="d",
        version="0.1.0",
        url="https://engine/a2a/v1/",
        preferredTransport=TransportProtocol.http_json,
        protocolVersion="0.3.0",
        skills=[],
        capabilities=AgentCapabilities(streaming=streaming),
        defaultInputModes=["text"],
        defaultOutputModes=["text"],
    )


def test_upgrade_registry_agent_enables_streaming():
    from a2a_common import client as c
    from google.adk.agents.remote_a2a_agent import RemoteA2aAgent

    # Registry helper returns a legacy (use_legacy=True), streaming=False proxy.
    legacy = RemoteA2aAgent(name="order_agent", agent_card=_card(False), description="d")
    upgraded = c.upgrade_registry_agent(legacy)

    assert upgraded is not legacy
    assert upgraded._agent_card.capabilities.streaming is True
    # new-impl registers the ADK-A2A integration request interceptor
    assert upgraded._config.request_interceptors


def test_attach_streaming_client_sets_streaming_factory(monkeypatch):
    import google.auth

    monkeypatch.setattr(google.auth, "default", lambda *a, **k: (None, "proj"))
    from a2a_common import client as c
    from google.adk.agents.remote_a2a_agent import RemoteA2aAgent

    agent = c.build_streaming_remote_agent("order_agent", _card(True), "d")
    c.attach_streaming_client(agent)

    assert agent._httpx_client is not None
    assert agent._a2a_client_factory._config.streaming is True
    assert agent._a2a_client_factory._config.polling is False


def test_specialist_transfer_stub_denies_gracefully():
    """Specialists carry a deny-stub transfer_to_agent so an imitated transfer
    (from the supervisor's context) corrects the model instead of crashing the
    A2A request with 'Tool not found' (seen live)."""
    from a2a_common.specialist import transfer_to_agent

    out = transfer_to_agent("order_agent")
    assert out["status"] == "unsupported" and "order_agent" in out["reason"]


def _ctx_with_user(uid):
    import types

    return types.SimpleNamespace(session=types.SimpleNamespace(user_id=uid))


def test_user_email_meta_provider_only_sends_real_emails():
    from a2a_common.client import user_email_meta_provider
    from a2a_common.model import USER_EMAIL_META_KEY

    assert user_email_meta_provider(_ctx_with_user(" Admin@X.com "), None) == {
        USER_EMAIL_META_KEY: "admin@x.com"}
    assert user_email_meta_provider(_ctx_with_user("dev-user"), None) == {}   # not an email
    assert user_email_meta_provider(_ctx_with_user(None), None) == {}


def test_converter_adopts_propagated_user_email(monkeypatch):
    """The specialist's converter turns the A2A metadata email into the ADK
    session user_id — the hop that makes specialist tokens user-attributable."""
    import types

    from a2a_common import specialist
    from a2a_common.model import USER_EMAIL_META_KEY
    from google.adk.a2a.converters.request_converter import AgentRunRequest
    from google.adk.agents.run_config import RunConfig

    monkeypatch.setattr(
        specialist, "convert_a2a_request_to_agent_run_request",
        lambda *a, **k: AgentRunRequest(user_id="A2A_USER_ctx1", run_config=RunConfig()),
    )
    ctx = types.SimpleNamespace(metadata={USER_EMAIL_META_KEY: " Sales@X.com "})
    out = specialist.force_sse_request_converter(ctx, "partconv")
    assert out.user_id == "sales@x.com"
    # no metadata -> default id kept
    out2 = specialist.force_sse_request_converter(types.SimpleNamespace(metadata={}), "p")
    assert out2.user_id == "A2A_USER_ctx1"


def test_user_attribution_model_callback_stamps_and_skips():
    import types

    from a2a_common.model import user_attribution_model_callback
    from google.adk.models.llm_request import LlmRequest
    from google.genai import types as gt

    def cb_ctx(uid):
        sess = types.SimpleNamespace(user_id=uid)
        return types.SimpleNamespace(_invocation_context=types.SimpleNamespace(session=sess))

    req = LlmRequest(model="m", config=gt.GenerateContentConfig(
        http_options=gt.HttpOptions(headers={"keep": "me"})))
    user_attribution_model_callback(cb_ctx(" U@X.com "), req)
    assert req.config.http_options.headers == {"keep": "me", "x-user-email": "u@x.com"}

    req2 = LlmRequest(model="m", config=gt.GenerateContentConfig())
    user_attribution_model_callback(cb_ctx("A2A_USER_ctx"), req2)   # not an email
    assert req2.config.http_options is None


def test_traceparent_threads_through_callback_and_meta(monkeypatch):
    """BFF -> streamQuery input -> contextvar -> model-call header + A2A
    metadata -> specialist contextvar: the in-band trace thread (engines can't
    read HTTP headers). One trace id across every gateway hop of a turn."""
    import types

    from a2a_common import model as m
    from a2a_common.client import user_email_meta_provider
    from a2a_common.model import TRACEPARENT_META_KEY
    from google.adk.models.llm_request import LlmRequest
    from google.genai import types as gt

    tp = "00-" + "ab" * 16 + "-" + "cd" * 8 + "-01"
    m.set_incoming_traceparent(tp)
    try:
        # model callback stamps BOTH headers
        sess = types.SimpleNamespace(user_id="u@x.com")
        ctx = types.SimpleNamespace(_invocation_context=types.SimpleNamespace(session=sess))
        req = LlmRequest(model="m", config=gt.GenerateContentConfig())
        m.user_attribution_model_callback(ctx, req)
        assert req.config.http_options.headers == {"x-user-email": "u@x.com", "traceparent": tp}
        # meta provider carries it across A2A
        meta = user_email_meta_provider(
            types.SimpleNamespace(session=types.SimpleNamespace(user_id="u@x.com")), None)
        assert meta[TRACEPARENT_META_KEY] == tp
        # invalid values are ignored (fail-safe)
        m.set_incoming_traceparent("garbage")
        assert m.incoming_traceparent() == tp
    finally:
        m._incoming_traceparent.set(None)


def test_converter_adopts_traceparent(monkeypatch):
    import types

    from a2a_common import specialist
    from a2a_common import model as m
    from a2a_common.model import TRACEPARENT_META_KEY, USER_EMAIL_META_KEY
    from google.adk.a2a.converters.request_converter import AgentRunRequest
    from google.adk.agents.run_config import RunConfig

    monkeypatch.setattr(specialist, "convert_a2a_request_to_agent_run_request",
                        lambda *a, **k: AgentRunRequest(user_id="A2A_USER_x", run_config=RunConfig()))
    tp = "00-" + "12" * 16 + "-" + "34" * 8 + "-01"
    ctx = types.SimpleNamespace(metadata={USER_EMAIL_META_KEY: "s@x.com", TRACEPARENT_META_KEY: tp})
    try:
        specialist.force_sse_request_converter(ctx, "p")
        assert m.incoming_traceparent() == tp
    finally:
        m._incoming_traceparent.set(None)


def test_session_propagates_through_meta_and_converter(monkeypatch):
    """The supervisor's live session (the frontend/user session) rides the A2A
    metadata to the specialist, which adopts it — so every agent logs the same
    user session id."""
    import types

    from a2a_common import model as m, specialist
    from a2a_common.client import user_email_meta_provider
    from a2a_common.model import SESSION_META_KEY
    from google.adk.a2a.converters.request_converter import AgentRunRequest
    from google.adk.agents.run_config import RunConfig

    ctx = types.SimpleNamespace(session=types.SimpleNamespace(user_id="u@x.com", id="FE-SESS"))
    assert user_email_meta_provider(ctx, None)[SESSION_META_KEY] == "FE-SESS"

    monkeypatch.setattr(specialist, "convert_a2a_request_to_agent_run_request",
                        lambda *a, **k: AgentRunRequest(user_id="A2A_USER_x", run_config=RunConfig()))
    try:
        specialist.force_sse_request_converter(
            types.SimpleNamespace(metadata={SESSION_META_KEY: "FE-SESS"}), "p")
        assert m.incoming_session() == "FE-SESS"
    finally:
        m._incoming_session.set(None)


def test_model_callback_logs_user_session_and_own_agent_session():
    """session_id = the propagated USER session (correlation key); agentSessionId
    = this agent's OWN session, present only when it differs (specialists)."""
    import logging as _logging
    import types

    from a2a_common import model as m

    m._log_setup_attempted = True   # skip the cloud-logging handler setup
    cc = types.SimpleNamespace(
        _invocation_context=types.SimpleNamespace(
            session=types.SimpleNamespace(user_id="u@x.com", id="LOCAL")),
        agent_name="product_agent")
    resp = types.SimpleNamespace(usage_metadata=types.SimpleNamespace(
        total_token_count=10, prompt_token_count=6, candidates_token_count=4))
    seen = {}

    class Cap(_logging.Handler):
        def emit(self, rec):
            seen.clear()
            seen.update(getattr(rec, "json_fields", {}))

    lg = _logging.getLogger("agent")
    h = Cap()
    lg.addHandler(h)
    old_level = lg.level
    lg.setLevel(_logging.INFO)
    try:
        m.set_incoming_session("FRONTEND")            # a specialist: user session propagated
        m.usage_logging_after_model_callback(cc, resp)
        assert seen["session_id"] == "FRONTEND" and seen["agentSessionId"] == "LOCAL"

        m._incoming_session.set(None)                 # the supervisor: local IS the user session
        m.usage_logging_after_model_callback(cc, resp)
        assert seen["session_id"] == "LOCAL" and "agentSessionId" not in seen
    finally:
        lg.removeHandler(h)
        lg.setLevel(old_level)
        m._incoming_session.set(None)


def test_orchestration_records_agent_turn_tool_a2a():
    """The three new spans (agent_turn, tool_call, a2a_call) share the canonical
    envelope: event/component + a latency_metrics block, plus their own body."""
    import logging as _logging
    import types

    from a2a_common import model as m

    m._log_setup_attempted = True
    cc = types.SimpleNamespace(
        _invocation_context=types.SimpleNamespace(
            session=types.SimpleNamespace(user_id="u@x.com", id="LOCAL")),
        agent_name="product_agent")
    seen = []

    class Cap(_logging.Handler):
        def emit(self, rec):
            seen.append(dict(getattr(rec, "json_fields", {})))

    lg = _logging.getLogger("agent")
    h = Cap()
    lg.addHandler(h)
    old = lg.level
    lg.setLevel(_logging.INFO)
    try:
        m.before_agent_log(cc); m.after_agent_log(cc)
        tool = types.SimpleNamespace(name="get_products")
        m.before_tool_log(tool, {}, cc); m.after_tool_log(tool, {}, cc, {})
        m.before_a2a_log(cc); m.after_a2a_log(cc)
        by = {r["event"]: r for r in seen}
        assert set(by) == {"agent_turn", "tool_call", "a2a_call"}
        for r in by.values():
            assert r["component"] == "engine"
            assert "respondedAt" in r["latency_metrics"]     # canonical block
        assert by["tool_call"]["tool"] == "get_products"
        assert by["a2a_call"]["targetAgent"] == "product_agent"
    finally:
        lg.removeHandler(h)
        lg.setLevel(old)
        m._agent_started.set(None); m._tool_started.set(None); m._a2a_started.set(None)


def test_setup_engine_cloud_logging_no_creds_is_noop(monkeypatch):
    """Without ADC the named-log setup must silently no-op (local dev/tests) —
    the platform stdout capture remains the fallback."""
    import logging

    from a2a_common import model as m

    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setenv("GCE_METADATA_HOST", "metadata.invalid")
    # ...and hide the developer's user ADC (~/.config/gcloud/...) — CLOUDSDK_CONFIG
    # relocates the well-known file, or the test fails on any gcloud-authed machine
    monkeypatch.setenv("CLOUDSDK_CONFIG", "/nonexistent-cloudsdk-config")
    before = list(logging.getLogger().handlers)
    m.setup_engine_cloud_logging()          # must not raise
    assert logging.getLogger().handlers == before


def test_usage_logging_after_model_callback(caplog):
    import logging
    import types

    from a2a_common.model import usage_logging_after_model_callback

    um = types.SimpleNamespace(prompt_token_count=10, candidates_token_count=5,
                               total_token_count=15)
    resp = types.SimpleNamespace(usage_metadata=um)
    sess = types.SimpleNamespace(user_id="u@x.com", id="s1")
    ctx = types.SimpleNamespace(
        _invocation_context=types.SimpleNamespace(session=sess), agent_name="order_agent")
    with caplog.at_level(logging.INFO, logger="agent"):
        usage_logging_after_model_callback(ctx, resp)
    rec = [r for r in caplog.records if r.name == "agent"][-1]
    jf = rec.json_fields
    assert jf["outputTokens"] == 5 and jf["user"] == "u@x.com" and jf["agent"] == "order_agent"
    # no usage -> no log, no crash
    assert usage_logging_after_model_callback(ctx, types.SimpleNamespace(usage_metadata=None)) is None
    # SSE partial (usage object with empty totals) -> also skipped
    partial = types.SimpleNamespace(usage_metadata=types.SimpleNamespace(
        prompt_token_count=None, candidates_token_count=None, total_token_count=None))
    n_before = len(caplog.records)
    assert usage_logging_after_model_callback(ctx, partial) is None
    assert len(caplog.records) == n_before
