# Proxy catalog — the 7 API proxies

Extracted source bundles (`<name>/apiproxy/…`), version-controlled and
deployed by [`../deploy_proxies.sh`](../README.md). Project ids never appear
in these files — `__AI_PROJECT__`-style tokens are rendered at deploy time
from [`demo-environment.yaml`](../../demo-environment.yaml)
(see [docs/TOOLING.md](../../docs/TOOLING.md)).

Architecture context (who calls what, with which credential):
[docs/ARCHITECTURE.md](../../docs/ARCHITECTURE.md).

## The catalog

| Proxy | Base path | Fronts | Caller | Deploy SA |
|---|---|---|---|---|
| `supervisor-agent-endpoint` | `/ai-agents` | The supervisor engine: `/agents/{id}/sessions*` + `/agents/{id}/query\|streamQuery` rewritten to the full `reasoningEngines/…` resource | BFF (app key `agents-bff`) | AI deploy SA (`meta.deploy_sa`) |
| `gemini-llm-apiproxy` | `/aiplatform/v1beta` | GEAP model calls from the **agents** (the google-genai SDK appends `/models/<model>:streamGenerateContent`) | each agent (its own key) | AI deploy SA |
| `unified-llm-endpoint-sse-apiproxy` | `/llm-stream` | Model calls from the **Direct view**: `POST /prompt` (SSE), routed to Gemini or Claude targets by model name; enforces the key↔IAP-email binding (`owner_email`) and the per-user LLM token quota | end users (personal keys) | AI deploy SA |
| `mcp-server-apiproxy` | `/mcp` | The MCP tool server (StreamableHTTP) | specialists (their keys) | none (no Google token minted) |
| `ecommerce-customer-management-apiproxy` | `/customer-management` | customers-service through the backend ILB | MCP server | ecommerce invoker SA (`meta.ecommerce_deploy_sa`) |
| `ecommerce-order-management-apiproxy` | `/order-management` | orders-service | MCP server | ecommerce invoker SA |
| `ecommerce-product-management-apiproxy` | `/product-management` | products-service | MCP server | ecommerce invoker SA |

Deploy-SA selection is automatic (`deploy_proxies.sh` greps each bundle for
`GoogleAccessToken`/`GoogleIDToken`/`MessageLogging`); the ecommerce trio
defaults to the dedicated invoker declared in
[`../provision/manifest.yaml`](../provision/manifest.yaml).

## Shared policy patterns

- **`VerifyAPIKey`** on every consumer-facing surface — the caller's identity
  is its Apigee app; products attach quotas and analytics to it.
- **`ML-interaction`** (MessageLogging → `apigee-ai-logs` in the AI project)
  on **all seven** proxies, attached in `PostClientFlow` so logging is never
  in the latency path. Field reference:
  [docs/OBSERVABILITY.md](../../docs/OBSERVABILITY.md). The LLM proxies add
  token counts + live quota counters; entries carry the inbound
  `traceparent`.
- **`LLMTokenQuota`** (`LTQ-enforce` + response-side counting) on the two LLM
  proxies — native token budgets from the products' `llmQuota` fields
  (per-agent 100k/hour on `agents-models`; per-user 10k/5min on the unified
  product), enforced per consumer key.
- **`GoogleAccessToken` / `GoogleIDToken`** in the targets — Apigee
  authenticates upstream as the proxy's deploy SA; callers never hold Google
  credentials. The ecommerce targets' ID-token audiences are the **stable
  custom audiences** (`https://<svc>.ecommerce.internal`) so bundles never
  change per project.
- **Targets** use TargetServers (`ts-aiplatform-api*`,
  `ts-ecommerce-services`) created by `provision.py apigee` — the ecommerce
  one resolves its host from the PSC endpoint attachment at provisioning
  time.
- **SSE**: the streaming surfaces set `response.streaming.enabled`; that's
  also why the gateway never buffers (and never logs) response bodies.
- `gemini-llm`'s `JS-strip-part-metadata` removes the `partMetadata` field
  ADK attaches to request parts (GEAP rejects unknown fields).

## Editing a proxy

1. Edit the XML under `<name>/apiproxy/` (never rendered copies; keep
   `__TOKENS__` intact — a guard test rejects literal project ids).
2. `./apigee/deploy_proxies.sh <name>` imports a new revision and deploys it
   (re-attaching the SA — Apigee X drops it on override).
3. `./apigee/deploy_proxies.sh --check` shows deployed revisions per proxy.

If you change a policy that the provisioning manifest also describes
(products, quota numbers, target servers), update
[`../provision/manifest.yaml`](../provision/manifest.yaml) too — the
consistency tests cross-check several of these couplings.
