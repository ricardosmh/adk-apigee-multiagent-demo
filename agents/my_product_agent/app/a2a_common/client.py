"""Supervisor-side A2A client: streaming RemoteA2aAgent proxies + bearer auth.

This is what makes specialist tokens stream back through the supervisor. Three
things the stock path gets wrong for our case (all verified against
google-adk 2.3.0 — see the ``adk-2.3-a2a-streaming-api`` memo):

  1. ``RemoteA2aAgent`` defaults ``use_legacy=True``. The legacy path does not
     speak the new ADK-A2A streaming extension, so we rebuild with
     ``use_legacy=False`` (which registers the extension request-interceptor).
  2. ``AgentRegistry.get_remote_a2a_agent()`` never passes ``use_legacy`` and,
     on its synthetic-card fallback, hardcodes ``streaming=False``. So we take
     the card it resolved and rebuild the proxy ourselves.
  3. ``RemoteA2aAgent``'s default ``A2AClientFactory`` is hardcoded
     ``streaming=False``. So at runtime we attach a factory built with
     ``streaming=True`` and our authed, loop-bound httpx client.

Auth: both engines run as the same runner SA; each A2A call is signed with that
SA's ADC bearer token (``GCPAuth``).
"""
import asyncio

import google.auth
import google.auth.transport.requests
import httpx
from a2a.client.client import ClientConfig as A2AClientConfig
from a2a.client.client_factory import ClientFactory as A2AClientFactory
from a2a.types import AgentCard
from a2a.types import TransportProtocol as A2ATransport
from google.adk.agents.remote_a2a_agent import RemoteA2aAgent

from .model import (SESSION_META_KEY, TRACEPARENT_META_KEY, USER_EMAIL_META_KEY,
                    after_a2a_log, before_a2a_log, incoming_session,
                    incoming_traceparent)

_A2A_TIMEOUT = httpx.Timeout(timeout=600.0)


# ── Auth ──────────────────────────────────────────────────────────────────────
class GCPAuth(httpx.Auth):
    """Refresh ADC credentials per request and set a Bearer token.

    Construct only at runtime (cloud side), never at module import:
    google.auth.Credentials holds a threading.RLock that cannot survive
    AdkApp.clone()'s deepcopy at deploy time.
    """

    def __init__(self) -> None:
        self._credentials, _ = google.auth.default()

    def auth_flow(self, request):
        # Refresh only when missing/expired — google.auth tracks expiry, so this
        # avoids a metadata-server round-trip on every single A2A call.
        if not self._credentials.valid:
            self._credentials.refresh(google.auth.transport.requests.Request())
        request.headers["Authorization"] = f"Bearer {self._credentials.token}"
        yield request


def make_authed_async_client() -> httpx.AsyncClient:
    """An httpx.AsyncClient that signs every A2A call with an ADC Bearer token."""
    return httpx.AsyncClient(auth=GCPAuth(), timeout=_A2A_TIMEOUT)


# ── Streaming RemoteA2aAgent construction ──────────────────────────────────────
def _streaming_factory(httpx_client: httpx.AsyncClient) -> A2AClientFactory:
    """An A2AClientFactory that requests SSE streaming (the stock default is off)."""
    return A2AClientFactory(
        config=A2AClientConfig(
            httpx_client=httpx_client,
            streaming=True,
            polling=False,
            supported_transports=[A2ATransport.jsonrpc, A2ATransport.http_json],
        )
    )


def user_email_meta_provider(ctx, a2a_message) -> dict:
    """a2a_request_meta_provider: attach the end user's email to every outgoing
    A2A request (-> MessageSendParams.metadata -> the specialist's
    RequestContext.metadata). Only real emails travel — the dev fallback ids
    and the no-identity case send nothing (specialist records anonymous)."""
    meta = {}
    uid = getattr(getattr(ctx, "session", None), "user_id", None)
    if isinstance(uid, str) and "@" in uid:
        meta[USER_EMAIL_META_KEY] = uid.strip().lower()
    tp = incoming_traceparent()
    if tp:
        meta[TRACEPARENT_META_KEY] = tp
    # Propagate the FRONTEND session so the specialist logs the conversation's
    # session, not its own A2A one. The supervisor's live session (ctx.session.id)
    # IS the frontend session; a propagated-in value (deeper A2A) wins if present.
    sid = incoming_session() or getattr(getattr(ctx, "session", None), "id", None)
    if sid:
        meta[SESSION_META_KEY] = str(sid)
    return meta


def build_streaming_remote_agent(
    name: str, agent_card: AgentCard | str, description: str = ""
) -> RemoteA2aAgent:
    """A new-impl (use_legacy=False) RemoteA2aAgent, WITHOUT a client.

    No httpx client / factory is attached here — both hold a threading.RLock
    that can't survive the deploy-time deepcopy. The authed, loop-bound client
    and the streaming factory are attached at runtime by
    ``attach_streaming_client`` / ``ensure_loop_bound_clients``.

    If ``agent_card`` is a concrete AgentCard we force ``capabilities.streaming``
    on — we own the specialists and they all publish streaming servers, so this
    corrects the registry's synthetic-card fallback (which hardcodes False).
    """
    if isinstance(agent_card, AgentCard) and agent_card.capabilities is not None:
        agent_card.capabilities.streaming = True
    return RemoteA2aAgent(
        name=name,
        agent_card=agent_card,
        description=description or "",
        use_legacy=False,
        # Propagate the END USER to the specialist (per-user token attribution
        # at the Apigee gateway). Module-level function: pickles by reference.
        a2a_request_meta_provider=user_email_meta_provider,
        # One a2a_call record per delegation — the round trip from the caller's
        # side (its backend is the specialist's own agent_turn + A2A transport).
        before_agent_callback=before_a2a_log,
        after_agent_callback=after_a2a_log,
    )


def agent_card_url(agent) -> str | None:
    """The A2A endpoint URL a RemoteA2aAgent will call, from its resolved card
    (or its card-source URL). Used by the supervisor's runtime refresh to detect
    that a registry entry now points at a DIFFERENT engine (specialist
    redeployed) so the stale proxy can be replaced in place."""
    card = getattr(agent, "_agent_card", None)
    if card is not None and getattr(card, "url", None):
        return card.url
    source = getattr(agent, "_agent_card_source", None)
    if isinstance(source, str):
        return source
    if source is not None and getattr(source, "url", None):
        return source.url
    return None


def upgrade_registry_agent(legacy: RemoteA2aAgent) -> RemoteA2aAgent:
    """Rebuild a registry-resolved RemoteA2aAgent as a streaming new-impl proxy.

    ``AgentRegistry.get_remote_a2a_agent()`` returns a legacy, non-streaming
    proxy but does the card resolution for us. We reuse its resolved card (or
    URL source) and rebuild with streaming enabled.
    """
    card = getattr(legacy, "_agent_card", None)
    source = card if card is not None else getattr(legacy, "_agent_card_source", None)
    if source is None:
        return legacy  # nothing to rebuild from — keep the legacy proxy
    return build_streaming_remote_agent(legacy.name, source, legacy.description or "")


# ── Runtime client lifecycle (loop-bound) ──────────────────────────────────────
def attach_streaming_client(agent: RemoteA2aAgent) -> None:
    """Attach an authed httpx client AND a matching streaming factory.

    Both must be set together: ``RemoteA2aAgent._ensure_httpx_client`` only
    swaps the factory's client when it has to *create* the client itself, so if
    we pre-set the client we must also supply the streaming factory built with
    that same client — otherwise the stock streaming=False factory is used.
    """
    client = make_authed_async_client()
    agent._httpx_client = client
    agent._httpx_client_needs_cleanup = True
    agent._a2a_client_factory = _streaming_factory(client)


def walk_agents(agent):
    yield agent
    for sub in getattr(agent, "sub_agents", []) or []:
        yield from walk_agents(sub)


def ensure_loop_bound_clients(root_agent) -> None:
    """Bind every reachable RemoteA2aAgent's authed client to the CURRENT loop.

    httpx.AsyncClient binds its connection pool to the loop of first use. The
    warm-start client is built on the worker-startup loop, but Agent Engine
    serves each A2A transfer on a per-request loop; reusing the startup client
    raises 'got Future attached to a different loop'. Call this every turn from
    before_agent_callback (which runs in the request loop). Bounded: at most one
    client per (agent, loop).
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    for agent in walk_agents(root_agent):
        if not isinstance(agent, RemoteA2aAgent):
            continue
        if (
            getattr(agent, "_httpx_client", None) is not None
            and getattr(agent, "_a2a_client_loop", None) is loop
        ):
            continue
        attach_streaming_client(agent)
        agent._a2a_client_loop = loop


def inject_streaming_clients(app) -> None:
    """Warm-start: attach authed streaming clients to every RemoteA2aAgent.

    Runs cloud-side after AdkApp.clone()'s deepcopy (clients hold an RLock and
    can't survive that deepcopy at deploy time).
    """
    for agent in walk_agents(app.root_agent):
        if isinstance(agent, RemoteA2aAgent):
            attach_streaming_client(agent)
