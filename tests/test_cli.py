"""CLI tests: the dashboard is built from real objects, so its inputs stay testable."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import socket
import sys

from museai.cli import (
    DashboardState,
    RingHandler,
    _console,
    _read_cookie_input,
    _startup_failure,
    apply_overrides,
    build_parser,
    info_lines,
    masked,
    port_available,
    run_doctor,
    run_setup,
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


def test_parser_exposes_setup_and_doctor():
    parser = build_parser()
    assert parser.parse_args(["setup"]).command == "setup"
    assert parser.parse_args(["doctor", "--live"]).live is True
    assert parser.parse_args(["start", "--no-ui"]).command == "start"


def test_setup_without_cookies_writes_env_and_points_at_cookies(settings, monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))  # not a tty -> skip the prompt
    code = asyncio.run(run_setup(settings, build_parser().parse_args(["setup"])))
    assert code == 1
    assert (tmp_path / ".env").read_text(encoding="utf-8").count("MUSE2API_DRIVER=http") == 1
    assert "no cookies imported yet" in capsys.readouterr().out


def test_setup_imports_a_pasted_export(settings, monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    export = tmp_path / "cookies.json"
    export.write_text(
        json.dumps(
            [
                {"name": "hatch_sess", "value": "s"},
                {"name": "hatch_gw", "value": "g"},
                {"name": "hatch_native_auth_device", "value": "d"},
            ]
        ),
        encoding="utf-8",
    )
    args = build_parser().parse_args(["setup", "--cookies", str(export), "--no-verify"])
    assert asyncio.run(run_setup(settings, args)) == 0
    out = capsys.readouterr().out
    assert "account saved" in out
    assert "http://127.0.0.1:18610/v1" in out


def test_setup_refuses_an_incomplete_export(settings, monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    export = tmp_path / "cookies.json"
    export.write_text('{"hatch_sess": "only"}', encoding="utf-8")
    args = build_parser().parse_args(["setup", "--cookies", str(export), "--no-verify"])
    assert asyncio.run(run_setup(settings, args)) == 1
    assert "missing required cookies" in capsys.readouterr().out


def test_doctor_fails_until_an_account_exists(settings, monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    code = asyncio.run(run_doctor(settings, build_parser().parse_args(["doctor"])))
    assert code == 1
    out = capsys.readouterr().out
    assert "accounts" in out and "muse2api setup" in out


def test_cookie_prompt_accepts_a_path(tmp_path, monkeypatch):
    export = tmp_path / "export.json"
    export.write_text('{"hatch_sess": "s"}', encoding="utf-8")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda _prompt: str(export))
    text = _read_cookie_input(build_parser().parse_args(["setup"]), _console())
    assert text == '{"hatch_sess": "s"}'


def test_cookie_prompt_accepts_pasted_json(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda _prompt: '{"hatch_gw": "g"}')
    text = _read_cookie_input(build_parser().parse_args(["setup"]), _console())
    assert text == '{"hatch_gw": "g"}'
