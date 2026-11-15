<h1 align="center">Muse AI</h1>

<p align="center">
  <b>OpenAI-compatible API gateway for <a href="https://muse.ai">muse.ai</a> personal workspaces, with native 9Router integration.</b>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10%2B-blue?logo=python" alt="Python" />
  <img src="https://img.shields.io/badge/FastAPI-async-009688?logo=fastapi" alt="FastAPI" />
  <img src="https://img.shields.io/badge/API-OpenAI%20Compatible-green" alt="OpenAI Compatible" />
  <img src="https://img.shields.io/badge/9Router-Ready-purple" alt="9Router Ready" />
  <img src="https://img.shields.io/badge/License-MIT-lightgrey" alt="License" />
</p>

---

`museai` exposes a personal muse.ai workspace — which is browser-only — as a standard
OpenAI API (`/v1/chat/completions`, `/v1/responses`, `/v1/images/generations`,
`/v1/videos`, `/v1/models`). The default `http` driver speaks the muse.ai gateway
protocol directly (WebSocket + Noise XX), so chat needs **no Chromium**; the optional
`browser` driver adds image and video generation. Point any OpenAI-compatible client at
it: OpenAI SDKs, LangChain, LobeChat, NextChat, Cherry Studio, or 9Router.

## Table of contents

- [Install](#install)
- [What you need](#what-you-need)
- [Key features](#key-features)
- [Tech stack](#tech-stack)
- [Onboarding walkthrough](ONBOARDING.md)
- [Account setup](#account-setup)
- [Usage](#usage)
- [Endpoints](#endpoints)
- [Models and aliases](#models-and-aliases)
- [Configuration reference](#configuration-reference)
- [Architecture](#architecture)
- [Gateway protocol](docs/protocol.md)
- [Development](#development)
- [Deployment](#deployment)
- [Troubleshooting](#troubleshooting)
- [Security notes](#security-notes)
- [Limitations and roadmap](#limitations-and-roadmap)
- [Contributing](#contributing)
- [License](#license)

---

## Key features

- **OpenAI wire compatibility** — drop-in replacement for OpenAI SDKs and any client
  that speaks `/v1/chat/completions` or `/v1/responses`. Streaming SSE and buffered
  JSON, with `: ping` keepalives on idle streams.
- **Multi-tenant mode** — `MUSE2API_ACCOUNT_STORE=sqlite` keeps accounts, hashed API
  keys and usage counters in one `data/museai.db`, with per-key rate limiting
  (`MUSE2API_RATE_LIMIT_RPM`) and `/admin/usage` aggregates.
- **Driver abstraction** — routes never touch the browser. Three drivers ship today:
  `mock` (offline echo, used by the test suite), `http` (muse.ai gateway protocol over
  WebSocket + Noise XX — no Chromium, ~65 MB RSS), and `browser` (headless Chromium
  over CDP, for media generation).
- **Agent parity** — `tools` / `tool_choice` are rendered into the prompt and parsed
  back out of the token stream into OpenAI `tool_calls`, and `/v1/responses` emits the
  Responses API event stream (`response.output_text.delta`, `response.completed`).
- **Account pool** — multiple muse.ai accounts with `affinity`, `lru`, or
  `round_robin` scheduling, per-account concurrency limits, cooldown on quota
  errors, and transparent failover (up to `MUSE2API_MAX_FAILOVER` retries).
- **Cookie injection that survives the edge proxy** — percent-encoded tokens are
  URL-decoded and registered for both `.muse.ai` and `muse.ai` scopes; the `datr`
  cookie is passed through so Meta's device-integrity redirect does not fire.
- **Image and video generation** — text-to-image, image-to-video with a first frame,
  plus asynchronous video tasks with progress polling.
- **9Router hook** — `scripts/connect_9router.py` registers the provider and model
  routes directly into 9Router's SQLite database.
- **Deterministic offline tests** — the whole suite runs on `MockDriver`, no network,
  no browser.

## Tech stack

| Layer | Choice |
|---|---|
| Language | Python 3.10+ |
| HTTP framework | FastAPI + Uvicorn |
| Schemas / settings | Pydantic 2 + pydantic-settings |
| Browser automation | Headless Chromium via Chrome DevTools Protocol (`websockets`, optional browser extra) |
| Gateway transport | `noise` (Noise XX protocol) + `websockets` + hand-rolled protobuf codec |
| Storage | JSON files by default; SQLite (`data/museai.db`) in multi-tenant mode |
| Upstream HTTP | `httpx` |
| Tests | `pytest` + `pytest-asyncio` (51 tests, ~2 s, 100% offline) |
| Lint / format | `ruff` |
| Packaging | Hatchling (`pip install -e .`) |
| CI | GitHub Actions, Python 3.10 + 3.12 |

## Install

Two commands, then a wizard. No hand-written `.env`, no manual JSON.

```bash
pipx install "museai[http] @ git+https://github.com/yayayapluto/museai.git"
```

```bash
muse2api setup      # imports your muse.ai cookies and verifies them live
muse2api start      # dashboard on http://127.0.0.1:18610
```

Windows without `pipx` yet:

```powershell
py -m pip install --user pipx
py -m pipx ensurepath        # open a NEW terminal afterwards
py -m pipx install "museai[http] @ git+https://github.com/yayayapluto/museai.git"
```

Working on the code instead (editable, gets `[dev]` too):

```bash
git clone https://github.com/yayayapluto/museai.git
cd museai
python -m venv .venv
# Windows: .venv\Scripts\Activate.ps1    macOS/Linux: source .venv/bin/activate
pip install -e ".[http,dev]"
muse2api setup
```

`muse2api setup` writes `.env` with `MUSE2API_DRIVER=http`, accepts the cookie export as
pasted JSON / `cookies.txt` / a `Cookie:` header (`--cookies FILE` and `--clipboard`
exist for scripts), verifies the session against muse.ai **before** saving it, and prints
ready-to-paste client settings.

Stuck on anything? `muse2api doctor` lists every check with the fix, and
[`ONBOARDING.md`](ONBOARDING.md) is the 5-minute walkthrough with a troubleshooting table.

| Client field | Value |
|---|---|
| Base URL | `http://127.0.0.1:18610/v1` |
| API key | printed by `muse2api setup`; also stored in `data/api_key` |
| Model | `muse-chat` (aliases: `gpt-4o`, `gpt-4o-mini`, `gpt-5`, `claude-3-5-sonnet`) |

```bash
curl http://127.0.0.1:18610/v1/chat/completions \
  -H "Authorization: Bearer <API_KEY>" -H "Content-Type: application/json" \
  -d '{"model":"muse-chat","messages":[{"role":"user","content":"say pong"}]}'
```

### What you need

- **Python 3.10+**. Nothing else for the default `http` driver — no Chromium, no
  browser profile, ~65 MB RSS.
- **An active muse.ai account.** Cookies come from the bundled extension
  (`chrome://extensions` → Developer mode → Load unpacked → `extension/`), or from any
  cookie exporter.
- **Chromium only if you want image or video generation** (`--driver browser`). Set
  `MUSE2API_CHROMIUM_PATH` on Windows; auto-detection covers the usual Linux and macOS
  locations.
- *(Optional)* [9Router](https://github.com/9router/9router) for the multi-model gateway.

### Commands

| Command | What it does |
|---|---|
| `muse2api setup` | writes `.env`, imports + verifies cookies, prints client snippets |
| `muse2api doctor` | checks driver, extras, data dir, accounts, cookie expiry, port; `--live` also runs the upstream handshake |
| `muse2api start` | server + live Rich dashboard (`--no-ui` for containers) |
| `muse2api start --driver browser` | Chromium mode, the only mode with image/video |
| `python -m museai` | server without the CLI wrapper |

`muse2api start` prints the configuration it actually resolved, which settles the usual
"why is it still using Chromium" question:

```text
config: driver=http data_dir=data cwd=E:\muse2api
```

A variable exported in your shell wins over `.env`. Check `echo $env:MUSE2API_DRIVER`
(PowerShell) or `printenv MUSE2API_DRIVER` before editing the file.

Dashboard, on a healthy instance:

```text
╭───────────────────────── muse2api ─────────────────────────╮
│ URL        http://127.0.0.1:18610                         │
│ Version    0.1.0 (driver=http)                            │
│ Uptime     00:01:07                                       │
│ API key    sk-mus…-key                                    │
│ Admin key  sk-mus…-key  stored in data\api_key            │
│ Store      json                                           │
│ Accounts   1 total · 1 available · 0 invalid · 0 in flight │
╰──────────────────────── Ctrl+C to stop ───────────────────╯
```

Health checks: `/healthz` → `{"ok":true,"version":"0.1.0"}`; `/readyz` reports
`ready:false` until an account is available.

## Account setup

`muse2api setup` covers the normal path. This section is the reference for what it does
and for the manual/advanced cases (multiple accounts, SQLite store).

`muse.ai` authenticates with browser cookies. Export them from a browser session already
logged in to `https://muse.ai` — the bundled extension (Copy cookies), DevTools →
`Application` → `Cookies` → `https://muse.ai`, or any exporter → JSON.

| Cookie | Required | Purpose |
|---|---|---|
| `hatch_sess` | yes | Session authorization token |
| `hatch_gw` | yes | Cluster gateway routing cookie |
| `hatch_native_auth_device` | yes | Device fingerprint UUID |
| `hatch_vml` | recommended | Workspace lease token; short-lived, minted per thread |
| `datr` | recommended | Meta device-integrity cookie, prevents login-page redirects |

Notes:

- Values may be percent-encoded (`v1%3A...`). This is fine — the driver URL-decodes
  them before injecting via CDP.
- All five cookies are served `HttpOnly`, so `document.cookie` in the browser console
  returns nothing useful — verified against a live export, `datr` included. Use the
  DevTools `Application` panel or a cookie-exporter extension instead of a console
  snippet.
- Cookies are persisted in plaintext in `data/accounts.json` (see
  [Security notes](#security-notes)).

### Import via admin API

```bash
curl -X POST http://127.0.0.1:18610/admin/accounts \
  -H "Authorization: Bearer sk-museai-admin-key" \
  -H "Content-Type: application/json" \
  -d '{
    "label": "primary",
    "cookies": {
      "hatch_sess": "...",
      "hatch_gw": "...",
      "hatch_native_auth_device": "...",
      "hatch_vml": "...",
      "datr": "..."
    },
    "cookie_expires": {"hatch_sess": 1797350030}
  }'
```

`cookie_expires` (optional) maps cookie name to a Unix expiry timestamp. The response
echoes `missing_cookies`; an empty list means the session cookie set is complete.
Cookie values are always masked in responses (`e6f6...XP3g`).

### Import with the CLI (any export format)

```bash
python scripts/import_cookies.py exported_cookies.json --label primary \
  --url http://127.0.0.1:18610 --key "$MUSE2API_ADMIN_KEY"
```

The CLI accepts Cookie-Editor JSON, `cookies.txt`, or a raw `Cookie` header, warns
about missing session cookies, and talks to the running instance. A Manifest V3
extension in [`extension/`](extension/) does the same from the browser toolbar
(load unpacked, paste the admin key, click *Send cookies*).

### Convert an export with the helper script

`scripts/extract_cookies.py` accepts a Chrome DevTools / Cookie-Editor JSON array, a
Netscape `cookies.txt`, or a raw `name=value; name=value` header string:

```bash
python scripts/extract_cookies.py exported_cookies.json --label primary --out account.json
curl -X POST http://127.0.0.1:18610/admin/accounts \
  -H "Authorization: Bearer sk-museai-admin-key" \
  -H "Content-Type: application/json" \
  -d @account.json
```

After importing, confirm the pool and the tab count:

```bash
curl -H "Authorization: Bearer sk-museai-admin-key" http://127.0.0.1:18610/admin/status
```

## Usage

### Streaming chat (curl)

```bash
curl -N -X POST http://127.0.0.1:18610/v1/chat/completions \
  -H "Authorization: Bearer sk-museai-local-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "muse-chat",
    "stream": true,
    "messages": [
      {"role": "system", "content": "You are a concise engineering assistant."},
      {"role": "user", "content": "Explain raft consensus in two sentences."}
    ]
  }'
```

The stream is standard OpenAI SSE and ends with `data: [DONE]`.

### Python SDK

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:18610/v1", api_key="sk-museai-local-key")

response = client.chat.completions.create(
    model="muse-chat",
    messages=[{"role": "user", "content": "Write a thread-safe singleton in Python"}],
)
print(response.choices[0].message.content)
```

### Images

```bash
curl -X POST http://127.0.0.1:18610/v1/images/generations \
  -H "Authorization: Bearer sk-museai-local-key" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "a red bicycle at golden hour", "n": 1, "response_format": "url"}'
```

`response_format` is `url` (media is written to `data/media/` and served from
`/v1/media/{name}`) or `b64_json`.

### Videos

```bash
# Dispatch a task
curl -X POST http://127.0.0.1:18610/v1/videos \
  -H "Authorization: Bearer sk-museai-local-key" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "ocean waves at sunrise", "duration": 5}'

# Poll it
curl -H "Authorization: Bearer sk-museai-local-key" \
  http://127.0.0.1:18610/v1/videos/task_xxxxxxxxxxxxxxxx
```

`image` (or `input_reference`) accepts a first-frame image as a data URL, an
`http(s)` URL, or a base64 string.

### 9Router

```bash
python scripts/connect_9router.py --port 18610 --prefix muse
```

The API key defaults to `$MUSE2API_API_KEY` (fallback `sk-museai-local-key`); pass
`--api-key` to override, and `--db` if 9Router does not live at
`~/.9router/db/data.sqlite`. Then:

```bash
curl -s -X POST http://127.0.0.1:20128/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "muse/muse-chat", "messages": [{"role": "user", "content": "ping"}]}'
```

## Endpoints

| Method | Route | Auth | Description |
|---|---|---|---|
| `GET` | `/healthz` | — | Liveness: `{"ok": true, "version": "..."}` |
| `GET` | `/readyz` | — | Driver health + pool availability |
| `GET` | `/v1/models` | API key | Model catalog and alias mapping |
| `POST` | `/v1/chat/completions` | API key | Multi-turn chat, SSE or buffered |
| `POST` | `/v1/images/generations` | API key | Text-to-image |
| `POST` | `/v1/images/edits` | API key | `501` — not implemented |
| `POST` | `/v1/videos` | API key | Dispatch an async video task |
| `GET` | `/v1/videos/{task_id}` | API key | Poll task status, progress, result |
| `GET` | `/v1/media/{name}` | — | Serve generated media (unguessable name) |
| `POST` | `/v1/responses` | API key | Responses API (buffered or SSE events) |
| `GET` | `/admin/accounts` | Admin key | List accounts (cookies masked) |
| `POST` | `/admin/accounts` | Admin key | Add an account |
| `PATCH` | `/admin/accounts/{id}` | Admin key | Update label, enabled, cookies |
| `DELETE` | `/admin/accounts/{id}` | Admin key | Remove an account |
| `POST` | `/admin/accounts/{id}/reset` | Admin key | Clear cooldown/error state |
| `POST` | `/admin/accounts/{id}/renew` | Admin key | Refresh cookies / wake the workspace |
| `GET` | `/admin/status` | Admin key | Version, driver, capabilities, pool stats |
| `POST` | `/admin/keys` | Admin key | Mint a database-backed API key (returned once) |
| `GET` | `/admin/keys` | Admin key | List key hashes, labels, revocation state |
| `DELETE` | `/admin/keys/{key_hash}` | Admin key | Revoke a key (enforced ≤30 s later) |
| `GET` | `/admin/usage` | Admin key | Request/token aggregates per key and account (`?since=`) |
| `GET` | `/admin/tasks` | Admin key | Recent tasks (`?limit=`) |
| `POST` | `/admin/tasks/{id}/cancel` | Admin key | Cancel a background task |

Errors use the OpenAI shape:

```json
{"error": {"message": "no usable account in pool", "type": "server_error", "code": "no_account_available"}}
```

| Status | Code | Meaning |
|---|---|---|
| 400 | `invalid_request` | Bad payload, or a model of the wrong kind for the endpoint |
| 401 | `invalid_api_key` | Missing/wrong bearer token |
| 404 | `not_found` | Unknown account, task, or media name |
| 429 | `upstream_quota_exhausted` | Account out of quota; it enters cooldown |
| 429 | `rate_limit_exceeded` | API key over `MUSE2API_RATE_LIMIT_RPM` |
| 501 | `not_implemented` | Capability this driver does not implement |
| 502 | `upstream_error` / `upstream_auth_failed` / `upstream_refused` | Upstream failure; auth failure marks the account invalid |
| 503 | `no_account_available` | Pool empty, all accounts busy, or all invalid |
| 504 | `upstream_timeout` | First token / chat / image / video timeout |

## Models and aliases

Base models: `muse-chat` (chat), `muse-image` (image), `muse-video` (video).

Aliases resolve to the nearest base model, so existing OpenAI tooling works unchanged:

| Alias | Resolves to |
|---|---|
| `gpt-4o`, `gpt-4o-mini`, `gpt-4.1`, `gpt-5`, `claude-sonnet-4`, `deepseek-chat` | `muse-chat` |
| `dall-e-3`, `gpt-image-1` | `muse-image` |
| `sora` | `muse-video` |

An unknown model name falls back to the default of the requested kind; a known model
of the wrong kind (e.g. `muse-video` on a chat request) is rejected with `400`.

Sampling parameters (`temperature`, `top_p`, `max_tokens`, `tools`) are accepted for
compatibility and ignored — muse.ai exposes no such controls.

## Configuration reference

All variables use the `MUSE2API_` prefix and can be set in `.env` or the environment.

### Server and auth

| Variable | Default | Description |
|---|---|---|
| `MUSE2API_HOST` | `127.0.0.1` | Bind address |
| `MUSE2API_PORT` | `18610` | Bind port |
| `MUSE2API_LOG_LEVEL` | `INFO` | Uvicorn/log level |
| `MUSE2API_PUBLIC_BASE` | *(empty)* | External base URL used in media links; empty derives from the request |
| `MUSE2API_API_KEY` | *(empty)* | Bearer key for `/v1/*`. If empty, generated once and stored in `data/api_key` |
| `MUSE2API_ADMIN_KEY` | *(empty)* | Bearer key for `/admin/*`; falls back to the API key |

### Storage and driver selection

| Variable | Default | Description |
|---|---|---|
| `MUSE2API_DATA_DIR` | `data` | Accounts, tasks, media, browser profile, `museai.db` |
| `MUSE2API_DRIVER` | `mock` | `mock` \| `browser` \| `http`; process env wins over `.env` |
| `MUSE2API_SITE_URL` | `https://muse.ai` | Upstream base URL |
| `MUSE2API_ACCOUNT_STORE` | `json` | `json` (files) or `sqlite` (accounts + keys + usage) |
| `MUSE2API_RATE_LIMIT_RPM` | `0` | Requests per minute per API key; `0` disables |

### HTTP driver (gateway protocol)

| Variable | Default | Description |
|---|---|---|
| `MUSE2API_HTTP_TIMEOUT` | `60` | Bootstrap/WebSocket timeout in seconds |
| `MUSE2API_HTTP_MAX_CONNECTIONS` | `8` | Bootstrap HTTP connection pool per request |
| `MUSE2API_STREAM_PING_INTERVAL` | `15` | SSE `: ping` interval on idle streams; also the gateway read deadline |

### Browser driver

| Variable | Default | Description |
|---|---|---|
| `MUSE2API_CHROMIUM_PATH` | *(empty)* | Explicit Chrome/Chromium binary; required on Windows |
| `MUSE2API_CDP_PORT` | `19210` | Local DevTools port (loopback only) |
| `MUSE2API_HEADLESS` | `true` | Launch headless |
| `MUSE2API_BROWSER_PROXY` | *(empty)* | Proxy for the browser only, e.g. `socks5://127.0.0.1:10808` |
| `MUSE2API_BROWSER_PROFILE_DIR` | `data/browser-profile` | Chromium user-data dir |
| `MUSE2API_PAGE_READY_TIMEOUT` | `45` | Seconds to wait for the page to hydrate |

### Timeouts (seconds)

| Variable | Default |
|---|---|
| `MUSE2API_CHAT_TIMEOUT` | `300` |
| `MUSE2API_FIRST_TOKEN_TIMEOUT` | `45` |
| `MUSE2API_IMAGE_TIMEOUT` | `240` |
| `MUSE2API_VIDEO_TIMEOUT` | `600` |

### Account pool

| Variable | Default | Description |
|---|---|---|
| `MUSE2API_POOL_STRATEGY` | `affinity` | `affinity` \| `lru` \| `round_robin` |
| `MUSE2API_POOL_ACQUIRE_TIMEOUT` | `60` | Seconds to wait for a free account |
| `MUSE2API_ACCOUNT_MAX_CONCURRENCY` | `1` | In-flight requests per account |
| `MUSE2API_ACCOUNT_COOLDOWN` | `120` | Cooldown after a retryable upstream error |
| `MUSE2API_MAX_FAILOVER` | `2` | Retries on other accounts before surfacing the error |

`affinity` pins a conversation hint (or user id) to the account that served it, so the
hot page can be reused; `lru` picks the least recently used; `round_robin` cycles.

### Session keepalive

| Variable | Default | Description |
|---|---|---|
| `MUSE2API_KEEPALIVE_ENABLED` | `false` | Periodically renew session cookies upstream |
| `MUSE2API_KEEPALIVE_INTERVAL` | `21600` | Renewal interval in seconds (6 h) |

`hatch_sess` typically lives ~30 days and `hatch_vml` only a few days; enabling
keepalive keeps a long-running deployment logged in.

## Architecture

```text
OpenAI SDK / 9Router / Web client
              │ HTTP (Bearer token)
              ▼
       ┌──────────────┐
       │    museai    │ FastAPI proxy, default port 18610
       └──────┬───────┘
              │ Chrome DevTools Protocol (WebSocket, loopback)
              ▼
       ┌──────────────┐
       │   Chromium   │ one process, one isolated browser context per account
       └──────┬───────┘
              │ HTTPS/WSS (hatch_sess + hatch_gw + datr)
              ▼
           muse.ai
```

### Directory structure

```text
src/museai/
├── app.py                 # FastAPI factory, lifespan, error handlers
├── config.py              # Pydantic settings (MUSE2API_ prefix)
├── errors.py              # Exception hierarchy → HTTP + OpenAI error bodies
├── log.py                 # Logging setup
├── __main__.py            # `python -m museai` / `museai`
├── api/
│   ├── deps.py            # Bearer auth (constant-time), service accessors
│   ├── schemas.py         # OpenAI-ish request models
│   └── routes/            # health, models, chat, images, videos, media, admin, responses
├── services/
│   ├── container.py       # Wires settings, driver, pool, gateway, tasks, media
│   ├── gateway.py         # Account lease + failover + streaming orchestration
│   └── tasks.py           # Persistent background tasks (video)
├── accounts/
│   ├── model.py           # Account state: active / cooling / invalid
│   ├── pool.py            # Scheduling strategies, concurrency, cooldown
│   ├── store.py           # Atomic JSON persistence
│   └── keepalive.py       # Periodic renew loop
├── core/
│   ├── models.py          # Model catalog + alias resolution
│   ├── prompt.py          # Message flattening / follow-up detection
│   ├── tools.py           # Tool schema rendering + fenced-block parser → tool_calls
│   ├── cookies.py         # parse_any(): Cookie-Editor JSON, cookies.txt, raw header
│   └── media.py           # Media storage + image reference loading
├── drivers/
│   ├── base.py            # MuseDriver ABC and request/response types
│   ├── registry.py        # Driver selection (lazy imports)
│   ├── mock.py            # Offline driver for tests
│   ├── browser/           # chromium.py, cdp.py, dom.py, driver.py
│   └── http/              # gateway.py (Noise XX transport, protobuf frames) + driver.py
└── upstream/
    └── muse.py            # Upstream constants, cookie validation, session renewal

accounts/
├── store.py              # JSON account store (default)
├── sqlite_store.py       # accounts + hashed API keys + usage in data/museai.db
├── pool.py               # scheduling, concurrency, cooldown
└── keepalive.py          # periodic session renewal
```

### Request lifecycle

1. FastAPI validates the body (`api/schemas.py`) and the bearer token (`api/deps.py`,
   which also enforces the per-key rate limit and the DB key store when enabled).
2. The route resolves the model (`core/models.py`) and flattens messages
   (`core/prompt.py`).
3. `services/gateway.py` leases an account from the pool — or, for account-less
   drivers such as `mock`, uses an anonymous lease.
4. The driver performs the upstream call. `BrowserDriver` reuses one tab per account;
   `HttpDriver` bootstraps gateway credentials, dials `wss://hatch.metaaivm.com/v1/noise`,
   completes a Noise XX handshake and speaks protobuf RPC frames (see
   [docs/protocol.md](docs/protocol.md)).
5. Deltas flow back as SSE; on a retryable error before any text is emitted the
   gateway fails over to another account.
6. `Lease.fail()`/`release()` update account health and persist accounts (JSON file or
   `museai.db`, depending on `MUSE2API_ACCOUNT_STORE`). Usage rows are written by the
   chat and responses routes when usage tracking is enabled.

### Driver contract

All upstream interaction goes through `drivers/base.py`:

```python
async def chat_stream(account, req) -> AsyncIterator[str]
async def generate_image(account, req) -> list[MediaResult]
async def generate_video(account, req, progress_cb) -> MediaResult
async def renew_session(account) -> SessionInfo
```

`DriverCapabilities` advertises which of these a driver supports, so unsupported
endpoints return `501` instead of failing deep inside the driver. Adding a driver means
implementing this ABC and registering it in `drivers/registry.py`; HTTP routes never
change.

### Implementation notes worth preserving

- Cookies must be URL-decoded before `Network.setCookie`, and registered for both
  `.muse.ai` and `muse.ai`; skipping either breaks the session behind the edge proxy.
- Readiness detection is locale-independent (input presence + hydration), because the
  web client renders in the user's language.
- One Chromium process, one `BrowserContext` per account: sessions never bleed.
- The Chromium profile path is resolved to an absolute path — Chrome refuses a
  relative `--user-data-dir` and exits with code 21.
- No `--remote-allow-origins=*`: the loopback DevTools port has no authentication, so
  a wildcard origin would let any web page drive it.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the condensed version and
[AGENTS.md](AGENTS.md) for the machine-readable contract.

## Development

```bash
pytest                 # whole suite, offline (MockDriver)
pytest -q tests/test_api.py::test_chat_stream   # single test
ruff check .           # lint
ruff format .          # format
```

Expected: `23 passed` in roughly a second, and a clean `ruff` run. The suite uses
`MockDriver` and `TestClient`; it never opens a browser or touches muse.ai.

CI (`.github/workflows/ci.yml`) runs `pip install -e '.[http,dev]'   # [http] = gateway driver deps`, `ruff check .` and
`pytest -q` on Python 3.10 and 3.12.

Adding a route: define the schema in `api/schemas.py`, the handler in
`api/routes/<name>.py`, include the router in `api/routes/__init__.py`, and keep
upstream work in the driver layer.

## Deployment

### Docker

```bash
docker build -t museai .
docker run --rm -p 127.0.0.1:18610:18610 \
  -e MUSE2API_DRIVER=http \
  -e MUSE2API_API_KEY=sk-museai-local-key \
  -e MUSE2API_ADMIN_KEY=sk-museai-admin-key \
  -v "$PWD/data:/app/data" \
  museai
```

`Dockerfile` is the Chromium-free image (default driver `http`). For image/video
generation build `Dockerfile.browser` instead. Both run as an unprivileged `muse` user
and store state in `/app/data`.

### Docker Compose

```bash
cp .env.example .env   # then run `muse2api setup` for cookies
docker compose up -d --build
docker compose logs -f
```

Compose publishes the port on loopback only (`127.0.0.1:18610:18610`) and mounts
`./data` into the container. Keep the admin key private: `/admin/*` can add, replace
and delete accounts.

### Bare metal

```bash
git pull
pip install -e ".[http]"
muse2api doctor && muse2api start
```

The service holds no database; state is `data/accounts.json`, `data/tasks.json` and
`data/media/`. Back up `data/` to preserve accounts and pending tasks.

## Troubleshooting

### `Chromium did not expose a DevTools endpoint`

Chrome failed to start or never bound the debug port.

1. Set an explicit binary: `MUSE2API_CHROMIUM_PATH`.
2. Make sure the profile directory is writable and that no other process owns
   `MUSE2API_CDP_PORT` (default `19210`). Change the port if it is taken.
3. `RC=21` on Windows means Chrome rejected the user-data dir; this build resolves the
   path automatically, so a stale checkout without that fix is the usual cause.

### `401 invalid_api_key`

The bearer token does not match `MUSE2API_API_KEY`. If you left it empty, the key was
generated into `data/api_key` — read it from there. `x-api-key:` works as well as
`Authorization: Bearer`.

### `503 no_account_available`

The pool is empty, all accounts are busy (wait or raise
`MUSE2API_ACCOUNT_MAX_CONCURRENCY`), or every account is `invalid`/`cooling`. Check
`/admin/status`.

### `502 upstream_auth_failed`

muse.ai rejected the session. Re-export `hatch_sess`/`hatch_gw`/
`hatch_native_auth_device` from a browser that is logged in and `PATCH` the account.
Expired cookies are the most common cause.

### `502 upstream_refused`

Upstream answered but did not produce what was asked (for example text instead of an
image). Retrying the same prompt usually gives the same result — rephrase it.

### `504 upstream_timeout`

Slow or sleeping workspace. Raise `MUSE2API_FIRST_TOKEN_TIMEOUT` /
`MUSE2API_CHAT_TIMEOUT`, or `POST /admin/accounts/{id}/renew` to wake the VM.

### Empty or partial stream

Clients that buffer SSE show nothing until the stream ends. Use `curl -N`, or disable
buffering in the client/proxy. Once any text has been emitted the gateway cannot fail
over mid-stream — the error is surfaced in the stream.

### muse.ai unreachable from this host

Set `MUSE2API_BROWSER_PROXY` (browser traffic only) and, if needed, the standard
`HTTPS_PROXY` for the Python-side session renewal calls.

### Windows: Chrome not found automatically

Auto-detection only covers Linux and macOS paths; set `MUSE2API_CHROMIUM_PATH` to the
full path of `chrome.exe`.

## Security notes

- **The service is a credential holder.** `data/accounts.json` stores session cookies
  in plaintext; `data/media/` stores generated content. Both live under
  `MUSE2API_DATA_DIR` (`data/` by default) and are git-ignored. Do not sync, back up to
  a public location, or share that directory. `chmod 0600` is applied on POSIX
  systems; it is a no-op on Windows, where the file inherits the directory ACL.
- **Bind to loopback.** The default host is `127.0.0.1`. Exposing `/admin/*` to a
  network lets anyone with the admin key add, replace, and delete accounts.
- **The DevTools port is unauthenticated.** Chromium runs with
  `--remote-debugging-port` on loopback; any local process can attach to it and read
  the account cookies. Do not leave the service running when it is not needed. The
  wildcard origin (`--remote-allow-origins=*`) was removed so browser pages cannot
  attach, but local processes are still trusted.
- **`/v1/media/{name}` is public by design** — media URLs must be embeddable in
  clients that cannot send auth headers. Names are random; the endpoint is a bearer
  capability, treat a URL as a secret.
- **Image references are fetched server-side.** `/v1/chat/completions`, `/v1/videos`
  and `/v1/images/*` accept `http(s)` image URLs and download them, so an untrusted
  caller could make the server fetch internal addresses. Keep the service on loopback
  or behind a trusted network.
- **Upstream terms of service.** Automating a personal web session may violate
  muse.ai's terms and can get the account suspended. Use an account you are willing to
  lose.
- Session cookies expire (`hatch_sess` ~30 days, `hatch_vml` a few days). Logging out
  of muse.ai invalidates them immediately; delete the account from the pool and the
  entry from `data/accounts.json` when rotating.

## Limitations and roadmap

Not implemented today, tracked in [TODO.md](TODO.md):

- `/v1/images/edits` returns `501`.
- The `http` driver covers chat, tools and `/v1/responses`, but **not** image or video
  generation: those need the upstream media RPCs and the `delta.presentation` download
  path mapped (see [docs/protocol.md](docs/protocol.md) §5). Use `driver=browser` for
  media.
- Tool calling is emulated through the prompt: the schema is rendered, the reply is
  parsed for a fenced `tool` block. The muse.ai agent also ignores that block for some
  prompts (it answers directly instead), so a coding agent may see plain text rather
  than `tool_calls`. The upstream `delta.tool_start` / `delta.tool_done` events are not
  yet mapped.
- `temperature`, `top_p`, and `max_tokens` are accepted but ignored (the upstream web
  client exposes no sampling controls).
- Quota lookup, media cleanup, and a web admin UI are not built.
- Revoking a database-backed API key takes effect within 30 seconds (positive-result
  cache in `api/deps.py`).

## Contributing

1. Branch from `main`.
2. Keep upstream-specific knowledge in `drivers/` or `upstream/` — routes must stay
   driver-agnostic.
3. Add or update a `MockDriver`-based test for behavior changes; the suite must stay
   offline.
4. `ruff check .` and `pytest` must pass before opening a PR.
5. Preserve the implementation notes in [Architecture](#implementation-notes-worth-preserving);
   each one corresponds to a bug that already cost debugging time.

See [CONTRIBUTING.md](CONTRIBUTING.md) and [AGENTS.md](AGENTS.md).

## License

[MIT](LICENSE) © 2026 D4NNBOZ
