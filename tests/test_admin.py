"""Unit tests for the BFF admin API's decision logic (in-memory store, no
Firestore) — is_admin resolution, validation, and the last-admin lockout guard."""
import pytest

import admin


class MemStore:
    def __init__(self, roles=None, users=None):
        self._roles = dict(roles or {})
        self._users = dict(users or {})

    def roles(self):
        return {k: dict(v) for k, v in self._roles.items()}

    def users(self):
        return {k: dict(v) for k, v in self._users.items()}

    def set_role(self, name, data):
        self._roles[name] = data

    def delete_role(self, name):
        self._roles.pop(name, None)

    def set_user(self, email, data):
        self._users[email] = data

    def delete_user(self, email):
        self._users.pop(email, None)


def seeded():
    return MemStore(
        roles={"admin": {"agents": ["*"], "is_admin": True},
               "ops": {"agents": ["order_agent"], "is_admin": False}},
        users={"boss@x.com": {"roles": ["admin"]},
               "worker@x.com": {"roles": ["ops"]}},
    )


def test_is_admin_resolution_and_fail_closed():
    st = seeded()
    assert admin.is_admin(st, "boss@x.com") is True
    assert admin.is_admin(st, " Boss@X.com ") is True       # normalized
    assert admin.is_admin(st, "worker@x.com") is False       # role without flag
    assert admin.is_admin(st, "nobody@x.com") is False
    assert admin.is_admin(st, None) is False

    class Broken:
        def roles(self):
            raise RuntimeError("boom")
        users = roles
    assert admin.is_admin(Broken(), "boss@x.com") is False   # error -> fail closed


def test_require_admin_raises_for_non_admin():
    with pytest.raises(admin.AdminError):
        admin.require_admin(seeded(), "worker@x.com")
    assert admin.require_admin(seeded(), "boss@x.com") == "boss@x.com"


def test_put_role_validates_and_writes():
    st = seeded()
    admin.put_role(st, "Sales", ["product_agent", "product_agent"], False)
    assert st.roles()["sales"] == {"agents": ["product_agent"], "is_admin": False}
    with pytest.raises(admin.ValidationError):
        admin.put_role(st, "bad name!", ["x"], False)
    with pytest.raises(admin.ValidationError):
        admin.put_role(st, "ok", "not-a-list", False)


def test_put_user_validates_roles_exist_and_normalizes():
    st = seeded()
    admin.put_user(st, " New@X.com ", ["ops"])
    assert st.users()["new@x.com"] == {"roles": ["ops"]}
    with pytest.raises(admin.ValidationError):
        admin.put_user(st, "new@x.com", ["ghost-role"])
    with pytest.raises(admin.ValidationError):
        admin.put_user(st, "not-an-email", ["ops"])


def test_lockout_guard_protects_last_admin():
    st = seeded()
    # demoting the only admin user -> refused
    with pytest.raises(admin.ValidationError):
        admin.put_user(st, "boss@x.com", ["ops"])
    # deleting the only admin user -> refused
    with pytest.raises(admin.ValidationError):
        admin.delete_user(st, "boss@x.com")
    # stripping is_admin from the only admin role -> refused
    with pytest.raises(admin.ValidationError):
        admin.put_role(st, "admin", ["*"], False)
    # deleting the only admin role -> refused
    with pytest.raises(admin.ValidationError):
        admin.delete_role(st, "admin")
    # but with a SECOND admin, demoting the first is fine
    admin.put_user(st, "boss2@x.com", ["admin"])
    admin.put_user(st, "boss@x.com", ["ops"])
    assert admin.is_admin(st, "boss2@x.com") and not admin.is_admin(st, "boss@x.com")


def test_delete_flows_and_unknowns():
    st = seeded()
    admin.delete_user(st, "worker@x.com")
    assert "worker@x.com" not in st.users()
    admin.delete_role(st, "ops")
    assert "ops" not in st.roles()
    with pytest.raises(admin.ValidationError):
        admin.delete_role(st, "ops")          # already gone
    with pytest.raises(admin.ValidationError):
        admin.delete_user(st, "ghost@x.com")


def test_list_acl_shape():
    out = admin.list_acl(seeded())
    assert out["users"]["boss@x.com"] == ["admin"]
    assert out["roles"]["admin"]["is_admin"] is True
