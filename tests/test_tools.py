"""Tool-call parsing tests: the model's fenced ``tool`` block never reaches the client."""

from __future__ import annotations

import json

from museai.core.tools import MARKER, ToolCallStream, render_prompt, tool_call_payload


def parse(chunks: list[str]) -> tuple[str, list]:
    stream = ToolCallStream()
    text = ""
    for chunk in chunks:
        text += "".join(value for kind, value in stream.feed(chunk) if kind == "text")
    text += "".join(value for kind, value in stream.flush() if kind == "text")
    return text, stream.calls


def test_plain_text_passes_through():
    text, calls = parse(["Hello", " there"])
    assert text == "Hello there"
    assert calls == []


def test_block_is_split_out_of_the_text():
    body = '{"tool_calls": [{"name": "read_file", "arguments": {"path": "a.py"}}]}'
    text, calls = parse(["Let me look.", f"\n{MARKER}\n{body}\n```\n"])
    assert text.strip() == "Let me look."
    assert [call.name for call in calls] == ["read_file"]
    assert json.loads(calls[0].arguments) == {"path": "a.py"}


def test_marker_split_across_deltas_is_still_detected():
    body = '{"tool_calls": [{"name": "run", "arguments": "{}"}]}'
    chunks = ["ok ", "``", "`to", "ol\n", body[:20], body[20:], "\n```"]
    text, calls = parse(chunks)
    assert text.strip() == "ok"
    assert calls[0].name == "run"
    assert calls[0].arguments == "{}"


def test_invalid_json_falls_back_to_text():
    text, calls = parse([f"{MARKER}\nnot json\n```\n"])
    assert calls == []
    assert MARKER in text
    assert "not json" in text


def test_backticks_before_marker_survive():
    text, calls = parse(["use `x` then ", f"{MARKER}\n{{\"name\": \"t\", \"arguments\": {{}}}}\n```"])
    assert text.startswith("use `x` then")
    assert calls[0].name == "t"


def test_tool_call_payload_shape():
    _text, calls = parse([f'{MARKER}\n{{"tool_calls": [{{"name": "f", "arguments": {{"a": 1}}}}]}}\n```'])
    payload = tool_call_payload(calls)
    assert payload[0]["type"] == "function"
    assert payload[0]["function"]["name"] == "f"
    assert json.loads(payload[0]["function"]["arguments"]) == {"a": 1}
    assert payload[0]["id"]


def test_render_prompt_includes_schema_and_forced_choice():
    tools = [{"function": {"name": "f", "description": "does f",
                           "parameters": {"type": "object", "properties": {}}}}]
    prompt = render_prompt(tools, {"type": "function", "function": {"name": "f"}})
    assert "`f`" in prompt
    assert "You must call" in prompt
    assert "arguments schema" in prompt
