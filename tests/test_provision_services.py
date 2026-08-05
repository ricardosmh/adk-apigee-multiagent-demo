"""Unit tests for the services-project comparators (pure, no I/O)."""
import provision_services as ps

NET = {
    "vpc": "serverless-vpc",
    "subnets": [
        {"name": "serverless-subnet", "range": "10.0.0.0/24"},
        {"name": "proxy-only-subnet", "range": "10.0.1.0/24", "purpose": "REGIONAL_MANAGED_PROXY"},
        {"name": "psc-nat-subnet", "range": "10.0.2.0/24", "purpose": "PRIVATE_SERVICE_CONNECT"},
    ],
}
LB = {
    "name": "rilb-ecommerce-services",
    "address": {"name": "ip-rilb-ecommerce", "ip": "10.0.0.11"},
    "port": 80,
    "global_access": True,
    "default_backend": "customers",
    "backends": {
        "customers": {"backend": "backend-ecommerce-customers", "neg": "sneg-ecommerce-customers"},
        "orders": {"backend": "backend-ecommerce-orders", "neg": "sneg-ecommerce-orders"},
    },
    "path_rules": {"/customer-management/*": "customers", "/order-management/*": "orders"},
}
SVCS = {"customers": {"run_service": "customers"}, "orders": {"run_service": "orders"}}


def _by(findings, kind):
    return {f["name"]: f for f in findings if f["kind"] == kind}


def test_iam_db_user_and_project_validation():
    # Cloud SQL MYSQL truncates the whole domain: username = SA local part
    # (the @<project>.iam long form is the POSTGRES convention).
    assert ps.iam_db_user("my-backend-project") == "ecommerce-sa"
    assert ps.validate_project("") is not None                      # empty
    assert ps.validate_project("my-backend-project") is None  # any length ok
    # only the SA NAME is capped by MySQL's 32-char username limit
    assert "32" in ps.validate_project("p", runtime_sa="x" * 33)


def test_compare_network_states():
    live_subs = {
        "serverless-subnet": {"range": "10.0.0.0/24", "purpose": "PRIVATE"},
        "proxy-only-subnet": {"range": "10.0.9.0/24", "purpose": "REGIONAL_MANAGED_PROXY"},
    }
    out = ps.compare_network(NET, {"serverless-vpc"}, live_subs)
    by = _by(out, "subnet")
    assert _by(out, "vpc")["serverless-vpc"]["status"] == ps.OK
    assert by["serverless-subnet"]["status"] == ps.OK
    assert by["proxy-only-subnet"]["status"] == ps.DRIFT and "range" in by["proxy-only-subnet"]["detail"]
    assert by["psc-nat-subnet"]["status"] == ps.MISSING
    # unreadable -> single honest UNKNOWN
    unk = ps.compare_network(NET, None, None)
    assert len(unk) == 1 and "UNKNOWN" in unk[0]["detail"]


def test_compare_sql_states():
    db = {"instance": "ecommerce-sql", "version": "MYSQL_8_0", "name": "database"}
    assert ps.compare_sql(db, "p", None, None, None)[0]["status"] == ps.MISSING
    inst = {"psc_enabled": True, "iam_flag": "on", "version": "MYSQL_8_0"}
    out = ps.compare_sql(db, "p", inst, {"database"}, {"ecommerce-sa"})
    assert all(f["status"] == ps.OK for f in out)
    bad = ps.compare_sql(db, "p", {"psc_enabled": False, "iam_flag": None,
                                   "version": "MYSQL_8_0"}, set(), set())
    assert bad[0]["status"] == ps.DRIFT and "PSC" in bad[0]["detail"]
    assert bad[1]["status"] == ps.MISSING and bad[2]["status"] == ps.MISSING


def test_compare_lb_states():
    live = {
        "negs": {"sneg-ecommerce-customers": "customers"},          # orders neg missing
        "backends": {"backend-ecommerce-customers": ["…/sneg-ecommerce-customers"]},
        "urlmap": {"default": ".../backend-ecommerce-customers",
                   "rules": {"/customer-management/*": ".../backend-ecommerce-customers"}},
        "fwd": {"ip": "10.0.0.11", "port": "80", "global_access": False},
    }
    out = ps.compare_lb(LB, SVCS, live)
    by_neg = _by(out, "neg")
    assert by_neg["sneg-ecommerce-customers"]["status"] == ps.OK
    assert by_neg["sneg-ecommerce-orders"]["status"] == ps.MISSING
    um = _by(out, "urlMap")[LB["name"]]
    assert um["status"] == ps.DRIFT and "/order-management/*" in um["detail"]
    fwd = _by(out, "forwardingRule")[LB["name"]]
    assert fwd["status"] == ps.DRIFT and "global access" in fwd["detail"]
    assert ps.compare_lb(LB, SVCS, None)[0]["detail"].endswith("UNKNOWN")


def test_compare_psc_states():
    psc = {"service_attachment": {"name": "sa-ecommerce-services",
                                  "connection_preference": "ACCEPT_MANUAL"},
           "apigee": {"org": "demo-apigeex", "endpoint_attachment": "ecommerce-services"}}
    out = ps.compare_psc(psc, None, None)
    assert out[0]["status"] == ps.MISSING and out[1]["status"] == ps.MISSING
    accepted_sa = {"connection_preference": "ACCEPT_MANUAL",
                   "accept_list": ["tenant-proj-123"],
                   "consumers": [{"project": "tenant-proj-123", "status": "ACCEPTED"}]}
    out2 = ps.compare_psc(psc, accepted_sa, {"host": "10.9.9.9", "connectionState": "ACCEPTED"})
    assert all(f["status"] == ps.OK for f in out2)
    assert "10.9.9.9" in out2[1]["detail"]
    pending = ps.compare_psc(psc, accepted_sa,
                             {"host": "10.9.9.9", "connectionState": "PENDING"})
    assert pending[1]["status"] == ps.DRIFT


def test_compare_psc_manual_accept_flags_unaccepted_tenant():
    """The consumer is Apigee's TENANT project (never the org project); a
    connected-but-unaccepted tenant must surface as DRIFT."""
    psc = {"service_attachment": {"name": "sa-x", "connection_preference": "ACCEPT_MANUAL"},
           "apigee": {"org": "o", "endpoint_attachment": "ea"}}
    sa_live = {"connection_preference": "ACCEPT_MANUAL", "accept_list": [],
               "consumers": [{"project": "gcp-tenant-42", "status": "PENDING"}]}
    out = ps.compare_psc(psc, sa_live, {"host": "h", "connectionState": "PENDING"})
    assert out[0]["status"] == ps.DRIFT and "gcp-tenant-42" in out[0]["detail"]
    assert "TENANT" in out[0]["detail"]


def test_consumer_project_from_endpoint():
    url = ("https://www.googleapis.com/compute/v1/projects/gcp-tenant-42/"
           "regions/southamerica-west1/forwardingRules/psc-ea-xyz")
    assert ps.consumer_project_from_endpoint(url) == "gcp-tenant-42"
    assert ps.consumer_project_from_endpoint("garbage") is None
    assert ps.consumer_project_from_endpoint("") is None


def test_compare_identity_build_sa_row():
    ident = {"runtime_sa": "ecommerce-sa", "roles": ["roles/cloudsql.client"],
             "build_sa_role": "roles/cloudbuild.builds.builder"}
    email = "ecommerce-sa@p.iam.gserviceaccount.com"
    out = ps.compare_identity(ident, "p", {email},
                              {email: {"roles/cloudsql.client"}}, "123",
                              {"user:me@x"}, "me@x")
    by = {f["kind"]: f for f in out}
    assert by["serviceAccount"]["status"] == ps.OK
    assert by["buildSA"]["status"] == ps.DRIFT     # compute default SA lacks builder
    assert by["deployerActAs"]["status"] == ps.OK
    # unknown project number -> honest UNKNOWN, not a false OK
    out2 = ps.compare_identity(ident, "p", {email},
                               {email: {"roles/cloudsql.client"}}, None, set(), None)
    assert any("UNKNOWN" in f["detail"] for f in out2 if f["kind"] == "buildSA")


def test_compare_identity_actas_email_case_insensitive():
    # IAM returns the member with the ACCOUNT's canonical casing (found live:
    # user:First.Last@... vs gcloud's lowercase) — must still be OK.
    ident = {"runtime_sa": "ecommerce-sa", "roles": [],
             "build_sa_role": "roles/cloudbuild.builds.builder"}
    email = "ecommerce-sa@p.iam.gserviceaccount.com"
    out = ps.compare_identity(ident, "p", {email}, {email: set()}, "123",
                              {"user:Me.Name@X.com"}, "me.name@x.com")
    by = {f["kind"]: f for f in out}
    assert by["deployerActAs"]["status"] == ps.OK


def test_compare_invoker_sa():
    """The ecommerce invoker SA lives in the APIGEE project but must exist
    before Phase A's deploy_services grants it run.invoker (fresh-org gap:
    provision.py apigee only creates it in Phase B)."""
    inv = "cloud-run-invoker@apigee-p.iam.gserviceaccount.com"
    assert ps.compare_invoker_sa(inv, "apigee-p", {inv})[0]["status"] == ps.OK
    missing = ps.compare_invoker_sa(inv, "apigee-p", set())[0]
    assert missing["status"] == ps.MISSING and "run.invoker" in missing["detail"]
    unknown = ps.compare_invoker_sa(inv, "apigee-p", None)[0]
    assert unknown["status"] == ps.DRIFT and "UNKNOWN" in unknown["detail"]


def test_compare_addresses():
    out = ps.compare_addresses([("a", "10.0.0.5"), ("b", "10.0.0.11")],
                               {"a": "10.0.0.5", "b": "10.0.0.99"})
    assert out[0]["status"] == ps.OK
    assert out[1]["status"] == ps.DRIFT
    assert ps.compare_addresses([("a", "x")], None)[0]["detail"].endswith("UNKNOWN")
