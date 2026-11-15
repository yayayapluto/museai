from __future__ import annotations

import sys

import pytest

from museai.config import Settings
from museai.errors import ConfigError


def test_browser_driver_requires_extra(monkeypatch):
    """Without the optional [browser] deps the registry must fail with a fixable message."""
    for name in ("websockets", "websockets.asyncio", "websockets.asyncio.client"):
        monkeypatch.setitem(sys.modules, name, None)
    for name in list(sys.modules):
        if name.startswith("museai.drivers.browser"):
            monkeypatch.delitem(sys.modules, name, raising=False)

    from museai.drivers.registry import create_driver

    with pytest.raises(ConfigError, match=r"museai\[browser\]"):
        create_driver(Settings(_env_file=None, driver="browser"))
