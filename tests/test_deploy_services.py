"""Unit tests for the services deploy tool (pure comparators + derivations)."""
from pathlib import Path
import deploy_services as ds

SPEC = {"audience": "https://customers.ecommerce.internal"}
ENV = {"DB_HOST": "10.0.0.5", "DB_USER": "ecommerce-sa", "DB_NAME": "database"}
RUNTIME = "ecommerce-sa@p.iam.gserviceaccount.com"
INVOKER = "apigee-ai-consumer@my-apigee-org.iam.gserviceaccount.com"


def _live(**over):
    base = {"sa": RUNTIME, "ingress": "internal-and-cloud-load-balancing",
            "audiences": [SPEC["audience"]], "env": dict(ENV)}
    base.update(over)
    return base


def test_every_proxy_logs_interactions():
    """All 7 proxies (AI + ecommerce + MCP) carry ML-interaction in a
    PostClientFlow writing to the tokenized AI-project log."""
    root = Path(__file__).resolve().parents[1] / "apigee" / "proxies"
    for proxy_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        pol = proxy_dir / "apiproxy" / "policies" / "ML-interaction.xml"
        assert pol.exists(), f"{proxy_dir.name}: no ML-interaction policy"
        assert "projects/__AI_PROJECT__/logs/apigee-ai-logs" in pol.read_text()
        ep = (proxy_dir / "apiproxy" / "proxies" / "default.xml").read_text()
        assert "ML-interaction" in ep and "PostClientFlow" in ep, \
            f"{proxy_dir.name}: ML-interaction not attached in PostClientFlow"


def test_services_ship_ai_logging():
    root = Path(__file__).resolve().parents[1] / "services"
    for d in ("customers-service", "orders-service", "products-service"):
        assert (root / d / "app" / "ailog.py").exists()
        assert "install_ai_logging" in (root / d / "app" / "main.py").read_text()
        assert "google-cloud-logging" in (root / d / "requirements.txt").read_text()


def test_derived_env_computes_iam_db_user():
    m = {"project": "p", "identity": {"runtime_sa": "ecommerce-sa"},
         "database": {"name": "database"},
         "services": {"env": {"DB_HOST": "10.0.0.5"}, "customers": {}}}
    env = ds.derived_env(m)
    # Cloud SQL MYSQL stores IAM SA usernames as the LOCAL PART only — the
    # legacy deploy.sh value was right all along.
    assert env["DB_USER"] == "ecommerce-sa"
    assert env["DB_NAME"] == "database" and env["DB_HOST"] == "10.0.0.5"


def test_check_service_all_green():
    out = ds.check_service("customers", SPEC, ENV, INVOKER, RUNTIME, _live(),
                           {"members": {f"serviceAccount:{INVOKER}"}}, SPEC["audience"])
    assert all(f["status"] == ds.OK for f in out)


def test_check_service_flags_public_and_missing_invoker():
    out = ds.check_service("customers", SPEC, ENV, INVOKER, RUNTIME, _live(),
                           {"members": {"allUsers"}}, SPEC["audience"])
    iam = [f for f in out if f["name"].endswith("/iam")][0]
    assert iam["status"] == ds.DRIFT
    assert "PUBLIC" in iam["detail"] and "invoker" in iam["detail"]


def test_check_service_flags_stray_invokers():
    """After an invoker-SA change the OLD SA must surface as a stray (deploy
    reconciles to exactly the manifest SA)."""
    out = ds.check_service("customers", SPEC, ENV, INVOKER, RUNTIME, _live(),
                           {"members": {f"serviceAccount:{INVOKER}",
                                        "serviceAccount:old-invoker@x.iam.gserviceaccount.com"}},
                           SPEC["audience"])
    iam = [f for f in out if f["name"].endswith("/iam")][0]
    assert iam["status"] == ds.DRIFT and "old-invoker@x" in iam["detail"]


def test_check_service_env_audience_and_missing():
    assert ds.check_service("x", SPEC, ENV, INVOKER, RUNTIME, None, None, None)[0]["status"] == ds.MISSING
    out = ds.check_service("customers", SPEC, ENV, INVOKER, RUNTIME,
                           _live(env={**ENV, "DB_USER": "wrong-user"}, audiences=[]),
                           {"members": set()}, "https://WRONG")
    spec_row = out[0]
    assert spec_row["status"] == ds.DRIFT
    assert "custom audience" in spec_row["detail"] and "DB_USER" in spec_row["detail"]
    aud = [f for f in out if f["name"].endswith("/proxyAudience")][0]
    assert aud["status"] == ds.DRIFT and "WRONG" in aud["detail"]


def test_check_service_iam_unreadable_is_unknown():
    out = ds.check_service("customers", SPEC, ENV, INVOKER, RUNTIME, _live(),
                           None, SPEC["audience"])
    iam = [f for f in out if f["name"].endswith("/iam")][0]
    assert iam["status"] == ds.DRIFT and "UNKNOWN" in iam["detail"]
