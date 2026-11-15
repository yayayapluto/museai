"""Gateway codec tests.

The fixtures are real frames captured from the live gateway
(``tests/fixtures/gateway_frames.json``), so these tests fail if the framing is ever
re-encoded wrongly or the server changes shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from museai.drivers.http import gateway

FIXTURES = Path(__file__).parent / "fixtures"


def _frame(name: str) -> bytes:
    data = json.loads((FIXTURES / "gateway_frames.json").read_text(encoding="utf-8"))
    return bytes.fromhex(data[name])


def test_reset_frame_decodes_to_protocol_error():
    stream_id, kind, fields = gateway.decode_reply(_frame("reset"))
    assert kind == "reset"
    assert fields[1] == 3  # PROTOCOL_ERROR
    assert fields[2] == b"empty frame kind"


def test_response_frame_carries_status():
    _stream_id, kind, fields = gateway.decode_reply(_frame("response"))
    assert kind == "response"
    assert fields[1] == 200
    assert 4 not in fields  # proto3 omits end_body until set


def test_body_chunk_frame_is_json():
    _stream_id, kind, fields = gateway.decode_reply(_frame("body_chunk"))
    assert kind == "body_chunk"
    payload = json.loads(fields[1])
    assert isinstance(payload, dict)


def test_encode_frames_roundtrip():
    plain = gateway.encode_frames(7, 99, b"hello", index=1, total=2)
    fields = gateway.pb_parse(plain)
    assert fields == {1: 99, 2: 1, 3: 2, 4: b"hello"}


def test_application_request_without_body_has_no_body_field():
    fields = gateway.pb_parse(gateway.application_request("GET", "/api/session/list"))
    assert fields[1] == b"GET"
    assert fields[2] == b"/api/session/list"
    assert 4 not in fields
    assert fields[5] == 1


def test_text_delta_only_yields_assistant_text():
    assert gateway.text_delta({"event": "delta.text_append", "payload": {"text": "Hi"}}) == "Hi"
    assert gateway.text_delta({"event": "delta.message_start", "payload": {"text": "x"}}) is None
    assert gateway.text_delta({"event": "delta.text_append", "payload": {}}) is None


@pytest.mark.skipif(
    not (FIXTURES / "chat_events.jsonl").exists(), reason="no recorded chat stream"
)
def test_recorded_events_join_into_the_answer():
    lines = (FIXTURES / "chat_events.jsonl").read_text(encoding="utf-8").splitlines()
    events = [json.loads(line) for line in lines if line.strip()]
    answer = "".join(filter(None, (gateway.text_delta(ev) for ev in events)))
    assert answer.strip() == "Red, yellow, blue"
    assert any(ev.get("event") == "delta.message_done" for ev in events)
