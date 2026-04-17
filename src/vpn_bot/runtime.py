from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from vpn_bot.config import PlanDefinition, Settings
from vpn_bot.services.nodes import NodeRegistry


@dataclass
class AppContext:
    settings: Settings
    plans: dict[str, PlanDefinition]
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    nodes: NodeRegistry
