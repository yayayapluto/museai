"""Probe the muse.ai gateway protocol (WebSocket + Noise XX) without a browser.

Runs the exact bootstrap the web client uses:

1. ``GET /thread/new`` with the account cookies and read ``window.__HATCH_GATEWAY_INIT__``
   (``gatewayUrl``, ``vmName``).
2. ``POST /api/hatch/token`` -> edge JWT, ``POST /api/hatch/noise-notary-token`` -> notary.
3. ``wss://hatch.metaaivm.com/v1/noise?vm_id=..&auth_token=..&notary_token=..`` and a
   ``Noise_XX_25519_AESGCM_SHA256`` handshake as initiator.
4. ``POST /chat/stream`` to create/continue a chat, then ``POST /chat/subscribe`` with the
   returned ``session_id`` to receive the agent event stream (SSE over Noise frames).

Usage::

    python scripts/probe_gateway.py --message "Name three primary colours, one line."

Findings belong in ``docs/protocol.md``; the decrypted event stream is dumped to
``docs/protocol-frames.jsonl``. Exit code 0 only when at least one event arrived.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from noise.connection import Keypair, NoiseConnection  # noqa: E402
from websockets.asyncio.client import connect  # noqa: E402

from museai.upstream import muse  # noqa: E402

INIT_MARKER = "__HATCH_GATEWAY_INIT__="
DATA_DIR = Path(os.environ.get("MUSE2API_DATA_DIR", "data"))

FETCH_HEADERS = {
    # /api/hatch/* reject requests without these (403); the browser always sends them.
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
    "Accept": "*/*",
}
GATEWAY_HOST = "hatch.metaaivm.com"  # every VM tunnel shares this edge host
NOISE_PATH = "/v1/noise"
APP_ID = "muse-web"
APP_VERSION = "1"


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


def _tag(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def pb_int(field: int, value: int) -> bytes:
    return _tag(field, 0) + _varint(value)


def pb_bool(field: int, value: bool) -> bytes:
    return pb_int(field, 1 if value else 0)


def pb_bytes(field: int, value: bytes) -> bytes:
    return _tag(field, 2) + _varint(len(value)) + value


def pb_string(field: int, value: str) -> bytes:
    return pb_bytes(field, value.encode())


def pb_message(*fields: bytes) -> bytes:
    return b"".join(fields)


def pb_parse(data: bytes) -> list[tuple[int, int | bytes]]:
    out: list[tuple[int, int | bytes]] = []
    i = 0
    while i < len(data):
        key, i = _read_varint(data, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            val, i = _read_varint(data, i)
            out.append((field, val))
        elif wire == 2:
            size, i = _read_varint(data, i)
            out.append((field, data[i : i + size]))
            i += size
        elif wire == 5:
            out.append((field, data[i : i + 4]))
            i += 4
        elif wire == 1:
            out.append((field, data[i : i + 8]))
            i += 8
        else:
            raise ValueError(f"unsupported wire type {wire}")
    return out


def noise_frame(chunk_id: int, index: int, total: int, payload: bytes) -> bytes:
    """Encode ``ingress_rev_proxy.NoiseTransportFrame`` (noise_transport.proto).

    Fields: 1 chunk_id int64, 2 chunk_index uint32, 3 total_chunks uint32, 4 payload bytes.
    """
    return pb_message(
        pb_int(1, chunk_id), pb_int(2, index), pb_int(3, total), pb_bytes(4, payload)
    )


def application_request(
    verb: str, path: str, body: bytes = b"", request_id: str | None = None
) -> bytes:
    """Encode ``hatch.noise.ApplicationRequest``."""
    headers = [pb_bytes(3, pb_message(pb_string(1, "x-request-id"), pb_string(2, request_id or str(uuid.uuid4()))))]
    if body:
        headers.append(
            pb_bytes(
                3,
                pb_message(
                    pb_string(1, "content-type"), pb_string(2, "application/json")
                ),
            )
        )
    return pb_message(
        pb_string(1, verb),
        pb_string(2, path),
        *headers,
        pb_bytes(4, body) if body else b"",
        pb_bool(5, True),
    )


def service_frame(stream_id: int, request: bytes) -> bytes:
    """``hatch.noise.ServiceFrame{stream_id, request}``."""
    return pb_message(pb_int(1, stream_id), pb_bytes(2, request))


def service_request(frame: bytes) -> bytes:
    """``hatch.noise.ServiceRequest{service=daemon, payload}``; service 0 is the proto3 default."""
    return pb_bytes(2, frame)


def decode_reply(plain: bytes) -> tuple[int, str, dict[str, Any]]:
    """Unwrap NoiseTransportFrame -> ServiceResponse -> ServiceFrame.

    Returns ``(stream_id, kind, payload)`` where kind is ``response``/``body_chunk``/``reset``.
    """
    transport = dict(pb_parse(plain))
    response = dict(pb_parse(transport.get(4, b"")))
    frame = dict(pb_parse(response.get(1, b"")))
    for field, kind in ((3, "response"), (4, "body_chunk"), (5, "reset")):
        if field in frame:
            return int(frame.get(1, 0)), kind, dict(pb_parse(frame[field]))
    return int(frame.get(1, 0)), "unknown", {}


# --------------------------------------------------------------------------- bootstrap


def parse_gateway_init(page: str) -> dict[str, Any]:
    """The RSC payload embeds ``window.__HATCH_GATEWAY_INIT__={...}``; braces nest."""
    idx = page.find(INIT_MARKER)
    if idx < 0:
        raise SystemExit(f"{INIT_MARKER!r} not found in /thread/new")
    start = idx + len(INIT_MARKER)
    # The bootstrap is embedded inside an RSC string, so quotes arrive as \" .
    raw, _end = json.JSONDecoder().raw_decode(page[start:].replace('\\"', '"'))
    return raw


def load_account(account_id: str | None) -> dict[str, Any]:
    accounts = json.loads((DATA_DIR / "accounts.json").read_text(encoding="utf-8"))
    if account_id:
        accounts = [a for a in accounts if a["id"] == account_id]
    for acc in accounts:
        if acc.get("enabled", True):
            return acc
    raise SystemExit("no enabled account in data/accounts.json")


def bootstrap(client: httpx.Client) -> dict[str, Any]:
    """Collect vm id, edge JWT and notary token the same way the web client does."""
    init = parse_gateway_init(client.get(f"{muse.SITE}/thread/new").text)
    vm = init["vmName"]
    vm_address = init["gatewayUrl"]
    token = client.post(
        f"{muse.SITE}/api/hatch/token", json={"vmAddress": vm_address}, headers=FETCH_HEADERS
    )
    notary = client.post(
        f"{muse.SITE}/api/hatch/noise-notary-token", json={"vmName": vm}, headers=FETCH_HEADERS
    )
    init["vmAddress"] = vm_address
    init["authToken"] = token.json().get("token") if token.status_code == 200 else None
    init["tokenStatus"] = token.status_code
    init["notaryToken"] = notary.json().get("notaryToken") if notary.status_code == 200 else None
    init["notaryStatus"] = notary.status_code
    return init


def ws_url(init: dict[str, Any]) -> str:
    query = [
        f"vm_id={init['vmName']}",
        f"auth_token={init['authToken']}",
        f"notary_token={init['notaryToken']}",
        f"app_id={APP_ID}",
        f"app_version={APP_VERSION}",
        f"request_id={uuid.uuid4()}",
    ]
    return f"wss://{GATEWAY_HOST}{NOISE_PATH}?{'&'.join(query)}"


def noise_static(acc: dict[str, Any]) -> bytes:
    """Persist the client static key so repeated probes reuse one identity."""
    meta = acc.setdefault("meta", {})
    stored = meta.get("noise_static_priv")
    if stored:
        return base64.b64decode(stored)
    key = os.urandom(32)  # the Noise stack derives the public half
    meta["noise_static_priv"] = base64.b64encode(key).decode()
    return key


async def recv(ws) -> bytes | None:
    try:
        frame = await asyncio.wait_for(ws.recv(), 25.0)
    except asyncio.TimeoutError:
        return None
    return frame if isinstance(frame, bytes) else frame.encode()


async def open_noise(ws, acc: dict[str, Any]) -> NoiseConnection:
    noise = NoiseConnection.from_name(b"Noise_XX_25519_AESGCM_SHA256")
    noise.set_as_initiator()
    noise.set_keypair_from_private_bytes(Keypair.STATIC, noise_static(acc))
    noise.start_handshake()
    await ws.send(noise.write_message(b""))
    msg2 = await recv(ws)
    if msg2 is None:
        raise SystemExit("timeout waiting for handshake msg2")
    noise.read_message(msg2)
    await ws.send(noise.write_message(b""))
    return noise


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default=None)
    ap.add_argument("--message", default="Name three primary colours, one line.")
    ap.add_argument("--limit", type=int, default=12)
    ap.add_argument("--dump", type=Path, default=Path("docs/protocol-frames.jsonl"))
    args = ap.parse_args()

    acc = load_account(args.account)
    headers = {"User-Agent": muse.USER_AGENT, "Cookie": muse.cookie_header(acc["cookies"])}
    with httpx.Client(headers=headers, timeout=30, follow_redirects=True) as client:
        init = bootstrap(client)
    print("vm:", init.get("vmName"), "| token:", init.get("tokenStatus"), "| notary:", init.get("notaryStatus"))
    if not init.get("authToken") or not init.get("notaryToken"):
        print("bootstrap incomplete; aborting")
        return 2

    url = ws_url(init)
    events = 0
    with args.dump.open("w", encoding="utf-8") as dump:

        def record(kind: str, data: Any) -> None:
            dump.write(json.dumps({"kind": kind, "data": data}) + "\n")

        async with connect(url, max_size=None, ping_interval=None, open_timeout=20) as ws:
            print("ws open")
            noise = await open_noise(ws, acc)
            print("noise handshake complete")

            stream_id = uuid.uuid4().int & 0x7FFFFFFF
            body = json.dumps({"items": [{"type": "text", "text": args.message}]}).encode()
            request = application_request("POST", "/chat/stream", body)
            await ws.send(
                noise.encrypt(
                    noise_frame(stream_id, 0, 1, service_request(service_frame(stream_id, request)))
                )
            )
            print("sent chat.stream")

            session_id = None
            while session_id is None:
                raw = await recv(ws)
                if raw is None:
                    print("no chat.stream reply")
                    return 3
                sid, kind, payload = decode_reply(noise.decrypt(raw))
                if kind == "body_chunk":
                    text = payload.get(1, b"").decode("utf-8", "replace")
                    record("chat_stream_chunk", {"stream_id": sid, "text": text})
                    print("  chat.stream:", text)
                    if "session_id" in text:
                        session_id = json.loads(text).get("session_id")
                elif kind == "response":
                    print("  chat.stream status:", payload.get(1))
                elif kind == "reset":
                    print("  chat.stream reset:", payload)
                    return 4

            print("session:", session_id)
            sub_id = uuid.uuid4().int & 0x7FFFFFFF
            sub_body = json.dumps({"session_id": session_id}).encode()
            sub_req = application_request("POST", "/chat/subscribe", sub_body)
            await ws.send(
                noise.encrypt(noise_frame(sub_id, 0, 1, service_request(service_frame(sub_id, sub_req))))
            )
            print("sent chat.subscribe")

            while events < args.limit:
                raw = await recv(ws)
                if raw is None:
                    print("stream idle; stopping")
                    break
                sid, kind, payload = decode_reply(noise.decrypt(raw))
                if kind != "body_chunk":
                    if kind == "reset":
                        print("  reset:", payload)
                        break
                    continue
                text = payload.get(1, b"").decode("utf-8", "replace").strip()
                record("chat_event", {"stream_id": sid, "text": text})
                print("  event:", text[:200])
                events += 1
    print(f"events: {events}")
    return 0 if events else 5


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
