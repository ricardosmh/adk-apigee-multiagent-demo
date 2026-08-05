# tests/ — the guard rails

```bash
pytest tests/        # no GCP access needed; heavy-dep tests skip when
                     # google-adk / a2a-sdk aren't installed
```

The suite has three jobs:

**1. Pure comparator tests** — every provisioning tool's compare logic is a
pure function tested against fixture states (fresh project → all MISSING,
converged → all OK, unreadable → honest UNKNOWN): `test_provision_check.py`
(apigee), `test_provision_agents.py`, `test_provision_services.py`,
`test_deploy_services.py`, `test_deploy_agents.py`, `test_frontend_check.py`.

**2. Consistency guards** — facts declared in more than one place must agree:
- `test_demo_env.py`: the `${token}` mechanism works; **no manifest or
  deployable contains a literal project id/number** —
  [`demo-environment.yaml`](../demo-environment.yaml) is the only place
  naming projects ([docs/TOOLING.md](../docs/TOOLING.md)).
- `test_docs_guard.py`: the same discipline for documentation — no literal
  project ids, no retired endpoints/files, no broken relative links.
- `test_manifest_consistency.py`: cross-manifest agreements (agent SAs and
  secret names ↔ apigee apps/accessors; the services' invoker SA ↔ the
  apigee ecommerce deploy SA; proxy bundle audiences ↔ service audiences;
  every proxy carries its `ML-interaction` logging policy).
- `test_dep_consistency.py`: pinned dependency agreement across the agents.

**3. Behavior tests** — BFF endpoints (`test_frontend.py`), IAP JWT
verification fail-closed paths (`test_iap.py`), tracing/log setup
(`test_tracing.py`), A2A streaming/attribution helpers (`test_a2a_common.py`),
ACL logic (`test_acl.py`).

[`smoketest/`](smoketest/README.md) is different: a **live** headless check
of the Apigee LLM endpoint (raw + ADK), useful when bisecting gateway vs
agent problems.

Convention: every new comparator or cross-file coupling gets a test here —
the suite is what lets the fresh-project rebuilds converge on the first try.
