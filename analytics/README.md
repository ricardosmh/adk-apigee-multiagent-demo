# Analytics — aggregate, near-real-time metrics (OPTIONAL module)

The aggregate complement to the per-transaction **Trace Explorer**: tokens per
model/user/app, latency, traffic, faults — refreshed minutes-fresh. Every
metric already lives in telemetry the demo emits, so this is a **viz layer,
not a pipeline** — no new telemetry, no BFF views, and nothing manual: both
surfaces (Cloud Monitoring + Apigee custom reports) are fully provisioned by
one tool.

**Optional & self-contained.** This directory is the whole feature. It owns its
own prerequisites (the `monitoring` API it enables itself — deliberately *not*
in `agents/runtime-manifest.yaml`), and reads the project ids straight from
`demo-environment.yaml`. Delete `analytics/` and the base demo is untouched.

## What's here

| File | Purpose |
|---|---|
| `provision_analytics.py` | manifest-free `--check` / `--dry-run` / `--apply` tool that stands up everything |
| `log_based_metrics.json` | 5 Cloud Logging metrics (`ai_llm_calls`, `ai_tokens_total`, `ai_quota_used`, `ai_latency_ms`, `ai_gateway_faults`) — low-cardinality labels only |
| `dashboard.json` | the Cloud Monitoring dashboard over those metrics (AI project) |
| `apigee_reports.json` | the Apigee **custom reports** — token/user/model/latency cuts over the gateway's Data Collectors, rendered by the Apigee console |

## Run it (needs gcloud, authed on the AI project + the Apigee org)

```bash
python analytics/provision_analytics.py --check    # read-only status
python analytics/provision_analytics.py --apply    # reconcile (enables APIs, etc.)
```

`--apply` **re-asserts content every run** (idempotent), so after you edit
`log_based_metrics.json`, `dashboard.json`, or `apigee_reports.json`, just
re-run `--apply` to push it — metrics are created/updated, the dashboard is
deleted + recreated, and reports are diffed by `displayName` and
created/updated in place. For metrics + dashboard, `--check` is existence-only
(it can't cheaply diff a dashboard's JSON); report rows ARE content-diffed
(chart type, dimensions, metrics, filter).

### What `--apply` creates — nothing destructive

| Resource | Where | Storage | Retroactive? |
|---|---|---|---|
| `monitoring` API enabled | AI project | — | — |
| 5 log-based metrics | AI project (Cloud Logging → Monitoring) | none | **no — forward-only** |
| 1 Monitoring dashboard | AI project | none | n/a |
| Apigee custom reports | the Apigee org (`organizations/{org}/reports`) | none — report *definitions* only | **yes** — they query Apigee Analytics, which has been recording since the org existed |

Two timing notes: the log-based metrics only count log entries received
*after* they're created (drive traffic, wait ~1–2 min); Apigee Analytics
ingests with a **~5–10 min delay**, so a fresh report may lag the traffic that
should populate it.

## Apigee custom reports — how the automation works

The gateway's DataCapture policies already feed six org-scoped **Data
Collectors** (declared in `apigee/provision/manifest.yaml`): `dc_model` and
`dc_user_id` (strings → report *dimensions*), `dc_tokenCount`,
`dc_thoughtsTokenCount`, `dc_inputToken`, `dc_outputToken` (integers → report
*metrics*). Custom reports are just saved queries over that data, and they ARE
API-provisionable — `apigee_reports.json` declares them, the tool reconciles
via `organizations/{org}/reports` (same credential as
`apigee/provision/provision.py`; `APIGEE_TOKEN` overrides). The server assigns
each report's id, so **`displayName` is the idempotency key — don't rename a
report in the JSON without deleting the old one in the console** (a rename
creates a second report).

View them: Apigee console → **Analytics → Custom Reports** → pick a report →
select the environment + time window → Run. The five shipped reports:

| Report | Cut |
|---|---|
| AI - Tokens by model | total/input/output/thinking tokens per `dc_model` |
| AI - Tokens by user | tokens per IAP-verified email (`dc_user_id`) — the per-user FinOps view |
| AI - Tokens by app (agent vs direct) | tokens + calls per developer app (`a2a-*` = agents, `llm-user-*` = Direct users, `agents-bff` = the UI) |
| AI - Model traffic over time | calls per model |
| AI - Latency and errors by proxy | avg response time + error count per proxy |

Caveats worth knowing:

- **The Analytics add-on must be enabled on the org** — report definitions
  create fine without it, but render no data. The tool surfaces this as a
  `analyticsAddon` finding; enabling it is a manual/org-level step (console →
  Admin → Add-ons) because it's billing-affecting.
- On a **failed** LLM call the token collectors record their `-1` default, so
  token sums can undercount by a few units under fault injection — trivia at
  demo volume, but it's why the numbers aren't audit-grade.

## Gotcha: Apigee's dual monitored resource

Apigee X emits each gateway log under **two** Monitoring resource types — the
current `api` and a legacy `deprecated_resource` — so every log-based metric has
*two identical series per label*, and Monitoring **can't reduce across different
resource types** (so `REDUCE_SUM`/`groupBy` don't collapse them → every tile
doubled). Every `dashboard.json` filter therefore ends with
`AND resource.type="api"` to pin one. The Apigee custom reports are unaffected
(they read Apigee Analytics, not Monitoring); this is purely a Monitoring-metric
artifact.
