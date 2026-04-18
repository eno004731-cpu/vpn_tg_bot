from __future__ import annotations

from vpn_bot.app import disable_telegram_webhook_for_polling


class FakePollingBot:
    def __init__(self) -> None:
        self.calls = []

    async def delete_webhook(self, **kwargs) -> None:
        self.calls.append(kwargs)


async def test_disable_telegram_webhook_for_polling_keeps_updates() -> None:
    bot = FakePollingBot()

    await disable_telegram_webhook_for_polling(bot)  # type: ignore[arg-type]

    assert bot.calls == [{"drop_pending_updates": False}]
