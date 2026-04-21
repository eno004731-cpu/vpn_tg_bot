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
    "panel/api/inbound/list",
    "panel/inbound/list",
    "api/inbounds/list",
    "api/inbound/list",
    "inbound/list",
)
ADD_CLIENT_PATHS = (
    "panel/api/inbounds/addClient",
    "panel/api/inbound/addClient",
    "panel/inbound/addClient",
    "api/inbounds/addClient",
    "api/inbound/addClient",
    "inbound/addClient",
)
UPDATE_CLIENT_PATHS = (
    "panel/api/inbounds/updateClient/{client_id}",
    "panel/api/inbound/updateClient/{client_id}",
    "panel/inbound/updateClient/{client_id}",
    "api/inbounds/updateClient/{client_id}",
    "api/inbound/updateClient/{client_id}",
    "inbound/updateClient/{client_id}",
)
DELETE_CLIENT_PATHS = (
    "panel/api/inbounds/{inbound_id}/delClient/{client_id}",
    "panel/api/inbound/{inbound_id}/delClient/{client_id}",
    "panel/inbound/{inbound_id}/delClient/{client_id}",
    "api/inbounds/{inbound_id}/delClient/{client_id}",
    "api/inbound/{inbound_id}/delClient/{client_id}",
    "inbound/{inbound_id}/delClient/{client_id}",
)


@dataclass(frozen=True)
class TrafficSnapshot:
    """Traffic counters returned by 3x-ui for one client email."""

    email: str
    upload_bytes: int
    download_bytes: int
    total_bytes: int
    expiry_time_ms: Optional[int] = None


@dataclass(frozen=True)
class ProvisionedClient:
    """VPN client data that must be saved after provisioning."""

    client_id: str
    email: str
    access_url: str


def _parse_json_maybe(value: Any) -> Any:
    """Decode 3x-ui fields that are sometimes JSON strings and sometimes objects."""

    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


class XUIClient:
    """Minimal async client for the 3x-ui API paths used by the bot."""

    def __init__(self, settings: XUISettings) -> None:
        self.settings = settings
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=10.0),
            follow_redirects=True,
            verify=settings.verify_tls,
        )
        self._logged_in = False

    async def close(self) -> None:
        """Close the underlying HTTP session."""

        await self._http.aclose()

    def _url(self, path: str) -> str:
        """Build an absolute 3x-ui URL from the configured base URL."""

        return f"{self.settings.base_url.rstrip('/')}/{path.lstrip('/')}"

    async def _http_request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Execute one HTTP request and turn network failures into XUIError."""

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
        """Log in once and reuse the session cookie for later API calls."""

        if self._logged_in:
            return
        credentials = {"username": self.settings.username, "password": self.settings.password}
        errors: list[str] = []
        for kwargs in ({"data": credentials}, {"json": credentials}):
            response = await self._http_request("POST", "login", **kwargs)
            response.raise_for_status()
            try:
                payload = response.json()
            except json.JSONDecodeError:
                payload = {}
            if not isinstance(payload, dict) or payload.get("success") is not False:
                self._logged_in = True
                return
            errors.append(payload.get("msg") or payload.get("message") or "неверный логин или пароль")
        raise XUIError("Не удалось войти в 3x-ui: " + "; ".join(errors))

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_data: Optional[dict[str, Any]] = None,
        data: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Call a 3x-ui endpoint and return its JSON payload."""

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
        """Try equivalent API paths because 3x-ui versions use different routes."""

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
        """Return normalized inbound definitions from 3x-ui."""

        payload = await self._request_with_fallback("GET", INBOUND_LIST_PATHS)
        items = payload.get("obj") or payload.get("data") or []
        return [self._normalize_inbound(item) for item in items]

    async def get_inbound(self, inbound_id: int) -> dict[str, Any]:
        """Find one inbound by id or raise XUIError when it does not exist."""

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
        limit_ip: int = 2,
    ) -> ProvisionedClient:
        """Create a VLESS client in 3x-ui and return the generated access link."""

        inbound = await self.get_inbound(inbound_id)
        client_settings = {
            "clients": [
                {
                    "id": client_id,
                    "flow": flow,
                    "email": email,
                    "limitIp": max(1, int(limit_ip)),
                    "totalGB": traffic_limit_bytes,
                    "expiryTime": int(expires_at.timestamp() * 1000),
                    "enable": True,
                    "speedLimit": 0,
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

    async def update_client_speed_limit(
        self,
        inbound_id: int,
        *,
        client_id: str,
        speed_limit_kbytes_per_second: int,
    ) -> None:
        """Update one client's speed limit in 3x-ui."""

        await self._update_client(
            inbound_id,
            client_id=client_id,
            changes={"speedLimit": max(0, int(speed_limit_kbytes_per_second))},
        )

    async def set_client_enabled(self, inbound_id: int, *, client_id: str, enabled: bool) -> None:
        """Enable or disable a client; disabling deletes it in current 3x-ui flow."""

        if not enabled:
            await self.delete_client(inbound_id, client_id=client_id)
            return
        changes: dict[str, Any] = {"enable": enabled}
        await self._update_client(inbound_id, client_id=client_id, changes=changes)

    async def delete_client(self, inbound_id: int, *, client_id: str) -> None:
        """Delete a client from the configured inbound."""

        paths = tuple(
            path.format(
                inbound_id=inbound_id,
                client_id=quote(client_id, safe=""),
            )
            for path in DELETE_CLIENT_PATHS
        )
        await self._request_with_fallback("POST", paths)

    async def fetch_traffic_map(self) -> dict[str, TrafficSnapshot]:
        """Collect traffic counters for all clients, keyed by 3x-ui email."""

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
        """Build the VLESS REALITY URL shown to the user."""

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
        """Normalize mixed 3x-ui response fields into dictionaries/lists."""

        normalized = dict(inbound)
        normalized["settings"] = _parse_json_maybe(normalized.get("settings")) or {}
        normalized["streamSettings"] = _parse_json_maybe(normalized.get("streamSettings")) or {}
        normalized["sniffing"] = _parse_json_maybe(normalized.get("sniffing")) or {}
        client_stats = normalized.get("clientStats") or normalized.get("client_stats") or []
        normalized["clientStats"] = [_parse_json_maybe(item) for item in client_stats]
        return normalized

    def _find_client(self, inbound: dict[str, Any], client_id: str) -> dict[str, Any]:
        """Find one client inside a normalized inbound settings payload."""

        settings = inbound.get("settings")
        if not isinstance(settings, dict):
            raise XUIError("Не удалось прочитать settings inbound в 3x-ui")
        for client in settings.get("clients", []):
            parsed_client = _parse_json_maybe(client)
            if isinstance(parsed_client, dict) and parsed_client.get("id") == client_id:
                return dict(parsed_client)
        raise XUIError(f"Клиент {client_id} не найден в inbound {inbound.get('id')}")

    async def _update_client(self, inbound_id: int, *, client_id: str, changes: dict[str, Any]) -> None:
        """Patch one client by loading the full client object and submitting changes."""

        inbound = await self.get_inbound(inbound_id)
        client = self._find_client(inbound, client_id)
        client.update(changes)
        payload = {
            "id": inbound_id,
            "settings": json.dumps({"clients": [client]}),
        }
        paths = tuple(path.format(client_id=quote(client_id, safe="")) for path in UPDATE_CLIENT_PATHS)
        await self._request_with_fallback("POST", paths, json_data=payload)

    @staticmethod
    def generate_client_id() -> str:
        """Generate a UUID used as the VLESS client id."""

        return str(uuid4())

    @staticmethod
    def generate_sub_id() -> str:
        """Generate the short subscription id expected by 3x-ui."""

        return secrets.token_urlsafe(9).replace("-", "").replace("_", "")[:16]
