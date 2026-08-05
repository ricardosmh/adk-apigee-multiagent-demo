"""Unit tests for the agent-side identity comparators (pure, no I/O)."""
import provision_agents as pi

AGENTS = {
    "supervisor": {"dir": "my_supervisor_agent", "sa": "a2a-supervisor-sa",
                   "roles": ["roles/aiplatform.user", "roles/datastore.viewer"],
                   "secret": "apigee-key-supervisor"},
    "order": {"dir": "my_order_agent", "sa": "a2a-order-sa",
              "roles": ["roles/aiplatform.user"], "secret": "apigee-key-order"},
}
SUP = "a2a-supervisor-sa@p.iam.gserviceaccount.com"
ORD = "a2a-order-sa@p.iam.gserviceaccount.com"


def _by_name(findings, kind):
    return {f["name"]: f for f in findings if f["kind"] == kind}


def test_compare_agent_sas_missing_and_role_drift():
    existing = {SUP}                                        # order SA absent
    roles = {SUP: {"roles/aiplatform.user"}}                # datastore missing
    out = _by_name(pi.compare_agent_sas(AGENTS, existing, roles, "p"), "serviceAccount")
    assert out["a2a-supervisor-sa"]["status"] == pi.DRIFT
    assert "roles/datastore.viewer" in out["a2a-supervisor-sa"]["detail"]
    assert out["a2a-order-sa"]["status"] == pi.MISSING


def test_compare_agent_sas_all_ok():
    existing = {SUP, ORD}
    roles = {SUP: set(AGENTS["supervisor"]["roles"]), ORD: set(AGENTS["order"]["roles"])}
    out = pi.compare_agent_sas(AGENTS, existing, roles, "p")
    assert all(f["status"] == pi.OK for f in out)


def test_compare_grants_bucket_and_actas():
    bucket_members = {f"serviceAccount:{SUP}"}              # order lacks bucket role
    actas = {SUP: {"user:me@x"}, ORD: set()}                # order lacks actAs
    out = pi.compare_grants(AGENTS, "p", bucket_members, actas, "me@x")
    bucket = _by_name(out, "bucketAccess")
    actas_f = _by_name(out, "deployerActAs")
    assert bucket["a2a-supervisor-sa"]["status"] == pi.OK
    assert bucket["a2a-order-sa"]["status"] == pi.DRIFT
    assert actas_f["a2a-supervisor-sa"]["status"] == pi.OK
    assert actas_f["a2a-order-sa"]["status"] == pi.DRIFT


def test_compare_grants_actas_email_case_insensitive():
    # IAM returns the account's canonical casing; gcloud reports lowercase.
    actas = {SUP: {"user:Me@X"}, ORD: {"user:me@x"}}
    out = pi.compare_grants(AGENTS, "p", None, actas, "ME@x")
    actas_f = _by_name(out, "deployerActAs")
    assert actas_f["a2a-supervisor-sa"]["status"] == pi.OK
    assert actas_f["a2a-order-sa"]["status"] == pi.OK


def test_compare_grants_skips_unknowns():
    # bucket unknown (STAGING_BUCKET unset) and no deployer -> no findings at all
    assert pi.compare_grants(AGENTS, "p", None, {}, None) == []


def test_compare_services_enabled_vs_missing():
    out = {f["name"]: f for f in pi.compare_services(
        ["aiplatform.googleapis.com", "agentregistry.googleapis.com"],
        {"aiplatform.googleapis.com", "storage.googleapis.com"})}
    assert out["aiplatform.googleapis.com"]["status"] == pi.OK
    assert out["agentregistry.googleapis.com"]["status"] == pi.MISSING


def test_compare_services_listing_failure_is_unknown_not_missing():
    # a failed listing must NOT claim every service is missing (seen live)
    out = pi.compare_services(["aiplatform.googleapis.com"], None, "PERMISSION_DENIED: x")
    assert len(out) == 1 and out[0]["status"] == pi.DRIFT
    assert "UNKNOWN" in out[0]["detail"] and "PERMISSION_DENIED" in out[0]["detail"]


def test_fs_codec_roundtrip():
    doc = {"fields": {
        "agents": pi.fs_encode(["*", "order_agent"]),
        "is_admin": pi.fs_encode(True),
        "note": pi.fs_encode("x"),
    }}
    assert pi.fs_doc_to_dict(doc) == {"agents": ["*", "order_agent"], "is_admin": True, "note": "x"}
    assert pi.fs_doc_to_dict(None) is None


def test_compare_acl_states():
    roles = {"admin": {"agents": ["*"], "is_admin": True}}
    users = {"Some.User@Example.com": ["admin"]}          # un-normalized on purpose
    # database missing -> everything MISSING
    out = pi.compare_acl(roles, users, {}, {}, db_exists=False, database="(default)")
    assert {f["status"] for f in out} == {pi.MISSING}
    assert any(f["kind"] == "aclUser" and f["name"] == "some.user@example.com" for f in out)
    # role drift (lost wildcard) + user missing the seed role
    out = pi.compare_acl(roles, users,
                         {"admin": {"agents": ["order_agent"], "is_admin": True}},
                         {"some.user@example.com": {"roles": ["viewer"]}},
                         db_exists=True, database="(default)")
    by = {(f["kind"], f["name"]): f for f in out}
    assert by[("aclRole", "admin")]["status"] == pi.DRIFT
    assert by[("aclUser", "some.user@example.com")]["status"] == pi.DRIFT
    assert "admin" in by[("aclUser", "some.user@example.com")]["detail"]
    # all green — user having EXTRA roles is OK (UI-managed)
    out = pi.compare_acl(roles, users,
                         {"admin": {"agents": ["*"], "is_admin": True}},
                         {"some.user@example.com": {"roles": ["admin", "ops"]}},
                         db_exists=True, database="(default)")
    assert all(f["status"] == pi.OK for f in out)


def test_compare_bucket_states():
    vsa = "service-123@gcp-sa-aiplatform.iam.gserviceaccount.com"
    # unset -> skipped entirely
    assert pi.compare_bucket(None, False, vsa, set()) == []
    # missing bucket
    assert pi.compare_bucket("b", False, vsa, set())[0]["status"] == pi.MISSING
    # exists but vertex agent can't read the pickle
    out = pi.compare_bucket("b", True, vsa, {"serviceAccount:someone-else@x"})
    assert out[0]["status"] == pi.OK and out[1]["status"] == pi.DRIFT
    # fully green
    out2 = pi.compare_bucket("b", True, vsa, {f"serviceAccount:{vsa}"})
    assert all(f["status"] == pi.OK for f in out2)


def test_compare_ai_network_states():
    net = {"vpc": "ai-vpc",
           "subnets": [{"name": "bff-egress", "range": "10.0.10.0/24",
                        "region": "southamerica-west1"},
                       {"name": "agent-engine-subnet", "range": "10.0.12.0/24",
                        "region": "us-central1"}],
           "network_attachment": {"name": "agent-engine-attachment",
                                  "subnet": "agent-engine-subnet", "region": "us-central1"},
           "apigee_psc": {"org": "o",
                          "endpoint": {"name": "ea-apigee-internal-endpoint",
                                       "address": "10.0.10.2", "subnet": "bff-egress",
                                       "region": "southamerica-west1"},
                          "dns": {"zone": "apigee-internal", "domain": "apigee.com.",
                                  "record": "internal.apigee.com."}},
           "artifact_repo": {"name": "bff", "region": "southamerica-west1"}}
    # fresh project -> everything missing
    fresh = pi.compare_ai_network(net, {"vpc_routing": None, "subnets": {},
                                        "attachment": None, "endpoint_ip": None,
                                        "dns_record_ip": None, "ar_repo": None})
    assert {f["status"] for f in fresh} == {pi.MISSING}
    # converged
    live = {"vpc_routing": "GLOBAL",
            "subnets": {"bff-egress": {"range": "10.0.10.0/24", "region": "southamerica-west1"},
                        "agent-engine-subnet": {"range": "10.0.12.0/24", "region": "us-central1"}},
            "attachment": True, "endpoint_ip": "10.0.10.2",
            "dns_record_ip": "10.0.10.2", "ar_repo": True}
    assert all(f["status"] == pi.OK for f in pi.compare_ai_network(net, live))
    # regional routing -> DRIFT (cross-region PSC path breaks)
    bad = pi.compare_ai_network(net, {**live, "vpc_routing": "REGIONAL"})
    assert bad[0]["status"] == pi.DRIFT and "GLOBAL" in bad[0]["detail"]
    # dns record pointing at the wrong IP -> DRIFT
    bad2 = pi.compare_ai_network(net, {**live, "dns_record_ip": "10.9.9.9"})
    assert any(f["kind"] == "apigeeDns" and f["status"] == pi.DRIFT for f in bad2)
    # PSC connection not accepted by the Apigee instance -> DRIFT naming the fix
    pend = pi.compare_ai_network(net, {**live, "endpoint_status": "PENDING"})
    row = next(f for f in pend if f["kind"] == "apigeePscEndpoint")
    assert row["status"] == pi.DRIFT and "consumerAcceptList" in row["detail"]
    ok = pi.compare_ai_network(net, {**live, "endpoint_status": "ACCEPTED"})
    assert all(f["status"] == pi.OK for f in ok)
    # unreadable -> single honest UNKNOWN; undeclared -> nothing
    assert "UNKNOWN" in pi.compare_ai_network(net, None)[0]["detail"]
    assert pi.compare_ai_network(None, None) == []


def test_grant_retries_transient_iam_races(monkeypatch, capsys):
    """Fresh-project IAM bindings race just-created identities — _grant must
    retry (winning on a later attempt) and, if all attempts fail, say so
    instead of silently losing the grant into a second apply pass."""
    calls = {"n": 0}

    def flaky(args):
        calls["n"] += 1
        ok = calls["n"] >= 3
        r = types.SimpleNamespace(stderr="ERROR: member does not exist", stdout="")
        return ok, r

    import types

    monkeypatch.setattr(pi, "_gcloud", flaky)
    monkeypatch.setattr(pi.time, "sleep", lambda s: None)
    assert pi._grant("x", ["projects", "add-iam-policy-binding"]) is True
    assert calls["n"] == 3
    assert "! x failed" not in capsys.readouterr().out

    calls["n"] = -10                      # never reaches 3 ok-threshold
    assert pi._grant("y", ["whatever"], attempts=2) is False
    assert "! y failed: ERROR: member does not exist" in capsys.readouterr().out
