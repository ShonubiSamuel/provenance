"""GitHub App authentication (with a PAT fallback for local dev).

App flow: sign a short-lived JWT with the app private key → exchange it for an
installation access token (valid ~1h) → use that as the Bearer token. We cache the
installation token and refresh it just before expiry.
"""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

import httpx
import jwt

from packages.core.settings import Settings


class TokenProvider:
    """Yields a valid Authorization header value, refreshing App tokens as needed."""

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._install_token: str | None = None
        self._expires_at: float = 0.0

    def _app_jwt(self) -> str:
        assert self._s.gh_app_id and self._s.gh_app_private_key_path
        key = Path(self._s.gh_app_private_key_path).read_text()
        now = int(time.time())
        payload = {"iat": now - 60, "exp": now + 9 * 60, "iss": self._s.gh_app_id}
        return jwt.encode(payload, key, algorithm="RS256")

    async def _refresh_installation_token(self) -> None:
        url = (
            f"https://api.github.com/app/installations/"
            f"{self._s.gh_app_installation_id}/access_tokens"
        )
        headers = {
            "Authorization": f"Bearer {self._app_jwt()}",
            "Accept": "application/vnd.github+json",
        }
        async with httpx.AsyncClient(timeout=30) as c:
            resp = await c.post(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        self._install_token = data["token"]
        expires = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
        # Refresh a minute early to avoid using a token that expires mid-request.
        self._expires_at = expires.timestamp() - 60

    async def authorization(self) -> str:
        if self._s.has_app_auth:
            if self._install_token is None or time.time() >= self._expires_at:
                await self._refresh_installation_token()
            return f"Bearer {self._install_token}"
        if self._s.gh_pat:
            return f"Bearer {self._s.gh_pat}"
        raise RuntimeError(
            "No GitHub credentials configured. Set GH_APP_* (preferred) or GH_PAT in .env."
        )
