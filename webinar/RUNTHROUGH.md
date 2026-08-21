# Webinar Runthrough & Presenter Guide

This guide accompanies the interactive reveal.js slide deck in `webinar/index.html` (published live on GitHub Pages at `https://ricardosmh.github.io/adk-apigee-multiagent-demo/`).

## Presentation Overview

- **Subject:** Governing Enterprise Multi-Agent Systems on Google Cloud (`adk-apigee-multiagent-demo` reference implementation)
- **Target Audience:** Cloud Architects, Platform Engineers, Security & AI Engineering Leaders
- **Total Duration:** 35–40 minutes (22–24 minutes talk + 10–12 minutes live demos + Q&A)
- **Format:** 22 slides strictly structured around the SCQA framework (Situation → Complication → Question → Answer)

---

## Presenter Controls & Shortcuts

- **Advance Slide:** `Space`, `Right Arrow`, or `L`
- **Previous Slide:** `Left Arrow` or `H`
- **Speaker Notes Window:** Press `S` on your keyboard to open the synchronized speaker notes window with dictation-ready spoken prose and an elapsed timer.
- **Slide Overview Mode:** Press `Esc` or `O` to view all slides in grid format.
- **Full Screen:** Press `F`.
- **Slide Deep-Linking:** Direct navigation via URL hash (e.g. `index.html#/8` or `https://ricardosmh.github.io/adk-apigee-multiagent-demo/#/8`).

---

## Slide-by-Slide Timing & Script Outline

| # | Section | Assertive Slide Title / Focus | Target Time | Cumulative |
|---|---|---|---|---|
| **1** | Title | **Governing Enterprise Multi-Agent Systems on Google Cloud** (Welcome & positioning) | 1:00 | 1:00 |
| **2** | S1 | **Single-model chatbots are evolving into coordinated multi-agent networks** | 1:00 | 2:00 |
| **3** | S2 | **Google Cloud provides the core building blocks for enterprise multi-agent architectures** | 1:00 | 3:00 |
| **4** | C1 | **Unmanaged agent deployments create ungoverned model access and untracked token spend** | 1:15 | 4:15 |
| **5** | C2 | **Multi-hop agent delegations turn production transactions into opaque black boxes** | 1:15 | 5:30 |
| **6** | C3 | **Standard agent prototypes introduce critical identity, network, and database vulnerabilities** | 1:15 | 6:45 |
| **7** | C4 | **Brittle agent coupling and unrepeatable environments stall production operations** | 1:15 | 8:00 |
| **8** | **Q (Pivot)** | **"Can you build a multi-agent system where EVERY call is governed, attributed, traced, and private...?"** *(Pause 3-4s)* | 0:45 | 8:45 |
| **9** | A1 | **Three projects enforce strict separation of concerns across AI, governance, and data** (Anchor topology) | 1:30 | 10:15 |
| **10** | A2 | **Apigee acts as the single governance gateway across models, tools, and backend APIs** (7 proxies) | 1:15 | 11:30 |
| **11** | A3 | **Per-user API keys bound to IAP identities enforce token quotas before model execution** | 1:15 | 12:45 |
| **12** | A4 | **Domain specialists encapsulate scoped MCP toolsets under least-privilege service accounts** | 1:00 | 13:45 |
| **13** | A5 | **The supervisor coordinates over A2A with token streaming and dynamic self-healing discovery** | 1:15 | 15:00 |
| **14** | A6 | **Dynamic, Firestore-backed ACLs enforce fail-closed authorization per conversation turn** | 1:15 | 16:15 |
| **15** | A7 | **The entire data path is private by construction with Private Service Connect and IAM database auth** | 1:15 | 17:30 |
| **16** | A8 | **A single traceparent correlates every hop from browser to database across four named logs** | 1:15 | 18:45 |
| **17** | **A9 (Demo 1)** | **The Trace Explorer replays transactions with exact latency attribution across all three projects** | **5:00** | **23:45** |
| **18** | **A10 (Demo 2)**| **Edge Data Collectors deliver comprehensive token analytics and metrics without an ETL pipeline** | **5:00** | **28:45** |
| **19** | A11 | **One environment file and three declarative manifests drive automated convergence** (Tooling grammar) | 1:15 | 30:00 |
| **20** | A12 | **Automated guardrail tests fail the build on hardcoded IDs, documentation drift, and contract mismatches** | 1:00 | 31:00 |
| **21** | A13 | **Six production-proven patterns worth lifting directly into your own enterprise deployments** | 1:15 | 32:15 |
| **22** | Close | **Enterprise agent governance is ready today: deploy it, inspect it, and lift what you need** (Q&A handoff) | 1:15 | 33:30 |
| — | Q&A | **Audience Questions & Discussion** | 5:00–7:00 | ~40:00 |

---

## Live Demo Cut-In Instructions

### Demo Cut-In #1: Live Trace Explorer (Slide 17)
- **When:** Transitioning into Slide 17.
- **Action:**
  1. Open the BFF UI (e.g. `https://agent-bff-...run.app` or via authenticated tunnel `gcloud run services proxy agent-bff`).
  2. In the **Agent View**, submit a multi-domain prompt: *"Find the latest order for customer 101, check the stock for those items, and summarize the delivery status."*
  3. Point out the live handover reveal in the UI as the supervisor calls `transfer_to_agent(customer_agent)` and `transfer_to_agent(order_agent)`.
  4. Navigate to the **Trace View** (Admin) and select the completed transaction.
  5. Demonstrate the **packet replay**: show the animated packet dwelling on each project node in proportion to measured latency.
  6. Scroll down to the **Latency Waterfall** to isolate the exact latency breakdown between model inference, gateway overhead, MCP tool execution, and Cloud SQL query execution.

### Demo Cut-In #2: Live Dashboards & Token Metering (Slide 18)
- **When:** Transitioning into Slide 18.
- **Action:**
  1. Open the **Apigee Console** → **Analytics** → **Custom Reports**.
  2. Show the auto-provisioned custom report: demonstrate `dc_tokenCount` and `dc_inputToken` aggregated by `dc_model` and `developer_app`.
  3. Show per-user attribution: filter by `dc_user_id` to demonstrate that every individual user's token spend is recorded without an ETL pipeline.
  4. Open **Cloud Monitoring** → **Dashboards** → **AI Analytics Dashboard**.
  5. Highlight real-time platform metrics: requests per minute, token throughput, and p95 latency broken down across the 7 proxies.

---

## Visual Assets & Screenshot Refresh Checklist

The presentation bundles all images locally under `webinar/img/`.

- [x] `trace-explorer.png` — Hero screenshot embedded on Slide 17.
- [x] `apigee-reports.png` & `monitoring-dashboard.png` — Embedded on Slide 18.
- [x] `agent-view.png` & `direct-view.png` — Available in `webinar/img/`.
- [x] `architecture.svg` — Embedded vector topology.

> ⚠️ **Presenter Note on Live Screenshot Refreshes:**  
> If you run a live deployment and want to refresh the screenshots with your own custom brand or environment data:
> 1. Capture new window stills (~1200–1440px wide).
> 2. Save them into `docs/img/` under the existing filenames (`trace-explorer.png`, `apigee-reports.png`, etc.).
> 3. Copy them into `webinar/img/`: `cp docs/img/*.png webinar/img/`
> 4. Commit and push to `main` — GitHub Actions will automatically redeploy the updated deck to GitHub Pages.

---

## Repository Conventions Checklist

- [x] Zero real project IDs, project numbers, or real user emails appear on any slide.
- [x] All asset links are strictly relative (`img/...`, `js/...`, `css/...`) for seamless local execution and GitHub Pages subpath routing (`/adk-demos/`).
- [x] Completely offline capable: no external CDN links or remote script tags.
