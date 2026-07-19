"""Pluggable HTTP text-to-speech and speech-to-text clients.

The application keeps persistence, quotas, and authentication in ``app.py``.
This module only validates endpoints and translates the two supported wire
contracts into small Python values that are easy to test.
"""

from __future__ import annotations

import base64
import ipaddress
import json
from dataclasses import dataclass
from urllib.parse import urlparse

import requests


TTS_PROVIDERS = {"openai_compatible", "custom_http"}
STT_PROVIDERS = {"openai_compatible", "custom_http"}
ALLOWED_AUDIO_MIMES = {
    "audio/aac",
    "audio/flac",
    "audio/m4a",
    "audio/mp4",
    "audio/mpeg",
    "audio/ogg",
    "audio/opus",
    "audio/wav",
    "audio/webm",
    "audio/x-m4a",
    "audio/x-wav",
}
MIME_BY_FORMAT = {
    "aac": "audio/aac",
    "flac": "audio/flac",
    "mp3": "audio/mpeg",
    "opus": "audio/ogg",
    "wav": "audio/wav",
}


class VoiceServiceError(RuntimeError):
    """A safe, user-displayable voice provider error."""


@dataclass(frozen=True)
class SynthesizedAudio:
    content: bytes
    mime_type: str


def validate_voice_endpoint(value: str) -> str:
    endpoint = str(value or "").strip()
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("语音地址必须是完整的 http:// 或 https:// URL")
    if parsed.username or parsed.password:
        raise ValueError("语音地址里不能直接包含账号或密码")
    hostname = (parsed.hostname or "").strip("[]")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address and (address.is_link_local or address.is_multicast or address.is_unspecified):
        raise ValueError("语音地址不能指向链路本地、组播或未指定地址")
    return endpoint


def _authorization_headers(token: str) -> dict[str, str]:
    clean = str(token or "").strip()
    return {"Authorization": f"Bearer {clean}"} if clean else {}


def _provider_error(response, sensitive=()) -> VoiceServiceError:
    detail = ""
    try:
        payload = response.json()
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                detail = str(error.get("message") or "")
            elif error:
                detail = str(error)
            elif payload.get("message"):
                detail = str(payload["message"])
    except (ValueError, json.JSONDecodeError):
        detail = ""
    detail = detail.replace("\n", " ").strip()[:240]
    for value in sensitive:
        clean = str(value or "").strip()
        if clean:
            detail = detail.replace(clean, "[已隐藏]")
    suffix = f"：{detail}" if detail else ""
    return VoiceServiceError(f"语音服务返回 HTTP {response.status_code}{suffix}")


def _decode_custom_audio(payload: dict) -> tuple[bytes, str] | None:
    encoded = payload.get("audio_base64") or payload.get("audio")
    if not isinstance(encoded, str) or not encoded.strip():
        return None
    encoded = encoded.strip()
    mime_type = str(payload.get("content_type") or payload.get("mime_type") or "audio/mpeg")
    if encoded.startswith("data:"):
        header, separator, body = encoded.partition(",")
        if not separator or ";base64" not in header:
            raise VoiceServiceError("自定义 TTS 返回的 data URL 不是 base64 音频")
        mime_type = header[5:].split(";", 1)[0] or mime_type
        encoded = body
    try:
        return base64.b64decode(encoded, validate=True), mime_type
    except (ValueError, TypeError) as exc:
        raise VoiceServiceError("自定义 TTS 返回的音频 base64 无法解析") from exc


def synthesize_speech(
    *,
    provider: str,
    endpoint: str,
    token: str,
    model: str,
    voice_id: str,
    text: str,
    response_format: str = "mp3",
    timeout: float = 60,
    max_audio_bytes: int = 8 * 1024 * 1024,
    request_func=None,
) -> SynthesizedAudio:
    if provider not in TTS_PROVIDERS:
        raise VoiceServiceError("不支持的 TTS 类型")
    endpoint = validate_voice_endpoint(endpoint)
    model = str(model or "").strip()
    voice_id = str(voice_id or "").strip()
    text = str(text or "").strip()
    response_format = str(response_format or "mp3").strip().lower()
    if not model or not voice_id or not text:
        raise VoiceServiceError("TTS 需要地址、模型、voice_id 和文字")

    headers = {"Accept": "audio/*, application/json", **_authorization_headers(token)}
    if provider == "openai_compatible":
        payload = {
            "model": model,
            "input": text,
            "voice": voice_id,
            "response_format": response_format,
        }
    else:
        payload = {
            "text": text,
            "model": model,
            "voice_id": voice_id,
            "response_format": response_format,
        }
    sender = request_func or requests.post
    try:
        response = sender(endpoint, headers=headers, json=payload, timeout=timeout)
    except requests.RequestException as exc:
        raise VoiceServiceError(f"TTS 连接失败：{exc}") from exc
    if not 200 <= response.status_code < 300:
        raise _provider_error(response, (token,))

    mime_type = (response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    content = bytes(response.content or b"")
    if mime_type in {"application/json", "text/json"}:
        try:
            decoded = _decode_custom_audio(response.json())
        except (ValueError, json.JSONDecodeError) as exc:
            raise VoiceServiceError("TTS 返回了无法解析的 JSON") from exc
        if not decoded:
            raise VoiceServiceError("TTS JSON 响应缺少 audio_base64")
        content, mime_type = decoded
    elif not mime_type.startswith("audio/") and mime_type != "application/octet-stream":
        raise VoiceServiceError("TTS 没有返回音频内容")
    if not content:
        raise VoiceServiceError("TTS 返回了空音频")
    if len(content) > max_audio_bytes:
        raise VoiceServiceError("TTS 返回的音频超过大小限制")
    if mime_type == "application/octet-stream" or not mime_type:
        mime_type = MIME_BY_FORMAT.get(response_format, "audio/mpeg")
    return SynthesizedAudio(content=content, mime_type=mime_type)


def transcribe_speech(
    *,
    provider: str,
    endpoint: str,
    token: str,
    model: str,
    filename: str,
    mime_type: str,
    content: bytes,
    timeout: float = 90,
    request_func=None,
) -> str:
    if provider not in STT_PROVIDERS:
        raise VoiceServiceError("不支持的 STT 类型")
    endpoint = validate_voice_endpoint(endpoint)
    model = str(model or "").strip()
    if not model or not content:
        raise VoiceServiceError("STT 需要地址、模型和录音文件")
    sender = request_func or requests.post
    data = {"model": model}
    if provider == "openai_compatible":
        data["response_format"] = "json"
    files = {"file": (filename or "recording.webm", content, mime_type or "application/octet-stream")}
    try:
        response = sender(
            endpoint,
            headers=_authorization_headers(token),
            data=data,
            files=files,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise VoiceServiceError(f"STT 连接失败：{exc}") from exc
    if not 200 <= response.status_code < 300:
        raise _provider_error(response, (token,))

    response_type = (response.headers.get("content-type") or "").lower()
    if "json" in response_type:
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise VoiceServiceError("STT 返回了无法解析的 JSON") from exc
        transcript = payload.get("text") if isinstance(payload, dict) else ""
    else:
        transcript = response.text
    transcript = str(transcript or "").strip()
    if not transcript:
        raise VoiceServiceError("STT 没有识别出文字")
    return transcript
