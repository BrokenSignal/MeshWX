"""Client for the NWS active-alerts API.

Honors the NWS API User-Agent requirement and applies exponential backoff on
429/5xx responses. Never raises out of the poll path unexpectedly; the poller
converts failures into surfaced errors.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from . import __version__

logger = logging.getLogger("mesh_wx.nws")

API_URL = "https://api.weather.gov/alerts/active"


class NWSError(Exception):
    pass


class NWSClient:
    def __init__(self, contact: str, timeout: float = 30.0):
        self.contact = contact
        self.timeout = timeout
        self.last_server_date: str | None = None   # NWS response Date header (clock-skew check)

    def _user_agent(self) -> str:
        # NWS asks for "app/version (contact)".
        return f"mesh-wx/{__version__} ({self.contact})"

    async def fetch_active(
        self, zones: str, max_retries: int = 4
    ) -> tuple[dict, str]:
        """Fetch active alerts for comma-separated zones.

        Returns (parsed_json, raw_text). Retries 429/5xx with exponential
        backoff. Raises NWSError after exhausting retries.
        """
        params = {"zone": zones}
        headers = {
            "User-Agent": self._user_agent(),
            "Accept": "application/geo+json",
        }
        delay = 2.0
        last_err = "unknown error"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for attempt in range(1, max_retries + 1):
                try:
                    resp = await client.get(API_URL, params=params, headers=headers)
                except httpx.HTTPError as exc:
                    last_err = f"request error: {exc}"
                    logger.warning("NWS request error (attempt %d): %s", attempt, exc)
                else:
                    if resp.status_code == 200:
                        self.last_server_date = resp.headers.get("date")
                        return resp.json(), resp.text
                    if resp.status_code == 429 or resp.status_code >= 500:
                        last_err = f"HTTP {resp.status_code}"
                        logger.warning(
                            "NWS %s (attempt %d), backing off %.0fs",
                            resp.status_code, attempt, delay,
                        )
                    else:
                        raise NWSError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                if attempt < max_retries:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 60.0)
        raise NWSError(f"gave up after {max_retries} attempts: {last_err}")
