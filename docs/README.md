# Documentation index

The map for the whole repo's documentation. Three tiers: the
[root README](../README.md) orients, **area READMEs** live next to the code
they describe, and the files here cover what crosses areas.

## Read in this order

1. **[GETTING_STARTED.md](GETTING_STARTED.md)** — three fresh projects →
   working demo: prerequisites (incl. what you need from your Apigee
   project), the phased bootstrap, troubleshooting. **The** end-to-end
   sequence — no other doc repeats it.
2. **[ARCHITECTURE.md](ARCHITECTURE.md)** — the whole system: the three
   projects, topology + request lifecycles (mermaid), security model,
   networking (both PSC paths), and why each choice was made.
3. **[TOOLING.md](TOOLING.md)** — how the automation works: the shared
   check/apply grammar, `demo-environment.yaml` + token rendering, the three
   manifests, the tools, the guard tests.
4. **[OBSERVABILITY.md](OBSERVABILITY.md)** — the four named logs, the
   end-to-end trace, the enriched gateway record (tokens, quota counters,
   latency decomposition), the OTEL layer, a query cookbook, the admin
   **Trace Explorer & Sessions** views, and the **analytics layer**
   (Cloud Monitoring dashboard + Apigee custom reports).
5. **[OPERATIONS.md](OPERATIONS.md)** — day-2: invoke, read logs, redeploy,
   clean up, costs.
6. **[A2A_INTEGRATION.md](A2A_INTEGRATION.md)** — deep dive: supervisor ↔
   specialist A2A (server/card, discovery, auth, ACL, call flow, and token
   streaming with its ADK 2.3 traps).

## Area READMEs (live with the code)

| Area | Covers |
|---|---|
| [agents/](../agents/README.md) | The four agents, runtime manifest, `a2a_common`, provision/deploy tools |
| [apigee/](../apigee/README.md) | Proxy deploys, org prerequisites; [proxies/](../apigee/proxies/README.md) is the per-proxy catalog |
| [services/](../services/README.md) | The e-commerce backend, its manifest and tools |
| [frontend/](../frontend/README.md) | The BFF: views, config, `service.yaml`, deploy tool |
| [analytics/](../analytics/README.md) | Optional analytics module: log-based metrics + Monitoring dashboard, provisioned Apigee custom reports |
| [tests/](../tests/README.md) | What the suite guards and how to run it |

## Doc conventions (how these docs stay honest)

- **No project ids, engine ids, project numbers or IPs in prose** — reference
  `demo-environment.yaml` keys or use placeholders. Enforced by
  `tests/test_docs_guard.py`, exactly like the manifests.
- **One home per fact** — the end-to-end sequence lives in GETTING_STARTED;
  per-tool flags in the tool's README; everything else links instead of
  repeating.
- **No plan/phase/status documents** — docs describe what IS. Superseded docs
  are deleted (git history is the archive).
- **Mermaid for diagrams** (diffable), and every doc's first paragraph states
  its scope and names its canonical neighbors.
