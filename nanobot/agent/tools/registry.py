from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any, cast

from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.context import ContextAware, current_request_context

if TYPE_CHECKING:
    from nanobot.runtime_context import RuntimeContextProvider

_ERROR_HINT = (
    "\n\n[Consider this result in the context of the user's goal "
    "and decide whether and how to proceed.]"
)


def with_tool_error_hint(payload: str) -> str:
    """Append tool error guidance exactly once."""
    if payload.endswith(_ERROR_HINT):
        return payload
    return payload + _ERROR_HINT


def is_tool_error_result(result: Any) -> bool:
    return isinstance(result, ToolResult) and result.is_error


class ToolRegistry:
    """
    Registry for agent tools.

    Allows dynamic registration and execution of tools.
    """

    def __init__(self):
        self._tools: dict[str, Tool] = {}
        self._cached_definitions: list[dict[str, Any]] | None = None
        # Deferred tool name -> idle TTL in seconds (0 = never expires).
        self._deferred: dict[str, float] = {}
        # Session key -> {deferred tool name: last use (monotonic)}.
        self._activated: dict[str, dict[str, float]] = {}

    def register(self, tool: Tool) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool
        self._cached_definitions = None

    def unregister(self, name: str) -> None:
        """Unregister a tool by name."""
        self._tools.pop(name, None)
        self._deferred.pop(name, None)
        self._cached_definitions = None

    def defer(self, name: str, ttl_seconds: float = 0) -> None:
        """Hide a registered tool's schema until a session activates it (still callable)."""
        if name in self._tools:
            self._deferred[name] = ttl_seconds
            self._cached_definitions = None

    @staticmethod
    def _session_key() -> str:
        ctx = current_request_context()
        return (ctx.session_key if ctx is not None else None) or ""

    def activate(self, names: list[str]) -> None:
        """Expose deferred schemas to the current session until idle past their TTL."""
        now = time.monotonic()
        active = self._activated.setdefault(self._session_key(), {})
        for name in names:
            if name in self._deferred:
                active[name] = now

    def _active_names(self) -> set[str]:
        """Names activated for the current session; drops expired entries."""
        now = time.monotonic()
        for key in list(self._activated):
            active = self._activated[key]
            for name in list(active):
                ttl = self._deferred.get(name)
                if ttl is None or (ttl and now - active[name] > ttl):
                    del active[name]
            if not active:
                del self._activated[key]
        return set(self._activated.get(self._session_key(), ()))

    def deferred_tools(self) -> list[Tool]:
        """Deferred tools whose schemas the current session has not activated."""
        active = self._active_names()
        return [
            self._tools[n]
            for n in sorted(self._deferred)
            if n in self._tools and n not in active
        ]

    def get(self, name: str) -> Tool | None:
        """Get a tool by name."""
        return self._tools.get(name)

    def get_runtime_context_providers(self) -> list[RuntimeContextProvider]:
        """Return tool-owned providers in stable tool-name order."""
        providers: list[RuntimeContextProvider] = []
        for name in sorted(self._tools):
            provider = self._tools[name].runtime_context_provider()
            if provider is not None:
                providers.append(provider)
        return providers

    @staticmethod
    def _lookup_key(name: str) -> str:
        """Normalize names for suggestions only; never for execution."""
        return "".join(ch.lower() for ch in name if ch.isalnum())

    def _suggest_name(self, name: str) -> str | None:
        key = self._lookup_key(str(name or ""))
        if not key:
            return None
        matches = [
            registered
            for registered in self._tools
            if self._lookup_key(registered) == key
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    def has(self, name: str) -> bool:
        """Check if a tool is registered."""
        return self.get(name) is not None

    @staticmethod
    def _schema_name(schema: dict[str, Any]) -> str:
        """Extract a normalized tool name from either OpenAI or flat schemas."""
        fn = schema.get("function")
        if isinstance(fn, dict):
            name = cast(dict[str, Any], fn).get("name")
            if isinstance(name, str):
                return name
        name = schema.get("name")
        return name if isinstance(name, str) else ""

    def get_definitions(self) -> list[dict[str, Any]]:
        """Get tool definitions with stable ordering for cache-friendly prompts.

        Built-in tools are sorted first as a stable prefix, then MCP tools are
        sorted and appended. Deferred tools appear only while activated for the
        current session. The deferred-free list is cached until the next
        register/unregister call.
        """
        if not self._deferred:
            return self._sorted_definitions(set())
        return self._sorted_definitions(self._active_names())

    def _sorted_definitions(self, active: set[str]) -> list[dict[str, Any]]:
        if not active and self._cached_definitions is not None:
            return self._cached_definitions
        definitions = [
            tool.to_schema()
            for name, tool in self._tools.items()
            if name not in self._deferred or name in active
        ]
        builtins: list[dict[str, Any]] = []
        mcp_tools: list[dict[str, Any]] = []
        for schema in definitions:
            if self._schema_name(schema).startswith("mcp_"):
                mcp_tools.append(schema)
            else:
                builtins.append(schema)
        builtins.sort(key=self._schema_name)
        mcp_tools.sort(key=self._schema_name)
        result = builtins + mcp_tools
        if not active:
            self._cached_definitions = result
        return result

    def prepare_call(
        self,
        name: str,
        params: Any,
    ) -> tuple[Tool | None, Any, str | None]:
        """Resolve, cast, and validate one tool call."""
        tool = self.get(name)
        if not tool:
            suggestion = self._suggest_name(str(name))
            hint = f" Did you mean '{suggestion}'? Tool names must match exactly." if suggestion else ""
            return None, params, (
                ToolResult.error(
                    f"Error: Tool '{name}' not found.{hint} Available: {', '.join(self.tool_names)}"
                )
            )
        # Compatibility for external tools that still implement the legacy
        # setter protocol. Built-ins read the authoritative ContextVar
        # directly and never copy routing state.
        if isinstance(tool, ContextAware) and (ctx := current_request_context()) is not None:
            tool.set_context(ctx)

        params = self._coerce_params(tool, params)
        if not isinstance(params, dict):
            return tool, params, (
                ToolResult.error(
                    f"Error: Tool '{name}' parameters must be a JSON object, got "
                    f"{type(params).__name__}. Use named parameters like "
                    'tool_name(param1="value1", param2="value2") matching the tool schema.'
                )
            )

        cast_params = tool.cast_params(cast(dict[str, Any], params))
        errors = tool.validate_params(cast_params)
        if errors:
            return tool, cast_params, (
                ToolResult.error(f"Error: Invalid parameters for tool '{name}': " + "; ".join(errors))
            )
        return tool, cast_params, None

    @classmethod
    def _coerce_argument_value(cls, value: Any) -> Any:
        if value is None:
            return {}
        if not isinstance(value, str):
            return value

        stripped = value.strip()
        if not stripped:
            return {}

        if not stripped.startswith(("{", "[")):
            return value

        try:
            parsed = json.loads(stripped)
        except Exception:
            return value

        return parsed

    @classmethod
    def _coerce_params(cls, tool: Tool, params: Any) -> Any:
        params = cls._coerce_argument_value(params)
        return cls._unwrap_arguments_payload(tool, params)

    @classmethod
    def _unwrap_arguments_payload(cls, tool: Tool, params: Any) -> Any:
        if not isinstance(params, dict):
            return params
        arguments_payload = cast(dict[str, Any], params)
        if set(arguments_payload) != {"arguments"}:
            return arguments_payload
        properties = (tool.parameters or {}).get("properties", {})
        if isinstance(properties, dict) and "arguments" in properties:
            return arguments_payload
        return cls._coerce_argument_value(arguments_payload.get("arguments"))

    async def execute(self, name: str, params: Any) -> Any:
        """Execute a tool by name with given parameters."""
        tool, params, error = self.prepare_call(name, params)
        if error:
            return ToolResult.error(with_tool_error_hint(str(error)))

        if name in self._deferred:
            self.activate([name])
        try:
            assert tool is not None  # guarded by prepare_call()
            result = await tool.execute(**params)
            if is_tool_error_result(result):
                return ToolResult.error(with_tool_error_hint(str(result)))
            return result
        except Exception as e:
            return ToolResult.error(with_tool_error_hint(f"Error executing {name}: {str(e)}"))

    @property
    def tool_names(self) -> list[str]:
        """Get list of registered tool names."""
        return list(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return self.has(name)
