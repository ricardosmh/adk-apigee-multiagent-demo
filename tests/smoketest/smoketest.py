#!/usr/bin/env python3
"""Local smoke test for the Apigee-fronted Gemini LLM endpoint.

Validates the endpoint two ways before we refactor any agent onto it:

  RAW  — a plain HTTPS POST to
         {proxy}/{version}/models/{model}:generateContent
         sending ONLY the Apigee `x-api-key`. Shows the exact status + body the
         proxy returns, so a path/cert/key problem is obvious.

  ADK  — the identical call routed through google.adk.models.ApigeeLlm, i.e.
         the exact wrapper the supervisor + sub-agents will use.

The only credential needed is the Apigee `x-api-key`. In Gemini mode the genai
SDK also sends `x-goog-api-key=$GOOGLE_API_KEY`; that can be any dummy because
the proxy injects the real Vertex token upstream (and should strip the header).

Config comes from .env in this directory — see .env.example.

    python smoketest.py            # run both stages
    python smoketest.py --mode raw # only the raw HTTPS probe
    python smoketest.py --mode adk # only the ApigeeLlm probe
"""
import argparse
import asyncio
import json
import os
import ssl
import sys
import urllib.request
from urllib.error import HTTPError, URLError

from dotenv import load_dotenv

load_dotenv()

PROXY = os.environ.get("APIGEE_LLM_PROXY_URL", "").rstrip("/")
KEY = os.environ.get("APIGEE_API_KEY") or os.environ.get("APIGEE_LLM_API_KEY", "")
MODEL = os.environ.get("APIGEE_LLM_MODEL", "gemini-3.1-flash-lite")
# The API-version path segment the SDK inserts: {proxy}/{version}/models/...
# Set empty to omit it entirely (depends on how the proxy base path is set up).
VERSION = os.environ.get("APIGEE_LLM_API_VERSION", "v1beta").strip("/")
# nip.io fronts a cert that doesn't validate — mirror the frontend and skip
# verification for this local test. Set to "false" once on a real hostname.
INSECURE = os.environ.get("APIGEE_LLM_INSECURE_TLS", "true").lower() in ("1", "true", "yes")
PROMPT = os.environ.get(
    "APIGEE_LLM_PROMPT", "Reply with exactly this text: Apigee LLM endpoint OK"
)


def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if INSECURE:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _summarize(raw: str) -> str:
    """Pull text + usage out of a Gemini generateContent response, else raw."""
    try:
        j = json.loads(raw)
    except json.JSONDecodeError:
        return raw[:1200]
    text = "".join(
        part.get("text", "")
        for cand in j.get("candidates", []) or []
        for part in cand.get("content", {}).get("parts", []) or []
    )
    if text:
        return f"  text : {text!r}\n  usage: {j.get('usageMetadata')}"
    return json.dumps(j, indent=2)[:1200]


def raw_probe() -> bool:
    path = f"/{VERSION}/models/{MODEL}:generateContent" if VERSION else f"/models/{MODEL}:generateContent"
    url = PROXY + path
    body = {"contents": [{"role": "user", "parts": [{"text": PROMPT}]}]}
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-api-key": KEY},
        method="POST",
    )
    print(f"[RAW] POST {url}")
    print(f"[RAW] headers: x-api-key=<{len(KEY)} chars>  (TLS verify={'off' if INSECURE else 'on'})")
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx(), timeout=90) as resp:
            data = resp.read().decode("utf-8", "replace")
            print(f"[RAW] HTTP {resp.status} ✓")
            print(_summarize(data))
            return True
    except HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        print(f"[RAW] HTTP {e.code} ✗")
        print(detail[:1500])
        return False
    except URLError as e:
        print(f"[RAW] unreachable ✗: {e.reason}")
        return False


def _patch_httpx_insecure() -> None:
    """ApigeeLlm exposes no TLS knob; force httpx clients to skip verify."""
    import httpx

    for cls in (httpx.AsyncClient, httpx.Client):
        original = cls.__init__

        def make(orig):
            def __init__(self, *args, **kwargs):
                kwargs.setdefault("verify", False)
                return orig(self, *args, **kwargs)

            return __init__

        cls.__init__ = make(original)


async def adk_probe() -> bool:
    os.environ.setdefault("GOOGLE_API_KEY", "unused-dummy-key")
    if INSECURE:
        _patch_httpx_insecure()

    from google.adk.models.apigee_llm import ApigeeLlm
    from google.adk.models.llm_request import LlmRequest
    from google.genai import types

    # Force Gemini provider mode (no ADC, no upstream bearer from the client).
    model_str = f"apigee/gemini/{VERSION}/{MODEL}" if VERSION else f"apigee/gemini/{MODEL}"
    print(f"[ADK] ApigeeLlm(model={model_str!r}, proxy_url={PROXY!r})")
    llm = ApigeeLlm(
        model=model_str,
        proxy_url=PROXY,
        custom_headers={"x-api-key": KEY},
    )
    req = LlmRequest(
        model=model_str,
        contents=[types.Content(role="user", parts=[types.Part(text=PROMPT)])],
    )
    got = False
    try:
        async for resp in llm.generate_content_async(req, stream=False):
            got = True
            text = None
            if resp.content and resp.content.parts:
                text = "".join(p.text or "" for p in resp.content.parts)
            print(f"[ADK] text : {text!r}")
            if resp.usage_metadata:
                print(f"[ADK] usage: {resp.usage_metadata}")
    except Exception as e:  # noqa: BLE001 — surface whatever the wrapper raised
        print(f"[ADK] error ✗: {type(e).__name__}: {e}")
        return False
    if got:
        print("[ADK] ✓")
    return got


def main() -> None:
    if not PROXY or not KEY:
        print("Set APIGEE_LLM_PROXY_URL and APIGEE_API_KEY first (see .env.example).")
        sys.exit(2)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["raw", "adk", "both"], default="both")
    args = parser.parse_args()

    ok = True
    if args.mode in ("raw", "both"):
        print("=" * 64)
        ok = raw_probe() and ok
    if args.mode in ("adk", "both"):
        print("=" * 64)
        ok = asyncio.run(adk_probe()) and ok
    print("=" * 64)
    print("RESULT:", "PASS ✓" if ok else "FAIL ✗")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
