"""IAP fail-closed branch logic (frontend/iap.py) — ported from the old
frontend/iap_check.py into the pytest suite.

Covers the branches that don't need a live IAP-signed JWT:
  - IAP off              -> None (dev fallback)
  - IAP on, no audience  -> IdentityError
  - IAP on, no header    -> IdentityError
The success path needs a real IAP token (browser test behind IAP), not here.

iap.py reads IAP_ENABLED / IAP_AUDIENCE at import, so each case reloads it.
google-auth/requests are imported lazily inside iap.py, so these branch tests
need no heavy deps.
"""
import importlib
import sys

import pytest


class Headers(dict):
    """Case-insensitive .get, like FastAPI's request.headers."""

    def get(self, k, default=None):
        return super().get(k.lower(), default)


def _load_iap(monkeypatch, **env):
    for k in ("IAP_ENABLED", "IAP_AUDIENCE"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    sys.modules.pop("iap", None)  # force re-read of the module-level env
    return importlib.import_module("iap")


def test_iap_off_returns_none(monkeypatch):
    iap = _load_iap(monkeypatch)
    assert iap.resolve_user_id(Headers()) is None


def test_iap_on_without_audience_raises(monkeypatch):
    iap = _load_iap(monkeypatch, IAP_ENABLED="1")
    with pytest.raises(iap.IdentityError):
        iap.resolve_user_id(Headers({"x-goog-iap-jwt-assertion": "x"}))


def test_audience_implies_enabled_and_missing_header_raises(monkeypatch):
    iap = _load_iap(monkeypatch, IAP_AUDIENCE="/projects/123/apps/foo")
    assert iap.IAP_ENABLED is True
    with pytest.raises(iap.IdentityError):
        iap.resolve_user_id(Headers())


def test_identity_is_verified_email_lowercased(monkeypatch):
    """The ACL keys on the VERIFIED email claim, normalized — not sub."""
    import google.auth.jwt

    iap = _load_iap(monkeypatch, IAP_AUDIENCE="/projects/123/apps/foo")
    monkeypatch.setattr(iap, "_iap_certs", lambda: {})
    monkeypatch.setattr(google.auth.jwt, "decode",
                        lambda *a, **k: {"sub": "999", "email": " Some.User@Example.com "})
    uid = iap.resolve_user_id(Headers({"x-goog-iap-jwt-assertion": "tok"}))
    assert uid == "some.user@example.com"

    monkeypatch.setattr(google.auth.jwt, "decode", lambda *a, **k: {"sub": "999"})
    with pytest.raises(iap.IdentityError):   # no email claim -> fail closed
        iap.resolve_user_id(Headers({"x-goog-iap-jwt-assertion": "tok"}))
