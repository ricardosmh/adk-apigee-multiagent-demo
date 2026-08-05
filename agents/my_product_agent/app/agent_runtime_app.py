"""Runtime wrapper for the product specialist (A2A server).

Publishes the agent over A2A with ``streaming=True`` so a new-impl supervisor
client receives token-level SSE. All wiring lives in ``app.a2a_common``.
"""
from dotenv import load_dotenv

from app.a2a_common.specialist import build_a2a_server_runtime
from app.agent import app as adk_app

load_dotenv()

agent_runtime = build_a2a_server_runtime(adk_app, streaming=True)
