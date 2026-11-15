"""Cookie import tests: every browser export shape must parse to the same mapping."""

from __future__ import annotations

import pytest

from museai.core.cookies import CookieParseError, missing_required, parse_any

EXPECTED = {"hatch_sess": "abc", "hatch_gw": "eu1"}


def test_cookie_editor_json_array():
    raw = '[{"name": "hatch_sess", "value": "abc"}, {"name": "hatch_gw", "value": "eu1"}]'
    assert parse_any(raw) == EXPECTED


def test_json_object():
    assert parse_any('{"hatch_sess": "abc", "hatch_gw": "eu1"}') == EXPECTED


def test_json_with_cookies_key():
    assert parse_any('{"cookies": {"hatch_sess": "abc", "hatch_gw": "eu1"}}') == EXPECTED


def test_netscape_cookies_txt():
    raw = (
        "# Netscape HTTP Cookie File\n"
        ".muse.ai\tTRUE\t/\tTRUE\t0\thatch_sess\tabc\n"
        "#HttpOnly_.muse.ai\tTRUE\t/\tTRUE\t0\thatch_gw\teu1\n"
    )
    assert parse_any(raw) == EXPECTED


def test_raw_header_string():
    assert parse_any("hatch_sess=abc; hatch_gw=eu1; datr=") == {**EXPECTED, "datr": ""}


def test_empty_input_is_rejected():
    with pytest.raises(CookieParseError):
        parse_any("   ")


def test_missing_required_lists_session_cookies():
    assert "hatch_gw" in missing_required({"hatch_sess": "abc"})
    assert missing_required({"hatch_sess": "a", "hatch_gw": "b", "hatch_native_auth_device": "c"}) == []
