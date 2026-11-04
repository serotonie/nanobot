from __future__ import annotations

import asyncio

import pytest

from nanobot.agent.hook import AgentHook, AgentHookContext
from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.execution import execute_tool_calls
from nanobot.agent.tools.human_control import HumanControlActiveError, HumanControlGate
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.providers.base import ToolCallRequest


class _BrowserTool(Tool):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.finish = asyncio.Event()
        self.calls = 0

    @property
    def name(self) -> str:
        return "mcp_browser_click"

    @property
    def description(self) -> str:
        return "Click in the shared browser"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs):
        self.calls += 1
        self.started.set()
        await self.finish.wait()
        return "clicked"


async def test_human_control_drains_active_agent_work_before_activation() -> None:
    gate = HumanControlGate()
    registry = ToolRegistry(human_control=gate)
    tool = _BrowserTool()
    registry.register(tool)

    operation = asyncio.create_task(registry.execute_prepared(tool, {}))
    await tool.started.wait()
    handover = asyncio.create_task(gate.enter_human_control())
    await asyncio.sleep(0)

    assert gate.state == "draining"
    assert gate.active_operations == 1

    tool.finish.set()
    await operation
    await handover

    assert gate.state == "human"
    assert gate.active_operations == 0


async def test_agent_dispatch_stays_blocked_until_explicit_resume() -> None:
    gate = HumanControlGate()
    registry = ToolRegistry(human_control=gate)
    tool = _BrowserTool()
    registry.register(tool)
    await gate.enter_human_control()

    call = ToolCallRequest(id="browser-call", name=tool.name, arguments={})
    results, events = await execute_tool_calls(
        registry,
        [call],
        concurrent=True,
        external_lookup_counts={},
        workspace_violation_counts={},
        hook=AgentHook(),
        context=AgentHookContext(iteration=0, messages=[]),
    )

    assert tool.calls == 0
    assert len(results) == 1
    assert "paused" in str(results[0]).lower()
    assert events[0]["status"] == "error"

    await gate.resume_agent()
    tool.finish.set()
    results, events = await execute_tool_calls(
        registry,
        [call],
        concurrent=True,
        external_lookup_counts={},
        workspace_violation_counts={},
        hook=AgentHook(),
        context=AgentHookContext(iteration=0, messages=[]),
    )

    assert results == ["clicked"]
    assert events[0]["status"] == "ok"


async def test_cancelled_handover_stays_locked_until_retried_or_resumed() -> None:
    gate = HumanControlGate()
    registry = ToolRegistry(human_control=gate)
    tool = _BrowserTool()
    operation = asyncio.create_task(registry.execute_prepared(tool, {}))
    await tool.started.wait()

    handover = asyncio.create_task(gate.enter_human_control())
    await asyncio.sleep(0)
    handover.cancel()
    with pytest.raises(asyncio.CancelledError):
        await handover

    assert gate.state == "draining"
    with pytest.raises(HumanControlActiveError):
        await registry.execute_prepared(tool, {})

    tool.finish.set()
    await operation
    await gate.enter_human_control()
    assert gate.state == "human"
    await gate.resume_agent()
    assert gate.state == "agent"
