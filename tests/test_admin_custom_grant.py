from decimal import Decimal
from pathlib import Path

from aiogram.types import User as TelegramUser

from vpn_bot.config import AppSettings, PaymentSettings, PlanDefinition, Settings, TrafficPolicySettings, XUISettings
from vpn_bot.database import build_session_factory, init_db
from vpn_bot.handlers.admin import custom_grant_action
from vpn_bot.keyboards import (
    AdminCustomGrantAction,
    admin_custom_grant_keyboard,
    admin_grant_plans_keyboard,
)
from vpn_bot.runtime import AppContext
from vpn_bot.services.custom_plans import (
    CUSTOM_PLAN_DEFAULT_DAYS,
    CUSTOM_PLAN_DEFAULT_DEVICES,
    CUSTOM_PLAN_KIND,
)
from vpn_bot.services.nodes import NodeRegistry


def make_settings(*, admin_ids: tuple[int, ...]) -> Settings:
    node = XUISettings(
        node_code="main",
        base_url="https://panel.example.com/secret",
        username="admin",
        password="secret",
        inbound_id=1,
        public_host="vpn.example.com",
        public_port=443,
    )
    return Settings(
        app=AppSettings(
            bot_token="token",
            admin_ids=admin_ids,
            database_path=Path("data/bot.sqlite3"),
        ),
        payment=PaymentSettings(
            bank_name="Demo Bank",
            receiver_name="Demo User",
            card_number="0000000000000000",
            phone=None,
            invoice_lifetime_hours=12,
        ),
        traffic_policy=TrafficPolicySettings(),
        xui=node,
        xui_nodes=(node,),
        secrets_file=Path("secrets/runtime.toml"),
        plans_file=Path("config/plans.toml"),
    )


class FakeBot:
    async def send_message(self, chat_id: int, text: str, reply_markup=None) -> None:
        return None


class FakeMessage:
    def __init__(self, user_id: int = 1) -> None:
        self.from_user = TelegramUser(id=user_id, is_bot=False, first_name="Admin", username="admin")
        self.edits = []

    async def edit_text(self, text: str, reply_markup=None) -> None:
        self.edits.append((text, reply_markup))


class FakeCallback:
    def __init__(self, *, user_id: int) -> None:
        self.from_user = TelegramUser(id=user_id, is_bot=False, first_name="Admin", username="admin")
        self.bot = FakeBot()
        self.message = FakeMessage(user_id=user_id)
        self.answers = []

    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))


def test_admin_grant_plans_keyboard_shows_custom_entries() -> None:
    plan = PlanDefinition(
        code="starter",
        title="30 дней / 310 ГБ",
        price_rub=Decimal("100.00"),
        duration_days=30,
        traffic_limit_gb=310,
    )

    markup = admin_grant_plans_keyboard(user_id=1, page=0, plans=[plan])
    button_texts = [button.text for row in markup.inline_keyboard for button in row]

    assert button_texts[:3] == ["Собрать Custom", "Собрать Custom Premium", "30 дней / 310 ГБ"]
    assert button_texts[-1] == "Назад"


def test_admin_custom_grant_keyboard_shows_grant_and_back() -> None:
    markup = admin_custom_grant_keyboard(CUSTOM_PLAN_KIND, 30, 2, user_id=1, page=0)
    button_texts = [button.text for row in markup.inline_keyboard for button in row]

    assert button_texts[-2:] == ["Выдать этот тариф", "Назад"]


async def test_admin_custom_grant_show_opens_builder(tmp_path) -> None:
    settings = make_settings(admin_ids=(1,))
    engine, session_factory = build_session_factory(tmp_path / "bot.sqlite3")
    await init_db(engine)
    nodes = NodeRegistry.from_settings(settings)
    context = AppContext(settings=settings, plans={}, engine=engine, session_factory=session_factory, nodes=nodes)
    callback = FakeCallback(user_id=1)

    try:
        async with session_factory() as session:
            from vpn_bot.models import User

            user = User(tg_id=5396411633, username="Lopylu", full_name="Lopylu")
            session.add(user)
            await session.commit()
            user_id = user.id

        await custom_grant_action(
            callback,
            AdminCustomGrantAction(
                user_id=user_id,
                page=0,
                kind=CUSTOM_PLAN_KIND,
                days=CUSTOM_PLAN_DEFAULT_DAYS,
                devices=CUSTOM_PLAN_DEFAULT_DEVICES,
                action="show",
            ),
            context,
        )
    finally:
        await nodes.close()
        await engine.dispose()

    assert callback.answers == [("", False)]
    assert callback.message.edits
    text, markup = callback.message.edits[0]
    assert "Выдача доступа для <code>5396411633</code> @Lopylu:" in text
    assert "Custom: 30 дней / 1 устройств / 540 ГБ" in text
    button_texts = [button.text for row in markup.inline_keyboard for button in row]
    assert button_texts[-2:] == ["Выдать этот тариф", "Назад"]
