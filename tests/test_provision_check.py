"""Unit tests for the Apigee provisioning `check` comparators (pure, no I/O).

The live API layer (fetch_*) needs real creds and isn't exercised here; these
lock in the diff logic that decides OK / DRIFT / MISSING / EXTRA.
"""
import provision as p


def _status(findings, name):
    return next(f["status"] for f in findings if f["name"] == name)


def test_target_servers_ok_drift_missing():
    desired = [
        {"name": "ts-a", "host": "aiplatform.googleapis.com", "port": 443, "ssl": True},
        {"name": "ts-b", "host": "us-central1-aiplatform.googleapis.com", "port": 443, "ssl": True},
        {"name": "ts-missing", "host": "x", "port": 443, "ssl": True},
    ]
    live = {
        "ts-a": {"host": "aiplatform.googleapis.com", "port": 443, "sSLInfo": {"enabled": True}, "isEnabled": True},
        "ts-b": {"host": "WRONG", "port": 443, "sSLInfo": {"enabled": True}, "isEnabled": True},
    }
    out = p.compare_target_servers(desired, live)
    assert _status(out, "ts-a") == p.OK
    assert _status(out, "ts-b") == p.DRIFT
    assert _status(out, "ts-missing") == p.MISSING


def test_target_server_ssl_off_is_drift():
    desired = [{"name": "ts", "host": "h", "port": 443, "ssl": True}]
    live = {"ts": {"host": "h", "port": 443, "sSLInfo": {"enabled": False}, "isEnabled": True}}
    assert p.compare_target_servers(desired, live)[0]["status"] == p.DRIFT


def test_data_collectors_type_match_and_drift():
    desired = [{"name": "dc_model", "type": "STRING"}, {"name": "dc_tokenCount", "type": "INTEGER"}]
    live = {"dc_model": {"name": "dc_model", "type": "STRING"},
            "dc_tokenCount": {"name": "dc_tokenCount", "type": "TYPE_STRING"}}  # wrong type
    out = p.compare_data_collectors(desired, live)
    assert _status(out, "dc_model") == p.OK
    assert _status(out, "dc_tokenCount") == p.DRIFT


def test_products_environments_and_proxies():
    desired = [{"name": "prod", "environments": ["sandbox-internal"], "proxies": ["gemini-llm-apiproxy"]}]
    # env drift: still has sandbox
    live = {"prod": {"environments": ["sandbox", "sandbox-internal"],
                     "operationGroup": {"operationConfigs": [{"apiSource": "gemini-llm-apiproxy"}]}}}
    assert p.compare_products(desired, live)[0]["status"] == p.DRIFT
    # exact match
    live2 = {"prod": {"environments": ["sandbox-internal"],
                      "operationGroup": {"operationConfigs": [{"apiSource": "gemini-llm-apiproxy"}]}}}
    assert p.compare_products(desired, live2)[0]["status"] == p.OK


def test_apps_products_membership():
    desired = [{"name": "a2a-order", "products": ["agents-models-apiproduct", "mcp-server-apiproduct"]}]
    # only has one product -> drift
    live = {"a2a-order": {"credentials": [{"apiProducts": [{"apiproduct": "agents-models-apiproduct"}]}]}}
    assert p.compare_apps(desired, live)[0]["status"] == p.DRIFT
    # both products -> ok
    live2 = {"a2a-order": {"credentials": [{"apiProducts": [
        {"apiproduct": "agents-models-apiproduct"}, {"apiproduct": "mcp-server-apiproduct"}]}]}}
    assert p.compare_apps(desired, live2)[0]["status"] == p.OK


def test_retired_app_present_is_extra():
    assert p.compare_retired_apps(["mcp-server-app"], {"mcp-server-app": {}})[0]["status"] == p.EXTRA
    assert p.compare_retired_apps(["mcp-server-app"], {})[0]["status"] == p.OK


# ── derive_plan (pure) ────────────────────────────────────────────────────────
MANIFEST = {
    "targetServers": [{"name": "ts", "host": "h", "port": 443, "ssl": True}],
    "dataCollectors": [{"name": "dc_x", "type": "INTEGER"}],
    "apiProducts": [{"name": "prod", "environments": ["sandbox-internal"], "proxies": ["px"]}],
    "apps": [{"name": "app", "products": ["prod", "mcp"]}],
}


def _plan_verbs(actions):
    return [(a["verb"], a["kind"], a["name"]) for a in actions]


def test_derive_plan_covers_both_deploy_sas():
    """BOTH proxy deploy SAs (meta.deploy_sa AND meta.ecommerce_deploy_sa)
    must be plannable — the AI-side SA was once absent from the desired map,
    so a fresh org 403'd actAs on every AI-proxy deploy (found live)."""
    manifest = {"meta": {"deploy_sa": "ai@org.iam", "ecommerce_deploy_sa": "ec@org.iam"}}
    findings = [p._f(p.MISSING, "deploySA", "ai@org.iam"),
                p._f(p.DRIFT, "deploySA", "ec@org.iam", "cannot actAs")]
    plan = p.derive_plan(findings, manifest)
    assert {(a["verb"], a["name"]) for a in plan} == \
        {("CREATE", "ai@org.iam"), ("UPDATE", "ec@org.iam")}


def test_derive_plan_creates_deploy_sa_before_telemetry_grants():
    """telemetryGrants bind roles to the deploy SAs — on a fresh org the SA
    CREATE must run first or the grant fails into a second pass (found live:
    findings list grants before deploySAs, and both ranked 0)."""
    manifest = {"meta": {"deploy_sa": "ai@org.iam"},
                "telemetryGrants": [{"member": "deploy_sa",
                                     "role": "roles/logging.logWriter"}]}
    findings = [  # findings order: grant first, exactly as _findings emits
        p._f(p.DRIFT, "telemetryGrant", "deploy_sa → roles/logging.logWriter",
             "lacks the role"),
        p._f(p.MISSING, "deploySA", "ai@org.iam"),
    ]
    plan = p.derive_plan(findings, manifest)
    kinds = [a["kind"] for a in plan]
    assert kinds.index("deploySA") < kinds.index("telemetryGrant")


def test_derive_plan_maps_and_orders():
    findings = [
        p._f(p.MISSING, "targetServer", "ts"),
        p._f(p.DRIFT, "apiProduct", "prod", "environments drift"),
        p._f(p.DRIFT, "app", "app", "products drift"),
        p._f(p.EXTRA, "app(retired)", "mcp-server-app"),
        p._f(p.OK, "dataCollector", "dc_x"),  # OK -> no action
    ]
    plan = p.derive_plan(findings, MANIFEST)
    verbs = _plan_verbs(plan)
    assert ("OK",) not in [(v[0],) for v in verbs]                 # OK skipped
    assert ("CREATE", "targetServer", "ts") in verbs
    assert ("UPDATE", "apiProduct", "prod") in verbs
    assert ("UPDATE", "app", "app") in verbs
    # ordering: targetServer/product before app; delete last
    assert verbs.index(("CREATE", "targetServer", "ts")) < verbs.index(("UPDATE", "app", "app"))
    assert verbs[-1][0] == "DELETE"
    # the create action carries the desired payload for execute
    ts = next(a for a in plan if a["kind"] == "targetServer")
    assert ts["desired"]["host"] == "h"


def test_derive_plan_datacollector_type_drift_is_manual():
    findings = [p._f(p.DRIFT, "dataCollector", "dc_x", "type=STRING want INTEGER")]
    plan = p.derive_plan(findings, MANIFEST)
    assert plan[0]["verb"] == "MANUAL"


# ── secrets: app keys → Secret Manager (pure) ─────────────────────────────────
SECRETS_M = {
    "meta": {"org": "org", "ai_project": "proj", "geap_service_agent": "svc@gcp-sa-aiplatform.iam.gserviceaccount.com"},
    "serviceAccounts": [
        {"agent": "supervisor", "name": "a2a-supervisor-sa", "secretAccessor": ["apigee-key-supervisor"]},
        {"agent": "order", "name": "a2a-order-sa", "secretAccessor": ["apigee-key-order"]},
    ],
    "apps": [
        {"name": "a2a-supervisor-agent", "secret": "apigee-key-supervisor"},
        {"name": "a2a-order-management-agent", "secret": "apigee-key-order"},
        {"name": "agents-bff", "secret": "<managed by frontend/service.yaml>"},
    ],
}


def test_derive_secrets_maps_source_and_accessors():
    secrets = {s["name"]: s for s in p.derive_secrets(SECRETS_M)}
    assert set(secrets) == {"apigee-key-supervisor", "apigee-key-order"}  # BFF <managed> skipped
    sup = secrets["apigee-key-supervisor"]
    assert sup["source_app"] == "a2a-supervisor-agent"
    # accessor = the agent SA + the vertex service agent
    assert "a2a-supervisor-sa@proj.iam.gserviceaccount.com" in sup["accessors"]
    assert "svc@gcp-sa-aiplatform.iam.gserviceaccount.com" in sup["accessors"]


def test_compare_secrets_missing_accessor_is_drift():
    specs = [{"name": "apigee-key-order", "source_app": "a2a-order-management-agent",
              "accessors": ["a2a-order-sa@proj.iam.gserviceaccount.com", "svc@x"]}]
    out = p.compare_secrets(specs, {"apigee-key-order"}, {"apigee-key-order": {"a2a-order-sa@proj.iam.gserviceaccount.com"}})
    assert out[0]["status"] == p.DRIFT and "svc@x" in out[0]["detail"]


def test_compare_secrets_key_match_states():
    spec = {"name": "apigee-key-order", "source_app": "a2a-order-management-agent",
            "accessors": ["a2a-order-sa@proj.iam.gserviceaccount.com"]}
    acc = {"apigee-key-order": {"a2a-order-sa@proj.iam.gserviceaccount.com"}}
    # value matches the app's current consumer key -> OK, detail says so
    ok = p.compare_secrets([spec], {"apigee-key-order"}, acc, {"apigee-key-order": True})[0]
    assert ok["status"] == p.OK and "matches app a2a-order-management-agent" in ok["detail"]
    # app key rotated but secret not updated -> DRIFT flagged STALE
    stale = p.compare_secrets([spec], {"apigee-key-order"}, acc, {"apigee-key-order": False})[0]
    assert stale["status"] == p.DRIFT and "STALE" in stale["detail"]
    # secret unreadable (no versions.access) -> OK but explicitly marked skipped
    skipped = p.compare_secrets([spec], {"apigee-key-order"}, acc, {"apigee-key-order": None})[0]
    assert skipped["status"] == p.OK and "skipped" in skipped["detail"]


def test_compare_secrets_ok_detail_shows_what_was_checked():
    spec = {"name": "apigee-key-order", "source_app": "a2a-order-management-agent",
            "accessors": ["a@x", "b@y"]}
    out = p.compare_secrets([spec], {"apigee-key-order"}, {"apigee-key-order": {"a@x", "b@y"}},
                            {"apigee-key-order": True})[0]
    assert out["status"] == p.OK
    assert "2 accessor(s) granted" in out["detail"] and "value matches" in out["detail"]


# ── product UPDATE preserves LLM config (the risky write path) ─────────────────
def test_reconcile_product_preserves_llm_and_prunes_proxy():
    live = {
        "name": "unified-llm-enpdoint-apiproduct",
        "environments": ["sandbox", "sandbox-internal"],
        "quota": "1000", "quotaInterval": "5", "quotaTimeUnit": "minute",
        "attributes": [{"name": "access", "value": "private"}],
        "operationGroup": {
            "operationConfigType": "proxy",
            "operationConfigs": [
                {"apiSource": "unified-llm-endpoint-apiproxy", "operations": [{"resource": "/"}]},      # dead — prune
                {"apiSource": "unified-llm-endpoint-sse-apiproxy", "operations": [{"resource": "/"}],
                 "attributes": [{"name": "llmModels", "value": "gemini,claude"}]},                      # keep incl. llm attrs
            ],
        },
    }
    desired = {"environments": ["sandbox-internal"], "proxies": ["unified-llm-endpoint-sse-apiproxy"]}
    body = p._reconcile_product(live, desired)
    assert body["environments"] == ["sandbox-internal"]                      # env reconciled
    assert body["quota"] == "1000" and body["quotaTimeUnit"] == "minute"     # llmQuota preserved
    sources = [oc["apiSource"] for oc in body["operationGroup"]["operationConfigs"]]
    assert sources == ["unified-llm-endpoint-sse-apiproxy"]                   # dead proxy pruned
    kept = body["operationGroup"]["operationConfigs"][0]
    assert kept["attributes"][0]["value"] == "gemini,claude"                 # per-proxy llm attrs preserved


def test_execute_delete_retired_app_routes_without_desired(monkeypatch):
    # DELETE actions carry no "desired"; _execute must route on verb before kind.
    calls = []
    monkeypatch.setattr(p, "_write", lambda *a, **k: calls.append(a))
    m = {"meta": {"org": "o", "env": "e"}, "developer": {"email": "dev@x"}}
    p._execute({"verb": "DELETE", "kind": "app", "name": "mcp-server-app"}, m, "tok")
    assert calls and calls[0][0] == "DELETE" and calls[0][1].endswith("/apps/mcp-server-app")


# ── native LLM token quota (product-level allowance, per-key counters) ────────
def _live_unified():
    """Shape taken from the REAL org product (user-provided JSON): flat llm
    fields + llmOperationGroup with STALE entries for the deleted non-SSE proxy."""
    def oc(src, model):
        return {"apiSource": src, "llmOperations": [{"resource": "/", "model": model}],
                "llmTokenQuota": {}}
    return {
        "name": "unified-llm-enpdoint-apiproduct",
        "environments": ["sandbox-internal"],
        "operationGroup": {"operationConfigs": [
            {"apiSource": "unified-llm-endpoint-sse-apiproxy",
             "operations": [{"resource": "/"}], "quota": {}}]},
        "llmOperationGroup": {"operationConfigs": [
            oc("unified-llm-endpoint-apiproxy", "gemini-3.1-flash-lite"),      # dead proxy
            oc("unified-llm-endpoint-apiproxy", "claude-haiku-4-5"),           # dead proxy
            oc("unified-llm-endpoint-sse-apiproxy", "gemini-3.1-flash-lite"),
            oc("unified-llm-endpoint-sse-apiproxy", "gemini-3.1-pro-preview"),
        ]},
        "llmQuota": "1000", "llmQuotaInterval": "5", "llmQuotaTimeUnit": "minute",
    }


def _desired_unified():
    return {"name": "unified-llm-enpdoint-apiproduct",
            "environments": ["sandbox-internal"],
            "proxies": ["unified-llm-endpoint-sse-apiproxy"],
            "llmModels": ["gemini-3.1-flash-lite", "gemini-3.1-pro-preview"],
            "llmQuota": {"tokens": 10000, "interval": 5, "timeUnit": "minute"}}


def test_compare_products_llm_quota_and_stale_ops():
    d = _desired_unified()
    out = p.compare_products([d], {d["name"]: _live_unified()})[0]
    assert out["status"] == p.DRIFT
    assert "llmQuota=1000/5/minute want 10000/5/minute" in out["detail"]
    assert "llmOps stale" in out["detail"] and "unified-llm-endpoint-apiproxy" in out["detail"]

    # reconciled body -> compare goes green
    body = p._reconcile_product(_live_unified(), d)
    assert body["llmQuota"] == "10000"
    assert p._llm_pairs(body) == p.desired_llm_pairs(d)   # stale pruned, desired complete
    out2 = p.compare_products([d], {d["name"]: body})[0]
    assert out2["status"] == p.OK


def test_product_create_body_renders_llm_constructs():
    """Fresh-org gap (seen live): CREATE built products WITHOUT the LLM
    constructs — no llmOperationGroup allow-list, no token quota — because the
    old comment assumed products always pre-exist. The CREATE payload must
    satisfy the comparator immediately (one-pass convergence)."""
    d = _desired_unified()
    body = p._product_body(d)
    assert body["llmQuota"] == "10000"
    assert body["llmQuotaInterval"] == "5" and body["llmQuotaTimeUnit"] == "minute"
    assert p._llm_pairs(body) == p.desired_llm_pairs(d)
    assert p.compare_products([d], {d["name"]: body})[0]["status"] == p.OK

    # a product with no llmModels/llmQuota gets NO llm constructs
    plain = {"name": "mcp-server-apiproduct", "environments": ["e"],
             "proxies": ["mcp-server-apiproxy"]}
    body = p._product_body(plain)
    assert "llmQuota" not in body and "llmOperationGroup" not in body
    assert [oc["apiSource"] for oc in body["operationGroup"]["operationConfigs"]] == ["mcp-server-apiproxy"]


def test_reconcile_product_preserves_kept_llm_token_quota():
    live = _live_unified()
    live["llmOperationGroup"]["operationConfigs"][2]["llmTokenQuota"] = {"tokens": "77"}
    body = p._reconcile_product(live, _desired_unified())
    kept = [oc for oc in body["llmOperationGroup"]["operationConfigs"]
            if (oc["apiSource"], oc["llmOperations"][0]["model"]) ==
               ("unified-llm-endpoint-sse-apiproxy", "gemini-3.1-flash-lite")]
    assert kept and kept[0]["llmTokenQuota"] == {"tokens": "77"}


def test_compare_products_no_llm_manifest_fields_ignores_llm():
    # products without llmModels/llmQuota in the manifest never report llm drift
    d = {"name": "mcp-server-apiproduct", "environments": ["sandbox-internal"],
         "proxies": ["mcp-server-apiproxy"]}
    live = {"name": "mcp-server-apiproduct", "environments": ["sandbox-internal"],
            "operationGroup": {"operationConfigs": [
                {"apiSource": "mcp-server-apiproxy", "operations": [{"resource": "/"}]}]}}
    assert p.compare_products([d], {d["name"]: live})[0]["status"] == p.OK


# ── end users (app-per-user for the Direct view) ──────────────────────────────
def _user_app(email, products=("unified-llm-enpdoint-apiproduct",), owner=None):
    return {"name": p.user_app_name(email),
            "attributes": ([{"name": "owner_email", "value": owner}] if owner else []),
            "credentials": [{"consumerKey": "k",
                             "apiProducts": [{"apiproduct": x} for x in products]}]}


def test_compare_end_users_states():
    prod = "unified-llm-enpdoint-apiproduct"
    emails = ["a@x.com", "b@x.com", "c@x.com", "d@x.com"]
    live = {
        "a@x.com": {"developer": True, "app": _user_app("a@x.com", owner="a@x.com")},   # OK
        "b@x.com": {"developer": False, "app": None},                                    # no developer
        "c@x.com": {"developer": True, "app": None},                                     # no app
        "d@x.com": {"developer": True, "app": _user_app("d@x.com", products=("other",))},# drift x2
    }
    by = {f["name"]: f for f in p.compare_end_users(emails, prod, live)}
    assert by["a@x.com"]["status"] == p.OK
    assert by["b@x.com"]["status"] == p.MISSING and "developer" in by["b@x.com"]["detail"]
    assert by["c@x.com"]["status"] == p.MISSING and "no app" in by["c@x.com"]["detail"]
    d = by["d@x.com"]
    assert d["status"] == p.DRIFT and "lacks product" in d["detail"] and "owner_email" in d["detail"]


def test_user_app_name_deterministic_slug():
    assert p.user_app_name(" Some.Admin@Example-Corp.Test.com ") == \
        "llm-user-some-admin-at-example-corp-test-com"


def test_fetch_end_user_treats_404_none_as_missing(monkeypatch):
    """_get returns None on 404 (no exception) — a missing developer must
    report developer=False, not silently 'exists' (live bug: apply skipped
    developer creation and the app POST 404'd)."""
    monkeypatch.setattr(p, "_get", lambda url, token: None)
    assert p._fetch_end_user("org", "x@y.com", "t") == {"developer": False, "app": None}

    monkeypatch.setattr(p, "_get",
                        lambda url, token: {} if url.endswith("x@y.com") else None)
    assert p._fetch_end_user("org", "x@y.com", "t") == {"developer": True, "app": None}


def test_compare_trace_config_states():
    want = {"exporter": "CLOUD_TRACE", "endpoint": "my-apigee-org", "samplingRate": 0.5}
    # no manifest section -> nothing reported
    assert p.compare_trace_config(None, None) == []
    # live matches
    live = {"exporter": "CLOUD_TRACE", "endpoint": "my-apigee-org",
            "samplingConfig": {"sampler": "PROBABILITY", "samplingRate": 0.5}}
    assert p.compare_trace_config(want, live)[0]["status"] == p.OK
    # unset / different exporter -> DRIFT
    assert p.compare_trace_config(want, None)[0]["status"] == p.DRIFT
    assert p.compare_trace_config(want, {"exporter": "JAEGER"})[0]["status"] == p.DRIFT
    # listing failed (e.g. 400 on a fresh org flavor) -> UNKNOWN, never a crash
    f = p.compare_trace_config(want, {"_error": "HTTP 400: no addons"})[0]
    assert f["status"] == p.UNKNOWN and "HTTP 400" in f["detail"]


def test_derive_plan_unknown_is_manual():
    """UNKNOWN findings (failed listings) must never become apply actions —
    derive_plan routes them to MANUAL so apply can't act on unverified state."""
    findings = [p._f(p.UNKNOWN, "traceConfig", "distributed-trace", "HTTP 400: x")]
    plan = p.derive_plan(findings, MANIFEST)
    assert [a["verb"] for a in plan] == ["MANUAL"]


# ── telemetry grants (cross-project observability writers) ────────────────────
def test_resolve_telemetry_member():
    meta = {"deploy_sa": "apigee-ai-consumer@my-ai-project.iam.gserviceaccount.com",
            "ecommerce_deploy_sa": "cloud-run-invoker@o.iam.gserviceaccount.com"}
    assert p.resolve_telemetry_member("deploy_sa", meta, None) == \
        "serviceAccount:apigee-ai-consumer@my-ai-project.iam.gserviceaccount.com"
    assert p.resolve_telemetry_member("ecommerce_deploy_sa", meta, None) == \
        "serviceAccount:cloud-run-invoker@o.iam.gserviceaccount.com"
    # raw members (e.g. the ecommerce services' runtime SA) pass through
    assert p.resolve_telemetry_member(
        "serviceAccount:ecommerce-sa@b.iam.gserviceaccount.com", meta, None) == \
        "serviceAccount:ecommerce-sa@b.iam.gserviceaccount.com"
    assert p.resolve_telemetry_member("apigee_service_agent", meta, "12345") == \
        "serviceAccount:service-12345@gcp-sa-apigee.iam.gserviceaccount.com"
    assert p.resolve_telemetry_member("apigee_service_agent", meta, None) is None
    assert p.resolve_telemetry_member("wat", meta, "1") is None


def test_compare_telemetry_grants_states():
    grants = [{"member": "deploy_sa", "role": "roles/logging.logWriter"},
              {"member": "apigee_service_agent", "role": "roles/cloudtrace.agent"}]
    resolved = {"deploy_sa": "serviceAccount:dep@x", "apigee_service_agent": "serviceAccount:svc@y"}
    # one granted, one missing
    rm = {"roles/logging.logWriter": {"serviceAccount:dep@x"}, "roles/cloudtrace.agent": set()}
    out = p.compare_telemetry_grants(grants, resolved, rm)
    assert out[0]["status"] == p.OK
    assert out[1]["status"] == p.DRIFT and "lacks roles/cloudtrace.agent" in out[1]["detail"]
    # unreadable policy -> UNKNOWN drift, never false-missing
    out2 = p.compare_telemetry_grants(grants, resolved, None)
    assert all(f["status"] == p.DRIFT and "UNKNOWN" in f["detail"] for f in out2)
    # unresolvable member -> UNKNOWN
    out3 = p.compare_telemetry_grants(grants, {"deploy_sa": None, "apigee_service_agent": None}, rm)
    assert all("cannot resolve" in f["detail"] for f in out3)


# ── symbolic target-server hosts + shared-flow deps ───────────────────────────
def test_resolve_target_host(monkeypatch):
    # plain hosts pass through untouched
    assert p.resolve_target_host("aiplatform.googleapis.com", "o", "t") == \
        ("aiplatform.googleapis.com", "")
    # @endpointAttachment resolves via the org API
    monkeypatch.setattr(p, "_get", lambda url, tok: {"host": "10.9.9.9"})
    assert p.resolve_target_host("@endpointAttachment:ecommerce-services", "o", "t") == \
        ("10.9.9.9", "")
    # missing attachment -> (None, reason), never a fabricated host
    monkeypatch.setattr(p, "_get", lambda url, tok: None)
    host, reason = p.resolve_target_host("@endpointAttachment:x", "o", "t")
    assert host is None and "services provisioning" in reason


def test_compare_shared_flow_deps():
    assert p.compare_shared_flow_deps([], {}) == []
    out = p.compare_shared_flow_deps(["sf-custom-logging"], {"sf-custom-logging": True})
    assert out[0]["status"] == p.OK
    out2 = p.compare_shared_flow_deps(["sf-custom-logging"], {"sf-custom-logging": False})
    assert out2[0]["status"] == p.MISSING and "runtime" in out2[0]["detail"]
    unk = p.compare_shared_flow_deps(["sf-custom-logging"], None)
    assert unk[0]["status"] == p.DRIFT and "UNKNOWN" in unk[0]["detail"]


def _stub_findings_io(monkeypatch, org_number, minters):
    monkeypatch.setattr(p, "fetch_apps", lambda org, token: {})
    monkeypatch.setattr(p, "_org_project_number", lambda proj: org_number)
    monkeypatch.setattr(p, "_deployer_account", lambda: "Some.Admin@Example-Corp.Test.com")
    monkeypatch.setattr(p, "fetch_deploy_sa",
                        lambda sa, org: (True, {"user:Some.Admin@Example-Corp.Test.com"}, set(minters)))
    monkeypatch.setattr(p, "fetch_target_servers", lambda org, env, token: {})
    monkeypatch.setattr(p, "fetch_data_collectors", lambda org, token: {})
    monkeypatch.setattr(p, "fetch_products", lambda org, token: {})


def _min_manifest():
    return {"meta": {"org": "my-apigee-org", "env": "sandbox-internal",
                     "ai_project": "my-ai-project",
                     "deploy_sa": "apigee-ai-consumer@my-apigee-org.iam.gserviceaccount.com",
                     "ecommerce_deploy_sa": "cloud-run-invoker@my-apigee-org.iam.gserviceaccount.com"}}


def test_findings_check_token_minting_without_telemetry_grants(monkeypatch):
    """Regression (seen live, fresh org): the service agent was read from
    _telemetry_resolved, which only contains members named in telemetryGrants —
    with the optional cloudtrace.agent grant commented out, the tokenCreator
    check AND apply-time grant silently vanished, and every proxy call died
    AccessTokenGenerationFailure even after a full provision apply. The
    deploySA rows must flag the missing grant with NO telemetryGrants at all."""
    m = _min_manifest()
    _stub_findings_io(monkeypatch, "123", minters=[])
    findings = p._findings(m, "tok")
    agent = "serviceAccount:service-123@gcp-sa-apigee.iam.gserviceaccount.com"
    assert m["_apigee_service_agent"] == agent           # stashed for _execute
    sa_rows = [f for f in findings if f["kind"] == "deploySA"]
    assert len(sa_rows) == 2
    assert all(f["status"] == p.DRIFT and "TokenCreator" in f["detail"] for f in sa_rows)

    # grant in place -> both rows OK, token minting verified
    _stub_findings_io(monkeypatch, "123", minters=[agent])
    m2 = _min_manifest()
    sa_rows = [f for f in p._findings(m2, "tok") if f["kind"] == "deploySA"]
    assert all(f["status"] == p.OK and "token minting ✓" in f["detail"] for f in sa_rows)

    # org number unresolvable -> honest UNKNOWN row (routes to MANUAL), no false OK
    _stub_findings_io(monkeypatch, None, minters=[])
    m3 = _min_manifest()
    findings = p._findings(m3, "tok")
    unknown = [f for f in findings if f["name"] == "apigee-service-agent"]
    assert unknown and unknown[0]["status"] == p.UNKNOWN
    assert p.derive_plan(unknown, m3)[0]["verb"] == "MANUAL"


def test_compare_deploy_sa_states():
    sa = "cloud-run-invoker@my-org.iam.gserviceaccount.com"
    agent = "serviceAccount:service-123@gcp-sa-apigee.iam.gserviceaccount.com"
    assert p.compare_deploy_sa(None, None, None, None) == []          # not declared
    assert p.compare_deploy_sa(sa, None, None, "me@x")[0]["detail"].endswith("UNKNOWN")
    assert p.compare_deploy_sa(sa, False, None, "me@x")[0]["status"] == p.MISSING
    ok = p.compare_deploy_sa(sa, True, {"user:me@x"}, "me@x", agent, {agent})[0]
    assert ok["status"] == p.OK and "token minting ✓" in ok["detail"]
    drift = p.compare_deploy_sa(sa, True, set(), "me@x", agent, {agent})[0]
    assert drift["status"] == p.DRIFT and "actAs" in drift["detail"]
    # runtime gap found live: actAs fine but the service agent can't mint —
    # every GoogleAccessToken/GoogleIDToken call fails at RUN time
    mint = p.compare_deploy_sa(sa, True, {"user:me@x"}, "me@x", agent, set())[0]
    assert mint["status"] == p.DRIFT and "TokenCreator" in mint["detail"]
    # both problems reported together
    both = p.compare_deploy_sa(sa, True, set(), "me@x", agent, set())[0]
    assert "actAs" in both["detail"] and "TokenCreator" in both["detail"]
    # IAM returns the account's canonical casing — emails compare
    # case-insensitively (found live: user:First.Last@...)
    cased = p.compare_deploy_sa(sa, True, {"user:Me@X"}, "me@x", agent, {agent})[0]
    assert cased["status"] == p.OK
