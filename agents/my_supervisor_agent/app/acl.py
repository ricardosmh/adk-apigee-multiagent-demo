"""Per-user sub-agent ACL (production) — scope which specialists a session may use.

Design is the one proven by a standalone ACL spike (run 2026-06-05, since removed):

  * Enforcement lives ENTIRELY in the supervisor (the sole entrypoint; sub-agents
    are IAM-locked to its runner SA). No identity is propagated over A2A.
  * HIDING — `before_agent_acl` trims THIS request's `agent.sub_agents` to the
    user's allowed set. The spike confirmed the per-request agent is a deep copy
    (`SHARED=False`, shared `root_agent.sub_agents` never changed even with two
    users on one worker), so this is race-free. ADK rebuilds the transfer tool +
    "agents you can transfer to" instruction from the reduced set, so the model
    never *sees* a disallowed specialist.
  * HARD BLOCK — `before_tool_acl` denies any `transfer_to_agent` whose target is
    not in the allowed set (fail-closed safety net; covers a hallucinated transfer
    or a newly discovered agent). Policed set is DYNAMIC — anything not allowed —
    not a hardcoded list.

Master switch: ACL_ENABLED (default OFF). While off, every hook is an immediate
no-op — production behaviour is unchanged. Flip it on once IAP (3c) gives a real
`user_id`.

Identity key: the IAP-VERIFIED email, lowercased (the BFF's iap.resolve_user_id
sets it as the Agent Engine session user_id) — operator-legible ACL docs.

Store (ACL_BACKEND, default firestore):
  * firestore — roles indirection, read PER TURN (no cross-turn cache => instant
    revoke). acl_users/{email}.roles  ∪  acl_roles/{role}.agents. A role whose
    agents contain "*" grants ALL agents (e.g. the seeded `admin` role, which
    also carries is_admin=true for the BFF management view — ignored here).
    Provisioned/seeded by agents/provision/provision_agents.py (manifest acl:).
  * fake — ACL_FAKE_JSON = {"<email>": ["order_agent", ...]} (test/injection
    harness; no Firestore/IAM needed).

Fail-closed everywhere: ACL on + unknown user / backend error / no doc => empty
set => no specialists. A lookup failure must never grant access. Per-invocation
memoization avoids redundant reads within a single turn while staying instant
across turns.
"""
from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)

ALLOW_ALL = object()       # sentinel: "no filtering" (ACL_ALLOW_ALL=1, testing only)
_MISSING = object()


def acl_enabled() -> bool:
    return os.environ.get("ACL_ENABLED", "0").lower() in ("1", "true", "yes")


# ── Identity ─────────────────────────────────────────────────────────────────
def user_id_from_context(ctx) -> str | None:
    """Application user_id from a CallbackContext/ToolContext (the Agent Engine
    session user the BFF set — the IAP-verified EMAIL in prod). Normalized to
    lowercase so doc keys match regardless of casing. Defensive private access."""
    ic = getattr(ctx, "_invocation_context", None)
    session = getattr(ic, "session", None)
    uid = getattr(session, "user_id", None)
    if uid is None:
        logger.warning("ACL: could not read user_id from %s", type(ctx).__name__)
        return None
    return uid.strip().lower() if isinstance(uid, str) else uid


# ── Store ────────────────────────────────────────────────────────────────────
def _resolve_fake(user_id: str | None):
    if os.environ.get("ACL_ALLOW_ALL") == "1":
        return ALLOW_ALL
    try:
        table = json.loads(os.environ.get("ACL_FAKE_JSON", "{}"))
    except json.JSONDecodeError as e:
        logger.error("ACL: ACL_FAKE_JSON invalid (%s) — failing closed", e)
        return set()
    if not user_id or user_id not in table:
        return set()
    agents = set(table[user_id] or [])
    return ALLOW_ALL if "*" in agents else agents


def _resolve_firestore(user_id: str | None):
    if not user_id:
        return set()
    try:
        from google.cloud import firestore  # lazy: only for this backend

        db = firestore.Client()
        user_doc = db.collection("acl_users").document(user_id).get()
        if not user_doc.exists:
            logger.info("ACL[firestore]: no acl_users/%s — fail-closed", user_id)
            return set()
        roles = user_doc.to_dict().get("roles", []) or []
        allowed: set[str] = set()
        for role in roles:
            role_doc = db.collection("acl_roles").document(role).get()
            if role_doc.exists:
                allowed.update(role_doc.to_dict().get("agents", []) or [])
        if "*" in allowed:
            # Wildcard role (e.g. admin): every agent, including ones added to
            # the registry after this role was written.
            return ALLOW_ALL
        return allowed
    except Exception as e:  # noqa: BLE001 — any failure fails CLOSED
        logger.exception("ACL[firestore]: read failed (%s) — fail-closed", e)
        return set()


def resolve_allowed_agents(user_id: str | None):
    backend = os.environ.get("ACL_BACKEND", "firestore").lower()
    if backend == "fake":
        return _resolve_fake(user_id)
    return _resolve_firestore(user_id)


def _resolve_for_context(ctx):
    """Resolve the allowed set ONCE per invocation (memoized on the per-request
    invocation context). Fresh each turn => instant revoke; cheap within a turn."""
    ic = getattr(ctx, "_invocation_context", None)
    if ic is not None:
        cached = getattr(ic, "_acl_allowed", _MISSING)
        if cached is not _MISSING:
            return cached
    allowed = resolve_allowed_agents(user_id_from_context(ctx))
    if ic is not None:
        try:
            ic._acl_allowed = allowed
        except Exception:  # noqa: BLE001 — best-effort memo
            pass
    return allowed


# ── Hide: before_agent_callback ──────────────────────────────────────────────
# Set in agent.py so we can refuse to mutate the shared object if a future ADK
# stops deep-copying (defense in depth; the hard block still enforces).
MODULE_ROOT_AGENT = None


def before_agent_acl(callback_context) -> None:
    if not acl_enabled():
        return None
    ic = getattr(callback_context, "_invocation_context", None)
    agent = getattr(ic, "agent", None)
    if agent is None:
        return None

    allowed = _resolve_for_context(callback_context)
    if allowed is ALLOW_ALL:
        return None

    current = list(getattr(agent, "sub_agents", []) or [])
    hidden = [a for a in current if getattr(a, "name", None) not in allowed]
    if not hidden:
        return None

    if MODULE_ROOT_AGENT is not None and agent is MODULE_ROOT_AGENT:
        # Per-request isolation not holding (ADK regression?) — don't race the
        # shared object; the hard block still enforces.
        logger.warning("ACL: per-request agent is the shared root — skipping "
                       "sub_agents trim, relying on before_tool hard block")
        return None

    agent.sub_agents = [a for a in current if getattr(a, "name", None) in allowed]
    logger.info(
        "ACL audit: user=%s allowed=%s hid=%s remaining=%s",
        user_id_from_context(callback_context), sorted(allowed),
        [getattr(a, "name", "?") for a in hidden],
        [getattr(a, "name", "?") for a in agent.sub_agents],
    )
    return None


# ── Hard block: before_tool_callback ─────────────────────────────────────────
def before_tool_acl(tool, args, tool_context):
    if not acl_enabled():
        return None
    if getattr(tool, "name", None) != "transfer_to_agent":
        return None

    allowed = _resolve_for_context(tool_context)
    if allowed is ALLOW_ALL:
        return None

    target = (args or {}).get("agent_name")
    # Dynamic policed set: deny any transfer target the user isn't allowed.
    if target and target not in allowed:
        logger.warning(
            "ACL DENY: user=%s blocked transfer_to_agent(%s); allowed=%s",
            user_id_from_context(tool_context), target, sorted(allowed),
        )
        return {
            "status": "denied",
            "reason": (
                f"Access to '{target}' is not permitted for this user. "
                "Answer directly or ask the user to contact an administrator."
            ),
        }
    return None
