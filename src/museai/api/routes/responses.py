"""OpenAI Responses API (``/v1/responses``).

muse.ai has one text channel, so this is a thin projection of ``chat_stream``: the
``input`` items are flattened into the prompt, streamed text becomes
``response.output_text.delta`` events, and tool blocks become ``function_call`` items
(see :mod:`museai.core.tools`).
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from ...core.media import load_image_ref
from ...core.models import resolve_model
from ...core.prompt import flatten_messages, message_turns
from ...core.tools import ToolCallStream, render_prompt
from ...drivers.base import ChatRequest, InputImage
from ...errors import InvalidRequest, Muse2APIError
from ...services.container import Services
from ..deps import get_services, require_api_key
from ..schemas import ResponseCreateRequest

router = APIRouter(tags=["responses"], dependencies=[Depends(require_api_key)])


def _tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _messages(body: ResponseCreateRequest) -> list[dict[str, Any]]:
    """Normalise ``input`` (string or item list) into chat-style messages."""
    messages: list[dict[str, Any]] = []
    if body.instructions:
        messages.append({"role": "system", "content": body.instructions})
    items = body.input
    if isinstance(items, str):
        items = [{"role": "user", "content": items}]
    for item in items:
        role = item.get("role", "user")
        content = item.get("content", "")
        if isinstance(content, list):
            content = "\n".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") in (None, "input_text", "text")
            )
        messages.append({"role": role, "content": content})
    return messages


def _sse(event: dict[str, Any]) -> bytes:
    return f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n".encode()


def _message_item(response_id: str, text: str) -> dict[str, Any]:
    return {
        "id": f"msg_{uuid.uuid4().hex[:20]}",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def _function_call_item(call) -> dict[str, Any]:
    return {
        "id": f"fc_{uuid.uuid4().hex[:20]}",
        "type": "function_call",
        "call_id": call.id,
        "name": call.name,
        "arguments": call.arguments,
        "status": "completed",
    }


@router.post("/v1/responses")
async def create_response(body: ResponseCreateRequest, request: Request,
                          svc: Services = Depends(get_services)):
    spec = resolve_model(body.model, "chat")
    messages = _messages(body)
    flat = flatten_messages(messages)
    if not flat.text and not flat.images:
        raise InvalidRequest("input contains no text or images")

    prompt = flat.text
    if body.tools:
        prompt = f"{prompt}\n\n{render_prompt(body.tools, body.tool_choice)}"
    images = [InputImage(*(await load_image_ref(ref))) for ref in flat.images]
    cancel = asyncio.Event()
    req = ChatRequest(
        prompt=prompt,
        model=spec.id,
        images=images,
        timeout=svc.settings.chat_timeout,
        first_token_timeout=svc.settings.first_token_timeout,
        conversation_hint=body.user,
        turns=message_turns(messages),
        cancel=cancel,
    )
    stream = svc.gateway.chat_stream(req)
    response_id = f"resp_{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    model_name = body.model or spec.id
    parser = ToolCallStream() if body.tools else None

    if not body.stream:
        raw = "".join([delta async for delta in stream])
        text = raw
        calls = []
        if parser:
            text = "".join(v for k, v in parser.feed(raw) if k == "text")
            text += "".join(v for k, v in parser.flush() if k == "text")
            calls = parser.calls
        output: list[dict[str, Any]] = []
        if text:
            output.append(_message_item(response_id, text))
        output.extend(_function_call_item(call) for call in calls)
        usage = {
            "input_tokens": _tokens(flat.text),
            "output_tokens": _tokens(text),
        }
        usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
        await _record_usage(svc, request, model_name, flat.text, text)
        return {
            "id": response_id,
            "object": "response",
            "created_at": created,
            "status": "completed",
            "model": model_name,
            "output": output,
            "output_text": text,
            "parallel_tool_calls": False,
            "tool_choice": body.tool_choice if body.tool_choice is not None else "auto",
            "tools": body.tools or [],
            "usage": usage,
        }

    sequence = 0

    def event(kind: str, **fields: Any) -> bytes:
        nonlocal sequence
        sequence += 1
        return _sse({"type": kind, "sequence_number": sequence, **fields})

    async def events() -> AsyncIterator[bytes]:
        item_id = f"msg_{uuid.uuid4().hex[:20]}"
        text = ""
        emitted_text = False
        try:
            yield event(
                "response.created",
                response={"id": response_id, "object": "response", "created_at": created,
                          "status": "in_progress", "model": model_name, "output": []},
            )
            iterator = stream.__aiter__()
            while True:
                try:
                    delta = await asyncio.wait_for(
                        anext(iterator), timeout=svc.settings.stream_ping_interval
                    )
                except TimeoutError:
                    yield b": ping\n\n"
                    continue
                except StopAsyncIteration:
                    break
                parts = parser.feed(delta) if parser else [("text", delta)]
                for kind, value in parts:
                    if kind != "text" or not value:
                        continue
                    if not emitted_text:
                        emitted_text = True
                        yield event(
                            "response.output_item.added",
                            output_index=0,
                            item={"id": item_id, "type": "message", "role": "assistant",
                                  "status": "in_progress", "content": []},
                        )
                        yield event(
                            "response.content_part.added",
                            item_id=item_id,
                            output_index=0,
                            content_index=0,
                            part={"type": "output_text", "text": "", "annotations": []},
                        )
                    text += value
                    yield event("response.output_text.delta", item_id=item_id,
                                output_index=0, content_index=0, delta=value)
            if parser:
                for kind, value in parser.flush():
                    if kind == "text" and value:
                        text += value
                        yield event("response.output_text.delta", item_id=item_id,
                                    output_index=0, content_index=0, delta=value)
            if emitted_text:
                yield event("response.output_text.done", item_id=item_id, output_index=0,
                            content_index=0, text=text)
                yield event("response.output_item.done", output_index=0,
                            item=_message_item(response_id, text))
            for call in (parser.calls if parser else []):
                yield event("response.function_call_arguments.done",
                            item_id=f"fc_{call.id}", output_index=0, arguments=call.arguments)
            yield event(
                "response.completed",
                response={
                    "id": response_id,
                    "object": "response",
                    "created_at": created,
                    "status": "completed",
                    "model": model_name,
                    "output_text": text,
                    "output": [_message_item(response_id, text)] if text else [],
                    "usage": {"input_tokens": _tokens(flat.text),
                              "output_tokens": _tokens(text),
                              "total_tokens": _tokens(flat.text) + _tokens(text)},
                },
            )
        except Muse2APIError as exc:
            yield event("response.failed", response={"id": response_id, "status": "failed",
                                                     "error": exc.to_body()["error"]})
        finally:
            cancel.set()
            await stream.aclose()
            await _record_usage(svc, request, model_name, flat.text, text)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def _record_usage(
    svc: Services, request: Request, model: str, prompt: str, completion: str
) -> None:
    if svc.usage is None:
        return
    await svc.usage.record(
        key_hash=getattr(request.state, "key_hash", ""),
        route="/v1/responses",
        model=model,
        prompt_tokens=_tokens(prompt),
        completion_tokens=_tokens(completion),
    )
