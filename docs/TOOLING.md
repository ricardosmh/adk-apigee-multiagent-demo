# Tooling — how the automation works

Everything in this demo is reconciled or deployed by **seven core tools (plus
one optional analytics tool) sharing one model**: desired state lives in
version-controlled **manifests**, project
names live in **one environment file**, and every tool speaks the same
**check/apply grammar**. This doc describes the shared model; each tool's
flags and resource list live in its own README (table below). The end-to-end
run order lives in [GETTING_STARTED.md](GETTING_STARTED.md) only.

## The environment file

[`demo-environment.yaml`](../demo-environment.yaml) (repo root) is the single
place naming the GCP projects, regions, and the deployer's identity:

```yaml
projects: {apigee: ..., ai: ..., backend: ...}
apigee_env: ...
regions:  {ai: ..., gateway: ..., backend: ...}
identity: {admin_email: ...}   # → IAP member, ACL admin, Apigee developer, first end user
```

Replicating the demo in new projects = edit this file, run the bootstrap.

Two token mechanisms carry these values everywhere:

1. **Manifest tokens — `${dotted.path}`.** The three manifests reference
   environment values as `${projects.ai}`, `${regions.gateway}`,
   `${apigee_env}` … [`demo_env.py`](../demo_env.py) (`load_manifest()`)
   resolves them recursively at load time; an unknown token raises `KeyError`
   immediately — a typo cannot silently deploy. All Python tools load their
   manifests through it.
2. **Deployable tokens — `__UPPER_SNAKE__`.** Files that *ship somewhere*
   (Apigee proxy XML, `frontend/service.yaml`) can't execute Python, so they
   carry placeholder tokens (`__AI_PROJECT__`, `__AI_REGION__`,
   `__AI_PROJECT_NUMBER__`, `__TAG__`, `__SUPERVISOR_ENGINE_ID__`). The deploy
   tools render them into a **temporary copy** at deploy time
   (`deploy_proxies.sh` sed-renders before zipping and aborts on any
   unresolved token; `deploy_frontend.py` renders `service.yaml` including
   auto-resolving the newest supervisor engine id) — the files in git never
   contain a project id.

Guard tests enforce both: `tests/test_demo_env.py` fails if a manifest or
deployable reintroduces a literal project id/number, and
`tests/test_docs_guard.py` extends the same rule to documentation.

## The shared CLI grammar

Scope names **what**, flags name the **mode** — identical across tools:

| Invocation | Behavior | Exit code |
|---|---|---|
| `tool --check` | Read-only report: live state vs manifest | `1` if anything needs reconciling |
| `tool --dry-run` | Same report, explicitly "will write nothing" | `0` |
| `tool` (bare) | Report → interactive confirm → apply (degrades to dry-run without a TTY) | |
| `tool --apply` | Apply without prompting (CI mode) | non-zero on failure |

Reports are per-resource findings:

| Status | Meaning |
|---|---|
| ✅ `OK` | live matches the manifest |
| ⚠ `DRIFT` | exists but differs (the detail says exactly how, and what apply will do) |
| ❌ `MISSING` | declared but absent — apply creates it |
| `EXTRA` / retired | exists but shouldn't — apply deletes it (only for explicitly declared retirees) |
| `UNKNOWN` | the **listing itself** failed; reported honestly as one finding instead of fake-MISSING rows (normal on a fresh project before APIs are enabled) |
| `MANUAL` | drift the API can't fix (e.g. an immutable data-collector type) — printed, never auto-applied |

Apply is **converging**: create/patch in dependency order, refetch, re-report
(`provision_services.py` loops internally up to 3 passes; the others expect
one re-run on a fresh project). Comparators are pure functions, unit-tested
in `tests/` without touching GCP.

Destructive operations are never implicit: data reset is its own explicit
mode (`provision_services.py seed`), engine deletion its own flag
(`deploy_agents.py --cleanup`), and both confirm first.

## The three manifests

| Manifest | Declares | Consumed by |
|---|---|---|
| [`agents/runtime-manifest.yaml`](../agents/runtime-manifest.yaml) | The four agents (dir, SA, roles, secret name, per-agent env), shared `engine_env`, the BFF identity + IAP members, the Firestore ACL seed, AI-project infra (APIs, network, PSC toward Apigee, staging bucket, AR repo) | `provision_agents.py`, `deploy_agents.py` |
| [`apigee/provision/manifest.yaml`](../apigee/provision/manifest.yaml) | Org meta + the two deploy SAs, target servers (incl. runtime-resolved `@endpointAttachment:` hosts), API products with LLM quotas, developer + apps (key→secret wiring), end users, optional trace config (commented out), cross-project telemetry/model grants, retired resources | `provision.py` (all scopes), `deploy_proxies.sh` (reads `meta.ecommerce_deploy_sa`) |
| [`services/services-manifest.yaml`](../services/services-manifest.yaml) | Backend APIs, VPC/subnets, identities, Cloud SQL (+PSC endpoint, seed files), the three services (dirs, audiences, env), the internal ALB chain, the PSC service attachment + Apigee endpoint attachment | `provision_services.py`, `deploy_services.py` |

Cross-manifest agreements (e.g. the services' invoker SA == the apigee
manifest's `ecommerce_deploy_sa`; proxy bundle audiences == service
audiences; app→secret names == agent secret names) are **test-enforced** by
`tests/test_manifest_consistency.py`.

## The tools

| Tool | Targets | Owns | Reference |
|---|---|---|---|
| `agents/provision/provision_agents.py` | ai project | APIs, ai-vpc + PSC/DNS toward Apigee, network attachment, bucket/AR, SAs + service-agent grants, Firestore ACL | [agents/provision/README.md](../agents/provision/README.md) |
| `agents/provision/deploy_agents.py` | ai project | The four Agent Engines (+ `--check`, `--cleanup`, `--sync-registry`) | [agents/provision/README.md](../agents/provision/README.md) |
| `apigee/provision/provision.py` (`apigee` / `secrets` / `users`) | apigee org (+ secrets in ai project) | Target servers, products, apps, deploy SAs, telemetry grants, traceConfig when declared / key→Secret Manager sync / per-end-user apps | [apigee/provision/README.md](../apigee/provision/README.md) |
| `apigee/deploy_proxies.sh` | apigee env | Import + deploy the 7 proxy bundles (token rendering, per-proxy SA selection) | [apigee/README.md](../apigee/README.md) |
| `frontend/deploy_frontend.py` | ai project | The BFF Cloud Run service from `service.yaml` (+image builds, IAP wiring) | [frontend/README.md](../frontend/README.md) |
| `services/provision/provision_services.py` (+ `seed`) | backend project (+ in the apigee project: the endpoint attachment and the invoker SA's existence) | APIs, network, SAs, Cloud SQL + PSC, internal ALB, PSC chain / data seed | [services/provision/README.md](../services/provision/README.md) |
| `services/provision/deploy_services.py` | backend project | The three Cloud Run services (IAM-only, custom audiences, invoker reconcile) | [services/provision/README.md](../services/provision/README.md) |

Plus one **optional** tool for the analytics module (not needed for the core
demo; it owns its own APIs, so it isn't wired into the agents manifest):

| Tool | Targets | Owns | Reference |
|---|---|---|---|
| `analytics/provision_analytics.py` | ai project + apigee org | log-based metrics + Monitoring dashboard (AI project), Apigee custom reports (org) | [analytics/README.md](../analytics/README.md) |

## Conventions the tools enforce

- **Secrets never in git or output.** The repo stores secret *names* and
  key→secret *wiring*; values flow Apigee → Secret Manager via
  `provision.py secrets` and are printed exactly once (end-user keys) or
  never.
- **Least-privilege identities.** One SA per agent, a dedicated BFF runner, a
  dedicated ecommerce invoker whose only power is `run.invoker`, and a proxy
  deploy SA per concern — each declared in a manifest, each grant a check row.
- **No stale files.** Superseded scripts/docs are deleted, not archived —
  git history is the archive.
- **Honest reporting.** Listing failures are `UNKNOWN`, not fake-green or
  fake-missing; apply errors print the API's error body.

## Running the tests

```bash
pytest tests/            # pure comparators, manifest consistency, guard tests
```

No GCP access needed — see [tests/README.md](../tests/README.md).
