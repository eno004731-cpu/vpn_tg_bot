import pytest

from vpn_bot.config import ConfigError, load_plans, load_settings


def test_load_plans_reads_stars_options(tmp_path) -> None:
    plans_file = tmp_path / "plans.toml"
    plans_file.write_text(
        """
[[plans]]
code = "stars_test"
title = "Тест оплаты Stars"
price_rub = "0.00"
price_stars = 1
duration_days = 0
traffic_limit_gb = 0
daily_limit_gb = 1
description = "Проверка оплаты."
provision_access = true
device_limit = 1
one_time_per_user = true
""",
        encoding="utf-8",
    )

    plan = load_plans(plans_file)["stars_test"]

    assert plan.supports_stars
    assert not plan.supports_transfer
    assert plan.provision_access
    assert plan.price_stars == 1
    assert plan.daily_limit_gb == 1
    assert plan.daily_limit_bytes == 1024 * 1024 * 1024
    assert plan.device_limit == 1
    assert plan.one_time_per_user is True


def write_runtime_config(tmp_path, xui_block: str) -> None:
    (tmp_path / "runtime.toml").write_text(
        f"""
[app]
bot_token = "123456:telegram-bot-token"
admin_ids = [123456789]
database_path = "data/bot.sqlite3"
database_url = "postgresql://vpn:secret@postgres:5432/vpn_bot"

[payment]
bank_name = "Demo Bank"
receiver_name = "Demo User"
card_number = "0000000000000000"
invoice_lifetime_hours = 12

{xui_block}
""",
        encoding="utf-8",
    )


def test_load_settings_legacy_xui_creates_main_node(tmp_path, monkeypatch) -> None:
    write_runtime_config(
        tmp_path,
        """
[xui]
node_code = "main"
base_url = "https://panel.example.com/secret-path"
username = "admin"
password = "super-secret"
inbound_id = 1
public_host = "vpn.example.com"
public_port = 443
""",
    )
    monkeypatch.setenv("VPN_BOT_HOME", str(tmp_path))
    monkeypatch.setenv("VPN_BOT_SECRETS_FILE", str(tmp_path / "runtime.toml"))

    settings = load_settings()

    assert settings.xui.node_code == "main"
    assert settings.app.database_url == "postgresql://vpn:secret@postgres:5432/vpn_bot"
    assert settings.xui.public_host == "vpn.example.com"
    assert [node.node_code for node in settings.all_xui_nodes] == ["main"]


def test_load_settings_reads_multiple_xui_nodes(tmp_path, monkeypatch) -> None:
    write_runtime_config(
        tmp_path,
        """
[xui]
default_node_code = "nl-2"

[[xui.nodes]]
code = "main"
name = "Main"
enabled = true
priority = 100
base_url = "https://main.example.com/secret-path"
username = "admin"
password = "super-secret"
inbound_id = 1
public_host = "vpn-main.example.com"
public_port = 443

[[xui.nodes]]
code = "nl-2"
name = "Netherlands 2"
enabled = false
priority = 200
base_url = "https://nl2.example.com/secret-path"
username = "admin"
password = "super-secret"
inbound_id = 2
public_host = "vpn-nl2.example.com"
public_port = 8443
verify_tls = false
""",
    )
    monkeypatch.setenv("VPN_BOT_HOME", str(tmp_path))
    monkeypatch.setenv("VPN_BOT_SECRETS_FILE", str(tmp_path / "runtime.toml"))

    settings = load_settings()

    assert settings.xui.node_code == "nl-2"
    assert settings.xui.public_port == 8443
    assert [(node.node_code, node.enabled, node.priority) for node in settings.all_xui_nodes] == [
        ("main", True, 100),
        ("nl-2", False, 200),
    ]


def test_load_settings_rejects_duplicate_xui_node_codes(tmp_path, monkeypatch) -> None:
    write_runtime_config(
        tmp_path,
        """
[xui]

[[xui.nodes]]
code = "main"
base_url = "https://main.example.com/secret-path"
username = "admin"
password = "super-secret"
public_host = "vpn-main.example.com"

[[xui.nodes]]
code = "main"
base_url = "https://other.example.com/secret-path"
username = "admin"
password = "super-secret"
public_host = "vpn-other.example.com"
""",
    )
    monkeypatch.setenv("VPN_BOT_HOME", str(tmp_path))
    monkeypatch.setenv("VPN_BOT_SECRETS_FILE", str(tmp_path / "runtime.toml"))

    with pytest.raises(ConfigError, match="Дублируются"):
        load_settings()
