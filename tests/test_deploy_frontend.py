"""Unit tests for the BFF deploy tool's pure comparators (no I/O)."""
import deploy_frontend as df


def _svc(image, sa="bff-runner@p.iam.gserviceaccount.com", s_ann=None, r_ann=None, env=None):
    return {
        "metadata": {"annotations": s_ann or {}},
        "spec": {"template": {
            "metadata": {"annotations": r_ann or {}},
            "spec": {"serviceAccountName": sa,
                     "containers": [{"image": image, "env": env or []}]},
        }},
    }


def test_compare_env_literal_secret_missing_and_extra():
    want = [
        {"name": "AGENT_ENGINE_ID", "value": "NEW123"},
        {"name": "APIGEE_API_KEY", "valueFrom": {"secretKeyRef": {"name": "bff-key", "key": "latest"}}},
        {"name": "NOT_DEPLOYED", "value": "x"},
    ]
    live = [
        {"name": "AGENT_ENGINE_ID", "value": "OLD999"},                                   # drift
        {"name": "APIGEE_API_KEY", "valueFrom": {"secretKeyRef": {"name": "bff-key", "key": "latest"}}},  # ok
        {"name": "LEFTOVER", "value": "z"},                                               # extra
    ]
    by = {f["name"]: f for f in df.compare_env(want, live)}
    assert by["AGENT_ENGINE_ID"]["status"] == df.DRIFT and "OLD999" in by["AGENT_ENGINE_ID"]["detail"]
    assert by["APIGEE_API_KEY"]["status"] == df.OK and "secret" in by["APIGEE_API_KEY"]["detail"]
    assert by["NOT_DEPLOYED"]["status"] == df.MISSING
    assert by["LEFTOVER"]["status"] == df.DRIFT and "strips it" in by["LEFTOVER"]["detail"]


def test_compare_service_image_annotations_sa():
    want = _svc("host/repo/img:__TAG__",
                s_ann={"run.googleapis.com/iap-enabled": "true"},
                r_ann={"run.googleapis.com/vpc-access-egress": "private-ranges-only"})
    live_ok = _svc("host/repo/img:abc123",
                   s_ann={"run.googleapis.com/iap-enabled": "true"},
                   r_ann={"run.googleapis.com/vpc-access-egress": "private-ranges-only"})
    assert all(f["status"] == df.OK for f in df.compare_service(want, live_ok))

    live_bad = _svc("other-host/x:1", sa="wrong@p.iam.gserviceaccount.com",
                    s_ann={"run.googleapis.com/iap-enabled": "false"}, r_ann={})
    by_kind = {}
    for f in df.compare_service(want, live_bad):
        by_kind.setdefault(f["kind"], []).append(f)
    assert by_kind["image"][0]["status"] == df.DRIFT
    assert any(f["status"] == df.DRIFT for f in by_kind["service-annotation"])   # iap off
    assert any(f["status"] == df.DRIFT for f in by_kind["revision-annotation"])  # egress gone
    assert by_kind["serviceAccount"][0]["status"] == df.DRIFT


def test_real_service_yaml_parses_and_has_template_tag():
    want = df.load_yaml()
    image = want["spec"]["template"]["spec"]["containers"][0]["image"]
    assert image.endswith(":__TAG__"), "service.yaml image must keep the __TAG__ template"
    # IAP must be at SERVICE level (Cloud Run rejects it on the revision template)
    assert want["metadata"]["annotations"].get("run.googleapis.com/iap-enabled") == "true"
    assert "run.googleapis.com/iap-enabled" not in \
        want["spec"]["template"]["metadata"].get("annotations", {})
