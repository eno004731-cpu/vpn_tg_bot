from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vpn_bot.config import Settings, XUISettings
from vpn_bot.models import Subscription, SubscriptionStatus
from vpn_bot.services.xui import XUIClient


class NodeRegistryError(RuntimeError):
    """Raised when a VPN node cannot be selected or used."""


@dataclass(frozen=True)
class NodeStatus:
    node: XUISettings
    active_subscriptions: int
    api_ok: bool
    error: str | None = None


class NodeRegistry:
    def __init__(self, nodes: Iterable[XUISettings]) -> None:
        self._nodes = {node.node_code: node for node in nodes}
        self._clients: dict[str, XUIClient] = {}
        if not self._nodes:
            raise NodeRegistryError("Не настроены VPN-ноды.")

    @classmethod
    def from_settings(cls, settings: Settings) -> "NodeRegistry":
        return cls(settings.all_xui_nodes)

    @property
    def nodes(self) -> tuple[XUISettings, ...]:
        return tuple(self._nodes.values())

    @property
    def enabled_nodes(self) -> tuple[XUISettings, ...]:
        return tuple(node for node in self.nodes if node.enabled)

    def get_settings(self, node_code: str) -> XUISettings:
        try:
            return self._nodes[node_code]
        except KeyError as exc:
            raise NodeRegistryError(f"VPN-нода {node_code!r} не настроена.") from exc

    def get_client(self, node_code: str) -> XUIClient:
        node = self.get_settings(node_code)
        if node.node_code not in self._clients:
            self._clients[node.node_code] = XUIClient(node)
        return self._clients[node.node_code]

    async def close(self) -> None:
        for client in self._clients.values():
            await client.close()

    async def select_node_for_new_subscription(self, session: AsyncSession) -> XUISettings:
        enabled_nodes = self.enabled_nodes
        if not enabled_nodes:
            raise NodeRegistryError("Нет включённых VPN-нод для выдачи доступа.")

        active_counts = await count_active_subscriptions_by_node(session)
        return sorted(
            enabled_nodes,
            key=lambda node: (
                active_counts.get(node.node_code, 0),
                -node.priority,
                node.node_code,
            ),
        )[0]

    async def collect_statuses(self, session: AsyncSession) -> list[NodeStatus]:
        active_counts = await count_active_subscriptions_by_node(session)
        statuses: list[NodeStatus] = []
        for node in sorted(self.nodes, key=lambda item: item.node_code):
            try:
                await self.get_client(node.node_code).list_inbounds()
            except Exception as exc:  # noqa: BLE001
                logging.exception("Failed to check VPN node %s", node.node_code)
                statuses.append(
                    NodeStatus(
                        node=node,
                        active_subscriptions=active_counts.get(node.node_code, 0),
                        api_ok=False,
                        error=str(exc),
                    )
                )
                continue
            statuses.append(
                NodeStatus(
                    node=node,
                    active_subscriptions=active_counts.get(node.node_code, 0),
                    api_ok=True,
                )
            )
        return statuses


async def count_active_subscriptions_by_node(session: AsyncSession) -> dict[str, int]:
    rows = await session.execute(
        select(Subscription.node_code, func.count())
        .where(Subscription.status == SubscriptionStatus.active.value)
        .group_by(Subscription.node_code)
    )
    return {str(node_code): int(count) for node_code, count in rows.all()}
