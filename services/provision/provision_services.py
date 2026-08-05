#!/usr/bin/env python3
"""E-commerce services project provisioning — manifest-driven, SERVICES side.

Reconciles everything the services project needs EXCEPT the Cloud Run deploys
(those live in deploy_services.py), declared in services/services-manifest.yaml:

  * required Google APIs enabled
  * VPC + subnets (workload, REGIONAL_MANAGED_PROXY, PSC NAT)
  * runtime SA (ecommerce-sa) + roles; the compute default SA's build role
    (fresh projects build --source deploys as that SA); deployer actAs
  * Cloud SQL MySQL instance (PSC-only, cloudsql_iam_authentication=on),
    reserved PSC endpoint address + consumer forwarding rule, the `database`
    database, the IAM DB user (ecommerce-sa@<project>.iam)
  * the regional INTERNAL Application LB chain: serverless NEGs, backend
    services, url-map (path rules), target HTTP proxy, reserved address,
    forwarding rule (global access)
  * the Apigee southbound PSC chain: ILB service attachment + the Apigee
    endpoint attachment (host consumed by apigee/provision/manifest.yaml's
    ts-ecommerce-services target server at run time)

    python provision_services.py             # report + confirm before writing
    python provision_services.py --check     # read-only report, exit 1 on drift
    python provision_services.py --dry-run   # report only, write nothing
    python provision_services.py --apply     # write without prompting
    python provision_services.py seed        # DESTRUCTIVE RESET: import sample
                                             # data + IAM-user GRANTs (confirms)

Fresh-project order of operations: services/provision/README.md.
Needs: gcloud (authed). Pure comparators are unit-tested in tests/.
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# macOS python.org builds ship no system CAs — use certifi (repo-wide pattern).
try:
    import certifi

    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:  # certifi absent — platform default
    _SSL_CTX = ssl.create_default_context()

HERE = Path(__file__).resolve().parent            # services/provision
SERVICES_DIR = HERE.parent                        # services/
MANIFEST = SERVICES_DIR / "services-manifest.yaml"
APIGEE = "https://apigee.googleapis.com/v1"

OK, DRIFT, MISSING = "OK", "DRIFT", "MISSING"
_SYMBOL = {OK: "✅", DRIFT: "⚠", MISSING: "❌"}


def _f(status, kind, name, detail=""):
    return {"status": status, "kind": kind, "name": name, "detail": detail}


def sa_email(name: str, project: str) -> str:
    return f"{name}@{project}.iam.gserviceaccount.com"


def iam_db_user(project: str, runtime_sa: str = "ecommerce-sa") -> str:
    """MySQL IAM username for the runtime SA. Cloud SQL for MYSQL truncates
    the ENTIRE domain — the DB username is just the SA's local part
    ('ecommerce-sa'). The @<project>.iam long form is the POSTGRES convention;
    using it here fails auth. (The user is still CREATED with the full SA
    email via `gcloud sql users create`.)"""
    return runtime_sa


def validate_project(project: str, runtime_sa: str = "ecommerce-sa") -> str | None:
    """MySQL caps usernames at 32 chars — for MySQL that constrains only the
    SA NAME (the project id never enters the username)."""
    if not project:
        return "project: is empty — fill services/services-manifest.yaml"
    if len(runtime_sa) > 32:
        return f"runtime SA name '{runtime_sa}' exceeds MySQL's 32-char username cap"
    return None


# ── Pure comparators ──────────────────────────────────────────────────────────
def compare_services_enabled(required: list, enabled: set | None, list_error="") -> list:
    if enabled is None:
        return [_f(DRIFT, "apis", "listing",
                   f"could not list enabled services — state UNKNOWN ({list_error})")]
    return [_f(OK if s in enabled else MISSING, "api", s,
               "enabled ✓" if s in enabled else "not enabled") for s in required]


def compare_network(net: dict, live_vpcs: set | None, live_subnets: dict | None) -> list:
    """live_subnets = {name: {"range":…, "purpose":…}} (None = unreadable)."""
    out = []
    if live_vpcs is None or live_subnets is None:
        return [_f(DRIFT, "network", "listing", "could not list VPC/subnets — state UNKNOWN")]
    vpc = net["vpc"]
    out.append(_f(OK if vpc in live_vpcs else MISSING, "vpc", vpc,
                  "exists ✓" if vpc in live_vpcs else "does not exist"))
    for s in net.get("subnets", []):
        live = live_subnets.get(s["name"])
        if live is None:
            out.append(_f(MISSING, "subnet", s["name"], f"want {s['range']}"))
            continue
        diffs = []
        if live.get("range") != s["range"]:
            diffs.append(f"range={live.get('range')} want {s['range']}")
        want_purpose = s.get("purpose", "PRIVATE")
        if (live.get("purpose") or "PRIVATE") != want_purpose:
            diffs.append(f"purpose={live.get('purpose')} want {want_purpose}")
        out.append(_f(DRIFT if diffs else OK, "subnet", s["name"], "; ".join(diffs)))
    return out


def compare_identity(ident: dict, project: str, existing_sas: set,
                     role_map: dict, project_number: str | None,
                     actas_members: set | None, deployer: str | None) -> list:
    out = []
    email = sa_email(ident["runtime_sa"], project)
    if email not in existing_sas:
        out.append(_f(MISSING, "serviceAccount", ident["runtime_sa"]))
    else:
        missing = [r for r in ident.get("roles", []) if r not in role_map.get(email, set())]
        out.append(_f(DRIFT if missing else OK, "serviceAccount", ident["runtime_sa"],
                      "missing roles: " + ", ".join(missing) if missing else ""))
    if project_number:
        build_sa = f"{project_number}-compute@developer.gserviceaccount.com"
        has = ident["build_sa_role"] in role_map.get(build_sa, set())
        out.append(_f(OK if has else DRIFT, "buildSA", build_sa,
                      f"{ident['build_sa_role']} ✓" if has
                      else f"lacks {ident['build_sa_role']} — --source deploys will fail"))
    else:
        out.append(_f(DRIFT, "buildSA", "compute-default",
                      "project number unknown — state UNKNOWN"))
    if deployer:
        # emails compare case-insensitively: IAM returns the member with the
        # ACCOUNT's canonical casing (found live: user:First.Last@...
        # vs gcloud config's lowercase → permanent false DRIFT).
        has = actas_members is not None and (
            f"user:{deployer}".lower() in {m.lower() for m in actas_members})
        out.append(_f(OK if has else DRIFT, "deployerActAs", ident["runtime_sa"],
                      "" if has else f"{deployer} cannot actAs (needed for --service-account)"))
    return out


def compare_invoker_sa(invoker_sa: str, apigee_project: str,
                       existing: set | None) -> list:
    """The SA the ecommerce proxies run as lives in the APIGEE project, but
    deploy_services grants it run.invoker in Phase A — before `provision.py
    apigee` (Phase B, which also declares it) has run on a fresh org. Ensure
    it exists here so the grant can't race the phase order; creation is
    idempotent whichever tool gets there first."""
    if existing is None:
        return [_f(DRIFT, "invokerSA", invoker_sa,
                   f"could not list SAs in {apigee_project} — state UNKNOWN")]
    if invoker_sa in existing:
        return [_f(OK, "invokerSA", invoker_sa, f"exists in {apigee_project} ✓")]
    return [_f(MISSING, "invokerSA", invoker_sa,
               f"not in {apigee_project} — deploy_services' run.invoker "
               "grant will fail")]


def compare_sql(db: dict, project: str, inst: dict | None,
                databases: set | None, users: set | None) -> list:
    """inst = relevant fields of the live instance (None = absent)."""
    out = []
    name = db["instance"]
    if inst is None:
        return [_f(MISSING, "sqlInstance", name,
                   "does not exist (PSC + IAM flags must be set AT CREATE)")]
    diffs = []
    if not inst.get("psc_enabled"):
        diffs.append("PSC not enabled (cannot be retrofitted — recreate)")
    if inst.get("iam_flag") != "on":
        diffs.append("cloudsql_iam_authentication flag not on")
    if inst.get("version") and inst["version"] != db["version"]:
        diffs.append(f"version={inst['version']} want {db['version']}")
    out.append(_f(DRIFT if diffs else OK, "sqlInstance", name, "; ".join(diffs)))
    if databases is not None:
        has = db["name"] in databases
        out.append(_f(OK if has else MISSING, "sqlDatabase", db["name"],
                      "exists ✓" if has else "not created"))
    user = iam_db_user(project)   # short name — how Cloud SQL MySQL stores it
    if users is not None:
        has = user in users
        out.append(_f(OK if has else MISSING, "sqlIamUser", user,
                      "exists ✓" if has else "IAM DB user not created"))
    return out


def compare_addresses(wanted: list, live: dict | None) -> list:
    """wanted = [(name, ip)]; live = {name: ip}."""
    if live is None:
        return [_f(DRIFT, "addresses", "listing", "could not list addresses — state UNKNOWN")]
    out = []
    for name, ip in wanted:
        got = live.get(name)
        if got is None:
            out.append(_f(MISSING, "address", name, f"want {ip}"))
        else:
            out.append(_f(OK if got == ip else DRIFT, "address", name,
                          f"{got} ✓" if got == ip else f"{got} want {ip}"))
    return out


def compare_lb(lb: dict, services: dict, live: dict | None) -> list:
    """live = {'negs': {name: cloud_run_service}, 'backends': {name: [neg names]},
    'urlmap': {'default': backend, 'rules': {path: backend}} | None,
    'fwd': {'ip':…, 'port':…, 'global_access': bool} | None}."""
    if live is None:
        return [_f(DRIFT, "lb", "listing", "could not read LB state — state UNKNOWN")]
    out = []
    for svc, spec in lb["backends"].items():
        neg, backend = spec["neg"], spec["backend"]
        got_svc = (live.get("negs") or {}).get(neg)
        want_run_svc = services[svc]["run_service"]
        if got_svc is None:
            out.append(_f(MISSING, "neg", neg, f"want -> {want_run_svc}"))
        else:
            out.append(_f(OK if got_svc == want_run_svc else DRIFT, "neg", neg,
                          f"-> {got_svc}" + ("" if got_svc == want_run_svc else f" want {want_run_svc}")))
        got_negs = (live.get("backends") or {}).get(backend)
        if got_negs is None:
            out.append(_f(MISSING, "backendService", backend))
        else:
            has = any(neg in n for n in got_negs)
            out.append(_f(OK if has else DRIFT, "backendService", backend,
                          "neg attached ✓" if has else f"neg {neg} not attached"))
    um = live.get("urlmap")
    if um is None:
        out.append(_f(MISSING, "urlMap", lb["name"]))
    else:
        diffs = []
        want_default = lb["backends"][lb["default_backend"]]["backend"]
        if want_default not in (um.get("default") or ""):
            diffs.append(f"default={um.get('default')} want {want_default}")
        for path, svc in lb["path_rules"].items():
            want_b = lb["backends"][svc]["backend"]
            got_b = (um.get("rules") or {}).get(path)
            if not got_b or want_b not in got_b:
                diffs.append(f"{path} -> {got_b or 'MISSING'} want {want_b}")
        out.append(_f(DRIFT if diffs else OK, "urlMap", lb["name"], "; ".join(diffs)))
    fwd = live.get("fwd")
    if fwd is None:
        out.append(_f(MISSING, "forwardingRule", lb["name"],
                      f"want {lb['address']['ip']}:{lb['port']}"))
    else:
        diffs = []
        if fwd.get("ip") != lb["address"]["ip"]:
            diffs.append(f"ip={fwd.get('ip')} want {lb['address']['ip']}")
        if str(fwd.get("port")) != str(lb["port"]):
            diffs.append(f"port={fwd.get('port')} want {lb['port']}")
        if lb.get("global_access") and not fwd.get("global_access"):
            diffs.append("global access OFF (Apigee PSC path needs it)")
        out.append(_f(DRIFT if diffs else OK, "forwardingRule", lb["name"], "; ".join(diffs)))
    return out


def consumer_project_from_endpoint(url: str) -> str | None:
    """The consumer project id out of a connectedEndpoints[].endpoint URL
    (…/projects/<TENANT>/regions/…) — for Apigee this is the Google-managed
    TENANT project, the one an accept list must contain."""
    parts = (url or "").split("/")
    try:
        return parts[parts.index("projects") + 1]
    except (ValueError, IndexError):
        return None


def compare_psc(psc: dict, sa_live: dict | None, ea_live: dict | None) -> list:
    out = []
    name = psc["service_attachment"]["name"]
    if sa_live is None:
        out.append(_f(MISSING, "serviceAttachment", name, "not created"))
    else:
        pref = sa_live.get("connection_preference")
        want = psc["service_attachment"]["connection_preference"]
        diffs = []
        if pref != want:
            diffs.append(f"{pref} want {want}")
        # ACCEPT_MANUAL: every connected consumer (the Apigee tenant project)
        # must be in the accept list, or the connection stays PENDING.
        if want == "ACCEPT_MANUAL":
            accept = set(sa_live.get("accept_list") or [])
            pending = [c for c in sa_live.get("consumers") or []
                       if c["project"] and c["project"] not in accept]
            if pending:
                diffs.append("pending consumer(s) not accepted: "
                             + ", ".join(c["project"] for c in pending)
                             + " (the Apigee TENANT project — apply accepts it)")
        out.append(_f(DRIFT if diffs else OK, "serviceAttachment", name, "; ".join(diffs)))
    ea = psc["apigee"]["endpoint_attachment"]
    if ea_live is None:
        out.append(_f(MISSING, "endpointAttachment", ea,
                      f"not created in org {psc['apigee']['org']}"))
    else:
        state = ea_live.get("connectionState") or ea_live.get("state") or "?"
        host = ea_live.get("host", "?")
        good = state in ("ACCEPTED", "ACTIVE")
        out.append(_f(OK if good else DRIFT, "endpointAttachment", ea,
                      f"host={host} state={state}" + ("" if good else " — not ACCEPTED yet")))
    return out


# ── gcloud / REST I/O ────────────────────────────────────────────────────────
def _gcloud(args: list, timeout=600):
    # STRICTLY non-interactive: on fresh projects gcloud PROMPTS ("API not
    # enabled. Enable and retry? (y/N)") and hangs a captured subprocess until
    # timeout (seen live). Prompts disabled -> fails fast -> honest UNKNOWN.
    r = subprocess.run(["gcloud", *args], capture_output=True, text=True,
                       timeout=timeout, stdin=subprocess.DEVNULL,
                       env={**os.environ, "CLOUDSDK_CORE_DISABLE_PROMPTS": "1"})
    return r.returncode == 0, r


def _gcloud_json(args: list):
    ok, r = _gcloud([*args, "--format=json"])
    if not ok:
        return None
    try:
        return json.loads(r.stdout or "null")
    except json.JSONDecodeError:
        return None


def _token() -> str:
    ok, r = _gcloud(["auth", "print-access-token"])
    if not ok:
        raise SystemExit("gcloud auth print-access-token failed — run gcloud auth login")
    return r.stdout.strip()


def _get(url: str, token: str):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30, context=_SSL_CTX) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def _write_api(method: str, url: str, token: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {token}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60, context=_SSL_CTX) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:500]
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"{method} {url} -> HTTP {e.code}: {detail}") from e


def fetch_enabled_apis(project):
    ok, r = _gcloud(["services", "list", "--enabled", f"--project={project}",
                     "--format=value(config.name)"])
    if not ok:
        return None, (r.stderr.strip().splitlines() or ["exit != 0"])[-1]
    return {ln.strip() for ln in r.stdout.splitlines() if ln.strip()}, ""


def fetch_network(project, region):
    vpcs = _gcloud_json(["compute", "networks", "list", f"--project={project}"])
    subs = _gcloud_json(["compute", "networks", "subnets", "list",
                         f"--project={project}", f"--regions={region}"])
    if vpcs is None or subs is None:
        return None, None
    return ({v["name"] for v in vpcs},
            {s["name"]: {"range": s.get("ipCidrRange"), "purpose": s.get("purpose", "PRIVATE")}
             for s in subs})


def fetch_project_number(project):
    ok, r = _gcloud(["projects", "describe", project, "--format=value(projectNumber)"])
    return r.stdout.strip() if ok and r.stdout.strip() else None


def fetch_sas(project):
    data = _gcloud_json(["iam", "service-accounts", "list", f"--project={project}"])
    return None if data is None else {sa["email"] for sa in data}


def fetch_role_map(project):
    data = _gcloud_json(["projects", "get-iam-policy", project])
    if data is None:
        return {}
    out: dict[str, set] = {}
    for b in data.get("bindings", []):
        for m in b.get("members", []):
            if m.startswith("serviceAccount:"):
                out.setdefault(m.split(":", 1)[1], set()).add(b.get("role"))
    return out


def fetch_actas(project, email):
    data = _gcloud_json(["iam", "service-accounts", "get-iam-policy", email,
                         f"--project={project}"])
    if data is None:
        return None
    out = set()
    for b in data.get("bindings", []):
        if b.get("role") == "roles/iam.serviceAccountUser":
            out.update(b.get("members", []))
    return out


def fetch_sql(project, db):
    inst = _gcloud_json(["sql", "instances", "describe", db["instance"], f"--project={project}"])
    if inst is None:
        return None, None, None
    flags = {f.get("name"): f.get("value")
             for f in (inst.get("settings", {}).get("databaseFlags") or [])}
    shaped = {
        "psc_enabled": bool(inst.get("settings", {}).get("ipConfiguration", {})
                            .get("pscConfig", {}).get("pscEnabled")),
        "iam_flag": flags.get("cloudsql_iam_authentication"),
        "version": inst.get("databaseVersion"),
        "psc_link": inst.get("pscServiceAttachmentLink"),
        "service_account": inst.get("serviceAccountEmailAddress"),
    }
    dbs = _gcloud_json(["sql", "databases", "list", f"--instance={db['instance']}",
                        f"--project={project}"])
    users = _gcloud_json(["sql", "users", "list", f"--instance={db['instance']}",
                          f"--project={project}"])
    return (shaped,
            None if dbs is None else {d["name"] for d in dbs},
            None if users is None else {u["name"] for u in users})


def fetch_addresses(project, region):
    data = _gcloud_json(["compute", "addresses", "list", f"--project={project}",
                         f"--regions={region}"])
    return None if data is None else {a["name"]: a.get("address") for a in data}


def fetch_lb(project, region, lb):
    negs = _gcloud_json(["compute", "network-endpoint-groups", "list",
                         f"--project={project}", f"--regions={region}"])
    if negs is None:
        return None
    neg_map = {}
    for n in negs:
        run_svc = (n.get("cloudRun") or {}).get("service")
        neg_map[n["name"]] = run_svc
    backends = {}
    for spec in lb["backends"].values():
        b = _gcloud_json(["compute", "backend-services", "describe", spec["backend"],
                          f"--project={project}", f"--region={region}"])
        if b is not None:
            backends[spec["backend"]] = [x.get("group", "") for x in b.get("backends", [])]
    um = _gcloud_json(["compute", "url-maps", "describe", lb["name"],
                       f"--project={project}", f"--region={region}"])
    urlmap = None
    if um is not None:
        rules = {}
        for pm in um.get("pathMatchers", []):
            for pr in pm.get("pathRules", []):
                for p in pr.get("paths", []):
                    rules[p] = pr.get("service", "")
        urlmap = {"default": um.get("defaultService", ""), "rules": rules}
    fr = _gcloud_json(["compute", "forwarding-rules", "describe", lb["name"],
                       f"--project={project}", f"--region={region}"])
    fwd = None
    if fr is not None:
        fwd = {"ip": fr.get("IPAddress"), "port": (fr.get("portRange") or "").split("-")[0],
               "global_access": bool(fr.get("allowGlobalAccess"))}
    return {"negs": neg_map, "backends": backends, "urlmap": urlmap, "fwd": fwd}


def fetch_psc(project, region, psc, token):
    sa = _gcloud_json(["compute", "service-attachments", "describe",
                       psc["service_attachment"]["name"],
                       f"--project={project}", f"--region={region}"])
    sa_shaped = None if sa is None else {
        "connection_preference": sa.get("connectionPreference"),
        "accept_list": [a.get("projectIdOrNum") for a in sa.get("consumerAcceptLists") or []],
        "consumers": [{"project": consumer_project_from_endpoint(c.get("endpoint", "")),
                       "status": c.get("status")}
                      for c in sa.get("connectedEndpoints") or []],
    }
    org = psc["apigee"]["org"]
    ea = _get(f"{APIGEE}/organizations/{org}/endpointAttachments/"
              f"{psc['apigee']['endpoint_attachment']}", token)
    return sa_shaped, ea


# ── Report + plan machinery ───────────────────────────────────────────────────
def report(findings) -> int:
    for f in findings:
        line = f"{_SYMBOL[f['status']]} {f['status']:<7} {f['kind']}: {f['name']}"
        if f["detail"]:
            line += f" — {f['detail']}"
        print(line)
    n_bad = sum(1 for f in findings if f["status"] != OK)
    print(f"\n{len(findings) - n_bad}/{len(findings)} OK"
          + (f", {n_bad} to reconcile" if n_bad else ""))
    return n_bad


def should_write(args, n: int) -> bool:
    if n == 0:
        return False
    if getattr(args, "apply", False):
        return True
    if getattr(args, "dry_run", False):
        return False
    if not sys.stdin.isatty():
        print("\n(no TTY — nothing applied. Pass --apply to run non-interactively.)")
        return False
    return input(f"\nApply {n} change(s)? [y/N] ").strip().lower() in ("y", "yes")


def load_manifest(path=MANIFEST):
    """Manifest with demo-environment.yaml tokens (${projects.*} etc.) resolved."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    import demo_env

    return demo_env.load_manifest(path)


# ── Apply ─────────────────────────────────────────────────────────────────────
def _run(args: list, timeout=900):
    ok, r = _gcloud(args, timeout=timeout)
    if not ok:
        tail = (r.stderr.strip().splitlines() or ["?"])[-1]
        print(f"    ! gcloud {' '.join(args[:4])}… failed: {tail}")
    return ok


def _apply(m, findings, token):
    project, region = m["project"], m["region"]
    bad = {(f["kind"], f["name"]) for f in findings if f["status"] != OK}

    def needs(kind, name=None):
        return any(k == kind and (name is None or n == name) for k, n in bad)

    # Enable only the APIs that are MISSING. If the listing itself was UNKNOWN
    # (kind "apis" — fresh project, serviceusage not yet reachable) we can't tell
    # which are on, so fall back to enabling the whole list.
    missing_apis = [n for (k, n) in bad if k == "api"]
    todo = m["apis"] if needs("apis") else missing_apis
    if todo:
        print(f"  enabling {len(todo)} API(s) ...", flush=True)
        _run(["services", "enable", *todo, f"--project={project}"])

    net = m["network"]
    if needs("vpc"):
        _run(["compute", "networks", "create", net["vpc"], f"--project={project}",
              "--subnet-mode=custom"])
    for s in net.get("subnets", []):
        if not needs("subnet", s["name"]):
            continue
        args = ["compute", "networks", "subnets", "create", s["name"],
                f"--project={project}", f"--region={region}",
                f"--network={net['vpc']}", f"--range={s['range']}"]
        if s.get("purpose") == "REGIONAL_MANAGED_PROXY":
            args += ["--purpose=REGIONAL_MANAGED_PROXY", "--role=ACTIVE"]
        elif s.get("purpose") == "PRIVATE_SERVICE_CONNECT":
            args += ["--purpose=PRIVATE_SERVICE_CONNECT"]
        _run(args)

    ident = m["identity"]
    email = sa_email(ident["runtime_sa"], project)
    if needs("serviceAccount"):
        _run(["iam", "service-accounts", "create", ident["runtime_sa"],
              f"--project={project}", "--display-name=E-commerce services runtime"])
        for _ in range(12):
            data = _gcloud_json(["iam", "service-accounts", "describe", email,
                                 f"--project={project}"])
            if data:
                break
            time.sleep(5)
    for role in ident.get("roles", []):
        _run(["projects", "add-iam-policy-binding", project,
              f"--member=serviceAccount:{email}", f"--role={role}", "--quiet"])
    if needs("buildSA"):
        pn = fetch_project_number(project)
        if pn:
            _run(["projects", "add-iam-policy-binding", project,
                  f"--member=serviceAccount:{pn}-compute@developer.gserviceaccount.com",
                  f"--role={ident['build_sa_role']}", "--quiet"])
    if needs("deployerActAs"):
        deployer = _deployer()
        if deployer:
            _run(["iam", "service-accounts", "add-iam-policy-binding", email,
                  f"--project={project}", f"--member=user:{deployer}",
                  "--role=roles/iam.serviceAccountUser", "--quiet"])
    if needs("invokerSA"):
        # Lives in the APIGEE project (provision.py apigee reconciles its
        # grants later); created here so Phase A service deploys can grant it
        # run.invoker on a fresh org.
        inv = ident["invoker_sa"]
        _run(["iam", "service-accounts", "create", inv.split("@")[0],
              f"--project={m['psc']['apigee']['org']}",
              "--display-name=E-commerce Cloud Run invoker (Apigee proxies)"])
    # AR repo for --source deploys (idempempotent-ish: create ignores exists error)
    _gcloud(["artifacts", "repositories", "create", "cloud-run-source-deploy",
             f"--project={project}", f"--location={region}",
             "--repository-format=docker", "--quiet"])

    db = m["database"]
    if needs("sqlInstance", db["instance"]):
        print(f"  creating Cloud SQL instance {db['instance']} (10-15 min) ...", flush=True)
        _run(["sql", "instances", "create", db["instance"], f"--project={project}",
              f"--database-version={db['version']}", f"--region={region}",
              f"--tier={db['tier']}", f"--edition={db.get('edition', 'enterprise')}",
              "--enable-private-service-connect",
              f"--allowed-psc-projects={project}",
              "--no-assign-ip",
              "--database-flags=cloudsql_iam_authentication=on"], timeout=1800)
    if needs("address", db["psc_endpoint"]["name"]):
        _run(["compute", "addresses", "create", db["psc_endpoint"]["name"],
              f"--project={project}", f"--region={region}",
              f"--subnet={net['subnets'][0]['name']}",
              f"--addresses={db['psc_endpoint']['address']}"])
        inst = _gcloud_json(["sql", "instances", "describe", db["instance"],
                             f"--project={project}"])
        link = (inst or {}).get("pscServiceAttachmentLink")
        if link:
            _run(["compute", "forwarding-rules", "create", db["psc_endpoint"]["name"],
                  f"--project={project}", f"--region={region}",
                  f"--network={net['vpc']}",
                  f"--address={db['psc_endpoint']['name']}",
                  f"--target-service-attachment={link}"])
        else:
            print("    ! instance has no pscServiceAttachmentLink yet — re-run apply")
    if needs("sqlDatabase"):
        _run(["sql", "databases", "create", db["name"], f"--instance={db['instance']}",
              f"--project={project}"])
    if needs("sqlIamUser"):
        _run(["sql", "users", "create", sa_email(ident["runtime_sa"], project),
              f"--instance={db['instance']}", f"--project={project}",
              "--type=cloud_iam_service_account"])

    lb = m["lb"]
    services = _service_specs(m)
    for svc, spec in lb["backends"].items():
        if needs("neg", spec["neg"]):
            _run(["compute", "network-endpoint-groups", "create", spec["neg"],
                  f"--project={project}", f"--region={region}",
                  "--network-endpoint-type=serverless",
                  f"--cloud-run-service={services[svc]['run_service']}"])
        if needs("backendService", spec["backend"]):
            _run(["compute", "backend-services", "create", spec["backend"],
                  f"--project={project}", f"--region={region}",
                  "--load-balancing-scheme=INTERNAL_MANAGED", "--protocol=HTTP"])
            _run(["compute", "backend-services", "add-backend", spec["backend"],
                  f"--project={project}", f"--region={region}",
                  f"--network-endpoint-group={spec['neg']}",
                  f"--network-endpoint-group-region={region}"])
    if needs("urlMap"):
        default_b = lb["backends"][lb["default_backend"]]["backend"]
        _run(["compute", "url-maps", "create", lb["name"], f"--project={project}",
              f"--region={region}", f"--default-service={default_b}"])
        path_bits = []
        for path, svc in lb["path_rules"].items():
            base = path[:-2] if path.endswith("/*") else path
            path_bits.append(f"{base}={lb['backends'][svc]['backend']}")
            path_bits.append(f"{base}/*={lb['backends'][svc]['backend']}")
        _run(["compute", "url-maps", "add-path-matcher", lb["name"],
              f"--project={project}", f"--region={region}",
              "--path-matcher-name=ecommerce-matcher",
              f"--default-service={default_b}",
              f"--path-rules={','.join(path_bits)}"])
        _run(["compute", "target-http-proxies", "create", lb["name"],
              f"--project={project}", f"--region={region}", f"--url-map={lb['name']}",
              f"--url-map-region={region}"])
    if needs("address", lb["address"]["name"]):
        _run(["compute", "addresses", "create", lb["address"]["name"],
              f"--project={project}", f"--region={region}",
              f"--subnet={net['subnets'][0]['name']}",
              f"--addresses={lb['address']['ip']}"])
    if needs("forwardingRule"):
        args = ["compute", "forwarding-rules", "create", lb["name"],
                f"--project={project}", f"--region={region}",
                "--load-balancing-scheme=INTERNAL_MANAGED",
                f"--network={net['vpc']}", f"--subnet={net['subnets'][0]['name']}",
                f"--address={lb['address']['name']}", f"--ports={lb['port']}",
                f"--target-http-proxy={lb['name']}",
                f"--target-http-proxy-region={region}"]
        if lb.get("global_access"):
            args.append("--allow-global-access")
        _run(args)

    psc = m["psc"]
    if needs("serviceAttachment"):
        _run(["compute", "service-attachments", "create",
              psc["service_attachment"]["name"],
              f"--project={project}", f"--region={region}",
              f"--producer-forwarding-rule={lb['name']}",
              f"--connection-preference={psc['service_attachment']['connection_preference']}",
              f"--nat-subnets={psc['service_attachment']['nat_subnet']}"])
    if needs("endpointAttachment"):
        org = psc["apigee"]["org"]
        ea_id = psc["apigee"]["endpoint_attachment"]
        sa_res = (f"projects/{project}/regions/{region}/serviceAttachments/"
                  f"{psc['service_attachment']['name']}")
        print(f"  creating Apigee endpoint attachment {ea_id} (LRO, ~2 min) ...", flush=True)
        try:
            _write_api("POST",
                       f"{APIGEE}/organizations/{org}/endpointAttachments"
                       f"?endpointAttachmentId={ea_id}", token,
                       {"location": region, "serviceAttachment": sa_res})
        except RuntimeError as e:
            print(f"    ! {e}")
    if needs("endpointAttachment") or needs("serviceAttachment"):
        _accept_pending_consumers(m)
        org = psc["apigee"]["org"]
        ea_id = psc["apigee"]["endpoint_attachment"]
        for _ in range(30):
            ea = _get(f"{APIGEE}/organizations/{org}/endpointAttachments/{ea_id}", token)
            state = (ea or {}).get("connectionState") or (ea or {}).get("state")
            if ea and ea.get("host") and state in ("ACCEPTED", "ACTIVE"):
                print(f"  endpoint attachment host: {ea['host']} state={state} "
                      f"(consumed by ts-ecommerce-services at apigee provision time)")
                break
            _accept_pending_consumers(m)   # tenant shows up PENDING after the EA lands
            time.sleep(10)


def _accept_pending_consumers(m):
    """ACCEPT_MANUAL two-phase: the Apigee endpoint attachment's consumer is a
    Google-managed TENANT project unknowable in advance. Read it from the
    service attachment's connectedEndpoints and put it on the accept list."""
    psc = m["psc"]
    if psc["service_attachment"]["connection_preference"] != "ACCEPT_MANUAL":
        return
    project, region = m["project"], m["region"]
    name = psc["service_attachment"]["name"]
    sa = _gcloud_json(["compute", "service-attachments", "describe", name,
                       f"--project={project}", f"--region={region}"])
    if not sa:
        return
    accepted = {a.get("projectIdOrNum") for a in sa.get("consumerAcceptLists") or []}
    consumers = {consumer_project_from_endpoint(c.get("endpoint", ""))
                 for c in sa.get("connectedEndpoints") or []}
    consumers.discard(None)
    missing = consumers - accepted
    if not missing:
        return
    full = sorted(accepted | consumers)
    print(f"    accepting PSC consumer(s) (Apigee tenant project): {sorted(missing)}",
          flush=True)
    _run(["compute", "service-attachments", "update", name,
          f"--project={project}", f"--region={region}",
          f"--consumer-accept-list={','.join(f'{p}=10' for p in full)}"])


def _deployer():
    ok, r = _gcloud(["config", "get-value", "account"])
    return r.stdout.strip() if ok and r.stdout.strip() else None


def _service_specs(m) -> dict:
    """{svc: {run_service, dir, audience}} — the Cloud Run service name is the
    manifest key (customers/orders/products), matching the legacy deploys."""
    out = {}
    for svc, spec in m["services"].items():
        if svc == "env":
            continue
        out[svc] = {"run_service": svc, "dir": spec["dir"], "audience": spec["audience"]}
    return out


# ── seed (destructive reset) ─────────────────────────────────────────────────
def cmd_seed(args, m):
    project, region = m["project"], m["region"]
    db = m["database"]
    bucket = f"{project}{db['seed']['bucket_suffix']}"
    schema_src = SERVICES_DIR / db["seed"]["schema"]
    data_src = SERVICES_DIR / db["seed"]["data"]
    missing = [p for p in (schema_src, data_src) if not p.exists()]
    if missing:
        print(f"seed file(s) not found: {missing}")
        return 1
    user = iam_db_user(project, m["identity"]["runtime_sa"])
    grants = (f"GRANT SELECT, INSERT, UPDATE, DELETE ON `{db['name']}`.* "
              f"TO '{user}'@'%';\nFLUSH PRIVILEGES;\n")
    print(f"# seed — DESTRUCTIVE RESET of `{db['name']}` on {db['instance']}\n"
          f"  (schema.sql drops + recreates all tables, sample-data.sql "
          f"repopulates, then grants to {user})")
    if not getattr(args, "apply", False):
        if not sys.stdin.isatty():
            print("(no TTY — pass --apply)")
            return 1
        if input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
            return 1
    _gcloud(["storage", "buckets", "create", f"gs://{bucket}",
             f"--project={project}", f"--location={region}"])
    inst = _gcloud_json(["sql", "instances", "describe", db["instance"],
                         f"--project={project}"])
    sql_sa = (inst or {}).get("serviceAccountEmailAddress")
    if not sql_sa:
        print("cannot determine the instance service account — is the instance up?")
        return 1
    _run(["storage", "buckets", "add-iam-policy-binding", f"gs://{bucket}",
          f"--member=serviceAccount:{sql_sa}", "--role=roles/storage.objectViewer",
          "--quiet"])
    grants_path = HERE / "grants.sql"
    grants_path.write_text(grants)
    print("  staging schema + data + grants ...", flush=True)
    _run(["storage", "cp", str(schema_src), f"gs://{bucket}/seed/schema.sql"])
    _run(["storage", "cp", str(data_src), f"gs://{bucket}/seed/sample-data.sql"])
    _run(["storage", "cp", str(grants_path), f"gs://{bucket}/seed/grants.sql"])
    grants_path.unlink()
    for label, obj in (("schema", "schema.sql"), ("sample data", "sample-data.sql"),
                       ("grants", "grants.sql")):
        print(f"  importing {label} ...", flush=True)
        if not _run(["sql", "import", "sql", db["instance"],
                     f"gs://{bucket}/seed/{obj}",
                     f"--project={project}", "--quiet"], timeout=900):
            return 1
    print("done. Tables created, sample data loaded, IAM DB user granted.")
    return 0


# ── main ──────────────────────────────────────────────────────────────────────
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="E-commerce services project provisioning")
    ap.add_argument("mode", nargs="?", choices=["seed"], default=None,
                    help="seed: import sample data + grants (DESTRUCTIVE RESET)")
    ap.add_argument("--manifest", default=str(MANIFEST))
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true", help="read-only report (exit 1 on drift)")
    g.add_argument("--dry-run", action="store_true", help="report only; write nothing")
    g.add_argument("--apply", action="store_true", help="write without prompting")
    args = ap.parse_args(argv)

    m = load_manifest(args.manifest)
    err = validate_project(m.get("project", ""), m["identity"]["runtime_sa"])
    if err:
        print(f"❌ {err}")
        return 1
    if args.mode == "seed":
        return cmd_seed(args, m)

    project, region = m["project"], m["region"]
    token = _token()
    print(f"# services project — {project}, region {region}\n")

    findings = _findings(m, token)
    n_bad = report(findings)
    if args.check:
        return 1 if n_bad else 0
    if not should_write(args, n_bad):
        if n_bad:
            print(f"nothing written — {n_bad} change(s) pending.")
        return 1 if n_bad else 0
    # CONVERGE in one invocation: real-world gating (API enablement unlocks
    # the listings; the blocking SQL create unlocks the PSC link) means one
    # apply pass can reveal the next — loop until green or no progress.
    for iteration in range(1, 4):
        _apply(m, findings, token)
        print(f"\n── reconcile pass {iteration + 1}: re-checking ──", flush=True)
        prev = {(f["kind"], f["name"], f["status"]) for f in findings if f["status"] != OK}
        findings = _findings(m, token)
        n_bad = report(findings)
        if n_bad == 0:
            print("\nconverged ✓ — next: `seed`, then deploy_services.py, "
                  "then `provision.py apigee`.")
            return 0
        now = {(f["kind"], f["name"], f["status"]) for f in findings if f["status"] != OK}
        if now == prev:
            print("\nno further progress this run — the remaining rows need "
                  "time (LROs/propagation) or a fix; re-run when ready.")
            return 1
    print("\nstopping after 3 passes — re-run to continue converging.")
    return 1


def _findings(m, token):
    project, region = m["project"], m["region"]
    enabled, api_err = fetch_enabled_apis(project)
    vpcs, subnets = fetch_network(project, region)
    pn = fetch_project_number(project)
    sas = fetch_sas(project) or set()
    role_map = fetch_role_map(project)
    email = sa_email(m["identity"]["runtime_sa"], project)
    actas = fetch_actas(project, email) if email in sas else set()
    inst, dbs, users = fetch_sql(project, m["database"])
    addrs = fetch_addresses(project, region)
    lb_live = fetch_lb(project, region, m["lb"]) if addrs is not None else None
    sa_live, ea_live = fetch_psc(project, region, m["psc"], token)
    apigee_project = m["psc"]["apigee"]["org"]
    return (
        compare_services_enabled(m["apis"], enabled, api_err)
        + compare_network(m["network"], vpcs, subnets)
        + compare_identity(m["identity"], project, sas, role_map, pn, actas, _deployer())
        + compare_invoker_sa(m["identity"]["invoker_sa"], apigee_project,
                             fetch_sas(apigee_project))
        + compare_sql(m["database"], project, inst, dbs, users)
        + compare_addresses(
            [(m["database"]["psc_endpoint"]["name"], m["database"]["psc_endpoint"]["address"]),
             (m["lb"]["address"]["name"], m["lb"]["address"]["ip"])], addrs)
        + compare_lb(m["lb"], _service_specs(m), lb_live)
        + compare_psc(m["psc"], sa_live, ea_live)
    )


if __name__ == "__main__":
    raise SystemExit(main())
