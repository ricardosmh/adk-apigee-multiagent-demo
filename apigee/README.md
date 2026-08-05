# apigee/ — the gateway

Everything Apigee: the 7 proxy source bundles, the org manifest, the
provisioning tool, and the proxy deploy script. Apigee is the demo's single
governance point — every model call, tool call, agent invocation and
e-commerce API call crosses it
([docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md)).

## Layout

| Path | What it is |
|---|---|
| [`proxies/`](proxies/README.md) | The 7 extracted proxy bundles — **the catalog README there documents each proxy's contract, policies and deploy SA** |
| [`provision/`](provision/README.md) | `provision.py` + `manifest.yaml`: reconciles the org (target servers, data collectors, products + LLM quotas, apps, grants, optional trace config) and syncs keys to Secret Manager |
| [`deploy_proxies.sh`](deploy_proxies.sh) | Imports + deploys proxy bundles (below) |
| `.env.example` | **Optional** overrides → `apigee/.env` (gitignored); the deploy script needs no `.env` — org/env/SA default from `demo-environment.yaml` + the manifest |

## Deploying proxies — `deploy_proxies.sh`

No config needed — the org and env come from
[`demo-environment.yaml`](../demo-environment.yaml) (`projects.apigee`,
`apigee_env`) and the deploy SAs from
[`provision/manifest.yaml`](provision/manifest.yaml) (`meta.deploy_sa`,
`meta.ecommerce_deploy_sa`). An optional `apigee/.env` (see `.env.example`)
overrides any of them for experiments.

```bash
./deploy_proxies.sh --all              # every bundle
./deploy_proxies.sh gemini-llm-apiproxy supervisor-agent-endpoint
./deploy_proxies.sh --check            # deployed revisions, read-only
./deploy_proxies.sh                    # usage + available bundle names
```

What it does per proxy, via the Apigee management API (curl + jq + zip, no
apigeecli):

1. **Renders tokens**: copies the bundle to a temp dir and substitutes
   `__AI_PROJECT__` / `__AI_REGION__` / `__AI_PROJECT_NUMBER__` from
   [`demo-environment.yaml`](../demo-environment.yaml); aborts if any token
   remains unresolved. The bundles in git never contain project ids.
2. **Selects the deploy SA**: auto-detects need (bundles that mint Google
   tokens or write Cloud Logging); precedence per-proxy `APIGEE_SA_<name>` →
   group default (`ecommerce-*` → the manifest's `ecommerce_deploy_sa`) →
   global `APIGEE_DEPLOY_SA` (defaults to the manifest's `meta.deploy_sa`).
   Proxies that need none get none.
3. **Imports a new revision and deploys** with override — re-attaching the SA
   every time (Apigee X drops it on override otherwise).

Auth: `APIGEE_TOKEN` or `gcloud auth print-access-token`. The deployer needs
`iam.serviceAccounts.actAs` on the deploy SAs.

## Org prerequisites

Env-level resources the proxies depend on (target servers, data collectors,
API products, apps/keys, the two deploy SAs) are **declared in
[`provision/manifest.yaml`](provision/manifest.yaml) and created by
`provision.py apigee`** — audit any environment with:

```bash
python apigee/provision/provision.py apigee --check
```

The only thing that must pre-exist is the org itself (with an environment on
an instance) — see
[docs/GETTING_STARTED.md — prerequisites](../docs/GETTING_STARTED.md#prerequisites).

## Refreshing a tracked bundle from the org

If a proxy was edited in the Apigee UI (avoid — the repo is the source of
truth), re-export it:

```bash
apigeecli apis fetch --name <proxy> --rev <N> --org <ORG> --token "$(gcloud auth print-access-token)"
unzip -o <proxy>.zip -d apigee/proxies/<proxy>/ && rm <proxy>.zip   # zips are gitignored
```

Then re-introduce the `__TOKENS__` for any project-specific values — the guard
test (`tests/test_demo_env.py`) fails on literal ids.
