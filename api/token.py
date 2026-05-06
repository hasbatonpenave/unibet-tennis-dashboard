"""Token acquisition and refresh for Unibet.fr API."""

import logging
import time

import aiohttp

from api.client import BASE_URL, HEADERS_TEMPLATE, LVS_TOKEN_URL

logger = logging.getLogger("unibet_api")


class TokenManager:
    """Acquires and refreshes the X-LVS-HSToken."""

    def __init__(self):
        self._token: str | None = None
        self._expiry: float = 0

    @property
    def token(self) -> str | None:
        return self._token

    async def fetch(self, session: aiohttp.ClientSession) -> str:
        """Fetch a fresh token from /lvs-api/acc/token."""
        url = LVS_TOKEN_URL
        headers = {**HEADERS_TEMPLATE, "referer": f"{BASE_URL}/paris-tennis"}
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
            self._token = data.get("hsToken")
            if not self._token:
                raise RuntimeError(f"Failed to get token: {data}")
            self._expiry = time.time() + 1800
            logger.info("Token acquired")
            return self._token

    def is_expired(self) -> bool:
        return time.time() > self._expiry - 60

    def set_token(self, token: str) -> None:
        self._token = token
        self._expiry = time.time() + 1800
