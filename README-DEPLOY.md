# Deploying this webinar deck — drop-in instructions

This zip is a full replacement for the `webinar/` directory of
`ricardosmh/adk-apigee-multiagent-demo`, plus the Pages workflow.

## Steps

1. **Replace the folder** — delete the repo's existing `webinar/` directory and
   unzip this archive at the repo root so it recreates `webinar/`.
   Notable changes vs the old version:
   - `css/theme.css` is new (replaces `css/theme/black.css`, which had broken
     font references — you can delete the `css/theme/` folder).
   - `js/mermaid.min.js` (3.2 MB) is no longer used — delete it.
   - `img/` keeps the same filenames; `architecture.svg` and both `.gif`
     captures are now actually used by the deck.

2. **Delete the duplicate Pages workflow** — remove
   `.github/workflows/static.yml`. It deploys the *entire repository* to Pages
   and races `pages.yml` (both fire on every push to main, last writer wins).
   Keep only `pages.yml` (a copy is included here; it publishes just
   `webinar/`). With `static.yml` gone, the root `index.html` redirect shim is
   also unnecessary — you can delete it too.

3. **Commit and push to `main`.** The `pages.yml` workflow deploys to
   `https://ricardosmh.github.io/adk-apigee-multiagent-demo/`.

4. **Verify locally first** (optional but 30 seconds):
   `python3 -m http.server -d webinar 8000` → open `http://localhost:8000`.

## What changed in the deck itself

- Presentation-scale typography (reveal's 42px base, em-sized throughout).
- Persistent SCQA rail at the top: Situation → Complication → Question →
  Answer, with the active phase lit and click-to-jump.
- Interactive architecture slide: the full draw.io SVG with drag-pan,
  scroll-zoom, double-click dive-in, and a Fit/reset button.
- Screenshots on 6 slides, including both animated GIF captures (the Trace
  Explorer demo slide animates on screen).
- Per-phase background tints, progressive card reveals, stat chips, and a
  gradient cover.
- GEAP terminology (Gemini Enterprise Agent Platform, "formerly Vertex AI" on
  first mention); API resource names left untouched.
- Speaker notes on all 24 slides, written for dictation (press `S`).
- Bottom-left HUD on every slide: a clickable GitHub-repo button and a
  fullscreen toggle.
- Both animated GIF captures in use (Trace Explorer on the Demo 1 slide,
  Direct view on the governance slide).
- Official product logos (`img/logos/`) on the building-blocks slide: GEAP,
  ADK, A2A, Apigee, Cloud Run, Cloud SQL.
- The deck canvas now sits below the SCQA rail, so no slide content can
  render underneath it.
