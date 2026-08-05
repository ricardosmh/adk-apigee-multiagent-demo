# Getting started — three fresh projects → working demo

This is the **one** end-to-end bootstrap guide. It takes you from three bare
GCP projects to the full demo: a browser UI (behind IAP) talking to a
supervisor agent that delegates to three specialists, every LLM/tool/service
call flowing through Apigee, an e-commerce backend on Cloud Run + Cloud SQL,
and one trace-id filter showing a whole turn across four named logs.

Canonical neighbors: [ARCHITECTURE.md](ARCHITECTURE.md) explains *what* you're
building; [TOOLING.md](TOOLING.md) explains *how* the automation works;
per-tool flags live in each tool's README (linked at every step). This file
owns the **order of operations** — no other doc repeats it.

## Who this is for

A developer with **three GCP projects**:

| Project | State you start with | Becomes |
|---|---|---|
| **apigee** | A working **Apigee X org** (this is a prerequisite — see below) | The AI + API gateway: 7 proxies, products, per-agent/per-user API keys, quotas |
| **ai** | Empty | Vertex AI Agent Engine (4 agents), the BFF on Cloud Run behind IAP, Firestore ACL, Secret Manager, ALL telemetry (logs + traces) |
| **backend** | Empty | 3 FastAPI microservices on Cloud Run + Cloud SQL MySQL, behind an internal ALB, exposed to Apigee over PSC |

**Definition of done**: the Phase E smoke test passes — both UI views answer,
an agent turn fetches live e-commerce data, and the turn appears end-to-end in
the AI project's Logs Explorer.

## Prerequisites

### 1. The Apigee project (the one thing that must pre-exist)

The automation manages everything *inside* the org (target servers, products,
apps, deployments) but does **not** create the org itself. You need an
**Apigee X org with one environment attached to an instance** — a fresh
pay-as-you-go or eval provisioning is fine.

Starting from a completely empty project? The companion
[apigeex-terraform-deployment](https://github.com/ricardosmh/apigeex-terraform-deployment)
repo is self-contained Terraform that builds exactly this day-0 layer — VPC +
peering ranges, org, environment, environment group, runtime instance, private
PSC exposure — from nothing but a project id; its defaults match this demo's
manifests (environment/hostname/region) out of the box.

> **Billing type:** prefer a **pay-as-you-go** org. The demo's MCP tools ride
> on Apigee's **managed MCP server**, whose backend provisions asynchronously
> after the first `/mcp` proxy deploy — on a PAYG INTERMEDIATE org this
> completed routinely, while on an EVALUATION org (same region) it stayed
> stuck `PROGRESSING` for hours and every tool call failed with
> `NoResolvedHost: mcp.apigee.internal` (troubleshooting table has the
> symptom). Everything else in the demo works on EVALUATION.
>
> **Environment type** (newer pay-as-you-go orgs): the environment must be
> **INTERMEDIATE or above, with API Analytics** — the demo's data collectors
> (`dc_*`) and `DataCapture` policies ride on Analytics, which BASE lacks.
> The type is updatable in place
> (`PATCH …/environments/<env>?updateMask=type`); legacy orgs predate types
> and have everything. COMPREHENSIVE is **not** required: its extra
> (distributed tracing → gateway spans in the Cloud Trace console) is
> optional garnish the demo doesn't use — the Trace Explorer and waterfall
> are log-based. To add it anyway, uncomment `traceConfig` + the
> `cloudtrace.agent` grant in `apigee/provision/manifest.yaml`.

Collect these four values from your Apigee project — the table says where
each one goes:

| What you need | Where to find it | Where it goes |
|---|---|---|
| **Org name** (Apigee X: same as the project id) | Apigee console header / `gcloud config get-value project` | `demo-environment.yaml` → `projects.apigee` |
| **Environment name** | Apigee console → Admin → Environments | `demo-environment.yaml` → `apigee_env` |
| **Instance region** (where the runtime lives) | Apigee console → Admin → Instances → Location | `demo-environment.yaml` → `regions.gateway` |
| **Runtime hostname** for the env group (used for smoke curls / external testing) | Admin → Environments → Groups | nowhere in the repo — the demo reaches Apigee *privately* as `internal.apigee.com` (provisioned for you); keep the public hostname handy for curl tests |

> The private path (`internal.apigee.com` over PSC) is created **by the
> tools** — you do not configure DNS or endpoints by hand. See
> [ARCHITECTURE.md — Networking](ARCHITECTURE.md#networking).

### 2. Your own access

- **Apigee org admin** (`roles/apigee.admin`) on the apigee project.
- **Owner** (or equivalently broad) on the **ai** and **backend** projects —
  the tools create networks, SQL instances, IAM bindings, secrets, Firestore.
- **Billing linked** on all three projects.
- Note: provisioning the backend also writes to the *apigee org* (the
  endpoint attachment lives there), so one principal with all three is the
  simple setup.

### 3. Console-only steps (the complete list — everything else is scripted)

1. **OAuth consent screen** in the **ai** project (needed by IAP for the BFF).
   One-time; the IAP IAM wiring itself is automated. What you do depends on
   whether the project belongs to a Google Cloud **organization**:
   - **Org-owned project** (Workspace / Cloud Identity): Console → APIs &
     Services → OAuth consent screen → User type **Internal**, any app name.
     Done — IAP then uses a Google-managed OAuth client automatically.
   - **No organization** (personal-account project): Internal isn't offered
     and IAP's Google-managed client is **unavailable** — without extra setup
     the BFF answers **502 "Empty Google Account OAuth client ID(s)/
     secret(s)"** to every request. You need a custom client (all console,
     ~3 min): consent screen → **External** + add every demo user (incl.
     yourself) under **Test users**; then Credentials → Create credentials →
     **OAuth client ID** → Web application; then add its redirect URI
     `https://iap.googleapis.com/v1/oauth/clientIds/<CLIENT_ID>:handleRedirect`
     (the id it just generated); finally point IAP at it via the settings API
     (`iap web enable` does NOT support Cloud Run — this does, validated live):

     ```bash
     cat > /tmp/iap_settings.yaml <<EOF
     accessSettings:
       oauthSettings:
         clientId: <CLIENT_ID>
         clientSecret: <CLIENT_SECRET>
     EOF
     gcloud iap settings set /tmp/iap_settings.yaml --project=<projects.ai> \
       --resource-type=cloud-run --region=<regions.gateway> --service=agent-bff \
       && rm /tmp/iap_settings.yaml
     ```

     Run it AFTER Phase D has created the `agent-bff` service; ~30 s later the
     BFF answers 302 (login) instead of 502.
2. **Claude models** in the **ai** project (Model Garden partner terms have no
   public API): Vertex AI → Model Garden → **Claude Haiku 4.5** → Enable, and
   **Claude Sonnet 4.6** → Enable. Gemini needs nothing.

### 4. Local toolchain

```bash
gcloud auth login && gcloud auth application-default login
# BOTH logins matter: most tools use the gcloud credential, but the engine
# deploys (Phase C) use Application Default Credentials — a stale ADC from an
# earlier login fails there first (Troubleshooting has the symptom).
python3 -m venv .venv && source .venv/bin/activate
pip install -r agents/provision/requirements-deploy.txt   # pinned google-adk + deploy deps
pip install -r apigee/provision/requirements.txt
# deploy_proxies.sh additionally needs: curl, jq, zip
```

## Step 0 — Name your projects (the only file you edit)

```bash
$EDITOR demo-environment.yaml
```

The file ships with placeholders — replace **all six** values with yours:

| Key | Set it to |
|---|---|
| `projects.apigee` | The Apigee project's id (Apigee X: org name == project id) |
| `projects.ai` | The (empty) AI project's id — agents, BFF, telemetry |
| `projects.backend` | The (empty) backend project's id — services + Cloud SQL |
| `apigee_env` | Your Apigee environment name (prerequisites table above) |
| `regions.*` | Your regions — `regions.gateway` must match the Apigee instance's location |
| `identity.admin_email` | **The Google account you'll sign in with** — becomes the IAP member, the ACL admin, the Apigee developer, and your Direct-view key. Get this wrong and IAP locks you out of your own BFF |

`demo-environment.yaml` is the **single source of truth** for project ids,
regions and your identity. Every manifest references them as
`${projects.ai}`-style tokens, every tool resolves them at load time
(`deploy_proxies.sh` included — org, env and deploy SAs all default from this
file + the apigee manifest), and guard tests fail the suite if a project id or
personal email ever appears anywhere else. Details: [TOOLING.md](TOOLING.md).

All `.env` files are **optional overrides** — the manifests carry the config;
you create one only to experiment. See
[agents/provision/README.md](../agents/provision/README.md) and
[apigee/.env.example](../apigee/.env.example).

## Phase A — Backend project (services + database)

Tool reference: [services/provision/README.md](../services/provision/README.md).

```bash
python services/provision/provision_services.py        # report → confirm → apply
python services/provision/provision_services.py --check
```

Creates: APIs, VPC + subnets, the runtime/build SAs (plus the **invoker SA in
the apigee project** — created early so the service deploys below can grant
it), Cloud SQL MySQL
(**PSC-only + IAM auth — both set at create; the instance takes ~10–15 min**),
its pinned PSC endpoint, the internal ALB chain, and the PSC service
attachment + Apigee **endpoint attachment** (the tenant-project accept dance
is automated). The apply loop converges internally; re-run until `--check` is
all green.

```bash
python services/provision/provision_services.py seed   # ⚠ DESTRUCTIVE RESET
```

Imports the schema, the sample data (100 customers / 100 products / 59
orders), and the generated GRANTs for the IAM DB user. Safe on a fresh
project; on a running demo it **resets the data** — it asks first.

```bash
python services/provision/deploy_services.py --all     # 3 Cloud Run --source builds
python services/provision/deploy_services.py --check
```

Services deploy **IAM-only** (no public access; Apigee's invoker SA is the
only caller) with stable custom audiences, so the proxy bundles never change
per project.

## Phase B — Gateway (Apigee org)

Tool references: [apigee/provision/README.md](../apigee/provision/README.md),
[apigee/README.md](../apigee/README.md).

```bash
python apigee/provision/provision.py apigee --check    # see the full plan first
python apigee/provision/provision.py apigee            # confirm → apply
```

Reconciles: target servers (including `ts-ecommerce-services`, whose host is
resolved at run time from the Phase A endpoint attachment), API products with
**token quotas** (per-agent 100k/hour; per-end-user 10k/5min), developer
apps (one key per agent + the BFF app), the two deploy SAs, and the
cross-project telemetry/model grants onto the AI project. (The optional
Apigee→Cloud Trace exporter ships commented out — prerequisites §1.)

```bash
./apigee/deploy_proxies.sh --all
```

Imports + deploys the 7 proxies ([catalog](../apigee/proxies/README.md)),
rendering the `__AI_PROJECT__`-style tokens from `demo-environment.yaml` into
a temp copy first — the repo's bundles stay project-free.

## Phase C — AI project (agents)

Tool reference: [agents/provision/README.md](../agents/provision/README.md).

```bash
python agents/provision/provision_agents.py            # fresh project: run, apply,
python agents/provision/provision_agents.py --check    # re-run until green (~2 passes)
```

Creates: APIs, `ai-vpc` + subnets, the engines' **network attachment**, the
**PSC endpoint + private DNS toward Apigee** (`internal.apigee.com` — includes
patching the Apigee instance's `consumerAcceptList`, an LRO that takes a few
minutes to flip the connection to `ACCEPTED`), the Artifact Registry repo and
staging bucket, five service accounts, the four Vertex service-agent grants
fresh projects lack, and the Firestore **ACL** database with its seed
roles/users.

```bash
python apigee/provision/provision.py secrets           # AFTER provision_agents:
                                                       # grants accessors to the SAs it created
```

Copies each agent's Apigee consumer key into a Secret Manager secret **in the
AI project** (plus the BFF's `apigee-agent-key`) and grants the accessors.
Key values never land in the repo or your shell history.

```bash
python agents/provision/deploy_agents.py --all         # specialists first, supervisor last
python agents/provision/deploy_agents.py --check       # engines + registry rows green
```

No per-agent `.env` needed — project, bucket, SA, PSC interface and proxy
URLs all default from the manifests. First-ever deploys through a new network
attachment should be retried **serially** if any fail with an opaque 500 (see
Troubleshooting).

Optional — per-user keys for the Direct view:

```bash
python apigee/provision/provision.py users             # one app per end user;
                                                       # prints each key ONCE
```

## Phase D — Frontend (BFF on Cloud Run + IAP)

Tool reference: [frontend/README.md](../frontend/README.md).

```bash
python frontend/deploy_frontend.py --build
```

Builds the image into the AI project's AR repo and applies
`frontend/service.yaml` with every token rendered — including
`AGENT_ENGINE_ID`, which resolves automatically to the newest live supervisor
engine. After the apply it wires IAP (service-agent invoker + the
`iap_members` from the runtime manifest). ⚠️ Skipping console prerequisite #1
(consent screen / OAuth client) does **not** fail the deploy — the IAM wiring
is independent of it — it surfaces afterwards as a **502 on every request**
(troubleshooting table has the exact symptom). Do the console step, then on
org-owned projects nothing else is needed; on no-org projects run the
`iap web enable` command from prerequisite #1.

## Phase E — Smoke test

1. Open the BFF URL (printed by the deploy; IAP will authenticate you).
2. **Direct view**: paste your per-user key (from `provision.py users`), send
   a prompt to a Gemini model, then a Claude model (validates the Model
   Garden enables).
3. **Agent view**: ask something that forces delegation —
   *"show me 10 products and the orders of customer 54"*. The supervisor
   should call specialists, which call MCP tools, which hit the e-commerce
   services.
4. **Observability**: in the AI project's Logs Explorer, take the
   `traceparent` from any `front-ai-logs` entry and filter by its trace id —
   you should see the turn across `front-ai-logs`, `agent-ai-logs`,
   `apigee-ai-logs`, and `services-ai-logs`. Reading guide:
   [OBSERVABILITY.md](OBSERVABILITY.md).

## Phase F — Analytics dashboard (optional)

Tool reference: [analytics/README.md](../analytics/README.md).

**Optional and self-contained** — the core demo (Phases A–E) doesn't need it. It
stands up the aggregate analytics layer with zero manual report building: a
Cloud Monitoring dashboard (log-based metrics over the four ai-logs, AI project)
plus **Apigee custom reports** (token/user/model/latency cuts over the gateway's
Data Collectors, provisioned via the org's reports API). It owns its own API
(`monitoring`), so it's deliberately **not** wired into `provision_agents.py`;
everything the reports consume (Data Collectors, `llm.thinkingTokens` logging)
ships in the Phase B proxy bundles already.

```bash
python analytics/provision_analytics.py --check        # read-only status
python analytics/provision_analytics.py --apply        # metrics + dashboard +
                                                       # Apigee custom reports
```

Then view: Cloud Monitoring → Dashboards (AI project), and Apigee console →
**Analytics → Custom Reports** (pick a report, select the environment + time
window, Run). Timing: the log-based metrics are **forward-only** (they count
traffic *after* `--apply`; ~1–2 min lag), and Apigee Analytics ingests with a
**~5–10 min delay** — drive a few turns and be patient before either populates.
If the custom reports stay empty much longer, check the `analyticsAddon`
finding in `--check` — the org's Analytics add-on must be enabled
([analytics/README.md](../analytics/README.md)).

## Troubleshooting

Everything below was hit for real on fresh projects. General rule:
**fix forward** — correct the manifest or rerun the tool; never hand-edit
cloud resources the tools own (the next `--check` will just report your edit
as drift).

| Symptom | Cause | Fix |
|---|---|---|
| First provisioning pass shows many `UNKNOWN`/listing errors | APIs not enabled yet — listings fail before enablement | Expected. Apply (it enables APIs first), then re-run; green by pass 2–3 |
| `gcloud` step hangs ~10 min then times out | Fresh-project interactive "API not enabled. Enable and retry?" prompt with captured stdin | Already hardened in the tools (`CLOUDSDK_CORE_DISABLE_PROMPTS=1`); if you script your own gcloud, do the same |
| Engine deploy 403 `compute.networkAttachments.get` | Vertex service agent lacks rights on the attachment | `provision_agents.py` grants `compute.networkAdmin` (+ `dns.peer`, bucket access, identity creation) — re-run it, wait ~2 min for IAM propagation |
| All-but-one parallel engine deploys fail with opaque `500 … 13:` | First-ever PSC interfaces racing on a brand-new network attachment | Re-run the failed agents **one at a time**; once interfaces exist the race disappears |
| BFF/agents: `stream failed` / `All connection attempts failed`, zero Apigee logs | PSC endpoint to Apigee is `PENDING` — the instance's `consumerAcceptList` doesn't include the AI project | `provision_agents.py` flags it and apply patches the accept list (LRO, ~5 min); `--check` until the endpoint row says `ACCEPTED` |
| `consumerAcceptList update failed: 400 … resource is locked by another operation` | An Apigee instance operation (create/update) is in flight — the accept-list PATCH can't take the lock | Transient: wait for the instance operation to finish, re-run `provision_agents.py`; then `--check` until `ACCEPTED` |
| BFF `--build` 403 on the Cloud Build source bucket | Fresh projects build as the compute default SA, which starts with no roles | `provision_agents.py` grants `cloudbuild.builds.builder` — re-run it |
| Agent-view error names a `reasoningEngines/...` path in the **wrong project** | Running proxy revision was rendered against an older environment file | `./apigee/deploy_proxies.sh --all` re-renders and redeploys |
| Claude models 4xx in the Direct view while Gemini works | Model Garden enables missing in the AI project | Console prerequisite #2 above |
| Every BFF request answers **502** with `x-goog-iap-generated-response: true` and body `Empty Google Account OAuth client ID(s)/secret(s)` | The AI project has no usable IAP OAuth client — on org-owned projects the Internal consent screen is missing; on **no-org** projects the Google-managed client doesn't exist at all | Console prerequisite #1 above (no-org path: External consent screen + test users + custom OAuth client + `iap settings set`) |
| Agents answer but every tool call fails (`Tool 'listProducts' not found. Available tools: transfer_to_agent`); gateway shows `mcp-server` 503 `NoResolvedHost: Unable to resolve host mcp.apigee.internal` | Apigee's **managed MCP backend** hasn't finished provisioning — it builds asynchronously after the first `/mcp` proxy deploy, and the proxy's deployment stays `PROGRESSING` until then (observed stuck for hours on an EVALUATION org; PAYG INTERMEDIATE provisioned routinely) | Wait for the `mcp-server-apiproxy` deployment to reach `READY`; if it never does on an eval org, upgrade to PAYG (billing-type prerequisite above). Check with: `curl -H "Authorization: Bearer $(gcloud auth print-access-token)" https://apigee.googleapis.com/v1/organizations/<ORG>/environments/<ENV>/apis/mcp-server-apiproxy/revisions/1/deployments` |
| Direct view: `403` on a valid-looking key | Key belongs to a different user — the gateway binds each key to its owner's IAP email (`owner_email`) | Use the key issued for the signed-in account (`provision.py users`) |
| `CreateReasoningEngine … storage.objects.get` | Vertex service agent can't read the staging bucket | `provision_agents.py` re-run (it creates the service agent explicitly, then grants) |
| Specialist `/a2a/v1/card` returns 403 | Engine not running as its per-agent SA | `deploy_agents.py --check` shows the SA mismatch; redeploy the agent |
| Supervisor 403s to a `reasoningEngines/<id>` that no longer exists | A `--cleanup` deleted an engine a stale/duplicate registry entry still pointed at | Normally self-corrects — the supervisor re-points to the newest live engine each turn (~60s, A2A_INTEGRATION §3.4). If it persists: `deploy_agents.py --list-registry` (marks entries LIVE/DEAD) and `--sync-registry` prunes the stale duplicates |
| SQL seed import permission errors | Instance SA can't read the staging bucket object | `provision_services.py seed` handles the grant; re-run it |
| `deploy_services` warns `invoker grant failed: … cloud-run-invoker@… does not exist` | The invoker SA lives in the apigee project and didn't exist yet (fresh org) | Re-run `provision_services.py` (it now creates the SA), then `deploy_services.py --iam --all` — grants only, no rebuild |
| `provision.py apigee`: traceConfig row `MANUAL … HTTP 400: distributed tracing not enabled …` | The optional `traceConfig` block was uncommented on an env type below COMPREHENSIVE (prerequisites §1) | Re-comment the block (the demo doesn't need it), or upgrade the env type and re-run — everything else applies normally either way |
| Proxy deploy 403 `iam.serviceAccounts.actAs … apigee-ai-consumer@…` | The AI-side deploy SA doesn't exist / you lack actAs on it (fresh org) | Re-run `provision.py apigee` — it ensures **both** deploy SAs exist and grants the deployer actAs; wait ~1 min for IAM, then redeploy the failed proxies |
| Both UI views fail; Apigee debug shows `AccessTokenGenerationFailure — Failed to generate OAuth2 access token for service account …` | The **Apigee service agent** lacks `serviceAccountTokenCreator` on the deploy SA — deploys fine (that's actAs), but the runtime can't mint the proxy's Google tokens. Standard org provisioning grants it; fresh/Terraform orgs may not | Re-run `provision.py apigee` — the deploySA rows now check + grant it on both SAs; ~1–2 min IAM propagation, then retry |
| **Every** engine deploy fails `404 … storage(.mtls).googleapis.com … The requested project was not found` while all the gcloud-based phases passed | Stale **Application Default Credentials** — engine deploys run on ADC (the Vertex SDK), not your `gcloud auth login`; an ADC from an earlier login can't see the fresh project, so the SDK's staging-bucket lookup fails and it tries to *create* the bucket | `gcloud auth application-default login` (same account as gcloud) + `gcloud auth application-default set-quota-project <ai project>`; if the mTLS endpoint keeps failing after that: `gcloud config set context_aware/use_client_certificate false`. `deploy_agents.py` now checks ADC up front and prints exactly this |
| Fresh org: the LLM API products have **no per-model allow-list / no LLM token quota** (console shows none; the manifest declares `llmModels` + `llmQuota`) | An older `provision.py` CREATEd products without the LLM constructs (it assumed products always pre-exist; UPDATE was the only path that rendered them) | Pull + re-run `provision.py apigee` — `--check` flags the products as DRIFT (`llmQuota=-/-/- want …; llmOps missing …`) and apply rebuilds them in place |
| A service's **first DB-touching request** 500s (`Can't connect to MySQL server on '10.0.0.x' (timed out)` in its Cloud Run log), then works on retry; `/health` was fine throughout | First connections through a freshly programmed Cloud SQL PSC / VPC-egress path can exceed the connect timeout on a brand-new deploy — the dataplane warms on first use | Self-corrects once warm. The services now retry the connect internally (3×, 10s timeout) — if you hit this on an older build, pull + `python services/provision/deploy_services.py --all` |

## Cost notes

Four Agent Engines bill per CPU/RAM-second; Cloud SQL Enterprise runs
~$1.20/day at this tier; Cloud Trace bills per span and Logging per GiB
(interaction logs are compact — bodies are only logged, truncated, at the
BFF); the dominant cost is model usage (~6 LLM calls per agent turn). Tear
down by deleting the ai/backend projects; the Apigee org outlives the demo.
