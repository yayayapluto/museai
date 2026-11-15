"""``muse2api`` command line: start the proxy behind a small Rich dashboard.

    muse2api start                 # dashboard + server on the configured host/port
    muse2api start --no-ui         # plain logs (containers, systemd)
    muse2api start --port 18611 --driver http

The dashboard polls the same objects the admin API exposes (driver health, account
stats) plus an in-memory log ring, so what you see is the live process, not a guess.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import socket
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import uvicorn

from . import __version__, onboarding
from .app import create_app
from .config import Settings, get_settings
from .log import setup_logging

log = logging.getLogger(__name__)

LOG_BUFFER = 200
"""Log records kept for the dashboard."""

MASKED_HINT = "stored in {file} (pass --show-keys to print)"


class RingHandler(logging.Handler):
    """Keep the last ``capacity`` records for display without touching the log files."""

    def __init__(self, capacity: int = LOG_BUFFER) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []
        self.capacity = capacity

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        if len(self.records) > self.capacity:
            del self.records[: len(self.records) - self.capacity]


@dataclass
class DashboardState:
    settings: Settings
    handler: RingHandler
    started_at: float = field(default_factory=time.monotonic)
    server: uvicorn.Server | None = None
    rendered_records: int = 0

    def uptime(self) -> str:
        seconds = int(time.monotonic() - self.started_at)
        return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="muse2api", description="muse.ai → OpenAI-compatible proxy")
    sub = ap.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="run the server")
    start.add_argument("--host", default=None, help="bind address (default from settings)")
    start.add_argument("--port", type=int, default=None, help="bind port (default from settings)")
    start.add_argument(
        "--driver", default=None, choices=["mock", "browser", "http"], help="upstream driver"
    )
    start.add_argument("--no-ui", action="store_true", help="plain uvicorn logs, no dashboard")
    start.add_argument("--show-keys", action="store_true", help="print API keys in the dashboard")

    setup = sub.add_parser("setup", help="first run: write .env, import cookies, print snippets")
    setup.add_argument("--driver", default=None, choices=["mock", "browser", "http"])
    setup.add_argument("--cookies", default=None, metavar="FILE", help="cookie export file")
    setup.add_argument(
        "--clipboard", action="store_true", help="read the cookie export from the clipboard"
    )
    setup.add_argument("--label", default="", help="account label (default: primary)")
    setup.add_argument("--no-verify", action="store_true", help="skip the live handshake check")
    setup.add_argument(
        "--force", action="store_true", help="import even when mandatory cookies are missing"
    )

    doctor = sub.add_parser("doctor", help="explain why requests would fail")
    doctor.add_argument("--port", type=int, default=None, help="port to check (default from settings)")
    doctor.add_argument(
        "--live", action="store_true", help="also run the upstream handshake for each account"
    )
    return ap


def masked(key: str) -> str:
    if not key:
        return "(not set)"
    return key if len(key) <= 8 else f"{key[:6]}…{key[-4:]}"


def apply_overrides(settings: Settings, args: argparse.Namespace) -> Settings:
    if args.host:
        settings.host = args.host
    if args.port:
        settings.port = args.port
    if args.driver:
        settings.driver = args.driver
    return settings


def endpoint_lines() -> list[str]:
    return [
        "/v1/chat/completions   chat (SSE + buffered, tools)",
        "/v1/responses          Responses API",
        "/v1/models             model catalog",
        "/admin/accounts        cookie import (admin key)",
        "/admin/keys, /admin/usage   multi-tenant (sqlite store)",
    ]


def info_lines(
    settings: Settings, state: DashboardState, stats: dict[str, Any] | None, show_keys: bool
) -> list[tuple[str, str]]:
    """(label, value) pairs for the dashboard; pure so it is testable."""
    url = f"http://{settings.host}:{settings.port}"
    key = settings.api_key
    admin = settings.effective_admin_key
    rows = [
        ("URL", url),
        ("Version", f"{__version__} (driver={settings.driver})"),
        ("Uptime", state.uptime()),
        ("API key", key if show_keys else masked(key)),
        (
            "Admin key",
            admin if show_keys else masked(admin) + "  " + MASKED_HINT.format(file=settings.key_file),
        ),
        ("Data dir", str(settings.data_dir.resolve())),
        ("Store", settings.account_store),
    ]
    if stats:
        rows.append(
            (
                "Accounts",
                "{total} total · {available} available · {invalid} invalid · {inflight} in flight".format(
                    **{k: stats.get(k, 0) for k in ("total", "available", "invalid", "inflight")}
                ),
            )
        )
    if settings.rate_limit_rpm:
        rows.append(("Rate limit", f"{settings.rate_limit_rpm} rpm per key"))
    return rows


def _install_ring(level: str, *, mirror_to_terminal: bool = True) -> RingHandler:
    """Collect records for the dashboard.

    With the dashboard on screen the terminal belongs to Rich, so every stream handler is
    removed: a single log line written behind Rich's back corrupts its cursor bookkeeping
    and the panel gets appended instead of redrawn.
    """
    handler = RingHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    )
    root = logging.getLogger()
    root.setLevel(level.upper())
    if not mirror_to_terminal:
        for existing in list(root.handlers):
            if isinstance(existing, logging.StreamHandler):
                root.removeHandler(existing)
    root.addHandler(handler)
    return handler


def _level_style(levelno: int) -> str:
    if levelno >= logging.ERROR:
        return "bold red"
    if levelno >= logging.WARNING:
        return "yellow"
    if levelno <= logging.DEBUG:
        return "dim"
    return "white"


def render_dashboard(state: DashboardState, show_keys: bool) -> Any:
    from rich.console import Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    services = getattr(state.server.config.app if state.server else None, "state", None)
    stats = None
    if services is not None:
        svc = getattr(services, "services", None)
        if svc is not None:
            with contextlib.suppress(Exception):
                stats = svc.pool.stats()

    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold cyan", no_wrap=True)
    table.add_column()
    for label, value in info_lines(state.settings, state, stats, show_keys):
        table.add_row(label, value)

    endpoints = Text("\n".join(endpoint_lines()), style="dim")
    log = Text()
    for record in state.handler.records[-12:]:
        log.append(state.handler.format(record) + "\n", style=_level_style(record.levelno))
    if not state.handler.records:
        log.append("waiting for traffic…", style="dim")

    return Panel(
        Group(
            table,
            Text(""),
            Text("endpoints", style="bold"),
            endpoints,
            Text(""),
            Text("log", style="bold"),
            log,
        ),
        title="[bold]muse2api[/bold]",
        subtitle="Ctrl+C to stop",
        border_style="cyan",
    )


def port_available(host: str, port: int) -> bool:
    """Fail fast with a readable message instead of letting uvicorn sys.exit(3)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _startup_failure(task: asyncio.Task[Any]) -> str | None:
    """Return a message when the server task died before/while serving."""
    if not task.done():
        return None
    exc = task.exception()
    if exc is None:
        return None
    if isinstance(exc, SystemExit):
        return "address already in use"
    return f"{type(exc).__name__}: {exc}"


async def serve(settings: Settings, *, show_ui: bool, show_keys: bool) -> int:
    show_ui = show_ui and sys.stdout.isatty()
    handler = _install_ring(settings.log_level, mirror_to_terminal=not show_ui)
    state = DashboardState(settings=settings, handler=handler)
    app = create_app(settings)
    config = uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        access_log=False,  # a request line would fight the dashboard for the terminal
        log_config=None if show_ui else uvicorn.config.LOGGING_CONFIG,
    )
    server = uvicorn.Server(config)
    state.server = server
    task = asyncio.create_task(server.serve())
    try:
        if not show_ui:
            await task
            return 0
        from rich.live import Live

        with Live(
            render_dashboard(state, show_keys),
            console=None,
            screen=True,  # own the alternate screen: redraw, never stack
            auto_refresh=False,
        ) as live:
            live.refresh()
            while not server.should_exit and not task.done():
                await asyncio.sleep(0.25)
                live.update(render_dashboard(state, show_keys), refresh=True)
                if len(state.handler.records) != state.rendered_records:
                    state.rendered_records = len(state.handler.records)
        failure = _startup_failure(task)
        if failure:
            return 1
    except (KeyboardInterrupt, asyncio.CancelledError):
        server.should_exit = True
    finally:
        server.should_exit = True
        if not task.done():
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(task, timeout=10)
        elif task.exception() is not None:
            task.exception()  # consumed: reported by the caller
    return 0


def _console() -> Any:
    from rich.console import Console

    return Console()


def _rule(console: Any, title: str) -> None:
    from rich.rule import Rule

    console.print(Rule(title, style="cyan"))


def _read_cookie_input(args: argparse.Namespace, console: Any) -> str:
    """Cookie export from ``--cookies``, ``--clipboard`` or an interactive paste."""
    if args.cookies:
        from pathlib import Path

        return Path(args.cookies).read_text(encoding="utf-8")
    if args.clipboard:
        text = onboarding.read_clipboard()
        if not text.strip():
            console.print("[yellow]clipboard is empty or unreadable[/yellow]")
        return text
    if not sys.stdin.isatty():
        return ""
    console.print(
        "[bold]Get your cookies[/bold] — in Chrome: the muse2api extension popup → "
        "[bold]Copy cookies[/bold].\n"
        "Then paste the JSON here (a file path also works). Empty line = skip."
    )
    try:
        raw = input("cookies> ").strip()
    except EOFError:
        return ""
    from pathlib import Path

    if raw and not raw.startswith(("[", "{", "#")) and Path(raw).is_file():
        return Path(raw).read_text(encoding="utf-8")
    return raw


async def run_setup(settings: Settings, args: argparse.Namespace) -> int:
    console = _console()
    driver = args.driver or ("http" if settings.driver == "mock" else settings.driver)
    settings.driver = driver
    accounts = await onboarding.load_accounts(settings)

    _rule(console, "muse2api setup")
    action = onboarding.write_env_driver(driver)
    console.print(f".env  {'created' if action == 'created' else action}: driver={driver}")
    conflict = onboarding.driver_conflict(driver)
    if conflict:
        console.print(f"[yellow]note[/yellow] {conflict}")

    text = _read_cookie_input(args, console)
    imported = False
    if text.strip():
        try:
            export = onboarding.parse_export(text)
        except onboarding.CookieParseError as exc:
            console.print(f"[bold red]cannot read that export:[/bold red] {exc}")
            return 1
        if export.missing and not args.force:
            console.print(
                f"[bold red]missing required cookies:[/bold red] {', '.join(export.missing)}\n"
                "Log in to muse.ai, copy again, or pass --force to import anyway."
            )
            return 1
        if not args.no_verify and not export.missing:
            console.print("checking cookies against muse.ai…")
            ok, detail = await onboarding.verify_cookies(settings, export.cookies)
            if not ok:
                console.print(f"[bold red]cookies rejected:[/bold red] {detail}")
                return 1
            console.print(f"[green]gateway accepted the session[/green] ({detail})")
        account = await onboarding.async_save_account(
            settings, export.cookies, export.expires, args.label
        )
        console.print(f"account saved: {account.id} · {len(export.cookies)} cookies")
        imported = True
    elif accounts:
        console.print(f"keeping {len(accounts)} existing account(s)")
    else:
        console.print("[yellow]no cookies imported yet[/yellow] — run setup again when you have them")

    api_key = settings.resolve_api_key()
    for line in onboarding.client_snippets(settings, api_key):
        console.print(line, markup=False)
    console.print("")
    console.print(f"next: [bold]muse2api start[/bold]   ({onboarding.summary_line(settings, accounts)})")
    if not imported and not accounts:
        return 1
    return 0


async def run_doctor(settings: Settings, args: argparse.Namespace) -> int:
    from rich.table import Table

    console = _console()
    if args.port:
        settings.port = args.port
    accounts = await onboarding.load_accounts(settings)
    checks = onboarding.doctor_checks(settings, accounts)
    free = port_available(settings.host, settings.port)
    checks.append(
        onboarding.Check(
            "port",
            free,
            f"{settings.host}:{settings.port} free" if free else f"{settings.host}:{settings.port} busy",
            "" if free else "stop the other instance or use `muse2api start --port 18611`",
        )
    )
    if args.live:
        for account in accounts:
            ok, detail = await onboarding.verify_cookies(settings, account.cookies)
            checks.append(
                onboarding.Check(
                    f"live {account.id}",
                    ok,
                    detail,
                    "" if ok else "cookies expired or the account is logged out",
                )
            )

    table = Table(title="muse2api doctor", border_style="cyan", show_lines=False)
    table.add_column("check", style="bold cyan", no_wrap=True)
    table.add_column("state", no_wrap=True)
    table.add_column("detail", overflow="fold")
    table.add_column("fix", style="dim", overflow="fold")
    for check in checks:
        state = "[green]ok[/green]" if check.ok else "[bold red]fix[/bold red]"
        table.add_row(check.name, state, check.detail, check.fix if not check.ok else "")
    console.print(table)

    failing = [c for c in checks if not c.ok]
    if failing:
        console.print(f"{len(failing)} problem(s) — fixes above, then run `muse2api doctor` again")
        return 1
    console.print("everything checks out — `muse2api start` is ready")
    return 0


def run_start(settings: Settings, args: argparse.Namespace) -> int:
    settings = apply_overrides(settings, args)
    log.info(
        "config: driver=%s data_dir=%s cwd=%s", settings.driver, settings.data_dir, os.getcwd()
    )
    if not port_available(settings.host, settings.port):
        print(
            f"port {settings.port} on {settings.host} is already in use — another instance is "
            f"probably running. Stop it, or pick another port with "
            f"`muse2api start --port {settings.port + 1}`.",
            file=sys.stderr,
        )
        return 1
    if settings.driver != "mock" and not asyncio.run(onboarding.load_accounts(settings)):
        print(
            "no muse.ai account yet — run `muse2api setup` to import cookies. "
            "Requests will answer 503 until then.",
            file=sys.stderr,
        )
    try:
        return asyncio.run(serve(settings, show_ui=not args.no_ui, show_keys=args.show_keys))
    except KeyboardInterrupt:
        return 0
    except OSError as exc:
        print(f"cannot start on {settings.host}:{settings.port}: {exc}", file=sys.stderr)
        return 1
    except SystemExit as exc:  # uvicorn exits this way when the bind fails
        code = exc.code or 0
        if code:
            print(f"server failed to start (uvicorn exit {code}); see the log above", file=sys.stderr)
        return int(code)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    setup_logging(settings.log_level)
    if args.command == "setup":
        return asyncio.run(run_setup(settings, args))
    if args.command == "doctor":
        return asyncio.run(run_doctor(settings, args))
    return run_start(settings, args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
