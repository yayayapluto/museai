"""Onboarding: env writing, cookie exports, doctor rows, and account persistence."""

from __future__ import annotations

import base64
import json
import time

import pytest

from museai import onboarding
from museai.accounts.model import AccountStatus

COOKIES = {
    "hatch_sess": "sess-value",
    "hatch_gw": "gw-value",
    "hatch_native_auth_device": "device-value",
}

EXPORT_JSON = json.dumps(
    [
        {"name": name, "value": value, "expirationDate": 1900000000}
        for name, value in COOKIES.items()
    ]
)


def test_env_file_is_created_with_the_driver(tmp_path):
    path = tmp_path / ".env"
    assert onboarding.write_env_driver("http", path) == "created"
    assert onboarding.read_env(path)["MUSE2API_DRIVER"] == "http"

    assert onboarding.write_env_driver("http", path) == "unchanged"
    assert onboarding.write_env_driver("browser", path) == "updated"
    assert onboarding.read_env(path)["MUSE2API_DRIVER"] == "browser"


def test_env_append_keeps_existing_settings(tmp_path):
    path = tmp_path / ".env"
    path.write_text("MUSE2API_API_KEY=keep-me\n", encoding="utf-8")
    assert onboarding.write_env_driver("http", path) == "updated"
    body = path.read_text(encoding="utf-8")
    assert "MUSE2API_API_KEY=keep-me" in body
    assert "MUSE2API_DRIVER=http" in body


def test_shell_variable_beats_env_file():
    assert onboarding.driver_conflict("http") is None
    assert onboarding.driver_conflict("browser") is None


def test_parse_export_keeps_expiry_and_flags_missing():
    export = onboarding.parse_export(EXPORT_JSON)
    assert export.cookies == COOKIES
    assert export.expires["hatch_sess"] == 1900000000
    assert export.missing == []


def test_parse_export_reads_jwt_expiry():
    payload = base64.urlsafe_b64encode(json.dumps({"exp": 1900000123}).encode()).decode()
    token = f"v1:{'x' * 8}.{payload}.sig"
    export = onboarding.parse_export(f"hatch_sess={token}; hatch_gw=g; hatch_native_auth_device=d")
    assert export.expires["hatch_sess"] == 1900000123


def test_parse_export_reports_missing_cookies():
    export = onboarding.parse_export('{"hatch_sess": "only-this"}')
    assert export.missing == ["hatch_gw", "hatch_native_auth_device"]


def test_parse_export_rejects_junk():
    with pytest.raises(onboarding.CookieParseError):
        onboarding.parse_export("not a cookie export at all")


async def test_save_account_roundtrip(settings):
    account = await onboarding.async_save_account(settings, COOKIES, {"hatch_sess": 1900000000})
    assert account.status is AccountStatus.ACTIVE
    stored = await onboarding.load_accounts(settings)
    assert len(stored) == 1
    assert stored[0].cookies == COOKIES
    assert stored[0].cookie_expires == {"hatch_sess": 1900000000}


async def test_save_account_updates_in_place(settings):
    await onboarding.async_save_account(settings, COOKIES, {})
    updated = await onboarding.async_save_account(settings, {**COOKIES, "hatch_vml": "new"}, {})
    stored = await onboarding.load_accounts(settings)
    assert len(stored) == 1
    assert stored[0].id == updated.id
    assert stored[0].cookies["hatch_vml"] == "new"


def test_doctor_without_an_account_points_at_setup(settings):
    checks = onboarding.doctor_checks(settings, [])
    by_name = {check.name: check for check in checks}
    assert by_name["accounts"].ok is False
    assert "setup" in by_name["accounts"].fix


def test_doctor_flags_expired_and_cooling(settings):
    expired = onboarding.Account(
        cookies=COOKIES, cookie_expires={"hatch_sess": int(time.time()) - 60}
    )
    checks = onboarding.doctor_checks(settings, [expired])
    detail = next(c for c in checks if c.name.startswith("account")).detail
    assert "expired hatch_sess" in detail

    cooling = onboarding.Account(
        status=AccountStatus.COOLING,
        cooldown_until=time.time() + 30,
        last_error="no first token from assistant",
        cookies=COOKIES,
    )
    detail = next(c for c in onboarding.doctor_checks(settings, [cooling]) if c.name.startswith("account")).detail
    assert "cooling" in detail and "no first token" in detail


def test_doctor_flags_a_missing_http_extra(settings, monkeypatch):
    settings.driver = "http"
    real = onboarding._installed
    monkeypatch.setattr(onboarding, "_installed", lambda name: name != "noise" and real(name))
    checks = onboarding.doctor_checks(settings, [])
    extras = next(c for c in checks if c.name == "http extras")
    assert extras.ok is False
    assert "noise" in extras.detail


def test_client_snippets_carry_the_real_endpoint(settings):
    text = "\n".join(onboarding.client_snippets(settings, "sk-test"))
    assert f"http://{settings.host}:{settings.port}/v1" in text
    assert "sk-test" in text
    assert "/v1/chat/completions" in text


def test_env_conflicts_report_a_shell_override(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("MUSE2API_API_KEY=from-file\n", encoding="utf-8")
    monkeypatch.setenv("MUSE2API_API_KEY", "from-shell-long-value")
    conflicts = dict((key, (shell, env)) for key, shell, env in onboarding.env_conflicts())
    _, env_value = conflicts["MUSE2API_API_KEY"]
    assert env_value == "from-file"


def test_doctor_flags_the_override(settings, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("MUSE2API_DRIVER=http\n", encoding="utf-8")
    settings.driver = "http"
    monkeypatch.setenv("MUSE2API_API_KEY", "sk-someone-elses-key")
    rows = onboarding.doctor_checks(settings, [])
    assert any(row.name == "env override" and not row.ok for row in rows)
