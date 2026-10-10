"""UNSAFE path: "how many bots work". Broad token, every tool, no checks."""

from __future__ import annotations

from typing import Any

from backend.gateway.base import BaseGateway
from backend.gateway.upstream import Upstream
from backend.models import Denial, Enforcer, TaskContext, ToolResult


class DirectGateway(BaseGateway):
    enforcer = Enforcer.NONE

    def __init__(self, upstream: Upstream) -> None:
        self.upstream = upstream
        self.upstream_name = upstream.name

    async def _decide(self, agent_id: str, ctx: TaskContext, tool: str, args: dict[str, Any]) -> ToolResult | Denial:
        return ToolResult(tool=tool, data=await self.upstream.call(tool, args))
