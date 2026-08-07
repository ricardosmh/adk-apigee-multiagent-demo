# Agent provisioning + deployment — manifest-driven

> **Fresh AI project? Start with [docs/GETTING_STARTED.md](../../docs/GETTING_STARTED.md).**

The AGENT-side mirror of [`apigee/provision/`](../../apigee/provision/): desired
state in [`../runtime-manifest.yaml`](../runtime-manifest.yaml), two tools, same
scope/mode CLI. (Gateway context — products, apps, key→secret wiring — lives in
`apigee/provision/provision.py`.)

## Tools

**`provision_agents.py`** — per-agent runtime SAs + roles + bucket/actAs:

```bash
python agents/provision/provision_agents.py            # report + confirm
python agents/provision/provision_agents.py --check    # read-only, exit 1 on drift
python agents/provision/provision_agents.py --apply    # write unprompted (Step 0 uses this)
```

**`deploy_agents.py`** — the central deploy tool (replaces the per-agent
`deploy.py` scripts):

```bash
python agents/provision/deploy_agents.py --check                # live engines vs manifest
python agents/provision/deploy_agents.py --all                  # deploy all (confirms per agent)
python agents/provision/deploy_agents.py order customer         # just these
python agents/provision/deploy_agents.py --apply --all          # no prompts (CI)
python agents/provision/deploy_agents.py --cleanup              # delete stale generations (confirms)
python agents/provision/deploy_agents.py --sync-registry        # retarget stale registry entries (confirms)
```

- `--check` groups live engines by each agent's `DISPLAY_NAME` and reports:
  missing engine, **multiple generations (stale — cleanup candidates)**, and
  whether the newest engine runs as the manifest-declared SA.
- **All agents deploy in PARALLEL** (per-agent logs in
  `agents/.deploy-logs/<agent>.log`; summary + failure tails on the console),
  no ordering dependency: the supervisor discovers specialists via Agent Registry
  at RUNTIME, not via the just-created engines. One confirmation covers the batch.
- Every deploy **creates a new engine** (that's the Agent Engine model), so
  redeploys leave a stale one behind — `--check` flags it, `--cleanup` deletes
  it (keeps the newest per agent; lists everything and asks before deleting).
- **Isolation:** each agent deploys in a subprocess (cwd = its dir, its own
  `.env`) — one process can't import two `app` packages or two `.env`s.
- Specialist registry entries normally **auto-update on deploy**; `--check`
  verifies it, `--sync-registry` repairs a stale one. The supervisor's TTL
  heal picks changed URIs up in minutes either way.

## Setup

```bash
pip install -r agents/provision/requirements-deploy.txt   # PINNED deploy-host stack
```

The pin matters: `deploy_agents.py` imports each agent's app locally to pickle
it, so the host must run the same ADK as the engines
(`tests/test_dep_consistency.py` guards the pin against the pyprojects).

## Status

- ✅ `provision_agents.py` — validated live (SAs/roles/grants all green).
- ✅ `deploy_agents.py` — deploy / `--check` / `--cleanup` all validated live
  (parallel four-agent run + stale-generation cleanup). The legacy per-agent
  `deploy.py` scripts and `deploy_all_a2a.sh` are DELETED — `provision_agents.py`
  owns the infra bootstrap (APIs, bucket, GEAP-agent grant) declared in the
  manifest, and `deploy_agents.py` owns deploys.
- ✅ ACL store (phase 1): manifest `acl:` block → Firestore database +
  seed `admin` role (`agents: ["*"]`, `is_admin`) + seed users, reconciled by
  `provision_agents.py` (seed-only: never deletes, merges user roles). Identity
  key = IAP-verified email (BFF `iap.resolve_user_id`); supervisor `acl.py`
  honors the `*` wildcard. Flip `ACL_ENABLED=1` + redeploy the supervisor to
  enforce. Phase 2 = BFF admin API; phase 3 = the frontend management view.
- ✅ Registry sync: `--check` includes per-specialist registry rows (entry →
  engine id vs the newest engine), and `--sync-registry` retargets stale
  entries (PATCH on the v1alpha entry's URL fields; missing entries get a
  create-once pointer). NOTE: entries normally update AUTOMATICALLY on deploy
  (ADK auto-registration, observed live) — the rows are the verification that
  it fired, and sync is the REPAIR path when it didn't. With the supervisor's
  TTL heal: deploy → (auto-registered) → routed, no manual step.


## Rebuilding the AI project (fresh)

The end-to-end bootstrap (all three projects, prerequisites, phases,
troubleshooting) lives in **one** place:
[docs/GETTING_STARTED.md](../../docs/GETTING_STARTED.md). The AI-side facts
worth knowing when you run its Phase C here:

- `provision_agents.py` owns **everything project-side**: APIs, `ai-vpc`
  (global routing) + subnets, the engines' network attachment, the PSC
  endpoint + private DNS to Apigee (including the instance's
  `consumerAcceptList` patch — an LRO; re-check until `ACCEPTED`), the AR
  repo + staging bucket, five SAs with the four GEAP service-agent grants
  fresh projects lack, and Firestore + the ACL seed. Fresh project: run,
  apply, re-run until green (~2 passes).
- `apigee/provision/provision.py secrets` must run **after** it (the accessor
  grants target the SAs it creates).
- **No `.env` files needed** — all runtime config comes from the manifests; a
  local `.env` is purely an override (see each agent's `.env.example`).
- Console-only steps (once per project): the OAuth consent screen and the
  Model Garden Claude enables — see
  [GETTING_STARTED — prerequisites](../../docs/GETTING_STARTED.md#prerequisites).
