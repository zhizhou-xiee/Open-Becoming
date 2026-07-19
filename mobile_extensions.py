"""Stable extension contracts for optional mobile companion features.

The core application intentionally ships without native Android/iOS code.  This
module keeps the integration boundary small: proactive messages can be sent to
a signed webhook, while native-player sync, voice, and phone search are
implemented by an operator-controlled companion or MCP service.  The web app's
together-listening room is a separate built-in capability.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests


PUSH_EVENT_VERSION = 1
PUSH_PREVIEW_LIMIT = 240

MUSIC_MCP_TOOLS = (
    "music_get_state",
    "music_start_session",
    "music_control",
)
PHONE_MCP_TOOLS = ("phone_search",)
VOICE_MCP_TOOLS = (
    "voice_get_capabilities",
    "voice_transcribe",
    "voice_synthesize",
)


class MobilePushError(RuntimeError):
    """Raised when a configured companion webhook cannot accept an event."""


def validate_push_webhook_url(url: str) -> str:
    clean = (url or "").strip()
    parsed = urlparse(clean)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("移动推送地址必须是完整的 http:// 或 https:// URL")
    if parsed.username or parsed.password:
        raise ValueError("移动推送地址里不能直接包含账号或密码")
    return clean


def _env_enabled(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class MobilePushConfig:
    enabled: bool = False
    url: str = ""
    secret: str = ""
    timeout: float = 5.0


class MobilePushClient:
    def __init__(self, config: MobilePushConfig | None = None):
        self.config = config or MobilePushConfig()

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled and self.config.url and self.config.secret)

    @classmethod
    def from_env(cls, environ=None):
        values = os.environ if environ is None else environ
        enabled = _env_enabled(values.get("MOBILE_PUSH_ENABLED"))
        if not enabled:
            return cls()

        url = validate_push_webhook_url(values.get("MOBILE_PUSH_WEBHOOK_URL", ""))
        secret = str(values.get("MOBILE_PUSH_WEBHOOK_SECRET") or "").strip()
        if len(secret) < 16:
            raise ValueError("MOBILE_PUSH_WEBHOOK_SECRET 至少需要 16 个字符")
        try:
            timeout = float(values.get("MOBILE_PUSH_TIMEOUT", "5"))
        except (TypeError, ValueError) as exc:
            raise ValueError("MOBILE_PUSH_TIMEOUT 必须是数字") from exc
        if not 0.5 <= timeout <= 30:
            raise ValueError("MOBILE_PUSH_TIMEOUT 需在 0.5–30 秒之间")
        return cls(MobilePushConfig(True, url, secret, timeout))

    def send_message(
        self,
        *,
        character_id: str,
        character_name: str,
        text: str,
        message_id: int | None,
        source: str = "autonomous",
        now: float | None = None,
    ) -> bool:
        """Send a minimal, signed message preview to the configured bridge."""
        if not self.enabled:
            return False

        timestamp_value = float(time.time() if now is None else now)
        timestamp = str(int(timestamp_value))
        preview = " ".join(str(text or "").split())[:PUSH_PREVIEW_LIMIT]
        event = {
            "version": PUSH_EVENT_VERSION,
            "event": "message.created",
            "created_at": datetime.fromtimestamp(
                timestamp_value, timezone.utc
            ).isoformat().replace("+00:00", "Z"),
            "data": {
                "character_id": str(character_id or "")[:80],
                "character_name": str(character_name or "")[:120],
                "message_id": int(message_id) if message_id is not None else None,
                "preview": preview,
                "source": str(source or "autonomous")[:40],
            },
        }
        body = json.dumps(
            event,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        signature = hmac.new(
            self.config.secret.encode("utf-8"),
            timestamp.encode("ascii") + b"." + body,
            hashlib.sha256,
        ).hexdigest()
        try:
            response = requests.post(
                self.config.url,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Becoming-Event": "message.created",
                    "X-Becoming-Timestamp": timestamp,
                    "X-Becoming-Signature": f"sha256={signature}",
                },
                timeout=self.config.timeout,
            )
        except requests.RequestException as exc:
            raise MobilePushError(f"移动推送连接失败：{exc}") from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise MobilePushError(f"移动推送返回 HTTP {response.status_code}")
        return True


def public_mobile_manifest(push_enabled: bool = False) -> dict:
    """Return a secret-free description of the supported extension points."""
    return {
        "version": 1,
        "push": {
            "configured": bool(push_enabled),
            "transport": "signed-webhook-v1",
            "event": "message.created",
        },
        "music": {
            "extension_point": "custom_mcp",
            "scope": "native-player-bridge",
            "built_in": False,
            "web_room_built_in": True,
            "tools": list(MUSIC_MCP_TOOLS),
        },
        "phone": {
            "extension_point": "custom_mcp",
            "built_in": False,
            "read_only": True,
            "tools": list(PHONE_MCP_TOOLS),
        },
        "voice": {
            "extension_points": ["mobile_companion", "custom_mcp"],
            "built_in": False,
            "audio_transport": "opaque-reference-v1",
            "stores_audio": False,
            "requires_user_gesture": True,
            "directions": ["speech_to_text", "text_to_speech"],
            "tools": list(VOICE_MCP_TOOLS),
        },
    }
