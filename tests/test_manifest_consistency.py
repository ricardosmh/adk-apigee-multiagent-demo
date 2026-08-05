"""Cross-manifest drift guard.

Agent identity is split across two contexts on purpose (see
apigee/PROVISIONING.md): the GATEWAY manifest (apigee/provision/manifest.yaml)
grants secret access to SA names it does not create; the AGENT manifest
(agents/runtime-manifest.yaml) owns SA lifecycle. The .env.examples reference
both. These tests are the one enforced truth across the three.
"""
from pathlib import Path

import demo_env

ROOT = Path(__file__).resolve().parents[1]


def _load(path):
    # environment tokens resolved — tests validate what the tools consume
    return demo_env.load_manifest(ROOT / path)


def _agents():
    return _load("agents/runtime-manifest.yaml")["agents"]


def _gateway():
    return _load("apigee/provision/manifest.yaml")


def test_gateway_accessors_match_agent_manifest():
    agents = _agents()
    gateway_sas = {s["agent"]: s for s in _gateway()["serviceAccounts"]}
    # gateway grants accessors for every agent PLUS the BFF (apigee-agent-key)
    assert set(gateway_sas) == set(agents) | {"bff"}, "identity sets differ between manifests"
    for agent, spec in agents.items():
        gw = gateway_sas[agent]
        assert gw["name"] == spec["sa"], f"{agent}: SA name differs ({gw['name']} vs {spec['sa']})"
        assert gw["secretAccessor"] == [spec["secret"]], f"{agent}: secret differs"
    bff_rt = _load("agents/runtime-manifest.yaml")["bff"]
    assert gateway_sas["bff"]["name"] == bff_rt["sa"]
    assert gateway_sas["bff"]["secretAccessor"] == ["apigee-agent-key"]
    # the admin Trace Explorer reads the four ai-logs → the BFF needs read access
    assert "roles/logging.viewer" in bff_rt["roles"], "BFF lost logging.viewer (Trace Explorer)"


def test_gateway_apps_feed_the_agent_secrets():
    agent_secrets = {spec["secret"] for spec in _agents().values()}
    app_secrets = {a["secret"] for a in _gateway()["apps"] if not a["secret"].startswith("<")}
    # every agent secret is fed by an app, plus the BFF's apigee-agent-key
    assert app_secrets == agent_secrets | {"apigee-agent-key"}, \
        "app→secret map doesn't cover the agent + BFF secrets"


def test_agent_dirs_exist_with_matching_env_examples():
    project = _load("agents/runtime-manifest.yaml")["project"]
    for agent, spec in _agents().items():
        env = ROOT / "agents" / spec["dir"] / ".env.example"
        assert env.exists(), f"{agent}: {env} missing"
        text = env.read_text()
        # .env is OPTIONAL now (manifest env carries runtime config); the
        # example must still DOCUMENT this agent's real secret name so an
        # override starts from the right value.
        assert f"APIGEE_API_KEY_SECRET={spec['secret']}" in text, \
            f"{agent}: .env.example documents the wrong secret (want {spec['secret']})"


def test_supervisor_is_the_only_privileged_agent():
    agents = _agents()
    for agent, spec in agents.items():
        privileged = {"roles/agentregistry.viewer", "roles/datastore.viewer"} & set(spec["roles"])
        if agent == "supervisor":
            assert privileged, "supervisor lost its registry/datastore roles"
        else:
            assert not privileged, f"{agent} must not hold supervisor-only roles"


# ── services manifest <-> gateway + proxy bundles ─────────────────────────────
def _services_manifest():
    return _load("services/services-manifest.yaml")


def test_services_invoker_is_the_ecommerce_proxy_deploy_sa():
    """Apigee mints GoogleIDToken as the PROXY'S deployment SA — run.invoker
    must be granted to the SA the ecommerce proxies deploy as (a dedicated
    cloud-run-invoker, NOT the AI proxies' apigee-ai-consumer)."""
    gw = _gateway()["meta"]
    assert _services_manifest()["identity"]["invoker_sa"] == gw["ecommerce_deploy_sa"]
    assert gw["ecommerce_deploy_sa"] != gw["deploy_sa"], "must stay a DEDICATED SA"


def test_proxy_bundle_audiences_match_services_manifest():
    """Custom-audience design: the bundles' <Audience> values are stable and
    must equal services.*.audience (deploy passes --add-custom-audiences)."""
    import re

    m = _services_manifest()
    proxy_for = {
        "customers": "ecommerce-customer-management-apiproxy",
        "orders": "ecommerce-order-management-apiproxy",
        "products": "ecommerce-product-management-apiproxy",
    }
    for svc, proxy in proxy_for.items():
        tdir = ROOT / "apigee" / "proxies" / proxy / "apiproxy" / "targets"
        xml = "\n".join(f.read_text() for f in tdir.glob("*.xml"))
        got = re.search(r"<Audience>([^<]+)</Audience>", xml).group(1)
        assert got == m["services"][svc]["audience"], f"{svc}: {got}"


def test_ecommerce_target_server_is_symbolic():
    """ts-ecommerce-services host must stay runtime-resolved, never hand-pasted."""
    ts = {t["name"]: t for t in _gateway()["targetServers"]}
    entry = ts["ts-ecommerce-services"]
    assert entry["host"].startswith("@endpointAttachment:")
    assert entry["port"] == 80 and entry["ssl"] is False
