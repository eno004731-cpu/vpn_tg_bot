from __future__ import annotations

from aiogram.types import User as TelegramUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vpn_bot.models import User


async def ensure_user(
    session: AsyncSession, telegram_user: TelegramUser, admin_ids: tuple[int, ...]
) -> User:
    user = await session.scalar(select(User).where(User.tg_id == telegram_user.id))
    full_name = " ".join(
        part for part in [telegram_user.first_name, telegram_user.last_name] if part
    ).strip() or None
    is_admin = telegram_user.id in admin_ids

    if user is None:
        user = User(
            tg_id=telegram_user.id,
            username=telegram_user.username,
            full_name=full_name,
            is_admin=is_admin,
        )
        session.add(user)
        await session.flush()
        return user

    user.username = telegram_user.username
    user.full_name = full_name
    user.is_admin = is_admin
    await session.flush()
    return user

