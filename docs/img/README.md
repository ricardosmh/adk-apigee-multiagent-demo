# docs/img/ — README screenshots

The images the [root README](../../README.md) embeds. **The PNGs currently
here are generated placeholders** (dark frames with a label) — overwrite each
with a real capture from a live deploy, keeping the exact filename so the
README needs no edit.

## Shot list

| File | What to capture | State to show |
|---|---|---|
| `trace-explorer.png` | **Trace view** (admin) — the hero shot | Mid-replay: the animated packet dwelling on a hop across the 3-project topology, latency waterfall visible below |
| `agent-view.png` | **Agent view** | Mid-turn: delegation chip(s) already revealed (`transfer_to_agent` fired), answer text streaming in |
| `direct-view.png` | **Direct view** | Model picker open, showing both Gemini and Claude options |
| `sessions-view.png` | **Sessions view** (admin) | The user→session→trace tree expanded with rollup counts |
| `monitoring-dashboard.png` | Cloud Monitoring → the analytics dashboard | After driving traffic: tokens-by-model, calls/min and latency tiles populated |
| `apigee-reports.png` | Apigee console → **Analytics → Custom Reports** | An `AI - Tokens by …` report run over real traffic (chart + table visible) |

## Capture rules

- **No real user emails in frame.** Use the demo end users, or crop/blur any
  identity chips — these shots ship with the repo.
- Capture at a common width (~1200–1440 px window) so the strip renders evenly;
  PNG format, same filenames as above.
- Drive a few turns first (Direct + Agent, more than one specialist) so charts
  and traces have real data — the analytics surfaces are forward-only and start
  empty ([analytics/README.md](../../analytics/README.md)).
