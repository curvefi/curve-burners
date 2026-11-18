"""What the non-CoW publishers share: a small JSON HTTP client with timeouts
and error reporting, and the salt / nonce choices.

The client is the one seam tests fake: a publisher takes any object with
``request(method, url, *, params=None, json=None, headers=None) -> Any``.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

import requests

log = logging.getLogger(__name__)


class ApiError(RuntimeError):
    """A rail's backend answered with an error status (or a payload that is one)."""

    def __init__(self, status: int, body: Any, url: str = ""):
        self.status = status
        self.body = body
        self.url = url
        super().__init__(f"{url} -> {status}: {_short(body)}")


def _short(body: Any, limit: int = 300) -> str:
    text = body if isinstance(body, str) else repr(body)
    return text if len(text) <= limit else text[:limit] + "..."


class JsonClient:
    """``requests`` with a timeout, JSON in and out, and ``ApiError`` on 4xx/5xx."""

    def __init__(self, *, session: requests.Session | None = None, timeout: float = 30.0):
        self.session = session or requests.Session()
        self.timeout = timeout

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
        data: str | None = None,
    ) -> Any:
        """``data`` is a pre-serialized body (for request signing); else ``json``."""
        try:
            response = self.session.request(
                method,
                url,
                params=params,
                json=json if data is None else None,
                data=data,
                headers=headers,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise ApiError(0, str(exc), url) from exc
        try:
            payload: Any = response.json() if response.content else None
        except ValueError:
            payload = response.text
        if response.status_code >= 400:
            raise ApiError(response.status_code, payload, url)
        return payload


def bearer(api_key: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def random_salt(bits: int = 96) -> int:
    """A fresh order salt / nonce; every (re)post needs one no fill has used."""
    return secrets.randbits(bits) or 1


def hex_bytes(data: bytes) -> str:
    return "0x" + data.hex()


__all__ = ["ApiError", "JsonClient", "bearer", "hex_bytes", "random_salt"]
