import json
from datetime import datetime, timezone

import httpx
import pytest

from vpn_bot.config import XUISettings
from vpn_bot.services.xui import XUIClient, XUIError


def make_settings() -> XUISettings:
    return XUISettings(
        base_url="https://panel.example.com/secret",
        username="admin",
        password="secret",
        inbound_id=1,
        public_host="vpn.example.com",
        public_port=443,
    )


def make_reality_inbound() -> dict:
    return {
        "id": 1,
        "settings": {
            "clients": [
                {
                    "id": "uuid-1",
                    "email": "tg1@vpn.local",
                    "flow": "xtls-rprx-vision",
                    "limitIp": 2,
                    "totalGB": 1024,
                    "expiryTime": 0,
                    "enable": True,
                    "speedLimit": 0,
                    "tgId": "123",
                    "subId": "subid",
                    "comment": "test",
                    "reset": 0,
                }
            ]
        },
        "streamSettings": {
            "realitySettings": {
                "settings": {"publicKey": "PUBLIC_KEY"},
                "serverNames": ["www.cloudflare.com"],
                "shortIds": ["abcd1234"],
            }
        },
    }


def http_404(path: str) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", f"https://panel.example.com/secret/{path}")
    response = httpx.Response(404, request=request)
    return httpx.HTTPStatusError("not found", request=request, response=response)


def test_build_vless_reality_link() -> None:
    client = XUIClient(make_settings())
    inbound = make_reality_inbound()

    url = client.build_vless_reality_link(inbound, client_id="uuid-1", email="tg1@vpn.local")

    assert url.startswith("vless://uuid-1@vpn.example.com:443?")
    assert "security=reality" in url
    assert "pbk=PUBLIC_KEY" in url
    assert "sni=www.cloudflare.com" in url


async def test_list_inbounds_uses_modern_api_path_first() -> None:
    client = XUIClient(make_settings())
    calls = []

    async def fake_request(method, path, *, json_data=None, data=None):
        calls.append((method, path))
        return {"obj": []}

    client._request = fake_request

    assert await client.list_inbounds() == []
    assert calls == [("GET", "panel/api/inbounds/list")]

    await client.close()


async def test_list_inbounds_falls_back_to_singular_api_path() -> None:
    client = XUIClient(make_settings())
    calls = []

    async def fake_request(method, path, *, json_data=None, data=None):
        calls.append((method, path))
        if path == "panel/api/inbounds/list":
            raise http_404(path)
        return {"obj": []}

    client._request = fake_request

    assert await client.list_inbounds() == []
    assert calls == [
        ("GET", "panel/api/inbounds/list"),
        ("GET", "panel/api/inbound/list"),
    ]

    await client.close()


async def test_list_inbounds_falls_back_to_legacy_api_path() -> None:
    client = XUIClient(make_settings())
    calls = []

    async def fake_request(method, path, *, json_data=None, data=None):
        calls.append((method, path))
        if path in {"panel/api/inbounds/list", "panel/api/inbound/list"}:
            raise http_404(path)
        return {"obj": []}

    client._request = fake_request

    assert await client.list_inbounds() == []
    assert calls == [
        ("GET", "panel/api/inbounds/list"),
        ("GET", "panel/api/inbound/list"),
        ("GET", "panel/inbound/list"),
    ]

    await client.close()


async def test_add_client_uses_modern_api_and_serializes_settings() -> None:
    client = XUIClient(make_settings())
    requests = []

    async def fake_get_inbound(inbound_id):
        assert inbound_id == 1
        return make_reality_inbound()

    async def fake_request(method, path, *, json_data=None, data=None):
        requests.append((method, path, json_data))
        return {"success": True}

    client.get_inbound = fake_get_inbound
    client._request = fake_request

    await client.add_client(
        1,
        client_id="uuid-1",
        email="tg1@vpn.local",
        traffic_limit_bytes=1024,
        expires_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        flow="xtls-rprx-vision",
        telegram_user_id=123,
        comment="test",
        limit_ip=1,
    )

    assert len(requests) == 1
    method, path, payload = requests[0]
    assert method == "POST"
    assert path == "panel/api/inbounds/addClient"
    assert payload["id"] == 1
    settings = json.loads(payload["settings"])
    assert settings["clients"][0]["id"] == "uuid-1"
    assert settings["clients"][0]["flow"] == "xtls-rprx-vision"
    assert settings["clients"][0]["email"] == "tg1@vpn.local"
    assert settings["clients"][0]["limitIp"] == 1
    assert settings["clients"][0]["speedLimit"] == 0

    await client.close()


async def test_update_client_speed_limit_uses_update_endpoint() -> None:
    client = XUIClient(make_settings())
    requests = []

    async def fake_get_inbound(inbound_id):
        assert inbound_id == 1
        return make_reality_inbound()

    async def fake_request(method, path, *, json_data=None, data=None):
        requests.append((method, path, json_data))
        return {"success": True}

    client.get_inbound = fake_get_inbound
    client._request = fake_request

    await client.update_client_speed_limit(
        1,
        client_id="uuid-1",
        speed_limit_kbytes_per_second=1250,
    )

    method, path, payload = requests[0]
    assert method == "POST"
    assert path == "panel/api/inbounds/updateClient/uuid-1"
    settings = json.loads(payload["settings"])
    assert settings["clients"][0]["id"] == "uuid-1"
    assert settings["clients"][0]["email"] == "tg1@vpn.local"
    assert settings["clients"][0]["speedLimit"] == 1250

    await client.close()


async def test_set_client_enabled_false_deletes_client_without_listing_inbounds() -> None:
    client = XUIClient(make_settings())
    requests = []

    async def fake_request(method, path, *, json_data=None, data=None):
        requests.append((method, path, json_data))
        return {"success": True}

    client._request = fake_request

    await client.set_client_enabled(1, client_id="uuid-1", enabled=False)

    method, path, payload = requests[0]
    assert method == "POST"
    assert path == "panel/api/inbounds/1/delClient/uuid-1"
    assert payload is None

    await client.close()


async def test_login_rejects_success_false_response() -> None:
    client = XUIClient(make_settings())

    async def fake_http_request(method, path, **kwargs):
        request = httpx.Request(method, f"https://panel.example.com/secret/{path}")
        return httpx.Response(
            200,
            json={"success": False, "msg": "Неверные данные учетной записи.", "obj": None},
            request=request,
        )

    client._http_request = fake_http_request

    with pytest.raises(XUIError, match="Не удалось войти"):
        await client.list_inbounds()

    await client.close()
