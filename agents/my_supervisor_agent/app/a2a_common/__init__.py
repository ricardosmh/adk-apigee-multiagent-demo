"""Shared scaffolding for the A2A multi-agent system.

CANONICAL SOURCE. This package is the single source of truth for the mechanical
(non-behavioral) scaffolding shared across the supervisor and the specialists:

  * model.py      — Apigee-fronted Gemini model + host-scoped TLS skip (all 5)
  * specialist.py — build a specialist Agent + its A2A server runtime (3 agents)
  * client.py     — build streaming RemoteA2aAgent proxies + A2A auth (supervisor)

DEPLOY NOTE — why this is *vendored*. Each engine deploys with
``agent_engines.create(extra_packages=["./app"])`` from its own directory, which
bundles only that agent's ``app/`` dir. A repo-root sibling package would NOT
ship. So this package is copied into each agent as ``app/a2a_common/`` by
``sync_common.sh`` and imported as ``from app.a2a_common import ...``. Edit ONLY
this canonical copy, then re-run ``./sync_common.sh`` — never hand-edit a
vendored copy (they are overwritten).
"""
from .model import build_apigee_model, configure_genai_env, install_scoped_tls_skip

__all__ = [
    "build_apigee_model",
    "configure_genai_env",
    "install_scoped_tls_skip",
]
