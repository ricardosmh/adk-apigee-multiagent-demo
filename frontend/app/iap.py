"""IAP identity for the agent path — verify the IAP JWT, extract the user_id.

IAP protects the whole Cloud Run service at the edge, so every request that
reaches the BFF already carries a signed `X-Goog-IAP-JWT-Assertion`. The BFF
still must **verify** it (signature + audience) and uses the verified `email`
claim (lowercased) as the Agent Engine `user_id` — the ACL keys on email so
the documents are operator-legible. Do NOT trust the unsigned
`X-Goog-Authenticated-User-*` headers — they're spoofable if a request ever
bypasses IAP.

Toggle (so the BFF works before and after 3c without a code change):
  * IAP off (default, pre-3c): `resolve_user_id()` returns None → `agent_client`
    falls back to `AGENT_DEV_USER_ID`. Identical to today.
  * IAP on (`IAP_AUDIENCE` set, or `IAP_ENABLED=1`): verify the assertion and
    return its `sub`. A missing/invalid assertion raises `IdentityError` (the
    endpoint turns it into a 401). **Fail-closed** — never fall back to the dev
    user once IAP is enabled.

`IAP_AUDIENCE` is the audience string IAP signs for this resource — read it from
the IAP settings (Console → Security → IAP) and set it in `service.yaml`.
"""
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

IAP_AUDIENCE = os.environ.get("IAP_AUDIENCE", "").strip()
# On when explicitly enabled, or implicitly once an audience is configured.
IAP_ENABLED = os.environ.get(
    "IAP_ENABLED", "1" if IAP_AUDIENCE else "0"
).lower() in ("1", "true", "yes")

_JWT_HEADER = "x-goog-iap-jwt-assertion"
_IAP_CERTS_URL = "https://www.gstatic.com/iap/verify/public_key"
_CERTS_TTL_SECONDS = 3600  # IAP keys rotate slowly; cache an hour to avoid per-request egress.


class IdentityError(Exception):
    """IAP is enabled but the request carries no valid verified identity."""


# IAP public keys, cached process-wide. The old code called id_token.verify_token,
# which RE-FETCHES these certs on EVERY request; with `vpc-access-egress: all-traffic`
# that synchronous gstatic GET is slow, and doing it inline in an async endpoint
# blocked the event loop long enough that the /healthz liveness probe timed out and
# Cloud Run killed the instance (the agent-path 503s). Cache the certs and verify
# with pure-crypto jwt.decode (exactly what verify_token does internally) so a turn
# does no network I/O once the certs are warm.
_certs_lock = threading.Lock()
_certs_cache: dict = {"certs": None, "exp": 0.0}


def _iap_certs() -> dict:
    """Return the IAP signing certs, refreshing from gstatic at most once per TTL."""
    now = time.time()
    if _certs_cache["certs"] is not None and now < _certs_cache["exp"]:
        return _certs_cache["certs"]
    with _certs_lock:
        if _certs_cache["certs"] is not None and time.time() < _certs_cache["exp"]:
            return _certs_cache["certs"]
        import requests  # lazy: only needed when IAP is enabled
        resp = requests.get(_IAP_CERTS_URL, timeout=10)
        resp.raise_for_status()
        _certs_cache["certs"] = resp.json()
        _certs_cache["exp"] = time.time() + _CERTS_TTL_SECONDS
        return _certs_cache["certs"]


def resolve_user_id(headers) -> str | None:
    """Return the Agent Engine `user_id` for this request.

    `headers` is a case-insensitive mapping (FastAPI `request.headers`).
    IAP off → None (caller falls back to `AGENT_DEV_USER_ID`).
    IAP on  → the VERIFIED email claim, lowercased; raises `IdentityError` if
    the assertion can't be verified or carries no email.

    Email (not `sub`) is the identity key on purpose: the ACL documents
    (`acl_users/{email}`) become operator-legible — an admin can see who has
    access without a sub→person lookup. Safe because the value comes from the
    signature-verified IAP assertion, never from the spoofable unsigned
    `X-Goog-Authenticated-User-*` headers.
    """
    if not IAP_ENABLED:
        return None
    if not IAP_AUDIENCE:
        raise IdentityError("IAP_ENABLED but IAP_AUDIENCE is not configured")

    assertion = headers.get(_JWT_HEADER)
    if not assertion:
        raise IdentityError(
            "missing X-Goog-IAP-JWT-Assertion (request did not pass through IAP)"
        )

    try:
        # Lazy import: google-auth is only needed when IAP is enabled. jwt.decode
        # verifies signature (against the cached certs) + audience + expiry — the
        # same call verify_token makes internally, minus the per-request fetch.
        from google.auth import jwt

        claims = jwt.decode(assertion, certs=_iap_certs(), audience=IAP_AUDIENCE)
    except Exception as e:  # noqa: BLE001 — fetch/signature/audience/expiry all fail closed
        raise IdentityError(f"IAP JWT verification failed: {e}") from e

    email = (claims.get("email") or "").strip().lower()
    if not email:
        raise IdentityError("IAP JWT has no 'email' claim")
    logger.debug("IAP identity verified: email=%s sub=%s", email, claims.get("sub"))
    return email
