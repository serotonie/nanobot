from __future__ import annotations

import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from websockets.http11 import Request as WsRequest

from nanobot.webui.http_utils import bearer_token, parse_query, query_first

IssuedTokenAudience = Literal["client", "webui"]


@dataclass
class GatewayTokenStore:
    """Own short-lived WebSocket and WebUI API tokens for one gateway process."""

    max_tokens: int = 10_000
    instance_id: str = field(default_factory=lambda: str(uuid.uuid4()), init=False)
    issued_tokens: dict[str, float] = field(default_factory=dict)
    issued_token_audiences: dict[str, IssuedTokenAudience] = field(default_factory=dict)
    issued_token_sessions: dict[str, str] = field(default_factory=dict)
    api_tokens: dict[str, float] = field(default_factory=dict)
    api_token_sessions: dict[str, str] = field(default_factory=dict)
    sessions: dict[str, float] = field(default_factory=dict)

    def check_api_token(self, request: WsRequest) -> bool:
        self._purge_expired_api_tokens()
        self._purge_expired_sessions()
        token = bearer_token(request.headers) or query_first(
            parse_query(request.path), "token"
        )
        if not token:
            return False
        expiry = self.api_tokens.get(token)
        if expiry is None or time.monotonic() > expiry:
            self.api_tokens.pop(token, None)
            self.api_token_sessions.pop(token, None)
            return False
        session_id = self.api_token_sessions.get(token)
        if session_id is not None and session_id not in self.sessions:
            self.api_tokens.pop(token, None)
            self.api_token_sessions.pop(token, None)
            return False
        return True

    def can_issue(self, *, include_api_token: bool = False) -> bool:
        self._purge_expired_issued_tokens()
        self._purge_expired_api_tokens()
        self._purge_expired_sessions()
        if len(self.issued_tokens) >= self.max_tokens:
            return False
        if include_api_token and len(self.api_tokens) >= self.max_tokens:
            return False
        if len(self.sessions) >= self.max_tokens:
            return False
        return True

    def issue_session(self, ttl_s: int | float) -> str:
        """Create an opaque server-owned identity shared by one credential pair."""
        self._purge_expired_sessions()
        if len(self.sessions) >= self.max_tokens:
            raise OverflowError("too many active WebUI auth sessions")
        session_id = secrets.token_urlsafe(32)
        self.sessions[session_id] = time.monotonic() + float(ttl_s)
        return session_id

    def issue_token(
        self,
        ttl_s: int | float,
        *,
        audience: IssuedTokenAudience = "client",
        session_id: str | None = None,
    ) -> str:
        if audience == "webui" and session_id is None:
            session_id = self.issue_session(ttl_s)
        if session_id is not None and session_id not in self.sessions:
            raise ValueError("unknown WebUI auth session")
        token_value = f"nbwt_{secrets.token_urlsafe(32)}"
        expiry = time.monotonic() + float(ttl_s)
        self.issued_tokens[token_value] = expiry
        self.issued_token_audiences[token_value] = audience
        if session_id is not None:
            self.issued_token_sessions[token_value] = session_id
        return token_value

    def issue_api_token(self, ttl_s: int | float, *, session_id: str | None = None) -> str:
        if session_id is not None and session_id not in self.sessions:
            raise ValueError("unknown WebUI auth session")
        token_value = f"nbwt_{secrets.token_urlsafe(32)}"
        expiry = time.monotonic() + float(ttl_s)
        self.api_tokens[token_value] = expiry
        if session_id is not None:
            self.api_token_sessions[token_value] = session_id
        return token_value

    def take_issued_token_identity(
        self,
        token_value: str | None,
    ) -> tuple[IssuedTokenAudience, str | None] | None:
        if not token_value:
            return None
        self._purge_expired_issued_tokens()
        self._purge_expired_sessions()
        expiry = self.issued_tokens.pop(token_value, None)
        audience = self.issued_token_audiences.pop(token_value, None)
        session_id = self.issued_token_sessions.pop(token_value, None)
        if expiry is None or audience is None or time.monotonic() > expiry:
            return None
        if session_id is not None and session_id not in self.sessions:
            return None
        return audience, session_id

    def take_issued_token_audience(
        self,
        token_value: str | None,
    ) -> IssuedTokenAudience | None:
        identity = self.take_issued_token_identity(token_value)
        return identity[0] if identity is not None else None

    def revoke_session(self, session_id: str) -> bool:
        """Revoke credentials attached to one auth session."""
        existed = self.sessions.pop(session_id, None) is not None
        for token, owner in tuple(self.issued_token_sessions.items()):
            if owner == session_id:
                self.issued_token_sessions.pop(token, None)
                self.issued_tokens.pop(token, None)
                self.issued_token_audiences.pop(token, None)
        for token, owner in tuple(self.api_token_sessions.items()):
            if owner == session_id:
                self.api_token_sessions.pop(token, None)
                self.api_tokens.pop(token, None)
        return existed

    def session_is_active(self, session_id: str) -> bool:
        self._purge_expired_sessions()
        return session_id in self.sessions

    def clear(self) -> None:
        self.issued_tokens.clear()
        self.issued_token_audiences.clear()
        self.issued_token_sessions.clear()
        self.api_tokens.clear()
        self.api_token_sessions.clear()
        self.sessions.clear()

    def _purge_expired_api_tokens(self) -> None:
        now = time.monotonic()
        for token_key, expiry in list(self.api_tokens.items()):
            if now > expiry:
                self.api_tokens.pop(token_key, None)
                self.api_token_sessions.pop(token_key, None)

    def _purge_expired_issued_tokens(self) -> None:
        now = time.monotonic()
        for token_key, expiry in list(self.issued_tokens.items()):
            if now > expiry:
                self.issued_tokens.pop(token_key, None)
                self.issued_token_audiences.pop(token_key, None)
                self.issued_token_sessions.pop(token_key, None)

    def _purge_expired_sessions(self) -> None:
        now = time.monotonic()
        for session_id, expiry in tuple(self.sessions.items()):
            if now > expiry:
                self.revoke_session(session_id)


def token_response_payload(token: str, expires_in: Any) -> dict[str, Any]:
    return {"token": token, "expires_in": expires_in}
