# Operations

Post-deploy: how to invoke, where to find what when something goes
sideways, and what to do for routine maintenance.

## Invoke the supervisor

### From the GCP console

1. Gemini Enterprise Agent Platform → Agent Builder → Reasoning Engines → `a2a-supervisor-agent`
2. Click **Test** and send a message.

### From the CLI

```bash
TOKEN=$(gcloud auth print-access-token)
REGION=<regions.ai from demo-environment.yaml>
SUP_RID=projects/<project-number>/locations/${REGION}/reasoningEngines/<id>   # id: `python agents/provision/deploy_agents.py --check supervisor`

curl -X POST \
  "https://${REGION}-aiplatform.googleapis.com/v1beta1/${SUP_RID}:streamQuery" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"input": {"message": "Show me 3 active customers"}}'
```

### Smoke test (one per specialist)

```bash
# Order
"What's the status of order ORD-001?"
# Product
"List all products in the 'electronics' category"
# Customer
"Find customer with email john@example.com"
# Cross-domain
"Show me the orders for customer CUST-042 along with the products they bought"
```

## Add a user

The deployer (`identity.admin_email` in `demo-environment.yaml`) gets all
three of these automatically. Each **additional** person needs 1 — and
optionally 2 and/or 3 — depending on what they should be able to do:

1. **Through IAP (required for everyone)** — add them to
   `agents/runtime-manifest.yaml` → `bff.iap_members`
   (`- user:teammate@example.com`), then re-run
   `python frontend/deploy_frontend.py` (a config-only apply re-wires the
   IAP grants).
2. **Agent view (per-user agent ACL)** — grant them agents in the **BFF
   Admin view** (preferred: the provisioner merges the seed additively, so
   UI-made changes survive re-runs). Only add them to the manifest's
   `acl.users` seed if they should be baked into every fresh replication.
3. **Direct view (their own API key + token quota)** — add their email to
   `apigee/provision/manifest.yaml` → `endUsers.emails`, run
   `python apigee/provision/provision.py users`, and hand over the key it
   prints (**once**) out of band. The key is bound to their IAP email
   (`owner_email`) — it 403s for anyone else.

Revoke in reverse: remove the ACL grant in the Admin view (next turn it's
gone — the supervisor reads the ACL per turn); drop the `iap_members` entry
and re-run the frontend apply. Their Direct-view key must be deleted by
hand (Apigee console → the `llm-user-<email-slug>` app) — `provision.py
users` only reconciles emails **listed** in the manifest; removing an email
just stops managing it, it doesn't delete the live app.

## Where to look when something fails

| Failure surface | Where to look |
|---|---|
| Supervisor returns hallucinated text instead of delegating | Cloud Logging filter `resource.labels.reasoning_engine_id="<sup-id>"` — look for "Resolved ZERO sub-agents" or 403 from registry. |
| Specialist returns nothing | Cloud Logging on the specialist engine. Check for MCP errors (`Apigee` 4xx/5xx) and tool-validation errors. |
| Long latency (>30s per turn) | Cloud Trace → search service `a2a-supervisor-agent`. Look for the longest `httpx.client_request` or `google_genai.generate_content` span. |
| 401 on `/a2a/v1/message:send` | The auth-httpx-client injection didn't fire. See `_inject_auth_httpx_clients` in `agent_runtime_app.py`. |
| Frontend chat gets `upstream 400 messages: Field required` | Apigee proxy forwarding a Gemini-shaped body to an Anthropic target — policy gating by publisher. See [`../apigee/proxies/README.md`](../apigee/proxies/README.md). |
| Agent tool calls fail / e-commerce data missing | One trace-id filter across the four logs ([`OBSERVABILITY.md`](OBSERVABILITY.md)) shows which hop broke: `apigee-ai-logs` (MCP/ecommerce proxy status+fault) then `services-ai-logs` (service status+latency). |
| Direct view 429 / agent turns suddenly failing mid-demo | LLM token quota tripped — `apigee-ai-logs` entries carry the live counters (`jsonPayload.quota.*`); filter `jsonPayload.quota.exceeded!="0"`. Per-user 10k/5min, per-agent 100k/hour (apigee manifest). |

## Telemetry toggles (per engine)

Set at deploy time via env vars (default-on; opt out with
`DISABLE_DEFAULT_TELEMETRY=1`). Can also be flipped post-deploy from the
console:

1. Gemini Enterprise Agent Platform → Reasoning Engines → `<engine>` → **Observability** tab
2. Toggle **"Enable instrumentation of OpenTelemetry traces and logs"**
3. Toggle **"Enable logging of prompt inputs and response outputs"**

Caveat: the legacy `enable_tracing=False` on the AdkApp constructor
overrides the toggle. We pass `enable_tracing=True`, so the console
switch is authoritative.

Full env-var reference: [`OBSERVABILITY.md`](OBSERVABILITY.md).

## Redeploy flow

### Single agent

```bash
python agents/provision/deploy_agents.py <order|product|customer|supervisor>
```

After redeploying a specialist, Agent Registry gets a fresh entry AUTOMATICALLY
(GEAP auto-registration on `agent_engines.create`), and **the supervisor
follows the new engine within ~60s on its own** — it re-points its sub-agents
against the registry every turn (A2A_INTEGRATION §3.4). So there is normally
**no manual step and no supervisor redeploy** after a specialist rollout. Verify
with `deploy_agents.py --check` (registry rows) or `--list-registry` (each
entry's engine marked LIVE/DEAD).

After redeploying the **supervisor**, re-apply the BFF config
(`python frontend/deploy_frontend.py --build`) — its `AGENT_ENGINE_ID` token
resolves to the newest supervisor engine automatically.

Clean up old generations any time with `deploy_agents.py --cleanup`. Because the
supervisor always resolves the newest **live** engine, deleting old generations
is **safe** — it will not strand the supervisor on a deleted id.

### All four (parallel)

```bash
python agents/provision/deploy_agents.py --all        # infra first-time? run provision_agents.py --apply
```

### Deployment status

```bash
python agents/provision/deploy_agents.py --check      # engines vs runtime-manifest.yaml, stale generations, SAs
```

## Pin a specific specialist engine

Default behaviour: the supervisor's deploy resolves whichever engine is
registered under each display name. To pin a specific resource:

```bash
ORDER_AGENT_NAME="projects/<num>/locations/us-central1/agents/agentregistry-<uuid>" \
PROJECT_ID="${PROJECT_ID}" REGION="${REGION}" \
... \
python agents/provision/deploy_agents.py supervisor
```

The override is honoured per specialist:
`ORDER_AGENT_NAME`, `PRODUCT_AGENT_NAME`, `CUSTOMER_AGENT_NAME`.

## Garbage collection

Every deploy creates a fresh generation, so two kinds of leftovers accumulate —
old **engines** and old **registry entries**:

```bash
python agents/provision/deploy_agents.py --list-registry   # every entry + engine LIVE/DEAD
python agents/provision/deploy_agents.py --cleanup         # delete STALE engine generations
python agents/provision/deploy_agents.py --sync-registry   # prune STALE duplicate registry entries
```

- **`--cleanup`** deletes all but the newest engine per display name (confirms
  first). It never touches the newest, and since the supervisor resolves the
  newest live engine every turn (A2A_INTEGRATION §3.4), cleanup is **safe**.
- **`--sync-registry`** *prunes* duplicates — it **deletes** the entries not
  pointing at the newest live engine. (Agent Registry v1alpha has **no
  `PatchAgent`**, so there is nothing to "repair" in place — a PATCH 404s.) If
  **no** entry points at a live engine it warns instead of deleting — redeploy
  that agent to re-register a fresh entry.
- **`--list-registry`** cross-references live engines and marks each entry's
  engine `✓LIVE` / `✗DEAD` — the fastest way to spot a stale entry.

(Manual `gcloud ai reasoning-engines delete` works too, but then the newest
registry entry may point at the id you deleted — `--list-registry` shows it
`✗DEAD`, and redeploying that agent re-registers a fresh entry.)

## Cost monitoring

Budget alerts on:
- **Gemini Enterprise Agent Platform Reasoning Engine** SKUs (compute + invocation)
- **Cloud Logging** ingestion (full prompt content can balloon)
- **Gemini Enterprise Agent Platform Generative AI** (Gemini + Anthropic token usage)

If logging cost gets ugly, set `DISABLE_DEFAULT_TELEMETRY=1` and
redeploy: it removes the env vars that enable content capture but keeps
trace/log structure.
