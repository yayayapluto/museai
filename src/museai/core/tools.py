"""Tool calling for chat models that do not speak the OpenAI tool protocol.

muse.ai's agent has no tool-call channel on the wire: it emits plain text. To keep
OpenAI clients (Cline, Cursor, LangChain, ...) working, the driver renders the tool
schemas into the prompt, asks for one fenced ``tool`` block, and parses it back out of
the token stream. Text before the block stays assistant content.

Wire contract asked of the model (see :func:`render_prompt`)::

    ```tool
    {"tool_calls": [{"name": "read_file", "arguments": {"path": "a.py"}}]}
    ```
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

MARKER = "```tool"


def render_prompt(tools: list[dict[str, Any]], tool_choice: Any = None) -> str:
    """Instructions appended to the user prompt so the model emits ``tool`` blocks."""
    lines = [
        "# Tools",
        "You may call tools. To call one, end your reply with a fenced block tagged",
        "`tool` containing JSON: {\"tool_calls\": [{\"name\": <name>, \"arguments\": {<args>}}]}.",
        "Call at most one tool per reply, and do not wrap the JSON in prose.",
    ]
    choice_name = _forced_tool_name(tool_choice)
    if choice_name:
        lines.append(f"You must call `{choice_name}` in this reply.")
    elif tool_choice == "none":
        lines.append("Do not call any tool in this reply.")
    for tool in tools:
        fn = tool.get("function", tool)
        lines.append(
            f"- `{fn.get('name')}`: {fn.get('description', '')}\n"
            f"  arguments schema: {json.dumps(fn.get('parameters') or {}, ensure_ascii=False)}"
        )
    return "\n".join(lines)


def _forced_tool_name(tool_choice: Any) -> str | None:
    if isinstance(tool_choice, dict):
        fn = tool_choice.get("function") or {}
        return tool_choice.get("name") or fn.get("name")
    return None


@dataclass
class ToolCall:
    name: str
    arguments: str
    """JSON-encoded arguments, ready for an OpenAI ``tool_calls`` entry."""
    id: str = ""


Event = tuple[Literal["text", "call"], str]


@dataclass
class ToolCallStream:
    """Split a streamed reply into assistant text and tool calls.

    Text is held back only while it could still be the start of ``MARKER``, so deltas
    keep flowing; the fenced block itself never reaches the client.
    """

    _pending: str = ""
    _block: str | None = None
    calls: list[ToolCall] = field(default_factory=list)

    def feed(self, delta: str) -> list[Event]:
        out: list[Event] = []
        while delta:
            if self._block is not None:
                self._block += delta
                delta = ""
                if "```" in self._block.split("\n", 1)[-1] or self._block.count("```") >= 2:
                    out.extend(self._close_block())
                continue
            self._pending += delta
            delta = ""
            idx = self._pending.find(MARKER)
            if idx >= 0:
                if idx:
                    out.append(("text", self._pending[:idx]))
                self._block = self._pending[idx + len(MARKER) :]
                self._pending = ""
                if "```" in self._block:
                    out.extend(self._close_block())
                continue
            keep = _marker_prefix_len(self._pending)
            cut = len(self._pending) - keep
            if cut > 0:
                out.append(("text", self._pending[:cut]))
                self._pending = self._pending[cut:]
        return out

    def flush(self) -> list[Event]:
        """Drain buffered text at end of stream; unterminated blocks stay text."""
        if self._block is not None:
            self._close_block()
        out = [("text", self._pending)] if self._pending else []
        self._pending = ""
        return out

    def _close_block(self) -> list[Event]:
        block, self._block = self._block or "", None
        body = block.split("```", 1)[0].strip()
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return [("text", MARKER + block)]
        calls = payload.get("tool_calls") or ([payload] if "name" in payload else [])
        out: list[Event] = []
        for raw in calls:
            name = raw.get("name") or (raw.get("function") or {}).get("name")
            if not name:
                continue
            args = raw.get("arguments")
            if args is None:
                args = (raw.get("function") or {}).get("arguments") or {}
            self.calls.append(
                ToolCall(
                    name=name,
                    arguments=args if isinstance(args, str) else json.dumps(args, ensure_ascii=False),
                    id=f"call_{len(self.calls)}_{name}",
                )
            )
            out.append(("call", name))
        return out


def tool_call_payload(calls: list[ToolCall]) -> list[dict[str, Any]]:
    """OpenAI ``tool_calls`` entries for finished calls."""
    return [
        {"id": call.id, "type": "function", "function": {"name": call.name, "arguments": call.arguments}}
        for call in calls
    ]


def _marker_prefix_len(text: str) -> int:
    """Longest suffix of ``text`` that is a proper prefix of ``MARKER``."""
    for size in range(min(len(MARKER) - 1, len(text)), 0, -1):
        if MARKER.startswith(text[-size:]):
            return size
    return 0
