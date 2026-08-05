"""Environment-file substitution for the demo's manifests.

demo-environment.yaml (repo root) is the ONLY place naming the GCP projects
and the deployer's identity. Manifests reference its values as ${dotted.path}
tokens (e.g. ${projects.ai}, ${apigee_env}, ${identity.admin_email}); every
provisioning/deploy tool loads manifests through load_manifest() below, which
substitutes tokens in ALL string values recursively — dict KEYS included
(the ACL seed keys on the admin email).

Unknown tokens raise — a typo must fail loudly at load time, never reconcile
against a literal "${projects.ia}".
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / "demo-environment.yaml"

_TOKEN = re.compile(r"\$\{([a-zA-Z0-9_.]+)\}")


def deploy_log_dir(component: str | None = None) -> Path:
    """Repo-root ``deploy-logs/`` (gitignored) — ONE home for every deploy
    tool's output, instead of burying them under a component dir (deploy logs
    are about the deploy, not the component being deployed). Pass a component
    name (e.g. ``"agents"``, ``"services"``) for a per-tool subdir. Created on
    demand."""
    d = ROOT / "deploy-logs"
    if component:
        d = d / component
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_env(path: Path = ENV_FILE) -> dict:
    import yaml

    return yaml.safe_load(Path(path).read_text())


def _flatten(d: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, key + "."))
        else:
            out[key] = v
    return out


def substitute(obj, env_flat: dict):
    """Recursively substitute ${dotted.path} tokens in all string values
    (and dict keys — the ACL seed uses ${identity.admin_email} as a key)."""
    if isinstance(obj, str):
        def repl(m):
            key = m.group(1)
            if key not in env_flat:
                raise KeyError(
                    f"unknown token ${{{key}}} — not in demo-environment.yaml "
                    f"(known: {sorted(env_flat)})")
            return str(env_flat[key])
        return _TOKEN.sub(repl, obj)
    if isinstance(obj, list):
        return [substitute(v, env_flat) for v in obj]
    if isinstance(obj, dict):
        return {substitute(k, env_flat): substitute(v, env_flat)
                for k, v in obj.items()}
    return obj


def load_manifest(path, env_path: Path = ENV_FILE) -> dict:
    """A manifest with all environment tokens resolved."""
    import yaml

    manifest = yaml.safe_load(Path(path).read_text())
    return substitute(manifest, _flatten(load_env(env_path)))
