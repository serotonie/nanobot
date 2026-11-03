# pyright: reportIncompatibleMethodOverride=false

from collections.abc import Awaitable, Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any, cast

from loguru import logger

from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.context import ToolContext, current_request_context
from nanobot.agent.tools.path_utils import resolve_workspace_path
from nanobot.agent.tools.schema import (
    ArraySchema,
    BooleanSchema,
    IntegerSchema,
    ObjectSchema,
    StringSchema,
    tool_parameters_schema,
)
from nanobot.bus.delivery import DeliveryRequest
from nanobot.bus.events import OutboundMessage
from nanobot.config.paths import get_workspace_path
from nanobot.security.workspace_access import current_tool_workspace

_CURRENT_MESSAGE_SENDS: ContextVar[set[tuple[str, str]] | None] = ContextVar(
    "message_sends",
    default=None,
)

_REACTION_SCHEMA = ObjectSchema(
    emoji=StringSchema("Reaction emoji (one Unicode grapheme).", min_length=1),
    target_author=StringSchema(
        "Signal number or UUID of the original message author.", min_length=1,
    ),
    target_timestamp=IntegerSchema(
        description="Original Signal message timestamp in milliseconds.",
        minimum=1,
        maximum=2**63 - 1,
    ),
    remove=BooleanSchema(description="Remove this reaction instead of adding it.", default=False),
    required=["emoji"],
    additional_properties=False,
    description=(
        "Signal only: add or remove a reaction. Send content='' and no media/buttons. "
        "Omit both target_author and target_timestamp to react to the current incoming message "
        "(or the original message referenced by an incoming reaction). "
        "For another message or chat, supply both target fields."
    ),
)

_QUOTE_SCHEMA = ObjectSchema(
    target_author=StringSchema("Signal phone number or UUID of the message author.", min_length=1),
    target_timestamp=IntegerSchema(
        description="Original Signal message timestamp in milliseconds.",
        minimum=1,
        maximum=2**63 - 1,
    ),
    text=StringSchema("Original message text to display in the quote."),
    additional_properties=False,
    description=(
        "Signal only: send a native quoted reply with text or media. "
        "Use {} to reply to the current incoming message. "
        "To cite another message, supply both target_author and target_timestamp, "
        "and text when known. Never invent message identifiers."
    ),
)


@contextmanager
def capture_message_deliveries() -> Generator[set[tuple[str, str]], None, None]:
    """Record successful MessageTool targets within one agent run."""
    sends: set[tuple[str, str]] = set()
    token = _CURRENT_MESSAGE_SENDS.set(sends)
    try:
        yield sends
    finally:
        _CURRENT_MESSAGE_SENDS.reset(token)


@tool_parameters(
    tool_parameters_schema(
        content=StringSchema(
            "Message content."
        ),
        channel=StringSchema(
            "Target channel; defaults to the current channel."
        ),
        chat_id=StringSchema(
            "Target chat/user ID; defaults to the current chat. "
            "On WebSocket/WebUI turns: omit chat_id to use the server's conversation id "
            "(never pass client_id values like anon-…)."
        ),
        media=ArraySchema(
            StringSchema(""),
            description=(
                "Optional list of existing file paths to attach. "
                "Use artifact paths returned by generate_image here when delivering generated images."
            ),
        ),
        buttons=ArraySchema(
            ArraySchema(StringSchema("Button label")),
            description="Optional: inline keyboard buttons as list of rows, each row is list of button labels.",
        ),
        reaction=_REACTION_SCHEMA,
        quote=_QUOTE_SCHEMA,
        required=["content"],
    )
)
class MessageTool(Tool):
    """Tool to send messages to users on chat channels."""

    def __init__(
        self,
        send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
        default_channel: str = "",
        default_chat_id: str = "",
        default_message_id: str | None = None,
        workspace: str | Path | None = None,
        restrict_to_workspace: bool = False,
    ):
        self._send_callback = send_callback
        self._workspace = (
            Path(workspace).expanduser() if workspace is not None else get_workspace_path()
        )
        self._restrict_to_workspace = restrict_to_workspace
        self._fallback_channel = default_channel
        self._fallback_chat_id = default_chat_id
        self._fallback_message_id = default_message_id
        self._fallback_metadata: dict[str, Any] = {}
        self._suppress_delivery_var: ContextVar[bool] = ContextVar(
            "message_suppress_delivery",
            default=False,
        )

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        send_callback = ctx.bus.publish_outbound if ctx.bus else None
        return cls(
            send_callback=send_callback,
            workspace=ctx.workspace,
            restrict_to_workspace=ctx.config.restrict_to_workspace,
        )

    def set_send_callback(self, callback: Callable[[OutboundMessage], Awaitable[None]]) -> None:
        """Set the callback for sending messages."""
        self._send_callback = callback

    def set_suppress_delivery(self, active: bool) -> Token[bool]:
        """Acknowledge but don't deliver tool sends (heartbeat internal check)."""
        return self._suppress_delivery_var.set(active)

    def reset_suppress_delivery(self, token: Token[bool]) -> None:
        """Restore previous delivery-suppression state."""
        self._suppress_delivery_var.reset(token)

    @property
    def name(self) -> str:
        return "message"

    @property
    def description(self) -> str:
        return (
            "Send separate messages requested by the user or task, optionally with attachments. "
            "For normal replies, including scheduled reminders and reports, answer directly; "
            "the final reply is delivered to the current chat automatically. "
            "Use this tool to deliver generated images via media."
            " On Signal, use reaction to add or remove an emoji reaction instead of sending text."
            " Use quote={} to reply with a native Signal citation to the current message, "
            "or specify a quote target to cite another message."
        )

    def _resolve_media(self, media: list[str]) -> list[str]:
        resolved: list[str] = []
        access = current_tool_workspace(
            self._workspace,
            restrict_to_workspace=self._restrict_to_workspace,
        )
        workspace = access.project_path or self._workspace
        for p in media:
            if p.startswith(("http://", "https://")):
                resolved.append(p)
            elif not access.restrict_to_workspace:
                path = Path(p).expanduser()
                resolved.append(p if path.is_absolute() else str(workspace / path))
            else:
                resolved.append(str(resolve_workspace_path(p, workspace, access.allowed_root)))
        return resolved

    async def execute(
        self,
        content: str,
        channel: str | None = None,
        chat_id: str | None = None,
        message_id: str | None = None,
        media: list[str] | None = None,
        buttons: Any = None,
        reaction: Any = None,
        quote: Any = None,
        **kwargs: Any,
    ) -> str:
        from nanobot.utils.helpers import strip_think

        content = strip_think(content)

        button_rows: list[list[str]] | None = None
        if buttons is not None:
            raw_buttons = cast(list[Any], buttons) if isinstance(buttons, list) else None
            if raw_buttons is None or any(
                not isinstance(row, list)
                or any(not isinstance(label, str) for label in cast(list[Any], row))
                for row in raw_buttons
            ):
                return ToolResult.error("Error: buttons must be a list of list of strings")
            button_rows = cast(list[list[str]], raw_buttons)
        request_ctx = current_request_context()
        default_channel = (
            request_ctx.channel if request_ctx is not None else self._fallback_channel
        )
        default_chat_id = (
            request_ctx.chat_id if request_ctx is not None else self._fallback_chat_id
        )
        default_message_id = (
            request_ctx.message_id
            if request_ctx is not None
            else self._fallback_message_id
        )
        default_metadata = (
            request_ctx.metadata
            if request_ctx is not None
            else self._fallback_metadata
        )
        channel = channel or default_channel
        explicit_chat_id = chat_id
        if (
            default_channel == "websocket"
            and channel == "websocket"
            and explicit_chat_id is not None
            and str(explicit_chat_id).strip() != ""
            and str(explicit_chat_id).strip() != str(default_chat_id).strip()
        ):
            return ToolResult.error(
                "Error: chat_id does not match the active WebSocket conversation. "
                "Omit chat_id (and usually channel) so delivery uses the current "
                "conversation id from context — WebSocket client_id strings "
                "(e.g. anon-…) are not chat ids."
            )
        chat_id = chat_id or default_chat_id
        # Only inherit default message_id when targeting the same channel+chat.
        # Cross-chat sends must not carry the original message_id, because
        # some channels (e.g. Feishu) use it to determine the target
        # conversation via their Reply API, which would route the message
        # to the wrong chat entirely.
        same_target = channel == default_channel and chat_id == default_chat_id
        if same_target:
            message_id = message_id or default_message_id
        else:
            message_id = None

        if not channel or not chat_id:
            return ToolResult.error("Error: No target channel/chat specified")

        if not self._send_callback:
            return ToolResult.error("Error: Message sending not configured")

        if media:
            try:
                media = self._resolve_media(media)
            except (OSError, PermissionError, ValueError) as e:
                return ToolResult.error(f"Error: media path is not allowed: {str(e)}")

        metadata = dict(default_metadata) if same_target else {}
        if quote is not None:
            if channel != "signal":
                return ToolResult.error("Error: quotes are supported only on Signal")
            if reaction is not None or not (content or media):
                return ToolResult.error("Error: send quotes with text or media, not reactions")
            errors = _QUOTE_SCHEMA.validate_value(quote, "quote")
            if errors:
                return ToolResult.error(f"Error: {'; '.join(errors)}")
            quote_data = dict(cast(dict[str, Any], quote))
            has_author = "target_author" in quote_data
            has_timestamp = "target_timestamp" in quote_data
            if has_author != has_timestamp:
                return ToolResult.error(
                    "Error: supply both quote target_author and target_timestamp, or omit both"
                )
            if not has_author:
                if not same_target:
                    return ToolResult.error(
                        "Error: quotes to another chat require target_author and target_timestamp"
                    )
                if default_metadata.get("reaction"):
                    return ToolResult.error(
                        "Error: quoting a reaction requires an explicit original message target"
                    )
                quote_data["target_author"] = default_metadata.get("sender_number")
                quote_data["target_timestamp"] = default_metadata.get("timestamp")
                quote_data.setdefault("text", default_metadata.get("message_text", ""))
            errors = _QUOTE_SCHEMA.validate_value(quote_data, "quote")
            if errors:
                return ToolResult.error(
                    f"Error: cannot resolve Signal quote target: {'; '.join(errors)}"
                )
            author = quote_data.get("target_author")
            timestamp = quote_data.get("target_timestamp")
            if not isinstance(author, str) or not author.strip() or timestamp is None:
                return ToolResult.error("Error: cannot resolve Signal quote target")
            quote_data["target_author"] = author.strip()
            quote_data["target_timestamp"] = int(cast(int, timestamp))
            quote_data.setdefault("text", "")
            metadata["signal_quote"] = quote_data
        if reaction is not None:
            if channel != "signal":
                return ToolResult.error("Error: reactions are supported only on Signal")
            errors = _REACTION_SCHEMA.validate_value(reaction, "reaction")
            if errors:
                return ToolResult.error(f"Error: {'; '.join(errors)}")
            reaction_data = dict(cast(dict[str, Any], reaction))
            if content or media or button_rows:
                return ToolResult.error(
                    "Error: send reactions separately with content='' and no media/buttons"
                )
            has_author = "target_author" in reaction_data
            has_timestamp = "target_timestamp" in reaction_data
            if has_author != has_timestamp:
                return ToolResult.error(
                    "Error: supply both reaction target_author and target_timestamp, or omit both"
                )
            if not has_author:
                if not same_target:
                    return ToolResult.error(
                        "Error: reactions to another chat require target_author and target_timestamp"
                    )
                incoming_reaction = default_metadata.get("reaction")
                if isinstance(incoming_reaction, dict):
                    target = cast(dict[str, Any], incoming_reaction)
                    reaction_data["target_author"] = target.get("target_author")
                    reaction_data["target_timestamp"] = target.get("target_timestamp")
                else:
                    reaction_data["target_author"] = default_metadata.get("sender_number")
                    reaction_data["target_timestamp"] = default_metadata.get("timestamp")
            errors = _REACTION_SCHEMA.validate_value(reaction_data, "reaction")
            if errors:
                return ToolResult.error(
                    f"Error: cannot resolve Signal reaction target: {'; '.join(errors)}"
                )
            target_author = cast(str, reaction_data["target_author"])
            if (
                not reaction_data["emoji"].strip()
                or not target_author.strip()
            ):
                return ToolResult.error("Error: reaction emoji and target_author must not be blank")
            reaction_data["target_author"] = target_author.strip()
            reaction_data["target_timestamp"] = int(cast(int, reaction_data["target_timestamp"]))
            metadata["signal_reaction"] = reaction_data
        if message_id:
            metadata["message_id"] = message_id
        if media:
            metadata["_record_channel_delivery"] = True
        if same_target and request_ctx is not None and request_ctx.session_generation is not None:
            metadata["_session_key"] = request_ctx.session_key
            metadata["_session_generation"] = request_ctx.session_generation

        msg = OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=content,
            media=media or [],
            buttons=button_rows or [],
            metadata=metadata,
            delivery=(DeliveryRequest.from_context(
                default_metadata, request_ctx.session_key, request_ctx.session_generation,
            ) if request_ctx is not None else None),
        )

        if self._suppress_delivery_var.get():
            logger.debug("MessageTool: delivery suppressed during internal check")
            return f"Message acknowledged for {channel}:{chat_id} (not delivered)"

        try:
            await self._send_callback(msg)
            sends = _CURRENT_MESSAGE_SENDS.get()
            if sends is not None:
                sends.add((channel, chat_id))
            media_info = f" with {len(media)} attachments" if media else ""
            button_info = (
                f" with {sum(len(row) for row in button_rows)} button(s)"
                if button_rows
                else ""
            )
            action = "queued" if msg.delivery is not None else "sent"
            kind = "Reaction" if reaction is not None else "Message"
            return f"{kind} {action} to {channel}:{chat_id}{media_info}{button_info}"
        except Exception as e:
            return ToolResult.error(f"Error sending message: {str(e)}")
