"""ToolGateway: the only way an agent can reach GitHub.

Agents never import a GitHub or MCP client. They get a gateway, and every call
produces a `tool.call` event followed by a `decision` event.
"""

from __future__ import annotations

from typing import Any, Protocol

from backend.events import bus, redact
from backend.models import (
    Denial,
    Enforcer,
    Event,
    TaskContext,
    ToolResult,
    UpstreamError,
)


class ToolGateway(Protocol):
    enforcer: Enforcer
    upstream_name: str

    async def call(self, agent_id: str, ctx: TaskContext, tool: str, args: dict[str, Any]) -> ToolResult | Denial: ...


class BaseGateway:
    enforcer: Enforcer = Enforcer.NONE
    upstream_name: str = ""

    async def _decide(self, agent_id: str, ctx: TaskContext, tool: str, args: dict[str, Any]) -> ToolResult | Denial:
        raise NotImplementedError

    async def call(self, agent_id: str, ctx: TaskContext, tool: str, args: dict[str, Any]) -> ToolResult | Denial:
        common = dict(review_id=ctx.review_id, mode=ctx.mode, source=agent_id, tool=tool, args_redacted=redact(args))
        bus.emit(Event(type="tool.call", **common))
        try:
            result = await self._decide(agent_id, ctx, tool, args)
        except UpstreamError as exc:
            # The call was not refused by access control; the tool server failed it.
            bus.emit(Event(type="error", reason=f"upstream error: {exc}", payload={"upstream": self.upstream_name}, **common))
            raise
        if isinstance(result, Denial):
            bus.emit(
                Event(type="decision", decision="DENY", reason=result.reason, enforcer=result.enforcer,
                      payload={"policy": result.policy, "upstream": self.upstream_name}, **common)
            )
        else:
            bus.emit(
                Event(type="decision", decision="ALLOW", enforcer=self.enforcer,
                      reason="no access control" if self.enforcer is Enforcer.NONE else "allowed by policy",
                      payload={"upstream": self.upstream_name}, **common)
            )
        return result
