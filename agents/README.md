# agents/ — the four A2A agents

The AI side of the demo: a **supervisor** that delegates over the A2A
protocol to three **specialists** (orders, products, customers), all running
as separate Vertex AI Agent Engine deployments in the AI project. Every model
call goes through the Apigee LLM gateway; every tool call through the Apigee
MCP gateway ([docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md)).

## Layout

| Path | What it is |
|---|---|
| [`runtime-manifest.yaml`](runtime-manifest.yaml) | Desired state for this side: the four agents (dir, SA, secret, env), shared `engine_env`, the BFF identity + IAP members, the Firestore ACL seed, AI-project infra (network, PSC toward Apigee, bucket) |
| [`provision/`](provision/README.md) | The two tools: `provision_agents.py` (project infra + identities + ACL) and `deploy_agents.py` (engine deploys, checks, cleanup, registry sync) |
| [`my_supervisor_agent/`](my_supervisor_agent/README.md) | The coordinator: registry-driven sub-agent discovery, per-user ACL, A2A auth |
| [`my_order_agent/`](my_order_agent/README.md) [`my_product_agent/`](my_product_agent/README.md) [`my_customer_agent/`](my_customer_agent/README.md) | The specialists: one domain, one engine, one Apigee key, one MCP tool subset each |
| `a2a_common/` | Shared library (streaming converters, traceparent propagation, user attribution, logging setup). **Vendored** into each agent's `app/` at deploy time by `sync_common.sh` — edit only `a2a_common/`, the copies are generated |
| `deploy_common.py` | Shared deploy-time helpers: PSC interface config, telemetry env vars, secret refs, identity preflight |

## Working on agents

```bash
python agents/provision/deploy_agents.py --check          # live engines vs manifest
python agents/provision/deploy_agents.py order            # redeploy one agent
python agents/provision/deploy_agents.py --cleanup        # delete stale generations
```

No per-agent `.env` is required — project, bucket, SA, PSC plumbing, proxy
URLs and secret names all default from the manifests. A local `.env`
(gitignored) is purely an **override** for experiments; each agent's
`.env.example` documents the useful knobs. Permanent changes belong in
`runtime-manifest.yaml` (`engine_env` / the agent's `env:` block), not in
`.env` files.

Deep dives: [docs/A2A_INTEGRATION.md](../docs/A2A_INTEGRATION.md) (protocol,
discovery, auth, ACL, sub-agent streaming),
[docs/OBSERVABILITY.md](../docs/OBSERVABILITY.md) (what the engines log and
trace).
