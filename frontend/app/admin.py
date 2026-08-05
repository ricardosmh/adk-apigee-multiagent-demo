"""ACL administration for the BFF — the backend of the admin view (phase 2).

Manages the same Firestore documents the supervisor's ACL enforces
(acl_users/{email}, acl_roles/{role}) and lists Agent Registry so the UI can
offer real agent names. Design points:

  * EVERY endpoint is gated server-side on the caller's is_admin — resolved
    fresh from Firestore per request (same instant-revoke property as the
    supervisor's per-turn read). Hiding the frontend panel is UX, not security.
  * The caller identity is the IAP-verified email (iap.resolve_user_id) —
    never a client-supplied value.
  * Lockout guard: any mutation that would leave ZERO admin users is refused —
    you cannot delete/demote the last admin (recoverable only via Firestore
    console otherwise).
  * Storage goes through AclStore (thin Firestore wrapper) so the decision
    logic is unit-testable with an in-memory stand-in.

Sync Firestore/google-auth calls — callers run these via asyncio.to_thread.
"""
from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

_ROLE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class AdminError(Exception):
    """Caller is not an admin (→ 403)."""


class ValidationError(Exception):
    """Bad input or a refused mutation (→ 400)."""


# ── Store (thin Firestore wrapper; duck-typed for tests) ─────────────────────
class AclStore:
    """acl_users/acl_roles access. Lazily builds the Firestore client so the
    module imports without google-cloud-firestore (tests, IAP-off dev)."""

    def __init__(self):
        from google.cloud import firestore  # lazy

        self._db = firestore.Client()

    def roles(self) -> dict[str, dict]:
        return {d.id: (d.to_dict() or {}) for d in self._db.collection("acl_roles").stream()}

    def users(self) -> dict[str, dict]:
        return {d.id: (d.to_dict() or {}) for d in self._db.collection("acl_users").stream()}

    def set_role(self, name: str, data: dict) -> None:
        self._db.collection("acl_roles").document(name).set(data)

    def delete_role(self, name: str) -> None:
        self._db.collection("acl_roles").document(name).delete()

    def set_user(self, email: str, data: dict) -> None:
        self._db.collection("acl_users").document(email).set(data)

    def delete_user(self, email: str) -> None:
        self._db.collection("acl_users").document(email).delete()


def store() -> AclStore:
    return AclStore()


# ── Decision logic (pure over the store — unit-tested) ───────────────────────
def admin_emails(roles: dict[str, dict], users: dict[str, dict]) -> set[str]:
    admin_roles = {n for n, r in roles.items() if r.get("is_admin")}
    return {e for e, u in users.items() if admin_roles & set(u.get("roles") or [])}


def is_admin(st, email: str | None) -> bool:
    """Fail-closed: no identity / no docs / any error => not admin."""
    if not email:
        return False
    try:
        return email.strip().lower() in admin_emails(st.roles(), st.users())
    except Exception:  # noqa: BLE001
        logger.exception("admin check failed — failing closed")
        return False


def require_admin(st, email: str | None) -> str:
    if not is_admin(st, email):
        raise AdminError("admin access required")
    return email.strip().lower()


def _guard_lockout(roles: dict, users: dict, what: str) -> None:
    """Refuse any post-mutation state with zero admin users."""
    if not admin_emails(roles, users):
        raise ValidationError(f"refused: {what} would leave no admin users")


def list_acl(st) -> dict:
    return {"roles": st.roles(), "users": {e: (u.get("roles") or []) for e, u in st.users().items()}}


def put_role(st, name: str, agents: list, is_admin_flag: bool) -> None:
    name = (name or "").strip().lower()
    if not _ROLE_RE.match(name):
        raise ValidationError("role name must be a lowercase slug (a-z0-9_-)")
    if not isinstance(agents, list) or not all(isinstance(a, str) and a.strip() for a in agents):
        raise ValidationError("agents must be a list of agent names ('*' allowed)")
    roles, users = st.roles(), st.users()
    roles[name] = {"agents": sorted(set(agents)), "is_admin": bool(is_admin_flag)}
    _guard_lockout(roles, users, f"updating role '{name}'")
    st.set_role(name, roles[name])


def delete_role(st, name: str) -> None:
    name = (name or "").strip().lower()
    roles, users = st.roles(), st.users()
    if name not in roles:
        raise ValidationError(f"role '{name}' does not exist")
    roles.pop(name)
    _guard_lockout(roles, users, f"deleting role '{name}'")
    st.delete_role(name)


def put_user(st, email: str, role_names: list) -> None:
    email = (email or "").strip().lower()
    if not _EMAIL_RE.match(email):
        raise ValidationError("user key must be an email address")
    roles, users = st.roles(), st.users()
    unknown = [r for r in (role_names or []) if r not in roles]
    if unknown:
        raise ValidationError(f"unknown role(s): {unknown}")
    users[email] = {"roles": sorted(set(role_names or []))}
    _guard_lockout(roles, users, f"updating user '{email}'")
    st.set_user(email, users[email])


def delete_user(st, email: str) -> None:
    email = (email or "").strip().lower()
    roles, users = st.roles(), st.users()
    if email not in users:
        raise ValidationError(f"user '{email}' does not exist")
    users.pop(email)
    _guard_lockout(roles, users, f"deleting user '{email}'")
    st.delete_user(email)


# ── Agent Registry (the UI's agent list) ─────────────────────────────────────
def list_registry_agents() -> list[dict]:
    """[{name, resource}] from Agent Registry (v1alpha REST, ADC token) — the
    display names are exactly what acl_roles.agents / the supervisor route on."""
    import google.auth
    import google.auth.transport.requests
    import httpx

    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    location = os.environ.get("AGENT_REGISTRY_LOCATION", "us-central1")
    creds, adc_project = google.auth.default()
    project = project or adc_project
    if not project:
        raise ValidationError("GOOGLE_CLOUD_PROJECT is not configured")
    creds.refresh(google.auth.transport.requests.Request())
    url = (f"https://agentregistry.googleapis.com/v1alpha/projects/{project}"
           f"/locations/{location}/agents")
    resp = httpx.get(url, headers={"Authorization": f"Bearer {creds.token}"},
                     params={"pageSize": 100}, timeout=15)
    resp.raise_for_status()
    return [{"name": a.get("displayName"), "resource": a.get("name")}
            for a in resp.json().get("agents", []) if a.get("displayName")]
