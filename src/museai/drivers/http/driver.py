"""HTTP/WebSocket protocol driver: talks to muse.ai without a browser.

Transport and wire format live in :mod:`museai.drivers.http.gateway`; this class maps
the driver contract onto it. Chat is supported today; media generation still needs the
upstream image/video RPCs mapped (see ``docs/protocol.md``).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from ...accounts.model import Account
from ...config import Settings
from ...errors import ConfigError
from ...upstream import muse
from ..base import ChatRequest, DriverCapabilities, MuseDriver, SessionInfo
from . import gateway


class HttpDriver(MuseDriver):
    name = "http"
    capabilities = DriverCapabilities(chat=True, renew_session=True)

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def chat_stream(self, account: Account, req: ChatRequest) -> AsyncIterator[str]:
        prompt = "\n\n".join([*(f"{role}: {text}" for role, text in req.turns), req.prompt])
        timeout = req.timeout or self.settings.http_timeout
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        try:
            session = await gateway.open_session(account, self.settings)
        except OSError as exc:  # websockets raises plain OSError on dial failures
            raise ConfigError(f"cannot reach the muse.ai gateway: {exc}") from exc
        try:
            async for event in gateway.stream_chat(session, prompt, deadline):
                if req.cancel is not None and req.cancel.is_set():
                    return
                text = gateway.text_delta(event)
                if text:
                    yield text
        finally:
            await session.aclose()

    async def renew_session(self, account: Account) -> SessionInfo:
        return await muse.renew_session(account.cookies)
