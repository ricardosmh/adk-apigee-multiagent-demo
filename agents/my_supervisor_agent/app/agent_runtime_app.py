"""Runtime wrapper for the supervisor (A2A client)."""
import os

import vertexai
from dotenv import load_dotenv
from vertexai.agent_engines.templates.adk import AdkApp

from app.a2a_common import model as a2a_model
from app.a2a_common.client import inject_streaming_clients
from app.agent import app as adk_app

load_dotenv()


class AgentEngineApp(AdkApp):
    def set_up(self) -> None:
        vertexai.init(
            project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
            location=os.environ.get("GOOGLE_CLOUD_LOCATION") or "us-central1",
        )
        super().set_up()
        a2a_model.setup_engine_cloud_logging()   # named log: agent-ai-logs (+trace)
        cloud_app = self._tmpl_attrs.get("app")
        if cloud_app is not None:
            # Warm-start: attach authed STREAMING clients to the deploy-time-baked
            # RemoteA2aAgents. Runtime-added ones get their client when the
            # before_agent_callback in app.agent builds them.
            inject_streaming_clients(cloud_app)

    async def async_stream_query(self, *, traceparent: str | None = None, **kwargs):
        """Adopt the BFF's W3C trace context for this turn. Engines can't read
        HTTP headers, so the BFF threads `traceparent` IN-BAND in the
        streamQuery input (AdkApp's own **kwargs tolerates the extra key). The
        contextvar makes it available to the model-call header injection and
        the A2A metadata provider — every gateway hop of the turn then shares
        the BFF's trace id."""
        a2a_model.set_incoming_traceparent(traceparent)
        async for event in super().async_stream_query(**kwargs):
            yield event


# enable_tracing=True ensures the per-Agent-Engine "OpenTelemetry traces +
# prompt/response logging" console toggle is honoured. Without it, AdkApp's
# legacy override would force ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=false and
# suppress traces regardless of the console setting.
agent_runtime = AgentEngineApp(app=adk_app, enable_tracing=True)
