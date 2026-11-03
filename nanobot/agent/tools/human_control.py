from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

HumanControlState = Literal["agent", "draining", "human"]


class HumanControlActiveError(RuntimeError):
    """Raised when an agent tool is attempted during human browser control."""


class HumanControlGate:
    """Stop agent tool execution and drain active operations before human control."""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._active_operations = 0
        self._state: HumanControlState = "agent"
        self._generation = 0

    @property
    def state(self) -> HumanControlState:
        return self._state

    @property
    def active_operations(self) -> int:
        return self._active_operations

    @asynccontextmanager
    async def agent_operation(self) -> AsyncIterator[None]:
        async with self._condition:
            if self._state != "agent":
                raise HumanControlActiveError("Agent browser control is paused")
            self._active_operations += 1
        try:
            yield
        finally:
            async with self._condition:
                self._active_operations -= 1
                self._condition.notify_all()

    async def enter_human_control(self) -> None:
        """Block new agent work, then report human control only after work drains."""
        async with self._condition:
            if self._state == "human":
                return
            if self._state == "agent":
                self._state = "draining"
                self._generation += 1
                self._condition.notify_all()
            generation = self._generation
            await self._condition.wait_for(
                lambda: self._active_operations == 0 or generation != self._generation
            )
            if generation != self._generation:
                return
            self._state = "human"
            self._condition.notify_all()

    async def resume_agent(self) -> None:
        """Explicitly reopen agent tool execution, including a cancelled handover."""
        async with self._condition:
            self._generation += 1
            self._state = "agent"
            self._condition.notify_all()
