#!/usr/bin/env python3
"""Central deploy tool for the e-commerce Cloud Run services.

Replaces the per-service deploy.sh scripts (which used --allow-unauthenticated
and a bare DB_USER that fails IAM auth). Driven by services/services-manifest.yaml:

    deploy_services.py --check                 read-only: live services vs manifest
    deploy_services.py --all | <name> ...      deploy (confirms); names: customers orders products
    deploy_services.py --apply --all | ...     deploy without prompting
    deploy_services.py --iam --all | ...       reconcile invoker IAM only (no rebuild)

Each deploy is `gcloud run deploy --source` with:
  * --no-allow-unauthenticated (IAM required; Apigee's deploy SA gets run.invoker)
  * --add-custom-audiences=<manifest audience> — the proxies' GoogleIDToken
    audiences are these STABLE values, so proxy XML never changes per project
  * VPC egress via serverless-subnet (Cloud SQL PSC + ILB are RFC1918)
  * env: DB_HOST/<flags> from the manifest; DB_USER = the SA local part
    (Cloud SQL MYSQL truncates the domain from IAM usernames)

--check verifies per service: exists, SA, ingress, custom audience, env vars,
IAM (NO allUsers; the Apigee invoker SA present) and the proxy XML audience.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent            # services/provision
SERVICES_DIR = HERE.parent                        # services/
REPO = SERVICES_DIR.parent
MANIFEST = SERVICES_DIR / "services-manifest.yaml"
PROXY_DIR = REPO / "apigee" / "proxies"
# service key -> proxy bundle carrying its GoogleIDToken audience
PROXY_FOR = {
    "customers": "ecommerce-customer-management-apiproxy",
    "orders": "ecommerce-order-management-apiproxy",
    "products": "ecommerce-product-management-apiproxy",
}

OK, DRIFT, MISSING = "OK", "DRIFT", "MISSING"
_SYMBOL = {OK: "✅", DRIFT: "⚠", MISSING: "❌"}


def _f(status, name, detail=""):
    return {"status": status, "name": name, "detail": detail}


def load_manifest(path=MANIFEST):
    """Manifest with demo-environment.yaml tokens (${projects.*} etc.) resolved."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    import demo_env

    return demo_env.load_manifest(path)


def service_names(m) -> list[str]:
    return [k for k in m["services"] if k != "env"]


def derived_env(m) -> dict:
    env = dict(m["services"].get("env") or {})
    # Cloud SQL MYSQL truncates the whole domain: the IAM DB username is just
    # the SA local part. (@<project>.iam is the Postgres convention.)
    env["DB_USER"] = m["identity"]["runtime_sa"]
    env["DB_NAME"] = m["database"]["name"]
    return env


def proxy_audience(service: str) -> str | None:
    """The GoogleIDToken audience inside the service's proxy bundle."""
    import re

    tdir = PROXY_DIR / PROXY_FOR[service] / "apiproxy" / "targets"
    for f in sorted(tdir.glob("*.xml")) if tdir.exists() else []:
        mm = re.search(r"<Audience>([^<]+)</Audience>", f.read_text())
        if mm:
            return mm.group(1)
    return None


# ── Pure comparators (unit-tested) ────────────────────────────────────────────
def check_service(name: str, spec: dict, want_env: dict, invoker_sa: str,
                  runtime_sa_email: str, live: dict | None,
                  iam: dict | None, xml_audience: str | None) -> list:
    """live = shaped Cloud Run service (None = not deployed):
    {sa, ingress, audiences: [..], env: {k: v}}; iam = {members: set()} or None."""
    out = []
    if live is None:
        return [_f(MISSING, name, "not deployed")]
    diffs = []
    if live.get("sa") != runtime_sa_email:
        diffs.append(f"SA={live.get('sa')} want {runtime_sa_email}")
    if live.get("ingress") != "internal-and-cloud-load-balancing":
        diffs.append(f"ingress={live.get('ingress')}")
    if spec["audience"] not in (live.get("audiences") or []):
        diffs.append(f"custom audience {spec['audience']} not set")
    for k, v in want_env.items():
        got = (live.get("env") or {}).get(k)
        if got != v:
            diffs.append(f"env {k}={got!r} want {v!r}")
    out.append(_f(DRIFT if diffs else OK, name,
                  "; ".join(diffs) if diffs else "spec ✓"))
    if iam is None:
        out.append(_f(DRIFT, f"{name}/iam", "could not read IAM policy — state UNKNOWN"))
    else:
        members = iam.get("members", set())
        problems = []
        if "allUsers" in members:
            problems.append("PUBLIC (allUsers invoker) — must be IAM-only")
        if f"serviceAccount:{invoker_sa}" not in members:
            problems.append(f"Apigee invoker {invoker_sa} missing run.invoker")
        strays = sorted(m for m in members
                        if m.startswith("serviceAccount:")
                        and m != f"serviceAccount:{invoker_sa}")
        if strays:
            problems.append("unexpected invoker(s): " + ", ".join(strays)
                            + " (deploy reconciles to the manifest SA only)")
        out.append(_f(DRIFT if problems else OK, f"{name}/iam",
                      "; ".join(problems) if problems else "IAM-only + invoker ✓"))
    if xml_audience is None:
        out.append(_f(DRIFT, f"{name}/proxyAudience", "no <Audience> found in proxy bundle"))
    else:
        match = xml_audience == spec["audience"]
        out.append(_f(OK if match else DRIFT, f"{name}/proxyAudience",
                      f"{xml_audience} ✓" if match
                      else f"proxy XML={xml_audience} manifest={spec['audience']}"))
    return out


# ── I/O ───────────────────────────────────────────────────────────────────────
def _gcloud(args, timeout=1800):
    # Non-interactive (fresh-project gcloud prompts hang captured subprocesses).
    r = subprocess.run(["gcloud", *args], capture_output=True, text=True,
                       timeout=timeout, stdin=subprocess.DEVNULL,
                       env={**os.environ, "CLOUDSDK_CORE_DISABLE_PROMPTS": "1"})
    return r.returncode == 0, r


def _gcloud_json(args):
    ok, r = _gcloud([*args, "--format=json"])
    if not ok:
        return None
    try:
        return json.loads(r.stdout or "null")
    except json.JSONDecodeError:
        return None


def fetch_service(project, region, name):
    data = _gcloud_json(["run", "services", "describe", name,
                         f"--project={project}", f"--region={region}"])
    if data is None:
        return None
    tmpl = data.get("spec", {}).get("template", {})
    meta = data.get("metadata", {})
    containers = tmpl.get("spec", {}).get("containers") or [{}]
    env = {e.get("name"): e.get("value")
           for e in (containers[0].get("env") or []) if "value" in e}
    auds = []
    raw = (meta.get("annotations") or {}).get("run.googleapis.com/custom-audiences")
    if raw:
        try:
            auds = json.loads(raw)
        except json.JSONDecodeError:
            auds = [raw]
    return {
        "sa": tmpl.get("spec", {}).get("serviceAccountName"),
        "ingress": (meta.get("annotations") or {}).get("run.googleapis.com/ingress"),
        "audiences": auds,
        "env": env,
    }


def fetch_iam(project, region, name):
    data = _gcloud_json(["run", "services", "get-iam-policy", name,
                         f"--project={project}", f"--region={region}"])
    if data is None:
        return None
    members = set()
    for b in data.get("bindings", []):
        if b.get("role") == "roles/run.invoker":
            members.update(b.get("members", []))
    return {"members": members}


# ── Modes ─────────────────────────────────────────────────────────────────────
def cmd_check(m) -> int:
    project, region = m["project"], m["region"]
    env = derived_env(m)
    runtime_email = f"{m['identity']['runtime_sa']}@{project}.iam.gserviceaccount.com"
    print(f"# services — project {project}, region {region}\n")
    findings = []
    for name in service_names(m):
        spec = m["services"][name]
        live = fetch_service(project, region, name)
        iam = fetch_iam(project, region, name) if live is not None else None
        findings += check_service(name, spec, env, m["identity"]["invoker_sa"],
                                  runtime_email, live, iam, proxy_audience(name))
    n_bad = 0
    for f in findings:
        line = f"{_SYMBOL[f['status']]} {f['status']:<7} service: {f['name']}"
        if f["detail"]:
            line += f" — {f['detail']}"
        print(line)
        n_bad += f["status"] != OK
    print(f"\n{len(findings) - n_bad}/{len(findings)} OK"
          + (f", {n_bad} to look at" if n_bad else ""))
    return 1 if n_bad else 0


def deploy_one(m, name) -> bool:
    project, region = m["project"], m["region"]
    spec = m["services"][name]
    env = derived_env(m)
    env_str = ",".join(f"{k}={v}" for k, v in env.items())
    runtime_email = f"{m['identity']['runtime_sa']}@{project}.iam.gserviceaccount.com"
    net = m["network"]
    print(f"── deploying {name} (source build; several minutes) ──", flush=True)
    ok, r = _gcloud([
        "run", "deploy", name,
        f"--project={project}", f"--region={region}",
        f"--source={SERVICES_DIR / spec['dir']}",
        "--platform=managed",
        "--no-allow-unauthenticated",
        "--ingress=internal-and-cloud-load-balancing",
        f"--service-account={runtime_email}",
        f"--network={net['vpc']}", f"--subnet={net['subnets'][0]['name']}",
        "--vpc-egress=private-ranges-only",
        f"--add-custom-audiences={spec['audience']}",
        f"--set-env-vars={env_str}",
        "--quiet",
    ])
    # Capture the source-build output (Cloud Build log) — it's the bulk of a
    # deploy log and was otherwise discarded. Goes to the repo-root deploy-logs/.
    import demo_env
    log = demo_env.deploy_log_dir("services") / f"{name}.log"
    log.write_text((r.stdout or "") + (r.stderr or ""))
    if not ok:
        print(f"  ❌ {name}: " + (r.stderr.strip().splitlines() or ["deploy failed"])[-1]
              + f"  (log: {log})")
        return False
    reconcile_iam(m, name)
    print(f"  ✅ {name} deployed (invoker: {m['identity']['invoker_sa']})  (log: {log})")
    return True


def reconcile_iam(m, name) -> bool:
    """Grant the manifest invoker run.invoker and remove stray SA invokers —
    without touching the deployed revision. Standalone via --iam (e.g. the
    invoker SA came into existence after the service was deployed)."""
    project, region = m["project"], m["region"]
    inv = m["identity"]["invoker_sa"]
    ok, r = _gcloud(["run", "services", "add-iam-policy-binding", name,
                     f"--project={project}", f"--region={region}",
                     f"--member=serviceAccount:{inv}", "--role=roles/run.invoker",
                     "--quiet"])
    if not ok:
        print(f"  ⚠ {name}: invoker grant failed: "
              + (r.stderr.strip().splitlines() or ["?"])[-1])
    # Reconcile EXACTLY: the manifest invoker is the only SA invoker (removes
    # e.g. a previous invoker after a manifest change).
    iam = fetch_iam(project, region, name) or {"members": set()}
    for member in sorted(iam["members"]):
        if member.startswith("serviceAccount:") and member != f"serviceAccount:{inv}":
            print(f"    removing stray invoker {member}", flush=True)
            _gcloud(["run", "services", "remove-iam-policy-binding", name,
                     f"--project={project}", f"--region={region}",
                     f"--member={member}", "--role=roles/run.invoker", "--quiet"])
    return ok


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Deploy the e-commerce Cloud Run services")
    ap.add_argument("names", nargs="*", help="customers orders products (or --all)")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--apply", action="store_true", help="deploy without prompting")
    ap.add_argument("--iam", action="store_true",
                    help="reconcile invoker IAM only — no build, no new revision")
    ap.add_argument("--manifest", default=str(MANIFEST))
    args = ap.parse_args(argv)

    m = load_manifest(args.manifest)
    if not m.get("project"):
        print("❌ project: is empty — fill services/services-manifest.yaml")
        return 1
    if args.check:
        return cmd_check(m)
    names = service_names(m) if (args.all or not args.names) else args.names
    unknown = [n for n in names if n not in service_names(m)]
    if unknown:
        print(f"unknown service(s): {unknown} — choose from {service_names(m)}")
        return 2
    if args.iam:
        failed = [n for n in names if not reconcile_iam(m, n)]
        if failed:
            print(f"\nIAM FAILED: {failed}")
            return 1
        print(f"invoker IAM reconciled on {names} ✓")
        return 0
    if not args.apply:
        if not sys.stdin.isatty():
            print("(no TTY — pass --apply)")
            return 1
        if input(f"Deploy {names}? [y/N] ").strip().lower() not in ("y", "yes"):
            return 1
    failed = [n for n in names if not deploy_one(m, n)]
    if failed:
        print(f"\nFAILED: {failed}")
        return 1
    print("\nall deployed. Next: provision_services.py --check (LB/NEGs bind by "
          "service name) and the smoke tests in the README.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
