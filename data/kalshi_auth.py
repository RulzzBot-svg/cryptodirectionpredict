"""Kalshi authenticated API client (RSA-PSS request signing).

Used for credential checks and (later) live portfolio/order calls.
Does not place orders by itself — call sites must opt in explicitly.
"""

from __future__ import annotations

import base64
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import requests
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

logger = logging.getLogger(__name__)

DEFAULT_PROD_BASE = "https://external-api.kalshi.com/trade-api/v2"
DEFAULT_DEMO_BASE = "https://external-api.demo.kalshi.co/trade-api/v2"


@dataclass(frozen=True)
class KalshiBalance:
    """Portfolio cash balance from GET /portfolio/balance."""

    balance_cents: int
    portfolio_value_cents: Optional[int] = None
    raw: Optional[dict[str, Any]] = None

    @property
    def balance_usd(self) -> float:
        return self.balance_cents / 100.0

    @property
    def portfolio_value_usd(self) -> Optional[float]:
        if self.portfolio_value_cents is None:
            return None
        return self.portfolio_value_cents / 100.0


class KalshiAuthError(RuntimeError):
    """Raised when Kalshi credentials are missing or invalid."""


def _normalize_pem(raw: bytes) -> bytes:
    """Repair the common ways a pasted PEM arrives broken.

    Handles literal ``\\n`` escapes (env vars), CRLF line endings (Windows),
    indentation, blank lines, and a missing trailing newline. The base64 body
    itself is never altered.
    """
    text = raw.decode("utf-8", errors="replace").strip()
    # Env-var style: whole key on one line with escaped newlines
    if "\\n" in text:
        text = text.replace("\\r\\n", "\n").replace("\\n", "\n")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.strip() for line in text.split("\n")]
    lines = [line for line in lines if line]
    if not lines:
        raise KalshiAuthError("Private key is empty")
    if not lines[0].startswith("-----BEGIN") or not lines[-1].startswith("-----END"):
        raise KalshiAuthError(
            "Private key is not a PEM block. It must start with a "
            "'-----BEGIN ... PRIVATE KEY-----' line and end with the matching "
            f"'-----END ...' line. Got first line: {lines[0][:40]!r}"
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


class KalshiAuthClient:
    """Sign and send authenticated Kalshi Trade API requests."""

    def __init__(
        self,
        api_key_id: str,
        private_key_pem: bytes,
        *,
        base_url: str = DEFAULT_PROD_BASE,
        timeout: float = 15.0,
    ) -> None:
        if not api_key_id.strip():
            raise KalshiAuthError("KALSHI_API_KEY_ID is empty")
        self.api_key_id = api_key_id.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        normalized = _normalize_pem(private_key_pem)
        try:
            self._private_key = serialization.load_pem_private_key(
                normalized, password=None, backend=default_backend()
            )
        except (ValueError, TypeError) as exc:
            head = normalized.split(b"\n", 1)[0].decode("utf-8", "replace")
            raise KalshiAuthError(
                f"Could not read the private key ({exc}). "
                f"Header line looks like: {head!r}. "
                "Check the key was pasted whole, including the BEGIN and END "
                "lines, and that it is the .key file Kalshi gave you."
            ) from exc

    @classmethod
    def from_env(cls, env: Optional[dict[str, str]] = None) -> "KalshiAuthClient":
        """Build a client from env vars / .env values.

        Required:
          KALSHI_API_KEY_ID
          KALSHI_PRIVATE_KEY_PATH  (PEM .key file)  OR  KALSHI_PRIVATE_KEY_PEM
        Optional:
          KALSHI_API_BASE   (prod default; use demo URL for demo.kalshi.co keys)
          KALSHI_ENV        demo|prod  (sets base URL if KALSHI_API_BASE unset)
        """
        source = env if env is not None else os.environ
        api_key_id = (source.get("KALSHI_API_KEY_ID") or "").strip()
        if not api_key_id:
            raise KalshiAuthError(
                "Missing KALSHI_API_KEY_ID. Create a key under "
                "Kalshi → Account & security → API Keys."
            )

        pem = (source.get("KALSHI_PRIVATE_KEY_PEM") or "").strip()
        path = (source.get("KALSHI_PRIVATE_KEY_PATH") or "").strip()
        if pem:
            private_key_pem = pem.encode("utf-8")
        elif path:
            # Easy mistake: pasting the key itself into the *_PATH variable
            if path.startswith("-----BEGIN"):
                raise KalshiAuthError(
                    "KALSHI_PRIVATE_KEY_PATH contains the key text, not a file "
                    "path. Either point it at a file (e.g. "
                    "/etc/secrets/kalshi.key) or put the key in "
                    "KALSHI_PRIVATE_KEY_PEM instead."
                )
            key_path = Path(path).expanduser()
            if not key_path.is_file():
                raise KalshiAuthError(f"Private key file not found: {key_path}")
            private_key_pem = key_path.read_bytes()
        else:
            raise KalshiAuthError(
                "Missing private key. Set KALSHI_PRIVATE_KEY_PATH "
                "(path to .key file) or KALSHI_PRIVATE_KEY_PEM."
            )

        base = (source.get("KALSHI_API_BASE") or "").strip()
        if not base:
            env_name = (source.get("KALSHI_ENV") or "prod").strip().lower()
            base = DEFAULT_DEMO_BASE if env_name == "demo" else DEFAULT_PROD_BASE

        return cls(api_key_id, private_key_pem, base_url=base)

    def _sign(self, timestamp_ms: str, method: str, path: str) -> str:
        path_without_query = path.split("?", 1)[0]
        message = f"{timestamp_ms}{method.upper()}{path_without_query}".encode("utf-8")
        signature = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def _headers(self, method: str, full_path: str) -> dict[str, str]:
        timestamp_ms = str(int(time.time() * 1000))
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": self._sign(timestamp_ms, method, full_path),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def request(
        self,
        method: str,
        endpoint: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
    ) -> requests.Response:
        """Authenticated request. endpoint is relative, e.g. /portfolio/balance."""
        if not endpoint.startswith("/"):
            endpoint = "/" + endpoint
        url = self.base_url + endpoint
        # Sign the full path from API root (include /trade-api/v2/...), no query.
        sign_path = urlparse(url).path
        headers = self._headers(method, sign_path)
        response = requests.request(
            method.upper(),
            url,
            headers=headers,
            params=params,
            json=json_body,
            timeout=self.timeout,
        )
        return response

    def get_balance(self) -> KalshiBalance:
        """GET /portfolio/balance — safest smoke test for API keys."""
        response = self.request("GET", "/portfolio/balance")
        if response.status_code == 401:
            raise KalshiAuthError(
                "401 Unauthorized — check API Key ID, private key PEM, "
                "and that demo keys use KALSHI_ENV=demo (or demo base URL)."
            )
        if response.status_code >= 400:
            raise KalshiAuthError(
                f"Balance request failed ({response.status_code}): {response.text[:300]}"
            )
        payload = response.json()
        balance_cents = int(payload.get("balance", 0))
        portfolio = payload.get("portfolio_value")
        portfolio_cents = int(portfolio) if portfolio is not None else None
        return KalshiBalance(
            balance_cents=balance_cents,
            portfolio_value_cents=portfolio_cents,
            raw=payload,
        )

    def create_order_v2(self, order: dict[str, Any]) -> dict[str, Any]:
        """POST /portfolio/events/orders — places a real order when called."""
        response = self.request("POST", "/portfolio/events/orders", json_body=order)
        if response.status_code == 401:
            raise KalshiAuthError("401 Unauthorized creating order")
        if response.status_code >= 400:
            raise KalshiAuthError(
                f"Create order failed ({response.status_code}): {response.text[:400]}"
            )
        return response.json()

    def cancel_order_v2(self, order_id: str) -> dict[str, Any]:
        """DELETE /portfolio/events/orders/{order_id}."""
        response = self.request("DELETE", f"/portfolio/events/orders/{order_id}")
        if response.status_code == 401:
            raise KalshiAuthError("401 Unauthorized canceling order")
        if response.status_code >= 400:
            raise KalshiAuthError(
                f"Cancel order failed ({response.status_code}): {response.text[:400]}"
            )
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {}

    def get_positions(self, *, ticker: Optional[str] = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if ticker:
            params["ticker"] = ticker
        response = self.request("GET", "/portfolio/positions", params=params or None)
        if response.status_code >= 400:
            raise KalshiAuthError(
                f"Positions failed ({response.status_code}): {response.text[:300]}"
            )
        return response.json()

    def get_orders(self, *, ticker: Optional[str] = None, status: Optional[str] = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if ticker:
            params["ticker"] = ticker
        if status:
            params["status"] = status
        response = self.request("GET", "/portfolio/orders", params=params or None)
        if response.status_code >= 400:
            raise KalshiAuthError(
                f"Orders failed ({response.status_code}): {response.text[:300]}"
            )
        return response.json()


def credentials_configured(env: Optional[dict[str, str]] = None) -> bool:
    """True if enough env is set to attempt an authenticated call."""
    source = env if env is not None else os.environ
    has_id = bool((source.get("KALSHI_API_KEY_ID") or "").strip())
    has_pem = bool((source.get("KALSHI_PRIVATE_KEY_PEM") or "").strip())
    has_path = bool((source.get("KALSHI_PRIVATE_KEY_PATH") or "").strip())
    return has_id and (has_pem or has_path)
