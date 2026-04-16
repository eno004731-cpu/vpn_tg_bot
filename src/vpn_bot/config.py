from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional, Tuple

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
    sync_interval_seconds: int = 300


@dataclass
class PaymentSettings:
    bank_name: str
    receiver_name: str
    card_number: str
    phone: Optional[str]
    invoice_lifetime_hours: int
    instruction_hint: Optional[str] = None


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


@dataclass
class Settings:
    app: AppSettings
    payment: PaymentSettings
    xui: XUISettings
    secrets_file: Path
    plans_file: Path


@dataclass(frozen=True)
class PlanDefinition:
    code: str
    title: str
    price_rub: Decimal
    duration_days: int
    traffic_limit_gb: int
    description: Optional[str] = None

    @property
    def traffic_limit_bytes(self) -> int:
        return self.traffic_limit_gb * 1024 * 1024 * 1024


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


def load_settings() -> Settings:
    secrets_file = Path(os.getenv("VPN_BOT_SECRETS_FILE", DEFAULT_SECRETS_FILE))
    plans_file = Path(os.getenv("VPN_BOT_PLANS_FILE", DEFAULT_PLANS_FILE))
    raw = _read_toml(secrets_file)
    app = raw.get("app", {})
    payment = raw.get("payment", {})
    xui = raw.get("xui", {})

    bot_token = _coalesce(os.getenv("VPN_BOT_TOKEN"), app.get("bot_token"))
    if not bot_token:
        raise ConfigError(
            f"Не найден токен бота. Заполните {secrets_file} или VPN_BOT_TOKEN."
        )

    admin_ids = _env_list_of_ints("VPN_BOT_ADMIN_IDS") or tuple(
        int(item) for item in app.get("admin_ids", [])
    )
    if not admin_ids:
        raise ConfigError(
            f"Не найдены admin_ids. Заполните {secrets_file} -> [app].admin_ids."
        )

    database_path_raw = _coalesce(
        os.getenv("VPN_BOT_DB_PATH"), app.get("database_path"), default="data/bot.sqlite3"
    )
    database_path = Path(database_path_raw)
    if not database_path.is_absolute():
        database_path = BASE_DIR / database_path

    xui_base_url = _coalesce(os.getenv("VPN_BOT_XUI_BASE_URL"), xui.get("base_url"))
    xui_username = _coalesce(os.getenv("VPN_BOT_XUI_USERNAME"), xui.get("username"))
    xui_password = _coalesce(os.getenv("VPN_BOT_XUI_PASSWORD"), xui.get("password"))
    public_host = _coalesce(os.getenv("VPN_BOT_PUBLIC_HOST"), xui.get("public_host"))

    required = {
        "xui.base_url": xui_base_url,
        "xui.username": xui_username,
        "xui.password": xui_password,
        "xui.public_host": public_host,
        "payment.bank_name": _coalesce(os.getenv("VPN_BOT_BANK_NAME"), payment.get("bank_name")),
        "payment.receiver_name": _coalesce(
            os.getenv("VPN_BOT_RECEIVER_NAME"), payment.get("receiver_name")
        ),
        "payment.card_number": _coalesce(
            os.getenv("VPN_BOT_CARD_NUMBER"), payment.get("card_number")
        ),
    }
    missing = [key for key, value in required.items() if value in (None, "")]
    if missing:
        raise ConfigError(
            "Не заполнены обязательные секреты: " + ", ".join(sorted(missing))
        )

    return Settings(
        app=AppSettings(
            bot_token=bot_token,
            admin_ids=admin_ids,
            database_path=database_path,
            sync_interval_seconds=_coalesce(
                _env_int("VPN_BOT_SYNC_INTERVAL"), app.get("sync_interval_seconds"), default=300
            ),
        ),
        payment=PaymentSettings(
            bank_name=_coalesce(os.getenv("VPN_BOT_BANK_NAME"), payment.get("bank_name")),
            receiver_name=_coalesce(
                os.getenv("VPN_BOT_RECEIVER_NAME"), payment.get("receiver_name")
            ),
            card_number=_coalesce(
                os.getenv("VPN_BOT_CARD_NUMBER"), payment.get("card_number")
            ),
            phone=_coalesce(os.getenv("VPN_BOT_PHONE"), payment.get("phone")),
            invoice_lifetime_hours=_coalesce(
                _env_int("VPN_BOT_INVOICE_LIFETIME_HOURS"),
                payment.get("invoice_lifetime_hours"),
                default=12,
            ),
            instruction_hint=_coalesce(
                os.getenv("VPN_BOT_INSTRUCTION_HINT"), payment.get("instruction_hint")
            ),
        ),
        xui=XUISettings(
            base_url=xui_base_url,
            username=xui_username,
            password=xui_password,
            inbound_id=int(
                _coalesce(_env_int("VPN_BOT_XUI_INBOUND_ID"), xui.get("inbound_id"), default=1)
            ),
            public_host=public_host,
            public_port=int(
                _coalesce(_env_int("VPN_BOT_PUBLIC_PORT"), xui.get("public_port"), default=443)
            ),
            verify_tls=bool(
                _coalesce(
                    _env_bool("VPN_BOT_XUI_VERIFY_TLS"),
                    xui.get("verify_tls"),
                    default=True,
                )
            ),
            fingerprint=_coalesce(
                os.getenv("VPN_BOT_FINGERPRINT"), xui.get("fingerprint"), default="chrome"
            ),
            flow=_coalesce(os.getenv("VPN_BOT_FLOW"), xui.get("flow"), default="xtls-rprx-vision"),
            spider_x=_coalesce(os.getenv("VPN_BOT_SPIDER_X"), xui.get("spider_x"), default="/"),
        ),
        secrets_file=secrets_file,
        plans_file=plans_file,
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
        )
        plans[plan.code] = plan
    if not plans:
        raise ConfigError(f"Не удалось загрузить тарифы из {plans_path}")
    return plans
