"""Cookie import: turn whatever a browser exports into a cookie mapping.

Every browser and extension exports a different shape, and a wrong guess silently
produces an account that can never authenticate. :func:`parse_any` accepts the four
formats people actually paste:

* Cookie-Editor / DevTools JSON array of ``{name, value}`` objects
* a JSON object ``{"name": "value"}``
* Netscape ``cookies.txt`` (tab separated, ``#HttpOnly_`` prefixed domains)
* a raw ``Cookie`` header string ``a=b; c=d``
"""

from __future__ import annotations

import json
from typing import Any

from ..upstream.muse import SESSION_COOKIES


class CookieParseError(ValueError):
    """The input did not look like any supported cookie export."""


def parse_any(text: str) -> dict[str, str]:
    text = text.strip()
    if not text:
        raise CookieParseError("cookie input is empty")
    if text[0] in "[{":
        return _parse_json(text)
    if "\t" in text and text.startswith(("#", ".")) or text.startswith("#HttpOnly_"):
        return _parse_netscape(text)
    cookies = _parse_header(text)
    if not cookies:
        raise CookieParseError("no cookies found; expected JSON, cookies.txt or 'a=b; c=d'")
    return cookies


def _parse_json(text: str) -> dict[str, str]:
    try:
        data: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CookieParseError(f"invalid JSON: {exc}") from exc
    cookies: dict[str, str] = {}
    if isinstance(data, dict):
        raw = data.get("cookies") if isinstance(data.get("cookies"), (list, dict)) else data
        if isinstance(raw, dict):
            return {str(k): str(v) for k, v in raw.items() if v}
        data = raw
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            name, value = item.get("name"), item.get("value")
            if name and value is not None:
                cookies[str(name)] = str(value)
    if not cookies:
        raise CookieParseError("JSON input contained no name/value pairs")
    return cookies


def _parse_netscape(text: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") and not line.startswith("#HttpOnly_"):
            continue
        line = line.removeprefix("#HttpOnly_")
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        name, value = parts[5].strip(), parts[6].strip()
        if name:
            cookies[name] = value
    if not cookies:
        raise CookieParseError("Netscape cookie file contained no cookies")
    return cookies


def _parse_header(text: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in text.replace("\n", ";").split(";"):
        name, sep, value = part.partition("=")
        if not sep:
            continue
        name, value = name.strip(), value.strip().strip('"')
        if name:
            cookies[name] = value
    return cookies


def missing_required(cookies: dict[str, str]) -> list[str]:
    """Session cookies the gateway needs; anything else is optional."""
    return [name for name in SESSION_COOKIES if not cookies.get(name)]
