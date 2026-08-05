"""Supervisor orchestrator — resolves specialist sub-agents via Agent Registry.

Sub-agents are resolved two ways:
  * deploy-time warm start (`_resolve_sub_agents` at import, baked into the
    pickle) — gives the supervisor a working set the moment it boots.
  * runtime refresh (`_refresh_sub_agents` before_agent_callback) — re-reads
    Agent Registry on a TTL so a newly deployed/registered specialist is picked
    up WITHOUT redeploying the supervisor.

Specialists are reached as STREAMING RemoteA2aAgent proxies (use_legacy=False +
a streaming A2AClientFactory) so their tokens stream back through the supervisor.
All A2A client/auth/model scaffolding lives in ``app.a2a_common``.
"""
import asyncio
import logging
import os
import time

from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.integrations.agent_registry import AgentRegistry

from app import acl
from app.a2a_common import client as a2a_client
from app.a2a_common import model as a2a_model
from app.a2a_common.model import build_apigee_model

logger = logging.getLogger(__name__)

# Builds the Apigee-fronted Gemini model AND configures the GenAI env defaults
# (GOOGLE_CLOUD_PROJECT from ADC, etc.) + installs the host-scoped TLS skip.
# Must run before _resolve_sub_agents() so registry discovery sees the project.
apigee_model = build_apigee_model()


# Display names of the specialists we expect in Agent Registry. Registry
# resource IDs (agentregistry-<UUID>) are resolved at runtime by listing
# registered agents and filtering on these names — no IDs hardcoded.
_SPECIALIST_DISPLAY_NAMES = ("order_agent", "product_agent", "customer_agent")


def _attachment_allowed(display: str) -> bool:
    """ATTACHMENT policy for the runtime refresh: which registry agents the
    supervisor may adopt at all. (The per-user ACL then governs which of the
    attached agents each user may USE — two different layers.)

    Default: only the known specialists. Before this filter the refresh
    adopted EVERYTHING in the regional registry (Workspace_Agent, Gemini
    Enterprise built-ins) and warned every TTL about entries it couldn't
    build (the URI-less a2a-supervisor-agent entry).

    Overrides (deploy-time env):
      SUBAGENT_ALLOWLIST  — comma-separated display names REPLACING the default
      SUBAGENT_ATTACH_ALL=1 — restore the old adopt-everything behavior
    """
    if os.environ.get("SUBAGENT_ATTACH_ALL") == "1":
        return True
    raw = os.environ.get("SUBAGENT_ALLOWLIST", "")
    allowed = {n.strip() for n in raw.split(",") if n.strip()} or set(_SPECIALIST_DISPLAY_NAMES)
    return display in allowed


def _resolve_sub_agents() -> list:
    """Look up specialists in Agent Registry by display name.

    Each resolved agent is rebuilt as a STREAMING new-impl RemoteA2aAgent
    (``a2a_client.upgrade_registry_agent``) — the registry helper returns a
    legacy, non-streaming proxy but does the card resolution for us. The proxies
    are created WITHOUT an httpx client (httpx.AsyncClient holds a _thread.RLock
    that breaks AdkApp.clone()'s deepcopy at deploy time). The authed streaming
    client is attached at runtime (agent_runtime_app.set_up / before_agent).

    If an explicit override is set (e.g. ORDER_AGENT_NAME), that resource name
    wins over discovery. Cloud-side this re-runs on every worker import but the
    unpickled runtime already carries deploy-time sub_agents, and the runner SA
    typically lacks agentregistry.agents.list — so set SKIP_REGISTRY_DISCOVERY=1
    (the deploy worker does) to short-circuit and rely on the pickled state.
    """
    if os.environ.get("SKIP_REGISTRY_DISCOVERY") == "1":
        return []
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    location = os.environ.get("GOOGLE_CLOUD_LOCATION") or "us-central1"
    if not project:
        return []

    registry = AgentRegistry(project_id=project, location=location)

    # Discover {displayName: resourceName} once. Log loudly on failure so a
    # silent miss can't masquerade as a working supervisor with no sub-agents.
    discovered: dict[str, str] = {}
    try:
        page = registry.list_agents(page_size=100)
        for entry in page.get("agents", []):
            display = entry.get("displayName")
            name = entry.get("name")
            if display and name:
                discovered[display] = name
        logger.info(
            "AgentRegistry discovered %d agent(s) in %s/%s: %s",
            len(discovered), project, location, sorted(discovered),
        )
    except Exception as e:
        logger.exception("AgentRegistry.list_agents() failed: %s", e)

    resolved: list = []
    for display in _SPECIALIST_DISPLAY_NAMES:
        override = os.environ.get(f"{display.upper()}_NAME")
        agent_name = override or discovered.get(display)
        if agent_name:
            logger.info("Resolved sub-agent %r -> %s", display, agent_name)
            legacy = registry.get_remote_a2a_agent(agent_name)
            resolved.append(a2a_client.upgrade_registry_agent(legacy))
        else:
            logger.warning(
                "Sub-agent %r not found in registry (discovered: %s) "
                "and no %s env-var override set. Supervisor will lack this "
                "sub-agent and the model may hallucinate transfers to it.",
                display, sorted(discovered), f"{display.upper()}_NAME",
            )
    if not resolved:
        logger.error(
            "Supervisor resolved ZERO sub-agents — model will be unable to "
            "delegate. Check that display names %s exist in Agent Registry "
            "at %s/%s.",
            list(_SPECIALIST_DISPLAY_NAMES), project, location,
        )
    return resolved


# ── Runtime sub-agent refresh ────────────────────────────────────────────────
# Re-resolve specialists from Agent Registry at runtime (TTL-cached) so a newly
# deployed/registered specialist is picked up WITHOUT redeploying the
# supervisor. Requires the runner SA to have agentregistry read access (granted
# in agents/runtime-manifest.yaml). Set DISABLE_RUNTIME_SUBAGENT_REFRESH=1 to opt out and
# rely only on the deploy-time baked set.
_REFRESH_TTL = float(os.environ.get("SUBAGENT_REFRESH_TTL_SECONDS", "60"))
_refresh_lock = asyncio.Lock()
_last_refresh: float | None = None


_registry_client: AgentRegistry | None = None


def _registry() -> AgentRegistry:
    """The runtime AgentRegistry client, cached per worker process.

    Used by the TTL refresh (`_discover_agents` / `_build_remote_agent`, both run
    via `asyncio.to_thread` — the registry client is sync, not loop-bound, so a
    single cached instance is safe). Was reconstructed on every call before.
    """
    global _registry_client
    if _registry_client is None:
        project = os.environ.get("GOOGLE_CLOUD_PROJECT")
        location = os.environ.get("GOOGLE_CLOUD_LOCATION") or "us-central1"
        _registry_client = AgentRegistry(project_id=project, location=location)
    return _registry_client


def _discover_agents() -> dict[str, str]:
    """{displayName: resourceName} for every registered agent, choosing the
    NEWEST entry when a display name has duplicates. Blocking I/O.

    Vertex auto-registers a fresh registry entry per engine generation, so after
    a redeploy several `product_agent` entries linger — older ones point at
    engines that a later --cleanup deletes. Last-wins picked them arbitrarily,
    so the supervisor could resolve to a dead engine and A2A-403. Prefer the
    most recently created/updated entry (== the newest engine)."""
    if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
        return {}
    page = _registry().list_agents(page_size=100)
    best: dict[str, tuple[str, str]] = {}   # display -> (recency-key, resourceName)
    for entry in page.get("agents", []):
        display, name = entry.get("displayName"), entry.get("name")
        if not (display and name):
            continue
        key = entry.get("updateTime") or entry.get("createTime") or ""
        if display not in best or key >= best[display][0]:
            best[display] = (key, name)
    return {d: v[1] for d, v in best.items()}


def _build_remote_agent(resource_name: str):
    """Build a STREAMING RemoteA2aAgent from a registry entry. Blocking I/O.

    NO client is attached here — the refresh also builds these just to COMPARE
    card URLs (heal check) and discards unchanged ones; the caller attaches a
    client only when the agent is actually adopted."""
    legacy = _registry().get_remote_a2a_agent(resource_name)
    return a2a_client.upgrade_registry_agent(legacy)


# Cache of the freshest resolution: displayName -> built RemoteA2aAgent proxy
# (no client attached; per-request copies attach + loop-bind). The NETWORK list
# that fills this is throttled to _REFRESH_TTL; the per-turn re-point reads it,
# so a running supervisor tracks a specialist's NEW engine id WITHOUT a redeploy
# and with no stale-URI window on TTL-cache-hit turns (the old failure mode:
# baked/healed URI frozen until the next refresh -> A2A 403 after a rollout).
_resolved: dict[str, object] = {}


def _mirror_cache_to_root() -> None:
    """Point the module-global root_agent's sub_agents at the cached remotes so
    FUTURE per-request deep-copies start already-fresh (steady state: the
    per-turn re-point then finds nothing to change). Best-effort; under lock."""
    import copy as _copy
    idx = {getattr(sa, "name", None): i for i, sa in enumerate(root_agent.sub_agents)}
    for display, cached in _resolved.items():
        try:
            shared = _copy.copy(cached)
            shared.parent_agent = root_agent
            if display in idx:
                root_agent.sub_agents[idx[display]] = shared
            else:
                root_agent.sub_agents.append(shared)
                idx[display] = len(root_agent.sub_agents) - 1
        except Exception as e:  # noqa: BLE001
            logger.warning("mirror %r to shared root failed (%s)", display, e)


async def _refresh_resolved_cache() -> None:
    """THROTTLED (TTL) network step: list the registry, build a proxy for each
    allowed specialist's NEWEST entry, cache it, and mirror into the shared root.
    One round-trip per TTL window; keeps the existing cache on any error."""
    global _last_refresh
    now = time.monotonic()
    if _last_refresh is not None and now - _last_refresh < _REFRESH_TTL:
        return
    async with _refresh_lock:
        if _last_refresh is not None and time.monotonic() - _last_refresh < _REFRESH_TTL:
            return
        try:
            discovered = await asyncio.to_thread(_discover_agents)  # displayName -> NEWEST resourceName
        except Exception as e:  # noqa: BLE001 — keep the current cache on failure
            logger.warning("sub-agent resolve: list_agents failed (%s); keeping cache", e)
            return
        for display, resource_name in discovered.items():
            if display == root_agent.name or not _attachment_allowed(display):
                continue
            try:
                _resolved[display] = await asyncio.to_thread(_build_remote_agent, resource_name)
            except Exception as e:  # noqa: BLE001 — keep the prior proxy if we have one
                logger.warning("sub-agent resolve: build %r failed (%s)", display, e)
        _mirror_cache_to_root()
        _last_refresh = time.monotonic()


def _apply_resolved(agent) -> None:
    """EVERY turn: re-point THIS request's sub_agents at the cached (freshest)
    remotes. Cheap — compares card URLs and only rebuilds a proxy when the
    registry actually moved. This is what removes the 'stale until a redeploy /
    next TTL heal' window: once the cache has the new engine, no turn serves a
    baked-but-dead URI. In steady state (URL unchanged) it does nothing."""
    if not _resolved:
        return
    import copy as _copy
    idx = {getattr(sa, "name", None): i for i, sa in enumerate(agent.sub_agents)}
    changed = []
    for display, cached in _resolved.items():
        new_url = a2a_client.agent_card_url(cached)
        if not new_url:
            continue
        if display in idx:
            i = idx[display]
            if a2a_client.agent_card_url(agent.sub_agents[i]) == new_url:
                continue                      # already current — no work
            proxy = _copy.copy(cached)
            a2a_client.attach_streaming_client(proxy)
            proxy.parent_agent = agent
            agent.sub_agents[i] = proxy
            changed.append(f"{display} -> {new_url}")
        else:
            proxy = _copy.copy(cached)
            a2a_client.attach_streaming_client(proxy)
            proxy.parent_agent = agent
            agent.sub_agents.append(proxy)
            idx[display] = len(agent.sub_agents) - 1
            changed.append(f"+{display}")
    if changed:
        logger.info("sub-agent re-point (fresh engine): %s", changed)


async def _refresh_sub_agents(callback_context) -> None:
    """before_agent_callback: keep this request's sub_agents pointed at the
    freshest registered engines WITHOUT a supervisor redeploy.

    Per turn: re-point from the resolution cache (cheap; rebuilds only on a real
    change). The registry LIST that fills the cache is throttled to _REFRESH_TTL.
    Then bind every sub_agent's A2A client to THIS request's event loop (else
    transfers raise 'Future attached to a different loop'). Best-effort — never
    raises; DISABLE_RUNTIME_SUBAGENT_REFRESH=1 falls back to the baked set."""
    agent = callback_context._invocation_context.agent
    if os.environ.get("DISABLE_RUNTIME_SUBAGENT_REFRESH") != "1":
        try:
            await _refresh_resolved_cache()
            _apply_resolved(agent)
        except Exception as e:  # noqa: BLE001 — serving the turn matters more
            logger.warning("sub-agent refresh failed (%s); using current set", e)
    # Loop-safety runs EVERY turn (even when refresh is disabled): bind all
    # sub_agent clients — including any just re-pointed — to this request's loop.
    a2a_client.ensure_loop_bound_clients(agent)
    return None


async def _before_agent(callback_context) -> None:
    """before_agent_callback: runtime sub-agent refresh, then the per-user ACL
    filter. The ACL is a no-op unless ACL_ENABLED (default off), so production
    behaviour is unchanged until IAP (Phase 3c) provides a real user_id."""
    await _refresh_sub_agents(callback_context)
    acl.before_agent_acl(callback_context)
    return None


root_agent = Agent(
    model=apigee_model,
    name="root_agent",
    description="Main supervisor agent that answers user queries by coordinating specialized sub-agents over secure A2A protocols.",
    instruction="""
      You are the Main Coordinator Agent. You assist users by delegating specialized tasks to your remote sub-agents over native A2A protocol:
      - If a query involves order management, tracking, line items, or order status updates, transfer to `order_agent`.
      - If a query involves product details, searching products, brand/category catalog lookups, or product stock updates, transfer to `product_agent`.
      - If a query involves customer account management, customer profiles, or addresses, transfer to `customer_agent`.

      You may also have additional specialist sub-agents beyond those listed above. When one is available, pick the most appropriate agent for the query based on its described capabilities.

      Make sure to orchestrate the sub-agents sequentially when a query involves multiple domains. For example, if a user wants to find product details and then create an order for a customer with those products, first retrieve product details using `product_agent`, retrieve customer details using `customer_agent`, and then place the order using `order_agent`.

      Combine information from all relevant sub-agents to formulate a comprehensive and helpful response.
    """,
    sub_agents=_resolve_sub_agents(),
    # agent_turn (supervisor's whole run) stamps first, then the refresh+ACL.
    before_agent_callback=[a2a_model.before_agent_log, _before_agent],
    after_agent_callback=a2a_model.after_agent_log,
    # tool_call span, then the per-user ACL hard block (fail-closed; no-op
    # unless ACL_ENABLED — returns a block dict that short-circuits the list).
    before_tool_callback=[a2a_model.before_tool_log, acl.before_tool_acl],
    after_tool_callback=a2a_model.after_tool_log,
    # Per-user token attribution at the Apigee gateway (dc_user_id).
    before_model_callback=a2a_model.user_attribution_model_callback,
    # Per-model-call usage entry in agent-ai-logs (trace-linked).
    after_model_callback=a2a_model.usage_logging_after_model_callback,
)

# Lets the ACL defensively refuse to mutate the shared object if a future ADK
# stops deep-copying the per-request agent (the hard block still enforces).
acl.MODULE_ROOT_AGENT = root_agent

app = App(root_agent=root_agent, name="app")
