# Supervisor ↔ Sub-Agent Integration (A2A)

How the supervisor orchestrator delegates to the three domain specialists over
the **Agent-to-Agent (A2A)** protocol on Vertex AI Agent Engine — the exact
classes, the auth, the discovery, and the per-turn call flow, with the code that
makes each part work.

> **TL;DR.** Each specialist is deployed as an Agent Engine wrapped in a
> `vertexai…A2aAgent`, which publishes an **AgentCard** and an A2A message
> endpoint. The supervisor is an ADK `Agent` whose `sub_agents` are
> **`RemoteA2aAgent`** proxies, resolved by display name through **Agent
> Registry**. ADK turns those sub-agents into a `transfer_to_agent` tool; when
> the model calls it, the matching `RemoteA2aAgent` makes an authenticated HTTP
> A2A call to the specialist's engine. No identity is propagated over A2A — both
> sides run as the **same runner service account**, and the call is signed with
> that SA's ADC bearer token.

> **Refactor note (ADK 2.3).** The *mechanical* scaffolding shown inline below
> now lives in the shared **`a2a_common/`** package (canonical at `agents/`,
> vendored into each `app/a2a_common/` by `agents/sync_common.sh`): `model.py`
> (Apigee model + TLS skip), `specialist.py` (`build_specialist` +
> `build_a2a_server_runtime`), `client.py` (streaming `RemoteA2aAgent` + A2A
> auth + loop-binding). Specialists now **stream** their tokens over A2A
> (AgentCard `streaming=True` + an SSE request-converter) — but Vertex Agent
> Engine coalesces the sub-agent stream, so delegated answers still arrive
> message-granular. See [§9](#9-streaming-over-a2a-sub-agents-adk-23). The snippets
> below stay as-is to explain the *mechanics*; for the current file layout see
> §7.

---

## 1. The two roles

| Role | Code | Wrapper class | Publishes / consumes |
|---|---|---|---|
| **Specialist (server)** | `agents/my_{order,product,customer}_agent/` | `A2aAgent` (`vertexai.preview.reasoning_engines`) | **Publishes** an AgentCard + `/a2a/v1/...` endpoint |
| **Supervisor (client)** | `agents/my_supervisor_agent/` | `AdkApp` | **Consumes** specialists via `RemoteA2aAgent` |

Both are ordinary ADK `Agent`s underneath (`App(root_agent=…)`). The difference
is the **runtime wrapper** (`app/agent_runtime_app.py`) each is deployed with.

```
                          Agent Registry
                   {displayName -> resourceName}
                              ▲   │  get_remote_a2a_agent()
                  register    │   ▼
   ┌───────────────────────┐  │  ┌──────────────────────────────────────┐
   │  SPECIALIST  (server) │  │  │  SUPERVISOR  (client)                │
   │  A2aAgent             │  │  │  AdkApp(root_agent)                  │
   │   ├─ AgentCard        │◄─┼──┤   sub_agents = [RemoteA2aAgent, ...] │
   │   └─ A2aAgentExecutor │  │  │   tool: transfer_to_agent            │
   │       └─ Runner       │  │  │                                      │
   │           └─ root_agent (ADK) │      model picks a specialist       │
   │               └─ MCP tools    │      → RemoteA2aAgent.run()         │
   └───────────────────────┘     └──────────┬───────────────────────────┘
            ▲  A2A message:send (HTTP+JSON, Bearer ADC)                  │
            └──────────────────────────────────────────────────────────┘
```

---

## 2. Specialist side — publishing an A2A server

### 2.1 The agent itself (`app/agent.py`)

A plain ADK agent with MCP tools, wrapped in an `App`. Nothing A2A-specific here
— the A2A surface is added by the runtime wrapper.

```python
root_agent = Agent(
    model=apigee_model,                       # ApigeeLlm — model calls via Apigee
    name="order_agent",                       # ← this name is the A2A identity
    description="Specialist agent for customer orders and fulfillment. …",
    instruction="You are a highly specialized Order Management Agent. …",
    tools=order_tools,                        # McpToolset → Apigee MCP gateway
)
app = App(root_agent=root_agent, name="app")
```

The `name` (`order_agent`) and `description` are **load-bearing**: they become
the AgentCard's name/skills, which is what the supervisor's prompt and the
`transfer_to_agent` routing key on.

### 2.2 The A2A wrapper (`app/agent_runtime_app.py`)

This is what makes the engine speak A2A. It subclasses
`vertexai.preview.reasoning_engines.A2aAgent` and builds an **AgentCard** from
the ADK agent:

```python
from vertexai.preview.reasoning_engines import A2aAgent
from google.adk.a2a.executor.a2a_agent_executor import A2aAgentExecutor
from google.adk.a2a.utils.agent_card_builder import AgentCardBuilder

class AgentEngineApp(A2aAgent):
    @staticmethod
    def create(app=None):
        app = app or adk_app

        def create_runner() -> Runner:
            return Runner(
                app=app,
                session_service=InMemorySessionService(),
                artifact_service=InMemoryArtifactService(),
            )

        agent_card = asyncio.run(AgentEngineApp._build_agent_card(app))
        return AgentEngineApp(
            agent_executor_builder=lambda: A2aAgentExecutor(runner=create_runner()),
            agent_card=agent_card,
        )

    @staticmethod
    async def _build_agent_card(app) -> AgentCard:
        builder = AgentCardBuilder(
            agent=app.root_agent,
            capabilities=AgentCapabilities(
                streaming=False,                # message:send only — see note below
                extensions=[AgentExtension(uri=".../a2a-extension/", ...)],
            ),
            rpc_url="http://localhost:9999/",   # placeholder — Agent Engine rewrites it
            agent_version=os.getenv("AGENT_VERSION", "0.1.0"),
        )
        card = await builder.build()
        card.preferred_transport = TransportProtocol.http_json
        card.supports_authenticated_extended_card = True
        return card

agent_runtime = AgentEngineApp.create(app=adk_app)
```

Key points:

- **`AgentCardBuilder`** introspects the ADK `root_agent` and emits an A2A
  `AgentCard` (name, description, skills derived from the agent). This card is
  what a client fetches to learn how to talk to the specialist.
- **`A2aAgentExecutor(runner=…)`** is the server handler: each inbound A2A
  `message:send` is run through a fresh ADK `Runner` over the specialist's
  `root_agent`. Sessions/artifacts are **in-memory** (each A2A call is
  effectively stateless from the specialist's POV; the supervisor owns the
  conversation).
- **`rpc_url="http://localhost:9999/"`** is a deliberate placeholder. Once
  deployed, Agent Engine serves the agent at its reasoning-engine endpoint and
  the real transport URL is the engine's `…/a2a/v1/` path — the client resolves
  that from the engine resource, not from the card's `rpc_url`.
- **`streaming=True`** (capability `message:stream`) as of the ADK 2.3
  migration — specialist tokens now stream back through the supervisor. This
  requires the supervisor to build new-impl (`use_legacy=False`) proxies with a
  streaming `A2AClientFactory`; see [§9](#9-streaming-over-a2a-sub-agents-adk-23). (The
  old `partMetadata` bug that forced `streaming=False` is fixed in ADK 2.3.)
- **`preferred_transport = http_json`** — A2A over HTTP+JSON (not gRPC).

### 2.3 Deploy quirk — the pydantic AgentCard patch (deploy worker)

`A2aAgent` carries an `agent_card` attribute. During
`agent_engines.create(...)`, the SDK serializes the deployed object's attributes
and calls `google.protobuf.json_format.MessageToJson(agent_card)` — but
`agent_card` is an `a2a.types.AgentCard` **pydantic model**, not a protobuf
message, so it crashes with `'AgentCard' object has no attribute 'DESCRIPTOR'`.
The fix monkeypatches `MessageToJson` to delegate to pydantic when handed a
pydantic model:

```python
def _patched(message, *args, **kwargs):
    if hasattr(message, "model_dump_json"):
        return message.model_dump_json()
    return _original(message, *args, **kwargs)
json_format.MessageToJson = _patched
```

This must run **before** `agent_engines.create(...)`. (The supervisor's
the deploy worker keeps the same patch for parity even though `AdkApp` has no
`agent_card`.)

### 2.4 Registration in Agent Registry

`agent_engines.create(...)` **auto-registers** each specialist in **Agent
Registry** under its display name (`order_agent`, `product_agent`,
`customer_agent`), mapped to that engine's `resourceName`. This is the
indirection that lets the supervisor find specialists **by name** instead of by
hardcoded engine id.

Two properties of the registry shaped the resolution logic (§3.1, §3.4):

- **A fresh entry per generation.** Every deploy creates a *new* engine **and a
  new registry entry** (`agentregistry-<uuid>`); existing entries are **not**
  updated in place — Agent Registry v1alpha has **no `PatchAgent`** (a PATCH
  gets a frontend `404`). So after a few redeploys one display name has several
  entries, the older ones still pointing at engines a later `--cleanup` will
  delete. Every reader therefore **picks the newest entry** (by create/update
  time). Never assume an entry is unique or current by id alone.
- **Reconciliation is delete-based.** `deploy_agents.py --sync-registry`
  **prunes** the stale duplicate entries (it deletes — it can't patch), keeping
  the one at the newest live engine; `--list-registry` dumps every entry with
  its engine marked LIVE/DEAD. See OPERATIONS → *Garbage collection*.

---

## 3. Supervisor side — consuming specialists as `RemoteA2aAgent`

### 3.1 Discovery: registry → `RemoteA2aAgent` (`app/agent.py`)

At import (deploy-time warm start), the supervisor lists Agent Registry, filters
to the display names it expects, and turns each into a `RemoteA2aAgent` proxy:

```python
_SPECIALIST_DISPLAY_NAMES = ("order_agent", "product_agent", "customer_agent")

def _resolve_sub_agents() -> list:
    if os.environ.get("SKIP_REGISTRY_DISCOVERY") == "1":
        return []                                   # cloud-side: use pickled set
    registry = AgentRegistry(project_id=project, location=location)

    discovered = {}                                 # {displayName: resourceName}
    for entry in registry.list_agents(page_size=100).get("agents", []):
        discovered[entry["displayName"]] = entry["name"]

    resolved = []
    for display in _SPECIALIST_DISPLAY_NAMES:
        override = os.environ.get(f"{display.upper()}_NAME")   # optional pin
        agent_name = override or discovered.get(display)
        if agent_name:
            resolved.append(registry.get_remote_a2a_agent(agent_name))  # ← proxy
    return resolved
```

- **`registry.get_remote_a2a_agent(resource_name)`** returns an ADK
  `RemoteA2aAgent` whose `_agent_card_source` is the specialist's `AgentCard`
  object and whose transport targets the specialist's engine endpoint.
- **No engine ids are hardcoded** — only display names. Overrides
  (`ORDER_AGENT_NAME=…`) can pin a specific engine when duplicates exist.
- **`SKIP_REGISTRY_DISCOVERY=1`** is set by the deploy worker for the cloud side: the
  unpickled `agent_runtime` already carries the resolved `sub_agents`, and the
  runner SA often lacks `agentregistry.agents.list` (would 403 + flood logs), so
  the cloud re-import short-circuits and trusts the pickled state.

### 3.2 Wiring sub-agents into the model (`root_agent`)

The resolved proxies are passed as `sub_agents`. ADK automatically derives a
`transfer_to_agent` tool and an "agents you can transfer to" instruction from
that list — that's the mechanism the model uses to delegate:

```python
root_agent = Agent(
    model=apigee_model,
    name="root_agent",
    instruction="""You are the Main Coordinator Agent. … delegate …
      - order/tracking/line items   → transfer to `order_agent`.
      - product/catalog/stock       → transfer to `product_agent`.
      - customer/profile/addresses  → transfer to `customer_agent`.
      You may also have additional specialist sub-agents … pick the most
      appropriate agent based on its described capabilities.""",
    sub_agents=_resolve_sub_agents(),               # ← RemoteA2aAgent proxies
    before_agent_callback=_before_agent,            # refresh + loop-bind + ACL
    before_tool_callback=acl.before_tool_acl,       # ACL hard block on transfer
)
```

Because the model only ever **sees** the sub-agents currently in
`agent.sub_agents`, hiding/showing specialists per request (the ACL) is just a
matter of trimming that list — covered in §6.

### 3.3 Authenticating the A2A call (`app/a2a_auth.py`)

A `RemoteA2aAgent` makes a real HTTP call to another reasoning engine, which is
an IAM-protected Vertex endpoint. The call must carry a **Bearer token**. We
attach an `httpx.AsyncClient` whose auth refreshes ADC credentials per request:

```python
class GCPAuth(httpx.Auth):
    def __init__(self):
        self._credentials, _ = google.auth.default()
    def auth_flow(self, request):
        self._credentials.refresh(google.auth.transport.requests.Request())
        request.headers["Authorization"] = f"Bearer {self._credentials.token}"
        yield request

def make_authed_async_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(auth=GCPAuth(), timeout=httpx.Timeout(timeout=120))

def attach_auth_client(agent: RemoteA2aAgent) -> None:
    agent._httpx_client = make_authed_async_client()
    agent._httpx_client_needs_cleanup = True
```

Two hard-won subtleties:

1. **Can't build the client at import time.** `google.auth.Credentials` and
   `httpx.AsyncClient` both hold a `threading.RLock`, which cannot survive the
   `deepcopy` that `AdkApp.clone()` does at deploy. So discovery builds the
   `RemoteA2aAgent`s **without** clients, and the supervisor's
   `agent_runtime_app.py` injects them cloud-side **after** the clone, in
   `set_up()`:

   ```python
   class AgentEngineApp(AdkApp):
       def set_up(self):
           vertexai.init(project=…, location=…)
           super().set_up()
           inject_auth_httpx_clients(self._tmpl_attrs.get("app"))   # warm-start clients
   ```

2. **The client must be bound to the request's event loop.** `httpx.AsyncClient`
   binds its connection pool to the loop of first use. The warm-start client is
   built on the worker-startup loop, but Agent Engine serves each A2A transfer on
   a **per-request loop** — reusing the startup client raises *"got Future
   attached to a different loop"* deep in ADK's node executor. So
   `ensure_loop_bound_clients()` rebinds (one client per `(agent, loop)`) and is
   called **every turn** from `before_agent_callback`, which runs in the request
   loop:

   ```python
   def ensure_loop_bound_clients(root_agent):
       loop = asyncio.get_running_loop()
       for agent in walk_agents(root_agent):
           if isinstance(agent, RemoteA2aAgent) and agent._a2a_client_loop is not loop:
               agent._httpx_client = make_authed_async_client()
               agent._a2a_client_loop = loop
   ```

### 3.4 Runtime refresh — track new specialist engines without redeploying

The supervisor must follow a specialist to its **new engine** whenever that
specialist is redeployed — without redeploying the supervisor. `before_agent_callback`
does this in two cheap pieces: a **throttled network resolve** and a **per-turn
re-point**.

- **Throttled resolve — `_refresh_resolved_cache`.** At most once per
  `_REFRESH_TTL` (default **60s**) it lists Agent Registry, picks the **newest**
  entry per display name (`_discover_agents`), builds a `RemoteA2aAgent` proxy
  for each allowed specialist, and caches them in `_resolved` (mirrored onto the
  module-global `root_agent` so future deep-copies start fresh). One round-trip
  per TTL window; on any error it keeps the existing cache.
- **Per-turn re-point — `_apply_resolved`.** On **every** turn it points this
  request's `sub_agents` at the cached proxies. Cheap: it compares card URLs and
  rebuilds a proxy **only when the registry actually moved**. In steady state it
  does nothing. Since ADK reads transfer targets from `agent.sub_agents` each
  turn, a re-point takes effect on the same turn.

```python
async def _refresh_sub_agents(callback_context):
    agent = callback_context._invocation_context.agent
    if not DISABLE_RUNTIME_SUBAGENT_REFRESH:
        await _refresh_resolved_cache()   # TTL: list(newest) + build -> _resolved
        _apply_resolved(agent)            # EVERY turn: re-point from cache (cheap)
    ensure_loop_bound_clients(agent)      # MUST run every turn (loop-bind clients)
```

**Why the split.** An earlier version re-pointed only on TTL-*refresh* turns, so
a turn that hit the TTL cache kept serving whatever URI was baked or last-healed.
After a specialist redeploy plus a `--cleanup` that deleted the old engine, that
frozen URI was a **dead engine → A2A 403** (recurring, until the next refresh or
a supervisor redeploy). Re-pointing every turn from the cache closes that window:
once the cache resolves the new engine, no turn serves a stale URI.

Best-effort — never raises; on error the supervisor keeps its current set. Gated
by `DISABLE_RUNTIME_SUBAGENT_REFRESH=1` (baked set only); TTL via
`SUBAGENT_REFRESH_TTL_SECONDS`; requires the runner SA to have
`agentregistry.viewer`. **Net effect: redeploy a specialist and the supervisor
follows it within ~60s — no supervisor redeploy, and `--cleanup` is safe.**

---

## 4. End-to-end call flow (one delegated turn)

```
User ──"track order 4711"──► Supervisor engine
  │
  1. before_agent_callback (_before_agent):
  │      • _refresh_sub_agents()  → TTL-cached registry reconcile (add + heal stale URIs)
  │      • ensure_loop_bound_clients() → rebind A2A httpx clients to THIS loop
  │      • acl.before_agent_acl()  → (if ACL on) trim sub_agents to allowed set
  │
  2. Supervisor model call ── via ApigeeLlm ──► Apigee LLM gateway ──► Gemini
  │      model emits:  transfer_to_agent(agent_name="order_agent")
  │
  3. before_tool_callback (acl.before_tool_acl):
  │      • (if ACL on) deny if "order_agent" not in allowed set  → fail-closed
  │
  4. ADK routes the transfer to the RemoteA2aAgent named "order_agent"
  │      • GCPAuth refreshes ADC → Authorization: Bearer <runner-SA token>
  │      • HTTP A2A message:send  ──────────────► Order specialist engine
  │                                                  /a2a/v1/  (http_json)
  │                                                    │
  │                                              5. A2aAgentExecutor → Runner →
  │                                                 order_agent (ADK)
  │                                                    │  model via Apigee LLM
  │                                                    │  tools via Apigee MCP
  │                                                    ▼
  │      ◄──────── A2A response (message) ───────────┘
  │
  6. Supervisor incorporates the specialist's answer, may chain another
  │    transfer (sequential orchestration), then replies to the user.
  ▼
User ◄── final answer
```

**Both model hops and both tool hops traverse Apigee.** The A2A hop (step 4) is
engine→engine over Google's network, authenticated by the shared runner SA's
bearer token — see the trust boundary below.

---

## 5. Auth & trust boundary

- **One service account per engine** (`a2a-<agent>-sa@<project>…`, declared
  in `agents/runtime-manifest.yaml`). The supervisor signs A2A calls with
  **its own** ADC token, and the specialist engines are IAM-locked so only
  the supervisor's SA can invoke them. `roles/aiplatform.user` on the
  supervisor's SA is what authorizes engine→engine calls.
- **No end-user identity crosses A2A.** The specialists never see the IAP `sub`.
  All per-user authorization happens **in the supervisor** (the sole entrypoint)
  via the ACL — see §6. This keeps the specialists simple and the trust boundary
  singular.
- **The model/tool gateways are separate from A2A.** Model calls use `ApigeeLlm`
  + `x-api-key`; tools use `McpToolset` → Apigee MCP. A2A auth is ADC bearer.
  Three independent credentials, three independent surfaces.

---

## 6. Per-user authorization over the A2A surface (ACL)

Because the supervisor is the only entrypoint and sub-agents are IAM-locked to
its SA, **scoping which specialists a user may reach is done entirely in the
supervisor** by controlling its `sub_agents` list — no change to the A2A
mechanics above. Two layers (`app/acl.py`, both no-ops unless `ACL_ENABLED`):

1. **Hide** (`before_agent_acl`) — trims THIS request's `agent.sub_agents` to
   the user's allowed set, so ADK rebuilds `transfer_to_agent` from the reduced
   list and the model never *sees* a disallowed specialist. Safe to mutate
   because ADK deep-copies the per-request agent (`SHARED=False`).
2. **Hard block** (`before_tool_acl`) — denies any `transfer_to_agent` whose
   target isn't allowed (fail-closed safety net for a hallucinated or
   newly-discovered agent).

Allowed set = union of `acl_roles[*].agents` over `acl_users/{email}.roles`
in Firestore, read **per turn** (instant revoke), keyed by the
**signature-verified IAP email** the BFF passes as `user_id`. Fail-closed
everywhere: unknown user / backend error / no doc ⇒ empty set ⇒ no
specialists. Seeded from the manifest `acl:` block (`provision_agents.py`,
merge-only — Admin-view edits survive re-runs); edited live in the BFF's
Admin view. See `ARCHITECTURE.md` (security model).

---

## 7. Files at a glance

| File | Side | Responsibility |
|---|---|---|
| `a2a_common/model.py` | both | Apigee-fronted Gemini model + host-scoped TLS skip (`build_apigee_model`) |
| `a2a_common/specialist.py` | specialist | `build_specialist` + `build_a2a_server_runtime` (AgentCard `streaming=True`, `A2aAgentExecutor` with the SSE request-converter — the A2A **server**) |
| `a2a_common/client.py` | supervisor | streaming `RemoteA2aAgent` build (`use_legacy=False` + streaming factory), `GCPAuth` bearer client, deploy/loop-binding lifecycle |
| `my_{order,product,customer}_agent/app/agent.py` | specialist | `build_specialist(...)` call — behavioral def only (name/desc/instruction/tool_filter) |
| `…/app/agent_runtime_app.py` | specialist | one-liner: `build_a2a_server_runtime(app, streaming=True)` |
| `agents/provision/deploy_agents.py` | all four | central deploy: `agent_engines.create(...)` per agent worker + pydantic-AgentCard `MessageToJson` patch (helpers in `agents/deploy_common.py`) |
| `my_supervisor_agent/app/agent.py` | supervisor | Registry discovery → streaming `RemoteA2aAgent` sub-agents (via `a2a_common.client`), runtime refresh, ACL hooks |
| `…/app/agent_runtime_app.py` | supervisor | `AdkApp` wrapper — injects authed streaming clients in `set_up()` (the A2A **client**) |
| `…/app/acl.py` | supervisor | Per-user authorization over the sub-agent set |
| `agents/sync_common.sh` | both | Vendors `a2a_common/` into each `app/a2a_common/` (run by `deploy_agents.py`) |
| `agents/provision/provision_agents.py` | both | APIs, staging bucket, per-agent runtime SAs + IAM (manifest-driven) |

---

## 8. Common failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `Tool 'order_agent:…' not found. Available tools: (empty)` | Supervisor resolved **zero** sub-agents (no transfer tool) → model hallucinates a transfer | Check registry display names + runner SA `agentregistry.viewer`; deploy-time `WARNING: resolved zero sub-agents` |
| `'AgentCard' object has no attribute 'DESCRIPTOR'` at deploy | SDK serializes the pydantic AgentCard with protobuf `MessageToJson` | The `_patch_messagetojson_for_pydantic()` patch (must run before `create()`) |
| `got Future attached to a different loop` on transfer | A2A httpx client bound to the worker-startup loop, reused on a request loop | `ensure_loop_bound_clients()` every turn (already wired) |
| A2A call returns 401/403 (auth) | RemoteA2aAgent went out without a bearer client, or runner SA lacks `aiplatform.user` | `inject_auth_httpx_clients()` in `set_up()`; check runner SA roles |
| A2A **403 to a `reasoningEngines/<id>` that no longer exists** | Supervisor resolved a **stale** registry entry (duplicate) or a baked URI whose engine was `--cleanup`'d | Resolved automatically now — newest-entry resolution + per-turn re-point (§3.4). Prune leftovers with `deploy_agents.py --sync-registry`; inspect LIVE/DEAD with `--list-registry` |
| New specialist / new engine not picked up | Supervisor still on its baked/cached set | Picked up within `_REFRESH_TTL` (~60s) by the per-turn re-point (§3.4); if not, check runner SA `agentregistry.viewer` and that the entry exists (`--list-registry`) |

---



---

## 9. Streaming over A2A sub-agents (ADK 2.3)

How specialist tokens stream back through the supervisor, why it needs ADK
2.x, and exactly what to validate on deploy.

### What changed and why

Before: specialists published `AgentCapabilities(streaming=False)` (A2A
`message:send` only). A delegated answer came back as one block — the
supervisor's own framing text streamed, but the specialist's answer appeared
all-at-once. The blocker was a `partMetadata` bug on the **legacy** ADK-A2A path.

Now (ADK 2.3): specialists advertise `streaming=True` and the supervisor reaches
them as **new-impl** (`use_legacy=False`) `RemoteA2aAgent` proxies wired to a
**streaming** `A2AClientFactory`. Specialist token deltas stream up through the
supervisor's event stream and out to the browser over the existing SSE path.

ADK 2.0 turned out **not** to be a rewrite for us: `Agent` / `sub_agents` /
`before_*_callback` / `ApigeeLlm` are unchanged, and the `Event` schema only
*added* `output` / `node_info`, so the BFF's hand parsing stays compatible.

### The three traps (all verified against `google-adk==2.3.0`)

Enabling streaming is not a single flag. On the **client** (supervisor) side:

1. **`RemoteA2aAgent` defaults `use_legacy=True`.** The legacy path doesn't
   speak the new ADK-A2A streaming extension. We rebuild with
   `use_legacy=False`, which registers the extension request-interceptor.
2. **`AgentRegistry.get_remote_a2a_agent()` never passes `use_legacy`** and, on
   its synthetic-card fallback, hardcodes `streaming=False`. So we let it do card
   resolution, then **rebuild the proxy ourselves** from the resolved card
   (`a2a_common.client.upgrade_registry_agent`).
3. **The default `A2AClientFactory` is hardcoded `streaming=False`.** We attach a
   factory built with `streaming=True` and the authed, loop-bound httpx client
   (`a2a_common.client.attach_streaming_client`).

On the **server** (specialist) side there are **two** things:

4. **`AgentCapabilities(streaming=True)`** on the card (`A2aAgentExecutor`
   already defaults to the new impl, `use_legacy=False`).
5. **Force the specialist's Runner into SSE.** This one is the trap that the
   first deploy exposed: the supervisor streamed its own text but the specialist
   answer still arrived as one block. Root cause — ADK's default
   `convert_a2a_request_to_agent_run_request` builds a `RunConfig` with **no
   `streaming_mode`** (→ `NONE`), so the specialist's *model* never streams,
   regardless of the A2A transport. Both the legacy and new-impl executors read
   `config.request_converter`, so we override it (`build_a2a_server_runtime` →
   `A2aAgentExecutorConfig(request_converter=...)`) to set
   `streaming_mode=SSE`. A non-streaming caller still gets the final aggregated
   result.

### Where it lives

| Concern | File |
|---|---|
| Specialist card `streaming=True` + A2A server runtime | `a2a_common/specialist.py` (`build_a2a_server_runtime`) |
| Streaming proxy build + auth + loop-binding | `a2a_common/client.py` |
| Supervisor uses streaming proxies (deploy + runtime refresh) | `my_supervisor_agent/app/agent.py` |
| Warm-start client injection | `my_supervisor_agent/app/agent_runtime_app.py` |

`a2a_common/` is the **canonical** copy; `agents/sync_common.sh` vendors it into each
`app/a2a_common/` (deploy bundles only `./app`). Edit the canonical copy only.

### Deploy findings

**First deploy:** supervisor streamed its own tokens, specialist answer arrived
as one block. Root-caused (above, trap #5) to the specialist Runner's
`streaming_mode=NONE` — **not** a platform limitation. Fixed by forcing SSE in
the request converter; **redeploy the 3 specialists** to pick it up (the
supervisor needs no redeploy for this fix).

The design still **degrades cleanly**: if Agent Engine ever doesn't serve SSE
engine→engine, the client falls back to message-level responses — nothing
breaks, you just don't see token-level streaming.

### Deploy-validation checklist

1. Deploy ONE specialist + the supervisor first (not all four) to isolate
   breakage. `agents/provision/deploy_agents.py` runs `agents/sync_common.sh` automatically.
2. Smoke-test a non-delegated supervisor turn (model streams as before).
3. Delegate to the deployed specialist with the BFF "Tokens" toggle on. Watch
   for specialist text arriving as **incremental deltas**, not one block.
4. If it arrives as one block: streaming isn't coming through engine→engine.
   Check Agent Engine logs for `message:stream` vs `message:send`, and confirm
   the specialist's served AgentCard shows `capabilities.streaming=true`.
5. Watch for `got Future attached to a different loop` (loop-binding) and any
   duplicated final event (the legacy `TaskArtifactUpdateEvent` echo — should be
   gone on the new impl; the BFF's partial/consolidated dedup also covers it).

> **Note:** ADK still flags `RemoteA2aAgent` / `A2aAgentExecutor` as
> **experimental** (subject to breaking changes) even in 2.3.

---

*See also: `ARCHITECTURE.md` (system topology + security model),
[`../apigee/proxies/README.md`](../apigee/proxies/README.md) (gateway
contracts), `GETTING_STARTED.md` (bootstrap + IAM).*
