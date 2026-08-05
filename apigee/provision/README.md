# Apigee provisioning — manifest-driven, scope + mode CLI

Declarative desired state in [`manifest.yaml`](manifest.yaml); a small engine
reconciles the live Apigee org / GCP to it. No secret values here — only secret
names (values live in Secret Manager). Design + rationale in
[`../README.md`](../README.md).

## Model

- **`manifest.yaml`** = the GATEWAY source of truth (target servers, data
  collectors, API products, developer, apps → secrets bridge, retired resources).
- **`apigee --check`** (read-only) — diff manifest ↔ live; report per resource:
  `✅ match · ⚠ drift (field=actual, want=desired) · ❌ missing · ➖ should-not-exist`.
  This is the "how do I verify target servers / apps / products" answer.
- **`apigee`** — reconcile the Apigee org (idempotent; bare = confirm, `--dry-run` = preview, `--apply` = unprompted): create missing, fix drift
  (e.g. product `environments` → `[sandbox-internal]`), add app↔product links,
  delete `retired`.
- **`secrets`** — wire each app's consumer key into its `apigee-key-<agent>`
  secret in the AI project (version only if changed) + grant `secretAccessor`
  to the agent SAs and the Vertex service agent. Validates per secret:
  exists → accessors granted → **value still equals the app's current consumer
  key** (rotation drift is flagged `STALE` and fixed by the same command;
  values are compared, never printed).

**Context split:** this tool owns the **gateway** context only. Agent SA
lifecycle (creation, project roles, bucket/actAs) is the **agent** context —
[`../../agents/runtime-manifest.yaml`](../../agents/runtime-manifest.yaml) +
`agents/provision/provision_agents.py` (run FIRST; `secrets` skips grants for missing
SAs). `tests/test_manifest_consistency.py` keeps the two manifests + the
`.env.example`s agreeing on SA/secret names.

Uses the Apigee management API (curl/jq or a small Python engine) — same
credentials as `../deploy_proxies.sh` (gcloud token / `APIGEE_TOKEN`). Not
Terraform: the Apigee provider has poor coverage for data collectors and the LLM
product constructs (`llmOperationGroup`/`llmQuota`) this repo uses, and this
avoids tfstate. (The manifest stays the human-readable spec either way.)

## Scope notes

- Everything scopes to **`sandbox-internal`** (env-scoped resources per env;
  products reconciled to `[sandbox-internal]`).
- **Cross-project:** the Apigee org project; agents + key secrets in
  the AI project (`demo-environment.yaml → projects.ai`). The engine reads app keys from Apigee and writes secrets to
  the AI project.
- **Ordering gotcha:** reconcile products (add `mcp-server-apiproduct` to
  `sandbox-internal` + to the specialist apps) **before** relying on the now-enabled
  MCP `VerifyAPIKey`, or specialist tool calls 401.

## Run

```bash
pip install -r apigee/provision/requirements.txt        # PyYAML (HTTP is stdlib)
# auth: APIGEE_TOKEN, else falls back to `gcloud auth print-access-token`
python apigee/provision/provision.py apigee --check      # read-only drift report
```
Exit code is non-zero if anything is `DRIFT` / `MISSING` / `EXTRA` (CI-friendly).

## Status

- ✅ `manifest.yaml` — final desired state.
- ✅ `provision.py apigee --check` — read-only drift report (target servers, data
  collectors, products, apps, retired). Pure comparators unit-tested in
  `tests/test_provision_check.py`; the live `fetch_*` layer needs real creds.
- ✅ `provision.py apigee` — plan-first reconcile. Prints the plan and **asks
  before writing** (`--dry-run` previews; `--apply` writes unprompted).
  Creates missing target servers /
  data collectors / products / apps, fixes product env + app product membership
  (UPDATE preserves live llm config), deletes `retired` apps. `derive_plan` is
  unit-tested; the write path needs live validation (data-collector TYPE drift is
  flagged MANUAL — types are immutable).
- ✅ `provision.py secrets` — app keys → Secret Manager + accessor grants
  (agent SA **and** Vertex service agent). Same modes: confirm / `--dry-run` /
  `--apply`. Skips (with a pointer) accessor grants for SAs that don't exist yet.
  SA creation/roles moved to `agents/provision/provision_agents.py` (context split).
- ✅ deploy wiring — `deploy_common.apigee_secret_env_vars()` sends
  `APIGEE_API_KEY` as a `SecretRef` when the agent's `.env` sets
  `APIGEE_API_KEY_SECRET`; `preflight_identity()` verifies the runtime SA exists
  (blocks), checks the manifest-declared roles + secret accessors, and
  cross-checks the `.env` against `runtime-manifest.yaml` (wrong SA/secret =
  caught drift) — warnings point at the right provisioning tool.
  `provision_agents.py --apply` reconciles infra + SAs (no shared
  `AGENT_RUNNER_SA` anywhere). BFF secret names still `<managed by service.yaml>`.

Run order: `apigee` (gateway) → `agents/provision/provision_agents.py --apply`
(agent SAs) → `secrets --apply` (keys) → deploy agents. Full command walkthrough:
the "Deployment" section of the repo README.
