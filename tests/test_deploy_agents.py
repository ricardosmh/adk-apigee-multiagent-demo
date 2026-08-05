"""Unit tests for the central agent deploy tool (pure helpers, no I/O)."""
import pytest

import deploy_agents as da


def _e(display, eid, created, sa=None):
    e = {"name": f"projects/1/locations/us-central1/reasoningEngines/{eid}",
         "displayName": display, "createTime": created}
    if sa:
        e["spec"] = {"deploymentSpec": {"serviceAccount": sa}}
    return e


AGENTS = {
    "supervisor": {"dir": "my_supervisor_agent", "sa": "a2a-supervisor-sa"},
    "order": {"dir": "my_order_agent", "sa": "a2a-order-sa"},
}
NAMES = {"supervisor": "a2a-supervisor-agent", "order": "a2a-order-agent"}


def test_group_engines_newest_first_and_ignores_foreign():
    engines = [
        _e("a2a-order-agent", "111", "2026-06-01T00:00:00Z"),
        _e("a2a-order-agent", "222", "2026-07-02T00:00:00Z"),
        _e("something-else", "999", "2026-07-01T00:00:00Z"),   # not ours — ignored
    ]
    g = da.group_engines(engines, NAMES)
    assert [da.engine_id(e) for e in g["order"]] == ["222", "111"]  # newest first
    assert g["supervisor"] == []


def test_derive_cleanup_keeps_newest_only():
    g = da.group_engines(
        [_e("a2a-order-agent", "111", "2026-06-01T00:00:00Z"),
         _e("a2a-order-agent", "222", "2026-07-02T00:00:00Z"),
         _e("a2a-supervisor-agent", "333", "2026-07-02T00:00:00Z")], NAMES)
    plan = da.derive_cleanup(g)
    assert len(plan) == 1                       # supervisor (single) untouched
    agent, keep, stale = plan[0]
    assert agent == "order" and da.engine_id(keep) == "222"
    assert [da.engine_id(e) for e in stale] == ["111"]


def _patch_cleanup_io(monkeypatch, engines, deleted):
    monkeypatch.setattr(da, "_env", lambda: ("proj", "us-central1"))
    monkeypatch.setattr(da, "_token", lambda: "tok")
    monkeypatch.setattr(da, "display_name", lambda a, s: NAMES[a])
    monkeypatch.setattr(da, "list_engines", lambda p, r, t: engines)
    monkeypatch.setattr(da, "should_write", lambda *a, **k: True)
    monkeypatch.setattr(da, "delete_engine", lambda name, region, token: deleted.append(name))


def test_cmd_cleanup_deletes_stale(monkeypatch):
    # Regression: cmd_cleanup must REACH the delete loop when there's a stale
    # plan (a mis-indented early `return 0` once made the whole loop dead code,
    # so --cleanup silently deleted nothing after a redeploy).
    engines = [_e("a2a-order-agent", "111", "2026-06-01T00:00:00Z"),
               _e("a2a-order-agent", "222", "2026-07-02T00:00:00Z")]
    deleted = []
    _patch_cleanup_io(monkeypatch, engines, deleted)
    rc = da.cmd_cleanup(object(), {"order": AGENTS["order"]}, "proj")
    assert rc == 0
    assert deleted == ["projects/1/locations/us-central1/reasoningEngines/111"]  # stale gone, 222 kept


def test_cmd_cleanup_nothing_stale_is_noop(monkeypatch):
    engines = [_e("a2a-order-agent", "222", "2026-07-02T00:00:00Z")]   # single gen
    deleted = []
    _patch_cleanup_io(monkeypatch, engines, deleted)
    rc = da.cmd_cleanup(object(), {"order": AGENTS["order"]}, "proj")
    assert rc == 0 and deleted == []


def test_check_findings_missing_stale_and_sa_drift():
    g = da.group_engines(
        [_e("a2a-order-agent", "111", "2026-06-01T00:00:00Z",
            sa="a2a-customer-sa@p.iam.gserviceaccount.com"),   # wrong SA
         _e("a2a-order-agent", "222", "2026-07-02T00:00:00Z",
            sa="a2a-customer-sa@p.iam.gserviceaccount.com")], NAMES)
    by_agent = {f["agent"]: f for f in da.check_findings(g, AGENTS, "p")}
    assert by_agent["supervisor"]["status"] == da.MISSING
    order = by_agent["order"]
    assert order["status"] == da.DRIFT
    assert "STALE" in order["detail"] and "manifest declares a2a-order-sa@" in order["detail"]


def test_check_findings_all_ok():
    g = da.group_engines(
        [_e("a2a-order-agent", "222", "2026-07-02T00:00:00Z",
            sa="a2a-order-sa@p.iam.gserviceaccount.com")],
        {"order": "a2a-order-agent"})
    f = da.check_findings(g, {"order": AGENTS["order"]}, "p")[0]
    assert f["status"] == da.OK and "SA a2a-order-sa ✓" in f["detail"]


def test_check_findings_card_identity():
    g = da.group_engines(
        [_e("a2a-order-agent", "222", "2026-07-02T00:00:00Z",
            sa="a2a-order-sa@p.iam.gserviceaccount.com")],
        {"order": "a2a-order-agent"})
    agents = {"order": AGENTS["order"]}
    # matching card -> OK with the ✓ note
    ok = da.check_findings(g, agents, "p", {"order": "order_agent"})[0]
    assert ok["status"] == da.OK and "card identity order_agent ✓" in ok["detail"]
    # wrong identity (sibling's pickle) -> DRIFT
    wrong = da.check_findings(g, agents, "p", {"order": "product_agent"})[0]
    assert wrong["status"] == da.DRIFT and "MISMATCH" in wrong["detail"]
    # no A2A surface at all (booted an AdkApp pickle) -> DRIFT
    dead = da.check_findings(g, agents, "p", {"order": "HTTP 403"})[0]
    assert dead["status"] == da.DRIFT and "HTTP 403" in dead["detail"]
    # supervisor is exempt (AdkApp has no card)
    gsup = da.group_engines(
        [_e("a2a-supervisor-agent", "333", "2026-07-02T00:00:00Z",
            sa="a2a-supervisor-sa@p.iam.gserviceaccount.com")],
        {"supervisor": "a2a-supervisor-agent"})
    sup = da.check_findings(gsup, {"supervisor": AGENTS["supervisor"]}, "p", {})[0]
    assert sup["status"] == da.OK and "card" not in sup["detail"]


def test_find_service_account_recurses():
    assert da.find_service_account(
        {"spec": {"deploymentSpec": {"env": [], "serviceAccount": "x@y"}}}) == "x@y"
    assert da.find_service_account({"a": [{"b": {}}]}) is None


def test_select_orders_supervisor_last_and_rejects_unknown():
    sel = da.select(AGENTS, ["--all"])
    assert list(sel)[-1] == "supervisor"
    assert list(da.select(AGENTS, ["order"])) == ["order"]
    with pytest.raises(SystemExit):
        da.select(AGENTS, ["nope"])


def test_report_worker_success_and_failure(tmp_path, capsys):
    ok_log = tmp_path / "order.log"
    ok_log.write_text("stuff\nDeployed: projects/1/locations/x/reasoningEngines/42\n")
    assert da._report_worker("order", 0, ok_log) is True
    assert "reasoningEngines/42" in capsys.readouterr().out

    bad_log = tmp_path / "customer.log"
    bad_log.write_text("line1\nboom: InternalServerError 500\n")
    assert da._report_worker("customer", 1, bad_log) is False
    assert "InternalServerError" in capsys.readouterr().err


def test_preflight_adc_states(capsys):
    """ADC preflight: pass when the staging bucket is visible; fail with the
    application-default fix when it isn't (seen live: stale ADC from an older
    login → all four engine deploys 404 'project not found' from the storage
    mTLS endpoint while every gcloud call succeeded)."""
    assert da.preflight_adc("proj", "bucket", _lookup=lambda b: object()) is True

    assert da.preflight_adc("proj", "bucket", _lookup=lambda b: None) is False
    err = capsys.readouterr().err
    assert "gcloud auth application-default login" in err
    assert "set-quota-project proj" in err

    def boom(_b):
        raise RuntimeError("404 POST https://storage.mtls.googleapis.com/storage/v1/b"
                           "?project=proj: The requested project was not found.")
    assert da.preflight_adc("proj", "bucket", _lookup=boom) is False
    err = capsys.readouterr().err
    assert "The requested project was not found" in err       # original error surfaced
    assert "use_client_certificate" in err                    # mTLS hint present


# ── registry sync (pure) ──────────────────────────────────────────────────────
def _entry(url_card=None, url_proto=None):
    d = {"name": "projects/p/locations/x/agents/agentregistry-uuid", "displayName": "order_agent"}
    if url_card:
        d["card"] = {"type": "A2A_AGENT_CARD", "content": {"name": "order_agent", "url": url_card}}
    if url_proto:
        d["protocols"] = [{"type": "A2A_AGENT",
                           "interfaces": [{"protocolBinding": "HTTP_JSON", "url": url_proto}]}]
    return d


URL = "https://x/v1beta1/projects/1/locations/us-central1/reasoningEngines/{}/a2a"


def test_registry_engine_id_from_both_shapes():
    assert da.registry_engine_id(_entry(url_card=URL.format(111))) == "111"
    assert da.registry_engine_id(_entry(url_proto=URL.format(222))) == "222"
    assert da.registry_engine_id(_entry()) is None


def test_registry_split_brain_entry_is_drift():
    """Seen live: card.content.url updated but protocols url still pointed at a
    DELETED engine — first-match said healthy while ADK routed via protocols.
    ALL url fields must agree with the newest engine."""
    split = _entry(url_card=URL.format(999), url_proto=URL.format(111))
    assert da.registry_engine_ids(split) == ["999", "111"]
    groups = da.group_engines(
        [_e("a2a-order-agent", "999", "2026-07-03T00:00:00Z")],
        {"order": "a2a-order-agent"})
    out = da.registry_findings(groups, {"order_agent": split})[0]
    assert out["status"] == da.DRIFT and "111" in out["detail"]
    # retarget fixes BOTH fields
    fixed, changed = da.retarget_entry(split, "999")
    assert changed and da.registry_engine_ids(fixed) == ["999"]


def test_retarget_entry_rewrites_all_urls_copy_only():
    e = _entry(url_card=URL.format(111), url_proto=URL.format(111))
    out, changed = da.retarget_entry(e, "999")
    assert changed
    assert out["card"]["content"]["url"] == URL.format(999)
    assert out["protocols"][0]["interfaces"][0]["url"] == URL.format(999)
    assert e["card"]["content"]["url"] == URL.format(111)   # original untouched
    same, changed2 = da.retarget_entry(out, "999")
    assert not changed2                                      # idempotent


def test_registry_findings_states():
    groups = da.group_engines(
        [_e("a2a-order-agent", "999", "2026-07-03T00:00:00Z"),
         _e("a2a-supervisor-agent", "555", "2026-07-03T00:00:00Z")],
        {"order": "a2a-order-agent", "supervisor": "a2a-supervisor-agent"})
    # stale entry -> DRIFT naming both ids
    stale = da.registry_findings(groups, {"order_agent": _entry(url_proto=URL.format(111))})
    assert len(stale) == 1                                   # supervisor exempt
    assert stale[0]["status"] == da.DRIFT and "111" in stale[0]["detail"] and "999" in stale[0]["detail"]
    # current entry -> OK
    ok = da.registry_findings(groups, {"order_agent": _entry(url_proto=URL.format(999))})
    assert ok[0]["status"] == da.OK
    # missing entry -> DRIFT
    missing = da.registry_findings(groups, {})
    assert missing[0]["status"] == da.DRIFT and "no Agent Registry entry" in missing[0]["detail"]
