# Onboarding — from clone to first answer in ~5 minutes

This is the short path. Everything here is verified on a clean machine: install, import
cookies, start, and get a completion. If a step fails, jump to
[Troubleshooting](#troubleshooting) or run `muse2api doctor`.

---

## 1. Install (one command)

Pick one. Both give you the `muse2api` command on your `PATH`.

**pipx (recommended — isolated, no venv to manage):**

```bash
pipx install "museai[http] @ git+https://github.com/yayayapluto/museai.git"
```

Windows PowerShell, if `pipx` is not installed yet:

```powershell
py -m pip install --user pipx
py -m pipx ensurepath          # then open a NEW terminal
py -m pipx install "museai[http] @ git+https://github.com/yayayapluto/museai.git"
```

**editable clone (development / you want to hack on it):**

```bash
git clone https://github.com/yayayapluto/museai.git
cd museai
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -e ".[http]"
```

Check it:

```bash
muse2api --help
```

> Requires Python 3.10+. The `[http]` extra pulls `websockets` and `noiseprotocol` — the
> driver that talks to muse.ai without Chromium.

---

## 2. Import cookies (30 seconds with the extension)

The proxy authenticates as *you*, so it needs your muse.ai session cookies.

1. Open `chrome://extensions` → enable **Developer mode** → **Load unpacked** → select
   the `extension/` folder from this repo (for a pipx install, download that folder from
   the repository page).
2. Log in to <https://muse.ai> in that Chrome profile.
3. Click the muse2api extension → **Copy cookies**.
4. Run setup and paste:

```bash
muse2api setup --clipboard      # straight from the clipboard
# or: muse2api setup            # then paste into the prompt
# or: muse2api setup --cookies cookies.json
```

Setup does four things and tells you what it decided:

- writes `.env` with `MUSE2API_DRIVER=http` (creates it if missing)
- parses whatever you paste — extension JSON, `cookies.txt`, or a `Cookie:` header
- **verifies the cookies against muse.ai before saving them** (skip with `--no-verify`)
- saves the account and prints copy-paste client snippets

---

## 3. Verify, then start

```bash
muse2api doctor      # every reason a request would fail, with the fix
muse2api start       # dashboard + server on http://127.0.0.1:18610
```

Startup prints the configuration it actually resolved, so there is no guessing:

```
config: driver=http data_dir=data cwd=...
```

First answer takes 5–15 s (upstream agent latency); later turns are faster.

---

## 4. Point your client at it

From `muse2api setup` (or `muse2api start --show-keys`):

| Field | Value |
|---|---|
| Base URL | `http://127.0.0.1:18610/v1` |
| API key | printed by setup, also `data/api_key` |
| Model | `muse-chat` (aliases: `gpt-4o`, `gpt-4o-mini`, `gpt-5`, `claude-3-5-sonnet`) |

Works in NextChat, LobeChat, Cherry Studio, OpenAI SDKs, LangChain and 9Router.

Prove the whole path with one request:

```bash
curl http://127.0.0.1:18610/v1/chat/completions \
  -H "Authorization: Bearer <API_KEY>" -H "Content-Type: application/json" \
  -d '{"model":"muse-chat","messages":[{"role":"user","content":"say pong"}]}'
```

Expected: a JSON body whose `choices[0].message.content` is `pong` (or similar).

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `503 no usable account in pool` | no account imported, or the account is cooling after a failure | `muse2api setup`, then `muse2api doctor` |
| dashboard says `driver=browser` but you set `http` | a shell variable wins over `.env` | `echo $env:MUSE2API_DRIVER` — clear it or pass `--driver http` |
| `port 18610 already in use` | an old instance is still running | `muse2api doctor --port 18610`, kill the PID it names, or `muse2api start --port 18611` |
| `missing required cookies` | `hatch_sess`, `hatch_gw` or `hatch_native_auth_device` absent | log in again, re-copy from the extension |
| `cookies rejected: UpstreamAuthError` | session logged out or expired | log in to muse.ai, copy cookies again |
| `account cooling …` | upstream error parked the account for `MUSE2API_ACCOUNT_COOLDOWN` seconds (default 120) | wait, or restart; `doctor` shows the last error |
| images/videos return 501 | the `http` driver does not implement media | run with `--driver browser` (needs Chromium) |
| `ModuleNotFoundError: websockets` | installed without the extra | `pip install "museai[http] @ git+https://github.com/yayayapluto/museai.git"` |

Every one of these is also a `muse2api doctor` row, with the fix in the last column.
