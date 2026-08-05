"""Build a domain specialist: its ADK Agent + its A2A server runtime.

Shared by my_{order,product,customer}_agent. Each specialist's ``agent.py``
collapses to a single ``build_specialist(...)`` call supplying only its
*behavioral* definition (name, description, instruction, tool_filter); the
mechanical scaffolding (model, MCP wiring, A2A server) lives here.
"""
import logging
import os

from google.adk.a2a.converters.request_converter import (
    convert_a2a_request_to_agent_run_request,
)
from google.adk.agents import Agent
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.apps import App
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset

from .model import (SESSION_META_KEY, TRACEPARENT_META_KEY, USER_EMAIL_META_KEY,
                    after_agent_log, after_tool_log, before_agent_log,
                    before_tool_log, build_apigee_model, set_incoming_session,
                    set_incoming_traceparent, usage_logging_after_model_callback,
                    user_attribution_model_callback)

logger = logging.getLogger(__name__)

_sse_logged = False


def force_sse_request_converter(*args, **kwargs):
    """Force the specialist's Runner into SSE so its MODEL streams token deltas.

    MODULE-LEVEL on purpose: it's stored on the A2aAgentExecutorConfig that gets
    cloudpickled into the deployed engine. A nested/local function is pickled by
    value and can be silently dropped on the round-trip (reverting to the stock
    converter → streaming_mode=NONE → the specialist answers in one block). A
    module-level function is pickled by REFERENCE (import path) and survives.

    ADK's default converter builds RunConfig with no streaming_mode (→ NONE);
    both the legacy and new-impl executors read config.request_converter, so
    setting SSE here makes run_async emit partial events. A non-streaming caller
    still gets the final aggregated result.
    """
    global _sse_logged
    req = convert_a2a_request_to_agent_run_request(*args, **kwargs)
    req.run_config = req.run_config or RunConfig()
    req.run_config.streaming_mode = StreamingMode.SSE
    # End-user propagation: the supervisor attaches the user's email to the A2A
    # request metadata (client.user_email_meta_provider). Adopt it as this
    # specialist's session user_id so user_attribution_model_callback can stamp
    # x-user-email on the specialist's own model calls. Without it the default
    # A2A_USER_<ctx> id stays (and no header is sent -> anonymous).
    ctx = args[0] if args else kwargs.get("request")
    meta = getattr(ctx, "metadata", None) or {}
    email = meta.get(USER_EMAIL_META_KEY)
    if isinstance(email, str) and "@" in email:
        req.user_id = email.strip().lower()
    # Adopt the caller's trace context (threaded from the BFF via the
    # supervisor) so THIS specialist's model calls join the same trace.
    set_incoming_traceparent(meta.get(TRACEPARENT_META_KEY))
    # Adopt the caller's FRONTEND session so this specialist logs the user
    # session as session_id (its own A2A session still goes out as agentSessionId).
    set_incoming_session(meta.get(SESSION_META_KEY))
    if not _sse_logged:
        # One-time marker per worker — confirms from Cloud Logging that this
        # override runs in the deployed specialist. INFO (not WARNING): it's
        # expected steady-state behaviour, not a problem.
        logger.info("A2A specialist: RunConfig.streaming_mode forced to SSE")
        _sse_logged = True
    return req


def _rebuild_mcp_toolset(tool_filter: list[str]) -> "EnvKeyMcpToolset":
    """Unpickle hook — reconstructs the toolset from the CURRENT env."""
    ts = build_mcp_toolset(tool_filter)
    if ts is None:
        raise RuntimeError("APIGEE_MCP_URL missing at runtime — cannot rebuild McpToolset")
    return ts


class EnvKeyMcpToolset(McpToolset):
    """McpToolset that pickles as a RECIPE, not a snapshot.

    Same reason as EnvKeyApigeeLlm: the engine runs the unpickled object, so
    headers built on the deploy host (where APIGEE_API_KEY doesn't exist) would
    ship WITHOUT the x-api-key and 401 against the MCP gateway's VerifyAPIKey.
    Unpickling re-runs build_mcp_toolset() on the engine, reading the
    SecretRef-injected key; only ``tool_filter`` (plain data) is serialized.
    """

    def __init__(self, *, connection_params, tool_filter: list[str]):
        super().__init__(connection_params=connection_params, tool_filter=tool_filter)
        self._recipe_tool_filter = list(tool_filter)

    def __reduce__(self):
        return (_rebuild_mcp_toolset, (self._recipe_tool_filter,))


def build_mcp_toolset(tool_filter: list[str]) -> EnvKeyMcpToolset | None:
    """Wire an McpToolset to the Apigee MCP gateway, scoped to ``tool_filter``.

    Returns None if APIGEE_MCP_URL is unset (no hardcoded IP fallback — a baked
    default silently points an agent at a stale gateway).
    """
    mcp_url = os.environ.get("APIGEE_MCP_URL")
    if not mcp_url:
        return None

    headers: dict[str, str] = {}
    token = os.environ.get("APIGEE_MCP_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    # Same single key as the model call — the agent's one Apigee key covers both
    # the LLM and MCP gateways. Legacy APIGEE_MCP_API_KEY kept as a fallback.
    api_key = os.environ.get("APIGEE_API_KEY") or os.environ.get("APIGEE_MCP_API_KEY")
    if api_key:
        headers["x-api-key"] = api_key

    return EnvKeyMcpToolset(
        connection_params=StreamableHTTPConnectionParams(
            url=mcp_url,
            headers=headers or None,
            timeout=15.0,
        ),
        tool_filter=tool_filter,
    )


def transfer_to_agent(agent_name: str) -> dict:
    """Deny-stub: specialists cannot transfer to other agents.

    The A2A context handed to a specialist contains the SUPERVISOR's
    transfer_to_agent calls, and the specialist's model sometimes imitates
    them. Specialists have no sub-agents, so ADK creates no transfer tool and
    an imitated call crashed the whole A2A request with "Tool
    'transfer_to_agent' not found" (seen live). This stub turns that crash
    into a structured correction the model can recover from in-turn.
    """
    return {
        "status": "unsupported",
        "reason": (
            f"Transfers are not available here — '{agent_name}' can only be "
            "reached by the coordinator. Answer the parts of the request "
            "within YOUR domain using your own tools; if something is outside "
            "it, say so and let the coordinator route the rest."
        ),
    }


_NO_TRANSFER_INSTRUCTION = (
    "\n\nYou are a specialist and CANNOT transfer to other agents — never call "
    "transfer_to_agent. Handle what is in your domain with your own tools and "
    "state plainly when part of a request is outside it."
)


def build_specialist(
    *,
    name: str,
    description: str,
    instruction: str,
    tool_filter: list[str],
) -> tuple[Agent, App]:
    """Return ``(root_agent, app)`` for a specialist.

    ``name`` is load-bearing — it becomes the A2A AgentCard identity that the
    supervisor's ``transfer_to_agent`` routes on.
    """
    toolset = build_mcp_toolset(tool_filter)
    root_agent = Agent(
        model=build_apigee_model(),
        name=name,
        description=description,
        instruction=instruction + _NO_TRANSFER_INSTRUCTION,
        tools=([toolset] if toolset else []) + [transfer_to_agent],
        # Per-user token attribution at the Apigee gateway (dc_user_id); no-op
        # unless the A2A request carried the user's email (see converter above).
        before_model_callback=user_attribution_model_callback,
        # Per-model-call usage entry in agent-ai-logs (trace-linked).
        after_model_callback=usage_logging_after_model_callback,
        # agent_turn (this specialist's whole run) + tool_call (each MCP call)
        before_agent_callback=before_agent_log,
        after_agent_callback=after_agent_log,
        before_tool_callback=before_tool_log,
        after_tool_callback=after_tool_log,
    )
    app = App(root_agent=root_agent, name="app")
    return root_agent, app


def build_a2a_server_runtime(app: App, *, streaming: bool = True):
    """Build the vertexai ``A2aAgent`` runtime that publishes ``app`` over A2A.

    ``streaming=True`` advertises ``message:stream`` on the AgentCard so a
    new-impl client (use_legacy=False + a streaming A2AClientFactory) receives
    token-level SSE. The A2aAgentExecutor defaults to the new impl
    (use_legacy=False) in ADK 2.x, so no extra flag is needed server-side.

    vertexai is imported lazily so the pure-ADK ``build_specialist`` path stays
    importable without the aiplatform SDK.
    """
    import asyncio

    import nest_asyncio
    from a2a.types import AgentCapabilities, AgentCard, AgentExtension, TransportProtocol
    from google.adk.a2a.executor.a2a_agent_executor import A2aAgentExecutor
    from google.adk.a2a.executor.config import A2aAgentExecutorConfig
    from google.adk.a2a.utils.agent_card_builder import AgentCardBuilder
    from google.adk.artifacts import InMemoryArtifactService
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from vertexai.preview.reasoning_engines import A2aAgent

    # Uses the MODULE-LEVEL force_sse_request_converter (pickle-robust — see its
    # docstring) so streaming_mode=SSE survives the deploy-time cloudpickle.
    _executor_config = A2aAgentExecutorConfig(
        request_converter=force_sse_request_converter
    )

    async def _build_agent_card() -> AgentCard:
        builder = AgentCardBuilder(
            agent=app.root_agent,
            capabilities=AgentCapabilities(
                streaming=streaming,
                extensions=[
                    AgentExtension(
                        uri="https://google.github.io/adk-docs/a2a/a2a-extension/",
                        description="Agent executor extension",
                    ),
                ],
            ),
            rpc_url="http://localhost:9999/",  # placeholder — Agent Engine rewrites it
            agent_version=os.getenv("AGENT_VERSION", "0.1.0"),
        )
        card = await builder.build()
        card.preferred_transport = TransportProtocol.http_json
        card.supports_authenticated_extended_card = True
        return card

    class AgentEngineApp(A2aAgent):
        @staticmethod
        def create() -> "AgentEngineApp":
            def create_runner() -> Runner:
                return Runner(
                    app=app,
                    session_service=InMemorySessionService(),
                    artifact_service=InMemoryArtifactService(),
                )

            try:
                asyncio.get_running_loop()
                nest_asyncio.apply()
            except RuntimeError:
                pass

            agent_card = asyncio.run(_build_agent_card())
            return AgentEngineApp(
                agent_executor_builder=lambda: A2aAgentExecutor(
                    runner=create_runner(), config=_executor_config
                ),
                agent_card=agent_card,
            )

        def set_up(self) -> None:
            # Explicit project/location avoids the Cloud Resource Manager API
            # lookup at startup that fails when CRM API isn't enabled.
            import vertexai

            vertexai.init(
                project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
                location=os.environ.get("GOOGLE_CLOUD_LOCATION") or "us-central1",
            )
            super().set_up()
            from .model import setup_engine_cloud_logging

            setup_engine_cloud_logging()   # named log: agent-ai-logs (+trace)

    return AgentEngineApp.create()
