# muse.ai gateway protocol

How the muse.ai web client reaches its agent VM, and how `museai`'s HTTP driver
reproduces it without a browser. Every statement below was verified against the live
gateway on 2026-11-16 with `scripts/probe_gateway.py`; nothing here is guessed from
upstream documentation (there is none).

## 1. Bootstrap

The web app is a Next.js client that fetches three things before it can talk to a VM.

| Step | Request | Result |
| --- | --- | --- |
| 1 | `GET https://muse.ai/thread/new` (cookies) | HTML containing `window.__HATCH_GATEWAY_INIT__={...}` |
| 2 | `POST /api/hatch/token` `{"vmAddress": gatewayUrl}` | `{"token": "muse-secret-edge.<jwt>"}` |
| 3 | `POST /api/hatch/noise-notary-token` `{"vmName": vm}` | `{"notaryToken": "endorsement.v1...."}` |

`__HATCH_GATEWAY_INIT__` is embedded inside the RSC text stream, so its quotes arrive
escaped (`\"`); replace them before `json.JSONDecoder().raw_decode`. Useful fields:
`gatewayUrl` (per-VM host, `wss://<vm>.metaaivm.com/`), `vmName`, `authToken`.

Both `/api/hatch/*` endpoints answer **403** unless the request carries the browser
fetch metadata: `Sec-Fetch-Site: same-origin`, `Sec-Fetch-Mode: cors`,
`Sec-Fetch-Dest: empty`, `Accept: */*`.

The JWT payload carries `env_id` and `ot` (`hatch:<vm>`); the notary payload (part 3,
base64) carries `restrictions` such as `timeout:<ts>`, `hostname:<vm>.metaaivm.com`,
`uri-handle-prefix:/v1/noise`.

## 2. Transport

`<vm>.metaaivm.com` does **not** resolve publicly (NXDOMAIN from local resolver,
8.8.8.8 and 1.1.1.1). The dial target is the shared edge host:

```
wss://hatch.metaaivm.com/v1/noise
    ?vm_id=<vm>
    &auth_token=<edge jwt>
    &notary_token=<notary token>
    &app_id=muse-web
    &app_version=1
    &request_id=<uuid4>
```

Handshake: `Noise_XX_25519_AESGCM_SHA256`, client is the initiator, all three handshake
payloads are **empty** (`msg1`, `msg3`; `msg2` is the server's 132-byte attestation). The
client static key is long-lived and stored per account (`Account.meta["noise_static_priv"]`,
base64) — the gateway tracks identities across reconnects.

A non-empty `msg1` payload (`{2:1}`) is rejected: the socket closes before `msg2`.

## 3. Framing

Every WebSocket binary message is one Noise transport message whose plaintext is:

```
ingress_rev_proxy.NoiseTransportFrame     // noise_transport.proto
  1 chunk_id      int64
  2 chunk_index   uint32
  3 total_chunks  uint32
  4 payload       bytes    // <= 65489 per chunk
```

`payload` is direction dependent:

```
hatch.noise.ServiceRequest                // client -> server
  1 service  ServiceType   // 0 daemon, 1 sentinel, 2 vault, 3 authd, 4 ssh_access, 5 yolk
  2 payload  bytes         // serialised ServiceFrame

hatch.noise.ServiceResponse               // server -> client
  1 payload  bytes         // serialised ServiceFrame

hatch.noise.ServiceFrame
  1 stream_id  int64
  oneof kind {
    2 request     ApplicationRequest
    3 response    ApplicationResponse
    4 body_chunk  BodyChunk
    5 reset       Reset
  }

hatch.noise.ApplicationRequest
  1 verb      string    // "GET" | "POST" | ...
  2 path      string    // route path, query already expanded
  3 headers   repeated Header{1 key, 2 value}
  4 body      bytes     // JSON when present
  5 end_body  bool

hatch.noise.ApplicationResponse
  1 status    int32
  2 headers   repeated Header
  3 body      bytes
  4 end_body  bool

hatch.noise.BodyChunk
  1 data      bytes
  2 end_body  bool

hatch.noise.Reset
  1 code    Reset.Code  // 1 cancelled, 2 timeout, 3 protocol_error, 4 refused_stream,
                        // 5 internal_error, 6 service_unavailable
  2 reason  string
```

`service` 0 is the proto3 default and is therefore omitted from the wire.

Diagnostics come back as a `Reset` frame; an empty plaintext yields
`code=3, reason="empty frame kind"`, so protocol mistakes are visible rather than silent.

Compression: the browser zstd-compresses request bodies once they exceed 1024 bytes and
strictly for `POST` to non-webhook daemon paths (`Content-Encoding: zstd`). Plain identity
bodies are accepted, so the driver never compresses.

## 4. RPC routes

The client keeps a static route table; the entries the driver uses:

| Method | Verb + path | Notes |
| --- | --- | --- |
| `chat.stream` | `POST /chat/stream` | subscription stream; returns `session_id` |
| `chat.subscribe` | `POST /chat/subscribe` | subscription stream of agent events |
| `sessions.list` | `GET /api/session/list` | plain JSON response |

`POST /chat/stream` body:

```json
{"items": [{"type": "text", "text": "<user prompt>"}]}
```

The response body is a single JSON object:

```json
{"channel":"main","is_thread":false,"message_id":"…",
 "reply_to_message_id":"…","session_id":"b97e8249-…"}
```

`session_id` is the conversation. `POST /chat/subscribe` body:

```json
{"session_id": "b97e8249-…"}
```

Its response is `text/event-stream` delivered through `BodyChunk` frames: newline
delimited JSON events, several events per chunk, sometimes empty chunks.

### Event shape

```json
{"event": "<type>", "payload": {...}, "seq": 66, "ts_ms": 1794765321314, "type": "event"}
```

Observed event types:

| Event | Meaning |
| --- | --- |
| `delta.message_start` | assistant message begins; `payload.metadata.model` names the model |
| `delta.text_append` | `payload.text` is the next text delta (concatenate in `seq` order) |
| `delta.message_done` | turn finished; `payload.status` plus a full `payload.transcript` |
| `agent.status` | `payload.activity_code`: `working` / `responding` / `online` |
| `task.status` | `payload.active_subagent_count`, used for tool/sub-agent progress |
| `approvals.snapshot` | pending human approvals (empty in normal chat) |
| `model_lock.updated` | model selection state |

A turn ends at `delta.message_done` (or a `Reset` frame).

## 5. Consequences for this fork

- No Chromium, no CDP, no DOM selectors: the driver is socket + protobuf only.
- Latency is one WS round trip per turn plus the events; there is no page hydration wait.
- The browser driver's known P0s (locale-dependent selectors, cookie scope, tab leaks,
  session bleeding) do not exist on this path because there is no DOM.
- Failure modes are explicit: bootstrap 403 (missing `Sec-Fetch-*`), handshake close
  (bad tokens), `Reset` frames (protocol or routing errors).

## 6. Reproduction

```bash
python scripts/probe_gateway.py --message "Name three primary colours, one line."
```

Exit code 0 and a printed event stream means the protocol above still holds. The probe
writes the decrypted frames to `docs/protocol-frames.jsonl`, which the test suite replays.
