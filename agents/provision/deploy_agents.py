#!/usr/bin/env python3
"""Central agent deploy tool — manifest-driven, AGENT side.

Replaces the four per-agent deploy.py scripts with one tool driven by
agents/runtime-manifest.yaml. Same scope/mode CLI as the rest of the repo:

    deploy_agents.py --check [--all | <agent> ...]   read-only: live engines vs
                                                     the manifest (stale
                                                     generations, SA match)
    deploy_agents.py --all | <agent> ...             deploy (specialists before
                                                     supervisor); confirms first
    deploy_agents.py --apply --all | <agent> ...     deploy without prompting
    deploy_agents.py --cleanup [--all | <agent> ...] delete STALE engines (all
                                                     but the newest per display
                                                     name); lists, then confirms
    deploy_agents.py --sync-registry                 retarget stale Agent
                                                     Registry entries at the
                                                     newest engines (confirms);
                                                     the supervisor heals from
                                                     the registry on its TTL

Agent names = manifest keys (supervisor, order, product, customer).

ISOLATION: each agent deploys in a SUBPROCESS (this file re-invoked with the
internal --worker flag, cwd = the agent's dir). One process cannot deploy two
agents: every agent has its own `app` package (module-name collision) and its
own .env (same variable names would leak between agents).

Needs the deploy-host venv (agents/provision/requirements-deploy.txt) + gcloud.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent          # agents/provision
AGENTS_DIR = HERE.parent                        # agents/
MANIFEST = AGENTS_DIR / "runtime-manifest.yaml"

OK, DRIFT, MISSING = "OK", "DRIFT", "MISSING"
_SYMBOL = {OK: "✅", DRIFT: "⚠", MISSING: "❌"}

try:
    import certifi

    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL_CTX = ssl.create_default_context()

# Env vars deploy passes through to the engine when set locally (union of what
# the per-agent deploy.py scripts passed; unset vars are simply skipped).
PASSTHROUGH = [
    "DISPLAY_NAME",             # names the engine in agent-ai-logs entries
    "GEMINI_MODEL",
    "GOOGLE_CLOUD_LOCATION",
    "APIGEE_LLM_PROXY_URL",
    "APIGEE_API_KEY",
    "APIGEE_LLM_API_KEY",       # legacy fallback
    "APIGEE_LLM_INSECURE_TLS",
    "APIGEE_MCP_URL",
    "APIGEE_MCP_TOKEN",
    "APIGEE_MCP_API_KEY",       # legacy fallback
    # supervisor-only knobs (unset for specialists, so skipped there)
    "SUBAGENT_REFRESH_TTL_SECONDS",
    "DISABLE_RUNTIME_SUBAGENT_REFRESH",
    "SUBAGENT_ALLOWLIST",
    "SUBAGENT_ATTACH_ALL",
    "ORDER_AGENT_NAME",
    "PRODUCT_AGENT_NAME",
    "CUSTOMER_AGENT_NAME",
    "ACL_ENABLED",
    "ACL_BACKEND",
    "ACL_FAKE_JSON",
    "ACL_ALLOW_ALL",
]


# ── Manifest + selection ──────────────────────────────────────────────────────
def load_agents() -> dict:
    return _load_manifest_env()["agents"]


def _load_manifest_env() -> dict:
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    import demo_env

    return demo_env.load_manifest(MANIFEST)


def select(agents: dict, args_names: list[str]) -> dict:
    if args_names == ["--all"] or args_names == []:
        chosen = dict(agents)
    else:
        unknown = [n for n in args_names if n not in agents]
        if unknown:
            raise SystemExit(f"unknown agent(s): {', '.join(unknown)} — choose from: {', '.join(agents)}")
        chosen = {n: agents[n] for n in args_names}
    # deploy order: specialists first, supervisor last (it discovers them)
    return dict(sorted(chosen.items(), key=lambda kv: kv[0] == "supervisor"))


def display_name(agent: str, spec: dict) -> str:
    """DISPLAY_NAME from the agent's .env (its config source of truth), with
    the same default the old deploy.py had."""
    try:
        from dotenv import dotenv_values

        v = dotenv_values(AGENTS_DIR / spec["dir"] / ".env").get("DISPLAY_NAME")
        if v:
            return v
    except Exception:
        pass
    return f"a2a-{agent}-agent"


# ── Pure helpers (unit-tested) ────────────────────────────────────────────────
def group_engines(engines: list[dict], names: dict) -> dict:
    """{agent: [its engines, newest first]} — matched by displayName."""
    by_display: dict[str, list] = {}
    for e in engines:
        by_display.setdefault(e.get("displayName", ""), []).append(e)
    out = {}
    for agent, dn in names.items():
        mine = sorted(by_display.get(dn, []), key=lambda e: e.get("createTime", ""), reverse=True)
        out[agent] = mine
    return out


def derive_cleanup(groups: dict) -> list[tuple[str, dict, list[dict]]]:
    """[(agent, keep_newest, [stale...])] for agents with >1 engine."""
    return [(a, es[0], es[1:]) for a, es in groups.items() if len(es) > 1]


# ── Registry sync (pure parts) ────────────────────────────────────────────────
# A registry entry carries the engine's A2A URL in protocols[].interfaces[].url
# (and, when a full card is stored, card.content.url). The supervisor routes to
# WHATEVER those URLs say — so after a specialist redeploy the entry must be
# retargeted at the newest engine or delegation keeps hitting the dead one.
# [\w-]+ not \d+: ids are numeric today, but nothing guarantees that — a
# format change must not silently turn every entry into "unparseable".
_ENGINE_ID_RE = re.compile(r"reasoningEngines/([\w-]+)")


def _entry_urls(detail: dict):
    """Yield (container, key) for every URL field in a registry entry."""
    card_content = (detail.get("card") or {}).get("content") or {}
    if card_content.get("url"):
        yield card_content, "url"
    for proto in detail.get("protocols") or []:
        for iface in proto.get("interfaces") or []:
            if iface.get("url"):
                yield iface, "url"


def registry_engine_ids(detail: dict) -> list[str]:
    """ALL engine ids referenced by a registry entry's URL fields (card +
    every protocols interface), deduped in order. MUST be all of them: seen
    live, auto-registration updated card.content.url but left a STALE
    protocols url — ADK's runtime resolution reads protocols FIRST, so the
    supervisor kept routing to a deleted engine while a first-match check
    declared the entry healthy ("nothing to sync")."""
    ids: list[str] = []
    for container, key in _entry_urls(detail):
        m = _ENGINE_ID_RE.search(container[key])
        if m and m.group(1) not in ids:
            ids.append(m.group(1))
    return ids


def registry_engine_id(detail: dict) -> str | None:
    """First engine id (display convenience; drift checks use the full list)."""
    ids = registry_engine_ids(detail)
    return ids[0] if ids else None


def retarget_entry(detail: dict, new_id: str) -> tuple[dict, bool]:
    """Copy of the entry with every engine URL repointed at new_id."""
    out = json.loads(json.dumps(detail))
    changed = False
    for container, key in _entry_urls(out):
        updated = _ENGINE_ID_RE.sub(f"reasoningEngines/{new_id}", container[key])
        if updated != container[key]:
            container[key] = updated
            changed = True
    return out, changed


def registry_findings(groups: dict, entries: dict) -> list[dict]:
    """Per specialist: does its registry entry point at the NEWEST engine?
    entries = {registry displayName: entry detail}. Supervisor exempt (it is
    the registry CONSUMER; nothing routes to it via the registry)."""
    out = []
    for agent, engines in groups.items():
        if agent == "supervisor" or not engines:
            continue
        display = f"{agent}_agent"
        detail = entries.get(display)
        if detail is None:
            out.append({"status": DRIFT, "agent": agent,
                        "detail": f"no Agent Registry entry '{display}' — the supervisor"
                                  " cannot discover this specialist"})
            continue
        newest = engine_id(engines[0])
        ids = registry_engine_ids(detail)
        if ids == [newest]:
            out.append({"status": OK, "agent": agent,
                        "detail": f"registry '{display}' → engine {newest} ✓"})
        else:
            out.append({"status": DRIFT, "agent": agent,
                        "detail": f"registry '{display}' → engine(s) {ids or '?'} but newest"
                                  f" is {newest} — run --sync-registry"})
    return out


def engine_id(e: dict) -> str:
    return e.get("name", "").rsplit("/", 1)[-1]


def find_service_account(obj):
    """Recursive search for a serviceAccount field anywhere in the engine
    resource (its exact location varies by API version)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "serviceAccount" and isinstance(v, str):
                return v
            found = find_service_account(v)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = find_service_account(v)
            if found:
                return found
    return None


def check_findings(groups: dict, agents: dict, project: str,
                   card_names: dict | None = None) -> list[dict]:
    """card_names: {agent: A2A card name | 'HTTP <code>' | None} for the NEWEST
    engine — identity check (an engine that booted a sibling's pickle answers
    with the WRONG name, or with no A2A surface at all; both seen live)."""
    out = []
    for agent, engines in groups.items():
        spec = agents[agent]
        if not engines:
            out.append({"status": MISSING, "agent": agent, "detail": "no engine deployed"})
            continue
        newest = engines[0]
        bits = [f"engine {engine_id(newest)} (created {newest.get('createTime', '?')[:16]})"]
        sa = find_service_account(newest)
        want_sa = f"{spec['sa']}@{project}.iam.gserviceaccount.com"
        status = OK
        if sa and sa != want_sa:
            status = DRIFT
            bits.append(f"runs as {sa}, manifest declares {want_sa}")
        elif sa:
            bits.append(f"SA {spec['sa']} ✓")
        if card_names is not None and agent != "supervisor":   # AdkApp has no card
            got, want = card_names.get(agent), f"{agent}_agent"
            if got == want:
                bits.append(f"card identity {want} ✓")
            else:
                status = DRIFT
                bits.append(f"card identity MISMATCH: got {got!r}, want {want!r}"
                            " (wrong pickle? no A2A surface?)")
        if len(engines) > 1:
            status = DRIFT
            stale = ", ".join(engine_id(e) for e in engines[1:])
            bits.append(f"{len(engines) - 1} STALE generation(s): {stale} — run --cleanup")
        out.append({"status": status, "agent": agent, "detail": "; ".join(bits)})
    return out


# ── REST layer ────────────────────────────────────────────────────────────────
def _token() -> str:
    return subprocess.run(["gcloud", "auth", "print-access-token"],
                          capture_output=True, text=True, check=True).stdout.strip()


def _request(method: str, url: str, token: str, body: dict | None = None):
    headers = {"Authorization": f"Bearer {token}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60, context=_SSL_CTX) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        # surface the API's error body — a bare "404 Not Found" hides whether
        # it's a bad updateMask field or an unsupported method on the resource
        body = ""
        try:
            body = e.read().decode(errors="replace")
        except Exception:  # noqa: BLE001
            pass
        raise urllib.error.HTTPError(
            e.url, e.code, f"{e.reason} — {method} {url} :: {body[:400]}", e.headers, None) from None


def list_engines(project: str, region: str, token: str) -> list[dict]:
    base = f"https://{region}-aiplatform.googleapis.com/v1beta1/projects/{project}/locations/{region}/reasoningEngines"
    engines, page = [], ""
    while True:
        data = _request("GET", base + (f"?pageToken={page}" if page else ""), token)
        engines += data.get("reasoningEngines", [])
        page = data.get("nextPageToken", "")
        if not page:
            return engines


def delete_engine(name: str, region: str, token: str):
    url = f"https://{region}-aiplatform.googleapis.com/v1beta1/{name}?force=true"
    return _request("DELETE", url, token)


def fetch_card_name(engine_name: str, region: str, token: str) -> str | None:
    """The engine's A2A card identity, or 'HTTP <code>' when the card endpoint
    errors (e.g. the engine serves no A2A surface at all)."""
    url = f"https://{region}-aiplatform.googleapis.com/v1beta1/{engine_name}/a2a/v1/card"
    try:
        return (_request("GET", url, token) or {}).get("name")
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}"
    except Exception:
        return None


REG_BASE = "https://agentregistry.googleapis.com/v1alpha"


def fetch_registry_entries(project: str, location: str, token: str) -> dict:
    """{displayName: full entry detail} for every registered agent."""
    base = f"{REG_BASE}/projects/{project}/locations/{location}/agents"
    listing = _request("GET", base, token) or {}
    chosen = {}  # dn -> (recency-key, summary) — pick the NEWEST entry per name
    for a in listing.get("agents", []):
        dn, name = a.get("displayName"), a.get("name")
        if not dn or not name:
            continue
        key = a.get("updateTime") or a.get("createTime") or ""
        if dn not in chosen or key >= chosen[dn][0]:
            chosen[dn] = (key, a)
    out = {}
    for dn, (_, a) in chosen.items():
        try:
            out[dn] = _request("GET", f"{REG_BASE}/{a['name']}", token) or a
        except Exception:
            out[dn] = a  # summary is enough for URL extraction in most cases
    return out


def patch_registry_entry(detail: dict, token: str):
    """PATCH the entry's URL-bearing fields (protocols + card when present)."""
    mask = "protocols" + (",card" if detail.get("card") else "")
    body = {"protocols": detail.get("protocols") or []}
    if detail.get("card"):
        body["card"] = detail["card"]
    return _request("PATCH", f"{REG_BASE}/{detail['name']}?updateMask={mask}", token, body)


# ── Modes ─────────────────────────────────────────────────────────────────────
def should_write(args, n: int, verb: str) -> bool:
    if n == 0:
        return False
    if getattr(args, "apply", False):
        return True
    if not sys.stdin.isatty():
        print(f"\n(no TTY — nothing {verb}ed. Pass --apply to run non-interactively.)")
        return False
    return input(f"\n{verb} {n} item(s)? [y/N] ").strip().lower() in ("y", "yes")


def _env(project=None, region=None):
    project = project or os.environ.get("PROJECT_ID")
    region = region or os.environ.get("REGION", "us-central1")
    return project, region


def preflight_adc(project: str, staging_bucket: str, _lookup=None) -> bool:
    """Engine deploys authenticate via Application Default Credentials (the
    GEAP SDK) — every phase before this one uses the gcloud CLI credential,
    so a stale or wrong ADC surfaces HERE first. Seen live: all four deploys
    404ing `The requested project was not found` from storage(.mtls)
    .googleapis.com while every gcloud call in the run succeeded — the ADC was
    an older login with no access to the fresh project, so the SDK's
    staging-bucket lookup came back empty and it tried to CREATE the bucket.
    One clear failure up front beats four identical worker log tails."""
    if _lookup is None:
        def _lookup(bucket):
            import google.auth
            from google.cloud import storage

            creds, _ = google.auth.default()
            return storage.Client(project=project, credentials=creds).lookup_bucket(bucket)
    try:
        if _lookup(staging_bucket) is not None:
            return True
        problem = (f"ADC cannot see gs://{staging_bucket} — either this ADC identity"
                   f" has no access to {project}, or the bucket was never provisioned")
    except Exception as e:  # noqa: BLE001 — any auth/transport failure = same fix
        problem = f"ADC storage check failed: {type(e).__name__}: {e}"
    acct = ""
    try:
        acct = subprocess.run(["gcloud", "config", "get-value", "account"],
                              capture_output=True, text=True).stdout.strip()
    except Exception:  # noqa: BLE001 — the hint is optional
        pass
    print(f"""\
ERROR: Application Default Credentials can't reach the staging bucket.
  {problem}
  Engine deploys run on ADC (the GEAP SDK), NOT your `gcloud auth login`
  credential — everything before this phase used gcloud, so a stale ADC from
  an earlier login bites here first. Fix (same account as gcloud{f": {acct}" if acct else ""}):
      gcloud auth application-default login
      gcloud auth application-default set-quota-project {project}
  If the error mentions storage.mtls.googleapis.com, the client also picked up
  corp device certificates; if it persists after re-login:
      gcloud config set context_aware/use_client_certificate false
  (Bucket never created? Run: python agents/provision/provision_agents.py)""",
          file=sys.stderr)
    return False


def cmd_check(args, agents, project_manifest):
    project, region = _env()
    project = project or project_manifest
    token = _token()
    names = {a: display_name(a, s) for a, s in agents.items()}
    print(f"# agents — project {project}, region {region}\n")
    groups = group_engines(list_engines(project, region, token), names)
    card_names = {a: fetch_card_name(es[0]["name"], region, token)
                  for a, es in groups.items() if es and a != "supervisor"}
    findings = check_findings(groups, agents, project_manifest, card_names)
    try:
        entries = fetch_registry_entries(project, region, token)
        findings += registry_findings(groups, entries)
    except Exception as e:  # noqa: BLE001 — report honestly, never fake-OK
        findings.append({"status": DRIFT, "agent": "registry",
                         "detail": f"could not read Agent Registry — state UNKNOWN ({e})"})
    n_bad = 0
    for f in findings:
        print(f"{_SYMBOL[f['status']]} {f['status']:<7} agent: {f['agent']} — {f['detail']}", flush=True)
        n_bad += f["status"] != OK
    print(f"\n{len(findings) - n_bad}/{len(findings)} OK" + (f", {n_bad} to look at" if n_bad else ""))
    return 1 if n_bad else 0


def cmd_sync_registry(args, agents, project_manifest):
    """PRUNE stale Agent Registry entries so exactly one per agent survives —
    the one pointing at the newest LIVE engine.

    GEAP auto-registers a fresh entry on every engine deploy, and v1alpha has
    no PatchAgent (retarget-by-PATCH 404s), so 'sync' = DELETE the duplicates.
    The supervisor already prefers the newest entry at runtime; pruning keeps the
    registry honest and stops a later --cleanup from stranding a picker on a
    deleted engine (the A2A 403 class)."""
    project, region = _env()
    project = project or project_manifest
    token = _token()
    names = {a: display_name(a, s) for a, s in agents.items()}
    live = group_engines(list_engines(project, region, token), names)   # {agent: [engines newest-first]}
    live_ids = {engine_id(e) for v in live.values() for e in v}
    # registry entries are keyed by the CARD name (e.g. "product_agent"), not
    # the ENGINE display name ("a2a-product-agent") — so map by "{agent}_agent"
    newest = {f"{a}_agent": engine_id(v[0]) for a, v in live.items() if v}   # cardName -> newest live engine id
    listing = _request("GET", f"{REG_BASE}/projects/{project}/locations/{region}/agents", token) or {}
    by_display: dict[str, list] = {}
    for a in listing.get("agents", []):
        dn, name = a.get("displayName"), a.get("name")
        if dn and name:
            by_display.setdefault(dn, []).append(a)

    print(f"# registry prune — project {project}, region {region}\n")
    stale = []          # (displayName, summary) to delete
    for dn in sorted(by_display):
        entries = sorted(by_display[dn],
                         key=lambda a: a.get("updateTime") or a.get("createTime") or "", reverse=True)
        target = newest.get(dn)
        at_live = []    # entries whose engine is the newest LIVE one
        for a in entries:
            try:
                det = _request("GET", f"{REG_BASE}/{a['name']}", token) or a
            except Exception:  # noqa: BLE001
                det = a
            a["_ids"] = registry_engine_ids(det)
            if target and target in a["_ids"]:
                at_live.append(a)
        keep = at_live[0] if at_live else entries[0]   # prefer live; else newest by time
        if target and not at_live:
            print(f"  ⚠ {dn:14} NO entry points at the newest live engine {target}"
                  f" — REDEPLOY {dn.replace('_agent','')} to re-register a fresh entry")
        for a in entries:
            if a is not keep:
                stale.append((dn, a))
        keptid = keep.get("name", "").rsplit("/", 1)[-1]
        dead = "" if not target else "".join(
            f" [{i}{'✓' if i in live_ids else '✗DEAD'}]" for i in keep.get("_ids", []))
        print(f"  {dn:16} {len(entries)} entr{'y' if len(entries) == 1 else 'ies'} — keep {keptid}{dead}"
              + (f", delete {sum(1 for d, _ in stale if d == dn)}" if any(d == dn for d, _ in stale) else ""))
    if not stale:
        print("\nnothing to prune — one entry per agent.")
        return 0
    if not should_write(args, len(stale), "Delete"):
        print(f"nothing deleted — {len(stale)} stale entry(ies).")
        return 1
    for dn, a in stale:
        rid = a["name"].rsplit("/", 1)[-1]
        print(f"  deleting stale {dn} entry {rid} ...", flush=True)
        try:
            _request("DELETE", f"{REG_BASE}/{a['name']}", token)
        except Exception as e:  # noqa: BLE001 — keep going
            print(f"    ✗ {dn}: delete failed — {e}", flush=True)
    print("done. One entry per agent → the supervisor resolves the newest engine.")
    return 0


def cmd_list_registry(args, agents, project_manifest):
    """Dump every Agent Registry entry with its engine ids marked LIVE/DEAD, and
    the live engines — reveals duplicates and entries pointing at deleted engines
    (the A2A-403 root cause: the supervisor resolves engine ids ONLY from here)."""
    project, region = _env()
    project = project or project_manifest
    token = _token()
    names = {a: display_name(a, s) for a, s in agents.items()}
    live = group_engines(list_engines(project, region, token), names)   # {agent: [engines newest-first]}
    live_ids = {engine_id(e) for v in live.values() for e in v}
    print(f"# registry vs live engines — project {project}, region {region}\n")
    print("live engines (newest first):")
    for a, v in live.items():
        print(f"  {a:10} {[engine_id(e) for e in v] or '(none)'}")
    print("\nregistry entries:")
    listing = _request("GET", f"{REG_BASE}/projects/{project}/locations/{region}/agents", token) or {}
    rows = sorted(listing.get("agents", []),
                  key=lambda a: (a.get("displayName") or "", a.get("createTime") or ""))
    if not rows:
        print("  (none)")
        return 0
    for a in rows:
        name = a.get("name") or ""
        try:
            det = _request("GET", f"{REG_BASE}/{name}", token) or a
        except Exception:  # noqa: BLE001
            det = a
        marked = " ".join(f"{i}{'✓LIVE' if i in live_ids else '✗DEAD'}"
                          for i in registry_engine_ids(det)) or "(no engine url)"
        print(f"  {str(a.get('displayName')):16} id={name.rsplit('/', 1)[-1]:44}"
              f" created={(a.get('createTime') or '')[:19]}  {marked}")
    return 0


def cmd_cleanup(args, agents, project_manifest):
    project, region = _env()
    project = project or project_manifest
    token = _token()
    names = {a: display_name(a, s) for a, s in agents.items()}
    groups = group_engines(list_engines(project, region, token), names)
    plan = derive_cleanup(groups)
    if not plan:
        print("nothing stale — at most one engine per agent.")
        print("note: if the supervisor was redeployed in the same batch, its baked\n"
          "      sub-agent URIs may still point at the engines just deleted —\n"
          "      the runtime heal repoints them within one refresh TTL (~5 min).\n"
          "      Turns during that window fail with A2A 403 and self-recover.")
        return 0
    total = 0
    print("stale engines (newest per agent is KEPT):")
    for agent, keep, stale in plan:
        print(f"  {agent}: keeping {engine_id(keep)} (created {keep.get('createTime', '?')[:16]})")
        for e in stale:
            print(f"    DELETE {engine_id(e)} (created {e.get('createTime', '?')[:16]})")
            total += 1
    if not should_write(args, total, "Delete"):
        print(f"nothing deleted — {total} stale engine(s) remain.")
        return 1
    for agent, _keep, stale in plan:
        for e in stale:
            print(f"  deleting {agent} engine {engine_id(e)} ...", flush=True)
            delete_engine(e["name"], region, token)
    print("done. (deletions run as LROs; engines disappear shortly)")
    return 0


def _run_worker(agent: str, spec: dict, log_dir: Path) -> tuple[str, int, Path]:
    log = log_dir / f"{agent}.log"
    with open(log, "w") as fh:
        r = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--worker", agent],
            cwd=AGENTS_DIR / spec["dir"], stdout=fh, stderr=subprocess.STDOUT,
        )
    return agent, r.returncode, log


def _report_worker(agent: str, rc: int, log: Path) -> bool:
    text = log.read_text()
    deployed = next((ln for ln in text.splitlines() if ln.startswith("Deployed:")), "")
    if rc == 0:
        print(f"  ✓ {agent}: {deployed or 'done'}  (log: {log})", flush=True)
        return True
    tail = "\n      | ".join(text.splitlines()[-15:])
    print(f"  ✗ {agent} FAILED (log: {log}). Last lines:\n      | {tail}", file=sys.stderr)
    return False


def cmd_deploy(args, agents, project_manifest):
    # ALL agents deploy in parallel — there is no ordering dependency: the
    # supervisor discovers specialists via Agent Registry at RUNTIME (by
    # display name, TTL-refreshed), never via the just-created engines. The
    # registry entries are updated after deploys either way.
    from concurrent.futures import ThreadPoolExecutor

    subprocess.run([str(AGENTS_DIR / "sync_common.sh")], check=True)
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))   # repo root → demo_env
    import demo_env
    log_dir = demo_env.deploy_log_dir("agents")   # repo-root deploy-logs/agents/

    manifest = _load_manifest_env()
    project = os.environ.get("PROJECT_ID") or manifest["project"]
    staging_bucket = (os.environ.get("STAGING_BUCKET")
                      or manifest["infra"].get("staging_bucket"))
    if project and staging_bucket and not preflight_adc(project, staging_bucket):
        return 2

    print(f"will deploy (each creates a NEW engine): {', '.join(agents)}")
    if not should_write(args, len(agents), "Deploy"):
        print("nothing deployed.")
        return 1

    print(f"\n>>> deploying {len(agents)} agent(s) in parallel"
          f" (logs: {log_dir}/<agent>.log) ...", flush=True)
    ok = True
    with ThreadPoolExecutor(max_workers=len(agents)) as pool:
        futures = [pool.submit(_run_worker, a, s, log_dir) for a, s in agents.items()]
        for fut in futures:
            agent, rc, log = fut.result()
            ok = _report_worker(agent, rc, log) and ok
    if ok:
        print("\nall deployed. Next: update Agent Registry entries for redeployed"
              " specialists, point the BFF at the new supervisor, then run --check"
              " / --cleanup.")
    else:
        print("\nfailure(s) above — rerun just those agents by name.", file=sys.stderr)
    return 0 if ok else 1


# ── Worker: deploy ONE agent (runs with cwd = the agent's dir) ───────────────
def worker(agent: str) -> int:
    sys.path.insert(0, os.getcwd())                 # `import app` from the agent dir
    sys.path.insert(0, str(AGENTS_DIR))             # deploy_common
    import vertexai
    from dotenv import load_dotenv
    from vertexai import agent_engines

    from deploy_common import (
        apigee_secret_env_vars,
        build_env_vars,
        load_requirements,
        patch_messagetojson_for_pydantic,
        preflight_identity,
        psc_interface_config,
        telemetry_env_vars,
    )

    # Explicit path: bare load_dotenv() searches from THIS file's directory
    # (agents/provision/), not the cwd — the worker runs with cwd = the agent's
    # dir and must load THAT agent's .env.
    load_dotenv(dotenv_path=Path.cwd() / ".env")
    manifest = _load_manifest_env()
    spec = manifest["agents"][agent]
    # Defaults come from demo-environment.yaml (via the manifest tokens) — the
    # per-agent .env files no longer need project/bucket plumbing entries.
    project = os.environ.get("PROJECT_ID") or manifest["project"]
    region = os.environ.get("REGION") or manifest["infra"].get("region", "us-central1")
    staging_bucket = (os.environ.get("STAGING_BUCKET")
                      or manifest["infra"].get("staging_bucket"))
    if not project or not staging_bucket:
        print("ERROR: PROJECT_ID and STAGING_BUCKET env vars are required.", file=sys.stderr)
        return 2
    os.environ.setdefault("PROJECT_ID", project)          # deploy_common reads these
    os.environ.setdefault("STAGING_BUCKET", staging_bucket)
    # Manifest-carried runtime env (engine_env + this agent's env:) becomes the
    # DEFAULT layer — anything set locally (.env / exported) still overrides.
    # setdefault into os.environ so the HOST-side app import (build_apigee_model
    # runs at import) sees the same values the engine will.
    manifest_env = {**(manifest.get("engine_env") or {}), **(spec.get("env") or {})}
    for k, v in manifest_env.items():
        os.environ.setdefault(k, str(v))
    os.environ.setdefault("APIGEE_API_KEY_SECRET", spec["secret"])
    disp = os.environ.get("DISPLAY_NAME", f"a2a-{agent}-agent")
    description = os.environ.get("DESCRIPTION", spec.get("description", disp))
    # Regional registry for the HOST-side import too (the supervisor discovers
    # specialists at import; "global" returns none of the regional entries).
    os.environ.setdefault("GOOGLE_CLOUD_LOCATION", region)
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", project)

    vertexai.init(project=project, location=region, staging_bucket=f"gs://{staging_bucket}")

    from app.agent_runtime_app import agent_runtime  # noqa: E402  (after init)

    env_vars = telemetry_env_vars(disp)
    env_vars.update(manifest_env)                     # manifest layer
    env_vars.update(build_env_vars(PASSTHROUGH))      # local .env/exports win
    # Agent Registry is REGIONAL: without this, configure_genai_env() defaults
    # GOOGLE_CLOUD_LOCATION to "global" on the engine and the supervisor's TTL
    # refresh queries the GLOBAL registry — dropping the regional specialists
    # and picking up Google's built-in global agents instead (seen live: the
    # old deploy_all_a2a.sh exported this; the tool must too).
    env_vars.setdefault("GOOGLE_CLOUD_LOCATION", region)
    if agent == "supervisor":
        # registry discovery already ran at import; skip the runtime re-check
        # on workers (cloud workers re-import app/agent.py).
        env_vars["SKIP_REGISTRY_DISCOVERY"] = "1"
    env_vars.update(apigee_secret_env_vars())

    patch_messagetojson_for_pydantic()
    runner_sa = (os.environ.get("AGENT_RUNNER_SA")
                 or f"{spec['sa']}@{project}.iam.gserviceaccount.com")
    if not preflight_identity(runner_sa, project, os.environ.get("APIGEE_API_KEY_SECRET")):
        return 2

    print(f"Deploying {disp} to {project}/{region}...", flush=True)
    if runner_sa:
        print(f"  Running as service account: {runner_sa}", flush=True)
    psc_config = psc_interface_config()
    if psc_config:
        print(f"  PSC-I egress via network attachment: {psc_config.network_attachment}", flush=True)
    remote = agent_engines.create(
        agent_engine=agent_runtime,
        requirements=load_requirements(),
        extra_packages=["./app"],
        display_name=disp,
        description=description,
        env_vars=env_vars or None,
        service_account=runner_sa,
        psc_interface_config=psc_config,
        # CRITICAL for parallel deploys: without this the SDK stages every
        # engine's artifacts at the SAME bucket path (agent_engine/…pkl) and
        # concurrent workers race — an engine can boot a SIBLING's pickle
        # (seen live: the supervisor booted a specialist graph and crashed on
        # its MCP recipe; specialists could silently swap identities).
        gcs_dir_name=f"agent_engine_{agent}",
    )
    print(f"Deployed: {remote.resource_name}", flush=True)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Central agent deploy (manifest-driven)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="read-only: live engines vs manifest")
    mode.add_argument("--cleanup", action="store_true", help="delete stale engine generations (confirms)")
    mode.add_argument("--sync-registry", action="store_true",
                      help="prune duplicate Agent Registry entries, keeping the newest per agent (confirms)")
    mode.add_argument("--list-registry", action="store_true",
                      help="dump every Agent Registry entry (displayName, id, engine ids)")
    mode.add_argument("--worker", metavar="AGENT", help=argparse.SUPPRESS)  # internal
    ap.add_argument("--all", action="store_true", help="every agent in the manifest")
    ap.add_argument("--apply", action="store_true", help="write without prompting")
    ap.add_argument("names", nargs="*", help="agent names (manifest keys), or use --all")
    args = ap.parse_args(argv)

    if args.worker:
        return worker(args.worker)

    names = [] if args.all else args.names   # select() treats [] as all
    manifest = _load_manifest_env()
    agents = select(manifest["agents"], names)
    project_manifest = manifest["project"]
    if args.check:
        return cmd_check(args, agents, project_manifest)
    if args.cleanup:
        return cmd_cleanup(args, agents, project_manifest)
    if args.sync_registry:
        return cmd_sync_registry(args, agents, project_manifest)
    if args.list_registry:
        return cmd_list_registry(args, agents, project_manifest)
    if not args.names and not args.all:
        ap.print_help()
        return 2
    return cmd_deploy(args, agents, project_manifest)


if __name__ == "__main__":
    raise SystemExit(main())
