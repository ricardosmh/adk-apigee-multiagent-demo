"""BFF trace propagation (frontend/app/tracing.py) — pure logic, no I/O."""
import json
import logging

import tracing


class H(dict):
    def get(self, k, default=None):
        return super().get(k.lower(), default)


def test_capture_traceparent_and_propagate():
    tp = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    tracing.capture(H({"traceparent": tp}))
    assert tracing.trace_id() == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert tracing.outbound_headers() == {"traceparent": tp}


def test_capture_falls_back_to_xctc():
    tracing.capture(H({"x-cloud-trace-context": "4BF92F3577B34DA6A3CE929D0E0E4736/123;o=1"}))
    assert tracing.trace_id() == "4bf92f3577b34da6a3ce929d0e0e4736"
    out = tracing.outbound_headers()["traceparent"]
    assert out.startswith("00-4bf92f3577b34da6a3ce929d0e0e4736-") and out.endswith("-01")


def test_capture_nothing_is_safe():
    tracing.capture(H())
    assert tracing.trace_id() is None
    assert tracing.outbound_headers() == {}


def test_structured_formatter_links_trace():
    tracing.capture(H({"traceparent": "00-" + "ab" * 16 + "-" + "cd" * 8 + "-01"}))
    fmt = tracing.StructuredFormatter("my-ai-project")
    rec = logging.LogRecord("bff", logging.INFO, __file__, 1, "hello %s", ("world",), None)
    entry = json.loads(fmt.format(rec))
    assert entry["message"] == "hello world" and entry["severity"] == "INFO"
    assert entry["logging.googleapis.com/trace"] == f"projects/my-ai-project/traces/{'ab' * 16}"
    # no trace captured -> field omitted, still valid JSON
    tracing.capture(H())
    entry2 = json.loads(fmt.format(rec))
    assert "logging.googleapis.com/trace" not in entry2


def test_structured_formatter_merges_json_fields():
    fmt = tracing.StructuredFormatter(None)
    rec = logging.LogRecord("bff", logging.INFO, __file__, 1, "agent turn", (), None)
    rec.json_fields = {"event": "turn", "usage": {"total": 9}}
    entry = json.loads(fmt.format(rec))
    assert entry["event"] == "turn" and entry["usage"] == {"total": 9}
