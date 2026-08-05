"""demo-environment.yaml substitution + the no-hardcoded-ids guard."""
from pathlib import Path

import pytest

import demo_env

ROOT = Path(__file__).resolve().parents[1]
MANIFESTS = [
    "apigee/provision/manifest.yaml",
    "agents/runtime-manifest.yaml",
    "services/services-manifest.yaml",
]


def test_substitute_resolves_tokens():
    env = {"projects": {"ai": "my-ai", "apigee": "my-org"}, "apigee_env": "dev"}
    flat = demo_env._flatten(env)
    obj = {"meta": {"org": "${projects.apigee}", "sa": "x@${projects.apigee}.iam"},
           "envs": ["${apigee_env}"], "n": 3}
    out = demo_env.substitute(obj, flat)
    assert out["meta"]["org"] == "my-org"
    assert out["meta"]["sa"] == "x@my-org.iam"
    assert out["envs"] == ["dev"] and out["n"] == 3


def test_substitute_resolves_dict_keys():
    """The ACL seed keys user roles on ${identity.admin_email} — keys must
    substitute exactly like values."""
    flat = demo_env._flatten({"identity": {"admin_email": "me@corp.com"}})
    out = demo_env.substitute({"users": {"${identity.admin_email}": ["admin"]}}, flat)
    assert out["users"] == {"me@corp.com": ["admin"]}


def test_unknown_token_fails_loudly():
    with pytest.raises(KeyError):
        demo_env.substitute("${projects.typo}", {"projects.ai": "x"})


def test_manifests_have_no_hardcoded_project_ids():
    """The environment file is the ONLY place naming projects — reintroducing
    a literal id in a manifest is a regression."""
    env = demo_env.load_env()
    ids = [env["projects"]["apigee"], env["projects"]["ai"], env["projects"]["backend"]]
    for m in MANIFESTS:
        text = (ROOT / m).read_text()
        for pid in ids:
            assert pid not in text, f"{m} hardcodes {pid} — use ${{projects.*}}"
        assert "@gcp-sa-aiplatform" not in text or "service-" not in text.split(
            "@gcp-sa-aiplatform")[0][-15:], f"{m} hardcodes a project NUMBER"


DEPLOYABLES = [
    "frontend/service.yaml",
    *[str(p.relative_to(ROOT)) for p in (ROOT / "apigee" / "proxies").glob("*/apiproxy/**/*.xml")],
]


def test_deployables_have_no_hardcoded_project_refs():
    """Deployables carry __TOKENS__ rendered at deploy time — literal project
    ids or project NUMBERS (projects/<digits>) are regressions."""
    import re

    env = demo_env.load_env()
    ids = list(env["projects"].values())
    for rel in DEPLOYABLES:
        text = (ROOT / rel).read_text()
        for pid in ids:
            assert pid not in text, f"{rel} hardcodes {pid}"
        m = re.search(r"projects/(\d{6,})", text)
        assert not m, f"{rel} hardcodes project number {m.group(1)}"


def test_manifests_load_and_resolve():
    env = demo_env.load_env()
    apigee = demo_env.load_manifest(ROOT / "apigee/provision/manifest.yaml")
    assert apigee["meta"]["org"] == env["projects"]["apigee"]
    assert apigee["meta"]["ai_project"] == env["projects"]["ai"]
    assert env["projects"]["apigee"] in apigee["meta"]["ecommerce_deploy_sa"]
    agents = demo_env.load_manifest(ROOT / "agents/runtime-manifest.yaml")
    assert agents["project"] == env["projects"]["ai"]
    services = demo_env.load_manifest(ROOT / "services/services-manifest.yaml")
    assert services["project"] == env["projects"]["backend"]
    assert services["psc"]["apigee"]["org"] == env["projects"]["apigee"]


def test_identity_flows_from_environment_file():
    """Every identity-bearing manifest field derives from
    ${identity.admin_email} — a fresh deployer sets ONE value in
    demo-environment.yaml and IAP, the ACL admin, the Apigee developer and
    the first end user all follow."""
    email = demo_env.load_env()["identity"]["admin_email"]
    agents = demo_env.load_manifest(ROOT / "agents/runtime-manifest.yaml")
    assert f"user:{email}" in agents["bff"]["iap_members"]
    assert agents["acl"]["users"].get(email) == ["admin"]
    apigee = demo_env.load_manifest(ROOT / "apigee/provision/manifest.yaml")
    assert apigee["developer"]["email"] == email
    assert email in apigee["endUsers"]["emails"]


# Real accounts live in personal/demo domains; repo examples use example.com
# (or the ${identity.admin_email} token). Any address in these domains is
# someone's identity leaking into a distributable repo — pattern-based so the
# guard itself names no one.
def test_no_personal_identities_anywhere():
    import re
    import subprocess

    pat = re.compile(
        r"[A-Za-z0-9._%+-]+@(?:gmail\.com|googlemail\.com|google\.com"
        r"|[A-Za-z0-9.-]*altostrat\.com)|(?<![A-Za-z0-9])altostrat\.com")
    out = subprocess.run(["git", "ls-files"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout
    bad = []
    for rel in out.splitlines():
        p = ROOT / rel
        if not p.is_file() or p.suffix in {".png", ".jpg", ".ico", ".zip"}:
            continue
        for m in pat.finditer(p.read_text(errors="ignore")):
            bad.append(f"{rel}: '{m.group(0)}'")
    assert not bad, ("personal identities belong ONLY in your local "
                     "demo-environment.yaml edit — use example.com in "
                     "examples:\n" + "\n".join(bad))
