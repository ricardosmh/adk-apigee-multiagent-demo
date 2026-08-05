"""Unit tests for the analytics-module comparators + artifacts (pure, no I/O)."""
import json
from pathlib import Path

import provision_analytics as po

OBS = Path(po.__file__).resolve().parent   # analytics/ — artifacts sit beside the tool

# The gateway's Data Collectors, from apigee/provision/manifest.yaml. STRING
# collectors are report DIMENSIONS, INTEGER collectors are report METRICS —
# mixing them up creates a report the console can't render.
DC_STRING = {"dc_model", "dc_user_id"}
DC_INTEGER = {"dc_tokenCount", "dc_thoughtsTokenCount", "dc_inputToken", "dc_outputToken"}
BUILTIN_DIMS = {"apiproxy", "developer_app", "api_product", "proxy_basepath",
                "response_status_code", "client_id", "developer_email"}
BUILTIN_METRICS = {"message_count", "is_error", "total_response_time",
                   "target_response_time", "request_size", "response_size"}


def _by(findings, kind):
    return {f["name"]: f for f in findings if f["kind"] == kind}


# ── compare_apis ────────────────────────────────────────────────────────────────
def test_compare_apis_missing_and_ok():
    out = _by(po.compare_apis(po.REQUIRED_APIS, set()), "service")
    assert out["monitoring.googleapis.com"]["status"] == po.MISSING
    out = _by(po.compare_apis(po.REQUIRED_APIS, {"monitoring.googleapis.com"}), "service")
    assert out["monitoring.googleapis.com"]["status"] == po.OK


def test_compare_apis_listing_failed_is_one_drift_not_false_missing():
    out = po.compare_apis(po.REQUIRED_APIS, None)
    assert len(out) == 1 and out[0]["status"] == po.DRIFT


# ── compare_metrics ─────────────────────────────────────────────────────────────
def test_compare_metrics():
    out = _by(po.compare_metrics(["ai_tokens_total", "ai_latency_ms"], {"ai_tokens_total"}), "logMetric")
    assert out["ai_tokens_total"]["status"] == po.OK
    assert out["ai_latency_ms"]["status"] == po.MISSING


# ── compare_reports (Apigee custom reports, keyed by displayName) ───────────────
def _desired():
    return [{"displayName": "AI - Tokens by model", "chartType": "column",
             "dimensions": ["dc_model"],
             "metrics": [{"name": "dc_tokenCount", "function": "sum"}]}]


def test_compare_reports_missing():
    out = po.compare_reports(_desired(), {})
    assert out[0]["status"] == po.MISSING and out[0]["kind"] == "apigeeReport"


def test_compare_reports_listing_failed_is_one_drift():
    out = po.compare_reports(_desired(), None)
    assert len(out) == 1 and out[0]["status"] == po.DRIFT


def test_compare_reports_ok_ignores_server_fields_and_metric_order():
    live = {"AI - Tokens by model": {
        "name": "abc123", "organization": "my-apigee-org", "createdAt": "1",
        "displayName": "AI - Tokens by model", "chartType": "column",
        "dimensions": ["dc_model"],
        "metrics": [{"name": "dc_tokenCount", "function": "sum"}]}}
    out = po.compare_reports(_desired(), live)
    assert out[0]["status"] == po.OK


def test_compare_reports_drift_names_the_field():
    live = {"AI - Tokens by model": {
        "name": "abc123", "displayName": "AI - Tokens by model",
        "chartType": "line",                      # column desired
        "dimensions": ["dc_model"],
        "metrics": [{"name": "dc_tokenCount", "function": "avg"}]}}   # sum desired
    out = po.compare_reports(_desired(), live)
    assert out[0]["status"] == po.DRIFT
    assert "chartType" in out[0]["detail"] and "metrics" in out[0]["detail"]


def test_compare_reports_absent_filter_equals_empty():
    d = _desired()
    live = {"AI - Tokens by model": {**d[0], "name": "x", "filter": ""}}
    assert po.compare_reports(d, live)[0]["status"] == po.OK


# ── compare_addon ───────────────────────────────────────────────────────────────
def test_addon_enabled_from_org():
    # EVALUATION orgs include analytics but expose NO analyticsConfig (live)
    assert po.addon_enabled_from_org({"billingType": "EVALUATION",
                                      "addonsConfig": {}}) is True
    assert po.addon_enabled_from_org(
        {"billingType": "PAYG",
         "addonsConfig": {"analyticsConfig": {"enabled": True}}}) is True
    assert po.addon_enabled_from_org({"billingType": "PAYG", "addonsConfig": {}}) is False


def test_compare_addon_states():
    assert po.compare_addon(True)[0]["status"] == po.OK
    disabled = po.compare_addon(False)[0]
    assert disabled["status"] == po.DRIFT and "NO data" in disabled["detail"]
    assert po.compare_addon(None)[0]["status"] == po.DRIFT


# ── build_findings (the report/apply gate) ──────────────────────────────────────
def test_build_findings_all_present_is_all_ok():
    desired = _desired()
    state = {
        "enabled_apis": set(po.REQUIRED_APIS),
        "metrics": {"ai_tokens_total", "ai_latency_ms"},
        "dashboard": True,
        "reports": {d["displayName"]: {**d, "name": "srv-id"} for d in desired},
        "analytics_addon": True,
    }
    out = po.build_findings(state, po.REQUIRED_APIS, ["ai_tokens_total", "ai_latency_ms"], desired)
    assert all(f["status"] == po.OK for f in out)


def test_build_findings_empty_state_flags_every_layer():
    state = {"enabled_apis": set(), "metrics": set(), "reports": {}, "analytics_addon": False}
    out = po.build_findings(state, po.REQUIRED_APIS, ["ai_tokens_total"], _desired())
    bad_kinds = {f["kind"] for f in out if f["status"] != po.OK}
    assert {"service", "logMetric", "dashboard", "apigeeReport", "analyticsAddon"} <= bad_kinds


# ── artifacts: the desired custom reports ───────────────────────────────────────
def test_apigee_reports_wellformed():
    reports = json.loads((OBS / "apigee_reports.json").read_text())
    assert len(reports) >= 4
    names = [r["displayName"] for r in reports]
    assert len(names) == len(set(names)), "displayName is the idempotency key — must be unique"
    for r in reports:
        assert r["chartType"] in ("column", "line")
        assert r["dimensions"], "a report needs at least one dimension"
        assert r["metrics"], "a report needs at least one metric"
        # dimensions: string collectors or built-ins; metrics: integer
        # collectors or built-ins — an INTEGER collector as a dimension (or
        # vice versa) yields a report the console can't render.
        for dim in r["dimensions"]:
            assert dim in DC_STRING | BUILTIN_DIMS, f"unknown/invalid dimension {dim}"
        for m in r["metrics"]:
            assert m["name"] in DC_INTEGER | BUILTIN_METRICS, f"unknown/invalid metric {m['name']}"
            assert m["function"] in ("sum", "avg", "min", "max")


def test_apigee_reports_cover_the_core_cuts():
    blob = (OBS / "apigee_reports.json").read_text()
    # the core cuts: by model, by user, by app
    for dim in ("dc_model", "dc_user_id", "developer_app"):
        assert dim in blob
    assert "dc_tokenCount" in blob


def test_apigee_reports_match_manifest_collectors():
    """Every dc_* used in a report must exist in the org manifest's
    dataCollectors (or the report silently renders nothing)."""
    import re
    manifest = (OBS.parent / "apigee" / "provision" / "manifest.yaml").read_text()
    declared = set(re.findall(r"\{name:\s*(dc_\w+)", manifest))
    used = set(re.findall(r"dc_\w+", (OBS / "apigee_reports.json").read_text()))
    assert used <= declared, f"reports use undeclared collectors: {used - declared}"


# ── artifacts: log-based metrics ─────────────────────────────────────────────────
def test_log_based_metrics_wellformed():
    metrics = json.loads((OBS / "log_based_metrics.json").read_text())
    names = {m["name"] for m in metrics}
    assert {"ai_llm_calls", "ai_tokens_total", "ai_quota_used",
            "ai_latency_ms", "ai_gateway_faults"} <= names
    for m in metrics:
        cfg = m["config"]
        assert cfg["filter"] and cfg["metricDescriptor"]["metricKind"]
        # low-cardinality labels only — never per-user email (cardinality blowup)
        labels = {l["key"] for l in cfg["metricDescriptor"].get("labels", [])}
        assert "user" not in labels
        # distributions must carry bucketOptions
        if cfg["metricDescriptor"]["valueType"] == "DISTRIBUTION":
            assert "bucketOptions" in cfg


# ── artifacts: Monitoring dashboard ─────────────────────────────────────────────
def test_dashboard_wellformed_and_references_the_metrics():
    dash = json.loads((OBS / "dashboard.json").read_text())
    assert dash["displayName"] == po.DASHBOARD_TITLE
    tiles = dash["mosaicLayout"]["tiles"]
    assert len(tiles) >= 4
    blob = json.dumps(dash)
    # cross-platform tiles: calls, tokens, latency, faults
    for name in ("ai_llm_calls", "ai_tokens_total", "ai_latency_ms", "ai_gateway_faults"):
        assert f"logging.googleapis.com/user/{name}" in blob
    # Apigee-sourced tiles must pin resource.type="api" (dual-resource dedup);
    # latency (engine/service/bff) must NOT — it isn't under the api resource.
    assert 'resource.type=\\"api\\"' in blob or 'resource.type="api"' in blob
