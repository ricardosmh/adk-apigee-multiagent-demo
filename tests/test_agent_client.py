"""BFF (frontend/agent_client.py) pure-logic tests — no network.

Focus: the thought/answer split (the fix that stopped scaffolding leaking into
the visible answer) and the streamed-JSON parsers.
"""
import pytest

pytest.importorskip("httpx")  # agent_client imports httpx at module load

import agent_client as ac


def _model_ev(parts, partial=None):
    ev = {"content": {"role": "model", "parts": parts}}
    if partial is not None:
        ev["partial"] = partial
    return ev


def test_aggregate_splits_thoughts_from_answer():
    events = [
        _model_ev([
            {"text": "For context:", "thought": True},
            {"text": "[root_agent] called tool transfer_to_agent", "thought": True},
        ]),
        _model_ev([{"text": "Real answer."}, {"text": ""}]),
    ]
    out = ac._aggregate(events)
    assert out["text"] == "Real answer."
    assert out["thoughts"] and "For context:" in out["thoughts"]
    assert "For context:" not in out["text"]


def test_aggregate_thoughts_none_when_absent():
    out = ac._aggregate([_model_ev([{"text": "just an answer"}])])
    assert out["text"] == "just an answer"
    assert out["thoughts"] is None


def test_events_to_turns_splits_thoughts():
    events = [
        {"content": {"role": "user", "parts": [{"text": "hi"}]}},
        _model_ev([{"text": "thinking...", "thought": True}, {"text": "answer"}]),
    ]
    turns = ac._events_to_turns(events)
    roles = [t["role"] for t in turns]
    assert roles == ["user", "model"]
    model = turns[1]
    assert model["text"] == "answer"
    assert model["thoughts"] and "thinking" in model["thoughts"]


def test_parse_event_stream_variants():
    assert ac._parse_event_stream('{"a": 1}') == [{"a": 1}]
    assert ac._parse_event_stream('[{"a": 1}, {"b": 2}]') == [{"a": 1}, {"b": 2}]
    assert ac._parse_event_stream('{"a": 1}{"b": 2}') == [{"a": 1}, {"b": 2}]
    assert ac._parse_event_stream("") == []


def test_drain_json_keeps_trailing_partial():
    remaining, objs = ac._drain_json('[{"a": 1}, {"b": 2')
    assert objs == [{"a": 1}]
    assert '{"b": 2' in remaining


def test_user_header_from_agent_input():
    """x-user-email rides on every agent call whose input carries user_id —
    the /ai-agents proxy captures it into dc_user_id for per-user analytics."""
    import agent_client as ac

    assert ac._user_header({"user_id": "a@x.com"}) == {"x-user-email": "a@x.com"}
    assert ac._user_header({"message": "hi"}) == {}
    assert ac._user_header(None) == {}


def test_llm_user_header():
    import llm_client as lc

    assert lc._user_header("a@x.com") == {"x-user-email": "a@x.com"}
    assert lc._user_header(None) == {}
    assert lc._user_header("") == {}
