from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional
from urllib.parse import quote, urlencode
from uuid import uuid4

import httpx

from vpn_bot.config import XUISettings


class XUIError(RuntimeError):
    """Raised when the 3x-ui API returns an error."""


INBOUND_LIST_PATHS = (
    "panel/api/inbounds/list",
    "panel/inbound/list",
)
ADD_CLIENT_PATHS = (
    "panel/api/inbounds/addClient",
    "panel/inbound/addClient",
)


@dataclass(frozen=True)
class TrafficSnapshot:
    email: str
    upload_bytes: int
    download_bytes: int
    total_bytes: int
    expiry_time_ms: Optional[int] = None


@dataclass(frozen=True)
class ProvisionedClient:
    client_id: str
    email: str
    access_url: str


def _parse_json_maybe(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


class XUIClient:
    def __init__(self, settings: XUISettings) -> None:
        self.settings = settings
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=10.0),
            follow_redirects=True,
            verify=settings.verify_tls,
        )
        self._logged_in = False

    async def close(self) -> None:
        await self._http.aclose()

    def _url(self, path: str) -> str:
        return f"{self.settings.base_url.rstrip('/')}/{path.lstrip('/')}"

    async def _http_request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        url = self._url(path)
        try:
            return await self._http.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise XUIError(
                f"3x-ui не ответила вовремя: {url}. Проверьте xui.base_url, порт панели и firewall."
            ) from exc
        except httpx.RequestError as exc:
            raise XUIError(f"Не удалось подключиться к 3x-ui: {url}. {exc}") from exc

    async def _ensure_login(self) -> None:
        if self._logged_in:
            return
        response = await self._http_request(
            "POST",
            "login",
            data={"username": self.settings.username, "password": self.settings.password},
        )
        response.raise_for_status()
        self._logged_in = True

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_data: Optional[dict[str, Any]] = None,
        data: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        await self._ensure_login()
        response = await self._http_request(
            method,
            path,
            json=json_data,
            data=data,
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        body = response.text.strip()
        if not body:
            return {"success": True, "obj": None}
        payload = response.json()
        if isinstance(payload, dict) and payload.get("success") is False:
            raise XUIError(payload.get("msg") or payload.get("message") or "3x-ui returned error")
        return payload

    async def _request_with_fallback(
        self,
        method: str,
        paths: tuple[str, ...],
        *,
        json_data: Optional[dict[str, Any]] = None,
        data: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        last_not_found: Optional[httpx.HTTPStatusError] = None
        for path in paths:
            try:
                return await self._request(method, path, json_data=json_data, data=data)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
                last_not_found = exc
        raise XUIError(
            f"Не найден API 3x-ui. Проверьте версию панели и webBasePath. Пробовал пути: {', '.join(paths)}"
        ) from last_not_found

    async def list_inbounds(self) -> list[dict[str, Any]]:
        payload = await self._request_with_fallback("GET", INBOUND_LIST_PATHS)
        items = payload.get("obj") or payload.get("data") or []
        return [self._normalize_inbound(item) for item in items]

    async def get_inbound(self, inbound_id: int) -> dict[str, Any]:
        for inbound in await self.list_inbounds():
            if int(inbound.get("id")) == int(inbound_id):
                return inbound
        raise XUIError(f"Inbound {inbound_id} not found")

    async def add_client(
        self,
        inbound_id: int,
        *,
        client_id: str,
        email: str,
        traffic_limit_bytes: int,
        expires_at: datetime,
        flow: str,
        telegram_user_id: int,
        comment: str,
    ) -> ProvisionedClient:
        inbound = await self.get_inbound(inbound_id)
        client_settings = {
            "clients": [
                {
                    "id": client_id,
                    "flow": flow,
                    "email": email,
                    "limitIp": 2,
                    "totalGB": traffic_limit_bytes,
                    "expiryTime": int(expires_at.timestamp() * 1000),
                    "enable": True,
                    "tgId": str(telegram_user_id),
                    "subId": self.generate_sub_id(),
                    "comment": comment,
                    "reset": 0,
                }
            ]
        }
        payload = {
            "id": inbound_id,
            "settings": json.dumps(client_settings),
        }
        await self._request_with_fallback("POST", ADD_CLIENT_PATHS, json_data=payload)
        access_url = self.build_vless_reality_link(inbound, client_id=client_id, email=email)
        return ProvisionedClient(client_id=client_id, email=email, access_url=access_url)

    async def fetch_traffic_map(self) -> dict[str, TrafficSnapshot]:
        inbounds = await self.list_inbounds()
        traffic: dict[str, TrafficSnapshot] = {}
        for inbound in inbounds:
            for item in inbound.get("clientStats", []):
                email = item.get("email")
                if not email:
                    continue
                up = int(item.get("up", 0) or 0)
                down = int(item.get("down", 0) or 0)
                traffic[email] = TrafficSnapshot(
                    email=email,
                    upload_bytes=up,
                    download_bytes=down,
                    total_bytes=up + down,
                    expiry_time_ms=int(item["expiryTime"]) if item.get("expiryTime") not in (None, "") else None,
                )
        return traffic

    def build_vless_reality_link(self, inbound: dict[str, Any], *, client_id: str, email: str) -> str:
        stream_settings = inbound.get("streamSettings", {})
        reality = stream_settings.get("realitySettings", {})
        reality_settings = reality.get("settings", {})

        public_key = reality_settings.get("publicKey")
        server_names = reality.get("serverNames") or []
        short_ids = reality.get("shortIds") or []
        if not public_key:
            raise XUIError("Не найден publicKey в настройках REALITY")
        if not server_names:
            raise XUIError("Не найден serverNames в настройках REALITY")

        params = {
            "type": "tcp",
            "security": "reality",
            "pbk": public_key,
            "fp": self.settings.fingerprint,
            "sni": server_names[0],
            "sid": short_ids[0] if short_ids else "",
            "spx": self.settings.spider_x,
            "flow": self.settings.flow,
            "encryption": "none",
        }
        query = urlencode(params, quote_via=quote)
        remark = quote(email)
        return f"vless://{client_id}@{self.settings.public_host}:{self.settings.public_port}?{query}#{remark}"

    def _normalize_inbound(self, inbound: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(inbound)
        normalized["settings"] = _parse_json_maybe(normalized.get("settings")) or {}
        normalized["streamSettings"] = _parse_json_maybe(normalized.get("streamSettings")) or {}
        normalized["sniffing"] = _parse_json_maybe(normalized.get("sniffing")) or {}
        client_stats = normalized.get("clientStats") or normalized.get("client_stats") or []
        normalized["clientStats"] = [_parse_json_maybe(item) for item in client_stats]
        return normalized

    @staticmethod
    def generate_client_id() -> str:
        return str(uuid4())

    @staticmethod
    def generate_sub_id() -> str:
        return secrets.token_urlsafe(9).replace("-", "").replace("_", "")[:16]
