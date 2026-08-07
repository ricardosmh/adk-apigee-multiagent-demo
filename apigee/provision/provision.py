#!/usr/bin/env python3
"""Apigee provisioning — manifest-driven, GATEWAY side.

Subcommands name the SCOPE; flags name the MODE — uniform across both (and
across agents/provision/provision_agents.py):

    apigee          the Apigee org: target servers, collectors, products,
                    apps, retired resources
    secrets         each app's consumer key → its Secret Manager secret
                    (AI project) + secretAccessor for agent SAs and the
                    GEAP service agent

    --check         read-only findings report (OK/DRIFT/MISSING/EXTRA);
                    exit 1 on any delta (CI gate)
    --dry-run       preview the reconcile plan; write nothing
    --apply         write without prompting (automation)
    (bare)          show the plan, then ask for confirmation; degrades to
                    --dry-run without a TTY

Scope split: this tool owns the GATEWAY context. Agent SA lifecycle (creation,
project roles, bucket/actAs) is agents/provision/provision_agents.py against
agents/runtime-manifest.yaml; run that FIRST — accessor grants skip missing SAs.

Auth: APIGEE_TOKEN env, else `gcloud auth print-access-token` (secrets also
shells out to gcloud). Design: compare_* functions are pure (no I/O) and
unit-tested; fetch_*/gcloud layers do the I/O; writes only after confirmation.
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

APIGEE = "https://apigee.googleapis.com/v1"

# TLS context: prefer certifi's CA bundle (robust on macOS python.org builds,
# whose stdlib ssl has no local root certs); fall back to the system default.
try:
    import certifi

    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:  # certifi absent — use whatever the platform provides
    _SSL_CTX = ssl.create_default_context()
OK, DRIFT, MISSING, EXTRA, UNKNOWN = "OK", "DRIFT", "MISSING", "EXTRA", "UNKNOWN"
_SYMBOL = {OK: "✅", DRIFT: "⚠", MISSING: "❌", EXTRA: "➖", UNKNOWN: "❓"}


def _f(status, kind, name, detail=""):
    return {"status": status, "kind": kind, "name": name, "detail": detail}


# ── Pure comparators (unit-tested; take desired list + {name: live_obj}) ───────
def compare_target_servers(desired, live):
    out = []
    for d in desired:
        a = live.get(d["name"])
        if a is None:
            out.append(_f(MISSING, "targetServer", d["name"]))
            continue
        diffs = []
        if a.get("host") != d["host"]:
            diffs.append(f"host={a.get('host')!r} want {d['host']!r}")
        if int(a.get("port", -1)) != int(d["port"]):
            diffs.append(f"port={a.get('port')} want {d['port']}")
        a_ssl = bool((a.get("sSLInfo") or {}).get("enabled", False))
        if a_ssl != bool(d.get("ssl", False)):
            diffs.append(f"ssl={a_ssl} want {d.get('ssl')}")
        if a.get("isEnabled") is False:
            diffs.append("isEnabled=false")
        out.append(_f(DRIFT if diffs else OK, "targetServer", d["name"], "; ".join(diffs)))
    return out


def compare_data_collectors(desired, live):
    out = []
    for d in desired:
        a = live.get(d["name"])
        if a is None:
            out.append(_f(MISSING, "dataCollector", d["name"]))
            continue
        want = d["type"].upper()
        got = str(a.get("type", "")).upper().replace("TYPE_", "")
        out.append(_f(OK if got == want else DRIFT, "dataCollector", d["name"],
                      "" if got == want else f"type={got} want {want}"))
    return out


def _product_proxies(a):
    ocs = ((a.get("operationGroup") or {}).get("operationConfigs")) or []
    return [oc["apiSource"] for oc in ocs if oc.get("apiSource")]


def _llm_pairs(product: dict) -> set:
    """{(apiSource, model)} pairs in a live product's llmOperationGroup."""
    pairs = set()
    for oc in (product.get("llmOperationGroup") or {}).get("operationConfigs") or []:
        for op in oc.get("llmOperations") or []:
            if op.get("model"):
                pairs.add((oc.get("apiSource"), op["model"]))
    return pairs


def desired_llm_pairs(d: dict) -> set:
    """Every manifest proxy × llmModel combination (the llm token accounting
    matrix). Empty when the product declares no llmModels."""
    return {(px, m) for px in d.get("proxies", []) for m in d.get("llmModels", [])}


def resolve_telemetry_member(symbol: str, meta: dict, org_number: str | None) -> str | None:
    """Symbolic manifest member -> concrete IAM member. None = unresolvable
    (e.g. the org project number lookup failed) — reported, never guessed."""
    if symbol.startswith(("serviceAccount:", "user:", "group:")):
        return symbol                     # raw member (already fully qualified)
    if symbol == "deploy_sa":
        return f"serviceAccount:{meta['deploy_sa']}"
    if symbol == "ecommerce_deploy_sa":
        return f"serviceAccount:{meta['ecommerce_deploy_sa']}"
    if symbol == "apigee_service_agent":
        if not org_number:
            return None
        return f"serviceAccount:service-{org_number}@gcp-sa-apigee.iam.gserviceaccount.com"
    return None


def compare_telemetry_grants(grants: list, resolved: dict, role_members: dict | None) -> list:
    """grants = manifest telemetryGrants; resolved = {symbol: member-or-None};
    role_members = {role: set(members)} from the ai_project IAM policy (None =
    policy unreadable -> honest UNKNOWN, never a false MISSING)."""
    out = []
    for g in grants:
        name = f"{g['member']} → {g['role']}"
        member = resolved.get(g["member"])
        if member is None:
            out.append(_f(DRIFT, "telemetryGrant", name,
                          f"cannot resolve member '{g['member']}' — state UNKNOWN"))
            continue
        if role_members is None:
            out.append(_f(DRIFT, "telemetryGrant", name,
                          "could not read ai_project IAM policy — state UNKNOWN"))
            continue
        has = member in role_members.get(g["role"], set())
        out.append(_f(OK if has else DRIFT, "telemetryGrant", name,
                      f"{member} ✓" if has else f"{member} lacks {g['role']} on ai_project"))
    return out


def compare_deploy_sa(sa_email: str | None, exists: bool | None,
                      actas: set | None, deployer: str | None,
                      minter: str | None = None,
                      minters: set | None = None) -> list:
    """A proxy deploy SA — BOTH meta.deploy_sa (the AI proxies' identity) and
    meta.ecommerce_deploy_sa (the dedicated invoker) live in the org project
    and need: exist, deployer actAs (deploy time), and the APIGEE SERVICE
    AGENT with serviceAccountTokenCreator (run time — the runtime mints the
    proxy's Google tokens as this agent; without it every GoogleAccessToken/
    GoogleIDToken call fails AccessTokenGenerationFailure. Found live: fresh
    orgs lack the grant standard provisioning normally makes)."""
    if not sa_email:
        return []
    if exists is None:
        return [_f(DRIFT, "deploySA", sa_email, "could not read — state UNKNOWN")]
    if not exists:
        return [_f(MISSING, "deploySA", sa_email, "does not exist in the org project")]
    problems = []
    # case-insensitive: IAM returns the member with the account's canonical
    # casing, which can differ from gcloud's lowercase email.
    if deployer and (actas is None or f"user:{deployer}".lower()
                     not in {m.lower() for m in actas}):
        problems.append(f"{deployer} cannot actAs (proxy deploys as this SA)")
    if minter and (minters is None or minter not in minters):
        problems.append("apigee service agent lacks serviceAccountTokenCreator "
                        "(runtime Google-token minting fails)")
    if problems:
        return [_f(DRIFT, "deploySA", sa_email, "; ".join(problems))]
    return [_f(OK, "deploySA", sa_email, "exists ✓ actAs ✓ token minting ✓"
               if (deployer and minter) else "exists ✓")]


def fetch_deploy_sa(sa_email: str | None, org_project: str):
    if not sa_email:
        return None, None
    ok, r = _gcloud(["iam", "service-accounts", "describe", sa_email,
                     f"--project={org_project}"])
    if not ok:
        return (None, None, None) if "PERMISSION_DENIED" in r.stderr else (False, None, None)
    ok2, r2 = _gcloud(["iam", "service-accounts", "get-iam-policy", sa_email,
                       f"--project={org_project}", "--format=json"])
    actas, minters = set(), set()
    if ok2:
        for b in json.loads(r2.stdout or "{}").get("bindings", []):
            if b.get("role") == "roles/iam.serviceAccountUser":
                actas.update(b.get("members", []))
            if b.get("role") == "roles/iam.serviceAccountTokenCreator":
                minters.update(b.get("members", []))
    return True, actas, minters


def _deployer_account():
    ok, r = _gcloud(["config", "get-value", "account"])
    return r.stdout.strip() if ok and r.stdout.strip() else None


def compare_trace_config(desired: dict | None, live: dict | None) -> list:
    """Env traceConfig vs manifest (exporter/endpoint/samplingRate). Empty when
    the manifest declares none. A failed listing (live carries _error) is
    UNKNOWN — reported, never crashed on, never blindly applied."""
    if not desired:
        return []
    if live and live.get("_error"):
        return [_f(UNKNOWN, "traceConfig", "distributed-trace",
                   f"listing failed — {live['_error']}")]
    live = live or {}
    got = (live.get("exporter"), live.get("endpoint"),
           (live.get("samplingConfig") or {}).get("samplingRate"))
    want = (desired["exporter"], desired["endpoint"], desired.get("samplingRate"))
    ok = got == want
    return [_f(OK if ok else DRIFT, "traceConfig", "distributed-trace",
               "exporting to Cloud Trace ✓" if ok else f"live={got} want {want}")]


def compare_products(desired, live):
    out = []
    for d in desired:
        a = live.get(d["name"])
        if a is None:
            out.append(_f(MISSING, "apiProduct", d["name"]))
            continue
        diffs = []
        if sorted(a.get("environments", [])) != sorted(d.get("environments", [])):
            diffs.append(f"environments={sorted(a.get('environments', []))} want {sorted(d.get('environments', []))}")
        if sorted(_product_proxies(a)) != sorted(d.get("proxies", [])):
            diffs.append(f"proxies={sorted(_product_proxies(a))} want {sorted(d.get('proxies', []))}")
        # Native LLM token quota (per-key counters, product-level allowance):
        # manifest llmQuota {tokens, interval, timeUnit} <-> flat live fields.
        q = d.get("llmQuota")
        if q:
            got = (a.get("llmQuota"), a.get("llmQuotaInterval"), a.get("llmQuotaTimeUnit"))
            want = (str(q["tokens"]), str(q["interval"]), str(q["timeUnit"]))
            if got != want:
                diffs.append(f"llmQuota={'/'.join(x or '-' for x in got)} want {'/'.join(want)}")
        want_pairs = desired_llm_pairs(d)
        if want_pairs:
            got_pairs = _llm_pairs(a)
            if got_pairs != want_pairs:
                missing = sorted(want_pairs - got_pairs)
                stale = sorted(got_pairs - want_pairs)
                bits = []
                if missing:
                    bits.append(f"llmOps missing {missing}")
                if stale:
                    bits.append(f"llmOps stale {stale}")   # e.g. the deleted non-SSE proxy
                diffs.append("; ".join(bits))
        out.append(_f(DRIFT if diffs else OK, "apiProduct", d["name"], "; ".join(diffs)))
    return out


def _app_products(a):
    prods = set()
    for cred in a.get("credentials", []) or []:
        for p in cred.get("apiProducts", []) or []:
            if p.get("apiproduct"):
                prods.add(p["apiproduct"])
    return sorted(prods)


def compare_apps(desired, live):
    out = []
    for d in desired:
        a = live.get(d["name"])
        if a is None:
            out.append(_f(MISSING, "app", d["name"]))
            continue
        want = sorted(d.get("products", []))
        got = _app_products(a)
        out.append(_f(OK if got == want else DRIFT, "app", d["name"],
                      "" if got == want else f"products={got} want {want}"))
    return out


def compare_retired_apps(retired, live):
    return [
        _f(EXTRA, "app(retired)", n, "still exists — should be deleted")
        if n in live else _f(OK, "app(retired)", n, "absent (good)")
        for n in retired
    ]


# ── Live API layer (read-only GETs) ───────────────────────────────────────────
def _token():
    t = os.environ.get("APIGEE_TOKEN")
    if t:
        return t
    return subprocess.run(
        ["gcloud", "auth", "print-access-token"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _get(url, token):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30, context=_SSL_CTX) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def fetch_target_servers(org, env, token):
    base = f"{APIGEE}/organizations/{org}/environments/{env}/targetservers"
    names = _get(base, token) or []
    return {n: _get(f"{base}/{n}", token) for n in names}


def fetch_data_collectors(org, token):
    data = _get(f"{APIGEE}/organizations/{org}/datacollectors", token) or {}
    return {d["name"]: d for d in data.get("dataCollectors", []) if d.get("name")}


def fetch_products(org, token):
    data = _get(f"{APIGEE}/organizations/{org}/apiproducts?expand=true", token) or {}
    return {p["name"]: p for p in data.get("apiProduct", []) if p.get("name")}


def fetch_apps(org, token):
    # org-level apps list (doesn't need the developer email)
    data = _get(f"{APIGEE}/organizations/{org}/apps?expand=true", token) or {}
    return {a["name"]: a for a in data.get("app", []) if a.get("name")}


# ── Manifest + report ─────────────────────────────────────────────────────────
def load_manifest(path):
    """Manifest with demo-environment.yaml tokens (${projects.*} etc.) resolved."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    import demo_env

    return demo_env.load_manifest(path)


def report(findings):
    for f in findings:
        line = f"{_SYMBOL[f['status']]} {f['status']:<7} {f['kind']}: {f['name']}"
        if f["detail"]:
            line += f" — {f['detail']}"
        print(line, flush=True)
    n_bad = sum(1 for f in findings if f["status"] != OK)
    print(f"\n{len(findings) - n_bad}/{len(findings)} OK" + (f", {n_bad} to reconcile" if n_bad else ""),
          flush=True)
    return n_bad


def fetch_trace_config(org, env, token):
    # Some org flavors 400/403 this endpoint (e.g. add-on not provisioned) —
    # report honestly instead of crashing the whole run on one listing.
    try:
        return _get(f"{APIGEE}/organizations/{org}/environments/{env}/traceConfig", token)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode(errors="replace")[:300]
        except Exception:
            pass
        return {"_error": f"HTTP {e.code}: {body or e.reason}"}


def _geap_service_agent(ai_project: str) -> str | None:
    """service-<AI PROJECT NUMBER>@gcp-sa-aiplatform... — DERIVED so no project
    number ever lives in a manifest. None when the lookup fails (callers skip
    the grant and the accessor check reports it)."""
    num = _org_project_number(ai_project)
    return f"service-{num}@gcp-sa-aiplatform.iam.gserviceaccount.com" if num else None


def _org_project_number(org_project: str) -> str | None:
    ok, r = _gcloud(["projects", "describe", org_project, "--format=value(projectNumber)"])
    return r.stdout.strip() if ok and r.stdout.strip() else None


def _project_role_members(project: str) -> dict | None:
    ok, r = _gcloud(["projects", "get-iam-policy", project, "--format=json"])
    if not ok:
        return None
    out = {}
    for b in json.loads(r.stdout).get("bindings", []):
        out.setdefault(b.get("role"), set()).update(b.get("members", []))
    return out


def _telemetry_state(m):
    grants = m.get("telemetryGrants") or []
    if not grants:
        return [], {}
    resolved = {g["member"]: resolve_telemetry_member(
        g["member"], m["meta"], _org_project_number(m["meta"]["org"]))
        for g in grants}
    return compare_telemetry_grants(
        grants, resolved, _project_role_members(m["meta"]["ai_project"])), resolved


def resolve_target_host(host: str, org: str, token) -> tuple[str | None, str]:
    """Resolve symbolic target-server hosts. "@endpointAttachment:<id>" reads
    organizations/{org}/endpointAttachments/<id> and returns its `host` (the
    PSC endpoint the Apigee runtime can reach). (None, reason) when
    unresolvable — callers report UNKNOWN, never a false value."""
    if not host.startswith("@endpointAttachment:"):
        return host, ""
    ea_id = host.split(":", 1)[1]
    try:
        ea = _get(f"{APIGEE}/organizations/{org}/endpointAttachments/{ea_id}", token)
    except Exception as e:  # noqa: BLE001
        return None, f"endpointAttachment {ea_id} read failed: {e}"
    if not ea or not ea.get("host"):
        return None, (f"endpointAttachment {ea_id} not found or has no host yet — "
                      "run services provisioning first")
    return ea["host"], ""


def compare_shared_flow_deps(deps: list, deployed: dict | None) -> list:
    """deps = shared flow names proxies depend on; deployed = {name: bool}
    (None = listing failed). Existence-only: these predate the manifest."""
    if not deps:
        return []
    if deployed is None:
        return [_f(DRIFT, "sharedFlowDep", "listing",
                   "could not read shared-flow deployments — state UNKNOWN")]
    return [_f(OK if deployed.get(n) else MISSING, "sharedFlowDep", n,
               "deployed ✓" if deployed.get(n)
               else "NOT deployed — dependent proxies fail at runtime")
            for n in deps]


def fetch_shared_flow_deployments(org, env, names, token):
    out = {}
    for n in names:
        try:
            d = _get(f"{APIGEE}/organizations/{org}/environments/{env}"
                     f"/sharedflows/{n}/deployments", token)
        except Exception:  # noqa: BLE001
            return None
        out[n] = bool(d and d.get("deployments"))
    return out


def _findings(m, token):
    org, env = m["meta"]["org"], m["meta"]["env"]
    live_apps = fetch_apps(org, token)
    telemetry_findings, m["_telemetry_resolved"] = _telemetry_state(m)
    # Resolve symbolic hosts ONCE; unresolvable ones become honest UNKNOWN rows
    # and are excluded from the create/update plan (nothing false is written).
    desired_ts, ts_unknown = [], []
    for d in m.get("targetServers", []):
        host, reason = resolve_target_host(d["host"], org, token)
        if host is None:
            ts_unknown.append(_f(DRIFT, "targetServer", d["name"],
                                 f"host unresolvable — {reason}"))
        else:
            desired_ts.append({**d, "host": host})
    m["_resolved_target_servers"] = desired_ts
    # The Apigee service agent is needed by the deploySA rows REGARDLESS of
    # telemetryGrants — reading it from _telemetry_resolved silently disabled
    # the tokenCreator check AND grant once the optional cloudtrace.agent grant
    # was commented out (seen live: fresh org, AccessTokenGenerationFailure on
    # every view even after a full provision apply). Resolve it directly and
    # stash it for _execute; report honestly when unresolvable.
    agent_member = resolve_telemetry_member("apigee_service_agent", m["meta"],
                                            _org_project_number(org))
    m["_apigee_service_agent"] = agent_member
    agent_unknown = [] if agent_member else [
        _f(UNKNOWN, "deploySA", "apigee-service-agent",
           "cannot resolve the Apigee service agent (org project number lookup"
           " failed) — token-minting state UNKNOWN")]
    ai_sa = m["meta"].get("deploy_sa")
    ai_exists, ai_actas, ai_minters = fetch_deploy_sa(ai_sa, org)
    ec_sa = m["meta"].get("ecommerce_deploy_sa")
    ec_exists, ec_actas, ec_minters = fetch_deploy_sa(ec_sa, org)
    return (
        telemetry_findings
        + agent_unknown
        + compare_deploy_sa(ai_sa, ai_exists, ai_actas, _deployer_account(),
                            agent_member, ai_minters)
        + compare_deploy_sa(ec_sa, ec_exists, ec_actas, _deployer_account(),
                            agent_member, ec_minters)
        + compare_trace_config(m.get("traceConfig"),
                           fetch_trace_config(org, env, token)
                           if m.get("traceConfig") else None)
        + ts_unknown
        + compare_target_servers(desired_ts, fetch_target_servers(org, env, token))
        + compare_shared_flow_deps(
            m.get("sharedFlowDeps", []),
            fetch_shared_flow_deployments(org, env, m.get("sharedFlowDeps", []), token))
        + compare_data_collectors(m.get("dataCollectors", []), fetch_data_collectors(org, token))
        + compare_products(m.get("apiProducts", []), fetch_products(org, token))
        + compare_apps(m.get("apps", []), live_apps)
        + compare_retired_apps((m.get("retired") or {}).get("apps", []), live_apps)
    )


# ── apigee scope: plan (pure, tested) + execute (writes; validate live) ───────
# deploySA strictly first: telemetryGrants bind roles to those SAs, so on a
# fresh org granting before creating fails the grant into a second pass.
_RANK = {"deploySA": 0, "telemetryGrant": 1, "traceConfig": 1, "targetServer": 1, "dataCollector": 2, "apiProduct": 3, "app": 4, "app(retired)": 5}


def derive_plan(findings, manifest):
    """Pure: findings -> ordered reconcile actions. Creates/updates before app
    links; retired deletes last. Data-collector TYPE drift is MANUAL (immutable)."""
    desired = {
        "deploySA": {sa: {"email": sa}
                     for sa in (manifest.get("meta", {}).get("deploy_sa", ""),
                                manifest.get("meta", {}).get("ecommerce_deploy_sa", ""))
                     if sa},
        "telemetryGrant": {f"{g['member']} → {g['role']}": g
                           for g in manifest.get("telemetryGrants", [])},
        "traceConfig": {"distributed-trace": manifest.get("traceConfig")},
        "targetServer": {d["name"]: d for d in manifest.get(
            "_resolved_target_servers", manifest.get("targetServers", []))},
        "dataCollector": {d["name"]: d for d in manifest.get("dataCollectors", [])},
        "apiProduct": {d["name"]: d for d in manifest.get("apiProducts", [])},
        "app": {d["name"]: d for d in manifest.get("apps", [])},
    }
    actions = []
    for f in findings:
        k, n, st = f["kind"], f["name"], f["status"]
        if st == OK:
            continue
        if st == UNKNOWN:
            actions.append({"verb": "MANUAL", "kind": k, "name": n,
                            "detail": f.get("detail") or "listing failed — "
                            "investigate, then re-run"})
            continue
        if k == "sharedFlowDep":
            actions.append({"verb": "MANUAL", "kind": k, "name": n,
                            "detail": "deploy the shared flow to the env by hand "
                                      "(predates the manifest; not managed here)"})
            continue
        if k == "targetServer" and "unresolvable" in (f.get("detail") or ""):
            actions.append({"verb": "MANUAL", "kind": k, "name": n, "detail": f["detail"]})
            continue
        if k == "dataCollector" and st == DRIFT:
            actions.append({"verb": "MANUAL", "kind": k, "name": n,
                            "detail": "type is immutable — delete + recreate by hand"})
        elif st == MISSING:
            actions.append({"verb": "CREATE", "kind": k, "name": n, "desired": desired.get(k, {}).get(n)})
        elif st == DRIFT:
            actions.append({"verb": "UPDATE", "kind": k, "name": n,
                            "desired": desired.get(k, {}).get(n), "detail": f["detail"]})
        elif st == EXTRA:
            actions.append({"verb": "DELETE", "kind": "app", "name": n})
    actions.sort(key=lambda a: (_RANK.get(a["kind"], 6), 9 if a["verb"] == "DELETE" else 0))
    return actions


def print_plan(actions):
    if not actions:
        print("nothing to do — live matches the manifest.")
        return
    print("plan:")
    for a in actions:
        line = f"  {a['verb']:<6} {a['kind']}: {a['name']}"
        if a.get("detail"):
            line += f" — {a['detail']}"
        print(line)


def _write(method, url, token, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60, context=_SSL_CTX) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        # Surface Apigee's explanation — a bare HTTPError hides the message
        # that says WHAT was not found / rejected.
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:500]
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"{method} {url} -> HTTP {e.code}: {detail}") from e


def _product_body(d):
    """CREATE payload: the base product, then _reconcile_product renders the
    manifest's LLM constructs (flat llmQuota fields + the llmOperationGroup
    proxy×model matrix) onto it — the same logic UPDATE uses. Fresh orgs DO
    create products from scratch: the first fresh-org run shipped the LLM
    products with no model allow-list and no token quota because CREATE
    skipped these fields (they were assumed UPDATE-only)."""
    return _reconcile_product({
        "name": d["name"],
        "displayName": d.get("displayName", d["name"]),
        "approvalType": d.get("approvalType", "auto"),
        "environments": d["environments"],
        "attributes": [{"name": "access", "value": d.get("access", "private")}],
        "operationGroup": {
            "operationConfigType": "proxy",
            "operationConfigs": [
                {"apiSource": px, "operations": [{"resource": "/"}], "quota": {}}
                for px in d.get("proxies", [])
            ],
        },
    }, d)


def _reconcile_product(live, d):
    """UPDATE path: mutate the LIVE product in place — environments, proxies,
    and (when the manifest declares them) the LLM constructs: the flat
    llmQuota/Interval/TimeUnit fields and the llmOperationGroup proxy×model
    matrix (pruning entries for retired proxies, preserving any per-model
    llmTokenQuota on kept entries). Everything else is preserved as-is."""
    body = dict(live)
    body["environments"] = list(d["environments"])
    want = set(d.get("proxies", []))
    og = dict(body.get("operationGroup") or {})
    ocs = og.get("operationConfigs") or []
    if want and ocs:
        kept = [oc for oc in ocs if oc.get("apiSource") in want]          # drop dead proxies, keep their config
        have = {oc.get("apiSource") for oc in kept}
        for px in want - have:                                            # add any desired proxy not already present
            kept.append({"apiSource": px, "operations": [{"resource": "/"}]})
        og["operationConfigs"] = kept
        body["operationGroup"] = og
    q = d.get("llmQuota")
    if q:
        body["llmQuota"] = str(q["tokens"])
        body["llmQuotaInterval"] = str(q["interval"])
        body["llmQuotaTimeUnit"] = str(q["timeUnit"])
    want_pairs = desired_llm_pairs(d)
    if want_pairs:
        log = dict(body.get("llmOperationGroup") or {})
        kept, have = [], set()
        for oc in log.get("operationConfigs") or []:
            pairs = {(oc.get("apiSource"), op.get("model"))
                     for op in oc.get("llmOperations") or []}
            if pairs and pairs <= want_pairs:      # entirely desired -> keep (with its llmTokenQuota)
                kept.append(oc)
                have |= pairs
        for px, model in sorted(want_pairs - have):
            kept.append({"apiSource": px,
                         "llmOperations": [{"resource": "/", "model": model}],
                         "llmTokenQuota": {}})
        log["operationConfigs"] = kept
        body["llmOperationGroup"] = log
    return body


def _execute(a, m, token):
    org, env = m["meta"]["org"], m["meta"]["env"]
    base = f"{APIGEE}/organizations/{org}"
    k, n, verb = a["kind"], a["name"], a["verb"]
    if verb == "DELETE":  # retired app — no "desired"; handle before the kind branches
        _write("DELETE", f"{base}/developers/{m['developer']['email']}/apps/{n}", token)
        return
    if k == "targetServer":
        d = a["desired"]
        payload = {"name": d["name"], "host": d["host"], "port": int(d["port"]),
                   "isEnabled": True, "protocol": "HTTP",
                   "sSLInfo": {"enabled": bool(d.get("ssl", True))}}
        url = f"{base}/environments/{env}/targetservers"
        _write("POST", url, token, payload) if verb == "CREATE" else _write("PUT", f"{url}/{n}", token, payload)
    elif k == "dataCollector" and verb == "CREATE":
        _write("POST", f"{base}/datacollectors?dataCollectorId={n}", token,
               {"name": n, "type": a["desired"]["type"].upper()})
    elif k == "apiProduct":
        d = a["desired"]
        if verb == "CREATE":
            _write("POST", f"{base}/apiproducts", token, _product_body(d))
        else:  # UPDATE: fetch live, surgically reconcile env+proxies, PUT (keeps llm config)
            live = _get(f"{base}/apiproducts/{n}", token) or {}
            _write("PUT", f"{base}/apiproducts/{n}", token, _reconcile_product(live, d))
    elif k == "deploySA":
        org_project = m["meta"]["org"]
        if verb == "CREATE":
            display = ("Ecommerce proxies -> Cloud Run invoker"
                       if n == m["meta"].get("ecommerce_deploy_sa")
                       else "Apigee AI proxies deploy SA (GEAP + logging)")
            _gcloud(["iam", "service-accounts", "create", n.split("@")[0],
                     f"--project={org_project}",
                     f"--display-name={display}"])
        deployer = _deployer_account()
        if deployer:
            _gcloud(["iam", "service-accounts", "add-iam-policy-binding", n,
                     f"--project={org_project}", f"--member=user:{deployer}",
                     "--role=roles/iam.serviceAccountUser", "--quiet"])
        # Runtime token minting: the Apigee service agent generates the
        # proxy's Google tokens AS this SA — tokenCreator or every
        # GoogleAccessToken/GoogleIDToken call fails at run time.
        # _apigee_service_agent is resolved by _findings independently of
        # telemetryGrants (reading _telemetry_resolved here skipped this grant
        # whenever the optional cloudtrace.agent grant was commented out).
        agent_member = (m.get("_apigee_service_agent")
                        or (m.get("_telemetry_resolved") or {}).get("apigee_service_agent")
                        or resolve_telemetry_member("apigee_service_agent", m["meta"],
                                                    _org_project_number(org_project)))
        if agent_member:
            _gcloud(["iam", "service-accounts", "add-iam-policy-binding", n,
                     f"--project={org_project}", f"--member={agent_member}",
                     "--role=roles/iam.serviceAccountTokenCreator", "--quiet"])
        else:
            print(f"    ! cannot resolve the Apigee service agent — grant"
                  f" roles/iam.serviceAccountTokenCreator on {n} manually")
    elif k == "telemetryGrant":
        d = a["desired"]
        member = (m.get("_telemetry_resolved") or {}).get(d["member"])
        if not member:
            print(f"    ! cannot resolve member {d['member']} — grant it manually")
            return
        _gcloud(["projects", "add-iam-policy-binding", m["meta"]["ai_project"],
                 "--member", member, "--role", d["role"], "--quiet"])
    elif k == "traceConfig":
        d = a["desired"]
        _write("PATCH", f"{base}/environments/{m['meta']['env']}/traceConfig", token,
               {"exporter": d["exporter"], "endpoint": d["endpoint"],
                "samplingConfig": {"sampler": "PROBABILITY",
                                   "samplingRate": d.get("samplingRate", 0.5)}})
    elif k == "app":
        email, d = m["developer"]["email"], a["desired"]
        if verb == "CREATE":
            # Fresh org: the owning developer may not exist yet — the app POST
            # 404s without it. Idempotent ensure, same shape as _apply_end_user.
            if not _get(f"{base}/developers/{email}", token):
                local = email.split("@")[0]
                _write("POST", f"{base}/developers", token,
                       {"email": email, "userName": email,
                        "firstName": local, "lastName": "developer"})
            _write("POST", f"{base}/developers/{email}/apps", token,
                   {"name": n, "apiProducts": d.get("products", [])})
        else:  # UPDATE: add missing products to the existing key
            app = _get(f"{base}/developers/{email}/apps/{n}", token) or {}
            key = ((app.get("credentials") or [{}])[0]).get("consumerKey")
            add = [p for p in d.get("products", []) if p not in _app_products(app)]
            if key and add:
                _write("POST", f"{base}/developers/{email}/apps/{n}/keys/{key}", token,
                       {"apiProducts": add})


def should_write(args, n_changes: int) -> bool:
    """Resolve write mode: --dry-run never writes; --apply writes unprompted;
    the default shows the plan then asks — degrading to dry-run without a TTY."""
    if n_changes == 0 or getattr(args, "dry_run", False):
        return False
    if getattr(args, "apply", False):
        return True
    if not sys.stdin.isatty():
        print("\n(no TTY — treating as --dry-run. Pass --apply to write non-interactively.)")
        return False
    return input(f"\nApply {n_changes} change(s)? [y/N] ").strip().lower() in ("y", "yes")


def cmd_apigee(args):
    m = load_manifest(args.manifest)
    token = _token()
    print(f"# apigee — org={m['meta']['org']} env={m['meta']['env']}\n")
    findings = _findings(m, token)
    if args.check:  # read-only findings report (per-resource, incl. OK)
        return 1 if report(findings) else 0
    actions = derive_plan(findings, m)
    print_plan(actions)
    doable = [a for a in actions if a["verb"] != "MANUAL"]
    if not doable:
        return 0
    if not should_write(args, len(doable)):
        print(f"nothing written — {len(doable)} change(s) pending.")
        return 1
    for a in doable:
        print(f"  applying {a['verb']} {a['kind']}: {a['name']} ...", flush=True)
        _execute(a, m, token)
    print("done. (run `secrets` for the app-key → Secret Manager wiring)")
    return 0


# ── users: end-user developers/apps for the Direct view (manifest endUsers) ──
def user_app_name(email: str) -> str:
    """Deterministic app name for an end user (llm-user-<email-slug>)."""
    return "llm-user-" + email.strip().lower().replace("@", "-at-").replace(".", "-")


def _app_owner_email(app: dict) -> str | None:
    for attr in app.get("attributes") or []:
        if attr.get("name") == "owner_email":
            return attr.get("value")
    return None


def compare_end_users(emails: list, product: str, live: dict) -> list:
    """live = {email: {"developer": bool, "app": app-dict-or-None}}.
    Per user: developer exists, app exists, app carries the product, and the
    owner_email attribute (the key<->identity binding the proxy enforces)."""
    out = []
    for email in emails:
        email = email.strip().lower()
        st = live.get(email) or {}
        if not st.get("developer"):
            out.append(_f(MISSING, "endUser", email, "no developer"))
            continue
        app = st.get("app")
        if app is None:
            out.append(_f(MISSING, "endUser", email, f"no app {user_app_name(email)}"))
            continue
        diffs = []
        if product not in _app_products(app):
            diffs.append(f"app lacks product {product}")
        owner = _app_owner_email(app)
        if owner != email:
            diffs.append(f"owner_email={owner!r} want {email!r} (proxy 403s without it)")
        out.append(_f(DRIFT if diffs else OK, "endUser", email, "; ".join(diffs)))
    return out


def _fetch_end_user(org, email, token):
    # _get returns None on 404 (it does NOT raise) — checking exceptions here
    # previously misread every missing developer as existing, so apply skipped
    # the developer create and the app POST 404'd (seen live).
    dev = _get(f"{APIGEE}/organizations/{org}/developers/{email}", token)
    if dev is None:
        return {"developer": False, "app": None}
    app = _get(f"{APIGEE}/organizations/{org}/developers/{email}/apps/{user_app_name(email)}", token)
    return {"developer": True, "app": app}


def _apply_end_user(org, email, product, st, token):
    base = f"{APIGEE}/organizations/{org}/developers"
    app_name = user_app_name(email)
    if not st.get("developer"):
        local = email.split("@")[0]
        _write("POST", base, token, {"email": email, "userName": email,
                                     "firstName": local, "lastName": "end-user"})
    app = st.get("app")
    if app is None:
        _write("POST", f"{base}/{email}/apps", token,
               {"name": app_name, "apiProducts": [product],
                "attributes": [{"name": "owner_email", "value": email}]})
        created = _get(f"{base}/{email}/apps/{app_name}", token) or {}
        key = ((created.get("credentials") or [{}])[0]).get("consumerKey", "<fetch via console>")
        print(f"    key for {email} (hand out ONCE, out of band): {key}")
        return
    # drift: fix attributes and/or attach the product to the existing key
    if _app_owner_email(app) != email:
        attrs = [a for a in app.get("attributes") or [] if a.get("name") != "owner_email"]
        attrs.append({"name": "owner_email", "value": email})
        _write("POST", f"{base}/{email}/apps/{app_name}/attributes", token, {"attribute": attrs})
    if product not in _app_products(app):
        key = ((app.get("credentials") or [{}])[0]).get("consumerKey")
        if key:
            _write("POST", f"{base}/{email}/apps/{app_name}/keys/{key}", token,
                   {"apiProducts": [product]})


def cmd_users(args):
    m = load_manifest(args.manifest)
    spec = m.get("endUsers") or {}
    emails = [e.strip().lower() for e in spec.get("emails", [])]
    product = spec.get("product")
    if not emails or not product:
        print("manifest has no endUsers section — nothing to do.")
        return 0
    token = _token()
    org = m["meta"]["org"]
    print(f"# end users — org={m['meta']['org']} product={product}\n")
    live = {e: _fetch_end_user(org, e, token) for e in emails}
    findings = compare_end_users(emails, product, live)
    if args.check:
        return 1 if report(findings) else 0
    todo = [f for f in findings if f["status"] != OK]
    report(findings)
    if not todo:
        return 0
    if not should_write(args, len(todo)):
        print(f"nothing written — {len(todo)} user(s) pending.")
        return 1
    for f in todo:
        email = f["name"]
        print(f"  reconciling {email} ...", flush=True)
        _apply_end_user(org, email, product, live[email], token)
    print("done. Hand each printed key to its owner; the proxy binds it to their IAP email.")
    return 0


# ── secrets: app keys → Secret Manager + accessor grants (via gcloud) ─────────
# SA lifecycle (creation, project roles) is the AGENT side's concern —
# agents/provision/provision_agents.py against agents/runtime-manifest.yaml. This side
# only wires each app's consumer key into its secret and grants read access.
def _sa_email(name, project):
    return f"{name}@{project}.iam.gserviceaccount.com"


def derive_secrets(m):
    """Manifest -> [{name, source_app, accessors}]. A secret is fed by its source
    Apigee app's consumer key; accessors = the agent SA(s) that list it under
    secretAccessor, plus the GEAP service agent (deploy-time SecretRef)."""
    proj = m["meta"]["ai_project"]
    geap = m["meta"].get("geap_service_agent") or _geap_service_agent(proj)
    acc = {}
    for sa in m.get("serviceAccounts", []):
        for sec in sa.get("secretAccessor", []) or []:
            acc.setdefault(sec, set()).add(_sa_email(sa["name"], proj))
    src = {}
    for app in m.get("apps", []):
        name = app.get("secret", "")
        if name and not name.startswith("<"):
            src[name] = app["name"]
    out = []
    for name in sorted(set(acc) | set(src)):
        members = set(acc.get(name, set()))
        if geap:
            members.add(geap)
        out.append({"name": name, "source_app": src.get(name), "accessors": sorted(members)})
    return out


def compare_secrets(secret_specs, existing_names, accessor_map, key_match=None):
    """Per secret: exists → accessors granted → value matches the Apigee app's
    current consumer key. ``key_match`` maps name → True (matches) / False
    (STALE — app key rotated but secret not updated) / None (couldn't read the
    secret to compare); omit it to skip value checking entirely. OK findings
    carry detail too, so the report shows WHAT was validated, not just a ✅."""
    out = []
    for s in secret_specs:
        if s["name"] not in existing_names:
            out.append(_f(MISSING, "secret", s["name"]))
            continue
        problems, checked = [], []
        missing = [m for m in s["accessors"] if m not in accessor_map.get(s["name"], set())]
        if missing:
            problems.append("missing accessor: " + ", ".join(missing))
        else:
            checked.append(f"{len(s['accessors'])} accessor(s) granted")
        km = (key_match or {}).get(s["name"])
        if km is False:
            problems.append(f"value STALE — does not match app {s['source_app']}'s current consumer key")
        elif km is True:
            checked.append(f"value matches app {s['source_app']}'s key")
        elif key_match is not None and s["source_app"]:
            checked.append("key-match skipped (secret or app key unreadable)")
        out.append(_f(DRIFT if problems else OK, "secret", s["name"],
                      "; ".join(problems) if problems else "; ".join(checked)))
    return out


def _gcloud(args):
    r = subprocess.run(["gcloud", *args], capture_output=True, text=True,
                       stdin=subprocess.DEVNULL,
                       env={**os.environ, "CLOUDSDK_CORE_DISABLE_PROMPTS": "1"})
    return r.returncode == 0, r


def _sa_exists(email):
    return _gcloud(["iam", "service-accounts", "describe", email, "--format=json"])[0]


def _secret_exists(name, project):
    return _gcloud(["secrets", "describe", name, "--project", project, "--format=json"])[0]


def _secret_accessors(name, project):
    ok, r = _gcloud(["secrets", "get-iam-policy", name, "--project", project, "--format=json"])
    if not ok:
        return set()
    out = set()
    for b in json.loads(r.stdout).get("bindings", []):
        if b["role"] == "roles/secretmanager.secretAccessor":
            for mem in b.get("members", []):
                if mem.startswith("serviceAccount:"):
                    out.add(mem.split(":", 1)[1])
    return out


def _secret_value(name, project):
    """Latest secret version's value, or None if unreadable (missing version or
    no secretmanager.versions.access for the operator). Never printed."""
    ok, r = _gcloud(["secrets", "versions", "access", "latest", "--secret", name, "--project", project])
    return r.stdout if ok else None


def _add_secret_version_if_changed(name, project, value):
    if _secret_value(name, project) == value:
        return False
    subprocess.run(["gcloud", "secrets", "versions", "add", name, "--project", project, "--data-file=-"],
                   input=value, text=True, capture_output=True)
    return True


def _secrets_apply(m, secrets, proj):
    apps = fetch_apps(m["meta"]["org"], _token())
    for s in secrets:
        if not _secret_exists(s["name"], proj):
            print(f"  create secret {s['name']}", flush=True)
            _gcloud(["secrets", "create", s["name"], "--project", proj, "--replication-policy", "automatic"])
        if s["source_app"]:
            app = apps.get(s["source_app"]) or {}
            key = ((app.get("credentials") or [{}])[0]).get("consumerKey")
            if key and _add_secret_version_if_changed(s["name"], proj, key):
                print(f"  + version for {s['name']} (from {s['source_app']})", flush=True)
        for member in s["accessors"]:
            # Agent SAs are created agent-side; a grant on a missing SA fails, so
            # skip + point at the right tool instead of half-erroring.
            if member.endswith(f"@{proj}.iam.gserviceaccount.com") and not _sa_exists(member):
                print(f"  ⚠ {member} does not exist — skipped accessor grant on {s['name']}."
                      " Create agent SAs first: python agents/provision/provision_agents.py --apply")
                continue
            _gcloud(["secrets", "add-iam-policy-binding", s["name"], "--project", proj,
                     "--member", f"serviceAccount:{member}", "--role", "roles/secretmanager.secretAccessor", "--quiet"])


def cmd_secrets(args):
    m = load_manifest(args.manifest)
    proj, org = m["meta"]["ai_project"], m["meta"]["org"]
    secrets = derive_secrets(m)
    print(f"# secrets — project {proj}, {len(secrets)} secret(s)\n"
          f"# validating per secret: exists → accessor grants → value == its\n"
          f"# Apigee app's current consumer key (org {org}); values never printed\n",
          flush=True)
    print(f"  fetching app credentials from Apigee ({org}) ...", flush=True)
    apps = fetch_apps(org, _token())
    existing, acc_map, key_match = set(), {}, {}
    for s in secrets:
        print(f"  checking {s['name']} ...", flush=True)
        if not _secret_exists(s["name"], proj):
            continue
        existing.add(s["name"])
        acc_map[s["name"]] = _secret_accessors(s["name"], proj)
        app = apps.get(s["source_app"]) or {}
        app_key = ((app.get("credentials") or [{}])[0]).get("consumerKey")
        val = _secret_value(s["name"], proj)
        key_match[s["name"]] = None if (val is None or not app_key) else (val == app_key)
    print(flush=True)
    n_bad = report(compare_secrets(secrets, existing, acc_map, key_match))
    if args.check:
        return 1 if n_bad else 0
    if not should_write(args, n_bad):
        if n_bad:
            print(f"nothing written — {n_bad} finding(s) pending.")
        return 1 if n_bad else 0
    _secrets_apply(m, secrets, proj)
    print("done.")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="Apigee manifest-driven provisioning")
    here = Path(__file__).parent
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn in (("apigee", cmd_apigee), ("secrets", cmd_secrets), ("users", cmd_users)):
        sp = sub.add_parser(name)
        sp.add_argument("--manifest", default=str(here / "manifest.yaml"))
        g = sp.add_mutually_exclusive_group()
        g.add_argument("--check", action="store_true",
                       help="read-only findings report (exit 1 on drift); writes nothing")
        g.add_argument("--dry-run", action="store_true",
                       help="preview the plan; write nothing")
        g.add_argument("--apply", action="store_true",
                       help="write without prompting (automation); default asks for confirmation")
        sp.set_defaults(func=fn)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
