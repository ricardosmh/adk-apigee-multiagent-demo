"""Make the repo-root packages importable for tests.

Run from the repo root with a venv that has the agent deps installed:
    pytest tests/
Tests that need heavy deps (google-adk, a2a-sdk) skip themselves when those
aren't importable, so the pure-BFF tests still run in a minimal env.
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "agents", ROOT / "agents" / "provision", ROOT / "frontend",
          ROOT / "frontend" / "app", ROOT / "apigee" / "provision", ROOT / "services" / "provision",
          ROOT / "analytics"):
    sp = str(p)
    if sp not in sys.path:
        sys.path.insert(0, sp)
