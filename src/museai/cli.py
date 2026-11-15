"""``muse2ai`` command line: start the proxy behind a small Rich dashboard.

    muse2ai start                 # dashboard + server on the configured host/port
    muse2ai start --no-ui         # plain logs (containers, systemd)
    muse2ai start --port 18611 --driver http

The dashboard polls the same objects the admin API exposes (driver health, account
stats) plus an in-memory log ring, so what you see is the live process, not a guess.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import uvicorn

from . import __version__
from .app import create_app
from .config import Settings, get_settings
from .log import setup_logging

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

    def uptime(self) -> str:
        seconds = int(time.monotonic() - self.started_at)
        return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="muse2ai", description="muse.ai → OpenAI-compatible proxy")
    sub = ap.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="run the server")
    start.add_argument("--host", default=None, help="bind address (default from settings)")
    start.add_argument("--port", type=int, default=None, help="bind port (default from settings)")
    start.add_argument(
        "--driver", default=None, choices=["mock", "browser", "http"], help="upstream driver"
    )
    start.add_argument("--no-ui", action="store_true", help="plain uvicorn logs, no dashboard")
    start.add_argument("--show-keys", action="store_true", help="print API keys in the dashboard")
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


def _install_ring(level: str) -> RingHandler:
    handler = RingHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
    root = logging.getLogger()
    root.setLevel(level.upper())
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
        title="[bold]muse2ai[/bold]",
        subtitle="Ctrl+C to stop",
        border_style="cyan",
    )


async def serve(settings: Settings, *, show_ui: bool, show_keys: bool) -> int:
    handler = _install_ring(settings.log_level)
    state = DashboardState(settings=settings, handler=handler)
    app = create_app(settings)
    config = uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        access_log=not show_ui,
    )
    server = uvicorn.Server(config)
    state.server = server
    task = asyncio.create_task(server.serve())
    try:
        if not show_ui:
            await task
            return 0
        from rich.live import Live

        with Live(render_dashboard(state, show_keys), console=None, refresh_per_second=4) as live:
            while not server.should_exit and not task.done():
                live.update(render_dashboard(state, show_keys))
                await asyncio.sleep(0.25)
        if task.done():
            task.result()
    except (KeyboardInterrupt, asyncio.CancelledError):
        server.should_exit = True
    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command != "start":  # pragma: no cover - argparse enforces the subcommand
        return 2
    settings = apply_overrides(get_settings(), args)
    setup_logging(settings.log_level)
    try:
        return asyncio.run(serve(settings, show_ui=not args.no_ui, show_keys=args.show_keys))
    except KeyboardInterrupt:
        return 0
    except OSError as exc:
        print(f"cannot start on {settings.host}:{settings.port}: {exc}", file=sys.stderr)
        return 1
    except SystemExit as exc:  # uvicorn exits this way when the port is taken
        return int(exc.code or 0)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
