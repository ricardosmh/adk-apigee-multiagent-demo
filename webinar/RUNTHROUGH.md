# Webinar run-through — Governing Enterprise Multi-Agent Systems on Google Cloud

24 slides · target 40 min talk + 2 live demo cut-ins. Speaker notes are the
dictation script: press **S** for the speaker view (notes + timer + next slide).

## Navigation

| Key | Action |
|---|---|
| `→` / `←` / swipe | Next / previous slide (also steps card fragments) |
| `S` | Speaker view with notes and timer |
| `Esc` / `O` | Slide overview grid |
| `F` | Fullscreen |
| `#/12` in the URL | Deep-link to slide 12 |
| Click a phase in the top rail | Jump to that phase's first slide |

On the **architecture slide (10)**: drag to pan, scroll to zoom, double-click to
dive into a region, **Fit** button to reset. Practice the "AI project → Apigee →
backend" sweep once before going live.

## Slide list & timing

| # | Slide | Phase | Time |
|---|---|---|---|
| 1 | Cover | — | 1 min |
| 2 | Chatbots → multi-agent networks | S | 1.5 |
| 3 | GEAP / A2A / Apigee / Cloud Run building blocks | S | 1.5 |
| 4 | What we built (agent-view screenshot) | S | 1.5 |
| 5 | Ungoverned access + zero attribution | C | 2 |
| 6 | Black-box delegation chain | C | 2 |
| 7 | Four security holes | C | 2 |
| 8 | Coupling, drift, undocumented edges | C | 1.5 |
| 9 | **The Question** (pause here) | Q | 1 |
| 10 | **Interactive architecture diagram** | A | 4 |
| 11 | Three projects, one concern each | A | 2 |
| 12 | Governance table (5 base paths) | A | 2.5 |
| 13 | Keys↔identity, quotas (direct-view) | A | 2 |
| 14 | Specialists: small on purpose | A | 1.5 |
| 15 | A2A streaming + registry self-heal | A | 2 |
| 16 | Fail-closed ACL | A | 1.5 |
| 17 | Zero-trust networking, passwordless DB | A | 2 |
| 18 | Four logs, one traceparent | A | 2 |
| 19 | **DEMO 1: Trace Explorer** (GIF fallback on slide) | A | 3–4 live |
| 20 | Sessions navigator | A | 1 |
| 21 | **DEMO 2: token analytics** | A | 2–3 live |
| 22 | Manifests + check/apply + guard tests | A | 2 |
| 23 | Six patterns worth lifting | A | 1.5 |
| 24 | Close: "Yes — and you can rebuild it" + Q&A | A | 1 |

## Demo cut-ins

- **Slide 19** — switch to the live Trace Explorer. The slide itself shows the
  animated GIF capture, so if the live environment misbehaves you can present
  from the slide alone.
- **Slide 21** — switch to Apigee custom reports + the Cloud Monitoring
  dashboard. Drive a few turns beforehand so charts have data (they are
  forward-only and start empty).

## Before webinar day

- [ ] Re-capture any screenshot that changed in the product UI (same filenames
      in `img/`, then hard-refresh).
- [ ] Run one full pass in speaker view against the timer (~35 min without
      demos at normal pace).
- [ ] Load the deck once on the presentation machine while online (all assets
      are local — after the first load it works offline).
