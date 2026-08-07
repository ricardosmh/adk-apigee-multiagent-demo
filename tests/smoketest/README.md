# Apigee LLM smoke test

Minimal **live** check that the Apigee-fronted Gemini endpoint is correctly
exposed — useful when bisecting gateway problems from agent problems.

It runs two stages:

| Stage | What it does | Why |
|---|---|---|
| `RAW` | Plain HTTPS POST to `{proxy}/{version}/models/{model}:generateContent` with only `x-api-key` | Isolates the proxy/path/key/cert — no SDK in the way |
| `ADK` | Same call via `google.adk.models.ApigeeLlm` (Gemini mode) | Validates the exact wrapper the agents use |

## Setup

```bash
cd tests/smoketest
cp .env.example .env            # then paste your APIGEE_API_KEY
uv venv && uv pip install "google-adk>=1.27.0" python-dotenv
```

(or `python -m venv .venv && .venv/bin/pip install "google-adk>=1.27.0" python-dotenv`)

## Run

```bash
python smoketest.py             # both stages
python smoketest.py --mode raw  # just the raw HTTPS probe (fastest to debug)
python smoketest.py --mode adk  # just the ApigeeLlm path
```

`PASS` means the endpoint answered with a Gemini `generateContent` body.

## If it fails

- **RAW returns 404 / malformed-path** — the proxy's target path and the
  incoming `/{version}/models/...` suffix collide. Try
  `APIGEE_LLM_API_VERSION=` (empty) to see if the proxy expects no version
  segment, or fix the proxy to rebuild the GEAP path.
- **RAW 200 but ADK fails on upstream auth** — the proxy is forwarding the
  SDK's `x-goog-api-key` upstream; strip it in the proxy (`am-clean-headers`).
- **TLS errors** — keep `APIGEE_LLM_INSECURE_TLS=true` for the private gateway host.
