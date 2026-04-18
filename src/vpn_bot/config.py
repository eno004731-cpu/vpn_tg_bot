from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib


BASE_DIR = Path(os.getenv("VPN_BOT_HOME", Path.cwd())).resolve()
DEFAULT_SECRETS_FILE = BASE_DIR / "secrets" / "runtime.toml"
DEFAULT_PLANS_FILE = BASE_DIR / "config" / "plans.toml"


class ConfigError(RuntimeError):
    """Raised when required configuration is missing."""


@dataclass
class AppSettings:
    bot_token: str
    admin_ids: Tuple[int, ...]
    database_path: Path
    database_url: Optional[str] = None
    sync_interval_seconds: int = 300
    worker_interval_seconds: int = 5
    web_host: str = "0.0.0.0"
    web_port: int = 8080
    worker_metrics_host: str = "0.0.0.0"
    worker_metrics_port: int = 9091
    webhook_path_secret: Optional[str] = None
    webhook_secret_token: Optional[str] = None
    public_webhook_base_url: str = "https://panel.swift-log.ru"
    field_encryption_key: Optional[str] = None


@dataclass
class PaymentSettings:
    bank_name: str
    receiver_name: str
    card_number: str
    phone: Optional[str]
    invoice_lifetime_hours: int
    instruction_hint: Optional[str] = None


@dataclass
class TrafficPolicySettings:
    enabled: bool = True
    daily_limit_gb: int = 75
    throttled_speed_kbytes_per_second: int = 1250
    timezone: str = "Europe/Moscow"

    @property
    def daily_limit_bytes(self) -> int:
        return self.daily_limit_gb * 1024 * 1024 * 1024


@dataclass
class XUISettings:
    base_url: str
    username: str
    password: str
    inbound_id: int
    public_host: str
    public_port: int
    verify_tls: bool = True
    fingerprint: str = "chrome"
    flow: str = "xtls-rprx-vision"
    spider_x: str = "/"
    node_code: str = "main"
    name: Optional[str] = None
    enabled: bool = True
    priority: int = 100

    @property
    def display_name(self) -> str:
        return self.name or self.node_code


@dataclass
class Settings:
    app: AppSettings
    payment: PaymentSettings
    traffic_policy: TrafficPolicySettings
    xui: XUISettings
    secrets_file: Path
    plans_file: Path
    xui_nodes: Tuple[XUISettings, ...] = ()

    @property
    def all_xui_nodes(self) -> Tuple[XUISettings, ...]:
        return self.xui_nodes or (self.xui,)


@dataclass(frozen=True)
class PlanDefinition:
    code: str
    title: str
    price_rub: Decimal
    duration_days: int
    traffic_limit_gb: int
    description: Optional[str] = None
    price_stars: Optional[int] = None
    provision_access: bool = True
    daily_limit_gb: Optional[int] = None
    device_limit: int = 2
    one_time_per_user: bool = False

    @property
    def traffic_limit_bytes(self) -> int:
        return self.traffic_limit_gb * 1024 * 1024 * 1024

    @property
    def daily_limit_bytes(self) -> Optional[int]:
        if self.daily_limit_gb is None:
            return None
        return self.daily_limit_gb * 1024 * 1024 * 1024

    @property
    def supports_transfer(self) -> bool:
        return self.provision_access and self.price_rub > Decimal("0.00")

    @property
    def supports_stars(self) -> bool:
        return self.price_stars is not None and self.price_stars > 0


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _coalesce(*values: Any, default: Any = None) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return default


def _env_int(name: str) -> Optional[int]:
    value = os.getenv(name)
    return int(value) if value else None


def _env_list_of_ints(name: str) -> Optional[Tuple[int, ...]]:
    raw = os.getenv(name)
    if not raw:
        return None
    return tuple(int(part.strip()) for part in raw.split(",") if part.strip())


def _env_bool(name: str) -> Optional[bool]:
    raw = os.getenv(name)
    if raw in (None, ""):
        return None
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise ConfigError(f"{name} должен быть true или false.")


def _load_legacy_xui(raw_xui: dict[str, Any]) -> XUISettings:
    xui_base_url = _coalesce(os.getenv("VPN_BOT_XUI_BASE_URL"), raw_xui.get("base_url"))
    xui_username = _coalesce(os.getenv("VPN_BOT_XUI_USERNAME"), raw_xui.get("username"))
    xui_password = _coalesce(os.getenv("VPN_BOT_XUI_PASSWORD"), raw_xui.get("password"))
    public_host = _coalesce(os.getenv("VPN_BOT_PUBLIC_HOST"), raw_xui.get("public_host"))

    required = {
        "xui.base_url": xui_base_url,
        "xui.username": xui_username,
        "xui.password": xui_password,
        "xui.public_host": public_host,
    }
    missing = [key for key, value in required.items() if value in (None, "")]
    if missing:
        raise ConfigError("Не заполнены обязательные секреты: " + ", ".join(sorted(missing)))

    return XUISettings(
        node_code=_coalesce(os.getenv("VPN_BOT_XUI_NODE_CODE"), raw_xui.get("node_code"), default="main"),
        name=_coalesce(os.getenv("VPN_BOT_XUI_NODE_NAME"), raw_xui.get("name")),
        enabled=bool(_coalesce(_env_bool("VPN_BOT_XUI_ENABLED"), raw_xui.get("enabled"), default=True)),
        priority=int(_coalesce(_env_int("VPN_BOT_XUI_PRIORITY"), raw_xui.get("priority"), default=100)),
        base_url=xui_base_url,
        username=xui_username,
        password=xui_password,
        inbound_id=int(_coalesce(_env_int("VPN_BOT_XUI_INBOUND_ID"), raw_xui.get("inbound_id"), default=1)),
        public_host=public_host,
        public_port=int(_coalesce(_env_int("VPN_BOT_PUBLIC_PORT"), raw_xui.get("public_port"), default=443)),
        verify_tls=bool(
            _coalesce(
                _env_bool("VPN_BOT_XUI_VERIFY_TLS"),
                raw_xui.get("verify_tls"),
                default=True,
            )
        ),
        fingerprint=_coalesce(os.getenv("VPN_BOT_FINGERPRINT"), raw_xui.get("fingerprint"), default="chrome"),
        flow=_coalesce(os.getenv("VPN_BOT_FLOW"), raw_xui.get("flow"), default="xtls-rprx-vision"),
        spider_x=_coalesce(os.getenv("VPN_BOT_SPIDER_X"), raw_xui.get("spider_x"), default="/"),
    )


def _load_xui_node(raw_node: dict[str, Any], *, index: int) -> XUISettings:
    node_code = _coalesce(raw_node.get("code"), raw_node.get("node_code"))
    if not node_code:
        raise ConfigError(f"Не заполнен xui.nodes[{index}].code")

    required = {
        f"xui.nodes[{node_code}].base_url": raw_node.get("base_url"),
        f"xui.nodes[{node_code}].username": raw_node.get("username"),
        f"xui.nodes[{node_code}].password": raw_node.get("password"),
        f"xui.nodes[{node_code}].public_host": raw_node.get("public_host"),
    }
    missing = [key for key, value in required.items() if value in (None, "")]
    if missing:
        raise ConfigError("Не заполнены обязательные секреты: " + ", ".join(sorted(missing)))

    return XUISettings(
        node_code=str(node_code),
        name=raw_node.get("name"),
        enabled=bool(raw_node.get("enabled", True)),
        priority=int(raw_node.get("priority", 100)),
        base_url=raw_node["base_url"],
        username=raw_node["username"],
        password=raw_node["password"],
        inbound_id=int(raw_node.get("inbound_id", 1)),
        public_host=raw_node["public_host"],
        public_port=int(raw_node.get("public_port", 443)),
        verify_tls=bool(raw_node.get("verify_tls", True)),
        fingerprint=raw_node.get("fingerprint", "chrome"),
        flow=raw_node.get("flow", "xtls-rprx-vision"),
        spider_x=raw_node.get("spider_x", "/"),
    )


def _load_xui_settings(raw_xui: dict[str, Any]) -> tuple[XUISettings, Tuple[XUISettings, ...]]:
    raw_nodes = raw_xui.get("nodes") or []
    if not raw_nodes:
        node = _load_legacy_xui(raw_xui)
        return node, (node,)

    nodes = tuple(_load_xui_node(raw_node, index=index) for index, raw_node in enumerate(raw_nodes))
    seen: set[str] = set()
    duplicates: set[str] = set()
    for node in nodes:
        if node.node_code in seen:
            duplicates.add(node.node_code)
        seen.add(node.node_code)
    if duplicates:
        raise ConfigError("Дублируются xui.nodes code: " + ", ".join(sorted(duplicates)))

    default_node_code = _coalesce(
        os.getenv("VPN_BOT_XUI_DEFAULT_NODE_CODE"),
        raw_xui.get("default_node_code"),
        default=nodes[0].node_code,
    )
    for node in nodes:
        if node.node_code == default_node_code:
            return node, nodes
    raise ConfigError(f"xui.default_node_code={default_node_code} не найден в xui.nodes")


def load_settings() -> Settings:
    secrets_file = Path(os.getenv("VPN_BOT_SECRETS_FILE", DEFAULT_SECRETS_FILE))
    plans_file = Path(os.getenv("VPN_BOT_PLANS_FILE", DEFAULT_PLANS_FILE))
    raw = _read_toml(secrets_file)
    app = raw.get("app", {})
    payment = raw.get("payment", {})
    traffic_policy = raw.get("traffic_policy", {})
    xui = raw.get("xui", {})

    bot_token = _coalesce(os.getenv("VPN_BOT_TOKEN"), app.get("bot_token"))
    if not bot_token:
        raise ConfigError(f"Не найден токен бота. Заполните {secrets_file} или VPN_BOT_TOKEN.")

    admin_ids = _env_list_of_ints("VPN_BOT_ADMIN_IDS") or tuple(int(item) for item in app.get("admin_ids", []))
    if not admin_ids:
        raise ConfigError(f"Не найдены admin_ids. Заполните {secrets_file} -> [app].admin_ids.")

    database_path_raw = _coalesce(os.getenv("VPN_BOT_DB_PATH"), app.get("database_path"), default="data/bot.sqlite3")
    database_path = Path(database_path_raw)
    if not database_path.is_absolute():
        database_path = BASE_DIR / database_path
    database_url = _coalesce(os.getenv("VPN_BOT_DATABASE_URL"), app.get("database_url"))

    xui_settings, xui_nodes = _load_xui_settings(xui)

    required = {
        "payment.bank_name": _coalesce(os.getenv("VPN_BOT_BANK_NAME"), payment.get("bank_name")),
        "payment.receiver_name": _coalesce(os.getenv("VPN_BOT_RECEIVER_NAME"), payment.get("receiver_name")),
        "payment.card_number": _coalesce(os.getenv("VPN_BOT_CARD_NUMBER"), payment.get("card_number")),
    }
    missing = [key for key, value in required.items() if value in (None, "")]
    if missing:
        raise ConfigError("Не заполнены обязательные секреты: " + ", ".join(sorted(missing)))

    traffic_policy_timezone = _coalesce(
        os.getenv("VPN_BOT_TRAFFIC_POLICY_TIMEZONE"),
        traffic_policy.get("timezone"),
        default="Europe/Moscow",
    )
    try:
        ZoneInfo(traffic_policy_timezone)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(f"Неизвестная timezone для traffic_policy: {traffic_policy_timezone}") from exc

    return Settings(
        app=AppSettings(
            bot_token=bot_token,
            admin_ids=admin_ids,
            database_path=database_path,
            database_url=database_url,
            sync_interval_seconds=_coalesce(
                _env_int("VPN_BOT_SYNC_INTERVAL"), app.get("sync_interval_seconds"), default=300
            ),
            worker_interval_seconds=_coalesce(
                _env_int("VPN_BOT_WORKER_INTERVAL_SECONDS"),
                app.get("worker_interval_seconds"),
                default=5,
            ),
            web_host=_coalesce(os.getenv("VPN_BOT_WEB_HOST"), app.get("web_host"), default="0.0.0.0"),
            web_port=int(_coalesce(_env_int("VPN_BOT_WEB_PORT"), app.get("web_port"), default=8080)),
            worker_metrics_host=_coalesce(
                os.getenv("VPN_BOT_WORKER_METRICS_HOST"),
                app.get("worker_metrics_host"),
                default="0.0.0.0",
            ),
            worker_metrics_port=int(
                _coalesce(
                    _env_int("VPN_BOT_WORKER_METRICS_PORT"),
                    app.get("worker_metrics_port"),
                    default=9091,
                )
            ),
            webhook_path_secret=_coalesce(
                os.getenv("VPN_BOT_WEBHOOK_PATH_SECRET"),
                app.get("webhook_path_secret"),
            ),
            webhook_secret_token=_coalesce(
                os.getenv("VPN_BOT_WEBHOOK_SECRET_TOKEN"),
                app.get("webhook_secret_token"),
            ),
            public_webhook_base_url=_coalesce(
                os.getenv("VPN_BOT_PUBLIC_WEBHOOK_BASE_URL"),
                app.get("public_webhook_base_url"),
                default="https://panel.swift-log.ru",
            ),
            field_encryption_key=_coalesce(
                os.getenv("VPN_BOT_FIELD_ENCRYPTION_KEY"),
                app.get("field_encryption_key"),
            ),
        ),
        payment=PaymentSettings(
            bank_name=_coalesce(os.getenv("VPN_BOT_BANK_NAME"), payment.get("bank_name")),
            receiver_name=_coalesce(os.getenv("VPN_BOT_RECEIVER_NAME"), payment.get("receiver_name")),
            card_number=_coalesce(os.getenv("VPN_BOT_CARD_NUMBER"), payment.get("card_number")),
            phone=_coalesce(os.getenv("VPN_BOT_PHONE"), payment.get("phone")),
            invoice_lifetime_hours=_coalesce(
                _env_int("VPN_BOT_INVOICE_LIFETIME_HOURS"),
                payment.get("invoice_lifetime_hours"),
                default=6,
            ),
            instruction_hint=_coalesce(os.getenv("VPN_BOT_INSTRUCTION_HINT"), payment.get("instruction_hint")),
        ),
        traffic_policy=TrafficPolicySettings(
            enabled=bool(
                _coalesce(
                    _env_bool("VPN_BOT_TRAFFIC_POLICY_ENABLED"),
                    traffic_policy.get("enabled"),
                    default=True,
                )
            ),
            daily_limit_gb=int(
                _coalesce(
                    _env_int("VPN_BOT_DAILY_LIMIT_GB"),
                    traffic_policy.get("daily_limit_gb"),
                    default=75,
                )
            ),
            throttled_speed_kbytes_per_second=int(
                _coalesce(
                    _env_int("VPN_BOT_THROTTLED_SPEED_KB_PER_SEC"),
                    traffic_policy.get("throttled_speed_kbytes_per_second"),
                    default=1250,
                )
            ),
            timezone=traffic_policy_timezone,
        ),
        xui=xui_settings,
        secrets_file=secrets_file,
        plans_file=plans_file,
        xui_nodes=xui_nodes,
    )


def load_plans(path: Optional[Path] = None) -> dict[str, PlanDefinition]:
    plans_path = path or DEFAULT_PLANS_FILE
    raw = _read_toml(plans_path)
    items = raw.get("plans", [])
    plans: dict[str, PlanDefinition] = {}
    for item in items:
        plan = PlanDefinition(
            code=item["code"],
            title=item["title"],
            price_rub=Decimal(str(item["price_rub"])).quantize(Decimal("0.01")),
            duration_days=int(item["duration_days"]),
            traffic_limit_gb=int(item["traffic_limit_gb"]),
            description=item.get("description"),
            price_stars=int(item["price_stars"]) if item.get("price_stars") is not None else None,
            provision_access=bool(item.get("provision_access", True)),
            daily_limit_gb=int(item["daily_limit_gb"]) if item.get("daily_limit_gb") is not None else None,
            device_limit=max(1, int(item.get("device_limit", 2))),
            one_time_per_user=bool(item.get("one_time_per_user", False)),
        )
        plans[plan.code] = plan
    if not plans:
        raise ConfigError(f"Не удалось загрузить тарифы из {plans_path}")
    return plans
