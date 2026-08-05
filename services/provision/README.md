# E-commerce services project — fresh-project bootstrap

One GCP project hosts the **Cloud SQL MySQL database** and the three **Cloud
Run services** (customers / orders / products), fronted by a **regional
internal Application Load Balancer** and exposed to Apigee through a **PSC
service attachment → Apigee endpoint attachment → TargetServer
`ts-ecommerce-services`**. Everything below is manifest-driven
(`services/services-manifest.yaml`) with the repo's standard
`--check / --dry-run / confirm / --apply` grammar.

```
Apigee (the org project)                  services project
  proxies ecommerce-*-management  ──►  endpoint attachment ──► PSC service
  (GoogleIDToken, run.invoker)                                 attachment
                                                                  │
                                              rilb-ecommerce-services (10.0.0.11:80)
                                              /customer-management/* ► customers ─┐
                                              /order-management/*    ► orders     ├─► Cloud SQL MySQL
                                              /product-management/*  ► products ──┘   (PSC 10.0.0.5,
                                                                                        IAM DB auth)
```

## Running it

The end-to-end bootstrap order (all three projects) lives in
[docs/GETTING_STARTED.md](../../docs/GETTING_STARTED.md) — its Phase A is
this directory's tools. The services-side facts worth knowing:

- `provision_services.py` **converges in one invocation** (up to 3 internal
  passes: enable APIs → re-check → create → re-check; the Cloud SQL create
  blocks ~10–15 min). It needs a principal that can administer the backend
  project AND the Apigee org (the endpoint attachment lives in the org).
- `provision_services.py seed` is a **destructive reset** (DROPs + recreates
  the four tables, loads 100 customers / 100 products / 59 orders, imports
  the IAM user's generated GRANTs). Safe to re-run for fresh demo data.
- `deploy_services.py --all` deploys the three services IAM-only with stable
  custom audiences and reconciles the invoker grant exactly (strays removed).
  `--iam` reconciles the invoker grant **only** — no build, no new revision
  (e.g. if the grant failed because the invoker SA appeared later).
- The invoker SA itself lives in the **apigee project**; `provision_services.py`
  ensures it exists (the `invokerSA` row) so the grant can't race Phase B,
  and `provision.py apigee` reconciles it later as its declared owner.
- After deploying, re-run `provision_services.py --check`: the LB/NEG rows
  bind by service name and the endpoint attachment should reach `ACCEPTED`.
- The three `ecommerce-*` proxies deploy **as** the dedicated invoker SA from
  the apigee manifest automatically (no env vars) —
  `./apigee/deploy_proxies.sh <names>`.

Smoke, through the gateway:

```bash
curl -k https://<apigee-host>/customer-management/health
curl -k https://<apigee-host>/product-management/products?limit=3
```

## Design notes (why it's built this way)
- **IAM everywhere**: services deploy `--no-allow-unauthenticated`; the ONLY
  invoker is the DEDICATED `cloud-run-invoker` SA the ecommerce proxies
  deploy as (`identity.invoker_sa`, cross-checked by tests against
  `apigee/provision/manifest.yaml meta.ecommerce_deploy_sa` — deliberately
  NOT the AI proxies' apigee-ai-consumer). The
  proxies mint `GoogleIDToken` with **stable custom audiences**
  (`https://<svc>.ecommerce.internal`) so the bundles never change per
  project — the audience is registered on each service via
  `--add-custom-audiences`.
- **IAM DB auth**: the MySQL username is just **`ecommerce-sa`** — Cloud SQL
  for MYSQL truncates the entire domain from IAM usernames (the
  `sa@<project>.iam` long form is the POSTGRES convention; using it here
  fails auth). The user is still *created* with the full SA email via
  `gcloud sql users create`. The instance is created PSC-only
  with `cloudsql_iam_authentication=on` (**both must be set at create**);
  a fresh IAM DB user has no privileges, which is why `seed` also imports a
  generated `grants.sql`.
- **`seed` is a reset, not a one-time step** — `schema.sql` `DROP TABLE IF
  EXISTS`s everything, `sample-data.sql` repopulates. Schema and data live in
  separate files on purpose: the data set is a legible artifact you can
  inspect or swap without touching DDL.
- **No health-check firewall**: serverless-NEG backends take no health
  checks; the legacy rule was dead weight and was intentionally dropped.
- **orders → products**: `PRODUCTS_SERVICE_URL` is deliberately NOT set. The
  in-code call is unauthenticated and would 403 through the IAM-protected
  ILB, then silently fall back to the shared DB (same data). Follow-up if
  ever wanted: mint an ID token in orders (metadata server, audience =
  products' custom audience) + grant `run.invoker` on products to
  `ecommerce-sa`.
- **PSC accept list**: `ACCEPT_MANUAL`, and the accept list must contain
  **Apigee's Google-managed TENANT project** — NOT the org project (the org
  project never appears as the consumer; accepting it leaves the connection
  PENDING forever). The tenant id is unknowable in advance, so apply runs the
  two-phase dance automatically: create the endpoint attachment → read the
  PENDING consumer from the service attachment's `connectedEndpoints` →
  accept that project → poll until ACCEPTED. `--check` flags any connected
  consumer missing from the accept list. (`ACCEPT_AUTOMATIC` also supported
  via the manifest if you prefer no accept list.)
- **The legacy shell deploy scripts were absorbed and deleted** — they
  carried one live bug (services deployed publicly) and a stale LB shape;
  git history preserves them.
