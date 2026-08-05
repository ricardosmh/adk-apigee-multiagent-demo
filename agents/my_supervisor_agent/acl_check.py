"""Offline check for the production ACL (app/acl.py) — no ADK/Firestore/deploy.

Verifies the master flag gating (inert when off), fail-closed resolution, the
dynamic policed set (deny anything not allowed, incl. unknown agents), and
per-invocation memoization. Run: `python acl_check.py`
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "app"))
import acl  # noqa: E402


class _Ctx:
    def __init__(self, uid):
        self._invocation_context = types.SimpleNamespace(
            session=types.SimpleNamespace(user_id=uid)
        )


class _Tool:
    def __init__(self, name):
        self.name = name


def _env(**kw):
    for k in ("ACL_ENABLED", "ACL_BACKEND", "ACL_FAKE_JSON", "ACL_ALLOW_ALL"):
        os.environ.pop(k, None)
    os.environ.update({k: v for k, v in kw.items() if v is not None})


FAKE = '{"alice":["order_agent"],"bob":[]}'
fails = 0


def check(label, got, want):
    global fails
    ok = got == want
    fails += 0 if ok else 1
    print(f"[{'PASS' if ok else 'FAIL'}] {label}: got={got!r} want={want!r}")


def transfer(target, uid):
    return acl.before_tool_acl(_Tool("transfer_to_agent"), {"agent_name": target}, _Ctx(uid))


# ── master flag OFF → completely inert (no deny, even for disallowed) ──
_env(ACL_BACKEND="fake", ACL_FAKE_JSON=FAKE)  # ACL_ENABLED unset
check("flag off: alice→product NOT blocked", transfer("product_agent", "alice"), None)
check("flag off: unknown user NOT blocked", transfer("order_agent", "nobody"), None)

# ── flag ON, fake backend ──
_env(ACL_ENABLED="1", ACL_BACKEND="fake", ACL_FAKE_JSON=FAKE)
check("alice→order allowed", transfer("order_agent", "alice"), None)
denied = transfer("product_agent", "alice")
check("alice→product DENIED", isinstance(denied, dict) and denied["status"] == "denied", True)
check("dynamic: alice→unknown_agent DENIED",
      isinstance(transfer("Workspace_Agent", "alice"), dict), True)
check("bob (empty) → order DENIED", isinstance(transfer("order_agent", "bob"), dict), True)
check("unlisted user → DENIED (fail-closed)", isinstance(transfer("order_agent", "x"), dict), True)
check("non-transfer tool ignored",
      acl.before_tool_acl(_Tool("other"), {}, _Ctx("alice")), None)

# ── ALLOW_ALL (testing) ──
_env(ACL_ENABLED="1", ACL_BACKEND="fake", ACL_FAKE_JSON=FAKE, ACL_ALLOW_ALL="1")
check("ALLOW_ALL → permitted", transfer("product_agent", "alice"), None)

# ── resolver fail-closed cases ──
_env(ACL_ENABLED="1", ACL_BACKEND="fake", ACL_FAKE_JSON="{bad json}")
check("bad JSON → fail-closed empty", acl.resolve_allowed_agents("alice"), set())
_env(ACL_ENABLED="1", ACL_BACKEND="fake", ACL_FAKE_JSON=FAKE)
check("alice resolves to {order}", acl.resolve_allowed_agents("alice"), {"order_agent"})
check("None user → empty", acl.resolve_allowed_agents(None), set())

# ── per-invocation memoization: same ctx reused, table change not seen mid-turn ──
_env(ACL_ENABLED="1", ACL_BACKEND="fake", ACL_FAKE_JSON=FAKE)
ctx = _Ctx("alice")
first = acl._resolve_for_context(ctx)
os.environ["ACL_FAKE_JSON"] = '{"alice":["order_agent","product_agent"]}'
second = acl._resolve_for_context(ctx)  # same ctx → memoized, ignores the change
check("memoized within invocation", first == second == {"order_agent"}, True)
third = acl._resolve_for_context(_Ctx("alice"))  # new ctx (next turn) → fresh read
check("fresh read next invocation (instant revoke)", third, {"order_agent", "product_agent"})

print(f"\n{'ALL PASSED' if fails == 0 else str(fails) + ' FAILURE(S)'}")
sys.exit(1 if fails else 0)
