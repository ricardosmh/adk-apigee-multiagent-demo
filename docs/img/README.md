# docs/img/ — README media

The images the [root README](../../README.md) and deep-dive docs embed —
captured from a live deploy. To refresh any of them, overwrite the file
keeping the exact name so no doc needs edits.

## Inventory

| File | Embedded in | Shows |
|---|---|---|
| `trace-explorer.gif` | README (hero) | Trace view mid-replay: animated packet on the 3-project topology, latency-by-layer donut, waterfall, log records. ~31 s capture, 12 fps |
| `direct-view.gif` | README strip | Direct view: Gemini answer → publisher flip to Anthropic (model chips visible) → Claude critique, streaming. 2× speed, 8 fps |
| `agent-view.png` | README strip | Agent view mid-turn: `transfer_to_agent` chips revealed, specialist answer + per-turn token/latency chips |
| `sessions-view.png` | *(spare)* | Admin Sessions: user→session→trace tree expanded with rollup counts |
| `monitoring-dashboard.png` | README strip | Cloud Monitoring AI Analytics dashboard: calls/min + tokens by model, latency p95 by type |
| `apigee-reports.png` | README strip | Apigee Analytics: `dc_tokenCount`/`dc_inputToken` by `dc_model` on the unified endpoint |
| `apigee-reports-by-agent.png` | [OBSERVABILITY.md](../OBSERVABILITY.md) | Apigee Analytics: tokens by developer app (per-agent attribution) on the model proxy |
| `architecture.svg` | README + [ARCHITECTURE.md](../ARCHITECTURE.md) | The detailed draw.io topology: 3 projects + tenant projects, both PSC chains, engine NICs, DNS peering. Edit in draw.io only; root `viewBox` is hand-tightened to the content box |

## Capture rules (for refreshes)

- **No real user emails in frame.** Demo identities (`*.altostrat.com`) are fine;
  crop/blur anything else — these ship with the repo.
- Stills: ~1200–1440 px window width, PNG, same filenames.
- GIFs: keep each **under ~10 MB** (README load time) — 1000–1200 px wide,
  8–12 fps, palette-optimized (`ffmpeg palettegen/paletteuse`), speed up long
  captures rather than cutting content.
- Drive a few turns first (Direct + Agent, more than one specialist) so charts
  and traces have real data — the analytics surfaces are forward-only and start
  empty ([analytics/README.md](../../analytics/README.md)).
