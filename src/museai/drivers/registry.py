from __future__ import annotations

from ..config import Settings
from ..errors import ConfigError
from .base import MuseDriver


def create_driver(settings: Settings) -> MuseDriver:
    """Instantiate the driver selected by ``MUSE2API_DRIVER``. Imports are lazy so that
    optional heavy dependencies of one driver never affect the others."""
    if settings.driver == "mock":
        from .mock import MockDriver

        return MockDriver()
    if settings.driver == "browser":
        try:
            from .browser.driver import BrowserDriver
        except ImportError as exc:  # websockets lives in the optional [browser] extra
            raise ConfigError(
                "browser driver requires extra dependencies: pip install 'museai[browser]'"
            ) from exc

        return BrowserDriver(settings)
    if settings.driver == "http":
        try:
            from .http.driver import HttpDriver
        except ImportError as exc:  # websockets + noiseprotocol live in the [http] extra
            raise ConfigError(
                "http driver requires extra dependencies: pip install 'museai[http]'"
            ) from exc

        return HttpDriver(settings)
    raise ValueError(f"unknown driver: {settings.driver}")
