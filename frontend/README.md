# Frontend — the BFF + browser UI

A FastAPI **BFF** (`app/server.py`) on Cloud Run behind **IAP**, serving a
static UI with five views (the last three admin-only):

- **Direct model** — SSE chat with the Apigee `/llm-stream` gateway. The user
  pastes **their own API key** (issued per person by
  `apigee/provision/provision.py users`; the gateway 403s a key whose
  `owner_email` doesn't match the signed-in IAP identity); a model picker
  covers Gemini and Claude.
- **Agent** — chat with the **supervisor Agent Engine** through the Apigee
  `/ai-agents` gateway. The BFF manages sessions (keyed by the verified
  user), streams the turn, and renders the delegation trace live.
- **Admin** (visible to ACL admins) — edit the Firestore ACL (roles/users)
  the supervisor enforces, and list the Agent Registry.
- **Sessions** (admin) — the sessions navigator: every user's
  user → session → trace → sub-session tree with rollup counts, each trace
  linking into the Trace Explorer.
- **Trace** (admin) — the **Trace Explorer**: pick a recent transaction and
  watch it replay across the full 3-project topology, an animated packet
  dwelling on each hop in proportion to its measured latency, with a latency
  waterfall and the raw log records. Pure read-back of the four ai-logs — see
  [`../docs/OBSERVABILITY.md`](../docs/OBSERVABILITY.md#trace-explorer-admin-ui).

Architecture context: [`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md).

## Deploy

Cloud Run only. **`service.yaml`** is the single source of truth (env literals +
Secret Manager `secretKeyRef`s + ingress + Direct VPC egress + runtime SA),
applied by **`deploy_frontend.py`** (same `--check` / apply / `--apply` grammar
as the agents + apigee tooling):

```bash
python frontend/deploy_frontend.py --check    # live service vs service.yaml + never-public probe
python frontend/deploy_frontend.py            # apply CONFIG only, reuse image (confirms)
python frontend/deploy_frontend.py --build    # build+push a NEW image, then apply (confirms)
python frontend/deploy_frontend.py --apply    # ...either mode, no prompt (automation)
```

`service.yaml`'s image ends in `__TAG__` — the tool substitutes it (git short
sha on `--build`, the live tag otherwise). Never `gcloud run services replace`
the file directly.

The service is private (auth required); smoke-test over an authenticated tunnel:

```bash
gcloud run services proxy agent-bff --region=southamerica-west1 --port=8080
# open http://localhost:8080   (/healthz returns {"status":"ok"})
```

After every apply the tool also wires IAP (the IAP service agent's invoker
role + `iap_members` from `agents/runtime-manifest.yaml`); the OAuth consent
screen is the one manual prerequisite
([GETTING_STARTED](../docs/GETTING_STARTED.md#prerequisites)). Tokens in
`service.yaml` (`__AI_PROJECT__`, `__SUPERVISOR_ENGINE_ID__`, …) are rendered
at apply time — `AGENT_ENGINE_ID` resolves automatically to the newest live
supervisor engine.

## Configuration

Config is injected on Cloud Run from **`service.yaml`** — non-secret values as
env literals, the two Apigee keys as Secret Manager references. The essentials:

| Var | View | Purpose |
|---|---|---|
| `APIGEE_STREAM_URL` | Direct | The `/llm-stream/prompt` SSE gateway (the user's own key is sent per request from the UI) |
| `APIGEE_INSECURE_TLS` | Direct | Host-scoped TLS skip for the private gateway host |
| `AGENT_BASE_URL` | Agent | Apigee `/ai-agents` base (required) |
| `AGENT_ENGINE_ID` | Agent | Supervisor engine id — `__SUPERVISOR_ENGINE_ID__`, auto-resolved at apply |
| `APIGEE_AGENT_API_KEY` | Agent | The BFF app's key (Secret Manager `secretKeyRef: apigee-agent-key`) |
| `AGENT_INSECURE_TLS` | Agent | Host-scoped TLS skip for the private gateway host |
| `GOOGLE_CLOUD_PROJECT` / `AGENT_REGISTRY_LOCATION` | Admin | Firestore ACL + regional registry listing |
| `IAP_AUDIENCE` | all | IAP JWT verification audience (identity = verified email) |

The Agent view reaches the supervisor **only through Apigee**: the BFF posts
`…/ai-agents/<id>/streamQuery` with `x-api-key`, and Apigee injects the
Google SA token and rewrites to the full Vertex URL. The client sends just the
bare engine id (Apigee's target holds project/location).

## HTTP surface (BFF)

| Route | Body | Returns |
|---|---|---|
| `GET /api/config` | — | publishers, complexities, defaults, model matrix, `sub_agents` legend |
| `POST /api/chat` | `{history, publisher, complexity, use_cache}` | `{text, model, publisher, complexity, latency_ms, usage}` |
| `POST /api/chat/stream` | `{history, publisher, complexity, use_cache}` | `text/event-stream`: `text`* → `done` (or `error`). Gemini + Anthropic streaming testbed; needs `APIGEE_STREAM_URL`. |
| `POST /api/agent` | `{message, session_id}` | `{text, session_id, delegations, usage, latency_ms}` (buffered; fallback) |
| `POST /api/agent/stream` | `{message, session_id}` | `text/event-stream`: `session` → `delegation`* → `text`* → `done` (or `error`) |
| `GET /api/sessions` | — | `{sessions: [{session_id, last_update_time, title}]}` (current user, newest first) |
| `GET /api/sessions/{id}` | — | `{session_id, turns: [{role, text, delegations?}]}` (replayed history) |
| `DELETE /api/sessions/{id}` | — | `{deleted: id}` (async_delete_session) |
| `GET /api/admin/traces` | `?user=&view=&limit=` | `{traces: [{trace_id, user, view, total_ms, …}]}` (admin; recent, newest first) |
| `GET /api/admin/traces/{trace_id}` | — | `{summary, nodes, edges, spans, records, ops}` — the reconstructed flow (admin) |
| `GET /api/admin/sessions` | `?user=` | `{summary, users:[…]}` — user→session→trace→sub-session tree with rollup counts (admin) |

The Agent view sends `session_id: null` on the first turn; the BFF creates a
session and returns its id, which the browser echoes back on later turns. The
sidebar lists the user's sessions and loads a prior one's history on click.

## Files

| File | Purpose |
|---|---|
| `app/server.py` | FastAPI BFF — static files + `/api/{config,chat,agent,admin}` |
| `app/llm_client.py` | Direct view: the `/llm-stream` SSE call + per-publisher parsing |
| `app/agent_client.py` | Agent view: Agent Engine sessions + turn aggregation |
| `app/trace_explorer.py` | Trace + Sessions views: reads the four ai-logs, reconstructs one trace into a drawable flow (pure `reconstruct_flow`) and groups turns into the session tree (`session_tree`) |
| `static/index.html` | Five-view UI shell (Direct · Agent · Admin · Sessions · Trace; toolbar, session sidebar, stats, chat) |
| `static/app.js` | View state, send logic, staged handover reveal, session sidebar, markdown, Sessions navigator, trace replay |
| `static/style.css` | Layout + dark theme |
| `requirements.txt` | Python deps for the Cloud Run image. The BFF is a **Docker container**, so it uses pip/`requirements.txt` — intentionally *not* the `pyproject.toml`/`uv` the agents use (those deploy to Agent Engine, which reads `pyproject`). Different runtime, different dep tool. |

## Notes

- **Streaming**: the Agent view streams. `POST /api/agent/stream` returns
  `text/event-stream`; the BFF reads the Agent Engine JSON-object stream
  incrementally (`_drain_json`) and re-emits SSE frames. Requires Apigee
  `response.streaming.enabled=true` on both the Proxy and Target endpoints so
  the response isn't buffered. The buffered `POST /api/agent` remains as a
  fallback. (Apigee token-metering policies can't read a streamed body — usage
  comes from each event's `usage_metadata`, summed in the BFF.)
- **Handover reveal**: delegations render the instant the supervisor emits
  `transfer_to_agent` — before the sub-agent answers — then the answer text
  streams in. (No artificial delay anymore.)
- **Thinking panel**: model "thought" parts (with thinking models the specialist
  echoes the injected "For context:" scaffolding as thoughts) are emitted as a
  separate `{type:"thought"}` SSE event and rendered in a collapsed **💭
  Thinking** panel above the answer — never mixed into the response. Applies to
  both the live stream and replayed session history.
- **Debug inspector**: the header **Debug** toggle makes the streaming endpoints
  forward each raw upstream event (`{type:"raw"}`); the UI collects them and adds
  a collapsible "raw events (N)" panel under each streamed message — handy for
  verifying token-usage frames against the Apigee edge capture. Streaming only.
- **Direct-view streaming testbed** (Gemini + Anthropic): set `APIGEE_STREAM_URL`
  to a *separate* Apigee proxy with `response.streaming.enabled`. A "Stream"
  toggle then appears in the Direct toolbar; `POST /api/chat/stream` parses the
  upstream SSE and re-emits `text` deltas. Per-publisher shapes: Gemini
  (`:streamGenerateContent?alt=sse` — text + `usageMetadata` per event) and
  Anthropic (`:streamRawPredict`, body `stream:true` — `content_block_delta`
  text, with usage split across `message_start`/`message_delta`). Kept distinct
  from the live proxies; buffered `/api/chat` stays the fallback.
- **Identity** (`iap.py`): the BFF **verifies** the signed
  `X-Goog-IAP-JWT-Assertion` (signature + audience) and uses its email as the
  Agent Engine `user_id` — it does **not** trust the spoofable
  `X-Goog-Authenticated-User-*` headers. Missing/invalid assertion → **401**
  (fail-closed). This is the identity the supervisor's per-user ACL scopes on
  and the one the gateway binds per-user keys to.
- **Logging**: every turn logs to `front-ai-logs` with the minted
  `traceparent` — the start of the end-to-end trace
  ([`../docs/OBSERVABILITY.md`](../docs/OBSERVABILITY.md)).
