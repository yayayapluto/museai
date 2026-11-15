"""CLI tests: the dashboard is built from real objects, so its inputs stay testable."""

from __future__ import annotations

import asyncio
import logging
import socket

from museai.cli import (
    DashboardState,
    RingHandler,
    _startup_failure,
    apply_overrides,
    build_parser,
    info_lines,
    masked,
    port_available,
)


def test_parser_accepts_start_flags():
    args = build_parser().parse_args(["start", "--port", "19000", "--driver", "http", "--no-ui"])
    assert args.command == "start"
    assert args.port == 19000
    assert args.driver == "http"
    assert args.no_ui is True


def test_overrides_win_over_settings(settings):
    args = build_parser().parse_args(["start", "--host", "0.0.0.0", "--port", "19001"])
    apply_overrides(settings, args)
    assert (settings.host, settings.port) == ("0.0.0.0", 19001)


def test_masked_hides_secret_middle():
    assert masked("sk-museai-local-key") == "sk-mus…-key"
    assert masked("") == "(not set)"


def test_info_lines_show_url_driver_and_stats(settings):
    settings.api_key = "sk-test-123456"
    settings.admin_key = "sk-admin-123456"
    state = DashboardState(settings=settings, handler=RingHandler())
    rows = dict(info_lines(settings, state, {"total": 2, "available": 1, "invalid": 0, "inflight": 1}, False))

    assert rows["URL"] == f"http://{settings.host}:{settings.port}"
    assert f"driver={settings.driver}" in rows["Version"]
    assert "2 total · 1 available" in rows["Accounts"]
    assert "sk-test-123456" not in rows["API key"]  # masked by default


def test_info_lines_reveal_keys_when_asked(settings):
    settings.api_key = "sk-test-123456"
    settings.admin_key = "sk-admin-123456"
    state = DashboardState(settings=settings, handler=RingHandler())
    rows = dict(info_lines(settings, state, None, True))
    assert rows["API key"] == "sk-test-123456"
    assert rows["Admin key"] == "sk-admin-123456"


def test_ring_handler_keeps_last_records():
    handler = RingHandler(capacity=3)
    logger = logging.getLogger("cli-test")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    for index in range(5):
        logger.info("line %d", index)
    logger.removeHandler(handler)
    assert [record.getMessage() for record in handler.records] == ["line 2", "line 3", "line 4"]


def test_port_available_detects_a_busy_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        host, port = busy.getsockname()
        assert port_available(host, port) is False
    assert port_available(host, port) is True


def test_startup_failure_explains_a_taken_port():
    async def make(exc: BaseException):
        future = asyncio.get_running_loop().create_future()
        future.set_exception(exc)
        return future

    assert _startup_failure(asyncio.run(make(SystemExit(3)))) == "address already in use"
    assert "boom" in str(_startup_failure(asyncio.run(make(RuntimeError("boom")))))


def test_startup_failure_is_none_while_running():
    async def make():
        return asyncio.get_running_loop().create_future()

    assert _startup_failure(asyncio.run(make())) is None
