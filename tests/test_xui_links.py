from vpn_bot.config import XUISettings
from vpn_bot.services.xui import XUIClient


def test_build_vless_reality_link() -> None:
    settings = XUISettings(
        base_url="https://panel.example.com/secret",
        username="admin",
        password="secret",
        inbound_id=1,
        public_host="vpn.example.com",
        public_port=443,
    )
    client = XUIClient(settings)
    inbound = {
        "streamSettings": {
            "realitySettings": {
                "settings": {"publicKey": "PUBLIC_KEY"},
                "serverNames": ["www.cloudflare.com"],
                "shortIds": ["abcd1234"],
            }
        }
    }

    url = client.build_vless_reality_link(inbound, client_id="uuid-1", email="tg1@vpn.local")

    assert url.startswith("vless://uuid-1@vpn.example.com:443?")
    assert "security=reality" in url
    assert "pbk=PUBLIC_KEY" in url
    assert "sni=www.cloudflare.com" in url
