from typing import Any, Optional

import httpx

from app.config import Settings
from app.models import OrderRequest, OrderResult


class IGClientError(RuntimeError):
    pass


class IGMarketsClient:
    DEMO_URL = "https://demo-api.ig.com/gateway/deal"
    LIVE_URL = "https://api.ig.com/gateway/deal"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base_url = self.LIVE_URL if settings.ig_account_type == "live" else self.DEMO_URL
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=15)
        self._cst: Optional[str] = None
        self._security_token: Optional[str] = None

    async def close(self) -> None:
        await self._client.aclose()

    async def authenticate(self) -> None:
        response = await self._request(
            "POST",
            "/session",
            json={
                "identifier": self.settings.ig_identifier,
                "password": self.settings.ig_password.get_secret_value(),
            },
            auth_required=False,
            version="2",
        )

        self._cst = response.headers.get("CST")
        self._security_token = response.headers.get("X-SECURITY-TOKEN")
        if not self._cst or not self._security_token:
            raise IGClientError("IG authentication succeeded without session tokens")

        if self.settings.ig_account_id:
            await self._switch_account(self.settings.ig_account_id)

    async def place_market_order(self, order: OrderRequest) -> OrderResult:
        if self.settings.ig_account_type == "live" and not self.settings.live_trading_enabled:
            return OrderResult(
                submitted=False,
                mode="safety_block",
                reason="live IG account requires TRADING_MODE=live",
            )

        if not self._cst or not self._security_token:
            await self.authenticate()

        payload = {
            "epic": order.symbol,
            "expiry": "-",
            "direction": "BUY" if order.side == "long" else "SELL",
            "size": order.size,
            "orderType": "MARKET",
            "timeInForce": "FILL_OR_KILL",
            "forceOpen": True,
            "guaranteedStop": False,
            "stopDistance": order.stop_distance,
            "limitDistance": order.take_profit_distance,
        }

        response = await self._request("POST", "/positions/otc", json=payload, version="2")
        body = response.json()
        return OrderResult(
            submitted=True,
            mode=self.settings.trading_mode,
            deal_reference=body.get("dealReference"),
            raw_response=body,
        )

    async def _switch_account(self, account_id: str) -> None:
        await self._request(
            "PUT",
            "/session",
            json={"accountId": account_id, "defaultAccount": ""},
            version="1",
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[dict[str, Any]] = None,
        auth_required: bool = True,
        version: str = "1",
    ) -> httpx.Response:
        headers = {
            "X-IG-API-KEY": self.settings.ig_api_key.get_secret_value(),
            "Version": version,
            "Accept": "application/json; charset=UTF-8",
            "Content-Type": "application/json; charset=UTF-8",
        }
        if auth_required:
            if not self._cst or not self._security_token:
                raise IGClientError("IG request attempted without authentication")
            headers["CST"] = self._cst
            headers["X-SECURITY-TOKEN"] = self._security_token

        try:
            response = await self._client.request(method, path, headers=headers, json=json)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise IGClientError(
                f"IG API returned {exc.response.status_code}: {exc.response.text}"
            ) from exc
        except httpx.HTTPError as exc:
            raise IGClientError(f"IG API request failed: {exc}") from exc

        return response
