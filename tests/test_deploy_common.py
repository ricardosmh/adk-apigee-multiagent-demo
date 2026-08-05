"""Unit tests for deploy_common's SecretRef wiring + identity preflight.

The gcloud-backed layer is stubbed via _gcloud_json; these lock in the decision
logic (what blocks a deploy vs. what only warns).
"""
import deploy_common as dc


def test_apigee_secret_env_vars_empty_without_secret(monkeypatch):
    monkeypatch.delenv("APIGEE_API_KEY_SECRET", raising=False)
    assert dc.apigee_secret_env_vars() == {}


def test_apigee_secret_env_vars_builds_secretref(monkeypatch):
    monkeypatch.setenv("APIGEE_API_KEY_SECRET", "apigee-key-order")
    monkeypatch.delenv("APIGEE_API_KEY_SECRET_VERSION", raising=False)
    env = dc.apigee_secret_env_vars()
    ref = env["APIGEE_API_KEY"]
    assert ref.secret == "apigee-key-order" and ref.version == "latest"

    monkeypatch.setenv("APIGEE_API_KEY_SECRET_VERSION", "3")
    assert dc.apigee_secret_env_vars()["APIGEE_API_KEY"].version == "3"


def test_bound_roles_filters_by_member():
    policy = {"bindings": [
        {"role": "roles/aiplatform.user", "members": ["serviceAccount:a@p.iam.gserviceaccount.com"]},
        {"role": "roles/logging.logWriter", "members": ["serviceAccount:b@p.iam.gserviceaccount.com"]},
    ]}
    assert dc._bound_roles(policy, "a@p.iam.gserviceaccount.com") == {"roles/aiplatform.user"}


def _fake_gcloud(responses):
    """Map the first arg-tuple element ('iam', 'projects', 'secrets') to a response."""
    return lambda args: responses.get(args[0])


def test_preflight_missing_sa_blocks(monkeypatch, capsys):
    monkeypatch.setattr(dc, "_gcloud_json", _fake_gcloud({"iam": None}))
    assert dc.preflight_identity("sa@p.iam.gserviceaccount.com", "p") is False
    assert "provision_agents.py --apply" in capsys.readouterr().out


def test_preflight_warns_but_does_not_block(monkeypatch, capsys):
    sa = "a2a-order-sa@p.iam.gserviceaccount.com"
    responses = {
        "iam": {"email": sa},
        "projects": {"bindings": [], "projectNumber": "123"},  # no roles at all
        "secrets": {"bindings": []},                            # no accessors at all
    }
    monkeypatch.setattr(dc, "_gcloud_json", _fake_gcloud(responses))
    assert dc.preflight_identity(sa, "p", secret="apigee-key-order") is True
    out = capsys.readouterr().out
    assert "lacks roles/aiplatform.user" in out
    assert "lacks secretAccessor" in out
    assert "gcp-sa-aiplatform" in out  # vertex service agent checked too


def test_preflight_skips_without_runner_sa():
    assert dc.preflight_identity(None, "p") is True


def test_preflight_cross_checks_env_against_manifest(monkeypatch, capsys):
    """A .env pointing at another agent's SA/secret is flagged as drift."""
    entry = {"sa": "a2a-order-sa", "secret": "apigee-key-order",
             "roles": ["roles/aiplatform.user"]}
    monkeypatch.setattr(dc, "_manifest_entry", lambda: entry)
    wrong_sa = "a2a-customer-sa@p.iam.gserviceaccount.com"
    responses = {
        "iam": {"email": wrong_sa},
        "projects": {"bindings": [{"role": "roles/aiplatform.user",
                                   "members": [f"serviceAccount:{wrong_sa}"]}],
                     "projectNumber": "123"},
    }
    monkeypatch.setattr(dc, "_gcloud_json", _fake_gcloud(responses))
    assert dc.preflight_identity(wrong_sa, "p", secret="apigee-key-customer") is True
    out = capsys.readouterr().out
    assert "declares a2a-order-sa@p.iam.gserviceaccount.com" in out   # SA mismatch caught
    assert "declares apigee-key-order" in out                          # secret mismatch caught


def test_preflight_manifest_roles_checked(monkeypatch, capsys):
    """Roles come from the manifest entry, not the hardcoded default."""
    entry = {"sa": "a2a-supervisor-sa", "secret": None,
             "roles": ["roles/aiplatform.user", "roles/agentregistry.viewer"]}
    monkeypatch.setattr(dc, "_manifest_entry", lambda: entry)
    sa = "a2a-supervisor-sa@p.iam.gserviceaccount.com"
    responses = {
        "iam": {"email": sa},
        "projects": {"bindings": [{"role": "roles/aiplatform.user",
                                   "members": [f"serviceAccount:{sa}"]}]},
    }
    monkeypatch.setattr(dc, "_gcloud_json", _fake_gcloud(responses))
    assert dc.preflight_identity(sa, "p") is True
    assert "lacks roles/agentregistry.viewer" in capsys.readouterr().out
