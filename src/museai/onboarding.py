"""First-run experience: cookies in, working proxy out.

A fresh clone used to need a hand-written ``.env``, a hand-started server and a
hand-crafted JSON import before a single request worked. Everything here exists to
collapse that into ``muse2api setup`` plus ``muse2api doctor``:

* :func:`write_env_driver` — keep ``.env`` truthful about the driver
* :func:`parse_export` — cookies plus their expiry from any browser export
* :func:`read_clipboard` — the shortest path from the extension popup to us
* :func:`verify_cookies` — prove the cookies work before saving them
* :func:`async_save_account` — write the account without a running server
* :func:`doctor_checks` — every reason a request would fail, with the fix
* :func:`client_snippets` — copy-paste blocks for the usual clients
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .accounts.model import Account, AccountStatus
from .accounts.store import JsonAccountStore
from .config import Settings
from .core.cookies import CookieParseError, parse_any
from .upstream import muse

ENV_FILE = Path(".env")

ENV_TEMPLATE = """\
# muse2api settings. Values already exported in your shell win over this file.
MUSE2API_DRIVER={driver}
# Empty = generated on first start and stored in data/api_key
MUSE2API_API_KEY=
MUSE2API_ADMIN_KEY=
MUSE2API_DATA_DIR=data
"""

RECOMMENDED_MODEL = "muse-chat"
MODEL_ALIASES = ("gpt-4o", "gpt-4o-mini", "gpt-5", "claude-3-5-sonnet")


# --------------------------------------------------------------------------- env


def read_env(path: Path = ENV_FILE) -> dict[str, str]:
    """Parse the tiny subset of dotenv we write ourselves (``KEY=value`` per line)."""
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def write_env_driver(driver: str, path: Path = ENV_FILE) -> str:
    """Make ``.env`` name the driver. Returns ``created`` / ``updated`` / ``unchanged``."""
    if not path.is_file():
        path.write_text(ENV_TEMPLATE.format(driver=driver), encoding="utf-8")
        return "created"
    lines = path.read_text(encoding="utf-8").splitlines()
    key = "MUSE2API_DRIVER"
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(f"{key}=") or stripped == key:
            if stripped == f"{key}={driver}":
                return "unchanged"
            lines[index] = f"{key}={driver}"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return "updated"
    lines.append(f"{key}={driver}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return "updated"


def driver_conflict(driver: str) -> str | None:
    """Explain a shell variable silently overriding ``.env``."""
    shell = os.environ.get("MUSE2API_DRIVER")
    if shell and shell != driver:
        return f"shell variable MUSE2API_DRIVER={shell} overrides .env ({driver})"
    return None


WATCHED_ENV = (
    "MUSE2API_DRIVER",
    "MUSE2API_API_KEY",
    "MUSE2API_ADMIN_KEY",
    "MUSE2API_DATA_DIR",
    "MUSE2API_PORT",
)
"""Shell variables users trip over. ``pydantic-settings`` prefers these over ``.env``."""


def env_conflicts() -> list[tuple[str, str, str]]:
    """``(key, shell value, .env value)`` for every watched variable they disagree on."""
    from_file = read_env(ENV_FILE)
    conflicts: list[tuple[str, str, str]] = []
    for key in WATCHED_ENV:
        shell = os.environ.get(key)
        if not shell:
            continue
        configured = from_file.get(key, "")
        if configured == shell:
            continue
        conflicts.append((key, _mask(value=shell), configured or "(unset)"))
    return conflicts


def _mask(*, value: str) -> str:
    return value if len(value) <= 8 else f"{value[:6]}…{value[-4:]}"


# --------------------------------------------------------------------------- cookies


@dataclass
class CookieExport:
    cookies: dict[str, str]
    expires: dict[str, int] = field(default_factory=dict)

    @property
    def missing(self) -> list[str]:
        return muse.missing_session_cookies(self.cookies)


def parse_export(text: str) -> CookieExport:
    """Parse any supported export and keep the per-cookie expiry when present."""
    cookies = parse_any(text)
    expires: dict[str, int] = {}
    stripped = text.strip()
    if stripped.startswith("["):
        try:
            for item in json.loads(stripped):
                if not isinstance(item, dict):
                    continue
                name = item.get("name")
                deadline = item.get("expirationDate") or item.get("expires")
                if name and isinstance(deadline, (int, float)):
                    expires[str(name)] = int(deadline)
        except (json.JSONDecodeError, TypeError):
            expires = {}
    for name, value in cookies.items():
        if name not in expires:
            parsed = _jwt_expiry(value)
            if parsed:
                expires[name] = parsed
    return CookieExport(cookies=cookies, expires=expires)


def _jwt_expiry(value: str) -> int:
    """muse cookies are ``prefix.v1:<jwt>``; read ``exp`` when the shape allows it."""
    if "v1%3A" not in value and "v1:" not in value:
        return 0
    token = value.split("v1:", 1)[-1].split("v1%3A", 1)[-1]
    token = token.split("%3A", 1)[0]
    parts = token.split(".")
    if len(parts) < 2:
        return 0
    import base64

    body = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(body))
    except (ValueError, json.JSONDecodeError):
        return 0
    exp = payload.get("exp")
    return int(exp) if isinstance(exp, (int, float)) else 0


def read_clipboard() -> str:
    """Best-effort clipboard read; returns "" when no tool is available."""
    commands = {
        "win32": ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
        "darwin": ["pbpaste"],
    }
    command = commands.get(sys.platform) or (
        ["wl-paste"] if shutil.which("wl-paste") else ["xclip", "-selection", "clipboard", "-o"]
    )
    if not shutil.which(command[0]):
        return ""
    try:
        # Windows clipboards routinely hold non-UTF-8 (screenshots, ANSI apps); decoding
        # must not raise, or setup dies before it can tell the user anything.
        done = subprocess.run(
            command, capture_output=True, encoding="utf-8", errors="replace", timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (done.stdout or "") if done.returncode == 0 else ""


async def verify_cookies(settings: Settings, cookies: dict[str, str]) -> tuple[bool, str]:
    """Run the gateway bootstrap handshake. Success means the cookies authenticate."""
    from .drivers.http.gateway import bootstrap

    headers = {"User-Agent": muse.USER_AGENT, "Cookie": muse.cookie_header(cookies)}
    try:
        async with httpx.AsyncClient(
            base_url=settings.site_url,
            headers=headers,
            timeout=settings.http_timeout,
            follow_redirects=True,
        ) as client:
            cred = await bootstrap(client)
    except Exception as exc:  # noqa: BLE001 - surfaced verbatim to the user
        return False, f"{type(exc).__name__}: {exc}"
    return True, f"gateway {cred.vm_address} (vm {cred.vm_name})"


def account_store(settings: Settings) -> Any:
    """The store the server would use, so setup writes what startup will read."""
    if settings.account_store == "sqlite":
        return _sqlite_store(settings)
    return JsonAccountStore(settings.accounts_file)


async def load_accounts(settings: Settings) -> list[Account]:
    return await account_store(settings).load()


async def async_save_account(
    settings: Settings, cookies: dict[str, str], expires: dict[str, int], label: str = ""
) -> Account:
    """Store the account without needing a running server."""
    store = account_store(settings)
    accounts = await store.load()
    account = accounts[0] if accounts else Account()
    account.cookies = cookies
    account.cookie_expires = expires
    account.status = AccountStatus.ACTIVE
    account.cooldown_until = 0
    account.last_error = None
    account.enabled = True
    if label:
        account.label = label
    elif not account.label:
        account.label = "primary"
    if account not in accounts:
        accounts.append(account)
    await store.save(accounts)
    return account


def _sqlite_store(settings: Settings) -> Any:
    from .accounts.sqlite_store import SqliteAccountStore

    return SqliteAccountStore(settings.db_file)


# --------------------------------------------------------------------------- doctor


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    fix: str = ""

    @property
    def status(self) -> str:
        return "ok" if self.ok else "fix"


def doctor_checks(settings: Settings, accounts: list[Account]) -> list[Check]:
    """Static checks: no network, safe to run anywhere."""
    checks: list[Check] = []

    env_values = read_env(ENV_FILE)
    if not ENV_FILE.is_file():
        checks.append(
            Check("config file", False, ".env missing", "run `muse2api setup`")
        )
    else:
        configured = env_values.get("MUSE2API_DRIVER", "(unset)")
        conflict = driver_conflict(settings.driver)
        checks.append(
            Check(
                "driver",
                conflict is None,
                f"{settings.driver} (in .env: {configured})",
                conflict or "",
            )
        )

    for key, shell_value, env_value in env_conflicts():
        checks.append(
            Check(
                "env override",
                False,
                f"{key}={shell_value} (shell) vs {env_value} (.env)",
                "unset the shell variable or match it, or the snippets below are wrong",
            )
        )

    if settings.driver == "http":
        missing = [m for m in ("websockets", "noise") if not _installed(m)]
        checks.append(
            Check(
                "http extras",
                not missing,
                "websockets + noise" if not missing else f"missing {', '.join(missing)}",
                'install with `pip install -e ".[http]"`',
            )
        )
    elif settings.driver == "browser":
        from .drivers.browser.chromium import find_chromium  # noqa: PLC0415 - optional import

        found = find_chromium(settings.chromium_path)
        checks.append(
            Check(
                "chromium",
                bool(found),
                str(found) if found else "not found",
                "set MUSE2API_CHROMIUM_PATH, or use --driver http",
            )
        )

    try:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        probe = settings.data_dir / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        checks.append(Check("data dir", True, str(settings.data_dir.resolve())))
    except OSError as exc:
        checks.append(Check("data dir", False, str(exc), "point MUSE2API_DATA_DIR elsewhere"))

    if not accounts:
        checks.append(
            Check("accounts", False, "none imported", "run `muse2api setup` to paste cookies")
        )
    for account in accounts:
        problems: list[str] = []
        if muse.missing_session_cookies(account.cookies):
            problems.append("missing " + ", ".join(muse.missing_session_cookies(account.cookies)))
        expired = [
            name
            for name, deadline in account.cookie_expires.items()
            if deadline and deadline < time.time()
        ]
        if expired:
            problems.append("expired " + ", ".join(sorted(expired)))
        if account.status == "cooling":
            wait = max(0, int(account.cooldown_until - time.time()))
            problems.append(f"cooling {wait}s after: {account.last_error or 'unknown error'}")
        checks.append(
            Check(
                f"account {account.id}",
                not problems,
                "; ".join(problems) if problems else f"{len(account.cookies)} cookies, active",
                "re-import cookies with `muse2api setup --cookies FILE`",
            )
        )
    return checks


def _installed(module: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


# --------------------------------------------------------------------------- output


def client_snippets(settings: Settings, api_key: str) -> list[str]:
    """Copy-paste blocks: base URL, key, model, and a curl that proves the whole path."""
    base = f"http://{settings.host}:{settings.port}"
    admin = settings.effective_admin_key
    return [
        f"Base URL   {base}/v1",
        f"API key    {api_key}",
        f"Admin key  {admin or api_key}",
        f"Model      {RECOMMENDED_MODEL}   (aliases: {', '.join(MODEL_ALIASES)})",
        "",
        "curl (buffered):",
        f'  curl {base}/v1/chat/completions -H "Authorization: Bearer {api_key}" '
        '-H "Content-Type: application/json" '
        f'-d \'{{"model": "{RECOMMENDED_MODEL}", "messages": [{{"role": "user", "content": "hi"}}]}}\'',
        "",
        "curl (stream):",
        f'  curl -N {base}/v1/chat/completions -H "Authorization: Bearer {api_key}" '
        '-H "Content-Type: application/json" '
        f'-d \'{{"model": "{RECOMMENDED_MODEL}", "stream": true, '
        '"messages": [{"role": "user", "content": "count to three"}]}\'',
    ]


def summary_line(settings: Settings, accounts: list[Account]) -> str:
    return (
        f"driver={settings.driver} · accounts={len(accounts)} · "
        f"data={settings.data_dir.resolve()} · port={settings.port}"
    )


__all__ = [
    "Check",
    "CookieExport",
    "CookieParseError",
    "ENV_FILE",
    "ENV_TEMPLATE",
    "WATCHED_ENV",
    "env_conflicts",
    "account_store",
    "async_save_account",
    "client_snippets",
    "doctor_checks",
    "driver_conflict",
    "load_accounts",
    "parse_export",
    "read_clipboard",
    "read_env",
    "summary_line",
    "verify_cookies",
    "write_env_driver",
]
