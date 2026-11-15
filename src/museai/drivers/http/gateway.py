"""muse.ai gateway transport: WebSocket + Noise XX, no browser.

The muse.ai web client reaches its agent VM through the ``hatch`` gateway:

    browser -> wss://hatch.metaaivm.com/v1/noise -> (Noise XX) -> daemon HTTP RPC

Everything here is verified against the live gateway (see ``docs/protocol.md``):

1. ``GET /thread/new`` embeds ``window.__HATCH_GATEWAY_INIT__`` (``vmName``, ``gatewayUrl``).
2. ``POST /api/hatch/token`` returns the edge JWT; ``POST /api/hatch/noise-notary-token``
   returns the notary token. Both require the browser ``Sec-Fetch-*`` headers.
3. The socket performs ``Noise_XX_25519_AESGCM_SHA256`` with empty handshake payloads.
4. Every transport message is a ``NoiseTransportFrame`` carrying a chunk of a
   ``ServiceRequest{payload: ServiceFrame{stream_id, request}}``; the server answers with
   ``ServiceResponse{payload: ServiceFrame{stream_id, response | body_chunk | reset}}``.
5. ``POST /chat/stream`` opens a chat and returns its ``session_id``;
   ``POST /chat/subscribe`` then streams newline-delimited JSON events until
   ``delta.message_done``.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
from noise.connection import Keypair, NoiseConnection
from websockets.asyncio.client import connect

from ...accounts.model import Account
from ...config import Settings
from ...errors import UpstreamAuthError, UpstreamError, UpstreamTimeout
from ...upstream import muse

INIT_MARKER = "__HATCH_GATEWAY_INIT__="
GATEWAY_HOST = "hatch.metaaivm.com"
NOISE_PATH = "/v1/noise"
APP_ID = "muse-web"
APP_VERSION = "1"
MAX_CHUNK = 65489
"""Largest payload the gateway accepts in one ``NoiseTransportFrame``."""

FETCH_HEADERS = {
    # /api/hatch/* answer 403 without these; the web client always sends them.
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
    "Accept": "*/*",
}


# --------------------------------------------------------------------------- protobuf


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _read_varint(data: bytes, i: int) -> tuple[int, int]:
    shift = value = 0
    while True:
        byte = data[i]
        i += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, i
        shift += 7


def pb_int(field: int, value: int) -> bytes:
    return _varint(field << 3) + _varint(value)


def pb_bytes(field: int, value: bytes) -> bytes:
    return _varint((field << 3) | 2) + _varint(len(value)) + value


def pb_string(field: int, value: str) -> bytes:
    return pb_bytes(field, value.encode())


def pb_parse(data: bytes) -> dict[int, Any]:
    """Decode one protobuf message level. Repeated fields collapse to the last value.

    Only the wire types the gateway uses are supported: varint and length-delimited.
    """
    out: dict[int, Any] = {}
    i = 0
    while i < len(data):
        key, i = _read_varint(data, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            out[field], i = _read_varint(data, i)
        elif wire == 2:
            size, i = _read_varint(data, i)
            out[field] = data[i : i + size]
            i += size
        else:
            raise UpstreamError(f"unsupported protobuf wire type {wire}")
    return out


# --------------------------------------------------------------------------- frames


def encode_frames(
    stream_id: int, chunk_id: int, payload: bytes, index: int = 0, total: int = 1
) -> bytes:
    """One ``NoiseTransportFrame``: chunk_id, chunk_index, total_chunks, payload."""
    return b"".join(
        (
            pb_int(1, chunk_id),
            pb_int(2, index),
            pb_int(3, total),
            pb_bytes(4, payload),
        )
    )


def decode_reply(plain: bytes) -> tuple[int, str, dict[str, Any]]:
    """Unwrap ``NoiseTransportFrame -> ServiceResponse -> ServiceFrame``.

    Returns ``(stream_id, kind, fields)`` with kind ``response``/``body_chunk``/``reset``.
    """
    transport = pb_parse(plain)
    service = pb_parse(transport.get(4, b""))
    frame = pb_parse(service.get(1, b""))
    for field, kind in ((3, "response"), (4, "body_chunk"), (5, "reset")):
        if field in frame:
            return int(frame.get(1, 0)), kind, pb_parse(frame[field])
    return int(frame.get(1, 0)), "unknown", {}


def application_request(verb: str, path: str, body: bytes = b"") -> bytes:
    """``ApplicationRequest{verb, path, headers, body, end_body}``."""
    headers = [pb_bytes(3, b"".join((pb_string(1, "x-request-id"), pb_string(2, str(uuid.uuid4())))))]
    if body:
        headers.append(
            pb_bytes(3, b"".join((pb_string(1, "content-type"), pb_string(2, "application/json"))))
        )
    return b"".join(
        (
            pb_string(1, verb),
            pb_string(2, path),
            *headers,
            pb_bytes(4, body) if body else b"",
            pb_int(5, 1),
        )
    )


def service_frame(stream_id: int, request: bytes) -> bytes:
    return b"".join((pb_int(1, stream_id), pb_bytes(2, request)))


# --------------------------------------------------------------------------- bootstrap


class GatewayCredentials:
    """Edge JWT + notary token + VM name for one account."""

    __slots__ = ("vm_name", "vm_address", "token", "notary")

    def __init__(self, vm_name: str, vm_address: str, token: str, notary: str) -> None:
        self.vm_name = vm_name
        self.vm_address = vm_address
        self.token = token
        self.notary = notary


def parse_gateway_init(page: str) -> dict[str, Any]:
    idx = page.find(INIT_MARKER)
    if idx < 0:
        raise UpstreamAuthError("gateway init marker missing from /thread/new")
    start = idx + len(INIT_MARKER)
    # The bootstrap is embedded in RSC text, so inner quotes arrive escaped.
    raw, _end = json.JSONDecoder().raw_decode(page[start:].replace('\\"', '"'))
    return raw


async def bootstrap(client: httpx.AsyncClient) -> GatewayCredentials:
    init = parse_gateway_init((await client.get("/thread/new")).text)
    vm = init["vmName"]
    token = await client.post("/api/hatch/token", json={"vmAddress": init["gatewayUrl"]}, headers=FETCH_HEADERS)
    notary = await client.post("/api/hatch/noise-notary-token", json={"vmName": vm}, headers=FETCH_HEADERS)
    if token.status_code != 200 or notary.status_code != 200:
        raise UpstreamAuthError(
            f"gateway bootstrap failed (token={token.status_code}, notary={notary.status_code})"
        )
    return GatewayCredentials(vm, init["gatewayUrl"], token.json()["token"], notary.json()["notaryToken"])


def ws_url(cred: GatewayCredentials) -> str:
    query = (
        f"vm_id={cred.vm_name}&auth_token={cred.token}&notary_token={cred.notary}"
        f"&app_id={APP_ID}&app_version={APP_VERSION}&request_id={uuid.uuid4()}"
    )
    return f"wss://{GATEWAY_HOST}{NOISE_PATH}?{query}"


def _static_key(account: Account) -> bytes:
    """Persist the Noise static key on the account so the gateway sees one identity."""
    stored = account.meta.get("noise_static_priv")
    if stored:
        return base64.b64decode(stored)
    key = os.urandom(32)  # the Noise stack derives the public half
    account.meta["noise_static_priv"] = base64.b64encode(key).decode()
    return key


# --------------------------------------------------------------------------- session


class GatewaySession:
    """One Noise channel to one account's agent VM."""

    def __init__(self, ws, noise: NoiseConnection, timeout: float) -> None:
        self._ws = ws
        self._noise = noise
        self._timeout = timeout
        self._streams: dict[int, asyncio.Queue[tuple[str, dict[str, Any]]]] = {}

    async def call(self, verb: str, path: str, body: bytes = b"") -> int:
        """Send one RPC and register a queue for its frames; returns the stream id."""
        stream_id = uuid.uuid4().int & 0x7FFFFFFF
        self._streams[stream_id] = asyncio.Queue()
        # ServiceRequest{service=daemon(default), payload}; the wrapper is mandatory.
        payload = pb_bytes(2, service_frame(stream_id, application_request(verb, path, body)))
        total = max(1, -(-len(payload) // MAX_CHUNK))
        chunk_id = uuid.uuid4().int & 0x7FFFFFFF
        for index in range(total):
            chunk = payload[index * MAX_CHUNK : (index + 1) * MAX_CHUNK]
            await self._ws.send(self._noise.encrypt(encode_frames(stream_id, chunk_id, chunk, index, total)))
        return stream_id

    async def frames(self, stream_id: int) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """Yield ``(kind, fields)`` for one stream until it resets."""
        queue = self._streams[stream_id]
        while True:
            kind, fields = await queue.get()
            yield kind, fields
            if kind == "reset":
                return

    @contextlib.asynccontextmanager
    async def reading(self, deadline: float) -> AsyncIterator[None]:
        """Pump the socket for the duration of the block; a stall raises ``UpstreamTimeout``."""
        pump = asyncio.create_task(self.pump(deadline))
        try:
            yield
        finally:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump

    async def pump(self, deadline: float) -> None:
        """Read transport frames and fan them out to per-stream queues.

        Runs until ``deadline``; raises ``UpstreamTimeout`` when nothing arrives.
        """
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise UpstreamTimeout("gateway stream timed out")
            try:
                raw = await asyncio.wait_for(self._ws.recv(), remaining)
            except asyncio.TimeoutError as exc:
                raise UpstreamTimeout("gateway stream timed out") from exc
            if not isinstance(raw, bytes):
                continue
            stream_id, kind, fields = decode_reply(self._noise.decrypt(raw))
            queue = self._streams.get(stream_id)
            if queue is not None:
                await queue.put((kind, fields))

    async def aclose(self) -> None:
        await self._ws.close()


async def open_session(account: Account, settings: Settings) -> GatewaySession:
    """Bootstrap credentials, dial the edge host and complete the Noise handshake."""
    limits = httpx.Limits(max_connections=settings.http_max_connections)
    headers = {"User-Agent": muse.USER_AGENT, "Cookie": muse.cookie_header(account.cookies)}
    async with httpx.AsyncClient(
        base_url=muse.SITE,
        headers=headers,
        timeout=settings.http_timeout,
        limits=limits,
        follow_redirects=True,
    ) as client:
        cred = await bootstrap(client)

    ws = await connect(ws_url(cred), max_size=None, ping_interval=None, open_timeout=settings.http_timeout)
    noise = NoiseConnection.from_name(b"Noise_XX_25519_AESGCM_SHA256")
    noise.set_as_initiator()
    noise.set_keypair_from_private_bytes(Keypair.STATIC, _static_key(account))
    noise.start_handshake()
    await ws.send(noise.write_message(b""))
    msg2 = await ws.recv()
    if not isinstance(msg2, bytes):
        await ws.close()
        raise UpstreamError("gateway sent a non-binary handshake reply")
    noise.read_message(msg2)
    await ws.send(noise.write_message(b""))
    return GatewaySession(ws, noise, settings.http_timeout)


# --------------------------------------------------------------------------- chat


def chat_body(prompt: str) -> bytes:
    return json.dumps({"items": [{"type": "text", "text": prompt}]}).encode()


async def stream_chat(
    session: GatewaySession, prompt: str, deadline: float
) -> AsyncIterator[dict[str, Any]]:
    """Yield agent events for one prompt: chat.stream, then chat.subscribe."""
    async with session.reading(deadline):
        stream_id = await session.call("POST", "/chat/stream", chat_body(prompt))
        session_id = ""
        async for kind, fields in session.frames(stream_id):
            if kind == "body_chunk":
                text = fields.get(1, b"").decode("utf-8", "replace")
                if "session_id" in text:
                    session_id = json.loads(text)["session_id"]
                    break
            elif kind == "reset":
                raise UpstreamError(f"chat.stream reset: {fields}")
        if not session_id:
            raise UpstreamError("chat.stream returned no session_id")

        sub_id = await session.call(
            "POST", "/chat/subscribe", json.dumps({"session_id": session_id}).encode()
        )
        async for kind, fields in session.frames(sub_id):
            if kind != "body_chunk":
                if kind == "reset":
                    raise UpstreamError(f"chat.subscribe reset: {fields}")
                continue
            for line in fields.get(1, b"").decode("utf-8", "replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                yield event
                if event.get("event") == "delta.message_done":
                    return


def text_delta(event: dict[str, Any]) -> str | None:
    """Assistant text carried by a streamed event, if any."""
    if event.get("event") != "delta.text_append":
        return None
    text = event.get("payload", {}).get("text")
    return text if isinstance(text, str) and text else None


__all__ = [
    "GatewaySession",
    "bootstrap",
    "decode_reply",
    "encode_frames",
    "open_session",
    "stream_chat",
    "text_delta",
]
