# Product Agent

**Role:** Specialist for product catalog — lookups, stock updates, health.
Called via A2A from the supervisor.

**ADK type:** `Agent` wrapped in `A2aAgent`, both built by the shared
`app.a2a_common` package (`build_specialist` / `build_a2a_server_runtime`).
Registered in Agent Registry under display name `product_agent`. Publishes an A2A
card with `streaming=True`.

## Tools

MCP toolset against `${APIGEE_MCP_URL}/mcp`, filtered to five tools:

| Tool | Purpose |
|---|---|
| `listProducts` | Page through catalog |
| `getProductById` | Single product detail |
| `deleteProductById` | Remove product |
| `updateProductStock` | Adjust stock level |
| `checkProductHealth` | Service health check |

## Tools intentionally excluded

`createProduct` and `updateProductById` are commented out of the
`tool_filter`. Their MCP schemas have ~12 parameters each, which
trips Gemini's tool-call validation with:

```
google.genai.errors.ClientError: 400 INVALID_ARGUMENT
```

If/when those upstream schemas get sanitised, re-add them.

## Files

| File | Purpose |
|---|---|
| `app/agent.py` | `build_specialist(...)` call — behavioral def only |
| `app/agent_runtime_app.py` | one-liner: `build_a2a_server_runtime(app, streaming=True)` |
| `app/a2a_common/` | vendored shared scaffolding (edit canonical `agents/a2a_common/`, then `./agents/sync_common.sh`) |
| *(deploy)* | central tool: `python agents/provision/deploy_agents.py product` (see `agents/provision/README.md`) |
| `pyproject.toml` | Cloud-side deps |

## Env vars consumed

Same as `my_order_agent`. Only the defaults differ:

| Var | Default |
|---|---|
| `DISPLAY_NAME` | `a2a-product-agent` |

## Deploy

No `.env` needed — project, bucket, SA, proxy URLs and the secret name all
default from the manifests ([`../README.md`](../README.md)):

```bash
python agents/provision/deploy_agents.py product
```

A local `.env` (copy `.env.example`) or an inline env var overrides any value
for experiments; permanent changes belong in `agents/runtime-manifest.yaml`.


Then register with Agent Registry under display name `product_agent`.
