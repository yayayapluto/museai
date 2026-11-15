from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from ...core.media import load_image_ref
from ...core.models import resolve_model
from ...core.prompt import flatten_messages, message_turns
from ...core.tools import ToolCallStream, render_prompt, tool_call_payload
from ...drivers.base import ChatRequest, InputImage
from ...errors import InvalidRequest, Muse2APIError
from ...services.container import Services
from ..deps import get_services, require_api_key
from ..schemas import ChatCompletionRequest

log = logging.getLogger(__name__)
router = APIRouter(tags=["chat"], dependencies=[Depends(require_api_key)])


def _approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _sse(payload: dict | str) -> bytes:
    data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return f"data: {data}\n\n".encode()


@router.post("/v1/chat/completions")
async def chat_completions(body: ChatCompletionRequest, request: Request,
                           svc: Services = Depends(get_services)):
    spec = resolve_model(body.model, "chat")
    flat = flatten_messages([m.model_dump() for m in body.messages])
    if not flat.text and not flat.images:
        raise InvalidRequest("messages contain no text or images")

    images = [InputImage(*(await load_image_ref(ref))) for ref in flat.images]
    cancel = asyncio.Event()
    prompt = flat.text
    if body.tools:
        prompt = f"{prompt}\n\n{render_prompt(body.tools, body.tool_choice)}"
    req = ChatRequest(
        prompt=prompt,
        model=spec.id,
        images=images,
        timeout=svc.settings.chat_timeout,
        first_token_timeout=svc.settings.first_token_timeout,
        conversation_hint=body.user,
        turns=message_turns([m.model_dump() for m in body.messages]),
        cancel=cancel,
    )
    completion_id = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())
    model_name = body.model or spec.id
    stream = svc.gateway.chat_stream(req)

    if not body.stream:
        text = "".join([d async for d in stream])
        calls = []
        if body.tools:
            parser = ToolCallStream()
            kept = "".join(value for kind, value in parser.feed(text) if kind == "text")
            kept += "".join(value for kind, value in parser.flush() if kind == "text")
            text = kept
            calls = tool_call_payload(parser.calls)
        await _record_usage(svc, request, model_name, flat.text, text)
        message: dict = {"role": "assistant", "content": text or None}
        if calls:
            message["tool_calls"] = calls
        return {
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": model_name,
            "choices": [{"index": 0, "message": message,
                         "finish_reason": "tool_calls" if calls else "stop"}],
            "usage": {
                "prompt_tokens": _approx_tokens(flat.text),
                "completion_tokens": _approx_tokens(text),
                "total_tokens": _approx_tokens(flat.text) + _approx_tokens(text),
            },
        }

    # Pull the first delta before committing to a 200 so that account/upstream
    # failures surface as proper HTTP errors instead of a broken event stream.
    parse_tools = bool(body.tools)
    parser = ToolCallStream() if parse_tools else None
    try:
        first = await anext(stream)
    except StopAsyncIteration:
        first = ""

    def chunk(delta: dict, finish: str | None = None) -> dict:
        return {"id": completion_id, "object": "chat.completion.chunk", "created": created,
                "model": model_name,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

    async def events() -> AsyncIterator[bytes]:
        produced = ""
        role_sent = False

        def text_chunks(pending: list[tuple[str, str]]) -> list[bytes]:
            nonlocal produced, role_sent
            out: list[bytes] = []
            for kind, value in pending:
                if kind != "text" or not value:
                    continue
                produced += value
                delta: dict = {"content": value}
                if not role_sent:
                    delta["role"] = "assistant"
                    role_sent = True
                out.append(_sse(chunk(delta)))
            return out

        try:
            initial = parser.feed(first) if parser else [("text", first)]
            for payload in text_chunks(initial):
                yield payload
            iterator = stream.__aiter__()
            while True:
                try:
                    delta_text = await asyncio.wait_for(
                        anext(iterator), timeout=svc.settings.stream_ping_interval
                    )
                except TimeoutError:
                    yield b": ping\n\n"  # keep proxies from closing an idle stream
                    continue
                except StopAsyncIteration:
                    break
                pending = parser.feed(delta_text) if parser else [("text", delta_text)]
                for payload in text_chunks(pending):
                    yield payload
            if parser:
                for payload in text_chunks(parser.flush()):
                    yield payload
                for index, call in enumerate(parser.calls):
                    yield _sse(
                        chunk(
                            {
                                "tool_calls": [
                                    {
                                        "index": index,
                                        "id": call.id,
                                        "type": "function",
                                        "function": {
                                            "name": call.name,
                                            "arguments": call.arguments,
                                        },
                                    }
                                ]
                            }
                        )
                    )
            yield _sse(chunk({}, "tool_calls" if parser and parser.calls else "stop"))
            if (body.stream_options or {}).get("include_usage"):
                usage = {"prompt_tokens": _approx_tokens(flat.text),
                         "completion_tokens": _approx_tokens(produced)}
                usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
                yield _sse({"id": completion_id, "object": "chat.completion.chunk",
                            "created": created, "model": model_name, "choices": [],
                            "usage": usage})
        except Muse2APIError as exc:
            yield _sse(exc.to_body())
        except Exception:  # noqa: BLE001
            log.exception("stream crashed")
            yield _sse({"error": {"message": "internal error", "type": "server_error"}})
        finally:
            cancel.set()
            await stream.aclose()
            await _record_usage(svc, request, model_name, flat.text, produced)
        yield _sse("[DONE]")

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _emit(parser: ToolCallStream, delta: str) -> list[tuple[str, str]]:
    """Feed one upstream delta through the tool parser."""
    return parser.feed(delta)


async def _record_usage(
    svc: Services, request: Request, model: str, prompt: str, completion: str
) -> None:
    if svc.usage is None:
        return
    await svc.usage.record(
        key_hash=getattr(request.state, "key_hash", ""),
        route="/v1/chat/completions",
        model=model,
        prompt_tokens=_approx_tokens(prompt),
        completion_tokens=_approx_tokens(completion),
    )
