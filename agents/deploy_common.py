"""Shared deploy-time helpers for the agent deploy worker.

LOCAL-ONLY: these run on the deploy host (building env_vars, patching the SDK,
reading requirements) — they are NOT bundled into the engine, so unlike
``a2a_common`` this needs no vendoring. Consumed by
``agents/provision/deploy_agents.py`` (whose per-agent worker subprocess adds
this directory to ``sys.path``).
"""
import json
import os
import pathlib
import subprocess
import tomllib

from google.protobuf import json_format


def patch_messagetojson_for_pydantic() -> None:
    """Workaround for a vertexai SDK bug.

    ``agent_engines.create(...)`` serializes the A2aAgent's ``agent_card``
    attribute via ``google.protobuf.json_format.MessageToJson(agent_card)``. But
    ``agent_card`` is an ``a2a.types.AgentCard`` — a pydantic model, not a
    protobuf message — so MessageToJson crashes with ``AttributeError:
    'AgentCard' object has no attribute 'DESCRIPTOR'``. Delegate to pydantic's
    own JSON serializer when handed a pydantic model. Must run BEFORE
    ``agent_engines.create(...)``. (No-op for the supervisor's AdkApp, which has
    no agent_card — kept for parity.)
    """
    _original = json_format.MessageToJson

    def _patched(message, *args, **kwargs):
        if hasattr(message, "model_dump_json"):
            return message.model_dump_json()
        return _original(message, *args, **kwargs)

    json_format.MessageToJson = _patched


def load_requirements() -> list[str]:
    """The deployed engine's pip requirements = this agent's pyproject deps."""
    with open("pyproject.toml", "rb") as f:
        return tomllib.load(f)["project"]["dependencies"]


def build_env_vars(passthrough_keys: list[str]) -> dict[str, str]:
    """Pass through the named env vars that are actually set locally."""
    return {k: os.environ[k] for k in passthrough_keys if k in os.environ}


def apigee_secret_env_vars() -> dict:
    """APIGEE_API_KEY as a Secret Manager reference when the .env names a secret.

    When APIGEE_API_KEY_SECRET is set (e.g. ``apigee-key-order``, created by
    ``apigee/provision/provision.py secrets``), return
    ``{"APIGEE_API_KEY": SecretRef(secret=..., version=...)}`` — Agent Engine
    resolves it at deploy and injects the value at runtime, so the key never
    transits the deploy config. Apply AFTER build_env_vars so the SecretRef wins
    over any plaintext APIGEE_API_KEY (kept as the local-dev fallback).
    """
    secret = os.environ.get("APIGEE_API_KEY_SECRET")
    if not secret:
        return {}
    from google.cloud.aiplatform_v1.types import SecretRef

    version = os.environ.get("APIGEE_API_KEY_SECRET_VERSION", "latest")
    print(f"  APIGEE_API_KEY from Secret Manager: {secret} (version {version})", flush=True)
    return {"APIGEE_API_KEY": SecretRef(secret=secret, version=version)}


def _bound_roles(policy: dict, member: str) -> set[str]:
    """Roles a serviceAccount member holds in an IAM policy dict."""
    want = f"serviceAccount:{member}"
    return {b["role"] for b in policy.get("bindings", []) if want in b.get("members", [])}


def _gcloud_json(args: list[str]):
    r = subprocess.run(["gcloud", *args, "--format=json"], capture_output=True, text=True)
    return json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else None


def _manifest_entry() -> dict | None:
    """This agent's entry in agents/runtime-manifest.yaml, matched by the cwd
    directory name (the deploy worker always runs from the agent's own directory).
    None when the manifest/PyYAML is unavailable — preflight degrades gracefully.
    """
    try:
        import yaml

        m = yaml.safe_load((pathlib.Path(__file__).parent / "runtime-manifest.yaml").read_text())
        cwd = pathlib.Path.cwd().name
        return next((s for s in m["agents"].values() if s.get("dir") == cwd), None)
    except Exception:
        return None


def preflight_identity(runner_sa: str | None, project: str, secret: str | None = None) -> bool:
    """Verify the runtime identity against runtime-manifest.yaml before deploying.

    Checks, in order:
    - the SA exists (the ONLY blocker — the deploy would die cryptically anyway);
    - the .env matches the manifest: AGENT_RUNNER_SA is this agent's declared SA
      and APIGEE_API_KEY_SECRET is its declared secret (catches pointing an
      agent at another agent's identity/key);
    - the SA holds the manifest-declared roles (falls back to aiplatform.user
      when the manifest is unavailable);
    - runner SA + GEAP service agent hold secretAccessor on the secret.

    Everything but the missing SA is a WARNING — never blocks a low-privilege
    deploy. Fixes are the provisioning tools, not ad-hoc grants:
    SA/roles → ``python agents/provision/provision_agents.py --apply``;
    secrets → ``python apigee/provision/provision.py secrets --apply``.
    Skips silently when gcloud is unavailable or no runner_sa is set.
    """
    if not runner_sa:
        return True
    try:
        sa = _gcloud_json(["iam", "service-accounts", "describe", runner_sa])
    except FileNotFoundError:  # no gcloud on this host — don't block
        return True
    if sa is None:
        print(f"ERROR: runtime SA {runner_sa} not found — create it with"
              " `python agents/provision/provision_agents.py --apply`.")
        return False

    warnings = []
    expected = _manifest_entry()
    if expected:
        want_sa = f"{expected['sa']}@{project}.iam.gserviceaccount.com"
        if runner_sa != want_sa:
            warnings.append(f".env AGENT_RUNNER_SA={runner_sa} but runtime-manifest.yaml"
                            f" declares {want_sa} for this agent")
        if secret and expected.get("secret") and secret != expected["secret"]:
            warnings.append(f".env APIGEE_API_KEY_SECRET={secret} but runtime-manifest.yaml"
                            f" declares {expected['secret']} for this agent")
    required_roles = (expected or {}).get("roles") or ["roles/aiplatform.user"]
    policy = _gcloud_json(["projects", "get-iam-policy", project])
    if policy is not None:
        held = _bound_roles(policy, runner_sa)
        warnings += [f"{runner_sa} lacks {r} on {project}" for r in required_roles if r not in held]
    if secret:
        sec_policy = _gcloud_json(["secrets", "get-iam-policy", secret, "--project", project])
        if sec_policy is None:
            warnings.append(f"secret {secret} not found in {project}")
        else:
            accessors = {
                m.split(":", 1)[1]
                for b in sec_policy.get("bindings", [])
                if b["role"] == "roles/secretmanager.secretAccessor"
                for m in b.get("members", [])
                if m.startswith("serviceAccount:")
            }
            if runner_sa not in accessors:
                warnings.append(f"{runner_sa} lacks secretAccessor on {secret} (runtime read will fail)")
            proj = _gcloud_json(["projects", "describe", project])
            geap_agent = f"service-{proj['projectNumber']}@gcp-sa-aiplatform.iam.gserviceaccount.com" if proj else None
            if geap_agent and geap_agent not in accessors:
                warnings.append(f"{geap_agent} lacks secretAccessor on {secret} (SecretRef resolution at deploy will fail)")
    for w in warnings:
        print(f"  ⚠ preflight: {w}")
    if warnings:
        print("  ⚠ fix with: python agents/provision/provision_agents.py --apply (SA/roles)"
              " / python apigee/provision/provision.py secrets --apply (secret access)")
    return True


def _demo_env():
    """demo-environment.yaml values (repo root) — the source for defaults so
    per-agent .env files no longer need project-specific plumbing entries."""
    import sys

    root = pathlib.Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    import demo_env

    try:
        return demo_env.load_env()
    except Exception:  # noqa: BLE001 — env file absent (tests)
        return None


def _default_attachment() -> str | None:
    env = _demo_env()
    if not env:
        return None
    return (f"projects/{env['projects']['ai']}/regions/{env['regions']['ai']}"
            f"/networkAttachments/agent-engine-attachment")


def psc_interface_config():
    """Build the PSC-I config so the engine egresses through our VPC network
    attachment and can resolve the private Apigee hostname.

    Returns None (public egress) unless PSC_NETWORK_ATTACHMENT is set:
      PSC_NETWORK_ATTACHMENT  projects/<p>/regions/<region>/networkAttachments/<name>
                              (must be in the SAME region as this engine).
      PSC_DNS_DOMAIN          private DNS zone suffix, must end with '.'
                              (enables a DNS peering zone in the GEAP tenant VPC).
      PSC_DNS_TARGET_PROJECT  project hosting that zone (default: PROJECT_ID).
      PSC_DNS_TARGET_NETWORK  VPC where the zone is visible (e.g. "ai-vpc").
    """
    network_attachment = os.environ.get("PSC_NETWORK_ATTACHMENT") or _default_attachment()
    if not network_attachment:
        return None

    from google.cloud.aiplatform_v1 import types as aip_types

    dns_configs = []
    env = _demo_env()
    domain = os.environ.get("PSC_DNS_DOMAIN") or ("apigee.com." if env else None)
    target_network = os.environ.get("PSC_DNS_TARGET_NETWORK") or ("ai-vpc" if env else None)
    if domain and target_network:
        dns_configs.append(
            aip_types.DnsPeeringConfig(
                domain=domain,
                target_project=os.environ.get("PSC_DNS_TARGET_PROJECT")
                or os.environ.get("PROJECT_ID")
                or (env["projects"]["ai"] if env else ""),
                target_network=target_network,
            )
        )
    return aip_types.PscInterfaceConfig(
        network_attachment=network_attachment,
        dns_peering_configs=dns_configs,
    )


def telemetry_env_vars(service_name: str) -> dict[str, str]:
    """Defaults that turn on GEAP Agent Engine telemetry at deploy time.

    Without these, an engine ships with the per-engine console toggles OFF, so
    traces/logs/prompt-content don't populate until someone flips switches in
    the console. Set DISABLE_DEFAULT_TELEMETRY=1 locally to opt out (returns {}).

    Knobs: GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY (master switch, traces +
    logs), OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=EVENT_ONLY (prompt/
    response as OTEL log events; `true` is rejected by the latest GenAI
    semconv), OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental,
    ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=false (keep payloads in log events, not
    span attributes — avoids PII-on-spans + size violations),
    OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED=true, OTEL_SERVICE_NAME.

    PII/cost note: prompt/response logging captures full user content.
    """
    if os.environ.get("DISABLE_DEFAULT_TELEMETRY") == "1":
        return {}
    return {
        "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY": "true",
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "EVENT_ONLY",
        "OTEL_SEMCONV_STABILITY_OPT_IN": "gen_ai_latest_experimental",
        "ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS": "false",
        "OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED": "true",
        "OTEL_SERVICE_NAME": service_name,
    }
