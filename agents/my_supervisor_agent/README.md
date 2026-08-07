# Supervisor Agent

**Role:** Orchestrator. Delegates user queries to one of three domain
specialists (`order_agent`, `product_agent`, `customer_agent`) over A2A.

**ADK type:** `Agent` wrapped in `AdkApp` (subclassed as `AgentEngineApp`
for cloud-side setup).

## How it works

1. At deploy time, `_resolve_sub_agents()` calls Agent Registry's
   `list_agents()` and looks up the three specialist display names
   (`order_agent`, `product_agent`, `customer_agent`). For each match,
   it builds a `RemoteA2aAgent` via `registry.get_remote_a2a_agent(name)`.
2. The resolved `RemoteA2aAgent` objects are passed to `Agent(sub_agents=[...])`,
   and ADK auto-adds the `transfer_to_agent` tool.
3. The whole `agent_runtime` is pickled and shipped to Gemini Enterprise Agent Platform (GEAP).
4. On cloud boot, `AgentEngineApp.set_up()` runs:
   - `vertexai.init(project, location)` — explicit, so we don't depend
     on ADC default project.
   - `super().set_up()` — AdkApp deepcopies the app.
   - `_inject_auth_httpx_clients(cloud_app)` — attaches an authenticated
     `httpx.AsyncClient(auth=_GCPAuth())` to every `RemoteA2aAgent` so
     A2A calls carry an ADC Bearer token.
5. The deploy-time set is a **warm start**, not the final word. On every
   request the supervisor's `before_agent_callback` (`_refresh_sub_agents`)
   **re-points** the live agent's `sub_agents` at the freshest registered
   engines, in two cheap pieces: a THROTTLED registry list
   (`_refresh_resolved_cache`, `SUBAGENT_REFRESH_TTL_SECONDS`, default **60s**)
   that caches the **newest** entry per display name, and a per-turn re-point
   (`_apply_resolved`) that only rebuilds a proxy when the registry actually
   moved. Earlier this re-pointed only on TTL-refresh turns, so a TTL-cache-hit
   turn kept a stale/baked URI — after a specialist redeploy + `--cleanup` that
   was a dead engine → A2A 403. Re-pointing every turn closes that window: a
   redeployed specialist is followed within ~60s **with no supervisor redeploy**,
   and `--cleanup` is safe. Never removes, never raises (keeps the current set on
   any registry error), skips the supervisor itself; `_discover_agents` picks the
   **newest** entry when a display name has duplicates (GEAP auto-registers one
   per generation). Requires the runner SA to have `agentregistry.agents.list/get`
   (declared in `agents/runtime-manifest.yaml`). Set `DISABLE_RUNTIME_SUBAGENT_REFRESH=1`
   to rely only on the baked set.

   `SKIP_REGISTRY_DISCOVERY=1` still suppresses the redundant *import-time*
   re-resolution in the cloud (the warm start comes from the pickle); it does
   not affect the runtime refresh.

## Files

| File | Purpose |
|---|---|
| `app/agent.py` | Root agent, deploy-time + runtime sub-agent resolution, instruction |
| `app/a2a_auth.py` | `GCPAuth` + helpers that attach authed A2A clients to `RemoteA2aAgent`s |
| `app/agent_runtime_app.py` | `AgentEngineApp(AdkApp)` set_up: `vertexai.init` + warm-start client injection |
| *(deploy)* | central tool: `python agents/provision/deploy_agents.py supervisor` (see `agents/provision/README.md`) |
| `pyproject.toml` | Cloud-side deps (declares `mcp`, OTEL instrumentation) |

## Env vars consumed

All of these **default from the manifests** (`agents/runtime-manifest.yaml`
`engine_env` + this agent's block); a local `.env` (gitignored) overrides
them for experiments.

| Var | Required | Default | Notes |
|---|---|---|---|
| `PROJECT_ID` | yes | — | Used for vertexai.init and SA bindings |
| `REGION` | no | `us-central1` | Also sets `GOOGLE_CLOUD_LOCATION` |
| `STAGING_BUCKET` | yes | — | GEAP pulls deploy pickle from here |
| `GEMINI_MODEL` | no | `gemini-3.1-flash-lite` | Used by the supervisor for routing decisions and synthesis |
| `AGENT_RUNNER_SA` | yes (prod) | — | User-managed SA to run the engine |
| `DISPLAY_NAME` | no | `a2a-supervisor-agent` | GEAP resource display name |
| `ORDER_AGENT_NAME`, `PRODUCT_AGENT_NAME`, `CUSTOMER_AGENT_NAME` | no | — | Pin a specific registry resource (skip discovery) |
| `SUBAGENT_REFRESH_TTL_SECONDS` | no | `300` | Runtime registry refresh cadence (per worker process) |
| `DISABLE_RUNTIME_SUBAGENT_REFRESH` | no | unset | `=1` to disable runtime refresh; use only the deploy-time set |
| `DISABLE_DEFAULT_TELEMETRY` | no | unset | `=1` to omit OTEL env vars on deploy |

> Runtime refresh requires the runner SA to have `agentregistry.agents.list/get`
> (granted by `agents/provision/provision_agents.py`). Without it, the refresh logs a warning and
> the supervisor falls back to its deploy-time set.

## Deploy

No `.env` needed — project, bucket, SA, proxy URLs and the secret name all
default from the manifests ([`../README.md`](../README.md)):

```bash
python agents/provision/deploy_agents.py supervisor
```

A local `.env` (copy `.env.example`) or an inline env var overrides any value
for experiments (e.g. `GEMINI_MODEL=gemini-3.1-pro-preview python
agents/provision/deploy_agents.py supervisor`); permanent changes belong in
`agents/runtime-manifest.yaml`.


On success, prints (the deploy-time set is a warm start):

```
  Resolved 3 sub-agent(s) at deploy time: ['order_agent', 'product_agent', 'customer_agent']
Deploying a2a-supervisor-agent to <ai-project>/us-central1...
  Running as service account: a2a-supervisor-sa@...
Deployed: projects/.../reasoningEngines/...
```

If zero specialists resolve locally, the deploy **warns and continues**
(it no longer hard-fails): the supervisor backfills its sub-agents from Agent
Registry at runtime on the first request. After that request, look for the
`Runtime sub-agent refresh: added [...]` log line to confirm discovery.

## Why a separate `AgentEngineApp` subclass?

We override `set_up()` to:
- Pass explicit project/location to `vertexai.init` (don't trust ADC
  default — it's the bug that caused "ZERO sub-agents" deploys earlier).
- Inject the authenticated httpx client into each RemoteA2aAgent *after*
  `super().set_up()`'s deepcopy. Constructing the client at module import
  would break the deepcopy because `httpx.AsyncClient` holds an
  `RLock`.

See [`docs/ARCHITECTURE.md`](../../docs/ARCHITECTURE.md) for the full topology.
