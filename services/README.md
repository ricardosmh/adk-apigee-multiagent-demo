# services/ — the e-commerce backend

The business system the agents ultimately query: three FastAPI microservices
sharing one Cloud SQL MySQL database, running IAM-only on Cloud Run in the
**backend project**, fronted by a regional **internal** Application Load
Balancer, and reachable **only through Apigee** (PSC service attachment →
Apigee endpoint attachment → the three `ecommerce-*` proxies).
Topology: [docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md).

## Layout

| Path | What it is |
|---|---|
| [`services-manifest.yaml`](services-manifest.yaml) | Desired state for the whole side: APIs, VPC/subnets, identities, Cloud SQL (+PSC endpoint, seed), the three services (audiences, env), the ALB chain, the PSC/Apigee wiring |
| [`provision/`](provision/README.md) | The two tools: `provision_services.py` (everything except the deploys, + the `seed` data-reset mode) and `deploy_services.py` (the three Cloud Run deploys + invoker/audience reconcile) |
| [`customers-service/`](customers-service/README.md) [`orders-service/`](orders-service/README.md) [`products-service/`](products-service/README.md) | The microservices: FastAPI + SQLAlchemy, IAM DB auth, one domain each |
| `seed/` | `schema.sql` (DDL) + `sample-data.sql` (100 customers, 100 products, 59 orders) — imported by the `seed` mode |

## Key design points

- **IAM everywhere**: services take no unauthenticated traffic (Apigee's
  dedicated invoker SA is the only caller); the DB user *is* the runtime SA
  (Cloud SQL IAM auth — the MySQL username is the SA's local part,
  `ecommerce-sa`); the instance is PSC-only with no public IP.
- **Stable custom audiences** (`https://<svc>.ecommerce.internal`): the
  proxies' ID-token audiences never change when projects change.
- **Structured request logging**: each service's `app/ailog.py` middleware
  writes one entry per request to `services-ai-logs` **in the AI project**,
  carrying the inbound `traceparent` — the deepest hop of the demo's
  end-to-end trace ([docs/OBSERVABILITY.md](../docs/OBSERVABILITY.md)).
- The orders→products in-service call deliberately has no URL configured
  (it would need an ID token through the IAM-protected ILB); it falls back
  to the shared database. Noted as a follow-up in
  [provision/README.md](provision/README.md).

## Local development

Each service runs standalone against any MySQL (see its README for env
vars). The Cloud Run deploys are exclusively via
`services/provision/deploy_services.py` — there are no per-service deploy
scripts.
