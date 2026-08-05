#!/usr/bin/env python3
"""Analytics provisioning — the OPTIONAL analytics module.

Self-contained: this directory (`analytics/`) is the whole feature. It owns its
own prerequisites (the monitoring API it enables itself), so the base agent
stack does NOT depend on it — delete `analytics/` and nothing else breaks. The
project ids come straight from `demo-environment.yaml` (no coupling to the
agents' runtime-manifest).

Stands up the aggregate-analytics layer over the telemetry the demo already
emits — no new pipeline, no BigQuery, no manual report building:

  * log-based metrics on the AI project for near-real-time gauges
    (analytics/log_based_metrics.json)
  * a Cloud Monitoring dashboard over them (analytics/dashboard.json)
  * Apigee **custom reports** on the org (analytics/apigee_reports.json) —
    token/user/model/latency reports over the gateway's Data Collectors
    (dc_model, dc_user_id, dc_tokenCount, …), rendered by the Apigee console
    under Analytics → Custom Reports. Fully API-provisioned.

    python analytics/provision_analytics.py            # report + confirm before writing
    python analytics/provision_analytics.py --check    # read-only report, exit 1 on drift
    python analytics/provision_analytics.py --dry-run   # report only, write nothing
    python analytics/provision_analytics.py --apply     # write without prompting

Needs: gcloud (Cloud SDK), authed, with rights on the AI project AND the Apigee
org (same credential as apigee/provision/provision.py; APIGEE_TOKEN overrides).
This tool enables its own API (monitoring) — it is intentionally NOT in the
agents' infra.services, so analytics stays optional. Pure comparators are
unit-tested in tests/.
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

OK, DRIFT, MISSING = "OK", "DRIFT", "MISSING"
_SYMBOL = {OK: "✅", DRIFT: "⚠", MISSING: "❌"}

REQUIRED_APIS = ["monitoring.googleapis.com"]
DASHBOARD_TITLE = "AI Analytics — tokens · quota · latency · faults"

APIGEE = "https://apigee.googleapis.com/v1"
# The desired-vs-live surface of a custom report. Everything else on the live
# object is server-owned (name/id, timestamps, organization, legacy fields).
REPORT_FIELDS = ("chartType", "dimensions", "metrics", "filter")

# TLS context: prefer certifi's CA bundle (robust on macOS python.org builds,
# whose stdlib ssl has no local root certs); fall back to the system default.
try:
    import certifi

    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:  # certifi absent — use whatever the platform provides
    _SSL_CTX = ssl.create_default_context()

ART_DIR = Path(__file__).resolve().parent   # analytics/ — the JSON artifacts live beside this tool
REPO = ART_DIR.parent


def _f(status, kind, name, detail=""):
    return {"status": status, "kind": kind, "name": name, "detail": detail}


# ── Pure comparators (unit-tested, no I/O) ──────────────────────────────────────
def compare_apis(required: list[str], enabled: set | None) -> list:
    """Required analytics APIs vs enabled. enabled=None → listing failed, report
    that once rather than a false MISSING per API (same trap as provision_agents)."""
    if enabled is None:
        return [_f(DRIFT, "service", "listing", "could not list enabled services — state UNKNOWN")]
    return [_f(OK if s in enabled else MISSING, "service", s,
               "enabled ✓" if s in enabled else "not enabled")
            for s in required]


def compare_metrics(required: list[str], existing: set) -> list:
    """Log-based metric names required vs those that already exist."""
    return [_f(OK if n in existing else MISSING, "logMetric", n,
               "exists ✓" if n in existing else "not created")
            for n in required]


def _report_diff(desired: dict, live: dict) -> list[str]:
    """Field names where a live custom report diverges from the desired one.
    Metrics compare as an unordered set of (name, function) — the console
    reorders them; dimensions compare as an ordered list (drilldown order)."""
    diffs = []
    for field in REPORT_FIELDS:
        want, got = desired.get(field), live.get(field)
        if field == "metrics":
            norm = lambda ms: sorted((m.get("name"), m.get("function")) for m in ms or [])
            if norm(want) != norm(got):
                diffs.append(field)
        elif (want or None) != (got or None):    # absent == "" for filter etc.
            diffs.append(field)
    return diffs


def compare_reports(desired: list[dict], live: dict | None) -> list:
    """Desired custom reports (by displayName) vs the org's live ones.
    live=None → listing failed (no token / org unreachable) — one DRIFT row,
    not a false MISSING per report."""
    if live is None:
        return [_f(DRIFT, "apigeeReport", "listing",
                   "could not list the org's custom reports — state UNKNOWN")]
    out = []
    for d in desired:
        dn = d["displayName"]
        if dn not in live:
            out.append(_f(MISSING, "apigeeReport", dn, "not created"))
            continue
        diffs = _report_diff(d, live[dn])
        out.append(_f(DRIFT, "apigeeReport", dn, f"differs: {', '.join(diffs)}") if diffs
                   else _f(OK, "apigeeReport", dn, "matches ✓"))
    return out


def compare_addon(enabled: bool | None) -> list:
    """The org's Analytics add-on. Reports create fine without it, but render
    no data — surface that instead of shipping silently empty reports. Not
    auto-enabled (billing-affecting, org-level → manual/Terraform)."""
    if enabled is None:
        return [_f(DRIFT, "analyticsAddon", "org",
                   "could not read the org's addonsConfig — state UNKNOWN")]
    return [_f(OK, "analyticsAddon", "org", "enabled ✓") if enabled else
            _f(DRIFT, "analyticsAddon", "org",
               "Analytics add-on disabled — custom reports will show NO data; "
               "enable it on the org (console → Admin → Add-ons, or Terraform)")]


def build_findings(state: dict, required_apis: list[str], required_metrics: list[str],
                   desired_reports: list[dict]) -> list:
    """The whole analytics stack as findings, from an already-gathered `state`
    dict. Pure — the single source of the report AND the apply gate.

    state keys: enabled_apis(set|None), metrics(set), dashboard(bool),
    reports(dict[displayName→live]|None), analytics_addon(bool|None)."""
    out = compare_apis(required_apis, state.get("enabled_apis"))
    out.extend(compare_metrics(required_metrics, state.get("metrics", set())))
    out.append(_f(OK if state.get("dashboard") else MISSING, "dashboard", DASHBOARD_TITLE,
                  "exists ✓" if state.get("dashboard") else "not created"))
    out.extend(compare_addon(state.get("analytics_addon")))
    out.extend(compare_reports(desired_reports, state.get("reports")))
    return out


# ── gcloud shell ────────────────────────────────────────────────────────────────
def _run(cmd: list[str]):
    r = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                       env={**os.environ, "CLOUDSDK_CORE_DISABLE_PROMPTS": "1"})
    return r.returncode == 0, r


def _gcloud(args: list[str]):
    return _run(["gcloud", *args])


def _enabled_services(project: str) -> set | None:
    ok, r = _gcloud(["services", "list", "--enabled", f"--project={project}",
                     "--format=value(config.name)"])
    if not ok:
        return None
    return {ln.strip() for ln in r.stdout.splitlines() if ln.strip()}


def _load_metrics() -> list[dict]:
    return json.loads((ART_DIR / "log_based_metrics.json").read_text())


def _load_reports() -> list[dict]:
    return json.loads((ART_DIR / "apigee_reports.json").read_text())


# ── Apigee API layer (same credential model as apigee/provision/provision.py) ───
def _token() -> str:
    t = os.environ.get("APIGEE_TOKEN")
    if t:
        return t
    return subprocess.run(["gcloud", "auth", "print-access-token"],
                          capture_output=True, text=True, check=True).stdout.strip()


def _api(method: str, path: str, token: str, body: dict | None = None):
    req = urllib.request.Request(
        f"{APIGEE}{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}",
                 **({"Content-Type": "application/json"} if body is not None else {})})
    with urllib.request.urlopen(req, timeout=30, context=_SSL_CTX) as r:
        raw = r.read()
        return json.loads(raw) if raw.strip() else {}


def fetch_reports(org: str, token: str) -> dict | None:
    """{displayName: live report} for the org, or None if listing failed."""
    try:
        data = _api("GET", f"/organizations/{org}/reports", token) or {}
    except Exception:
        return None
    return {r["displayName"]: r for r in data.get("qualifier", []) if r.get("displayName")}


def addon_enabled_from_org(org_obj: dict) -> bool:
    """Whether Apigee Analytics is available on the org. EVALUATION orgs
    include analytics but carry NO analyticsConfig in addonsConfig (found
    live: eval org, working console analytics, empty addon block) — only
    paid orgs express it as an add-on."""
    if org_obj.get("billingType") == "EVALUATION":
        return True
    return bool(((org_obj.get("addonsConfig") or {}).get("analyticsConfig") or {}).get("enabled"))


def fetch_analytics_addon(org: str, token: str) -> bool | None:
    try:
        o = _api("GET", f"/organizations/{org}", token) or {}
    except Exception:
        return None
    return addon_enabled_from_org(o)


def _report_slug(display_name: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in display_name.lower()).strip("-")


def create_report(org: str, token: str, desired: dict):
    """POST the report. The server assigns the id (`name`); some org flavors
    reject a body without one, so retry once with a slug of the displayName."""
    body = {k: v for k, v in desired.items() if v is not None}
    try:
        return _api("POST", f"/organizations/{org}/reports", token, body)
    except urllib.error.HTTPError as e:
        if e.code == 400:
            return _api("POST", f"/organizations/{org}/reports", token,
                        {**body, "name": _report_slug(desired["displayName"])})
        raise


def update_report(org: str, token: str, live: dict, desired: dict):
    """PUT the desired fields under the live report's server-assigned name."""
    body = {"name": live["name"], **{k: v for k, v in desired.items() if v is not None}}
    return _api("PUT", f"/organizations/{org}/reports/{live['name']}", token, body)


# ── State gathering (I/O; the pure core above turns it into findings) ───────────
def _existing_metrics(project: str) -> set:
    ok, r = _gcloud(["logging", "metrics", "list", f"--project={project}",
                     "--format=value(name)"])
    return {ln.strip() for ln in r.stdout.splitlines() if ln.strip()} if ok else set()


def _dashboard_names(project: str) -> list[str]:
    """Resource names of EVERY dashboard whose displayName matches — matched in
    Python, not via a gcloud --filter (the title's em-dash/middots break filter
    quoting, so the filter silently found nothing → delete skipped → create left
    a duplicate). Returns all matches so a re-apply cleans up prior duplicates."""
    ok, r = _gcloud(["monitoring", "dashboards", "list", f"--project={project}", "--format=json"])
    if not ok or not r.stdout.strip():
        return []
    return [d["name"] for d in json.loads(r.stdout) if d.get("displayName") == DASHBOARD_TITLE]


def _dashboard_exists(project: str) -> bool:
    return bool(_dashboard_names(project))


def gather_state(project: str, org: str) -> dict:
    try:
        token = _token()
    except Exception:
        token = None
    return {
        "enabled_apis": _enabled_services(project),
        "metrics": _existing_metrics(project),
        "dashboard": _dashboard_exists(project),
        "reports": fetch_reports(org, token) if token else None,
        "analytics_addon": fetch_analytics_addon(org, token) if token else None,
    }


# ── Apply steps (idempotent) ────────────────────────────────────────────────────
def _step(label: str, ok_r) -> bool:
    """Print ✓/✗ for an apply step and, on failure, the last stderr line — so a
    failed gcloud call is never swallowed (the reason one metric silently
    didn't land the first time)."""
    ok, r = ok_r
    if ok:
        print(f"  ✓ {label}", flush=True)
        return True
    tail = (r.stderr or r.stdout or "").strip().splitlines()
    print(f"  ✗ {label}: {tail[-1] if tail else 'failed'}", flush=True)
    return False


def _api_step(label: str, fn) -> bool:
    """_step, for the Apigee REST calls (exceptions instead of returncodes)."""
    try:
        fn()
        print(f"  ✓ {label}", flush=True)
        return True
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:200] if hasattr(e, "read") else str(e)
        print(f"  ✗ {label}: HTTP {e.code} {detail}", flush=True)
    except Exception as e:  # noqa: BLE001 — one report failing must not stop the rest
        print(f"  ✗ {label}: {e}", flush=True)
    return False


def _apply(project: str, org: str, state: dict) -> None:
    # 1. APIs
    missing_apis = [s for s in REQUIRED_APIS if state.get("enabled_apis") is not None
                    and s not in state["enabled_apis"]]
    if missing_apis:
        _step(f"enable APIs {missing_apis}",
              _gcloud(["services", "enable", *missing_apis, f"--project={project}", "--quiet"]))

    # 2. Log-based metrics (create if missing, else update)
    for m in _load_metrics():
        name, cfg = m["name"], m["config"]
        verb = "update" if name in state.get("metrics", set()) else "create"
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(cfg, fh)
            cfg_path = fh.name
        try:
            _step(f"metric {name} ({verb})",
                  _gcloud(["logging", "metrics", verb, name, f"--config-from-file={cfg_path}",
                           f"--project={project}"]))
        finally:
            os.unlink(cfg_path)

    # 3. Monitoring dashboard — the API's update needs the live etag, so
    # (re)create instead: delete EVERY existing match (cleans up duplicates left
    # by earlier runs), then create one from the JSON. Fully repo-defined, so
    # replace is safe + idempotent.
    cfg = f"--config-from-file={ART_DIR / 'dashboard.json'}"
    for name in _dashboard_names(project):
        _step(f"delete existing dashboard {name.rsplit('/', 1)[-1]}",
              _gcloud(["monitoring", "dashboards", "delete", name, "--quiet",
                       f"--project={project}"]))
    _step("create Monitoring dashboard",
          _gcloud(["monitoring", "dashboards", "create", cfg, f"--project={project}"]))

    # 4. Apigee custom reports — POST missing, PUT drifted (keyed by
    # displayName; the id is server-assigned). The Analytics add-on itself is
    # NEVER touched here (billing-affecting) — the finding carries the hint.
    try:
        token = _token()
    except Exception as e:
        print(f"  ✗ apigee reports: no token ({e}) — skipped", flush=True)
        return
    live = state.get("reports")
    if live is None:
        live = fetch_reports(org, token)
    if live is None:
        print(f"  ✗ apigee reports: cannot list org {org} — skipped", flush=True)
        return
    for d in _load_reports():
        dn = d["displayName"]
        if dn not in live:
            _api_step(f"report {dn} (create)", lambda d=d: create_report(org, token, d))
        elif _report_diff(d, live[dn]):
            _api_step(f"report {dn} (update)",
                      lambda d=d, l=live[dn]: update_report(org, token, l, d))


# ── Report / write-mode ─────────────────────────────────────────────────────────
def should_write(args, n_changes: int) -> bool:
    # --apply always RECONCILES content, even at 0 drift: the metric/dashboard
    # checks only see whether a resource EXISTS, not whether its definition (a
    # metric config, the dashboard JSON) still matches the repo. The apply steps
    # are idempotent (metrics = create/update, dashboard = delete+recreate,
    # reports = diffed create/update), so re-asserting is how edits land.
    if getattr(args, "dry_run", False):
        return False
    if getattr(args, "apply", False):
        return True
    if n_changes == 0:
        return False
    if not sys.stdin.isatty():
        print("\n(no TTY — treating as --dry-run. Pass --apply to write non-interactively.)")
        return False
    return input(f"\nApply {n_changes} change(s)? [y/N] ").strip().lower() in ("y", "yes")


def report(findings) -> int:
    for f in findings:
        line = f"{_SYMBOL[f['status']]} {f['status']:<7} {f['kind']}: {f['name']}"
        if f["detail"]:
            line += f" — {f['detail']}"
        print(line)
    n_bad = sum(1 for f in findings if f["status"] != OK)
    print(f"\n{len(findings) - n_bad}/{len(findings)} OK" + (f", {n_bad} to reconcile" if n_bad else ""))
    return n_bad


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Analytics provisioning (optional module)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true", help="read-only report (exit 1 on drift)")
    g.add_argument("--dry-run", action="store_true", help="report only; write nothing")
    g.add_argument("--apply", action="store_true", help="write without prompting")
    args = ap.parse_args(argv)

    sys.path.insert(0, str(REPO))
    import demo_env

    # self-contained: both ids come straight from demo-environment.yaml (the one
    # place naming the projects) — no coupling to the agents manifest. The
    # Apigee org name == the apigee project id.
    env = demo_env.load_env()
    project = os.environ.get("PROJECT") or env["projects"]["ai"]
    org = os.environ.get("APIGEE_ORG") or env["projects"]["apigee"]
    print(f"# analytics — AI project {project}, Apigee org {org}\n")

    state = gather_state(project, org)
    findings = build_findings(state, REQUIRED_APIS,
                              [x["name"] for x in _load_metrics()], _load_reports())
    n_bad = report(findings)

    if args.check:
        return 1 if n_bad else 0
    if should_write(args, n_bad):
        print()
        _apply(project, org, state)
        print("\ndone. Dashboards: Cloud Monitoring (AI project) + Apigee console → "
              "Analytics → Custom Reports (data appears ~5–10 min after traffic).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
