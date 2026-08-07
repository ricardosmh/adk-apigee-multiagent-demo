#!/usr/bin/env python3
"""Agent identity + infra provisioning — manifest-driven, AGENT side.

Reconciles everything the agent deploys need on the AI project, declared in
runtime-manifest.yaml:
  * required Google APIs enabled (infra.services)
  * the staging bucket exists + the GEAP CONTROL-PLANE service agent can read
    the deploy pickle from it (storage.objectAdmin)
  * one runtime SA per agent + its project roles
  * per-SA staging-bucket access (telemetry/boot) + the deployer's actAs
  * the per-user ACL store (manifest acl:): Firestore database exists + the
    seed roles/users (acl_roles/*, acl_users/*). Seed-only: apply never
    deletes and MERGES user roles, so admin-UI edits survive re-runs.

    python provision_agents.py             # report + confirm before writing
    python provision_agents.py --check     # read-only report, exit 1 on drift
    python provision_agents.py --dry-run   # report only, write nothing
    python provision_agents.py --apply     # write without prompting

Run order: THIS (--apply) → apigee/provision/provision.py secrets (grants
accessors to SAs created here) → deploy_agents.py --all.

Needs: gcloud (authed), $STAGING_BUCKET for the bucket pieces (else skipped
with a note). Pure comparators are unit-tested in tests/.
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

OK, DRIFT, MISSING = "OK", "DRIFT", "MISSING"
_SYMBOL = {OK: "✅", DRIFT: "⚠", MISSING: "❌"}

try:
    import certifi

    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:  # certifi absent — platform default
    _SSL_CTX = ssl.create_default_context()


def _f(status, kind, name, detail=""):
    return {"status": status, "kind": kind, "name": name, "detail": detail}


def sa_email(name: str, project: str) -> str:
    return f"{name}@{project}.iam.gserviceaccount.com"


# ── Pure comparators ──────────────────────────────────────────────────────────
def compare_services(required: list[str], enabled: set | None,
                     list_error: str = "") -> list:
    """Required Google APIs (manifest infra.services) vs enabled services.

    ``enabled=None`` means the listing itself failed — report that honestly as
    ONE finding instead of falsely claiming every service is missing (seen
    live: a failed `gcloud services list` made 7 enabled services report
    MISSING, survive an apply, and report MISSING again)."""
    if enabled is None:
        return [_f(DRIFT, "services", "listing",
                   f"could not list enabled services — per-service state UNKNOWN"
                   f" ({list_error or 'gcloud services list failed'})")]
    return [_f(OK if s in enabled else MISSING, "service", s,
               "enabled ✓" if s in enabled else "not enabled")
            for s in required]


def compare_bucket(bucket: str | None, exists: bool, geap_agent: str | None,
                   bucket_members: set | None) -> list:
    """Staging bucket exists + the GEAP control-plane service agent can read
    the deploy pickle from it. Skipped (empty) when STAGING_BUCKET is unset."""
    if not bucket:
        return []
    if not exists:
        return [_f(MISSING, "bucket", bucket, "does not exist")]
    out = [_f(OK, "bucket", bucket, "exists ✓")]
    if geap_agent is not None:
        has = f"serviceAccount:{geap_agent}" in (bucket_members or set())
        out.append(_f(OK if has else DRIFT, "geapAgentAccess", geap_agent,
                      "objectAdmin ✓" if has else "no objectAdmin on the staging bucket"
                      " (CreateReasoningEngine can't read the deploy pickle)"))
    return out


def compare_agent_sas(agents: dict, existing: set, role_map: dict, project: str):
    """agents = manifest mapping; existing = SA emails that exist; role_map =
    {email: set(roles)} from the project IAM policy."""
    out = []
    for agent, spec in agents.items():
        email = sa_email(spec["sa"], project)
        if email not in existing:
            out.append(_f(MISSING, "serviceAccount", spec["sa"]))
            continue
        missing = [r for r in spec.get("roles", []) if r not in role_map.get(email, set())]
        out.append(_f(DRIFT if missing else OK, "serviceAccount", spec["sa"],
                      "missing roles: " + ", ".join(missing) if missing else ""))
    return out


def compare_grants(agents: dict, project: str, bucket_members: set | None,
                   actas_map: dict, deployer: str | None):
    """bucket_members = members holding the staging role on the bucket (None =
    bucket unknown/unset); actas_map = {sa_email: set(members with actAs)}."""
    out = []
    for agent, spec in agents.items():
        email = sa_email(spec["sa"], project)
        if bucket_members is not None:
            has = f"serviceAccount:{email}" in bucket_members
            out.append(_f(OK if has else DRIFT, "bucketAccess", spec["sa"],
                          "" if has else "no staging-bucket role"))
        if deployer:
            # case-insensitive: IAM returns the member with the account's
            # canonical casing, which can differ from gcloud's lowercase.
            has = f"user:{deployer}".lower() in {
                m.lower() for m in actas_map.get(email, set())}
            out.append(_f(OK if has else DRIFT, "deployerActAs", spec["sa"],
                          "" if has else f"{deployer} cannot actAs"))
    return out


# ── AI-project network comparators (pure) ─────────────────────────────────────
def compare_ai_network(net: dict | None, live: dict | None) -> list:
    """net = manifest infra.network; live = {"vpc_routing": "GLOBAL"|...|None,
    "subnets": {name: {"range","region"}}, "attachment": bool|None,
    "endpoint_ip": str|None, "dns_record_ip": str|None, "ar_repo": bool|None}.
    live=None => listing failed (honest UNKNOWN)."""
    if not net:
        return []
    if live is None:
        return [_f(DRIFT, "aiNetwork", "listing",
                   "could not read network state — state UNKNOWN")]
    out = []
    routing = live.get("vpc_routing")
    if routing is None:
        out.append(_f(MISSING, "vpc", net["vpc"], "does not exist"))
    else:
        out.append(_f(OK if routing == "GLOBAL" else DRIFT, "vpc", net["vpc"],
                      "global routing ✓" if routing == "GLOBAL"
                      else f"routing={routing} want GLOBAL (cross-region PSC path)"))
    for sub in net.get("subnets", []):
        got = (live.get("subnets") or {}).get(sub["name"])
        if got is None:
            out.append(_f(MISSING, "subnet", sub["name"],
                          f"want {sub['range']} in {sub['region']}"))
        else:
            diffs = []
            if got.get("range") != sub["range"]:
                diffs.append(f"range={got.get('range')} want {sub['range']}")
            if got.get("region") and got["region"] != sub["region"]:
                diffs.append(f"region={got.get('region')} want {sub['region']}")
            out.append(_f(DRIFT if diffs else OK, "subnet", sub["name"], "; ".join(diffs)))
    att = net.get("network_attachment", {})
    if att:
        has = live.get("attachment")
        out.append(_f(OK if has else MISSING, "networkAttachment", att["name"],
                      "exists ✓ (engines' PSC interface target)" if has
                      else "not created — engine deploys fall back to public egress"))
    psc = net.get("apigee_psc", {})
    if psc:
        ep = psc["endpoint"]
        got_ip = live.get("endpoint_ip")
        status = live.get("endpoint_status")
        if got_ip is None:
            out.append(_f(MISSING, "apigeePscEndpoint", ep["name"],
                          f"want {ep['address']} -> Apigee instance"))
        elif got_ip != ep["address"]:
            out.append(_f(DRIFT, "apigeePscEndpoint", ep["name"],
                          f"{got_ip} want {ep['address']}"))
        elif status and status != "ACCEPTED":
            # The Apigee INSTANCE gates PSC consumers: until this project is
            # in its consumerAcceptList the connection sits PENDING and every
            # BFF/engine call to internal.apigee.com fails at TCP connect.
            out.append(_f(DRIFT, "apigeePscEndpoint", ep["name"],
                          f"connection {status} — apply adds this project to the "
                          f"Apigee instance's consumerAcceptList"))
        else:
            out.append(_f(OK, "apigeePscEndpoint", ep["name"],
                          f"{got_ip} ✓" + (f" ({status})" if status else "")))
        rec_ip = live.get("dns_record_ip")
        want_rec = psc["dns"]["record"]
        if rec_ip is None:
            out.append(_f(MISSING, "apigeeDns", want_rec.rstrip("."),
                          f"no private record -> {ep['address']}"))
        else:
            out.append(_f(OK if rec_ip == ep["address"] else DRIFT,
                          "apigeeDns", want_rec.rstrip("."),
                          f"-> {rec_ip}" + ("" if rec_ip == ep["address"]
                                            else f" want {ep['address']}")))
    repo = net.get("artifact_repo", {})
    if repo:
        has = live.get("ar_repo")
        out.append(_f(OK if has else MISSING, "artifactRepo", repo["name"],
                      "exists ✓" if has else f"not created in {repo['region']}"))
    return out


# ── Firestore value codec (pure) ──────────────────────────────────────────────
def fs_encode(value):
    """Python → Firestore REST typed value (strings, bools, string lists)."""
    if isinstance(value, bool):
        return {"booleanValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, list):
        return {"arrayValue": {"values": [fs_encode(v) for v in value]}}
    raise TypeError(f"unsupported ACL field type: {type(value).__name__}")


def fs_decode(field):
    if "booleanValue" in field:
        return field["booleanValue"]
    if "stringValue" in field:
        return field["stringValue"]
    if "arrayValue" in field:
        return [fs_decode(v) for v in field["arrayValue"].get("values", [])]
    return None


def fs_doc_to_dict(doc: dict | None) -> dict | None:
    if doc is None:
        return None
    return {k: fs_decode(v) for k, v in (doc.get("fields") or {}).items()}


def compare_acl(desired_roles: dict, desired_users: dict,
                live_roles: dict, live_users: dict, db_exists: bool,
                database: str) -> list:
    """ACL seed vs live Firestore. Roles are managed exactly (the admin role
    must stay wildcard); users are SUBSET checks — the seed roles must be
    present, extra roles granted via the admin UI are fine (and apply merges,
    never clobbers)."""
    if not db_exists:
        return ([_f(MISSING, "firestore", database, "database does not exist")]
                + [_f(MISSING, "aclRole", n) for n in desired_roles]
                + [_f(MISSING, "aclUser", e.strip().lower()) for e in desired_users])
    out = [_f(OK, "firestore", database, "database exists ✓")]
    for name, spec in desired_roles.items():
        live = live_roles.get(name)
        if live is None:
            out.append(_f(MISSING, "aclRole", name))
            continue
        diffs = []
        if sorted(live.get("agents") or []) != sorted(spec.get("agents") or []):
            diffs.append(f"agents={sorted(live.get('agents') or [])}"
                         f" want {sorted(spec.get('agents') or [])}")
        if bool(live.get("is_admin")) != bool(spec.get("is_admin")):
            diffs.append(f"is_admin={live.get('is_admin')} want {spec.get('is_admin', False)}")
        ok_note = f"agents {sorted(spec.get('agents') or [])}" + \
                  ("; is_admin ✓" if spec.get("is_admin") else "")
        out.append(_f(DRIFT if diffs else OK, "aclRole", name,
                      "; ".join(diffs) if diffs else ok_note))
    for email, roles in desired_users.items():
        email_n = email.strip().lower()
        live = live_users.get(email_n)
        if live is None:
            out.append(_f(MISSING, "aclUser", email_n))
            continue
        live_r = live.get("roles") or []
        missing = [r for r in (roles or []) if r not in live_r]
        out.append(_f(DRIFT if missing else OK, "aclUser", email_n,
                      f"missing seed role(s): {missing}" if missing
                      else f"roles {sorted(live_r)} ⊇ seed"))
    return out


# ── gcloud layer ──────────────────────────────────────────────────────────────
def _gcloud(args: list[str]):
    r = subprocess.run(["gcloud", *args], capture_output=True, text=True,
                       stdin=subprocess.DEVNULL,
                       env={**os.environ, "CLOUDSDK_CORE_DISABLE_PROMPTS": "1"})
    return r.returncode == 0, r


def _grant(label: str, args: list[str], attempts: int = 3) -> bool:
    """IAM bindings on JUST-created identities lose a propagation race on
    fresh projects ('member does not exist' seconds after create), and
    gcloud's read-modify-write can abort on concurrent policy writes — retry
    briefly and NEVER fail silently. Seen live: the geap agent's dns.peer /
    networkAdmin and the first agent SA's roles all vanished into a second
    apply pass with no output."""
    ok, r = False, None
    for attempt in range(attempts):
        ok, r = _gcloud(args)
        if ok:
            return True
        if attempt < attempts - 1:
            time.sleep(10)
    tail = (r.stderr or "").strip().splitlines() if r else []
    print(f"    ! {label} failed: {tail[-1] if tail else '?'}", flush=True)
    return False


def _gcloud_json(args: list[str]):
    ok, r = _gcloud([*args, "--format=json"])
    return json.loads(r.stdout) if ok and r.stdout.strip() else None


def _sa_exists(email: str) -> bool:
    return _gcloud(["iam", "service-accounts", "describe", email])[0]


def _project_role_map(project: str) -> dict:
    policy = _gcloud_json(["projects", "get-iam-policy", project]) or {}
    role_map: dict[str, set] = {}
    for b in policy.get("bindings", []):
        for mem in b.get("members", []):
            if mem.startswith("serviceAccount:"):
                role_map.setdefault(mem.split(":", 1)[1], set()).add(b["role"])
    return role_map


def _bucket_members(bucket: str, role: str) -> set | None:
    policy = _gcloud_json(["storage", "buckets", "get-iam-policy", f"gs://{bucket}"])
    if policy is None:
        return None
    return {m for b in policy.get("bindings", []) if b["role"] == role for m in b.get("members", [])}


def _actas_members(email: str, role: str) -> set:
    policy = _gcloud_json(["iam", "service-accounts", "get-iam-policy", email]) or {}
    return {m for b in policy.get("bindings", []) if b.get("role") == role for m in b.get("members", [])}


def _deployer() -> str | None:
    ok, r = _gcloud(["config", "get-value", "account"])
    acct = r.stdout.strip() if ok else ""
    return acct or None


def _enabled_services(project: str) -> tuple[set | None, str]:
    """(enabled service names, error). Uses value(config.name) — one name per
    line. NO --page-size: the serviceusage API caps it at 200 and rejects more
    with SU_INVALID_PAGE_SIZE (the original root cause of the false-MISSING
    rows); gcloud auto-paginates without the flag. None = listing failed
    (caller reports UNKNOWN, never a false MISSING)."""
    ok, r = _gcloud(["services", "list", "--enabled", f"--project={project}",
                     "--format=value(config.name)"])
    if not ok:
        return None, r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "exit != 0"
    return {ln.strip() for ln in r.stdout.splitlines() if ln.strip()}, ""


def _bucket_exists(bucket: str) -> bool:
    return _gcloud(["storage", "buckets", "describe", f"gs://{bucket}"])[0]


def _geap_service_agent(project: str) -> str | None:
    proj = _gcloud_json(["projects", "describe", project])
    if not proj:
        return None
    return f"service-{proj['projectNumber']}@gcp-sa-aiplatform.iam.gserviceaccount.com"


# ── Firestore REST layer (documents; the db itself is gcloud) ────────────────
def _access_token() -> str:
    return subprocess.run(["gcloud", "auth", "print-access-token"],
                          capture_output=True, text=True, check=True).stdout.strip()


def _fs_url(project: str, database: str, path: str) -> str:
    return (f"https://firestore.googleapis.com/v1/projects/{project}"
            f"/databases/{database}/documents/{path}")


def _fs_request(method: str, url: str, token: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30, context=_SSL_CTX) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def _fs_get(project, database, path, token) -> dict | None:
    return fs_doc_to_dict(_fs_request("GET", _fs_url(project, database, path), token))


def _fs_patch(project, database, path, fields: dict, token):
    """PATCH only the named fields (updateMask) — other live fields survive."""
    mask = "&".join(f"updateMask.fieldPaths={k}" for k in fields)
    url = f"{_fs_url(project, database, path)}?{mask}"
    return _fs_request("PATCH", url, token,
                       {"fields": {k: fs_encode(v) for k, v in fields.items()}})


def _db_exists(project: str, database: str) -> bool:
    return _gcloud(["firestore", "databases", "describe",
                    f"--database={database}", f"--project={project}"])[0]


def _fetch_acl_live(acl: dict, project: str, token: str):
    database = acl.get("database", "(default)")
    live_roles = {n: _fs_get(project, database, f"acl_roles/{n}", token) or None
                  for n in (acl.get("roles") or {})}
    live_roles = {n: d for n, d in live_roles.items() if d is not None}
    live_users = {}
    for email in (acl.get("users") or {}):
        e = email.strip().lower()
        doc = _fs_get(project, database, f"acl_users/{e}", token)
        if doc is not None:
            live_users[e] = doc
    return live_roles, live_users


def _apply_acl(acl: dict, project: str, token: str):
    database = acl.get("database", "(default)")
    if not _db_exists(project, database):
        print(f"  create Firestore database {database} ({acl.get('location')})", flush=True)
        cmd = ["firestore", "databases", "create",
               f"--location={acl.get('location', 'us-central1')}", f"--project={project}"]
        if database != "(default)":
            cmd.append(f"--database={database}")
        ok, r = _gcloud(cmd)
        if not ok:
            print(f"  ✗ database create failed: {r.stderr.strip()[:300]}", file=sys.stderr)
            return
    for name, spec in (acl.get("roles") or {}).items():
        live = _fs_get(project, database, f"acl_roles/{name}", token)
        want = {"agents": list(spec.get("agents") or []),
                "is_admin": bool(spec.get("is_admin", False))}
        if live is None or sorted(live.get("agents") or []) != sorted(want["agents"]) \
                or bool(live.get("is_admin")) != want["is_admin"]:
            print(f"  write acl_roles/{name}", flush=True)
            _fs_patch(project, database, f"acl_roles/{name}", want, token)
    for email, roles in (acl.get("users") or {}).items():
        e = email.strip().lower()
        live = _fs_get(project, database, f"acl_users/{e}", token)
        merged = sorted(set((live or {}).get("roles") or []) | set(roles or []))
        if live is None or merged != sorted((live or {}).get("roles") or []):
            print(f"  write acl_users/{e} (roles={merged})", flush=True)
            _fs_patch(project, database, f"acl_users/{e}", {"roles": merged}, token)


def _gcloud_json(args: list[str]):
    ok, r = _gcloud([*args, "--format=json"])
    if not ok:
        return None
    try:
        return json.loads(r.stdout or "null")
    except json.JSONDecodeError:
        return None


def fetch_ai_network(project: str, net: dict | None) -> dict | None:
    """Shape the live network state for compare_ai_network (None = unreadable)."""
    if not net:
        return {}
    vpc = _gcloud_json(["compute", "networks", "describe", net["vpc"],
                        f"--project={project}"])
    subs_live: dict = {}
    regions = {s["region"] for s in net.get("subnets", [])}
    for region in regions:
        subs = _gcloud_json(["compute", "networks", "subnets", "list",
                             f"--project={project}", f"--regions={region}"])
        if subs is None:
            if vpc is None:
                return {"vpc_routing": None, "subnets": {}}  # fresh project
            continue
        for sub in subs:
            subs_live[sub["name"]] = {"range": sub.get("ipCidrRange"),
                                      "region": region}
    att = net.get("network_attachment", {})
    att_live = None
    if att:
        att_live = _gcloud_json(["compute", "network-attachments", "describe",
                                 att["name"], f"--project={project}",
                                 f"--region={att['region']}"]) is not None
    psc = net.get("apigee_psc", {})
    endpoint_ip = dns_ip = None
    if psc:
        fr = _gcloud_json(["compute", "forwarding-rules", "describe",
                           psc["endpoint"]["name"], f"--project={project}",
                           f"--region={psc['endpoint']['region']}"])
        endpoint_ip = (fr or {}).get("IPAddress")
        endpoint_status = (fr or {}).get("pscConnectionStatus")
        recs = _gcloud_json(["dns", "record-sets", "list",
                             f"--zone={psc['dns']['zone']}", f"--project={project}"])
        for rec in recs or []:
            if rec.get("name") == psc["dns"]["record"] and rec.get("type") == "A":
                dns_ip = (rec.get("rrdatas") or [None])[0]
    repo = net.get("artifact_repo", {})
    ar_live = None
    if repo:
        ar_live = _gcloud_json(["artifacts", "repositories", "describe",
                                repo["name"], f"--project={project}",
                                f"--location={repo['region']}"]) is not None
    return {"vpc_routing": (vpc or {}).get("routingConfig", {}).get("routingMode")
            if vpc else None,
            "subnets": subs_live, "attachment": att_live,
            "endpoint_ip": endpoint_ip, "endpoint_status": endpoint_status,
            "dns_record_ip": dns_ip, "ar_repo": ar_live}


def _apigee_instance_attachment(org: str, region: str) -> str | None:
    """The Apigee X instance's NORTHBOUND service attachment (ingress) for the
    gateway region — the target of our internal.apigee.com PSC endpoint."""
    try:
        token = _access_token()
        req = urllib.request.Request(
            f"https://apigee.googleapis.com/v1/organizations/{org}/instances",
            headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=30, context=_SSL_CTX) as r:
            data = json.load(r)
    except Exception:  # noqa: BLE001
        return None
    for inst in data.get("instances", []):
        if inst.get("location") == region and inst.get("serviceAttachment"):
            return inst["serviceAttachment"]
    return None


def _ensure_apigee_accepts(org: str, region: str, project: str) -> None:
    """PATCH the Apigee instance's consumerAcceptList to include `project`
    (idempotent). Without it the PSC endpoint stays PENDING forever."""
    try:
        token = _access_token()
        req = urllib.request.Request(
            f"https://apigee.googleapis.com/v1/organizations/{org}/instances",
            headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=30, context=_SSL_CTX) as r:
            instances = json.load(r).get("instances", [])
        inst = next((i for i in instances if i.get("location") == region), None)
        if not inst:
            print(f"    ! no Apigee instance in {region} — accept list not updated")
            return
        accept = list(inst.get("consumerAcceptList") or [])
        if project in accept:
            return
        accept.append(project)
        body = json.dumps({"consumerAcceptList": accept}).encode()
        # list() returns the SHORT instance name — build the full resource path
        inst_path = inst["name"] if inst["name"].startswith("organizations/") \
            else f"organizations/{org}/instances/{inst['name']}"
        req = urllib.request.Request(
            f"https://apigee.googleapis.com/v1/{inst_path}"
            "?updateMask=consumerAcceptList",
            data=body, method="PATCH",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60, context=_SSL_CTX):
            pass
        print(f"  Apigee instance accept list += {project} (LRO — the PSC "
              "connection flips to ACCEPTED in a few minutes)")
    except urllib.error.HTTPError as e:
        print(f"    ! consumerAcceptList update failed: {e.code} "
              + e.read().decode()[:200])
    except Exception as e:  # noqa: BLE001
        print(f"    ! consumerAcceptList update failed: {e}")


def _apply_ai_network(net: dict | None, project: str) -> None:
    if not net:
        return
    vpc = net["vpc"]
    if _gcloud_json(["compute", "networks", "describe", vpc,
                     f"--project={project}"]) is None:
        print(f"  create VPC {vpc} (global routing) ...", flush=True)
        _gcloud(["compute", "networks", "create", vpc, f"--project={project}",
                 "--subnet-mode=custom", "--bgp-routing-mode=global"])
    for sub in net.get("subnets", []):
        if _gcloud_json(["compute", "networks", "subnets", "describe", sub["name"],
                         f"--project={project}", f"--region={sub['region']}"]) is None:
            _gcloud(["compute", "networks", "subnets", "create", sub["name"],
                     f"--project={project}", f"--region={sub['region']}",
                     f"--network={vpc}", f"--range={sub['range']}"])
    att = net.get("network_attachment", {})
    if att and _gcloud_json(["compute", "network-attachments", "describe",
                             att["name"], f"--project={project}",
                             f"--region={att['region']}"]) is None:
        print(f"  create network attachment {att['name']} ...", flush=True)
        _gcloud(["compute", "network-attachments", "create", att["name"],
                 f"--project={project}", f"--region={att['region']}",
                 "--connection-preference=ACCEPT_AUTOMATIC",
                 f"--subnets={att['subnet']}"])
    psc = net.get("apigee_psc", {})
    if psc:
        ep = psc["endpoint"]
        if _gcloud_json(["compute", "addresses", "describe", ep["name"],
                         f"--project={project}", f"--region={ep['region']}"]) is None:
            _gcloud(["compute", "addresses", "create", ep["name"],
                     f"--project={project}", f"--region={ep['region']}",
                     f"--subnet={ep['subnet']}", f"--addresses={ep['address']}"])
        if _gcloud_json(["compute", "forwarding-rules", "describe", ep["name"],
                         f"--project={project}", f"--region={ep['region']}"]) is None:
            target = _apigee_instance_attachment(psc["org"], ep["region"])
            if target:
                print(f"  create PSC endpoint {ep['name']} -> {target.rsplit('/', 1)[-1]} ...",
                      flush=True)
                _gcloud(["compute", "forwarding-rules", "create", ep["name"],
                         f"--project={project}", f"--region={ep['region']}",
                         f"--network={vpc}", f"--address={ep['name']}",
                         f"--target-service-attachment={target}",
                         "--allow-psc-global-access"])
            else:
                print(f"    ! no Apigee instance service attachment found in "
                      f"{ep['region']} for org {psc['org']} — endpoint skipped")
        # Whether the rule is new or old: the connection only ACCEPTs once
        # this project is in the instance's consumer accept list.
        _ensure_apigee_accepts(psc["org"], ep["region"], project)
        dns = psc["dns"]
        if _gcloud_json(["dns", "managed-zones", "describe", dns["zone"],
                         f"--project={project}"]) is None:
            print(f"  create private DNS zone {dns['zone']} ({dns['domain']}) ...",
                  flush=True)
            _gcloud(["dns", "managed-zones", "create", dns["zone"],
                     f"--project={project}", f"--dns-name={dns['domain']}",
                     f"--description=internal Apigee host", "--visibility=private",
                     f"--networks={vpc}"])
        recs = _gcloud_json(["dns", "record-sets", "list",
                             f"--zone={dns['zone']}", f"--project={project}"]) or []
        if not any(r.get("name") == dns["record"] and r.get("type") == "A" for r in recs):
            _gcloud(["dns", "record-sets", "create", dns["record"],
                     f"--zone={dns['zone']}", f"--project={project}",
                     "--type=A", "--ttl=300", f"--rrdatas={ep['address']}"])
    repo = net.get("artifact_repo", {})
    if repo:
        _gcloud(["artifacts", "repositories", "create", repo["name"],
                 f"--project={project}", f"--location={repo['region']}",
                 "--repository-format=docker", "--quiet"])


def _apply_build_sa(infra, project):
    role = infra.get("build_sa_role")
    pn = _project_number(project) if role else None
    if role and pn:
        _gcloud(["projects", "add-iam-policy-binding", project,
                 "--member",
                 f"serviceAccount:{pn}-compute@developer.gserviceaccount.com",
                 "--role", role, "--quiet"])


_pn_cache: dict = {}


def _project_number(project: str) -> str | None:
    if project not in _pn_cache:
        ok, r = _gcloud(["projects", "describe", project,
                         "--format=value(projectNumber)"])
        _pn_cache[project] = r.stdout.strip() if ok and r.stdout.strip() else None
    return _pn_cache[project]


def _apply_infra(infra, project, region, bucket, geap_agent, enabled=None):
    services = infra.get("services", [])
    # Only enable what's actually MISSING. `enabled=None` means the live listing
    # failed (compare reported UNKNOWN) → fall back to enabling the whole list,
    # since we can't tell which are on. Enabling is idempotent, but the batch
    # `services enable` call is slow, so skipping already-on services saves that
    # round-trip on every apply (e.g. an apply that only adds a role).
    todo = services if enabled is None else [s for s in services if s not in enabled]
    if todo:
        print(f"  enabling {len(todo)} service(s) ...", flush=True)
        _gcloud(["services", "enable", *todo, f"--project={project}", "--quiet"])
    elif services:
        print(f"  services: all {len(services)} already enabled ✓", flush=True)
    if bucket:
        if not _bucket_exists(bucket):
            print(f"  create bucket gs://{bucket}", flush=True)
            _gcloud(["storage", "buckets", "create", f"gs://{bucket}",
                     f"--project={project}", f"--location={region}",
                     "--uniform-bucket-level-access", "--quiet"])
        if geap_agent:
            # Fresh project: the GEAP SERVICE AGENT doesn't exist until first
            # use — create it explicitly or the grant below fails (silently,
            # seen live: the geapAgentAccess row survived two applies).
            _gcloud(["beta", "services", "identity", "create",
                     "--service=aiplatform.googleapis.com", f"--project={project}"])
            # identity create returns BEFORE the agent is visible to IAM on a
            # brand-new project — every grant below goes through _grant's
            # retry instead of losing that race.
            _grant("geap agent bucket grant",
                   ["storage", "buckets", "add-iam-policy-binding", f"gs://{bucket}",
                    "--member", f"serviceAccount:{geap_agent}",
                    "--role", infra["staging_bucket_role"], "--quiet"])
            # Engines' DNS PEERING (PSC-I DnsPeeringConfig) runs as the GEAP
            # service agent — it needs dns.peer on the project hosting the zone.
            if infra.get("network", {}).get("apigee_psc"):
                _grant("geap agent dns.peer",
                       ["projects", "add-iam-policy-binding", project,
                        "--member", f"serviceAccount:{geap_agent}",
                        "--role", "roles/dns.peer", "--quiet"])
            # PSC-I itself: the service agent READS AND UPDATES the network
            # attachment (registers its connection) — networkUser alone 403s
            # on compute.networkAttachments.update; networkAdmin is required
            # (docs/PRIVATE_APIGEE.md; seen live: all 4 engine deploys 403'd
            # on networkAttachments.get in the fresh project).
            if infra.get("network", {}).get("network_attachment"):
                _grant("geap agent networkAdmin",
                       ["projects", "add-iam-policy-binding", project,
                        "--member", f"serviceAccount:{geap_agent}",
                        "--role", "roles/compute.networkAdmin", "--quiet"])


def _apply(agents, infra, project, bucket, deployer):
    for agent, spec in agents.items():
        email = sa_email(spec["sa"], project)
        if not _sa_exists(email):
            print(f"  create SA {spec['sa']}", flush=True)
            _gcloud(["iam", "service-accounts", "create", spec["sa"], "--project", project,
                     "--display-name", spec["sa"]])
        for role in spec.get("roles", []):
            _grant(f"{spec['sa']} {role}",
                   ["projects", "add-iam-policy-binding", project,
                    "--member", f"serviceAccount:{email}", "--role", role,
                    "--condition=None", "--quiet"])
        if bucket and spec.get("infra_grants", True):
            _grant(f"{spec['sa']} staging-bucket role",
                   ["storage", "buckets", "add-iam-policy-binding", f"gs://{bucket}",
                    "--member", f"serviceAccount:{email}",
                    "--role", infra["staging_bucket_role"], "--quiet"])
        if deployer and spec.get("infra_grants", True):
            _grant(f"deployer actAs {spec['sa']}",
                   ["iam", "service-accounts", "add-iam-policy-binding", email,
                    "--member", f"user:{deployer}", "--role", infra["deployer_role"],
                    "--project", project, "--quiet"])
        print(f"  ready: {email}", flush=True)


def should_write(args, n_changes: int) -> bool:
    """Resolve write mode: --dry-run never writes; --apply writes unprompted;
    the default shows the report then asks — degrading to dry-run without a TTY."""
    if n_changes == 0 or getattr(args, "dry_run", False):
        return False
    if getattr(args, "apply", False):
        return True
    if not sys.stdin.isatty():
        print("\n(no TTY — treating as --dry-run. Pass --apply to write non-interactively.)")
        return False
    return input(f"\nApply {n_changes} change(s)? [y/N] ").strip().lower() in ("y", "yes")


def report(findings):
    for f in findings:
        line = f"{_SYMBOL[f['status']]} {f['status']:<7} {f['kind']}: {f['name']}"
        if f["detail"]:
            line += f" — {f['detail']}"
        print(line)
    n_bad = sum(1 for f in findings if f["status"] != OK)
    print(f"\n{len(findings) - n_bad}/{len(findings)} OK" + (f", {n_bad} to reconcile" if n_bad else ""))
    return n_bad


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Per-agent runtime SA provisioning")
    ap.add_argument("--manifest",
                    default=str(Path(__file__).resolve().parents[1] / "runtime-manifest.yaml"))
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true",
                   help="read-only report (exit 1 on drift); writes nothing")
    g.add_argument("--dry-run", action="store_true",
                   help="report only; write nothing")
    g.add_argument("--apply", action="store_true",
                   help="write without prompting (automation); default asks for confirmation")
    args = ap.parse_args(argv)

    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    import demo_env

    m = demo_env.load_manifest(args.manifest)
    project, agents, infra = m["project"], m["agents"], m["infra"]
    # identities = the agents + the BFF (SA/roles only — no bucket/actAs: it's
    # a Cloud Run service, not an engine the deployer creates).
    identities = dict(agents)
    if m.get("bff"):
        identities["bff"] = {**m["bff"], "infra_grants": False}
    bucket = os.environ.get("STAGING_BUCKET") or infra.get("staging_bucket")
    region = os.environ.get("REGION", infra.get("region", "us-central1"))
    deployer = _deployer()

    print(f"# agent identity + infra — project {project}"
          + (f", bucket {bucket}" if bucket else " (STAGING_BUCKET unset — bucket checks skipped)")
          + "\n")
    enabled, svc_err = _enabled_services(project)
    bucket_ok = _bucket_exists(bucket) if bucket else False
    geap_agent = _geap_service_agent(project) if bucket else None
    existing = {sa_email(s["sa"], project) for s in identities.values()
                if _sa_exists(sa_email(s["sa"], project))}
    role_map = _project_role_map(project)
    bucket_members = _bucket_members(bucket, infra["staging_bucket_role"]) if bucket_ok else None
    actas = {e: _actas_members(e, infra["deployer_role"]) for e in existing}
    net = infra.get("network")
    dns_peer_findings = []
    if net and net.get("apigee_psc") and geap_agent:
        has_peer = "roles/dns.peer" in role_map.get(geap_agent, set())
        dns_peer_findings = [_f(OK if has_peer else DRIFT, "geapDnsPeer",
                                geap_agent,
                                "dns.peer ✓" if has_peer else
                                "lacks roles/dns.peer — engines' DNS peering to "
                                "apigee.com. fails (internal.apigee.com unresolvable)")]
    build_role = infra.get("build_sa_role")
    if build_role:
        pn = _project_number(project)
        if pn:
            build_sa = f"{pn}-compute@developer.gserviceaccount.com"
            has_build = build_role in role_map.get(build_sa, set())
            dns_peer_findings.append(_f(OK if has_build else DRIFT, "buildSA", build_sa,
                                        f"{build_role} ✓" if has_build else
                                        f"lacks {build_role} — BFF image builds 403 "
                                        "on the cloudbuild source bucket"))
        else:
            dns_peer_findings.append(_f(DRIFT, "buildSA", "compute-default",
                                        "project number unknown — state UNKNOWN"))
    if net and net.get("network_attachment") and geap_agent:
        has_na = "roles/compute.networkAdmin" in role_map.get(geap_agent, set())
        dns_peer_findings.append(_f(OK if has_na else DRIFT, "geapNetworkAdmin",
                                    geap_agent,
                                    "compute.networkAdmin ✓" if has_na else
                                    "lacks roles/compute.networkAdmin — PSC-I engine "
                                    "deploys 403 on networkAttachments.get/update"))
    findings = (compare_services(infra.get("services", []), enabled, svc_err)
                + dns_peer_findings
                + compare_ai_network(net, fetch_ai_network(project, net))
                + compare_bucket(bucket, bucket_ok, geap_agent, bucket_members)
                + compare_agent_sas(identities, existing, role_map, project)
                + compare_grants(agents, project, bucket_members, actas, deployer))
    acl = m.get("acl")
    if acl:
        database = acl.get("database", "(default)")
        db_ok = _db_exists(project, database)
        token = _access_token()
        live_roles, live_users = _fetch_acl_live(acl, project, token) if db_ok else ({}, {})
        findings += compare_acl(acl.get("roles") or {}, acl.get("users") or {},
                                live_roles, live_users, db_ok, database)
    n_bad = report(findings)
    if args.check:
        return 1 if n_bad else 0
    if not should_write(args, n_bad):
        if n_bad:
            print(f"nothing written — {n_bad} finding(s) pending.")
        return 1 if n_bad else 0
    _apply_infra(infra, project, region, bucket, geap_agent, enabled)
    _apply_ai_network(net, project)
    _apply_build_sa(infra, project)
    _apply(identities, infra, project, bucket, deployer)
    if acl:
        _apply_acl(acl, project, _access_token())
    print("done. Next: python apigee/provision/provision.py secrets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
