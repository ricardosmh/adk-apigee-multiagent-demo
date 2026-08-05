#!/usr/bin/env python3
"""BFF (Cloud Run) deploy tool — service.yaml is the manifest.

Same scope/mode grammar as agents/provision/deploy_agents.py and
apigee/provision/provision.py:

    deploy_frontend.py --check           read-only: live service vs service.yaml
                                         (image, env literals, secret refs,
                                         annotations, SA) + never-public probe
    deploy_frontend.py                   apply CONFIG only (reuse current image
                                         tag); confirms first
    deploy_frontend.py --build           build+push a NEW image (tag = git sha,
                                         or --tag), then apply; confirms first
    deploy_frontend.py --apply ...       skip the confirmation (automation)
    deploy_frontend.py --tag v1.2.3      explicit tag (with --build: build it;
                                         without: deploy that existing tag)

service.yaml carries `image: ...:__TAG__` — this tool substitutes the tag at
apply time. NEVER `gcloud run services replace service.yaml` directly: the
literal __TAG__ would ship. Overridable env: PROJECT, REGION, SERVICE, REPO.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVICE_YAML = HERE / "service.yaml"

# Environment values come from demo-environment.yaml (repo root) — the ONE
# file naming projects/regions. Env vars still override for ad-hoc use.
sys.path.insert(0, str(HERE.parent))
import demo_env  # noqa: E402

# macOS python.org builds ship no system CAs — use certifi (repo-wide pattern).
import ssl  # noqa: E402

try:
    import certifi

    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:  # certifi absent — platform default
    _SSL_CTX = ssl.create_default_context()

_ENV = demo_env.load_env()
PROJECT = os.environ.get("PROJECT", _ENV["projects"]["ai"])
REGION = os.environ.get("REGION", _ENV["regions"]["gateway"])   # Cloud Run + AR
AI_REGION = _ENV["regions"]["ai"]
SERVICE = os.environ.get("SERVICE", "agent-bff")
REPO = os.environ.get("REPO", "bff")
IMAGE_HOST = f"{REGION}-docker.pkg.dev/{PROJECT}/{REPO}/{SERVICE}"

_project_number_cache: list = []


def _project_number() -> str:
    if not _project_number_cache:
        try:
            r = subprocess.run(["gcloud", "projects", "describe", PROJECT,
                                "--format=value(projectNumber)"],
                               capture_output=True, text=True)
            _project_number_cache.append(r.stdout.strip() or "UNKNOWN")
        except Exception:  # noqa: BLE001 — no gcloud (tests) -> placeholder
            _project_number_cache.append("UNKNOWN")
    return _project_number_cache[0]


_supervisor_engine_cache: list = []


def _supervisor_engine_id() -> str:
    """Newest live reasoningEngine named a2a-supervisor-agent — the value
    AGENT_ENGINE_ID renders to, so redeploying the supervisor never requires
    editing service.yaml. "UNKNOWN" when unreachable (tests/no gcloud)."""
    if not _supervisor_engine_cache:
        try:
            tok = subprocess.run(["gcloud", "auth", "print-access-token"],
                                 capture_output=True, text=True).stdout.strip()
            req = urllib.request.Request(
                f"https://{AI_REGION}-aiplatform.googleapis.com/v1/projects/"
                f"{PROJECT}/locations/{AI_REGION}/reasoningEngines",
                headers={"Authorization": f"Bearer {tok}"})
            with urllib.request.urlopen(req, timeout=30, context=_SSL_CTX) as r:
                engines = json.load(r).get("reasoningEngines", [])
            mine = sorted((e for e in engines
                           if e.get("displayName") == "a2a-supervisor-agent"),
                          key=lambda e: e.get("createTime", ""), reverse=True)
            _supervisor_engine_cache.append(
                mine[0]["name"].rsplit("/", 1)[-1] if mine else "UNKNOWN")
        except Exception:  # noqa: BLE001 — no gcloud/network (tests)
            _supervisor_engine_cache.append("UNKNOWN")
    return _supervisor_engine_cache[0]


def ensure_iap_access() -> None:
    """Post-apply IAP wiring (idempotent):
      1. the IAP service agent gets run.invoker on the service (IAP fronts an
         IAM-protected Cloud Run service);
      2. every runtime-manifest bff.iap_members gets iap.httpsResourceAccessor.
    The OAuth consent screen (brand) is the one manual step on a fresh
    project — errors here print and continue so a missing brand is visible."""
    manifest = demo_env.load_manifest(HERE.parent / "agents" / "runtime-manifest.yaml")
    members = (manifest.get("bff") or {}).get("iap_members") or []
    pn = _project_number()
    iap_agent = f"service-{pn}@gcp-sa-iap.iam.gserviceaccount.com"
    subprocess.run(["gcloud", "beta", "services", "identity", "create",
                    "--service=iap.googleapis.com", f"--project={PROJECT}"],
                   capture_output=True, text=True)
    r = subprocess.run(["gcloud", "run", "services", "add-iam-policy-binding", SERVICE,
                        f"--project={PROJECT}", f"--region={REGION}",
                        f"--member=serviceAccount:{iap_agent}",
                        "--role=roles/run.invoker", "--quiet"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  ⚠ IAP agent invoker grant failed: "
              + (r.stderr.strip().splitlines() or ["?"])[-1])
    for member in members:
        r = subprocess.run(["gcloud", "beta", "iap", "web", "add-iam-policy-binding",
                            "--resource-type=cloud-run", f"--service={SERVICE}",
                            f"--region={REGION}", f"--project={PROJECT}",
                            f"--member={member}",
                            "--role=roles/iap.httpsResourceAccessor"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(f"  ⚠ IAP access grant for {member} failed: "
                  + (r.stderr.strip().splitlines() or ["?"])[-1])
        else:
            print(f"  IAP access ✓ {member}")


def render_service_yaml(tag: str = "__TAG__") -> str:
    """service.yaml with all environment tokens resolved (project ids never
    live in the file). __TAG__ resolves only when a tag is given."""
    s = SERVICE_YAML.read_text()
    s = (s.replace("__AI_PROJECT_NUMBER__", _project_number())
          .replace("__AI_PROJECT__", PROJECT)
          .replace("__GATEWAY_REGION__", REGION)
          .replace("__AI_REGION__", AI_REGION)
          .replace("__SUPERVISOR_ENGINE_ID__", _supervisor_engine_id()))
    if tag != "__TAG__":
        s = s.replace("__TAG__", tag)
    return s

OK, DRIFT, MISSING = "OK", "DRIFT", "MISSING"
_SYMBOL = {OK: "✅", DRIFT: "⚠", MISSING: "❌"}


def _anonymous_probe(url: str) -> tuple[int, str]:
    """(status_code, error). No credentials, no redirect-following, certifi CA
    bundle (macOS python.org builds have no local roots — stdlib verify fails)."""
    import ssl

    try:
        import certifi

        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        ctx = ssl.create_default_context()

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx), _NoRedirect)
    try:
        return opener.open(url + "/", timeout=10).status, ""
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


# ── Pure comparators (unit-tested) ────────────────────────────────────────────
def _f(status, kind, name, detail=""):
    return {"status": status, "kind": kind, "name": name, "detail": detail}


def _env_entry(e: dict):
    """('literal', value) | ('secret', 'name:key') for a service.yaml env entry."""
    if "value" in e:
        return ("literal", e["value"])
    ref = (e.get("valueFrom") or {}).get("secretKeyRef") or {}
    return ("secret", f"{ref.get('name')}:{ref.get('key', 'latest')}")


def compare_env(want_env: list[dict], live_env: list[dict]) -> list[dict]:
    """Per env var: literal values must match; secret refs must reference the
    same secret:key. Extra live vars are reported too (config not in the file
    would be silently stripped by the next replace)."""
    live = {e["name"]: _env_entry(e) for e in live_env}
    out = []
    for e in want_env:
        name, want = e["name"], _env_entry(e)
        if name not in live:
            out.append(_f(MISSING, "env", name, f"not set on the live service (want {want[0]})"))
            continue
        got = live[name]
        if got != want:
            out.append(_f(DRIFT, "env", name, f"live={got[1]!r} want={want[1]!r}"))
        else:
            out.append(_f(OK, "env", name, f"{want[0]} ✓"))
    extra = set(live) - {e["name"] for e in want_env}
    for name in sorted(extra):
        out.append(_f(DRIFT, "env", name, "on the live service but NOT in service.yaml — next apply strips it"))
    return out


def compare_service(want: dict, live: dict) -> list[dict]:
    """Image host+tag, service annotations (iap/ingress), revision annotations
    (vpc egress), and runtime SA."""
    out = []
    w_tmpl, l_tmpl = want["spec"]["template"]["spec"], live["spec"]["template"]["spec"]
    w_img = w_tmpl["containers"][0]["image"]
    l_img = l_tmpl["containers"][0]["image"]
    w_host = w_img.rsplit(":", 1)[0]
    if l_img.rsplit(":", 1)[0] != w_host:
        out.append(_f(DRIFT, "image", "repository", f"live={l_img} want host {w_host}"))
    else:
        out.append(_f(OK, "image", "repository", f"tag {l_img.rsplit(':', 1)[-1]} live"))
    for scope, w_ann, l_ann in (
        ("service-annotation", want["metadata"].get("annotations", {}),
         live["metadata"].get("annotations", {})),
        ("revision-annotation", want["spec"]["template"]["metadata"].get("annotations", {}),
         live["spec"]["template"]["metadata"].get("annotations", {})),
    ):
        for k, v in w_ann.items():
            got = l_ann.get(k)
            out.append(_f(OK if got == v else DRIFT, scope, k,
                          f"{v!r} ✓" if got == v else f"live={got!r} want={v!r}"))
    w_sa, l_sa = w_tmpl.get("serviceAccountName"), l_tmpl.get("serviceAccountName")
    out.append(_f(OK if w_sa == l_sa else DRIFT, "serviceAccount", w_sa or "?",
                  "✓" if w_sa == l_sa else f"live={l_sa}"))
    return out


# ── gcloud layer ──────────────────────────────────────────────────────────────
def _run(args: list[str], **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)


def _tee_run(args: list[str], log_path, **kw) -> int:
    """Run `args`, streaming combined output live to the terminal AND writing it
    to log_path — so a multi-minute build stays watchable but leaves a log."""
    with open(log_path, "w") as fh:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, bufsize=1, **kw)
        for line in proc.stdout:          # type: ignore[union-attr]
            sys.stdout.write(line)
            sys.stdout.flush()
            fh.write(line)
        return proc.wait()


def describe() -> dict | None:
    r = _run(["gcloud", "run", "services", "describe", SERVICE,
              f"--project={PROJECT}", f"--region={REGION}", "--format=json"])
    return json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else None


def live_tag(live: dict | None) -> str:
    if not live:
        return ""
    img = live["spec"]["template"]["spec"]["containers"][0]["image"]
    return img.rsplit(":", 1)[-1] if ":" in img else ""


def load_yaml() -> dict:
    import yaml

    return yaml.safe_load(render_service_yaml())


def should_write(args, what: str) -> bool:
    if getattr(args, "apply", False):
        return True
    if not sys.stdin.isatty():
        print("(no TTY — nothing applied. Pass --apply to run non-interactively.)")
        return False
    return input(f"\n{what}? [y/N] ").strip().lower() in ("y", "yes")


def cmd_check(args) -> int:
    print(f"# frontend — service {SERVICE} ({PROJECT} / {REGION})\n")
    want, live = load_yaml(), describe()
    if live is None:
        print(f"❌ MISSING service: {SERVICE} — not deployed")
        return 1
    findings = compare_service(want, live) + compare_env(
        want["spec"]["template"]["spec"]["containers"][0].get("env", []),
        live["spec"]["template"]["spec"]["containers"][0].get("env", []))
    n_bad = 0
    for f in findings:
        # the image tag in the file is a template — host match is the real check
        print(f"{_SYMBOL[f['status']]} {f['status']:<7} {f['kind']}: {f['name']} — {f['detail']}", flush=True)
        n_bad += f["status"] != OK
    url = live.get("status", {}).get("url", "")
    if url:
        code, err = _anonymous_probe(url)
        # 403 = Cloud Run IAM rejects anonymous; 302 = IAP redirects to Google
        # sign-in. Both mean "never public".
        ok = code in (403, 302)
        detail = (f"HTTP {code} (auth required — never public ✓)" if ok
                  else f"HTTP {code} — expected 403/302!" if code else f"probe failed: {err}")
        print(f"{_SYMBOL[OK if ok else DRIFT]} {'OK' if ok else 'DRIFT':<7} access: anonymous -> {detail}")
        n_bad += not ok
    print(f"\n{len(findings) + 1 - n_bad}/{len(findings) + 1} OK" + (f", {n_bad} to look at" if n_bad else ""))
    return 1 if n_bad else 0


def cmd_deploy(args) -> int:
    tag = args.tag
    if args.build:
        tag = tag or (_run(["git", "-C", str(HERE), "rev-parse", "--short", "HEAD"]).stdout.strip()
                      or "manual")
    else:
        tag = tag or live_tag(describe())
        if not tag:
            print("ERROR: no --build, no --tag, and no live tag to reuse.", file=sys.stderr)
            return 1
    image = f"{IMAGE_HOST}:{tag}"
    mode = "BUILD new image + apply config" if args.build else "apply CONFIG only (reuse image)"
    print(f"service: {SERVICE} ({PROJECT} / {REGION})\nimage:   {image}\nmode:    {mode}")
    if not should_write(args, f"{mode} for {SERVICE}"):
        print("nothing applied.")
        return 1
    if args.build:
        log = demo_env.deploy_log_dir("frontend") / f"bff-{tag}.log"
        print(f"==> building & pushing image ... (log: {log})", flush=True)
        rc = _tee_run(["gcloud", "builds", "submit", str(HERE),
                       f"--project={PROJECT}", "--tag", image], log)
        if rc != 0:
            return rc
    print("==> applying service.yaml ...", flush=True)
    rendered = render_service_yaml(tag)
    if "UNKNOWN" in rendered:
        print("ERROR: a render token resolved to UNKNOWN (project number or "
              "supervisor engine id) — is gcloud authed and the supervisor "
              "deployed? Not applying.", file=sys.stderr)
        return 2
    r = subprocess.run(["gcloud", "run", "services", "replace", "-",
                        f"--project={PROJECT}", f"--region={REGION}"],
                       input=rendered, text=True)
    if r.returncode != 0:
        return r.returncode
    print("==> IAP wiring (invoker for the IAP agent + web access members) ...", flush=True)
    ensure_iap_access()
    print("\ndone. Verify with --check (config + never-public probe).")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="BFF Cloud Run deploy (service.yaml = manifest)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="read-only: live service vs service.yaml")
    mode.add_argument("--build", action="store_true", help="build+push a new image, then apply")
    ap.add_argument("--apply", action="store_true", help="skip the confirmation prompt")
    ap.add_argument("--tag", help="image tag (default: git short sha for --build, live tag otherwise)")
    args = ap.parse_args(argv)
    if args.check:
        return cmd_check(args)
    return cmd_deploy(args)


if __name__ == "__main__":
    raise SystemExit(main())
