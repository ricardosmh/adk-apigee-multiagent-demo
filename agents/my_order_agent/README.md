# Order Agent

**Role:** Specialist for orders, fulfillment, line items, status updates.
Called via A2A from the supervisor.

**ADK type:** `Agent` wrapped in `A2aAgent`, both built by the shared
`app.a2a_common` package (`build_specialist` / `build_a2a_server_runtime`).
Registered in Agent Registry under display name `order_agent`. Publishes an A2A
card with `streaming=True`.

## Tools

MCP toolset against `${APIGEE_MCP_URL}/mcp`, filtered to:

| Tool | Purpose |
|---|---|
| `listOrders` | Page through orders |
| `listOrdersByCustomerId` | Filter by customer |
| `getOrderById` | Single order detail |
| `createOrder` | New order |
| `updateOrderStatus` | Status transitions |
| `checkOrderHealth` | Service health check |

## Files

| File | Purpose |
|---|---|
| `app/agent.py` | `build_specialist(...)` call — behavioral def only (name, tool filter, instruction) |
| `app/agent_runtime_app.py` | one-liner: `build_a2a_server_runtime(app, streaming=True)` |
| `app/a2a_common/` | vendored shared scaffolding (edit canonical `agents/a2a_common/`, then `./agents/sync_common.sh`) |
| *(deploy)* | central tool: `python agents/provision/deploy_agents.py order` (see `agents/provision/README.md`) |
| `pyproject.toml` | Cloud-side deps including `mcp>=1.24,<2.0` and OTEL |

## Env vars consumed

All of these **default from the manifests** (`agents/runtime-manifest.yaml`
`engine_env` + this agent's block); a local `.env` (gitignored) overrides
them for experiments.

| Var | Required | Default | Notes |
|---|---|---|---|
| `PROJECT_ID` | yes | — | |
| `REGION` | no | `us-central1` | |
| `STAGING_BUCKET` | yes | — | |
| `GEMINI_MODEL` | no | `gemini-3.1-flash-lite` | Order specialist's own Gemini |
| `APIGEE_MCP_URL` | yes | — | MCP server URL |
| `APIGEE_API_KEY` | yes | — | this agent's single Apigee key — covers the LLM gateway **and** MCP (sent as `x-api-key`) |
| `APIGEE_MCP_TOKEN` | if MCP needs a bearer | — | optional |
| `AGENT_RUNNER_SA` | yes (prod) | — | |
| `DISPLAY_NAME` | no | `a2a-order-agent` | |
| `DISABLE_DEFAULT_TELEMETRY` | no | unset | `=1` to omit OTEL env vars |

## Deploy

No `.env` needed — project, bucket, SA, proxy URLs and the secret name all
default from the manifests ([`../README.md`](../README.md)):

```bash
python agents/provision/deploy_agents.py order
```

A local `.env` (copy `.env.example`) or an inline env var overrides any value
for experiments (e.g. `GEMINI_MODEL=gemini-3.1-pro-preview python
agents/provision/deploy_agents.py order`); permanent changes belong in
`agents/runtime-manifest.yaml`.

Registration with Agent Registry happens automatically at deploy, under
display name `order_agent` (the exact key the supervisor looks for);
`deploy_agents.py --check` verifies the registry row.

## Verification

```bash
TOKEN=$(gcloud auth print-access-token)
RID=projects/<project-number>/locations/us-central1/reasoningEngines/<id>   # id from `deploy_agents.py --check order`
curl -s -o /dev/null -w "%{http_code}\n" \
  "https://us-central1-aiplatform.googleapis.com/v1beta1/${RID}/a2a/v1/card" \
  -H "Authorization: Bearer ${TOKEN}"
# Expect: 200
```

If 403: the engine is running as the GEAP Service Agent, not the
user-managed runner SA. Confirm `AGENT_RUNNER_SA` in this agent's `.env`
(the deploy preflight checks it against `runtime-manifest.yaml`).
