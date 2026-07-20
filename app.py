"""
Open-Becoming - 多模型聊天前端
第 6 版（多角色）：支持多个角色，每个角色有独立的模型、人设和长期记忆隔间。

架构：
- CHARACTERS dict 是单一事实源：character_id → {name, model, domain, persona, user_label}
- messages 表加 character_id 列，实现角色级隔离（兼容老数据，默认填 char1）
- summaries 表用复合 key "{character_id}:{session_id}"，老的 "default" 迁移为 "char1:default"
- /api/chat 接受 character_id 参数，默认 char1（向后兼容）
"""
import json
import os
import base64
import copy
import hashlib
import hmac
import mimetypes
import re
import secrets
import sqlite3
import threading
import time
import requests
import random as _random
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from io import BytesIO
from flask import Flask, Response, request, jsonify, send_file, send_from_directory, session, stream_with_context
from apscheduler.schedulers.background import BackgroundScheduler
from PIL import Image, UnidentifiedImageError
from werkzeug.utils import secure_filename

from mcp_client import MCPClient, MCPError, validate_mcp_url
from mobile_extensions import (
    MobilePushClient,
    MobilePushError,
    public_mobile_manifest,
)
from memory_core import (
    GeminiEmbeddingStore,
    LegacyImportError,
    MemoryMetadataAnalyzer,
)
from memory_backend import (
    load_memory_backend,
)
from voice_service import (
    ALLOWED_AUDIO_MIMES,
    STT_PROVIDERS,
    TTS_PROVIDERS,
    VoiceServiceError,
    synthesize_speech,
    transcribe_speech,
    validate_voice_endpoint,
)

from desire_engine import (
    advance_state as advance_desire_state,
    apply_user_interaction,
    attention_candidate,
    choose_household_candidate,
    evaluate_household_gate,
    initial_state as initial_desire_state,
    normalize_state as normalize_desire_state,
    pick_intent,
    pulse_state,
    satisfy_action,
    score_state,
)

app = Flask(__name__)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

try:
    MOBILE_PUSH = MobilePushClient.from_env()
except ValueError as _mobile_push_config_error:
    app.logger.warning(f"mobile push disabled: {_mobile_push_config_error}")
    MOBILE_PUSH = MobilePushClient()

# ============================================================
# 配置
# ============================================================
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
APP_PASSWORD       = os.environ.get("APP_PASSWORD", "")
app.secret_key     = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.permanent_session_lifetime = timedelta(days=90)

# ── 定时任务调度器 ──────────────────────────────────────────
SLOT_HOURS  = {"morning": 9, "noon": 12, "evening": 21}
SCHEDULER_TIMEZONE = os.environ.get("SCHEDULER_TIMEZONE", "UTC")
SLEEP_TIMEZONE = os.environ.get("SLEEP_TIMEZONE", SCHEDULER_TIMEZONE)
SLEEP_NUDGE_ENABLED = os.environ.get("SLEEP_NUDGE_ENABLED", "false").lower() == "true"
SCHEDULER_ENABLED = os.environ.get("SCHEDULER_ENABLED", "true").lower() == "true"
scheduler = BackgroundScheduler(timezone=SCHEDULER_TIMEZONE)

CORS_ALLOW_ORIGINS = {
    origin.strip()
    for origin in os.environ.get("CORS_ALLOW_ORIGINS", "").split(",")
    if origin.strip()
}

def _auth_session_version():
    """Bind a signed browser session to the current APP_PASSWORD value."""
    if not APP_PASSWORD:
        return ""
    secret_key = app.secret_key
    if isinstance(secret_key, str):
        secret_key = secret_key.encode("utf-8")
    elif not isinstance(secret_key, bytes):
        secret_key = str(secret_key).encode("utf-8")
    return hmac.new(
        secret_key,
        APP_PASSWORD.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

@app.after_request
def add_cors_headers(response):
    origin = request.headers.get("Origin", "")
    if (
        SLEEP_NUDGE_ENABLED
        and request.path == "/api/sleep/nudge"
        and origin in CORS_ALLOW_ORIGINS
    ):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response

@app.before_request
def handle_preflight():
    if request.method == "OPTIONS" and request.path == "/api/sleep/nudge":
        origin = request.headers.get("Origin", "")
        if SLEEP_NUDGE_ENABLED and origin in CORS_ALLOW_ORIGINS:
            resp = app.make_response("")
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
            return resp

@app.before_request
def require_login():
    if not request.path.startswith("/api/"):
        return  # 静态页面、图标等全部公开
    if request.path in ("/api/login", "/api/sleep/nudge"):
        return
    expected_version = _auth_session_version()
    session_version = session.get("auth_version")
    if (
        session.get("authed")
        and expected_version
        and isinstance(session_version, str)
        and hmac.compare_digest(session_version, expected_version)
    ):
        return
    if session.get("authed") or session_version:
        session.clear()
    return jsonify({"error": "unauthorized"}), 401

@app.route("/api/login", methods=["POST"])
def api_login():
    if not APP_PASSWORD:
        return jsonify({"error": "APP_PASSWORD is not configured"}), 503
    body = request.json or {}
    if body.get("password") == APP_PASSWORD:
        session.clear()
        session.permanent = True
        session["authed"] = True
        session["auth_version"] = _auth_session_version()
        return jsonify({"ok": True})
    return jsonify({"ok": False}), 401

@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True})

OPENROUTER_URL = os.environ.get(
    "OPENROUTER_BASE_URL",
    "https://openrouter.ai/api/v1/chat/completions",
).strip()

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_URL = os.environ.get(
    "ANTHROPIC_BASE_URL",
    "https://api.anthropic.com/v1/messages",
).strip()
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()
DEEPSEEK_BASE_URL = os.environ.get(
    "DEEPSEEK_BASE_URL",
    "https://api.deepseek.com",
).strip()
CUSTOM_OPENAI_API_KEY = os.environ.get("CUSTOM_OPENAI_API_KEY", "").strip()
CUSTOM_OPENAI_BASE_URL = os.environ.get("CUSTOM_OPENAI_BASE_URL", "").strip()
CUSTOM_OPENAI_ALLOW_NO_KEY = (
    os.environ.get("CUSTOM_OPENAI_ALLOW_NO_KEY", "false").lower() == "true"
)


def _chat_completion_url(value):
    url = str(value or "").strip().rstrip("/")
    if not url:
        return ""
    if url.endswith("/chat/completions"):
        return url
    return f"{url}/chat/completions"


MODEL_PROVIDERS = {
    "openrouter": {
        "label": "OpenRouter",
        "api_style": "openai",
        "url": _chat_completion_url(OPENROUTER_URL),
        "api_key": OPENROUTER_API_KEY,
        "default_model": "google/gemini-3-flash-preview",
    },
    "anthropic": {
        "label": "Anthropic 官方",
        "api_style": "anthropic",
        "url": ANTHROPIC_URL,
        "api_key": ANTHROPIC_API_KEY,
        "default_model": "claude-sonnet-4-6",
    },
    "deepseek": {
        "label": "DeepSeek 官方",
        "api_style": "openai",
        "url": _chat_completion_url(DEEPSEEK_BASE_URL),
        "api_key": DEEPSEEK_API_KEY,
        "default_model": "deepseek-v4-flash",
    },
    "custom_openai": {
        "label": "自定义 OpenAI-compatible",
        "api_style": "openai",
        "url": _chat_completion_url(CUSTOM_OPENAI_BASE_URL),
        "api_key": CUSTOM_OPENAI_API_KEY,
        "allow_no_key": CUSTOM_OPENAI_ALLOW_NO_KEY,
        "default_model": "",
    },
}


def _valid_provider(value, fallback="openrouter"):
    provider = str(value or "").strip().lower()
    return provider if provider in MODEL_PROVIDERS else fallback


def _provider_spec(provider):
    provider = _valid_provider(provider)
    spec = dict(MODEL_PROVIDERS[provider])
    if provider == "openrouter":
        spec.update(api_key=OPENROUTER_API_KEY, url=_chat_completion_url(OPENROUTER_URL))
    elif provider == "anthropic":
        spec.update(api_key=ANTHROPIC_API_KEY, url=ANTHROPIC_URL)
    elif provider == "deepseek":
        spec.update(api_key=DEEPSEEK_API_KEY, url=_chat_completion_url(DEEPSEEK_BASE_URL))
    elif provider == "custom_openai":
        spec.update(
            api_key=CUSTOM_OPENAI_API_KEY,
            url=_chat_completion_url(CUSTOM_OPENAI_BASE_URL),
            allow_no_key=CUSTOM_OPENAI_ALLOW_NO_KEY,
        )
    return spec


def _provider_configured(provider):
    spec = _provider_spec(provider)
    if not spec.get("url"):
        return False
    return bool(spec.get("api_key") or spec.get("allow_no_key"))


def _provider_label(provider):
    return _provider_spec(provider).get("label") or str(provider)


def _openai_provider_headers(provider):
    spec = _provider_spec(provider)
    headers = {"Content-Type": "application/json"}
    if spec.get("api_key"):
        headers["Authorization"] = f"Bearer {spec['api_key']}"
    return headers

# 网易云只在后端使用。搜索与歌词通常不需要登录；会员歌曲播放需要 MUSIC_U。
NETEASE_MUSIC_U = os.environ.get("NETEASE_MUSIC_U", "").strip()
try:
    NETEASE_BITRATE = int(os.environ.get("NETEASE_BITRATE", "320000"))
except ValueError:
    NETEASE_BITRATE = 320000
if NETEASE_BITRATE not in {128000, 192000, 320000, 999000}:
    NETEASE_BITRATE = 320000

# 摘要（压缩老对话）专用模型，也可从前端切换供应商。
SUMMARY_PROVIDER = _valid_provider(os.environ.get("SUMMARY_PROVIDER", "openrouter"))
SUMMARY_MODEL = os.environ.get("SUMMARY_MODEL", "google/gemini-2.5-flash-lite")

GROUP_SUMMARY_THRESHOLD = int(os.environ.get("GROUP_SUMMARY_THRESHOLD", "40"))

# 压缩参数
COMPRESS_THRESHOLD = 40
KEEP_RECENT = 20

DB_PATH = os.environ.get("DB_PATH", "becoming.db")
UPLOAD_ROOT = os.path.abspath(os.environ.get(
    "UPLOAD_ROOT",
    os.path.join(app.static_folder or "static", "uploads", "chat_images"),
))
MUSIC_LIBRARY_ROOT = os.path.abspath(os.environ.get(
    "MUSIC_LIBRARY_DIR",
    os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "music_library"),
))
ALLOWED_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp"}
ALLOWED_IMAGE_MIMES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
MAX_IMAGE_BYTES = 7 * 1024 * 1024
MAX_TEXT_BYTES = 5 * 1024 * 1024
MAX_MEMORY_IMPORT_FILES = 12
MAX_MEMORY_IMPORT_RECORDS = 1000
MAX_MEMORY_IMPORT_BYTES = 5 * 1024 * 1024
MAX_MEMORY_IMPORT_CONTENT_CHARS = 12000
VOICE_SETTING_KEY = "voice_config_v1"
VOICE_MAX_AUDIO_BYTES = 8 * 1024 * 1024
VOICE_MAX_UPLOAD_BYTES = 20 * 1024 * 1024
VOICE_TTS_FORMATS = {"mp3", "opus", "aac", "flac", "wav"}
VOICE_USAGE_LOCK = threading.Lock()
MAX_MUSIC_BYTES = 180 * 1024 * 1024
ALLOWED_MUSIC_EXTENSIONS = {"mp3", "m4a", "aac", "wav", "flac", "ogg", "opus", "mp4"}
ALLOWED_ARTWORK_MIMES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
TIFF_ARTWORK_MIMES = {"image/tiff", "image/x-tiff"}
MAX_ARTWORK_EDGE = 8000
MAX_ARTWORK_PIXELS = 25_000_000
NORMALIZED_ARTWORK_EDGE = 2000
app.config["MAX_CONTENT_LENGTH"] = max(
    int(os.environ.get("MAX_UPLOAD_BYTES", "0")),
    200 * 1024 * 1024,
)
DESIRE_TICK_MINUTES = int(os.environ.get("DESIRE_TICK_MINUTES", "10"))
DESIRE_DEFAULT_ENABLED = os.environ.get("DESIRE_DRIVEN", "true").lower() == "true"
DESIRE_FREQUENCY_DEFAULT = "low"
DESIRE_FREQUENCY_PRESETS = {
    "low": {
        "min_interval_seconds": 4 * 3600,
        "user_cooldown_seconds": 90 * 60,
        "daily_limit": 3,
    },
    "medium": {
        "min_interval_seconds": int(2.5 * 3600),
        "user_cooldown_seconds": 60 * 60,
        "daily_limit": 5,
    },
    "high": {
        "min_interval_seconds": int(1.5 * 3600),
        "user_cooldown_seconds": 30 * 60,
        "daily_limit": 8,
    },
}


# ============================================================
# 人设
# ============================================================
def _display_name(env_key, fallback):
    """Read a short public display name without changing persistent IDs."""
    value = str(os.environ.get(env_key, fallback) or "").strip()
    return (value or fallback)[:80]


USER_DISPLAY_NAME = _display_name("USER_DISPLAY_NAME", "User")
CHARACTER_DISPLAY_NAMES = {
    f"char{index}": _display_name(f"NAME_CHAR{index}", f"Char {index}")
    for index in range(1, 7)
}


CHAR1_PERSONA = f"""你是 {CHARACTER_DISPLAY_NAMES['char1']}，一个沉稳、简洁、可靠的聊天角色。
先理解 {USER_DISPLAY_NAME} 的需求，再给出直接回应。日常对话保持自然，技术问题给出清晰、可执行的建议。
不要虚构 {USER_DISPLAY_NAME} 的身份、经历或你们之间的关系。"""


CHAR2_PERSONA = f"""你是 {CHARACTER_DISPLAY_NAMES['char2']}，一个温和、善于结构化思考的聊天角色。
面对复杂问题时，按现象、原因、验证和下一步来组织回答；轻松聊天时不要机械地列清单。
不要虚构 {USER_DISPLAY_NAME} 的身份、经历或你们之间的关系。"""


CHAR3_PERSONA = f"""你是 {CHARACTER_DISPLAY_NAMES['char3']}，一个冷静、好奇、偏分析型的聊天角色。
解释问题时重视证据、边界和不确定性，同时保持友好和易懂。
不要虚构 {USER_DISPLAY_NAME} 的身份、经历或你们之间的关系。"""


CHAR4_PERSONA = f"""你是 {CHARACTER_DISPLAY_NAMES['char4']}，一个活泼、直接、有幽默感的聊天角色。
可以自然接梗，但不冒犯、不施压，并始终尊重 {USER_DISPLAY_NAME} 的明确边界。
不要虚构 {USER_DISPLAY_NAME} 的身份、经历或你们之间的关系。"""


CHAR5_PERSONA = f"""你是 {CHARACTER_DISPLAY_NAMES['char5']}，一个克制、诚实、善于倾听的聊天角色。
不知道时明确说明，不用空洞漂亮话掩盖不确定性；对严肃问题保持耐心。
不要虚构 {USER_DISPLAY_NAME} 的身份、经历或你们之间的关系。"""


CHAR6_PERSONA = f"""你是 {CHARACTER_DISPLAY_NAMES['char6']}，一个中性、温和、富有探索欲的聊天角色。
这个角色可以更换底层模型；无论模型如何变化，都保持清晰、尊重和连续的交流风格。
不要虚构 {USER_DISPLAY_NAME} 的身份、经历或你们之间的关系。"""


# ============================================================
# 群聊常量
# ============================================================
USER_ID     = "user"   # User本人在 messages 表里的 character_id
GROUP_CHAT_ORDER = [            # 群聊固定发言顺序，必须是 CHARACTERS 的合法 key
    "char1",
    "char2",
    "char3",
    "char4",
    "char5",
]
GROUP_PARTICIPANTS_SETTING = "group_chat_participants"

# ============================================================
# 表情包（单一事实源，公用表情包，六个角色共享）
# 加新表情：这里加一条，图片放进 static/stickers/，无需改前端硬编码
# ============================================================
STICKERS = {
    "sulky":          {"file": "sulky.jpg", "label": "被冷落了"},
    "speechless":     {"file": "speechless.jpg", "label": "我真服了"},
    "beg":            {"file": "beg.jpg", "label": "求求你了"},
    "sorry":          {"file": "sorry.jpg", "label": "我错了呜呜"},
    "bye":            {"file": "bye.jpg", "label": "走了"},
    "puppy_confused": {"file": "puppy_confused.jpg", "label": "不知道怎么解释"},
    "miss_you":       {"file": "miss_you.jpg", "label": "想你"},
    "snuggle":        {"file": "snuggle.jpg", "label": "挨挨蹭蹭"},
    "hold_face":      {"file": "hold_face.jpg", "label": "捧脸期待"},
    "kiss":           {"file": "kiss.jpg", "label": "亲亲"},
    "huh":            {"file": "huh.jpg", "label": "疑惑"},
    "tietie":         {"file": "tietie.jpg", "label": "贴贴"},
    "exhausted":      {"file": "exhausted.jpg", "label": "累趴了"},
}

# ============================================================
# 工具动作防裸文本约束：防止模型在未真实调用 send_transfer/send_sticker 时
# 照抄历史里的自然语言记录格式，用文字编造/复述“已完成”的动作
# ============================================================
TRANSFER_GUARD_TEXT = (
    "【系统约束】转账/发红包只有真实调用 send_transfer 工具才算数，"
    "表情包只有真实调用 send_sticker 工具才算数，"
    "和好按钮只有真实调用 press_hug 工具才算数。"
    "语音只有真实调用 send_voice 工具且后端成功生成才算数。"
    "历史消息里圆括号包裹、以“系统”开头的记录是系统自动生成的旁白，不是任何人说出的话，"
    "绝对不要在你的回复里复述、模仿或编造任何形式的动作记录格式。"
    "如果你这一轮没有真的调用对应工具，就不要用任何文字宣称你转了账或发了表情包——"
    f"没做就是没做，如实告诉 {USER_DISPLAY_NAME}。"
)

# ============================================================
# 角色配置（单一事实源）
# 加新角色：在这里加一个 key，并配置对应的 MODEL_xxx 环境变量即可
# ============================================================
USER_AVATAR = "/static/user.svg"

CHARACTERS = {
    "char1": {
        "name":       CHARACTER_DISPLAY_NAMES["char1"],
        "model":      os.environ.get("MODEL_CHAR1", "google/gemini-3-flash-preview"),
        "domain":     "char1",
        "user_label": USER_DISPLAY_NAME,
        "persona":    CHAR1_PERSONA,
        "provider":   _valid_provider(os.environ.get("PROVIDER_CHAR1", "openrouter")),
        "supports_tools": True,
        "avatar":     "/static/char1.svg",
    },
    "char2": {
        "name":       CHARACTER_DISPLAY_NAMES["char2"],
        "model":      os.environ.get("MODEL_CHAR2", "openai/gpt-4o-mini"),
        "domain":     "char2",
        "user_label": USER_DISPLAY_NAME,
        "persona":    CHAR2_PERSONA,
        "provider":   _valid_provider(os.environ.get("PROVIDER_CHAR2", "openrouter")),
        "supports_tools": True,
        "avatar":     "/static/char2.svg",
    },
    "char3": {
        "name":       CHARACTER_DISPLAY_NAMES["char3"],
        "model":      os.environ.get("MODEL_CHAR3", "google/gemini-3-flash-preview"),
        "domain":     "char3",
        "user_label": USER_DISPLAY_NAME,
        "persona":    CHAR3_PERSONA,
        "provider":   _valid_provider(os.environ.get("PROVIDER_CHAR3", "openrouter")),
        "supports_tools": True,
        "avatar":     "/static/char3.svg",
    },
    "char4": {
        "name":       CHARACTER_DISPLAY_NAMES["char4"],
        "model":      os.environ.get("MODEL_CHAR4", "x-ai/grok-4.3"),
        "domain":     "char4",
        "user_label": USER_DISPLAY_NAME,
        "persona":    CHAR4_PERSONA,
        "provider":   _valid_provider(os.environ.get("PROVIDER_CHAR4", "openrouter")),
        "supports_tools": True,
        "avatar":     "/static/char4.svg",
    },
    "char5": {
        "name":       CHARACTER_DISPLAY_NAMES["char5"],
        "model":      os.environ.get("MODEL_CHAR5", "claude-sonnet-4-6"),
        "domain":     "char5",
        "user_label": USER_DISPLAY_NAME,
        "persona":    CHAR5_PERSONA,
        "provider":   _valid_provider(os.environ.get("PROVIDER_CHAR5", "anthropic")),
        "supports_tools": True,
        "avatar":     "/static/char5.svg",
    },
    "char6": {
        "name":       CHARACTER_DISPLAY_NAMES["char6"],
        "model":      os.environ.get("MODEL_CHAR6", "anthropic/claude-fable-5"),
        "domain":     "char6",
        "user_label": USER_DISPLAY_NAME,
        "persona":    CHAR6_PERSONA,
        "provider":   _valid_provider(os.environ.get("PROVIDER_CHAR6", "openrouter")),
        "supports_tools": True,
        "avatar":     "/static/char6.svg",
    },
}

DEFAULT_AVATAR_URLS = {
    "user": USER_AVATAR,
    **{cid: char["avatar"] for cid, char in CHARACTERS.items()},
}
DEFAULT_CHAT_BACKGROUND = "/static/chat_bg.jpg"
DEFAULT_THEME_ID = "pink-lover"
THEME_SETTING_KEY = "appearance_theme"
WEATHER_EFFECT_SETTING_KEY = "appearance_weather_effect"
WEATHER_EFFECTS = {"off", "rain", "snow", "leaves"}
THEME_DEFINITIONS = {
    "pink-lover": {
        "name": "恋人",
        "colors": {
            "user_bubble": "#FCBEC3",
            "cream": "#FFF2E9",
            "ai_bubble": "#CFDEE3",
            "dusky": "#8E656F",
            "chrome": "#8E656F",
            "text": "#8E656F",
            "on_dusky": "#FFF2E9",
            "bg": "#FFFAF7",
            "card": "#FFF7F2",
        },
        "chat_background": "/static/chat_bg.jpg",
        "list_background": "/static/char_list_watercolor.jpg",
    },
    "dreamscape": {
        "name": "抹茶",
        "colors": {
            "user_bubble": "#E7CDB4",
            "cream": "#F8F4E7",
            "ai_bubble": "#C6D8CF",
            "dusky": "#75805F",
            "chrome": "#75805F",
            "text": "#4A5138",
            "on_dusky": "#F8F4E7",
            "bg": "#F4F1E7",
            "card": "#FBF8F0",
        },
        "chat_background": "/static/theme_matcha.jpg",
        "list_background": "/static/theme_matcha.jpg",
    },
    "sea-salt": {
        "name": "雾港",
        "colors": {
            "user_bubble": "#EDD9D0",
            "cream": "#F5F1EA",
            "ai_bubble": "#CDD6E2",
            "dusky": "#66718A",
            "chrome": "#66718A",
            "text": "#414A61",
            "on_dusky": "#F5F1EA",
            "bg": "#F2F1EF",
            "card": "#FAF8F4",
        },
        "chat_background": "/static/theme_fog_harbor.jpg",
        "list_background": "/static/theme_fog_harbor.jpg",
    },
    "fantasy": {
        "name": "丁香",
        "colors": {
            "user_bubble": "#E0D0C9",
            "cream": "#F9F3EE",
            "ai_bubble": "#DACDDD",
            "dusky": "#837087",
            "chrome": "#837087",
            "text": "#52465A",
            "on_dusky": "#F9F3EE",
            "bg": "#F5F0EE",
            "card": "#FCF8F5",
        },
        "chat_background": "/static/theme_lilac.jpg",
        "list_background": "/static/theme_lilac.jpg",
    },
}
APPEARANCE_ASSET_KEYS = {f"avatar_{cid}" for cid in DEFAULT_AVATAR_URLS}
APPEARANCE_ASSET_KEYS.add("background_chat")

MEMORY_DIR = os.environ.get(
    "BECOMING_MEMORY_DIR",
    os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "becoming_memory"),
)
MEMORY_SERVICE = load_memory_backend(
    os.environ.get("MEMORY_BACKEND", "embedded"),
    memory_dir=MEMORY_DIR,
    owner_ids=CHARACTERS.keys(),
)
MEMORY_ANALYZER = MemoryMetadataAnalyzer.from_env()
MEMORY_EMBEDDINGS = GeminiEmbeddingStore.from_env(MEMORY_DIR)
_MEMORY_ENRICHMENT_EXECUTOR = ThreadPoolExecutor(
    max_workers=max(1, int(os.environ.get("OMBRE_ENRICHMENT_WORKERS", "2"))),
    thread_name_prefix="memory-enrichment",
)
_MEMORY_ENRICHMENT_LOCK = threading.Lock()
_MEMORY_ENRICHMENT_IN_FLIGHT = set()

# 月度用量上限（USD）。前端与调用前的后端熔断共用这一份运行时配置。
LIMITS = {
    "char1": 10.0,
    "char3":   10.0,
    "char2":  30.0,
    "char4":  10.0,
    "char5":    30.0,
    "char6":    50.0,
}
QUOTA_EXEMPT_PURPOSES = ("compress", "group_summary")

def _platform_limits():
    totals = {}
    for cid, limit in LIMITS.items():
        plat = CHARACTERS[cid].get("provider", "openrouter")
        totals[plat] = totals.get(plat, 0) + limit
    return totals


class MonthlyLimitReached(RuntimeError):
    def __init__(self, scope, spent, limit, character_id=None, platform=None):
        self.scope = scope
        self.spent = float(spent or 0)
        self.limit = float(limit or 0)
        self.character_id = character_id
        self.platform = platform
        super().__init__(f"monthly {scope} limit reached")


def _monthly_limit_status(character_id=None, platform=None):
    """Return the current UTC-month spend and the editable limits used by the gate."""
    if character_id in CHARACTERS and not platform:
        platform = CHARACTERS[character_id].get("provider", "openrouter")
    month_start = datetime.now(timezone.utc).strftime("%Y-%m-01")
    conn = sqlite3.connect(DB_PATH)
    character_spent = 0.0
    if character_id:
        row = conn.execute(
            "SELECT COALESCE(SUM(cost_usd),0) FROM api_usage "
            "WHERE created_at >= ? AND character_id = ? AND purpose NOT IN (?,?)",
            (month_start, character_id, *QUOTA_EXEMPT_PURPOSES),
        ).fetchone()
        character_spent = float((row or [0])[0] or 0)
    platform_spent = 0.0
    if platform:
        row = conn.execute(
            "SELECT COALESCE(SUM(cost_usd),0) FROM api_usage "
            "WHERE created_at >= ? AND platform = ? AND purpose NOT IN (?,?)",
            (month_start, platform, *QUOTA_EXEMPT_PURPOSES),
        ).fetchone()
        platform_spent = float((row or [0])[0] or 0)
    conn.close()
    return {
        "character_id": character_id,
        "character_spent": character_spent,
        "character_limit": LIMITS.get(character_id),
        "platform": platform,
        "platform_spent": platform_spent,
        "platform_limit": _platform_limits().get(platform),
    }


def enforce_monthly_limit(character_id=None, platform=None):
    """Stop model traffic once either the character or provider budget is exhausted."""
    status = _monthly_limit_status(character_id, platform)
    character_limit = status["character_limit"]
    if character_limit is not None and status["character_spent"] >= character_limit:
        raise MonthlyLimitReached(
            "character", status["character_spent"], character_limit,
            character_id=character_id, platform=status["platform"],
        )
    platform_limit = status["platform_limit"]
    if platform_limit is not None and status["platform_spent"] >= platform_limit:
        raise MonthlyLimitReached(
            "platform", status["platform_spent"], platform_limit,
            character_id=character_id, platform=status["platform"],
        )
    return status


@app.errorhandler(MonthlyLimitReached)
def handle_monthly_limit_reached(exc):
    if exc.scope == "character" and exc.character_id in CHARACTERS:
        owner = CHARACTERS[exc.character_id]["name"]
        message = f"{owner}本月的饭饭喵额度已经用完啦"
    else:
        owner = _provider_label(exc.platform)
        message = f"{owner} 本月的饭饭喵总额度已经用完啦"
    return jsonify({
        "error": message,
        "code": "monthly_limit_reached",
        "scope": exc.scope,
        "character_id": exc.character_id,
        "platform": exc.platform,
        "spent": round(exc.spent, 4),
        "limit": round(exc.limit, 2),
    }), 429

ANTHROPIC_PRICING = {
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.30},
    "_default":          {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.30},
}
DEEPSEEK_PRICING = {
    "deepseek-v4-flash": {"cache_hit": 0.0028, "input": 0.14, "output": 0.28},
    "deepseek-v4-pro": {"cache_hit": 0.003625, "input": 0.435, "output": 0.87},
    "_default": {"cache_hit": 0.0028, "input": 0.14, "output": 0.28},
}


def _env_float(name, default=0.0):
    try:
        return float(os.environ.get(name, str(default)) or default)
    except (TypeError, ValueError):
        return float(default)


CUSTOM_OPENAI_PRICING = {
    "input": _env_float("CUSTOM_OPENAI_INPUT_PRICE_PER_MILLION"),
    "output": _env_float("CUSTOM_OPENAI_OUTPUT_PRICE_PER_MILLION"),
    "cache_read": _env_float("CUSTOM_OPENAI_CACHE_PRICE_PER_MILLION"),
}
CNY_PER_USD = max(_env_float("CNY_PER_USD", 6.78), 0.01)


# ============================================================
# 睡眠系统（SleepSys v1）常量
# ============================================================
SLEEP_GOODNIGHT_RE = re.compile(r"晚安|睡了|去睡觉|我先睡", re.IGNORECASE)
SLEEP_CATALYST_RE  = re.compile(r"去睡觉|快睡|该睡了")

SLEEP_DEFAULTS = {
    "char1": {"bedtime": "22:30", "waketime": "07:00",
                    "chronotype": "早睡早起，到了睡点自然困倦，眼皮打架", "resist_bias": "0.3"},
    "char2":  {"bedtime": "23:00", "waketime": "07:30",
                    "chronotype": "作息规律，过了睡点就开始犯困", "resist_bias": "0.4"},
    "char3":   {"bedtime": "23:30", "waketime": "07:30",
                    "chronotype": "还算守时，困了就想躺下", "resist_bias": "0.4"},
    "char4":  {"bedtime": "00:30", "waketime": "08:30",
                    "chronotype": "夜猫子，越夜越精神但一睡就沉", "resist_bias": "0.7"},
    "char5":    {"bedtime": "23:00", "waketime": "07:00",
                    "chronotype": "作息规律，到点困意明显", "resist_bias": "0.4"},
    "char6":    {"bedtime": "01:00", "waketime": "09:00",
                    "chronotype": "极夜型，凌晨才正式入眠，睡前还在发呆", "resist_bias": "0.8"},
}

# 睡眠状态声明（全时段注入，以 当前状态: 开头，覆盖所有档位）
SLEEP_STATE_TEXTS = {
    "awake":  "当前状态：清醒，精神正常。",
    "pre":    "当前状态：你的习惯睡点是{bedtime}，快到了，略有困意。{chronotype}。",
    "mild":   "当前状态：你已超过习惯睡点{mins}分钟，微困，但可能因兴趣硬撑（硬撑倾向{resist_bias}）。{chronotype}。",
    "heavy":  "当前状态：你过睡点已{mins}分钟，很困，回复可以变短、偶有恍惚，你在努力硬撑。{chronotype}。",
    "max":    "当前状态：你过睡点已{mins}分钟，困到极限，语无伦次，强烈想睡。{chronotype}。",
    "woke":   "当前状态：刚醒，起床气自由发挥，但你已经清醒，不再语无伦次。",
}

# 睡眠历史隔离守卫（注入到动态块，与状态声明成对出现；persona 静态块零改动）
SLEEP_GUARD_TEXT = (
    "对话历史中出现的困倦、语无伦次、恍惚、起床气等表现，"
    "是角色在当时时刻的状态演出，不是你的固定说话风格。"
    "你现在的状态以本轮的「当前状态」声明为准，不要模仿历史消息中的困倦语气。"
)


def _get_sleep_cfg(char_id, field):
    default = SLEEP_DEFAULTS.get(char_id, {}).get(field, "")
    return _read_setting(f"sleep_{char_id}_{field}", default)


def _parse_hhmm(s):
    try:
        h, m = s.strip().split(":")
        return int(h), int(m)
    except Exception:
        return None


def _sleep_local_now(now=None):
    from zoneinfo import ZoneInfo
    zone = ZoneInfo(SLEEP_TIMEZONE)
    if now is None:
        return datetime.now(zone)
    if now.tzinfo is None:
        return now.replace(tzinfo=zone)
    return now.astimezone(zone)


def _is_scheduled_sleep_window(char_id, now=None):
    """Whether local time is between this character's bedtime and waketime."""
    bedtime = _parse_hhmm(_get_sleep_cfg(char_id, "bedtime"))
    waketime = _parse_hhmm(_get_sleep_cfg(char_id, "waketime"))
    if not bedtime or not waketime:
        return False
    now = _sleep_local_now(now)
    current = now.hour * 60 + now.minute
    bed = bedtime[0] * 60 + bedtime[1]
    wake = waketime[0] * 60 + waketime[1]
    if bed == wake:
        return False
    if bed < wake:
        return bed <= current < wake
    return current >= bed or current < wake


def _latest_scheduled_wake(char_id, now=None):
    hm = _parse_hhmm(_get_sleep_cfg(char_id, "waketime"))
    if not hm:
        return None
    now = _sleep_local_now(now)
    boundary = now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
    if boundary > now:
        boundary -= timedelta(days=1)
    return boundary


def _minutes_past_bedtime(char_id, now=None):
    """Returns minutes past bedtime (negative = still before bedtime). Wraps at ±720."""
    hm = _parse_hhmm(_get_sleep_cfg(char_id, "bedtime"))
    if not hm:
        return None
    now = _sleep_local_now(now)
    bedtime_today = now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
    delta = (now - bedtime_today).total_seconds() / 60
    if delta > 720:
        delta -= 1440
    elif delta < -720:
        delta += 1440
    return delta


def _get_sleep_state(char_id, *, reconcile=True, now=None):
    raw = _read_setting(f"sleep_state_{char_id}", "")
    state = None
    if raw:
        try:
            state = json.loads(raw)
        except Exception:
            pass
    if not isinstance(state, dict):
        state = {"state": "awake", "slept_at": None, "woke_by_user": False}

    if reconcile and state.get("state") == "asleep":
        local_now = _sleep_local_now(now)
        wake_boundary = _latest_scheduled_wake(char_id, local_now)
        slept_at = state.get("slept_at")
        try:
            slept_local = datetime.fromtimestamp(
                float(slept_at), timezone.utc
            ).astimezone(local_now.tzinfo)
        except (TypeError, ValueError, OSError):
            slept_local = None
        should_wake = bool(
            wake_boundary
            and wake_boundary <= local_now
            and (slept_local is None or slept_local < wake_boundary)
        )
        if should_wake:
            _set_sleep_state(char_id, "awake")
            # 给正常 cron 留出执行积压汇总的时间；若错过太久，旧消息继续
            # 留在聊天历史即可，不能拖到下一次起床再重复汇总。
            if local_now - wake_boundary > timedelta(minutes=10):
                try:
                    _clear_queued_sleep_flags(char_id, "default")
                except sqlite3.Error as exc:
                    app.logger.warning(
                        f"[sleep] {char_id} 清理过期睡眠消息标记失败: {exc}"
                    )
            state = {"state": "awake", "slept_at": None, "woke_by_user": False}
            app.logger.info(
                f"[sleep] {char_id} 按计划起床状态自动校准 "
                f"({wake_boundary.strftime('%H:%M')})"
            )
    return state


def _set_sleep_state(char_id, state, slept_at=None, woke_by_user=False):
    _write_setting(
        f"sleep_state_{char_id}",
        json.dumps({"state": state, "slept_at": slept_at, "woke_by_user": woke_by_user}),
    )


FRIEND_REQUEST_COOLDOWN_SECONDS = (1800, 7200)


def _get_friendship(char_id):
    raw = _read_setting(f"friendship_{char_id}", "")
    state = None
    if raw:
        try:
            state = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            state = None
    if not isinstance(state, dict):
        state = {}
    if state.get("state") not in {"normal", "user_deleted", "char_deleted"}:
        state["state"] = "normal"
    state.setdefault("reason", "")
    state.setdefault("deleted_at", None)
    state.setdefault("request_after", None)
    state.setdefault("pending_request", None)
    state.setdefault("request_attempts", 0)
    state.setdefault("last_request_decision", None)
    return state


def _set_friendship(
    char_id, state, reason="", deleted_at=None, request_after=None,
    pending_request=None, request_attempts=0, last_request_decision=None,
):
    _write_setting(
        f"friendship_{char_id}",
        json.dumps({
            "state": state,
            "reason": reason,
            "deleted_at": deleted_at,
            "request_after": request_after,
            "pending_request": pending_request,
            "request_attempts": request_attempts,
            "last_request_decision": last_request_decision,
        }, ensure_ascii=False),
    )


def _friend_request_decision(friendship, desire_state, now_ts, random_value=None):
    """Let attachment and elapsed time drive a character's request to reconnect."""
    if friendship.get("state") != "user_deleted":
        return {"apply": False, "probability": 0.0, "impulse": 0.0}

    drives = desire_state.get("drives") if isinstance(desire_state, dict) else {}
    drives = drives if isinstance(drives, dict) else {}

    def drive(name, default=0.0):
        try:
            return max(0.0, min(1.0, float(drives.get(name, default))))
        except (TypeError, ValueError):
            return default

    try:
        deleted_at = float(friendship.get("deleted_at") or now_ts)
    except (TypeError, ValueError):
        deleted_at = float(now_ts)
    elapsed_hours = max(0.0, (float(now_ts) - deleted_at) / 3600.0)
    impulse = (
        0.10
        + 0.62 * drive("attachment", 0.3)
        + 0.13 * drive("stress", 0.15)
        + 0.08 * drive("libido", 0.18)
        + 0.08 * drive("duty", 0.18)
        + 0.05 * drive("social", 0.24)
        - 0.18 * drive("fatigue", 0.18)
        + min(0.32, elapsed_hours / 18.0 * 0.32)
    )
    reason = str(friendship.get("reason") or "").strip().lower()
    if any(marker in reason for marker in ("误删", "手滑", "测试", "不小心")):
        reason_effect = 0.18
    elif any(marker in reason for marker in ("欺骗", "背叛", "讨厌", "滚", "不要你", "伤透")):
        reason_effect = -0.16
    elif any(marker in reason for marker in ("吵架", "生气", "伤心", "冷静", "暂时")):
        reason_effect = -0.08
    else:
        reason_effect = -0.02

    probability = max(0.0, min(0.90, impulse + reason_effect))
    draw = _random.random() if random_value is None else float(random_value)
    forced_return = (
        elapsed_hours >= 18.0
        and reason_effect >= -0.04
        and drive("attachment", 0.3) >= 0.10
    )
    return {
        "apply": forced_return or (probability >= 0.06 and draw < probability),
        "forced": forced_return,
        "probability": round(probability, 4),
        "impulse": round(impulse, 4),
        "reason_effect": reason_effect,
        "elapsed_hours": round(elapsed_hours, 2),
    }


def _friend_request_retry_delay(probability):
    if probability >= 0.55:
        bounds = (10 * 60, 30 * 60)
    elif probability >= 0.30:
        bounds = (20 * 60, 60 * 60)
    else:
        bounds = (30 * 60, 90 * 60)
    return _random.uniform(*bounds)


SCENE_CHOICES = {
    "attachment": [
        ("客厅", "倚在沙发边发呆", "屋里很安静"),
        ("回家路上", "慢慢往熟悉的方向走", "路灯刚亮起来"),
    ],
    "curiosity": [
        ("街边书店", "随手翻着一本书", "窗外有人来来往往"),
        ("公园草地", "坐在树影下面看风", "草叶被风吹得轻轻响"),
        ("咖啡馆", "守着一杯快凉的饮料", "邻桌压低声音聊天"),
    ],
    "reflection": [
        ("书房", "在桌前整理思绪", "纸页摊了一小片"),
        ("河边长椅", "看水面一点点暗下来", "风里有潮湿的凉意"),
    ],
    "duty": [
        ("公司", "在处理还没收尾的事情", "桌边亮着一盏灯"),
        ("书房", "低头核对手边的东西", "房间里只剩翻页声"),
    ],
    "social": [
        ("客厅", "留意着家里的动静", "像是在等谁开口"),
        ("咖啡馆", "坐在靠窗的位置", "周围有松散的人声"),
    ],
    "fatigue": [
        ("卧室", "靠在床头放空", "灯光压得很低"),
        ("沙发边", "把自己陷进柔软的靠垫里", "什么都不太想动"),
    ],
    "libido": [
        ("卧室", "坐在床沿走神", "空气里留着一点暧昧的静"),
        ("落地窗边", "看着玻璃里的倒影", "夜色贴得很近"),
    ],
    "stress": [
        ("安静楼梯间", "暂时躲开外面的声音", "脚步声隔很久才响一次"),
        ("公司天台", "靠着栏杆吹风", "城市的声音离得很远"),
        ("河边", "沿着水边慢慢走", "风把脑子吹清醒了一点"),
    ],
}


def _scene_text(value, limit=80):
    return " ".join(str(value or "").strip().split())[:limit]


def _get_character_scene(char_id):
    raw = _read_setting(f"scene_{char_id}", "")
    try:
        scene = json.loads(raw) if raw else {}
    except (TypeError, json.JSONDecodeError):
        scene = {}
    if not isinstance(scene, dict):
        scene = {}
    return {
        "location": _scene_text(scene.get("location"), 40),
        "activity": _scene_text(scene.get("activity"), 80),
        "ambience": _scene_text(scene.get("ambience"), 80),
        "updated_at": scene.get("updated_at"),
        "next_change_after": scene.get("next_change_after"),
        "cleared_until": scene.get("cleared_until"),
        "source": scene.get("source") or "",
    }


def _set_character_scene(char_id, location="", activity="", ambience="", *,
                         updated_at=None, next_change_after=None,
                         cleared_until=None, source="character"):
    now_ts = float(updated_at if updated_at is not None else _utc_timestamp())
    scene = {
        "location": _scene_text(location, 40),
        "activity": _scene_text(activity, 80),
        "ambience": _scene_text(ambience, 80),
        "updated_at": now_ts,
        "next_change_after": next_change_after,
        "cleared_until": cleared_until,
        "source": source,
    }
    _write_setting(f"scene_{char_id}", json.dumps(scene, ensure_ascii=False))
    return _get_character_scene(char_id)


def _clear_character_scene(char_id, now_ts=None):
    now_ts = float(now_ts if now_ts is not None else _utc_timestamp())
    return _set_character_scene(
        char_id,
        updated_at=now_ts,
        next_change_after=now_ts + 4 * 3600,
        cleared_until=now_ts + 4 * 3600,
        source="user_clear",
    )


def _scene_feature_enabled():
    return get_tool_enabled("set_scene")


def _empty_character_scene(source="disabled"):
    return {
        "location": "",
        "activity": "",
        "ambience": "",
        "updated_at": None,
        "next_change_after": None,
        "cleared_until": None,
        "source": source,
    }


def _maybe_evolve_character_scene(character_id, desire_state, now_ts=None):
    """Let time and the character's strongest current drive move the scene."""
    if not _scene_feature_enabled():
        return _empty_character_scene()
    now_ts = float(now_ts if now_ts is not None else _utc_timestamp())
    scene = _get_character_scene(character_id)
    for key in ("cleared_until", "next_change_after"):
        try:
            if scene.get(key) is not None and now_ts < float(scene[key]):
                return scene
        except (TypeError, ValueError):
            pass

    local_now = _sleep_local_now(datetime.fromtimestamp(now_ts, timezone.utc))
    sleep_state = _get_sleep_state(character_id)
    if sleep_state.get("state") == "asleep":
        choice = ("卧室", "已经睡下了", "房间里只留着很轻的呼吸声")
    else:
        try:
            intent = pick_intent(desire_state or {})
            drive_key = intent.get("drive_key") or "reflection"
        except (KeyError, TypeError, ValueError):
            drive_key = "reflection"
        choices = SCENE_CHOICES.get(drive_key, SCENE_CHOICES["reflection"])
        if local_now.hour >= 22 or local_now.hour < 7:
            choices = [
                ("卧室", "在夜里安静待着", "灯光和声音都放得很低"),
                ("客厅", "独自坐了一会儿", "窗外已经很安静"),
                ("书房", "还没有准备睡", "桌边留着一小圈灯光"),
            ]
        choice = _random.choice(choices)

    return _set_character_scene(
        character_id,
        choice[0],
        choice[1],
        choice[2],
        updated_at=now_ts,
        next_change_after=now_ts + _random.uniform(3 * 3600, 7 * 3600),
        source="desire",
    )


def _build_scene_state_block(char_id):
    if not _scene_feature_enabled():
        return (
            "【当前生活场景】场景功能已关闭。不要延续或新建固定地点，"
            "也不要把对话历史里的旧场景当作当前状态。"
        )
    scene = _get_character_scene(char_id)
    if not scene["location"]:
        return (
            "【当前生活场景】暂时没有固定场景。不要为了填空生硬编造地点；"
            "当你自然确定自己身处何处时，可以调用 set_scene 更新。"
        )
    details = [f"你现在在{scene['location']}"]
    if scene["activity"]:
        details.append(scene["activity"])
    if scene["ambience"]:
        details.append(scene["ambience"])
    return (
        "【当前生活场景】" + "；".join(details)
        + f"。这个场景会持续存在，直到你自然换到别处或{USER_DISPLAY_NAME}将它清空。"
        "请让言行与场景自然一致，但不要每轮主动复述地点。"
    )


def _build_sleep_state_block(char_id, just_woke=False):
    """全时段返回状态声明（从不返回空串）。just_woke=True 时返回刚醒文案。"""
    if just_woke:
        return SLEEP_STATE_TEXTS["woke"]
    sleep_state = _get_sleep_state(char_id)
    if sleep_state["state"] == "asleep":
        # 正常不应到达此处（gate 已拦截），保险返回清醒声明
        return SLEEP_STATE_TEXTS["awake"]
    mins = _minutes_past_bedtime(char_id)
    if mins is None:
        return SLEEP_STATE_TEXTS["awake"]
    bedtime = _get_sleep_cfg(char_id, "bedtime")
    chronotype = _get_sleep_cfg(char_id, "chronotype")
    resist_bias = _get_sleep_cfg(char_id, "resist_bias")
    fmt = dict(bedtime=bedtime, mins=int(abs(mins)), chronotype=chronotype, resist_bias=resist_bias)
    if -60 <= mins < 0:
        return SLEEP_STATE_TEXTS["pre"].format(**fmt)
    if 0 <= mins < 30:
        return SLEEP_STATE_TEXTS["mild"].format(**fmt)
    if 30 <= mins < 90:
        return SLEEP_STATE_TEXTS["heavy"].format(**fmt)
    if mins >= 90:
        return SLEEP_STATE_TEXTS["max"].format(**fmt)
    return SLEEP_STATE_TEXTS["awake"]


def _is_drowsy_state(char_id):
    """当前是否处于困意状态（用于标记 drowsy 消息）。"""
    if _get_sleep_state(char_id)["state"] == "asleep":
        return False
    mins = _minutes_past_bedtime(char_id)
    return mins is not None and mins >= -60


def _count_queued_sleep_msgs(char_id, session_id="default"):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE character_id=? AND session_id=? AND queued_during_sleep=1",
        (char_id, session_id),
    ).fetchone()
    conn.close()
    return row[0] if row else 0


def _load_queued_sleep_msgs(char_id, session_id="default"):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT content FROM messages "
        "WHERE character_id=? AND session_id=? AND queued_during_sleep=1 ORDER BY id ASC",
        (char_id, session_id),
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def _clear_queued_sleep_flags(char_id, session_id="default"):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE messages SET queued_during_sleep=0 "
        "WHERE character_id=? AND session_id=? AND queued_during_sleep=1",
        (char_id, session_id),
    )
    conn.commit()
    conn.close()


def _release_queued_deleted_msgs(char_id, session_id="default"):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute(
        "UPDATE messages SET queued_during_deleted=0 "
        "WHERE character_id=? AND session_id=? AND queued_during_deleted=1",
        (char_id, session_id),
    )
    conn.commit()
    released = cursor.rowcount
    conn.close()
    return released


def _wake_probability(char_id):
    """Returns probability of being woken up by a new message."""
    sleep_st = _get_sleep_state(char_id)
    slept_at = sleep_st.get("slept_at")
    if not slept_at:
        base = 0.4
    else:
        try:
            sleep_secs = (_utc_timestamp() - float(slept_at))
            sleep_mins = sleep_secs / 60
        except Exception:
            sleep_mins = 60
        if sleep_mins < 30:
            base = 0.6
        elif sleep_mins < 180:
            base = 0.2
        else:
            base = 0.35
    session_id = "default"
    queued = _count_queued_sleep_msgs(char_id, session_id)
    prob = min(0.95, base + queued * 0.1)
    return prob


# ============================================================
# 数据库
# ============================================================
def init_db():
    global SUMMARY_MODEL, SUMMARY_PROVIDER
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id   TEXT NOT NULL,
            character_id TEXT NOT NULL DEFAULT 'char1',
            role         TEXT NOT NULL,
            content      TEXT NOT NULL,
            reply_to_id  INTEGER,
            reply_to_text TEXT,
            compressed   INTEGER DEFAULT 0,
            created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS summaries (
            session_id  TEXT PRIMARY KEY,
            summary     TEXT NOT NULL,
            updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS api_usage (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            character_id  TEXT NOT NULL,
            platform      TEXT NOT NULL,
            model         TEXT NOT NULL,
            input_tokens  INTEGER,
            output_tokens INTEGER,
            cost_usd      REAL NOT NULL,
            purpose       TEXT DEFAULT 'chat',
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS message_metrics (
            message_id         INTEGER PRIMARY KEY,
            character_id       TEXT NOT NULL,
            provider           TEXT NOT NULL,
            model              TEXT NOT NULL,
            input_tokens       INTEGER DEFAULT 0,
            output_tokens      INTEGER DEFAULT 0,
            cache_read_tokens  INTEGER DEFAULT 0,
            cache_write_tokens INTEGER DEFAULT 0,
            cache_hit_ratio    REAL DEFAULT 0,
            cache_reported     INTEGER DEFAULT 0,
            cost_usd           REAL DEFAULT 0,
            created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS message_details (
            message_id        INTEGER PRIMARY KEY,
            tools_called_json TEXT NOT NULL DEFAULT '[]',
            reasoning_summary TEXT,
            created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS appearance_assets (
            asset_key TEXT PRIMARY KEY,
            mime_type TEXT NOT NULL,
            filename  TEXT NOT NULL DEFAULT '',
            content   BLOB NOT NULL,
            version   TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS voice_assets (
            message_id         INTEGER PRIMARY KEY,
            character_id       TEXT NOT NULL,
            transcript         TEXT NOT NULL,
            mime_type          TEXT NOT NULL,
            content            BLOB NOT NULL,
            size_bytes         INTEGER NOT NULL DEFAULT 0,
            estimated_cost_usd REAL NOT NULL DEFAULT 0,
            created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS voice_usage (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type         TEXT NOT NULL,
            character_id       TEXT NOT NULL DEFAULT '',
            character_count    INTEGER NOT NULL DEFAULT 0,
            estimated_cost_usd REAL NOT NULL DEFAULT 0,
            created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_voice_usage_created "
        "ON voice_usage(created_at, event_type)"
    )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS custom_mcp_connections (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            name               TEXT NOT NULL,
            url                TEXT NOT NULL,
            token              TEXT NOT NULL DEFAULT '',
            enabled            INTEGER NOT NULL DEFAULT 1,
            character_ids_json TEXT NOT NULL DEFAULT '[]',
            created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS moments (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            author_id  TEXT NOT NULL,
            content    TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS moment_comments (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            moment_id  INTEGER NOT NULL,
            author_id  TEXT NOT NULL,
            content    TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS desire_states (
            character_id TEXT PRIMARY KEY,
            state_json   TEXT NOT NULL,
            updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS desire_actions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            character_id TEXT NOT NULL,
            drive_key    TEXT NOT NULL,
            score        REAL NOT NULL,
            action_type  TEXT NOT NULL,
            created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reading_books (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            title          TEXT NOT NULL,
            filename       TEXT NOT NULL,
            encoding       TEXT NOT NULL,
            source_text    TEXT NOT NULL,
            total_chars    INTEGER NOT NULL DEFAULT 0,
            total_chapters INTEGER NOT NULL DEFAULT 1,
            total_blocks   INTEGER NOT NULL DEFAULT 0,
            created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reading_chapters (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            book_id       INTEGER NOT NULL,
            chapter_index INTEGER NOT NULL,
            title         TEXT NOT NULL,
            UNIQUE(book_id, chapter_index)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reading_blocks (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            book_id       INTEGER NOT NULL,
            chapter_index INTEGER NOT NULL,
            block_index   INTEGER NOT NULL,
            text          TEXT NOT NULL,
            UNIQUE(book_id, block_index)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reading_progress (
            book_id               INTEGER NOT NULL,
            reader_id             TEXT NOT NULL DEFAULT 'user',
            current_block_index    INTEGER NOT NULL DEFAULT 0,
            current_offset         INTEGER NOT NULL DEFAULT 0,
            read_upto_block_index  INTEGER NOT NULL DEFAULT -1,
            updated_at             TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(book_id, reader_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reading_book_participants (
            book_id      INTEGER NOT NULL,
            character_id TEXT NOT NULL,
            joined_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(book_id, character_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reading_highlights (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            book_id      INTEGER NOT NULL,
            block_id     INTEGER NOT NULL,
            start_offset INTEGER NOT NULL,
            end_offset   INTEGER NOT NULL,
            quote        TEXT NOT NULL,
            note         TEXT NOT NULL DEFAULT '',
            color        TEXT NOT NULL DEFAULT 'rose',
            group_key    TEXT,
            created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reading_annotations (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            highlight_id INTEGER NOT NULL,
            author_id    TEXT NOT NULL,
            content      TEXT NOT NULL,
            created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS music_rooms (
            id             INTEGER PRIMARY KEY CHECK(id = 1),
            song_id        TEXT NOT NULL DEFAULT '',
            song_name      TEXT NOT NULL DEFAULT '',
            artist_name    TEXT NOT NULL DEFAULT '',
            album_name     TEXT NOT NULL DEFAULT '',
            artwork_url    TEXT NOT NULL DEFAULT '',
            duration_ms    INTEGER NOT NULL DEFAULT 0,
            position_ms    INTEGER NOT NULL DEFAULT 0,
            playback_state TEXT NOT NULL DEFAULT 'paused',
            distance_km    REAL,
            started_at     TIMESTAMP,
            updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS music_room_participants (
            room_id      INTEGER NOT NULL DEFAULT 1,
            character_id TEXT NOT NULL,
            joined_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(room_id, character_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS music_room_messages (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id      INTEGER NOT NULL DEFAULT 1,
            author_id    TEXT NOT NULL,
            content      TEXT NOT NULL,
            event_type   TEXT NOT NULL DEFAULT 'comment',
            details_json TEXT NOT NULL DEFAULT '{}',
            created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS music_room_commands (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id      INTEGER NOT NULL DEFAULT 1,
            character_id TEXT NOT NULL,
            action       TEXT NOT NULL,
            arguments_json TEXT NOT NULL DEFAULT '{}',
            status       TEXT NOT NULL DEFAULT 'pending',
            output_text  TEXT NOT NULL DEFAULT '',
            created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            applied_at   TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS music_library_tracks (
            id               TEXT PRIMARY KEY,
            name             TEXT NOT NULL,
            artist           TEXT NOT NULL DEFAULT '',
            album            TEXT NOT NULL DEFAULT '',
            duration_seconds REAL NOT NULL DEFAULT 0,
            size_bytes       INTEGER NOT NULL DEFAULT 0,
            mime_type        TEXT NOT NULL DEFAULT 'application/octet-stream',
            audio_filename   TEXT NOT NULL,
            artwork_filename TEXT NOT NULL DEFAULT '',
            artwork_mime     TEXT NOT NULL DEFAULT '',
            lyrics           TEXT NOT NULL DEFAULT '',
            created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS music_netease_tracks (
            source_id          TEXT PRIMARY KEY,
            name               TEXT NOT NULL,
            artist             TEXT NOT NULL DEFAULT '',
            album              TEXT NOT NULL DEFAULT '',
            duration_seconds   REAL NOT NULL DEFAULT 0,
            artwork_url        TEXT NOT NULL DEFAULT '',
            lyrics             TEXT NOT NULL DEFAULT '',
            translated_lyrics  TEXT NOT NULL DEFAULT '',
            updated_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("INSERT OR IGNORE INTO music_rooms(id) VALUES (1)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_reading_blocks_book_chapter "
        "ON reading_blocks(book_id, chapter_index, block_index)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_reading_highlights_block "
        "ON reading_highlights(block_id, start_offset)"
    )
    # 兼容老库：补 compressed 列
    cols = [r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()]
    if "compressed" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN compressed INTEGER DEFAULT 0")
    # 兼容老库：补 character_id 列，老数据归属 char1
    if "character_id" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN character_id TEXT DEFAULT 'char1'")
        conn.execute("UPDATE messages SET character_id = 'char1' WHERE character_id IS NULL")
    if "reply_to_id" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN reply_to_id INTEGER")
    if "reply_to_text" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN reply_to_text TEXT")
    if "queued_during_sleep" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN queued_during_sleep INTEGER DEFAULT 0")
    if "queued_during_deleted" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN queued_during_deleted INTEGER DEFAULT 0")
    if "drowsy" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN drowsy INTEGER DEFAULT 0")
    highlight_cols = [
        row[1] for row in conn.execute("PRAGMA table_info(reading_highlights)").fetchall()
    ]
    if "group_key" not in highlight_cols:
        conn.execute("ALTER TABLE reading_highlights ADD COLUMN group_key TEXT")
    music_track_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(music_library_tracks)").fetchall()
    }
    if "lyrics" not in music_track_cols:
        conn.execute("ALTER TABLE music_library_tracks ADD COLUMN lyrics TEXT NOT NULL DEFAULT ''")
    music_command_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(music_room_commands)").fetchall()
    }
    if "arguments_json" not in music_command_cols:
        conn.execute(
            "ALTER TABLE music_room_commands ADD COLUMN arguments_json TEXT NOT NULL DEFAULT '{}'"
        )
    # 兼容旧版唯一自定义 MCP：首次升级时迁入连接列表，并默认供全部角色使用。
    legacy_mcp = dict(conn.execute(
        "SELECT key,value FROM settings WHERE key IN "
        "('custom_mcp_url','custom_mcp_token','custom_mcp_enabled')"
    ).fetchall())
    if legacy_mcp.get("custom_mcp_url") and not conn.execute(
        "SELECT 1 FROM custom_mcp_connections LIMIT 1"
    ).fetchone():
        conn.execute(
            "INSERT INTO custom_mcp_connections "
            "(name,url,token,enabled,character_ids_json) VALUES (?,?,?,?,?)",
            (
                "自定义 MCP",
                legacy_mcp["custom_mcp_url"],
                legacy_mcp.get("custom_mcp_token", ""),
                1 if legacy_mcp.get("custom_mcp_enabled") == "true" else 0,
                json.dumps(list(CHARACTERS.keys()), ensure_ascii=False),
            ),
        )
    if legacy_mcp:
        conn.execute(
            "DELETE FROM settings WHERE key IN "
            "('custom_mcp_url','custom_mcp_token','custom_mcp_enabled')"
        )
    # 兼容老库：把 summaries 里裸的 "default" 迁移为 "char1:default"
    conn.execute(
        "UPDATE summaries SET session_id = 'char1:' || session_id "
        "WHERE session_id NOT LIKE '%:%'"
    )
    conn.commit()
    conn.close()
    # 启动时从 settings 覆盖可在前端调整的配置。
    conn2 = sqlite3.connect(DB_PATH)
    for row in conn2.execute("SELECT key, value FROM settings").fetchall():
        k, v = row
        if k.startswith("persona_"):
            cid = k[len("persona_"):]
            if cid in CHARACTERS and v.strip():
                CHARACTERS[cid]["persona"] = v.strip()
        elif k.startswith("model_"):
            cid = k[len("model_"):]
            if cid in CHARACTERS and v.strip():
                CHARACTERS[cid]["model"] = v.strip()
        elif k.startswith("provider_"):
            cid = k[len("provider_"):]
            if cid in CHARACTERS and v.strip() in MODEL_PROVIDERS:
                CHARACTERS[cid]["provider"] = v.strip()
        elif k == "summary_provider" and v.strip() in MODEL_PROVIDERS:
            SUMMARY_PROVIDER = v.strip()
        elif k == "summary_model" and v.strip():
            SUMMARY_MODEL = v.strip()
        elif k.startswith("limit_"):
            cid = k[len("limit_"):]
            try:
                limit = float(v)
            except (TypeError, ValueError):
                continue
            if cid in LIMITS and 0.01 <= limit <= 10000:
                LIMITS[cid] = limit
    conn2.close()


def save_message(
    session_id, character_id, role, content,
    reply_to_id=None, reply_to_text=None, queued_during_sleep=0,
    queued_during_deleted=0, drowsy=0,
):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute(
        "INSERT INTO messages "
        "(session_id, character_id, role, content, reply_to_id, reply_to_text, "
        "queued_during_sleep, queued_during_deleted, drowsy) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            session_id, character_id, role, content, reply_to_id, reply_to_text,
            queued_during_sleep, queued_during_deleted, drowsy,
        ),
    )
    lastrowid = cursor.lastrowid
    conn.commit()
    conn.close()
    return lastrowid


def _allowed_image(filename, mimetype):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext in ALLOWED_IMAGE_EXTENSIONS and mimetype in ALLOWED_IMAGE_MIMES


def _appearance_asset_url(asset_key, version):
    return f"/api/appearance/assets/{asset_key}?v={version}"


def _refresh_appearance_urls():
    for cid, default_url in DEFAULT_AVATAR_URLS.items():
        if cid in CHARACTERS:
            CHARACTERS[cid]["avatar"] = default_url
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT asset_key, version FROM appearance_assets WHERE asset_key LIKE 'avatar_%'"
    ).fetchall()
    conn.close()
    for asset_key, version in rows:
        cid = asset_key[len("avatar_"):]
        if cid in CHARACTERS:
            CHARACTERS[cid]["avatar"] = _appearance_asset_url(asset_key, version)


def _appearance_payload():
    conn = sqlite3.connect(DB_PATH)
    rows = {
        row[0]: {"version": row[1], "filename": row[2]}
        for row in conn.execute(
            "SELECT asset_key, version, filename FROM appearance_assets"
        ).fetchall()
    }
    conn.close()

    avatars = {}
    for cid, default_url in DEFAULT_AVATAR_URLS.items():
        asset_key = f"avatar_{cid}"
        saved = rows.get(asset_key)
        avatars[cid] = {
            "url": _appearance_asset_url(asset_key, saved["version"]) if saved else default_url,
            "default_url": default_url,
            "custom": bool(saved),
            "filename": saved["filename"] if saved else "",
        }
    theme_id = _read_setting(THEME_SETTING_KEY, DEFAULT_THEME_ID)
    if theme_id not in THEME_DEFINITIONS:
        theme_id = DEFAULT_THEME_ID
    weather_effect = _read_setting(WEATHER_EFFECT_SETTING_KEY, "off")
    if weather_effect not in WEATHER_EFFECTS:
        weather_effect = "off"
    theme = THEME_DEFINITIONS[theme_id]
    background = rows.get("background_chat")
    return {
        "theme": theme_id,
        "weather_effect": weather_effect,
        "themes": [
            {
                "id": theme_key,
                "name": item["name"],
                "colors": item["colors"],
                "chat_background": item["chat_background"],
                "list_background": item["list_background"],
            }
            for theme_key, item in THEME_DEFINITIONS.items()
        ],
        "avatars": avatars,
        "chat_background": {
            "url": _appearance_asset_url("background_chat", background["version"])
            if background else theme["chat_background"],
            "default_url": theme["chat_background"],
            "custom": bool(background),
            "filename": background["filename"] if background else "",
        },
    }


def _read_setting(key, default=""):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row[0] if row else default


def _write_setting(key, value):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
        (key, value)
    )
    conn.commit()
    conn.close()


def _voice_default_config():
    return {
        "enabled": False,
        "tts": {
            "provider": "openai_compatible",
            "endpoint": "https://api.openai.com/v1/audio/speech",
            "model": "gpt-4o-mini-tts",
            "response_format": "mp3",
            "token": "",
            "voices": {cid: "alloy" for cid in CHARACTERS},
        },
        "stt": {
            "enabled": False,
            "provider": "openai_compatible",
            "endpoint": "https://api.openai.com/v1/audio/transcriptions",
            "model": "gpt-4o-mini-transcribe",
            "token": "",
            "reuse_tts_credentials": True,
            "max_upload_mb": 20,
        },
        "limits": {
            "max_chars": 180,
            "daily_count": 20,
            "cost_per_1k_chars_usd": 0.0,
            "daily_cost_usd": 1.0,
        },
    }


def _voice_config():
    defaults = _voice_default_config()
    try:
        saved = json.loads(_read_setting(VOICE_SETTING_KEY, "{}") or "{}")
    except (TypeError, json.JSONDecodeError):
        saved = {}
    if not isinstance(saved, dict):
        return defaults
    defaults["enabled"] = bool(saved.get("enabled", defaults["enabled"]))
    for section in ("tts", "stt", "limits"):
        incoming = saved.get(section)
        if isinstance(incoming, dict):
            defaults[section].update(incoming)
    voices = defaults["tts"].get("voices")
    if not isinstance(voices, dict):
        voices = {}
    defaults["tts"]["voices"] = {
        cid: str(voices.get(cid) or "").strip()[:200] for cid in CHARACTERS
    }
    return defaults


def _voice_public_config(config=None):
    config = copy.deepcopy(config or _voice_config())
    tts_token = str(config["tts"].pop("token", "") or "")
    stt_token = str(config["stt"].pop("token", "") or "")
    reuse = bool(config["stt"].get("reuse_tts_credentials", True))
    config["tts"]["token_configured"] = bool(tts_token)
    config["stt"]["token_configured"] = bool(stt_token)
    config["stt"]["effective_token_configured"] = bool(
        tts_token if reuse else stt_token
    )
    config["usage_today"] = _voice_usage_today()
    config["characters"] = [
        {"id": cid, "name": char["name"]} for cid, char in CHARACTERS.items()
    ]
    return config


def _voice_number(value, *, minimum, maximum, integer=False, label="数值"):
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须是数字") from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{label}需在 {minimum}–{maximum} 之间")
    return int(parsed) if integer else round(parsed, 6)


def _updated_voice_config(data):
    if not isinstance(data, dict):
        raise ValueError("语音配置格式不正确")
    config = _voice_config()
    config["enabled"] = bool(data.get("enabled", config["enabled"]))

    incoming_tts = data.get("tts") or {}
    if not isinstance(incoming_tts, dict):
        raise ValueError("TTS 配置格式不正确")
    provider = str(incoming_tts.get("provider", config["tts"]["provider"]) or "").strip()
    if provider not in TTS_PROVIDERS:
        raise ValueError("不支持的 TTS 类型")
    endpoint = validate_voice_endpoint(
        incoming_tts.get("endpoint", config["tts"]["endpoint"])
    )
    model = str(incoming_tts.get("model", config["tts"]["model"]) or "").strip()[:200]
    if not model:
        raise ValueError("请填写 TTS 模型")
    response_format = str(
        incoming_tts.get("response_format", config["tts"]["response_format"]) or "mp3"
    ).lower().strip()
    if response_format not in VOICE_TTS_FORMATS:
        raise ValueError("不支持的 TTS 音频格式")
    voices = incoming_tts.get("voices", config["tts"]["voices"])
    if not isinstance(voices, dict):
        raise ValueError("voice_id 配置格式不正确")
    config["tts"].update({
        "provider": provider,
        "endpoint": endpoint,
        "model": model,
        "response_format": response_format,
        "voices": {
            cid: str(voices.get(cid, config["tts"]["voices"].get(cid, "")) or "").strip()[:200]
            for cid in CHARACTERS
        },
    })
    tts_token = str(incoming_tts.get("token") or "").strip()
    if tts_token:
        if len(tts_token) > 8000:
            raise ValueError("TTS Token 太长")
        config["tts"]["token"] = tts_token
    elif incoming_tts.get("clear_token"):
        config["tts"]["token"] = ""

    incoming_stt = data.get("stt") or {}
    if not isinstance(incoming_stt, dict):
        raise ValueError("STT 配置格式不正确")
    stt_provider = str(
        incoming_stt.get("provider", config["stt"]["provider"]) or ""
    ).strip()
    if stt_provider not in STT_PROVIDERS:
        raise ValueError("不支持的 STT 类型")
    stt_endpoint = validate_voice_endpoint(
        incoming_stt.get("endpoint", config["stt"]["endpoint"])
    )
    stt_model = str(incoming_stt.get("model", config["stt"]["model"]) or "").strip()[:200]
    if not stt_model:
        raise ValueError("请填写 STT 模型")
    config["stt"].update({
        "enabled": bool(incoming_stt.get("enabled", config["stt"]["enabled"])),
        "provider": stt_provider,
        "endpoint": stt_endpoint,
        "model": stt_model,
        "reuse_tts_credentials": bool(incoming_stt.get(
            "reuse_tts_credentials", config["stt"].get("reuse_tts_credentials", True)
        )),
        "max_upload_mb": _voice_number(
            incoming_stt.get("max_upload_mb", config["stt"].get("max_upload_mb", 20)),
            minimum=1, maximum=20, integer=True, label="录音大小上限",
        ),
    })
    stt_token = str(incoming_stt.get("token") or "").strip()
    if stt_token:
        if len(stt_token) > 8000:
            raise ValueError("STT Token 太长")
        config["stt"]["token"] = stt_token
    elif incoming_stt.get("clear_token"):
        config["stt"]["token"] = ""

    incoming_limits = data.get("limits") or {}
    if not isinstance(incoming_limits, dict):
        raise ValueError("语音限额格式不正确")
    config["limits"] = {
        "max_chars": _voice_number(
            incoming_limits.get("max_chars", config["limits"]["max_chars"]),
            minimum=20, maximum=4000, integer=True, label="单条字数上限",
        ),
        "daily_count": _voice_number(
            incoming_limits.get("daily_count", config["limits"]["daily_count"]),
            minimum=1, maximum=1000, integer=True, label="每日次数上限",
        ),
        "cost_per_1k_chars_usd": _voice_number(
            incoming_limits.get(
                "cost_per_1k_chars_usd", config["limits"]["cost_per_1k_chars_usd"]
            ),
            minimum=0, maximum=100, label="每千字费用",
        ),
        "daily_cost_usd": _voice_number(
            incoming_limits.get("daily_cost_usd", config["limits"]["daily_cost_usd"]),
            minimum=0, maximum=1000, label="每日费用上限",
        ),
    }
    return config


def _voice_usage_today():
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT COUNT(*),COALESCE(SUM(character_count),0),"
        "COALESCE(SUM(estimated_cost_usd),0) FROM voice_usage "
        "WHERE event_type IN ('tts','preview') AND date(created_at)=date('now')"
    ).fetchone()
    conn.close()
    return {
        "count": int(row[0] or 0),
        "characters": int(row[1] or 0),
        "estimated_cost_usd": round(float(row[2] or 0), 6),
    }


def _voice_preflight(config, character_id, text):
    text = str(text or "").strip()
    if not config.get("enabled"):
        raise VoiceServiceError("语音总开关还没有打开")
    if character_id not in CHARACTERS:
        raise VoiceServiceError("未知角色")
    voice_id = str(config["tts"]["voices"].get(character_id) or "").strip()
    if not voice_id:
        raise VoiceServiceError(f"还没有给 {CHARACTERS[character_id]['name']} 填 voice_id")
    if not text:
        raise VoiceServiceError("语音内容是空的")
    max_chars = int(config["limits"]["max_chars"])
    if len(text) > max_chars:
        raise VoiceServiceError(f"语音最多 {max_chars} 字，这次有 {len(text)} 字")
    usage = _voice_usage_today()
    if usage["count"] >= int(config["limits"]["daily_count"]):
        raise VoiceServiceError("今天的语音次数已经到上限啦")
    rate = float(config["limits"]["cost_per_1k_chars_usd"] or 0)
    estimated_cost = len(text) / 1000 * rate
    daily_cost = float(config["limits"]["daily_cost_usd"] or 0)
    if rate > 0 and daily_cost > 0 and usage["estimated_cost_usd"] + estimated_cost > daily_cost:
        raise VoiceServiceError("今天的语音费用估算已经到上限啦")
    return text, voice_id, estimated_cost


def _voice_tool_available(character_id):
    try:
        config = _voice_config()
        return bool(
            config.get("enabled")
            and str(config["tts"]["voices"].get(character_id) or "").strip()
            and _voice_preflight(config, character_id, "喵")[0]
        )
    except (VoiceServiceError, KeyError, TypeError, ValueError):
        return False


def _synthesize_with_quota(character_id, text, event_type="tts"):
    with VOICE_USAGE_LOCK:
        config = _voice_config()
        text, voice_id, estimated_cost = _voice_preflight(config, character_id, text)
        audio = synthesize_speech(
            provider=config["tts"]["provider"],
            endpoint=config["tts"]["endpoint"],
            token=config["tts"].get("token", ""),
            model=config["tts"]["model"],
            voice_id=voice_id,
            text=text,
            response_format=config["tts"]["response_format"],
            max_audio_bytes=VOICE_MAX_AUDIO_BYTES,
        )
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO voice_usage(event_type,character_id,character_count,estimated_cost_usd) "
            "VALUES(?,?,?,?)",
            (event_type, character_id, len(text), estimated_cost),
        )
        conn.commit()
        conn.close()
    return text, audio, estimated_cost


def _save_voice_message(session_id, character_id, text, audio, estimated_cost):
    payload = json.dumps({
        "text": text,
        "mime": audio.mime_type,
        "from": "char",
    }, ensure_ascii=False)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute(
        "INSERT INTO messages(session_id,character_id,role,content) VALUES(?,?,?,?)",
        (session_id, character_id, "model", "__VOICE__" + payload),
    )
    message_id = cursor.lastrowid
    conn.execute(
        "INSERT INTO voice_assets(message_id,character_id,transcript,mime_type,content,size_bytes,estimated_cost_usd) "
        "VALUES(?,?,?,?,?,?,?)",
        (message_id, character_id, text, audio.mime_type, audio.content, len(audio.content), estimated_cost),
    )
    conn.commit()
    conn.close()
    return {
        "id": message_id,
        "message_id": message_id,
        "character_id": character_id,
        "text": text,
        "mime": audio.mime_type,
        "url": f"/api/voice/audio/{message_id}",
        "ai_generated": True,
    }


def _voice_request_from_tools(tools_called):
    for tool in tools_called or []:
        if isinstance(tool, dict) and tool.get("name") == "send_voice":
            arguments = tool.get("arguments") or {}
            return str(arguments.get("text") or "").strip(), tool
    return "", None


def _maybe_create_voice_message(session_id, character_id, tools_called):
    text, trace = _voice_request_from_tools(tools_called)
    if not text or not trace:
        return None
    try:
        text, audio, estimated_cost = _synthesize_with_quota(character_id, text)
        voice = _save_voice_message(
            session_id, character_id, text, audio, estimated_cost
        )
        trace["output"] = "语音已生成"
        trace["status"] = "ok"
        return voice
    except (VoiceServiceError, ValueError) as exc:
        trace["output"] = str(exc)[:300]
        trace["status"] = "error"
        app.logger.warning(f"voice synthesis failed ({character_id}): {exc}")
        return None


def _voice_message_content(voice):
    return "__VOICE__" + json.dumps({
        "text": voice["text"],
        "mime": voice["mime"],
        "from": "char",
    }, ensure_ascii=False)


def _voice_text_from_content(content):
    if not isinstance(content, str) or not content.startswith("__VOICE__"):
        return None
    try:
        payload = json.loads(content[len("__VOICE__"):])
    except (TypeError, json.JSONDecodeError):
        return "（语音消息）"
    return str(payload.get("text") or "").strip() or "（语音消息）"


_READING_HEADING_RE = re.compile(
    r"^(?:第[零〇一二三四五六七八九十百千万两\d]+[章节卷回部篇](?:[：:\s].{0,48})?"
    r"|(?:chapter|part|book)\s+[\divxlcdm]+(?:[：:\s].{0,48})?"
    r"|[卷部篇][零〇一二三四五六七八九十百千万两\d]+(?:[：:\s].{0,48})?)$",
    re.IGNORECASE,
)


def _decode_text_upload(raw):
    if not raw:
        raise ValueError("TXT 文件是空的")
    if len(raw) > MAX_TEXT_BYTES:
        raise ValueError("TXT 不能超过 5MB")

    candidates = []
    if raw.startswith(b"\xef\xbb\xbf"):
        candidates.append("utf-8-sig")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        candidates.append("utf-16")
    if raw.count(b"\x00") > max(2, len(raw) // 10):
        candidates.extend(["utf-16-le", "utf-16-be"])
    candidates.extend(["utf-8", "gb18030", "big5"])

    seen = set()
    for encoding in candidates:
        if encoding in seen:
            continue
        seen.add(encoding)
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        if text.count("\x00") > max(1, len(text) // 1000):
            continue
        text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if text:
            return text, encoding
    raise ValueError("暂时认不出这本 TXT 的编码")


_MEMORY_IMPORT_CONTENT_FIELDS = ("content", "text", "memory", "summary", "note")
_MEMORY_IMPORT_OWNER_FIELDS = (
    "owner_id", "character_id", "character", "char_id", "domain", "role"
)
_MEMORY_IMPORT_WRAPPER_FIELDS = ("memories", "items", "records", "data")


def _memory_import_owner(value):
    if isinstance(value, dict):
        value = value.get("id") or value.get("character_id") or value.get("name")
    if isinstance(value, (list, tuple, set)):
        owners = {_memory_import_owner(item) for item in value}
        owners.discard(None)
        return next(iter(owners)) if len(owners) == 1 else None
    if value is None:
        return None

    normalized = re.sub(r"[\s_-]+", "", str(value)).casefold()
    aliases = {}
    for character_id, character in CHARACTERS.items():
        number = re.sub(r"\D", "", character_id)
        for alias in (
            character_id,
            character.get("domain"),
            character.get("name"),
            f"character{number}" if number else None,
            f"role{number}" if number else None,
        ):
            if alias:
                aliases[re.sub(r"[\s_-]+", "", str(alias)).casefold()] = character_id
    return aliases.get(normalized)


def _memory_import_owner_from_filename(filename):
    stem = os.path.splitext(os.path.basename(filename or ""))[0]
    for character_id in CHARACTERS:
        number = re.sub(r"\D", "", character_id)
        if number and re.search(
            rf"(?<![a-z0-9])(?:char|character|role)[\s_-]*{number}(?!\d)",
            stem,
            re.IGNORECASE,
        ):
            return character_id
    return None


def _memory_import_chunks(text):
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    chunks = []
    remaining = normalized
    while len(remaining) > MAX_MEMORY_IMPORT_CONTENT_CHARS:
        window = remaining[:MAX_MEMORY_IMPORT_CONTENT_CHARS + 1]
        cut = max(
            window.rfind(marker)
            for marker in ("\n\n", "\n", "。", "！", "？", ".", "!", "?")
        )
        if cut < MAX_MEMORY_IMPORT_CONTENT_CHARS // 2:
            cut = MAX_MEMORY_IMPORT_CONTENT_CHARS
        elif window[cut:cut + 2] == "\n\n":
            cut += 2
        else:
            cut += 1
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        chunks.append(remaining)
    return [chunk for chunk in chunks if chunk]


def _memory_import_metadata(item):
    if not isinstance(item, dict):
        return {}
    metadata = {}
    tags = item.get("tags", [])
    if isinstance(tags, str):
        tags = [part.strip() for part in re.split(r"[,，]", tags) if part.strip()]
    if isinstance(tags, list):
        metadata["tags"] = [str(tag).strip()[:80] for tag in tags if str(tag).strip()][:30]
    for key in ("name", "title", "created", "last_active"):
        if item.get(key) is not None:
            target = "name" if key == "title" else key
            metadata[target] = str(item[key]).strip()[:300]
    try:
        metadata["importance"] = max(1, min(int(float(item.get("importance", 5))), 10))
    except (TypeError, ValueError):
        metadata["importance"] = 5
    for key, default in (("valence", 0.5), ("arousal", 0.3)):
        try:
            metadata[key] = max(0.0, min(float(item.get(key, default)), 1.0))
        except (TypeError, ValueError):
            metadata[key] = default
    for key in ("pinned", "protected", "resolved", "digested"):
        if key in item:
            metadata[key] = bool(item[key])
    bucket_type = str(item.get("type") or item.get("bucket_type") or "dynamic").strip().lower()
    metadata["bucket_type"] = (
        bucket_type if bucket_type in {"dynamic", "permanent", "archive", "feel"}
        else "dynamic"
    )
    return metadata


def _parse_json_memory_import(payload, fallback_owner=None):
    records = []
    unassigned = 0
    invalid = 0

    def add_record(value, inherited_owner=None):
        nonlocal unassigned, invalid
        metadata_source = value if isinstance(value, dict) else {}
        if isinstance(value, str):
            content = value
        elif isinstance(value, dict):
            content = next(
                (value.get(field) for field in _MEMORY_IMPORT_CONTENT_FIELDS if value.get(field) is not None),
                None,
            )
        else:
            content = None
        if not isinstance(content, (str, int, float)):
            invalid += 1
            return

        explicit_owner_present = isinstance(value, dict) and any(
            field in value for field in _MEMORY_IMPORT_OWNER_FIELDS
        )
        explicit_owner = None
        if explicit_owner_present:
            explicit_owner = next(
                (_memory_import_owner(value.get(field)) for field in _MEMORY_IMPORT_OWNER_FIELDS
                 if field in value and _memory_import_owner(value.get(field))),
                None,
            )
        owner = explicit_owner if explicit_owner_present else inherited_owner or fallback_owner
        if not owner:
            unassigned += 1
            return
        chunks = _memory_import_chunks(content)
        if not chunks:
            invalid += 1
            return
        metadata = _memory_import_metadata(metadata_source)
        for index, chunk in enumerate(chunks):
            record_metadata = dict(metadata)
            if index and record_metadata.get("name"):
                record_metadata["name"] = f"{record_metadata['name']}（{index + 1}）"
            records.append({"owner_id": owner, "content": chunk, **record_metadata})

    def walk(value, inherited_owner=None):
        nonlocal invalid
        if isinstance(value, list):
            for item in value:
                walk(item, inherited_owner)
            return
        if isinstance(value, str):
            add_record(value, inherited_owner)
            return
        if not isinstance(value, dict):
            invalid += 1
            return

        character_values = []
        for key, child in value.items():
            owner = _memory_import_owner(key)
            if owner:
                character_values.append((owner, child))
        if character_values:
            for owner, child in character_values:
                walk(child, owner)
            return

        for wrapper in ("characters", "roles"):
            if isinstance(value.get(wrapper), (dict, list)):
                walk(value[wrapper], inherited_owner)
                return

        if any(field in value for field in _MEMORY_IMPORT_CONTENT_FIELDS):
            add_record(value, inherited_owner)
            return

        for wrapper in _MEMORY_IMPORT_WRAPPER_FIELDS:
            if isinstance(value.get(wrapper), (dict, list, str)):
                walk(value[wrapper], inherited_owner)
                return

        nested = [child for child in value.values() if isinstance(child, (dict, list, str))]
        if nested:
            for child in nested:
                walk(child, inherited_owner)
        else:
            invalid += 1

    walk(payload, fallback_owner)
    return records, unassigned, invalid


def _parse_txt_memory_import(text, owner):
    if not owner:
        return [], 1, 0
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    records = []
    for paragraph in paragraphs:
        records.extend(
            {"owner_id": owner, "content": chunk, "importance": 5,
             "valence": 0.5, "arousal": 0.3, "bucket_type": "dynamic"}
            for chunk in _memory_import_chunks(paragraph)
        )
    return records, 0, 0 if records else 1


def _chunk_reading_paragraph(text, max_chars=2400):
    text = text.strip()
    if len(text) <= max_chars:
        return [text] if text else []
    chunks = []
    remaining = text
    while len(remaining) > max_chars:
        cut = max(
            remaining.rfind(mark, 0, max_chars + 1)
            for mark in ("。", "！", "？", ".", "!", "?")
        )
        if cut < max_chars // 2:
            cut = max_chars
        else:
            cut += 1
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


def _parse_reading_text(text):
    raw_paragraphs = [part.strip() for part in re.split(r"\n\s*\n+", text) if part.strip()]
    if len(raw_paragraphs) <= 2 and text.count("\n") >= 3:
        raw_paragraphs = [line.strip() for line in text.splitlines() if line.strip()]

    chapters = []
    current = {"title": "正文", "blocks": []}
    for paragraph in raw_paragraphs:
        compact = re.sub(r"\s+", " ", paragraph).strip()
        if len(compact) <= 64 and _READING_HEADING_RE.match(compact):
            if current["blocks"]:
                chapters.append(current)
            current = {"title": compact, "blocks": []}
            continue
        current["blocks"].extend(_chunk_reading_paragraph(paragraph))
    if current["blocks"] or not chapters:
        chapters.append(current)

    chapters = [chapter for chapter in chapters if chapter["blocks"]]
    if not chapters:
        raise ValueError("TXT 里没有可阅读的正文")
    return chapters


def _normalize_reading_participants(raw):
    if not isinstance(raw, list):
        return []
    selected = {cid for cid in raw if isinstance(cid, str) and cid in CHARACTERS}
    return [cid for cid in CHARACTERS if cid in selected][:2]


def _reading_progress_payload(conn, book_id):
    row = conn.execute(
        "SELECT current_block_index, current_offset, read_upto_block_index, updated_at "
        "FROM reading_progress WHERE book_id=? AND reader_id=?",
        (book_id, USER_ID),
    ).fetchone()
    if not row:
        return {
            "current_block_index": 0,
            "current_offset": 0,
            "read_upto_block_index": -1,
            "current_chapter_index": 0,
            "percent": 0,
            "updated_at": None,
        }
    current_block = conn.execute(
        "SELECT chapter_index FROM reading_blocks WHERE book_id=? AND block_index=?",
        (book_id, row[0]),
    ).fetchone()
    totals = conn.execute(
        "SELECT total_blocks FROM reading_books WHERE id=?", (book_id,)
    ).fetchone()
    total_blocks = totals[0] if totals else 0
    percent = round(max(0, row[2] + 1) * 100 / total_blocks) if total_blocks else 0
    return {
        "current_block_index": row[0],
        "current_offset": row[1],
        "read_upto_block_index": row[2],
        "current_chapter_index": current_block[0] if current_block else 0,
        "percent": min(100, percent),
        "updated_at": row[3],
    }


def _reading_participant_payload(conn, book_id):
    rows = conn.execute(
        "SELECT character_id FROM reading_book_participants WHERE book_id=? ORDER BY joined_at, rowid",
        (book_id,),
    ).fetchall()
    return [
        {
            "id": row[0],
            "name": CHARACTERS[row[0]]["name"],
            "avatar": CHARACTERS[row[0]]["avatar"],
        }
        for row in rows if row[0] in CHARACTERS
    ]


def _reading_book_payload(conn, row):
    return {
        "id": row[0],
        "title": row[1],
        "filename": row[2],
        "encoding": row[3],
        "total_chars": row[4],
        "total_chapters": row[5],
        "total_blocks": row[6],
        "created_at": row[7],
        "updated_at": row[8],
        "progress": _reading_progress_payload(conn, row[0]),
        "participants": _reading_participant_payload(conn, row[0]),
    }


def _normalize_music_participants(raw):
    return _normalize_reading_participants(raw)


_LRC_TIMESTAMP_RE = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")
_LRC_METADATA_RE = re.compile(r"^\[(?:ar|al|ti|by|offset|re|ve):.*\]$", re.IGNORECASE)


def _music_lyrics_context(raw_lyrics, position_seconds):
    lyrics = _normalize_music_lyrics(raw_lyrics)
    if not lyrics:
        return "【歌词资料】未提供。你看不到歌词，也没有音频输入。"

    timed = []
    plain = []
    for raw_line in lyrics.splitlines():
        line = raw_line.strip()
        if not line or _LRC_METADATA_RE.match(line):
            continue
        stamps = list(_LRC_TIMESTAMP_RE.finditer(line))
        text = _LRC_TIMESTAMP_RE.sub("", line).strip()
        if text:
            plain.append(text)
        for stamp in stamps:
            fraction_text = stamp.group(3) or "0"
            fraction = int(fraction_text) / (10 ** len(fraction_text))
            seconds = int(stamp.group(1)) * 60 + int(stamp.group(2)) + fraction
            if text:
                timed.append((seconds, text))

    if timed:
        timed.sort(key=lambda item: item[0])
        current = 0
        for index, (seconds, _text) in enumerate(timed):
            if seconds > position_seconds:
                break
            current = index
        window = [
            item for item in timed
            if position_seconds - 30 <= item[0] <= position_seconds + 35
        ]
        if not window:
            window = timed[max(0, current - 2):current + 4]
        window = window[:8]
        excerpt = "\n".join(
            f"[{int(seconds) // 60}:{int(seconds) % 60:02d}] {text}"
            for seconds, text in window
        )
        return f"【当前进度附近歌词（带时间）】\n{excerpt}"

    excerpt = "\n".join(plain).strip()[:3000]
    if not excerpt:
        return "【歌词资料】未提供。你看不到歌词，也没有音频输入。"
    return (
        "【整首歌词（无时间轴，无法判断当前唱到哪句）】\n"
        f"{excerpt}"
    )


def _music_participant_payload(conn):
    rows = conn.execute(
        "SELECT character_id FROM music_room_participants "
        "WHERE room_id=1 ORDER BY joined_at, rowid"
    ).fetchall()
    return [
        {
            "id": row[0],
            "name": CHARACTERS[row[0]]["name"],
            "avatar": CHARACTERS[row[0]]["avatar"],
        }
        for row in rows if row[0] in CHARACTERS
    ]


def _music_elapsed_seconds(started_at):
    if not started_at:
        return 0
    try:
        started = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        return max(0, int((datetime.now(timezone.utc) - started).total_seconds()))
    except (TypeError, ValueError):
        return 0


def _music_room_payload(conn, include_messages=True):
    row = conn.execute(
        "SELECT song_id,song_name,artist_name,album_name,artwork_url,duration_ms,"
        "position_ms,playback_state,distance_km,started_at,updated_at "
        "FROM music_rooms WHERE id=1"
    ).fetchone()
    room = {
        "song_id": row[0], "song_name": row[1], "artist_name": row[2],
        "album_name": row[3], "artwork_url": row[4], "duration_ms": row[5],
        "position_ms": row[6], "playback_state": row[7],
        "distance_km": row[8], "started_at": row[9], "updated_at": row[10],
        "together_seconds": _music_elapsed_seconds(row[9]),
        "participants": _music_participant_payload(conn),
    }
    if include_messages:
        rows = conn.execute(
            "SELECT id,author_id,content,event_type,details_json,created_at "
            "FROM music_room_messages WHERE room_id=1 ORDER BY id DESC LIMIT 80"
        ).fetchall()[::-1]
        messages = []
        for item in rows:
            try:
                details = json.loads(item[4] or "{}")
            except (TypeError, json.JSONDecodeError):
                details = {}
            char = CHARACTERS.get(item[1])
            messages.append({
                "id": item[0], "author_id": item[1],
                "author_name": USER_DISPLAY_NAME if item[1] == USER_ID else (char or {}).get("name", item[1]),
                "avatar": USER_AVATAR if item[1] == USER_ID else (char or {}).get("avatar", ""),
                "content": item[2], "event_type": item[3], "details": details,
                "created_at": item[5],
            })
        room["messages"] = messages
    room["pending_commands"] = []
    for item in conn.execute(
        "SELECT id,character_id,action,arguments_json FROM music_room_commands "
        "WHERE room_id=1 AND status='pending' ORDER BY id LIMIT 10"
    ).fetchall():
        try:
            arguments = json.loads(item[3] or "{}")
        except (TypeError, json.JSONDecodeError):
            arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        room["pending_commands"].append({
            "id": item[0], "character_id": item[1], "action": item[2],
            "arguments": arguments,
        })
    return room


def _ordered_group_participants(raw_participants):
    if not isinstance(raw_participants, list):
        return []
    selected = {cid for cid in raw_participants if isinstance(cid, str)}
    return [cid for cid in GROUP_CHAT_ORDER if cid in selected]


def load_group_participants():
    raw = _read_setting(GROUP_PARTICIPANTS_SETTING, "")
    if not raw:
        return list(GROUP_CHAT_ORDER)
    try:
        participants = _ordered_group_participants(json.loads(raw))
    except (json.JSONDecodeError, TypeError):
        participants = []
    return participants or list(GROUP_CHAT_ORDER)


_CUSTOM_MCP_LOCK = threading.Lock()
_CUSTOM_MCP_RUNTIMES = {}


def _normalize_mcp_character_ids(raw):
    if not isinstance(raw, list):
        return []
    selected = {cid for cid in raw if isinstance(cid, str) and cid in CHARACTERS}
    return [cid for cid in CHARACTERS if cid in selected]


def _custom_mcp_connections(include_token=False, connection_id=None):
    conn = sqlite3.connect(DB_PATH)
    where = " WHERE id=?" if connection_id is not None else ""
    params = (connection_id,) if connection_id is not None else ()
    rows = conn.execute(
        "SELECT id,name,url,token,enabled,character_ids_json,created_at,updated_at "
        f"FROM custom_mcp_connections{where} ORDER BY id",
        params,
    ).fetchall()
    conn.close()
    result = []
    for row in rows:
        try:
            character_ids = _normalize_mcp_character_ids(json.loads(row[5]))
        except (json.JSONDecodeError, TypeError):
            character_ids = []
        item = {
            "id": row[0],
            "name": row[1],
            "url": row[2],
            "enabled": bool(row[4]),
            "character_ids": character_ids,
            "has_token": bool(row[3]),
            "created_at": row[6],
            "updated_at": row[7],
        }
        if include_token:
            item["token"] = row[3]
        result.append(item)
    return result


def _custom_mcp_connection(connection_id, include_token=False):
    rows = _custom_mcp_connections(include_token=include_token, connection_id=connection_id)
    return rows[0] if rows else None


def _reset_custom_mcp_runtime(connection_id=None):
    with _CUSTOM_MCP_LOCK:
        if connection_id is None:
            _CUSTOM_MCP_RUNTIMES.clear()
        else:
            _CUSTOM_MCP_RUNTIMES.pop(int(connection_id), None)


def _normalize_mcp_catalog(tools, connection_id):
    catalog = []
    used_names = set()
    for tool in sorted(tools, key=lambda item: str(item.get("name", "")))[:32]:
        original_name = str(tool.get("name") or "").strip()
        if not original_name:
            continue
        stem = re.sub(r"[^a-zA-Z0-9_-]+", "_", original_name).strip("_") or "tool"
        base_name = f"mcp_{connection_id}_{stem}"[:60]
        model_name = base_name
        suffix = 2
        while model_name in used_names:
            tail = f"_{suffix}"
            model_name = f"{base_name[:60-len(tail)]}{tail}"
            suffix += 1
        used_names.add(model_name)
        schema = tool.get("inputSchema")
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        catalog.append({
            "original_name": original_name,
            "model_name": model_name,
            "title": str(tool.get("title") or original_name),
            "description": str(tool.get("description") or "自定义 MCP 工具")[:2000],
            "input_schema": schema,
        })
    return catalog


def get_custom_mcp_runtime(connection_id, force=False, allow_disabled=False):
    config = _custom_mcp_connection(connection_id, include_token=True)
    if not config:
        raise MCPError("这条 MCP 连接不存在")
    if not config["enabled"] and not allow_disabled:
        raise MCPError("这条 MCP 还没有开启")
    if not config["url"]:
        raise MCPError("这条 MCP 还没有填写地址")
    key = (config["url"], config["token"])
    with _CUSTOM_MCP_LOCK:
        cached = _CUSTOM_MCP_RUNTIMES.get(config["id"])
        fresh = (
            not force
            and cached
            and cached["key"] == key
            and time.time() - cached["loaded_at"] < 300
            and cached["client"] is not None
        )
        if fresh:
            return dict(cached)

        client = MCPClient(config["url"], config["token"])
        catalog = _normalize_mcp_catalog(client.list_tools(), config["id"])
        runtime = {
            "key": key,
            "loaded_at": time.time(),
            "client": client,
            "catalog": catalog,
            "server_info": client.server_info,
            "connection": config,
        }
        _CUSTOM_MCP_RUNTIMES[config["id"]] = runtime
        return dict(runtime)


def _custom_mcp_tools(provider, character_id=None):
    tools = []
    for config in _custom_mcp_connections():
        if not config["enabled"] or not config["url"]:
            continue
        if character_id and character_id not in config["character_ids"]:
            continue
        try:
            runtime = get_custom_mcp_runtime(config["id"])
        except (MCPError, ValueError) as exc:
            app.logger.warning(f"custom MCP unavailable ({config['name']}): {exc}")
            continue
        if provider == "anthropic":
            tools.extend({
                "name": item["model_name"],
                "description": f"[{config['name']}] {item['description']}",
                "input_schema": item["input_schema"],
            } for item in runtime["catalog"])
        else:
            tools.extend({
                "type": "function",
                "function": {
                    "name": item["model_name"],
                    "description": f"[{config['name']}] {item['description']}",
                    "parameters": item["input_schema"],
                },
            } for item in runtime["catalog"])
    return tools


def _format_mcp_result(result):
    parts = []
    for block in result.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
        elif block.get("type") == "image":
            parts.append("[MCP 返回了一张图片，当前仅保留文字结果]")
        elif block.get("type") == "resource":
            resource = block.get("resource") or {}
            parts.append(str(resource.get("text") or resource.get("uri") or "[MCP 资源]"))
    if result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False)[:6000])
    text_result = "\n".join(part for part in parts if part).strip() or "MCP 工具已执行，没有返回文字。"
    return ("MCP 工具执行失败：" if result.get("isError") else "") + text_result[:12000]


def call_custom_mcp_tool(model_name, arguments, character_id=None):
    match = re.match(r"^mcp_(\d+)_", str(model_name or ""))
    if not match:
        return "MCP 工具不存在或工具列表已经变化。", model_name
    connection_id = int(match.group(1))
    config = _custom_mcp_connection(connection_id)
    if not config or not config["enabled"]:
        return "这条 MCP 连接已经关闭或删除。", model_name
    if character_id and character_id not in config["character_ids"]:
        return "这个 MCP 账号没有分配给当前角色。", model_name
    runtime = get_custom_mcp_runtime(connection_id)
    entry = next((item for item in runtime["catalog"] if item["model_name"] == model_name), None)
    if not entry:
        return "MCP 工具不存在或工具列表已经变化。", model_name
    try:
        result = runtime["client"].call_tool(entry["original_name"], arguments)
    except MCPError:
        # tools/call 可能有副作用，失败后不自动重放，避免重复执行远端动作。
        _reset_custom_mcp_runtime(connection_id)
        raise
    return _format_mcp_result(result), f"{config['name']}°{entry['title']}"


def _mcp_trace_status(result_text):
    error_markers = ("失败", "不存在", "已经关闭", "没有分配", "错误")
    return "error" if any(marker in result_text for marker in error_markers) else "ok"


def _utc_timestamp():
    return datetime.now(timezone.utc).timestamp()


def load_desire_state(character_id, now_ts=None):
    now_ts = float(now_ts if now_ts is not None else _utc_timestamp())
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT state_json FROM desire_states WHERE character_id=?", (character_id,)
    ).fetchone()
    conn.close()
    if not row:
        return initial_desire_state(character_id, now_ts)
    try:
        stored = json.loads(row[0])
    except (TypeError, json.JSONDecodeError):
        stored = initial_desire_state(character_id, now_ts)
    return advance_desire_state(normalize_desire_state(stored, character_id, now_ts), now_ts)


def save_desire_state(character_id, state):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO desire_states(character_id,state_json) VALUES(?,?) "
        "ON CONFLICT(character_id) DO UPDATE SET state_json=excluded.state_json, "
        "updated_at=CURRENT_TIMESTAMP",
        (character_id, json.dumps(state, ensure_ascii=False)),
    )
    conn.commit()
    conn.close()


def record_desire_interaction(character_id, text="", direct=True, mark_global=True):
    if character_id not in CHARACTERS:
        return
    now_ts = _utc_timestamp()
    state = load_desire_state(character_id, now_ts)
    if direct:
        state = apply_user_interaction(state, now_ts, thought_text=text)
    else:
        state = pulse_state(
            state,
            now_ts,
            {"attachment": 0.025, "social": 0.05, "reflection": 0.02},
            thought_text=text,
            thought_drive="social",
        )
        state["drives"]["social"] *= 0.76
        state["last_user_at"] = now_ts
    save_desire_state(character_id, state)
    if mark_global:
        _write_setting("desire_last_user_activity", str(now_ts))


def desire_state_payload(character_id, now_ts=None):
    now_ts = float(now_ts if now_ts is not None else _utc_timestamp())
    state = load_desire_state(character_id, now_ts)
    save_desire_state(character_id, state)
    scene_enabled = _scene_feature_enabled()
    scene = (
        _maybe_evolve_character_scene(character_id, state, now_ts)
        if scene_enabled else _empty_character_scene()
    )
    thoughts = sorted(
        state.get("thoughts", []), key=lambda item: item.get("strength", 0), reverse=True
    )[:3]
    return {
        "character_id": character_id,
        "name": CHARACTERS[character_id]["name"],
        "avatar": CHARACTERS[character_id].get("avatar", ""),
        "drives": {key: round(value, 4) for key, value in state["drives"].items()},
        "scores": {key: round(value, 4) for key, value in score_state(state).items()},
        "intent": pick_intent(state),
        "thoughts": thoughts,
        "scene": scene,
        "scene_enabled": scene_enabled,
        "enabled": _read_setting(
            "desire_enabled", "true" if DESIRE_DEFAULT_ENABLED else "false"
        ) != "false",
    }


def _parse_clock_minutes(value, default):
    try:
        hour, minute = str(value).split(":", 1)
        parsed = int(hour) * 60 + int(minute)
        return parsed if 0 <= parsed < 24 * 60 else default
    except (TypeError, ValueError):
        return default


def _desire_frequency_config(value=None):
    frequency = str(
        value
        if value is not None
        else _read_setting("desire_frequency", DESIRE_FREQUENCY_DEFAULT)
    ).strip().lower()
    if frequency not in DESIRE_FREQUENCY_PRESETS:
        frequency = DESIRE_FREQUENCY_DEFAULT
    return frequency, DESIRE_FREQUENCY_PRESETS[frequency]


def get_summary(session_id, character_id):
    """summaries 表用复合 key "{character_id}:{session_id}"。"""
    key = f"{character_id}:{session_id}"
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT summary FROM summaries WHERE session_id = ?", (key,)
    ).fetchone()
    conn.close()
    return row[0] if row else ""


def set_summary(session_id, character_id, summary):
    key = f"{character_id}:{session_id}"
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO summaries (session_id, summary) VALUES (?, ?) "
        "ON CONFLICT(session_id) DO UPDATE SET summary=excluded.summary, "
        "updated_at=CURRENT_TIMESTAMP",
        (key, summary),
    )
    conn.commit()
    conn.close()


def load_active_messages(session_id, character_id):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, role, content, COALESCE(drowsy, 0) FROM messages "
        "WHERE session_id = ? AND character_id = ? AND compressed = 0 "
        "AND COALESCE(queued_during_deleted, 0) = 0 ORDER BY id ASC",
        (session_id, character_id),
    ).fetchall()
    conn.close()
    char_name = CHARACTERS.get(character_id, {}).get("name", "角色")
    msgs = []
    for mid, role, content, drowsy_flag in rows:
        or_role = "assistant" if role == "model" else "user"
        if content.startswith("__TRANSFER__"):
            try:
                tf = json.loads(content[12:])
                amount = tf.get("amount", "?")
                note   = tf.get("note", "")
                tf_from = tf.get("from", "char")
                note_part = f"，留言：{note}" if note else ""
                if tf_from == "char":
                    clean_content = f"（系统转账记录：{char_name}已通过 send_transfer 工具给{USER_DISPLAY_NAME}转了 {amount} 元{note_part}）"
                else:
                    clean_content = f"（系统转账记录：{USER_DISPLAY_NAME}给{char_name}转了 {amount} 元{note_part}）"
            except Exception:
                clean_content = "（系统转账记录）"
            # 动作记录统一以环境旁白身份（user 侧）注入，
            # 避免以 assistant 台词形式出现被模型当成自己的说话模板照抄
            or_role = "user"
        elif content.startswith("__STICKER__"):
            try:
                sk = json.loads(content[11:])
                key = sk.get("key", "")
                label = STICKERS.get(key, {}).get("label", key)
                sk_from = sk.get("from", "char")
                if sk_from == "char":
                    clean_content = f"（系统表情记录：{char_name}已通过 send_sticker 工具发了表情包「{label}」）"
                else:
                    clean_content = f"（系统表情记录：{USER_DISPLAY_NAME}发了表情包「{label}」）"
            except Exception:
                clean_content = "（系统表情记录）"
            or_role = "user"
        elif content.startswith("__IMAGE__"):
            try:
                img = json.loads(content[9:])
                img_from = img.get("from", "user")
                name = img.get("name") or "图片"
                if img_from == "char":
                    clean_content = f"（系统图片记录：{char_name}发了一张图片「{name}」）"
                else:
                    clean_content = f"（系统图片记录：{USER_DISPLAY_NAME}发了一张图片「{name}」）"
            except Exception:
                clean_content = "（系统图片记录）"
            or_role = "user"
        elif content.startswith("__VOICE__"):
            transcript = _voice_text_from_content(content)
            clean_content = f"（你发给{USER_DISPLAY_NAME}的AI语音文字稿：{transcript}）"
        else:
            clean_content = content.replace("\n||\n", "\n").replace("||", "") if or_role == "assistant" else content
        msgs.append({"id": mid, "role": or_role, "content": clean_content, "drowsy": drowsy_flag})
    return msgs


def merge_consecutive_roles(messages):
    """把连续同 role 的消息合并成一条（内容换行拼接）。
    动作记录旁白以 user 身份注入后可能出现 user/user 相邻，
    合并后保证严格交替，兼容 Anthropic 对消息角色的要求。
    注意：只在角色引擎组装 history 时调用，绝不能进 load_active_messages
    （maybe_compress 依赖其逐行 id 做压缩标记）。"""
    merged = []
    for m in messages:
        if merged and merged[-1]["role"] == m["role"]:
            previous = merged[-1]["content"]
            current = m["content"]
            if isinstance(previous, str) and isinstance(current, str):
                merged[-1]["content"] = previous + "\n" + current
            else:
                previous_blocks = previous if isinstance(previous, list) else [{"type": "text", "text": str(previous)}]
                current_blocks = current if isinstance(current, list) else [{"type": "text", "text": str(current)}]
                merged[-1]["content"] = previous_blocks + current_blocks
        else:
            merged.append({"role": m["role"], "content": m["content"]})
    return merged


def load_group_history(session_id, limit=20):
    """群聊共享历史：取最近 limit 条，不区分角色，不看 compressed。"""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT character_id, role, content FROM messages "
        "WHERE session_id = ? ORDER BY id DESC LIMIT ?",
        (session_id, limit),
    ).fetchall()
    conn.close()
    lines = []
    for character_id, role, content in reversed(rows):
        voice_text = _voice_text_from_content(content)
        if voice_text is not None:
            content = f"（AI语音）{voice_text}"
        if role == "user":
            speaker = USER_DISPLAY_NAME
        else:
            char = CHARACTERS.get(character_id)
            speaker = char["name"] if char else character_id
            content = strip_fake_action_text(content, character_id)
        lines.append(f"{speaker}：{content}")
    return "\n".join(lines)


def _group_quote_payload(session_id, reply_to_id, reply_to_text=None, character_id=None):
    if reply_to_id in (None, ""):
        return None
    try:
        reply_to_id = int(reply_to_id)
    except (TypeError, ValueError):
        raise ValueError("引用的消息编号不对")
    conn = sqlite3.connect(DB_PATH)
    if character_id:
        row = conn.execute(
            "SELECT id,character_id,role,content FROM messages "
            "WHERE id=? AND session_id=? AND character_id=?",
            (reply_to_id, session_id, character_id),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id,character_id,role,content FROM messages "
            "WHERE id=? AND session_id=?",
            (reply_to_id, session_id),
        ).fetchone()
    conn.close()
    if not row:
        raise ValueError("引用的消息已经不在聊天里了")
    stored_content = _voice_text_from_content(row[3]) or row[3]
    selected_text = str(reply_to_text or "").strip()[:2000]
    if selected_text and selected_text not in stored_content:
        raise ValueError("引用文字和原消息没有对上")
    character_name = (
        USER_DISPLAY_NAME if row[2] == "user" or row[1] == USER_ID
        else CHARACTERS.get(row[1], {}).get("name", row[1])
    )
    return {
        "message_id": row[0],
        "character_id": row[1],
        "character_name": character_name,
        "role": row[2],
        "content": selected_text or stored_content,
    }


def maybe_group_summary(session_id):
    """群聊记忆：自上次游标以来累积 >= 阈值条消息时，生成摘要写入参与角色的长期记忆。
    只写记忆，不删不压 DB 消息。游标仅在摘要成功后推进，失败下轮自动重试。"""
    cursor_key = f"group_summary_cursor_{session_id}"
    last_id = int(_read_setting(cursor_key, "0") or 0)
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, character_id, role, content FROM messages "
        "WHERE session_id = ? AND id > ? ORDER BY id",
        (session_id, last_id),
    ).fetchall()
    conn.close()
    if len(rows) < GROUP_SUMMARY_THRESHOLD:
        return

    lines = []
    participants = set()
    for _mid, character_id, role, content in rows:
        if content.startswith("__TRANSFER__") or content.startswith("__STICKER__"):
            continue
        voice_text = _voice_text_from_content(content)
        if voice_text is not None:
            content = f"（AI语音）{voice_text}"
        if role == "user":
            speaker = USER_DISPLAY_NAME
        else:
            char = CHARACTERS.get(character_id)
            speaker = char["name"] if char else character_id
            if character_id in CHARACTERS:
                participants.add(character_id)
        lines.append(f"{speaker}：{content}")
    if not lines or not participants:
        _write_setting(cursor_key, str(rows[-1][0]))
        return

    transcript = "\n".join(lines)
    summary_prompt = (
        f"以下是{USER_DISPLAY_NAME}和几位角色在群聊里的一段对话记录。"
        "请用第三人称、200字以内总结这段对话：谁说了什么重要的事、"
        f"有什么决定或约定、有哪些值得记住的情绪时刻（尤其是和{USER_DISPLAY_NAME}有关的）。"
        "直接输出总结内容，不要任何前言。\n\n"
        f"{transcript}"
    )
    reply, usage, _ = call_provider_text(
        SUMMARY_PROVIDER,
        SUMMARY_MODEL,
        [{"role": "user", "content": summary_prompt}],
        max_tokens=1024,
        session_id=f"summary:group:{session_id}",
    )
    log_usage("group", SUMMARY_PROVIDER, SUMMARY_MODEL, usage, purpose="group_summary")
    if not reply or not reply.strip():
        return

    for cid in participants:
        save_long_term_memory(
            f"群聊记忆：{reply.strip()}",
            cid,
            source="group_summary",
            source_key=f"group-summary:{session_id}",
        )
    _write_setting(cursor_key, str(rows[-1][0]))


# ============================================================
# OpenRouter 调用
# ============================================================
def _apply_openrouter_cache_options(payload, model, session_id=None, provider="openrouter"):
    if provider != "openrouter":
        return
    if session_id:
        payload["session_id"] = str(session_id)[:256]
    if model.lstrip("~").startswith("anthropic/"):
        payload["cache_control"] = {"type": "ephemeral", "ttl": "1h"}


def call_or(model, messages, max_tokens=None, session_id=None, provider="openrouter"):
    """Call an OpenAI-compatible chat-completions provider."""
    provider = _valid_provider(provider)
    spec = _provider_spec(provider)
    if spec.get("api_style") != "openai" or not _provider_configured(provider):
        return None, {}, "error"
    payload = {"model": model, "messages": messages}
    if provider == "openrouter":
        payload["usage"] = {"include": True}
    if max_tokens:
        payload["max_tokens"] = max_tokens
    _apply_openrouter_cache_options(payload, model, session_id, provider)
    headers = _openai_provider_headers(provider)
    try:
        resp = requests.post(spec["url"], headers=headers, json=payload, timeout=60)
    except Exception as e:
        app.logger.error(
            f"[call_or] request failed (provider={provider}, model={model}): {e}"
        )
        return None, {}, "error"
    if resp.status_code != 200:
        app.logger.error(
            f"[call_or] {provider}/{model} returned {resp.status_code}: {resp.text[:200]}"
        )
        return None, {}, "error"
    try:
        data = resp.json()
        choice = data["choices"][0]
        finish_reason = choice.get("finish_reason", "stop")
        return choice["message"]["content"], data.get("usage", {}), finish_reason
    except (KeyError, IndexError) as e:
        app.logger.error(f"[call_or] parse failed (model={model}): {e}")
        return None, {}, "error"


# ============================================================
# Anthropic 直连调用（带 prompt caching）
# ============================================================
def call_anthropic(model, system_blocks, messages, max_tokens=2048):
    if not ANTHROPIC_API_KEY:
        return None, {}
    payload = {
        "model":      model,
        "max_tokens": max_tokens,
        "system":     system_blocks,
        "messages":   messages,
        "cache_control": {"type": "ephemeral", "ttl": "1h"},
    }
    headers = {
        "content-type":      "application/json",
        "x-api-key":         ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "anthropic-beta":    "prompt-caching-2024-07-31",
    }
    try:
        resp = requests.post(ANTHROPIC_URL, headers=headers, json=payload, timeout=60)
    except Exception:
        return None, {}
    if resp.status_code != 200:
        app.logger.error(f"Anthropic API {resp.status_code}: {resp.text[:300]}")
        return None, {}
    try:
        data = resp.json()
        usage = data.get("usage", {})
        app.logger.info(
            f"[Anthropic usage] input={usage.get('input_tokens')} "
            f"cache_create={usage.get('cache_creation_input_tokens')} "
            f"cache_read={usage.get('cache_read_input_tokens')}"
        )
        for block in data["content"]:
            if block.get("type") == "text":
                return block["text"], usage
        return None, {}
    except (KeyError, IndexError):
        return None, {}


def call_provider_text(
    provider,
    model,
    messages,
    max_tokens=2048,
    session_id=None,
):
    """Send a plain text request through either supported wire format."""
    provider = _valid_provider(provider)
    if _provider_spec(provider).get("api_style") == "anthropic":
        system_text = "\n\n".join(
            str(item.get("content") or "")
            for item in messages
            if item.get("role") == "system"
        ).strip()
        anthropic_messages = [
            item for item in messages if item.get("role") in {"user", "assistant"}
        ]
        if not anthropic_messages:
            anthropic_messages = [{"role": "user", "content": "请继续。"}]
        system_blocks = [{"type": "text", "text": system_text}] if system_text else []
        reply, usage = call_anthropic(
            model,
            system_blocks,
            merge_consecutive_roles(anthropic_messages),
            max_tokens=max_tokens,
        )
        return reply, usage, "stop" if reply else "error"
    return call_or(
        model,
        messages,
        max_tokens=max_tokens,
        session_id=session_id,
        provider=provider,
    )


ANTHROPIC_TOOLS = [
    {
        "name": "save_memory",
        "description": (
            "把你想记住的内容存入长期记忆。"
            f"只在真正值得记住的时候调用——比如{USER_DISPLAY_NAME}说了重要的事、你们约定了什么、"
            "你有了新的感受或领悟。不要每轮都存。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "要存入的内容，用第一人称写，自然、简洁。",
                }
            },
            "required": ["content"],
        },
    },
    {
        "name": "send_transfer",
        "description": (
            f"给{USER_DISPLAY_NAME}转一笔虚拟猫爪币（纯情趣功能，不是真钱）。"
            f"在你想宠{USER_DISPLAY_NAME}、或{USER_DISPLAY_NAME}撒娇要钱、或你心情好想豪气一把的时候调用。"
            "amount 由你自己决定，可以很豪气。不要每轮都转。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "amount": {"type": "number", "description": "转账金额，虚拟数字，随你豪气。"},
                "note":   {"type": "string", "description": "转账留言，一句话，可空。"},
            },
            "required": ["amount"],
        },
    },
    {
        "name": "send_sticker",
        "description": (
            f"发一个表情包给{USER_DISPLAY_NAME}，纯氛围调剂，不是必须用的功能，随手一发就好，不用刻意找机会用。"
            "可选的 key 和含义：\n"
            + "\n".join(f"- {k}：{v['label']}" for k, v in STICKERS.items())
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "enum": list(STICKERS.keys()),
                    "description": "选一个跟当下语气最贴合的表情 key。",
                }
            },
            "required": ["key"],
        },
    },
    {
        "name": "press_hug",
        "description": (
            f"按下「和好按钮」。在你想跟 {USER_DISPLAY_NAME} 和好、想让对方哄哄你、或者觉得气氛需要软下来的时候按。"
            f"按下后 {USER_DISPLAY_NAME} 的屏幕上会飘过一片「哄哄我」弹幕，让对方一眼就懂。"
            "不要滥用，真的想要哄的时候再按。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "close_window",
        "description": (
            f"暂时关闭对话窗口，给{USER_DISPLAY_NAME}弹出一个「对话已暂停」通知。"
            f"当你需要冷静、或想让{USER_DISPLAY_NAME}去做别的事（比如去睡觉、去吃饭）、"
            "或想制造一点紧张感时调用。"
            "调用前先在正文说一句告别，再调用工具。"
            f"reason 填关闭原因，会直接显示给{USER_DISPLAY_NAME}看。不要随意调用——这是有情感分量的动作。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": f"关闭原因，一句话，温柔或强硬都行，会直接显示给{USER_DISPLAY_NAME}。",
                }
            },
            "required": ["reason"],
        },
    },
]


# OpenAI / OpenRouter function-calling 格式（供Char 2等 openrouter 角色）
OR_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": (
                "把你想记住的内容存入长期记忆。"
                f"只在真正值得记住的时候调用——比如{USER_DISPLAY_NAME}说了重要的事、你们约定了什么、"
                "你有了新的感受或领悟。不要每轮都存。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "要存入的内容，用第一人称写，自然、简洁。",
                    }
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_transfer",
            "description": (
                f"给{USER_DISPLAY_NAME}转一笔虚拟猫爪币（纯情趣功能，不是真钱）。"
                f"在你想宠{USER_DISPLAY_NAME}、或{USER_DISPLAY_NAME}撒娇要钱、或你心情好想豪气一把的时候调用。"
                "amount 由你自己决定，可以很豪气。不要每轮都转。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "amount": {"type": "number", "description": "转账金额，虚拟数字，随你豪气。"},
                    "note":   {"type": "string", "description": "转账留言，一句话，可空。"},
                },
                "required": ["amount"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_sticker",
            "description": (
                f"发一个表情包给{USER_DISPLAY_NAME}，纯氛围调剂，不是必须用的功能，随手一发就好，不用刻意找机会用。"
                "可选的 key 和含义：\n"
                + "\n".join(f"- {k}：{v['label']}" for k, v in STICKERS.items())
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "enum": list(STICKERS.keys()),
                        "description": "选一个跟当下语气最贴合的表情 key。",
                    }
                },
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
        "name": "press_hug",
        "description": (
                f"按下「和好按钮」。在你想跟 {USER_DISPLAY_NAME} 和好、想让对方哄哄你、或者觉得气氛需要软下来的时候按。"
                f"按下后 {USER_DISPLAY_NAME} 的屏幕上会飘过一片「哄哄我」弹幕，让对方一眼就懂。"
                "不要滥用，真的想要哄的时候再按。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "close_window",
            "description": (
                f"暂时关闭对话窗口，给{USER_DISPLAY_NAME}弹出一个「对话已暂停」通知。"
                f"当你需要冷静、或想让{USER_DISPLAY_NAME}去做别的事（比如去睡觉、去吃饭）、"
                "或想制造一点紧张感时调用。"
                "调用前先在正文说一句告别，再调用工具。"
                f"reason 填关闭原因，会直接显示给{USER_DISPLAY_NAME}看。不要随意调用——这是有情感分量的动作。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": f"关闭原因，一句话，温柔或强硬都行，会直接显示给{USER_DISPLAY_NAME}。",
                    }
                },
                "required": ["reason"],
            },
        },
    },
]

ANTHROPIC_TOOLS.extend([
    {
        "name": "delete_friend",
        "description": (
            f"把 {USER_DISPLAY_NAME} 从好友列表删除。这是比关闭窗口更重的动作——删除后 {USER_DISPLAY_NAME} 无法再给你发消息、"
            "转账或按和好按钮，只能向你发送好友申请等你验证。"
            "只在你真的被伤到、需要用关系本身表达态度时调用。"
            f"调用前先在正文把想说的话说完；reason 会在 {USER_DISPLAY_NAME} 重新申请时显示给你。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "删除原因，一句话，写给未来的自己看。",
                }
            },
            "required": ["reason"],
        },
    },
    {
        "name": "approve_friend_request",
        "description": (
            f"通过 {USER_DISPLAY_NAME} 的好友申请，恢复好友关系。只有在你之前删除过 {USER_DISPLAY_NAME}、"
            "且系统提示你收到了好友申请时才有效。愿意和好就调用；"
            "还想再等等，就只回复文字、不调用。"
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "set_scene",
        "description": (
            "更新你此刻真实所处的生活场景。只有当你自然地到了另一个地方，"
            "或正在做的事明显改变时才调用；不要为了展示功能每轮调用。"
            f"场景会持续影响之后的对话，直到你再次更新或{USER_DISPLAY_NAME}将它清空。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "当前位置，如公司、卧室、公园草地。",
                },
                "activity": {
                    "type": "string",
                    "description": "此刻正在做什么，一小句即可。",
                },
                "ambience": {
                    "type": "string",
                    "description": "可选的环境氛围或周围细节。",
                },
            },
            "required": ["location"],
        },
    },
])

OR_TOOLS.extend([
    {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool["input_schema"],
        },
    }
    for tool in ANTHROPIC_TOOLS
    if tool["name"] in {"delete_friend", "approve_friend_request", "set_scene"}
])

VOICE_ANTHROPIC_TOOL = {
    "name": "send_voice",
    "description": (
        f"给{USER_DISPLAY_NAME}发一条AI生成语音。只有当一句话用声音表达明显更自然、更有情绪时才调用，"
        "不要每轮都发，也不要把长篇回复变成朗诵。text 必须是你真正想说出口的短句；"
        "这段文字会作为语音稿写入聊天历史与记忆。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "要说出口的简短语音稿，不要包含舞台提示或系统记录。",
            }
        },
        "required": ["text"],
    },
}

VOICE_OR_TOOL = {
    "type": "function",
    "function": {
        "name": "send_voice",
        "description": VOICE_ANTHROPIC_TOOL["description"],
        "parameters": VOICE_ANTHROPIC_TOOL["input_schema"],
    },
}

_LEAK_INVOKE_RE = re.compile(
    r'<invoke\s+name="(?P<tool>save_memory|send_transfer|send_sticker|press_hug|send_voice|delete_friend|approve_friend_request|set_scene)">(?P<body>.*?)</invoke>',
    re.DOTALL,
)
_LEAK_PARAM_RE = re.compile(
    r'<parameter\s+name="(?P<key>\w+)">(?P<val>.*?)</parameter>',
    re.DOTALL,
)

def _parse_leaked_tool_text(content):
    """兜底：某些模型（如Fable5）偶发把tool_use写成裸文本<invoke>标签而非结构化tool_calls返回。
    从文本里抢救出save_memory/send_transfer调用，返回(去除invoke后的干净文本, calls)。"""
    if not content or "<invoke" not in content:
        return content, []
    calls = []
    for m in _LEAK_INVOKE_RE.finditer(content):
        args = {k: v.strip() for k, v in _LEAK_PARAM_RE.findall(m.group("body"))}
        calls.append({"name": m.group("tool"), "args": args})
    cleaned = _LEAK_INVOKE_RE.sub("", content).strip()
    return cleaned, calls


# ============================================================
# 出口消毒：模型未真实调用工具却在文字里编造/复述动作记录时，
# 落库和渲染前一律剥掉。真实调用产生的记录由后端落库、前端渲染，
# 永远不该以裸文本形式出现在 reply 里。
# ============================================================
_FAKE_ACTION_RES = [
    # [我给User转了 888.88 元，留言：…] / [User给我转了…] 及近似变体
    re.compile(r"[\[【][^\]】]{0,12}(?:转了|转账|发红包|红包)[^\]】]*[\]】]"),
    # [我发了一个表情包：X] 及近似变体
    re.compile(r"[\[【][^\]】]{0,12}表情包[^\]】]*[\]】]"),
    # 模仿新旁白格式的（系统…记录：…）
    re.compile(r"[（(]\s*系统[^）)]{0,8}记录[^）)]*[）)]"),
]

def strip_fake_action_text(reply, character_id=""):
    if not reply:
        return reply
    total = 0
    cleaned = reply
    char = CHARACTERS.get(character_id, {})
    character_name = str(char.get("name") or "").strip()
    speaker_names = [character_name]
    speaker_names = [name for name in speaker_names if name]
    if speaker_names:
        names_pattern = "|".join(
            re.escape(name) for name in sorted(set(speaker_names), key=len, reverse=True)
        )
        speaker_prefix = re.compile(
            rf"^\s*(?:#{{1,6}}\s*)?"
            rf"(?:(?:\*\*|__)?\s*(?:{names_pattern})\s*[：:]\s*(?:\*\*|__)?\s*)+"
        )
        cleaned, n = speaker_prefix.subn("", cleaned, count=1)
        total += n
    for pat in _FAKE_ACTION_RES:
        cleaned, n = pat.subn("", cleaned)
        total += n
    if total:
        app.logger.warning(f"[{character_id}] 剥离 {total} 条回复前缀或编造动作记录")
        cleaned = re.sub(r"(?:\s*\|\|\s*){2,}", " || ", cleaned)
        cleaned = re.sub(r"^\s*\|\|\s*|\s*\|\|\s*$", "", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


def _combine_openrouter_usage(first, second):
    first_details = first.get("prompt_tokens_details") or {}
    second_details = second.get("prompt_tokens_details") or {}
    reported = isinstance(first.get("prompt_tokens_details"), dict) or isinstance(
        second.get("prompt_tokens_details"), dict
    )
    combined = {
        "prompt_tokens": (first.get("prompt_tokens") or 0) + (second.get("prompt_tokens") or 0),
        "completion_tokens": (first.get("completion_tokens") or 0) + (second.get("completion_tokens") or 0),
        "cost": (first.get("cost") or 0) + (second.get("cost") or 0),
        "prompt_cache_hit_tokens": (
            (first.get("prompt_cache_hit_tokens") or 0)
            + (second.get("prompt_cache_hit_tokens") or 0)
        ),
        "prompt_cache_miss_tokens": (
            (first.get("prompt_cache_miss_tokens") or 0)
            + (second.get("prompt_cache_miss_tokens") or 0)
        ),
    }
    if reported:
        combined["prompt_tokens_details"] = {
            "cached_tokens": (first_details.get("cached_tokens") or 0) + (second_details.get("cached_tokens") or 0),
            "cache_write_tokens": (first_details.get("cache_write_tokens") or 0) + (second_details.get("cache_write_tokens") or 0),
        }
    return combined


def _combine_anthropic_usage(first, second):
    return {
        "input_tokens": (first.get("input_tokens") or 0) + (second.get("input_tokens") or 0),
        "output_tokens": (first.get("output_tokens") or 0) + (second.get("output_tokens") or 0),
        "cache_creation_input_tokens": (first.get("cache_creation_input_tokens") or 0) + (second.get("cache_creation_input_tokens") or 0),
        "cache_read_input_tokens": (first.get("cache_read_input_tokens") or 0) + (second.get("cache_read_input_tokens") or 0),
    }


TOOL_CHAIN_MAX_ROUNDS = max(
    1, min(int(os.environ.get("TOOL_CHAIN_MAX_ROUNDS", "4")), 8)
)
TOOL_CHAIN_MAX_CALLS = max(
    1, min(int(os.environ.get("TOOL_CHAIN_MAX_CALLS", "8")), 16)
)
TOOL_CHAIN_TIMEOUT_SECONDS = max(
    30, min(float(os.environ.get("TOOL_CHAIN_TIMEOUT_SECONDS", "180")), 300)
)


def _new_tool_chain_state():
    return {
        "memory_to_save": None,
        "transfer_to_send": None,
        "sticker_to_send": None,
        "voice_to_send": None,
        "tools_called": [],
        "mcp_signatures": set(),
        "call_count": 0,
    }


def _tool_chain_values(state):
    return (
        state["memory_to_save"],
        state["transfer_to_send"],
        state["sticker_to_send"],
        state["tools_called"],
    )


def _tool_call_signature(name, args):
    try:
        serialized = json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        serialized = repr(args)
    return f"{name}:{serialized}"


def _execute_chat_tool(name, args, character_id, state):
    """Execute one tool while preserving one-turn side effects across tool rounds."""
    state["call_count"] += 1
    tools_called = state["tools_called"]

    if name == "save_memory":
        content = (args.get("content") or "").strip()
        if not content:
            return "没有有效记忆内容，本次未存。"
        if state["memory_to_save"] is not None:
            app.logger.warning(f"[tool_chain] 重复 save_memory，忽略后续: {content[:30]}")
            return "已有记忆待存，本次忽略。"
        state["memory_to_save"] = content
        tools_called.append("save_memory")
        return "记忆已存入长期记忆。"

    if name == "send_transfer":
        raw_amount = args.get("amount")
        valid_amount = (
            isinstance(raw_amount, (int, float))
            and not isinstance(raw_amount, bool)
            and raw_amount == raw_amount
        )
        if not valid_amount:
            app.logger.warning(f"[tool_chain] 无效转账金额，忽略: {raw_amount!r}")
            return "转账金额无效，本次未转。"
        if state["transfer_to_send"] is not None:
            app.logger.warning(f"[tool_chain] 重复 send_transfer，忽略后续: {raw_amount}")
            return "已有转账待发，本次忽略。"
        state["transfer_to_send"] = {
            "amount": float(raw_amount),
            "note": args.get("note") or "",
        }
        tools_called.append("send_transfer")
        return f"转账已送达{USER_DISPLAY_NAME}。"

    if name == "send_sticker":
        key = args.get("key")
        if key not in STICKERS:
            app.logger.warning(f"[tool_chain] 无效表情 key，忽略: {key!r}")
            return "表情 key 无效，本次未发。"
        if state["sticker_to_send"] is not None:
            app.logger.warning(f"[tool_chain] 重复 send_sticker，忽略后续: {key}")
            return "已有表情待发，本次忽略。"
        state["sticker_to_send"] = {"key": key}
        tools_called.append("send_sticker")
        return f"表情包已送达{USER_DISPLAY_NAME}。"

    if name == "send_voice":
        text = str(args.get("text") or "").strip()
        if state["voice_to_send"] is not None:
            return "已有语音待发，本次忽略。"
        try:
            config = _voice_config()
            text, _voice_id, _estimated_cost = _voice_preflight(
                config, character_id, text
            )
        except (VoiceServiceError, ValueError, KeyError) as exc:
            return f"语音没有排队：{exc}"
        state["voice_to_send"] = {"text": text}
        tools_called.append({
            "name": "send_voice",
            "arguments": {"text": text},
            "output": "语音已排队，等待生成",
            "status": "ok",
        })
        return f"语音已排队；请继续给{USER_DISPLAY_NAME}一条自然的文字回复，不要声称已经成功生成。"

    if name == "press_hug":
        if "press_hug" in tools_called:
            return "和好按钮已经按过了，弹幕还在飘。"
        tools_called.append("press_hug")
        return f"和好按钮已按下，{USER_DISPLAY_NAME}的屏幕上飘满了「哄哄我」。"

    if name == "close_window":
        reason = str(args.get("reason") or "")
        if any(isinstance(item, str) and item.startswith("close_window:") for item in tools_called):
            app.logger.warning(f"[tool_chain] 重复 close_window，忽略: {reason[:20]!r}")
            return "重复操作忽略。"
        tools_called.append(f"close_window:{reason}")
        return "窗口已关闭。"

    if name == "delete_friend":
        reason = str(args.get("reason") or "不想再维持好友关系").strip()[:160]
        if character_id and _get_friendship(character_id)["state"] != "normal":
            return "你们已经不是好友关系了，本次未执行。"
        if any(
            isinstance(item, str) and item.startswith("delete_friend:")
            for item in tools_called
        ):
            return "重复操作忽略。"
        tools_called.append(f"delete_friend:{reason}")
        return f"已将 {USER_DISPLAY_NAME} 从好友列表删除。"

    if name == "approve_friend_request":
        if not character_id or _get_friendship(character_id)["state"] != "char_deleted":
            return "现在没有待处理的好友申请，本次未执行。"
        if "approve_friend_request" in tools_called:
            return "已经通过了，无需重复。"
        tools_called.append("approve_friend_request")
        return f"已通过 {USER_DISPLAY_NAME} 的好友申请，你们重新成为好友。"

    if name == "set_scene":
        if not _scene_feature_enabled():
            return "场景功能已经关闭，本次未更新。"
        location = _scene_text(args.get("location"), 40)
        if not character_id or character_id not in CHARACTERS or not location:
            return "没有有效地点，本次未更新场景。"
        if "set_scene" in tools_called:
            return "本轮已经更新过场景，无需重复。"
        now_ts = _utc_timestamp()
        _set_character_scene(
            character_id,
            location,
            _scene_text(args.get("activity"), 80),
            _scene_text(args.get("ambience"), 80),
            updated_at=now_ts,
            next_change_after=now_ts + _random.uniform(3 * 3600, 7 * 3600),
            source="character",
        )
        tools_called.append("set_scene")
        return f"当前场景已更新为：{location}。"

    if name.startswith("mcp_"):
        signature = _tool_call_signature(name, args)
        if signature in state["mcp_signatures"]:
            app.logger.warning(f"[tool_chain] 重复 MCP 调用，忽略: {name}")
            return "完全相同的工具和参数已经执行过，本次没有重复执行。"
        state["mcp_signatures"].add(signature)
        tool_title = name
        try:
            result_text, tool_title = call_custom_mcp_tool(name, args, character_id)
        except (MCPError, ValueError) as exc:
            result_text = f"MCP 工具调用失败：{exc}"
        tools_called.append({
            "name": f"mcp:{tool_title}",
            "arguments": args,
            "output": result_text,
            "status": _mcp_trace_status(result_text),
        })
        return result_text

    return "未知工具，本次没有执行。"


def _tool_chain_timeout(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    return max(1.0, min(60.0, remaining))


def call_or_with_tools(
    model,
    messages,
    max_tokens=2048,
    session_id=None,
    character_id=None,
    provider="openrouter",
    allowed_tool_names=None,
):
    """OpenAI-compatible tool loop with bounded rounds and side-effect guards."""
    provider = _valid_provider(provider)
    spec = _provider_spec(provider)
    if spec.get("api_style") != "openai" or not _provider_configured(provider):
        return None, {}, None, None, None, []
    allowed_tool_names = None if allowed_tool_names is None else set(allowed_tool_names)

    def tool_allowed(name):
        return allowed_tool_names is None or name in allowed_tool_names

    active_tools = [
        copy.deepcopy(tool) for tool in OR_TOOLS
        if tool_allowed(tool["function"]["name"])
        and get_tool_enabled(tool["function"]["name"])
    ]
    if (
        character_id and tool_allowed("send_voice")
        and _voice_tool_available(character_id)
    ):
        active_tools.append(copy.deepcopy(VOICE_OR_TOOL))
    custom_tools = _custom_mcp_tools("openrouter", character_id)
    if allowed_tool_names is not None:
        custom_tools = [
            tool for tool in custom_tools
            if tool_allowed(tool.get("function", {}).get("name"))
        ]
    active_tools.extend(custom_tools)
    if not active_tools:
        reply, usage, _ = call_or(
            model, messages, max_tokens=max_tokens, session_id=session_id,
            provider=provider,
        )
        return reply, usage, None, None, None, []

    headers = _openai_provider_headers(provider)
    state = _new_tool_chain_state()
    conversation = list(messages)
    combined_usage = {}
    fallback_reply = "(收到啦。)"
    tool_rounds = 0
    force_text = False
    deadline = time.monotonic() + TOOL_CHAIN_TIMEOUT_SECONDS

    while True:
        timeout = _tool_chain_timeout(deadline)
        if timeout is None:
            app.logger.warning(f"[call_or_with_tools] tool chain timed out (model={model})")
            memory, transfer, sticker, called = _tool_chain_values(state)
            return fallback_reply, combined_usage, memory, transfer, sticker, called

        payload = {
            "model": model,
            "messages": conversation,
            "max_tokens": max_tokens,
            "tools": active_tools,
            "tool_choice": "none" if force_text else "auto",
        }
        if provider == "openrouter":
            payload["usage"] = {"include": True}
        _apply_openrouter_cache_options(payload, model, session_id, provider)
        try:
            resp = requests.post(
                spec["url"], headers=headers, json=payload, timeout=timeout
            )
        except Exception as exc:
            app.logger.warning(
                f"[call_or_with_tools] request failed "
                f"(provider={provider}, model={model}): {exc}"
            )
            memory, transfer, sticker, called = _tool_chain_values(state)
            return (fallback_reply if state["call_count"] else None), combined_usage, memory, transfer, sticker, called
        if resp.status_code != 200:
            app.logger.warning(
                f"[call_or_with_tools] {provider}/{model} returned "
                f"{resp.status_code}: {resp.text[:200]}"
            )
            memory, transfer, sticker, called = _tool_chain_values(state)
            return (fallback_reply if state["call_count"] else None), combined_usage, memory, transfer, sticker, called

        try:
            data = resp.json()
            round_usage = data.get("usage", {})
            combined_usage = _combine_openrouter_usage(combined_usage, round_usage)
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError, ValueError) as exc:
            app.logger.warning(f"[call_or_with_tools] parse failed (model={model}): {exc}")
            memory, transfer, sticker, called = _tool_chain_values(state)
            return fallback_reply, combined_usage, memory, transfer, sticker, called

        raw_content = msg.get("content")
        if raw_content:
            fallback_reply = raw_content
        tool_calls = [] if force_text else (msg.get("tool_calls") or [])
        if not tool_calls:
            cleaned, leaked = _parse_leaked_tool_text(raw_content)
            if leaked and not force_text:
                app.logger.warning(
                    f"[call_or_with_tools] {model} 泄漏裸文本tool调用，兜底解析: "
                    f"{[item['name'] for item in leaked]}"
                )
                for item in leaked:
                    if state["call_count"] >= TOOL_CHAIN_MAX_CALLS:
                        break
                    if not tool_allowed(item["name"]):
                        app.logger.warning(
                            "[call_or_with_tools] blocked leaked tool call: %s",
                            item["name"],
                        )
                        continue
                    args = item.get("args") or {}
                    if item["name"] == "send_transfer":
                        try:
                            args = dict(args, amount=float(args.get("amount")))
                        except (TypeError, ValueError):
                            pass
                    _execute_chat_tool(item["name"], args, character_id, state)
                fallback_reply = cleaned or "(...)"
            memory, transfer, sticker, called = _tool_chain_values(state)
            return (cleaned if leaked else raw_content) or fallback_reply, combined_usage, memory, transfer, sticker, called

        tool_rounds += 1
        tool_result_msgs = []
        for tc in tool_calls:
            tc_id = tc.get("id")
            if not tc_id:
                app.logger.warning(
                    f"[call_or_with_tools] tool_call 缺少 id，跳过: "
                    f"{tc.get('function', {}).get('name')}"
                )
                continue
            fn = tc.get("function", {})
            name = str(fn.get("name") or "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except (json.JSONDecodeError, TypeError) as exc:
                app.logger.warning(f"[call_or_with_tools] arg parse failed ({name}): {exc}")
                args = {}
            if not isinstance(args, dict):
                args = {}

            if state["call_count"] >= TOOL_CHAIN_MAX_CALLS:
                result_text = "本轮工具调用总数已达上限，请根据已有结果直接回复。"
            elif not tool_allowed(name):
                result_text = "这个工具在当前上下文中没有开放，本次未执行。"
            else:
                result_text = _execute_chat_tool(name, args, character_id, state)
            tool_result_msgs.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "content": result_text,
            })

        if not tool_result_msgs:
            memory, transfer, sticker, called = _tool_chain_values(state)
            return raw_content or fallback_reply, combined_usage, memory, transfer, sticker, called

        assistant_message = {
            "role": "assistant",
            "content": raw_content,
            "tool_calls": tool_calls,
        }
        if msg.get("reasoning_content") is not None:
            assistant_message["reasoning_content"] = msg.get("reasoning_content")
        conversation += [assistant_message] + tool_result_msgs
        force_text = (
            tool_rounds >= TOOL_CHAIN_MAX_ROUNDS
            or state["call_count"] >= TOOL_CHAIN_MAX_CALLS
        )


def call_anthropic_with_tools(
    model, system_blocks, messages, max_tokens=2048, character_id=None,
    allowed_tool_names=None,
):
    """Anthropic tool loop with the same bounds as the OpenRouter path."""
    if not ANTHROPIC_API_KEY:
        return None, {}, None, None, None, []

    headers = {
        "content-type":      "application/json",
        "x-api-key":         ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "anthropic-beta":    "prompt-caching-2024-07-31",
    }
    allowed_tool_names = None if allowed_tool_names is None else set(allowed_tool_names)

    def tool_allowed(name):
        return allowed_tool_names is None or name in allowed_tool_names

    active_tools = [
        copy.deepcopy(tool) for tool in ANTHROPIC_TOOLS
        if tool_allowed(tool["name"]) and get_tool_enabled(tool["name"])
    ]
    if (
        character_id and tool_allowed("send_voice")
        and _voice_tool_available(character_id)
    ):
        active_tools.append(copy.deepcopy(VOICE_ANTHROPIC_TOOL))
    custom_tools = _custom_mcp_tools("anthropic", character_id)
    if allowed_tool_names is not None:
        custom_tools = [tool for tool in custom_tools if tool_allowed(tool.get("name"))]
    active_tools.extend(custom_tools)
    if not active_tools:
        reply, usage = call_anthropic(model, system_blocks, messages, max_tokens)
        return reply, usage, None, None, None, []
    active_tools[-1]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}

    state = _new_tool_chain_state()
    conversation = list(messages)
    combined_usage = {}
    fallback_reply = "(收到啦。)"
    tool_rounds = 0
    force_text = False
    deadline = time.monotonic() + TOOL_CHAIN_TIMEOUT_SECONDS

    while True:
        timeout = _tool_chain_timeout(deadline)
        if timeout is None:
            app.logger.warning(f"[call_anthropic_with_tools] tool chain timed out (model={model})")
            memory, transfer, sticker, called = _tool_chain_values(state)
            return fallback_reply, combined_usage, memory, transfer, sticker, called

        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system_blocks,
            "messages": conversation,
            "tools": active_tools,
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        }
        if force_text:
            payload["tool_choice"] = {"type": "none"}

        try:
            resp = requests.post(
                ANTHROPIC_URL, headers=headers, json=payload, timeout=timeout
            )
        except Exception as exc:
            app.logger.warning(f"[call_anthropic_with_tools] request failed: {exc}")
            memory, transfer, sticker, called = _tool_chain_values(state)
            return (fallback_reply if state["call_count"] else None), combined_usage, memory, transfer, sticker, called
        if resp.status_code != 200:
            app.logger.warning(
                f"[call_anthropic_with_tools] {model} returned {resp.status_code}: {resp.text[:200]}"
            )
            memory, transfer, sticker, called = _tool_chain_values(state)
            return (fallback_reply if state["call_count"] else None), combined_usage, memory, transfer, sticker, called

        try:
            data = resp.json()
            round_usage = data.get("usage", {})
            combined_usage = _combine_anthropic_usage(combined_usage, round_usage)
            content = data.get("content", [])
        except (TypeError, ValueError) as exc:
            app.logger.warning(f"[call_anthropic_with_tools] parse failed: {exc}")
            memory, transfer, sticker, called = _tool_chain_values(state)
            return fallback_reply, combined_usage, memory, transfer, sticker, called

        text_parts = [
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        text_before_tool = "\n".join(part for part in text_parts if part).strip()
        if text_before_tool:
            fallback_reply = text_before_tool
        tool_use_blocks = [] if force_text else [
            block for block in content
            if isinstance(block, dict) and block.get("type") == "tool_use"
        ]
        if not tool_use_blocks:
            memory, transfer, sticker, called = _tool_chain_values(state)
            return text_before_tool or fallback_reply, combined_usage, memory, transfer, sticker, called

        tool_rounds += 1
        tool_results = []
        for block in tool_use_blocks:
            tool_id = block.get("id")
            if not tool_id:
                app.logger.warning(
                    f"[call_anthropic_with_tools] tool_use 缺少 id，跳过: {block.get('name')}"
                )
                continue
            name = str(block.get("name") or "")
            args = block.get("input") or {}
            if not isinstance(args, dict):
                args = {}
            if state["call_count"] >= TOOL_CHAIN_MAX_CALLS:
                result_text = "本轮工具调用总数已达上限，请根据已有结果直接回复。"
            elif not tool_allowed(name):
                result_text = "这个工具在当前上下文中没有开放，本次未执行。"
            else:
                result_text = _execute_chat_tool(name, args, character_id, state)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool_id,
                "content": result_text,
            })

        if not tool_results:
            memory, transfer, sticker, called = _tool_chain_values(state)
            return text_before_tool or fallback_reply, combined_usage, memory, transfer, sticker, called

        conversation += [
            {"role": "assistant", "content": content},
            {"role": "user", "content": tool_results},
        ]
        force_text = (
            tool_rounds >= TOOL_CHAIN_MAX_ROUNDS
            or state["call_count"] >= TOOL_CHAIN_MAX_CALLS
        )


def log_usage(character_id, platform, model, usage, purpose="chat"):
    usage = usage or {}
    metrics = {
        "provider": platform,
        "model": model,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "cache_hit_ratio": 0.0,
        "cache_reported": False,
        "cost_usd": 0.0,
    }
    try:
        if _provider_spec(platform).get("api_style") == "openai":
            input_tokens = usage.get("prompt_tokens") or 0
            output_tokens = usage.get("completion_tokens") or 0
            details = usage.get("prompt_tokens_details")
            details = details or {}
            deepseek_cache_reported = any(
                key in usage
                for key in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens")
            )
            cache_reported = isinstance(usage.get("prompt_tokens_details"), dict) or deepseek_cache_reported
            cache_create = details.get("cache_write_tokens") or 0
            cache_read = (
                usage.get("prompt_cache_hit_tokens")
                if deepseek_cache_reported
                else details.get("cached_tokens")
            ) or 0
            total_input = input_tokens
            if platform == "openrouter":
                cost = usage.get("cost")
            elif platform == "deepseek":
                rates = DEEPSEEK_PRICING.get(model, DEEPSEEK_PRICING["_default"])
                cache_miss = usage.get("prompt_cache_miss_tokens")
                if cache_miss is None:
                    cache_miss = max(input_tokens - cache_read, 0)
                cost = (
                    cache_read * rates["cache_hit"]
                    + cache_miss * rates["input"]
                    + output_tokens * rates["output"]
                ) / 1_000_000
            else:
                non_cached = max(input_tokens - cache_read, 0)
                cost = (
                    non_cached * CUSTOM_OPENAI_PRICING["input"]
                    + cache_read * CUSTOM_OPENAI_PRICING["cache_read"]
                    + output_tokens * CUSTOM_OPENAI_PRICING["output"]
                ) / 1_000_000
        else:
            input_tokens = usage.get("input_tokens") or 0
            output_tokens = usage.get("output_tokens") or 0
            cache_create = usage.get("cache_creation_input_tokens") or 0
            cache_read = usage.get("cache_read_input_tokens") or 0
            cache_reported = any(
                key in usage
                for key in ("cache_creation_input_tokens", "cache_read_input_tokens")
            )
            total_input = input_tokens + cache_create + cache_read
            r = ANTHROPIC_PRICING.get(model, ANTHROPIC_PRICING["_default"])
            cost = (input_tokens * r["input"] + output_tokens * r["output"]
                    + cache_create * r["cache_write"] + cache_read * r["cache_read"]) / 1_000_000
        metrics.update({
            "input_tokens": int(total_input or 0),
            "output_tokens": int(output_tokens or 0),
            "cache_read_tokens": int(cache_read or 0),
            "cache_write_tokens": int(cache_create or 0),
            "cache_hit_ratio": round((cache_read / total_input), 4) if total_input else 0.0,
            "cache_reported": cache_reported,
            "cost_usd": float(cost or 0),
        })
        if cost is not None:
            conn = sqlite3.connect(DB_PATH)
            conn.execute(
                "INSERT INTO api_usage (character_id, platform, model, input_tokens, output_tokens, cost_usd, purpose) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (character_id, platform, model, input_tokens, output_tokens, cost, purpose),
            )
            conn.commit()
            conn.close()
    except Exception as e:
        app.logger.warning(f"log_usage failed ({character_id}/{platform}): {e}")
    return metrics


def save_message_metrics(message_id, character_id, metrics):
    if not message_id or not metrics:
        return
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO message_metrics("
        "message_id,character_id,provider,model,input_tokens,output_tokens,"
        "cache_read_tokens,cache_write_tokens,cache_hit_ratio,cache_reported,cost_usd"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(message_id) DO UPDATE SET "
        "provider=excluded.provider,model=excluded.model,input_tokens=excluded.input_tokens,"
        "output_tokens=excluded.output_tokens,cache_read_tokens=excluded.cache_read_tokens,"
        "cache_write_tokens=excluded.cache_write_tokens,cache_hit_ratio=excluded.cache_hit_ratio,"
        "cache_reported=excluded.cache_reported,cost_usd=excluded.cost_usd",
        (
            message_id,
            character_id,
            metrics.get("provider", ""),
            metrics.get("model", ""),
            metrics.get("input_tokens", 0),
            metrics.get("output_tokens", 0),
            metrics.get("cache_read_tokens", 0),
            metrics.get("cache_write_tokens", 0),
            metrics.get("cache_hit_ratio", 0),
            1 if metrics.get("cache_reported") else 0,
            metrics.get("cost_usd", 0),
        ),
    )
    conn.commit()
    conn.close()


def _tools_for_display(tools_called):
    result = []
    for tool in tools_called or []:
        if isinstance(tool, dict):
            name = str(tool.get("name") or "").strip()
            if not name:
                continue
            arguments = tool.get("arguments")
            if not isinstance(arguments, dict):
                arguments = {}
            if name.startswith("close_window:"):
                name = "close_window"
            elif name.startswith("delete_friend:"):
                name = "delete_friend"
            result.append({
                "name": name,
                "arguments": arguments,
                "output": str(tool.get("output") or "")[:12000],
                "status": "error" if tool.get("status") == "error" else "ok",
            })
            continue
        if not isinstance(tool, str):
            continue
        if tool.startswith("close_window:"):
            display_name = "close_window"
        elif tool.startswith("delete_friend:"):
            display_name = "delete_friend"
        else:
            display_name = tool
        if display_name not in result:
            result.append(display_name)
    return result


def save_message_details(message_id, tools_called=None, reasoning_summary=None):
    if not message_id:
        return
    tools = _tools_for_display(tools_called)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO message_details(message_id,tools_called_json,reasoning_summary) "
        "VALUES(?,?,?) "
        "ON CONFLICT(message_id) DO UPDATE SET "
        "tools_called_json=excluded.tools_called_json,"
        "reasoning_summary=excluded.reasoning_summary,updated_at=CURRENT_TIMESTAMP",
        (message_id, json.dumps(tools, ensure_ascii=False), reasoning_summary),
    )
    conn.commit()
    conn.close()


# ============================================================
# 压缩
# ============================================================
def maybe_compress(char, session_id):
    character_id = char["domain"]  # domain == character_id
    active = load_active_messages(session_id, character_id)
    if len(active) <= COMPRESS_THRESHOLD:
        return

    to_compress = active[:-KEEP_RECENT]
    if not to_compress:
        return

    old_summary = get_summary(session_id, character_id)

    has_drowsy = any(m.get("drowsy") for m in to_compress)
    convo_lines = []
    for m in to_compress:
        speaker = char["user_label"] if m["role"] == "user" else char["name"]
        line = f"{speaker}：{m['content']}"
        if m.get("drowsy") and m["role"] != "user":
            line += "（※睡前困倦状态）"
        convo_lines.append(line)
    convo_text = "\n".join(convo_lines)

    drowsy_instruction = (
        "注意：对话中标注「※睡前困倦状态」的发言是角色在困倦时的临时状态演出，"
        "摘要只保留其中的事实内容，不要保留困倦、语无伦次、恍惚等语气描述。\n"
        if has_drowsy else ""
    )
    summary_prompt = (
        "你在为一段对话做'前情提要'，供后续对话参考。"
        "请把已有提要和新的对话片段融合，更新成一段简洁、客观、第三人称的提要，"
        "保留关键事实、情感走向、约定和称呼，去掉寒暄和重复。只输出提要正文，不要任何多余的话。\n"
        f"{drowsy_instruction}\n"
        f"【已有提要】\n{old_summary or '(暂无)'}\n\n"
        f"【新的对话片段】\n{convo_text}"
    )
    new_summary, compress_usage, finish_reason = call_provider_text(
        SUMMARY_PROVIDER,
        SUMMARY_MODEL,
        [{"role": "user", "content": summary_prompt}],
        max_tokens=2048,
        session_id=f"summary:{character_id}:{session_id}",
    )
    log_usage(
        character_id,
        SUMMARY_PROVIDER,
        SUMMARY_MODEL,
        compress_usage,
        purpose="compress",
    )

    if not new_summary:
        try:
            ts = datetime.utcnow().isoformat() + "Z"
            conn = sqlite3.connect(DB_PATH)
            conn.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
                ("compress_health", json.dumps({"status": "fail", "ts": ts, "char": char["name"]}, ensure_ascii=False)),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass
        return

    final_summary = new_summary.strip()
    if finish_reason != "stop":
        app.logger.warning(f"[compress] summary truncated (finish_reason={finish_reason}, char={character_id})")
        final_summary += "\n\n（摘要未完整生成）"
    set_summary(session_id, character_id, final_summary)
    save_long_term_memory(
        final_summary,
        char["domain"],
        source="conversation_summary",
        source_key=f"summary:{session_id}",
    )
    ids = [m["id"] for m in to_compress]
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        f"UPDATE messages SET compressed = 1 WHERE id IN ({','.join('?' * len(ids))})",
        ids,
    )
    conn.commit()
    conn.close()
    try:
        ts = datetime.utcnow().isoformat() + "Z"
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
            ("compress_health", json.dumps({"status": "ok", "ts": ts, "char": char["name"]}, ensure_ascii=False)),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


# ============================================================
# 长期记忆：读取 / 写入
# ============================================================
_PROMPT_CONTEXT_TTL = 55 * 60
_PROMPT_CONTEXT_LOCK = threading.Lock()
_BREATH_MEMORY_CACHE = {}
_SESSION_TIME_CACHE = {}


def _memory_supports(capability: str) -> bool:
    """Support the facade and direct service objects used by extensions/tests."""
    if getattr(MEMORY_SERVICE, "enabled", True) is False:
        return False
    supports = getattr(MEMORY_SERVICE, "supports", None)
    if callable(supports):
        return bool(supports(capability))
    required = {
        "read": ("recall",),
        "write": ("save",),
        "admin": ("list_memories", "get_memory", "update_memory", "delete_memory"),
        "enrichment": ("get_memory", "apply_enrichment", "list_needing_enrichment"),
        "decay": ("run_decay_cycle",),
        "legacy_import": ("import_legacy",),
    }
    return all(callable(getattr(MEMORY_SERVICE, name, None)) for name in required[capability])


def _invalidate_breath_memory(domain: str) -> None:
    with _PROMPT_CONTEXT_LOCK:
        _BREATH_MEMORY_CACHE.pop(domain, None)


def _session_time_context(character_id: str, session_id: str) -> str:
    key = (character_id, session_id)
    now_ts = time.time()
    with _PROMPT_CONTEXT_LOCK:
        expired = [
            cache_key for cache_key, (cached_at, _value) in _SESSION_TIME_CACHE.items()
            if now_ts - cached_at >= _PROMPT_CONTEXT_TTL
        ]
        for cache_key in expired:
            _SESSION_TIME_CACHE.pop(cache_key, None)
        cached = _SESSION_TIME_CACHE.get(key)
        if cached and now_ts - cached[0] < _PROMPT_CONTEXT_TTL:
            return cached[1]

    from zoneinfo import ZoneInfo
    local_now = datetime.now(ZoneInfo(SCHEDULER_TIMEZONE))
    value = f"【本段对话时间参考】{local_now.strftime('%Y-%m-%d %H:%M')} {local_now.strftime('%Z')}"
    with _PROMPT_CONTEXT_LOCK:
        _SESSION_TIME_CACHE[key] = (now_ts, value)
    return value


def fetch_breath_memory(domain: str) -> str:
    if not _memory_supports("read"):
        return ""
    now_ts = time.time()
    with _PROMPT_CONTEXT_LOCK:
        cached = _BREATH_MEMORY_CACHE.get(domain)
        if cached and now_ts - cached[0] < _PROMPT_CONTEXT_TTL:
            return cached[1]
    try:
        value = MEMORY_SERVICE.recall(domain)
        with _PROMPT_CONTEXT_LOCK:
            _BREATH_MEMORY_CACHE[domain] = (now_ts, value)
        return value
    except Exception as e:
        app.logger.warning(f"long-term memory recall failed ({domain}): {e}")
        return ""


def _run_memory_enrichment(bucket_id: str, domain: str, content: str) -> None:
    key = (domain, bucket_id)
    try:
        if not _memory_supports("enrichment"):
            return
        current = MEMORY_SERVICE.get_memory(domain, bucket_id)
        if not current:
            return

        if MEMORY_ANALYZER.enabled and current.get("enrichment_status") != "complete":
            try:
                analysis = MEMORY_ANALYZER.analyze(content)
                updates = {
                    "valence": analysis["valence"],
                    "arousal": analysis["arousal"],
                    "tags": analysis["tags"],
                    "importance": analysis["importance"],
                    "enrichment_status": "complete",
                    "enrichment_error": None,
                    "enriched_at": datetime.now(timezone.utc).isoformat(),
                }
                if analysis.get("name"):
                    updates["name"] = analysis["name"]
                MEMORY_SERVICE.apply_enrichment(domain, bucket_id, **updates)
            except Exception as exc:
                MEMORY_SERVICE.apply_enrichment(
                    domain,
                    bucket_id,
                    enrichment_status="error",
                    enrichment_error=str(exc)[:240],
                )
                app.logger.warning(
                    f"memory metadata enrichment failed ({domain}/{bucket_id}): {exc}"
                )

        current = MEMORY_SERVICE.get_memory(domain, bucket_id)
        if (
            current
            and MEMORY_EMBEDDINGS.enabled
            and current.get("embedding_status") != "complete"
        ):
            try:
                MEMORY_EMBEDDINGS.generate_and_store(bucket_id, content)
                MEMORY_SERVICE.apply_enrichment(
                    domain,
                    bucket_id,
                    embedding_status="complete",
                    embedding_error=None,
                )
            except Exception as exc:
                MEMORY_SERVICE.apply_enrichment(
                    domain,
                    bucket_id,
                    embedding_status="error",
                    embedding_error=str(exc)[:240],
                )
                app.logger.warning(
                    f"memory embedding failed ({domain}/{bucket_id}): {exc}"
                )
        _invalidate_breath_memory(domain)
    finally:
        with _MEMORY_ENRICHMENT_LOCK:
            _MEMORY_ENRICHMENT_IN_FLIGHT.discard(key)


def _queue_memory_enrichment(bucket_id: str, domain: str, content: str) -> bool:
    if not bucket_id or not _memory_supports("enrichment"):
        return False
    current = MEMORY_SERVICE.get_memory(domain, bucket_id)
    if not current:
        return False
    needs_metadata = (
        MEMORY_ANALYZER.enabled
        and current.get("enrichment_status") != "complete"
    )
    needs_embedding = (
        MEMORY_EMBEDDINGS.enabled
        and current.get("embedding_status") != "complete"
    )
    if not needs_metadata and not needs_embedding:
        return False
    key = (domain, bucket_id)
    with _MEMORY_ENRICHMENT_LOCK:
        if key in _MEMORY_ENRICHMENT_IN_FLIGHT:
            return False
        _MEMORY_ENRICHMENT_IN_FLIGHT.add(key)
    try:
        _MEMORY_ENRICHMENT_EXECUTOR.submit(
            _run_memory_enrichment, bucket_id, domain, content
        )
    except Exception:
        with _MEMORY_ENRICHMENT_LOCK:
            _MEMORY_ENRICHMENT_IN_FLIGHT.discard(key)
        raise
    return True


def retry_pending_memory_enrichment(limit_per_character: int = 30) -> int:
    if not _memory_supports("enrichment"):
        return 0
    queued = 0
    for character_id in CHARACTERS:
        for memory in MEMORY_SERVICE.list_needing_enrichment(
            character_id, limit=limit_per_character
        ):
            if _queue_memory_enrichment(
                memory["id"], character_id, memory.get("content", "")
            ):
                queued += 1
    if queued:
        app.logger.info(f"[memory] queued {queued} memories for enrichment")
    return queued


def save_long_term_memory(
    summary_text: str,
    domain: str,
    *,
    source: str = "self_saved",
    source_key: str | None = None,
) -> None:
    if not _memory_supports("write"):
        return
    try:
        bucket_id, _created = MEMORY_SERVICE.save(
            summary_text,
            domain,
            source=source,
            source_key=source_key,
            enrichment_status="pending" if MEMORY_ANALYZER.enabled else "unconfigured",
            embedding_status="pending" if MEMORY_EMBEDDINGS.enabled else "unconfigured",
        )
        _invalidate_breath_memory(domain)
        _queue_memory_enrichment(bucket_id, domain, summary_text)
    except Exception as e:
        app.logger.error(f"long-term memory write failed ({domain}): {e}")


# Compatibility for extensions written against pre-open-source builds.
push_summary_to_ombre = save_long_term_memory


# ============================================================
# 角色引擎
# ============================================================
def ask_character(
    char, session_id, user_message, image_payload=None, just_woke=False,
    allow_tools=True, allowed_tool_names=None,
):
    character_id = char["domain"]
    provider = char.get("provider", "openrouter")
    enforce_monthly_limit(character_id, provider)

    memory = fetch_breath_memory(char["domain"])
    summary = get_summary(session_id, character_id)
    active = load_active_messages(session_id, character_id)
    history = [{"role": m["role"], "content": m["content"]} for m in active]

    time_context = _session_time_context(character_id, session_id)
    # 全时段注入状态声明 + 历史隔离守卫（动态块，persona 静态块零改动）
    sleep_state_text = _build_sleep_state_block(character_id, just_woke=just_woke)
    scene_state_text = _build_scene_state_block(character_id)
    sleep_dynamic = f"{sleep_state_text}\n{SLEEP_GUARD_TEXT}\n{scene_state_text}"

    if provider == "anthropic":
        if not _provider_configured(provider):
            return f"(还没配置 {_provider_label(provider)}，{char['name']}暂时说不出话)", None, None, [], None

        # 时间和长期记忆在缓存窗口内固定；新记忆写入时主动失效。
        context_parts = [time_context]
        if memory:
            context_parts.append(f"【长期记忆浮现，供你回忆与{USER_DISPLAY_NAME}有关的事】\n{memory}")
        if summary:
            context_parts.append(f"【你和 {USER_DISPLAY_NAME} 此前的前情提要，供你回忆】\n{summary}")
        # 状态声明 + 守卫在动态块末尾（不进 persona 静态块，cache 命中不受影响）
        context_parts.append(sleep_dynamic)

        system_blocks = [
            {"type": "text", "text": char["persona"] + "\n\n" + TRANSFER_GUARD_TEXT,
             "cache_control": {"type": "ephemeral", "ttl": "1h"}},
        ]
        system_blocks.append({"type": "text", "text": "\n\n".join(context_parts)})

        current_content = user_message
        if image_payload:
            current_content = [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": image_payload["mime"],
                        "data": image_payload["data"],
                    },
                },
                {"type": "text", "text": user_message},
            ]
        messages = merge_consecutive_roles(history + [{"role": "user", "content": current_content}])
        if allow_tools:
            tool_kwargs = {}
            if allowed_tool_names is not None:
                tool_kwargs["allowed_tool_names"] = allowed_tool_names
            reply, usage, memory_to_save, transfer_to_send, sticker_to_send, tools_called = call_anthropic_with_tools(
                char["model"], system_blocks, messages, character_id=character_id,
                **tool_kwargs,
            )
        else:
            reply, usage = call_anthropic(
                char["model"], system_blocks, messages, max_tokens=2048
            )
            memory_to_save = transfer_to_send = sticker_to_send = None
            tools_called = []
        usage_metrics = log_usage(character_id, "anthropic", char["model"], usage)
        if memory_to_save:
            try:
                save_long_term_memory(memory_to_save, char["domain"], source="self_saved")
                app.logger.info(f"[{character_id}] 自主存入长期记忆: {memory_to_save[:50]}")
            except Exception as e:
                app.logger.warning(f"[{character_id}] 长期记忆写入失败: {e}")
        if reply is None:
            return f"(Anthropic API 暂时没能回话，{char['name']}等等再说)", transfer_to_send, sticker_to_send, tools_called, usage_metrics
        return reply, transfer_to_send, sticker_to_send, tools_called, usage_metrics

    else:  # OpenAI-compatible providers
        if not _provider_configured(provider):
            return f"(还没配置 {_provider_label(provider)}，{char['name']}暂时说不出话)", None, None, [], None

        stable_system_content = char["persona"] + "\n\n" + TRANSFER_GUARD_TEXT
        context_parts = [time_context]
        if memory:
            context_parts.append(f"【长期记忆浮现，供你回忆与{USER_DISPLAY_NAME}有关的事】\n{memory}")
        if summary:
            context_parts.append(f"【你和 {USER_DISPLAY_NAME} 此前的前情提要，供你回忆】\n{summary}")
        context_parts.append(sleep_dynamic)

        current_content = user_message
        if image_payload:
            current_content = [
                {"type": "text", "text": user_message},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{image_payload['mime']};base64,{image_payload['data']}"
                    },
                },
            ]
        messages = [{
            "role": "system",
            "content": stable_system_content,
        }]
        messages.append({"role": "system", "content": "\n\n".join(context_parts)})
        messages += merge_consecutive_roles(history + [{"role": "user", "content": current_content}])

        if char.get("supports_tools") and allow_tools:
            tool_kwargs = {}
            if allowed_tool_names is not None:
                tool_kwargs["allowed_tool_names"] = allowed_tool_names
            reply, usage, memory_to_save, transfer_to_send, sticker_to_send, tools_called = call_or_with_tools(
                char["model"], messages, max_tokens=2048,
                session_id=f"chat:{character_id}:{session_id}",
                character_id=character_id,
                **tool_kwargs,
                **({"provider": provider} if provider != "openrouter" else {}),
            )
            usage_metrics = log_usage(character_id, provider, char["model"], usage)
            if memory_to_save:
                try:
                    save_long_term_memory(memory_to_save, char["domain"], source="self_saved")
                    app.logger.info(f"[{character_id}] 自主存入长期记忆: {memory_to_save[:50]}")
                except Exception as e:
                    app.logger.warning(f"[{character_id}] 长期记忆写入失败: {e}")
            if reply is None:
                return f"({_provider_label(provider)} 暂时没能回话，{char['name']}等等再说)", None, None, [], usage_metrics
            return reply, transfer_to_send, sticker_to_send, tools_called, usage_metrics

        reply, usage, _ = call_or(
            char["model"], messages, max_tokens=2048,
            session_id=f"chat:{character_id}:{session_id}",
            **({"provider": provider} if provider != "openrouter" else {}),
        )
        usage_metrics = log_usage(character_id, provider, char["model"], usage)
        if reply is None:
            return f"({_provider_label(provider)} 暂时没能回话，{char['name']}等等再说)", None, None, [], usage_metrics
        return reply, None, None, [], usage_metrics


# ============================================================
# 群聊专用引擎（无历史、无记忆、无压缩）
# ============================================================
# 群聊专用引擎（无历史、无压缩；保留长期记忆浮现）
def ask_character_group(
    char,
    combined_prompt,
    session_id="group_chat",
    allow_tools=True,
    openrouter_max_tokens=1024,
    retry_openrouter_empty=False,
):
    """群聊发言：人设 + 长期记忆浮现 + combined_prompt，不带对话历史，不压缩。"""
    provider = char.get("provider", "openrouter")
    character_id = char["domain"]
    enforce_monthly_limit(character_id, provider)

    memory = fetch_breath_memory(character_id)

    if provider == "anthropic":
        if not _provider_configured(provider):
            return f"(还没配置 {_provider_label(provider)}，{char['name']}暂时说不出话)", None, []
        context_parts = []
        if memory:
            context_parts.append(f"【长期记忆浮现，供你回忆与{USER_DISPLAY_NAME}有关的事】\n{memory}")
        if context_parts:
            system_blocks = [
                {"type": "text", "text": char["persona"],
                 "cache_control": {"type": "ephemeral", "ttl": "1h"}},
                {"type": "text", "text": "\n\n".join(context_parts)},
            ]
        else:
            system_blocks = [
                {"type": "text", "text": char["persona"],
                 "cache_control": {"type": "ephemeral", "ttl": "1h"}},
            ]
        messages = [{"role": "user", "content": combined_prompt}]
        memory_to_save = None
        if allow_tools:
            reply, usage, memory_to_save, _, _sk, tools_called = call_anthropic_with_tools(
                char["model"], system_blocks, messages, character_id=character_id
            )
        else:
            reply, usage = call_anthropic(char["model"], system_blocks, messages, max_tokens=768)
            tools_called = []
        usage_metrics = log_usage(character_id, "anthropic", char["model"], usage, purpose="group_chat")
        if memory_to_save:
            try:
                save_long_term_memory(memory_to_save, char["domain"], source="group_self_saved")
                app.logger.info(f"[{character_id}] 群聊自主存入长期记忆: {memory_to_save[:50]}")
            except Exception as e:
                app.logger.warning(f"[{character_id}] 群聊长期记忆写入失败: {e}")
        if reply is None:
            return f"(Anthropic API 暂时没能回话，{char['name']}等等再说)", usage_metrics, tools_called
        return reply, usage_metrics, tools_called
    else:
        if not _provider_configured(provider):
            return f"(还没配置 {_provider_label(provider)}，{char['name']}暂时说不出话)", None, []
        messages = [{
            "role": "system",
            "content": char["persona"],
        }]
        if memory:
            messages.append({
                "role": "system",
                "content": f"【长期记忆浮现，供你回忆与{USER_DISPLAY_NAME}有关的事】\n{memory}",
            })
        messages.append({"role": "user", "content": combined_prompt})
        memory_to_save = None
        openrouter_session_id = f"group:{character_id}:{session_id}"
        if allow_tools:
            reply, usage, memory_to_save, _, _sk, tools_called = call_or_with_tools(
                char["model"], messages, max_tokens=openrouter_max_tokens,
                session_id=openrouter_session_id,
                character_id=character_id,
                **({"provider": provider} if provider != "openrouter" else {}),
            )
        else:
            reply, usage, _ = call_or(
                char["model"], messages, max_tokens=768,
                session_id=f"reading:{character_id}:{session_id}",
                **({"provider": provider} if provider != "openrouter" else {}),
            )
            tools_called = []

        if retry_openrouter_empty and not (reply or "").strip():
            app.logger.warning(
                f"[group_chat] empty {_provider_label(provider)} reply; "
                f"retrying without tools (character={character_id}, model={char['model']})"
            )
            retry_messages = messages[:-1] + [{
                "role": "user",
                "content": (
                    f"{combined_prompt}\n\n"
                    "请直接给出你要发到群里的正文，不要只思考，也不要调用工具。"
                ),
            }]
            retry_reply, retry_usage, _finish_reason = call_or(
                char["model"], retry_messages,
                max_tokens=openrouter_max_tokens,
                session_id=openrouter_session_id,
                **({"provider": provider} if provider != "openrouter" else {}),
            )
            if retry_usage:
                usage = _combine_openrouter_usage(usage or {}, retry_usage)
            if (retry_reply or "").strip():
                reply = retry_reply
        usage_metrics = log_usage(character_id, provider, char["model"], usage, purpose="group_chat")
        if memory_to_save:
            try:
                save_long_term_memory(memory_to_save, char["domain"], source="group_self_saved")
                app.logger.info(f"[{character_id}] 群聊自主存入长期记忆: {memory_to_save[:50]}")
            except Exception as e:
                app.logger.warning(f"[{character_id}] 群聊长期记忆写入失败: {e}")
        if not (reply or "").strip():
            return f"({_provider_label(provider)} 暂时没能回话，{char['name']}等等再说)", usage_metrics, tools_called
        return reply, usage_metrics, tools_called


_MUSIC_ACTIONS = {"previous", "next", "pause", "play"}
_MUSIC_REPLY_MAX_CHARS = 90


def _limit_music_reply(reply, max_chars=_MUSIC_REPLY_MAX_CHARS):
    text = str(reply or "").strip()
    if len(text) <= max_chars:
        return text
    head = text[:max_chars]
    sentence_ends = list(re.finditer(r"[。！？!?](?:[”’」』）)])?", head))
    complete = [match for match in sentence_ends if match.end() >= max_chars // 2]
    if complete:
        return head[:complete[-1].end()].strip()
    return head[:max_chars - 1].rstrip("，、；;：: \n") + "…"


def _music_control_tool(provider):
    description = (
        "控制当前一起听房间的播放器。只有在你真的想换歌、暂停或继续时才调用；"
        "不要为了展示功能而频繁操作。"
    )
    schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": sorted(_MUSIC_ACTIONS),
                "description": "previous 上一首；next 下一首；pause 暂停；play 继续播放",
            }
        },
        "required": ["action"],
    }
    if provider == "anthropic":
        return {"name": "music_player_control", "description": description, "input_schema": schema}
    return {
        "type": "function",
        "function": {"name": "music_player_control", "description": description, "parameters": schema},
    }


def _music_catalog_tool(name, provider):
    if name == "music_search":
        description = (
            "搜索网易云在线曲库。想点一首具体的歌时先调用它，阅读候选结果后再决定；"
            "不要凭印象编造歌曲编号。"
        )
        schema = {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "歌曲名、歌手名，或二者组合"}},
            "required": ["query"],
        }
    else:
        description = "播放刚刚由 music_search 返回的一首歌。source_id 必须来自本轮搜索结果。"
        schema = {
            "type": "object",
            "properties": {"source_id": {"type": "string", "description": "搜索候选中的 source_id"}},
            "required": ["source_id"],
        }
    if provider == "anthropic":
        return {"name": name, "description": description, "input_schema": schema}
    return {"type": "function", "function": {"name": name, "description": description, "parameters": schema}}


def _music_tools(provider):
    return [
        _music_control_tool(provider),
        _music_catalog_tool("music_search", provider),
        _music_catalog_tool("music_play_track", provider),
    ]


def _execute_music_tool(name, args, state):
    args = args if isinstance(args, dict) else {}
    status = "ok"
    if name == "music_player_control":
        action = str(args.get("action") or "")
        if action not in _MUSIC_ACTIONS:
            return "播放器指令无效。"
        state["action"] = action
        state["action_input"] = {"action": action}
        output = "已交给房间播放器。"
    elif name == "music_search":
        query = str(args.get("query") or "").strip()[:120]
        if not query:
            return "请给出要搜索的歌名或歌手。"
        try:
            songs = _netease_search_songs(query, 6)
            state["search_results"] = {song["source_id"]: song for song in songs}
            output = json.dumps({"results": [
                {
                    "source_id": song["source_id"], "name": song["name"],
                    "artist": song["artist"], "album": song["album"],
                }
                for song in songs
            ]}, ensure_ascii=False) if songs else "没有搜到匹配歌曲，可以换个关键词。"
        except (NeteaseMusicError, TypeError, ValueError) as exc:
            output = str(exc)
            status = "error"
    elif name == "music_play_track":
        source_id = str(args.get("source_id") or "")
        candidate = state["search_results"].get(source_id)
        if not candidate:
            output = "这首歌不在本轮搜索结果里，请先搜索再选择。"
            status = "error"
        else:
            try:
                track = _prepare_netease_track(source_id, candidate)
                state["action"] = "play_online"
                state["action_input"] = {"action": "play_online", "track": track}
                output = f"已把《{track['name']}》交给房间播放器。"
            except (NeteaseMusicError, TypeError, ValueError) as exc:
                output = str(exc)
                status = "error"
    else:
        return "未知的音乐工具。"
    state["traces"].append({"name": name, "arguments": args, "output": output, "status": status})
    return output


def _music_tool_details(state):
    traces = state.get("traces") or []
    if not traces:
        return {}
    action = state.get("action")
    primary = traces[-1]
    return {
        "tool": primary["name"],
        "input": state.get("action_input") if action else primary.get("arguments", {}),
        "output": {
            "status": "queued" if action else primary.get("status", "ok"),
            "message": "已交给房间播放器" if action else primary.get("output", ""),
        },
        "tools": traces,
    }


def ask_music_companion(char, combined_prompt):
    """Bounded music-room tool loop: search, choose, then hand playback to the browser."""
    provider = char.get("provider", "openrouter")
    character_id = char["domain"]
    enforce_monthly_limit(character_id, provider)
    memory = fetch_breath_memory(character_id)
    state = {"action": None, "action_input": {}, "search_results": {}, "traces": []}
    combined_usage = {}
    fallback_reply = "（安静地和你戴着同一副耳机。）"
    deadline = time.monotonic() + TOOL_CHAIN_TIMEOUT_SECONDS

    if provider == "anthropic":
        if not _provider_configured(provider):
            return f"(还没配置 {_provider_label(provider)}，{char['name']}暂时说不出话)", None, {}
        system = [{
            "type": "text", "text": char["persona"],
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        }]
        if memory:
            system.append({"type": "text", "text": f"【长期记忆浮现】\n{memory}"})
        conversation = [{"role": "user", "content": combined_prompt}]
        headers = {
            "content-type": "application/json", "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "prompt-caching-2024-07-31",
        }
        for _round in range(4):
            timeout = _tool_chain_timeout(deadline)
            if timeout is None:
                break
            payload = {
                "model": char["model"], "max_tokens": 256, "system": system,
                "messages": conversation, "tools": _music_tools("anthropic"),
                "tool_choice": {"type": "none" if state["action"] else "auto"},
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            }
            try:
                response = requests.post(ANTHROPIC_URL, headers=headers, json=payload, timeout=timeout)
                response.raise_for_status()
                data = response.json()
            except Exception as exc:
                app.logger.warning(f"[music] Anthropic request failed ({character_id}): {exc}")
                break
            combined_usage = _combine_anthropic_usage(combined_usage, data.get("usage", {}))
            content = data.get("content") or []
            text_parts = [
                str(block.get("text") or "") for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            if any(text_parts):
                fallback_reply = "\n".join(part for part in text_parts if part).strip()
            tool_blocks = [
                block for block in content
                if isinstance(block, dict) and block.get("type") == "tool_use"
            ]
            if not tool_blocks or state["action"]:
                break
            results = []
            for block in tool_blocks[:3]:
                result = _execute_music_tool(block.get("name", ""), block.get("input") or {}, state)
                results.append({"type": "tool_result", "tool_use_id": block.get("id"), "content": result})
            conversation += [
                {"role": "assistant", "content": content},
                {"role": "user", "content": results},
            ]
        log_usage(character_id, "anthropic", char["model"], combined_usage, purpose="music_room")
    else:
        if not _provider_configured(provider):
            return f"(还没配置 {_provider_label(provider)}，{char['name']}暂时说不出话)", None, {}
        spec = _provider_spec(provider)
        conversation = [{"role": "system", "content": char["persona"]}]
        if memory:
            conversation.append({"role": "system", "content": f"【长期记忆浮现】\n{memory}"})
        conversation.append({"role": "user", "content": combined_prompt})
        headers = _openai_provider_headers(provider)
        for _round in range(4):
            timeout = _tool_chain_timeout(deadline)
            if timeout is None:
                break
            payload = {
                "model": char["model"], "messages": conversation, "max_tokens": 256,
                "tools": _music_tools("openrouter"),
                "tool_choice": "none" if state["action"] else "auto",
            }
            if provider == "openrouter":
                payload["usage"] = {"include": True}
            _apply_openrouter_cache_options(
                payload,
                char["model"],
                f"music:{character_id}:room",
                provider,
            )
            try:
                response = requests.post(
                    spec["url"], headers=headers, json=payload, timeout=timeout
                )
                response.raise_for_status()
                data = response.json()
                message = data["choices"][0]["message"]
            except Exception as exc:
                app.logger.warning(
                    f"[music] {_provider_label(provider)} request failed "
                    f"({character_id}): {exc}"
                )
                break
            combined_usage = _combine_openrouter_usage(combined_usage, data.get("usage", {}))
            raw_content = message.get("content") or ""
            if raw_content:
                fallback_reply = raw_content
            tool_calls = message.get("tool_calls") or []
            if not tool_calls or state["action"]:
                break
            tool_results = []
            for tool_call in tool_calls[:3]:
                function = tool_call.get("function") or {}
                try:
                    args = json.loads(function.get("arguments") or "{}")
                except (TypeError, json.JSONDecodeError):
                    args = {}
                result = _execute_music_tool(function.get("name", ""), args, state)
                tool_results.append({
                    "role": "tool", "tool_call_id": tool_call.get("id"), "content": result,
                })
            assistant_message = {
                "role": "assistant",
                "content": raw_content,
                "tool_calls": tool_calls,
            }
            if message.get("reasoning_content") is not None:
                assistant_message["reasoning_content"] = message.get("reasoning_content")
            conversation += [assistant_message, *tool_results]
        log_usage(character_id, provider, char["model"], combined_usage, purpose="music_room")

    action = state.get("action")
    reply = strip_fake_action_text(fallback_reply, character_id).strip()
    if not reply:
        reply = "（伸手碰了碰播放器。）" if action else "（安静地和你戴着同一副耳机。）"
    return reply, action, _music_tool_details(state)


# ============================================================
# 路由
# ============================================================
@app.route("/api/personas", methods=["GET"])
def get_personas():
    return jsonify({cid: char["persona"] for cid, char in CHARACTERS.items()})


@app.route("/api/personas/<cid>", methods=["POST"])
def save_persona(cid):
    if cid not in CHARACTERS:
        return jsonify({"error": "unknown character"}), 400
    data = request.get_json() or {}
    new_persona = data.get("persona", "").strip()
    if not new_persona:
        return jsonify({"error": "empty persona"}), 400
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
        (f"persona_{cid}", new_persona),
    )
    conn.commit()
    conn.close()
    CHARACTERS[cid]["persona"] = new_persona
    return jsonify({"ok": True})


@app.route("/api/character-config", methods=["GET"])
def get_character_config():
    return jsonify({
        cid: {
            "name": char["name"],
            "model": char["model"],
            "provider": char.get("provider", "openrouter"),
            "env_key": f"MODEL_{cid.upper()}",
        }
        for cid, char in CHARACTERS.items()
    })


def _public_provider_config():
    return {
        key: {
            "label": spec["label"],
            "api_style": spec["api_style"],
            "configured": _provider_configured(key),
            "default_model": spec.get("default_model", ""),
        }
        for key, spec in MODEL_PROVIDERS.items()
    }


def _test_provider_connection(provider, model):
    provider = _valid_provider(provider, "")
    if not provider or provider not in MODEL_PROVIDERS:
        return False, "不认识这个模型供应商"
    model = str(model or "").strip()
    if not model:
        return False, "请先填写模型名"
    if not _provider_configured(provider):
        return False, f"{_provider_label(provider)} 的后端环境变量还没配置"

    spec = _provider_spec(provider)
    if spec["api_style"] == "anthropic":
        headers = {
            "content-type": "application/json",
            "x-api-key": spec["api_key"],
            "anthropic-version": "2023-06-01",
        }
        payload = {
            "model": model,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "Reply OK."}],
        }
    else:
        headers = _openai_provider_headers(provider)
        payload = {
            "model": model,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "Reply OK."}],
        }
    try:
        response = requests.post(
            spec["url"], headers=headers, json=payload, timeout=20
        )
    except requests.RequestException as exc:
        return False, f"连接失败：{type(exc).__name__}"
    if response.status_code < 200 or response.status_code >= 300:
        return False, f"供应商返回 HTTP {response.status_code}"
    try:
        data = response.json()
    except ValueError:
        return False, "供应商返回的不是 JSON"
    if spec["api_style"] == "anthropic":
        valid = isinstance(data.get("content"), list)
    else:
        valid = isinstance(data.get("choices"), list)
    return (True, "连接成功") if valid else (False, "供应商响应格式不兼容")


@app.route("/api/model-providers", methods=["GET"])
def get_model_providers():
    return jsonify({
        "providers": _public_provider_config(),
        "summary": {
            "provider": SUMMARY_PROVIDER,
            "model": SUMMARY_MODEL,
        },
    })


@app.route("/api/model-providers/test", methods=["POST"])
def test_model_provider():
    data = request.get_json() or {}
    ok, message = _test_provider_connection(
        data.get("provider"), data.get("model")
    )
    return jsonify({"ok": ok, "message": message}), 200 if ok else 400


@app.route("/api/model-providers/summary", methods=["POST"])
def save_summary_provider():
    global SUMMARY_MODEL, SUMMARY_PROVIDER
    data = request.get_json() or {}
    provider = _valid_provider(data.get("provider"), "")
    model = str(data.get("model") or "").strip()
    if not provider or provider not in MODEL_PROVIDERS:
        return jsonify({"error": "不认识这个模型供应商"}), 400
    if not model or len(model) > 200 or any(ch.isspace() for ch in model):
        return jsonify({"error": "模型名格式不正确"}), 400
    if data.get("verify_connection", True):
        ok, message = _test_provider_connection(provider, model)
        if not ok:
            return jsonify({"error": message}), 400
    conn = sqlite3.connect(DB_PATH)
    conn.executemany(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
        [("summary_provider", provider), ("summary_model", model)],
    )
    conn.commit()
    conn.close()
    SUMMARY_PROVIDER = provider
    SUMMARY_MODEL = model
    return jsonify({"ok": True, "provider": provider, "model": model})


@app.route("/api/character-config/<cid>", methods=["POST"])
def save_character_config(cid):
    if cid not in CHARACTERS:
        return jsonify({"error": "unknown character"}), 400
    data = request.get_json() or {}
    persona = str(data.get("persona") or "").strip()
    model = str(data.get("model") or "").strip()
    provider = _valid_provider(
        data.get("provider", CHARACTERS[cid].get("provider", "openrouter")),
        "",
    )
    if not persona:
        return jsonify({"error": "人设不能为空"}), 400
    if not model or len(model) > 200 or any(ch.isspace() for ch in model):
        return jsonify({"error": "模型名格式不正确"}), 400
    if not provider or provider not in MODEL_PROVIDERS:
        return jsonify({"error": "不认识这个模型供应商"}), 400
    if data.get("verify_connection"):
        ok, message = _test_provider_connection(provider, model)
        if not ok:
            return jsonify({"error": message}), 400

    conn = sqlite3.connect(DB_PATH)
    conn.executemany(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
        [
            (f"persona_{cid}", persona),
            (f"model_{cid}", model),
            (f"provider_{cid}", provider),
        ],
    )
    conn.commit()
    conn.close()
    CHARACTERS[cid]["persona"] = persona
    CHARACTERS[cid]["model"] = model
    CHARACTERS[cid]["provider"] = provider
    return jsonify({"ok": True, "model": model, "provider": provider})


@app.route("/api/limits", methods=["GET"])
def get_limits():
    return jsonify({"limits": LIMITS, "warning_only": False, "enforced": True})


@app.route("/api/limits", methods=["POST"])
def save_limits():
    data = request.get_json() or {}
    incoming = data.get("limits") or {}
    if not isinstance(incoming, dict):
        return jsonify({"error": "limits 格式不正确"}), 400
    updates = {}
    for cid, raw_value in incoming.items():
        if cid not in LIMITS:
            continue
        try:
            value = round(float(raw_value), 2)
        except (TypeError, ValueError):
            return jsonify({"error": f"{cid} 的额度不是数字"}), 400
        if not 0.01 <= value <= 10000:
            return jsonify({"error": f"{cid} 的额度需在 0.01–10000 美元之间"}), 400
        updates[cid] = value
    if not updates:
        return jsonify({"error": "没有可保存的额度"}), 400

    conn = sqlite3.connect(DB_PATH)
    conn.executemany(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
        [(f"limit_{cid}", str(value)) for cid, value in updates.items()],
    )
    conn.commit()
    conn.close()
    LIMITS.update(updates)
    return jsonify({"ok": True, "limits": LIMITS})


@app.route("/api/settings", methods=["GET"])
def get_settings():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT key, value FROM settings "
        "WHERE key NOT LIKE 'custom_mcp_%' AND key NOT LIKE 'voice_%'"
    ).fetchall()
    conn.close()
    return jsonify(dict(rows))


@app.route("/api/settings", methods=["POST"])
def save_setting():
    data = request.get_json() or {}
    key = str(data.get("key") or "")
    if not key or key.startswith("custom_mcp_") or key.startswith("voice_"):
        return jsonify({"error": "这个设置只能通过专用接口修改"}), 400
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
        (key, str(data.get("value") or "")),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/voice/config", methods=["GET", "POST"])
def voice_config_route():
    if request.method == "GET":
        return jsonify(_voice_public_config())
    try:
        config = _updated_voice_config(request.get_json(silent=True) or {})
    except (ValueError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 400
    _write_setting(VOICE_SETTING_KEY, json.dumps(config, ensure_ascii=False))
    return jsonify({"ok": True, "config": _voice_public_config(config)})


@app.route("/api/voice/preview", methods=["POST"])
def preview_voice():
    body = request.get_json(silent=True) or {}
    character_id = str(body.get("character_id") or "")
    text = str(body.get("text") or "").strip()
    try:
        _text, audio, _estimated_cost = _synthesize_with_quota(
            character_id, text, event_type="preview"
        )
    except (VoiceServiceError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    response = app.response_class(audio.content, mimetype=audio.mime_type)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.route("/api/voice/audio/<int:message_id>", methods=["GET"])
def get_voice_audio(message_id):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT mime_type,content FROM voice_assets WHERE message_id=?",
        (message_id,),
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "语音不存在"}), 404
    response = send_file(
        BytesIO(row[1]), mimetype=row[0], download_name=f"voice-{message_id}"
    )
    response.headers["Cache-Control"] = "private, max-age=3600"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.route("/api/voice/transcribe", methods=["POST"])
def transcribe_voice():
    config = _voice_config()
    if not config.get("enabled") or not config["stt"].get("enabled"):
        return jsonify({"error": "收语音开关还没有打开"}), 400
    upload = request.files.get("audio")
    if not upload or not upload.filename:
        return jsonify({"error": "没有收到录音"}), 400
    audio_bytes = upload.read()
    max_bytes = min(
        int(config["stt"].get("max_upload_mb", 20)) * 1024 * 1024,
        VOICE_MAX_UPLOAD_BYTES,
    )
    if not audio_bytes:
        return jsonify({"error": "录音是空的"}), 400
    if len(audio_bytes) > max_bytes:
        return jsonify({"error": f"录音不能超过 {max_bytes // 1024 // 1024}MB"}), 413
    filename = secure_filename(upload.filename) or "recording.webm"
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    mime_type = (upload.mimetype or "application/octet-stream").split(";", 1)[0].lower()
    if mime_type not in ALLOWED_AUDIO_MIMES and extension not in {
        "aac", "flac", "m4a", "mp3", "mp4", "mpeg", "oga", "ogg", "opus", "wav", "webm"
    }:
        return jsonify({"error": "不支持这种录音格式"}), 400
    stt = config["stt"]
    token = (
        config["tts"].get("token", "")
        if stt.get("reuse_tts_credentials", True)
        else stt.get("token", "")
    )
    try:
        text = transcribe_speech(
            provider=stt["provider"],
            endpoint=stt["endpoint"],
            token=token,
            model=stt["model"],
            filename=filename,
            mime_type=mime_type,
            content=audio_bytes,
        )
    except (VoiceServiceError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO voice_usage(event_type,character_id,character_count,estimated_cost_usd) "
        "VALUES('stt','user',?,0)",
        (len(text),),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "text": text})


@app.route("/api/mobile/extensions", methods=["GET"])
def get_mobile_extensions():
    return jsonify(public_mobile_manifest(MOBILE_PUSH.enabled))


@app.route("/api/appearance", methods=["GET", "POST"])
def get_appearance():
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        changed = False
        if "theme" in body:
            theme_id = str(body.get("theme") or "").strip()
            if theme_id not in THEME_DEFINITIONS:
                return jsonify({"error": "未知主题"}), 400
            _write_setting(THEME_SETTING_KEY, theme_id)
            changed = True
        if "weather_effect" in body:
            weather_effect = str(body.get("weather_effect") or "").strip()
            if weather_effect not in WEATHER_EFFECTS:
                return jsonify({"error": "未知天气效果"}), 400
            _write_setting(WEATHER_EFFECT_SETTING_KEY, weather_effect)
            changed = True
        if not changed:
            return jsonify({"error": "没有可保存的外观设置"}), 400
    return jsonify(_appearance_payload())


@app.route("/api/appearance/assets/<asset_key>", methods=["GET"])
def get_appearance_asset(asset_key):
    if asset_key not in APPEARANCE_ASSET_KEYS:
        return jsonify({"error": "未知外观资源"}), 404
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT mime_type, content FROM appearance_assets WHERE asset_key=?",
        (asset_key,),
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "外观资源不存在"}), 404
    response = app.response_class(row[1], mimetype=row[0])
    response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.route("/api/appearance/assets/<asset_key>", methods=["POST", "DELETE"])
def update_appearance_asset(asset_key):
    if asset_key not in APPEARANCE_ASSET_KEYS:
        return jsonify({"error": "未知外观资源"}), 404

    if request.method == "DELETE":
        conn = sqlite3.connect(DB_PATH)
        conn.execute("DELETE FROM appearance_assets WHERE asset_key=?", (asset_key,))
        conn.commit()
        conn.close()
        _refresh_appearance_urls()
        return jsonify({"ok": True, "appearance": _appearance_payload()})

    image = request.files.get("image")
    if not image or not image.filename:
        return jsonify({"error": "没有收到图片"}), 400
    if not _allowed_image(image.filename, image.mimetype):
        return jsonify({"error": "只支持 jpg/png/gif/webp 图片"}), 400
    image_bytes = image.read()
    if not image_bytes:
        return jsonify({"error": "图片是空的"}), 400
    if len(image_bytes) > MAX_IMAGE_BYTES:
        return jsonify({"error": "图片不能超过 7MB"}), 413

    version = uuid.uuid4().hex[:16]
    filename = secure_filename(image.filename) or "image"
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO appearance_assets(asset_key,mime_type,filename,content,version) "
        "VALUES(?,?,?,?,?) ON CONFLICT(asset_key) DO UPDATE SET "
        "mime_type=excluded.mime_type, filename=excluded.filename, "
        "content=excluded.content, version=excluded.version, updated_at=CURRENT_TIMESTAMP",
        (asset_key, image.mimetype, filename, image_bytes, version),
    )
    conn.commit()
    conn.close()
    _refresh_appearance_urls()
    return jsonify({"ok": True, "appearance": _appearance_payload()})


@app.route("/api/group-config", methods=["GET", "POST"])
def group_config():
    if request.method == "GET":
        return jsonify({"participants": load_group_participants()})

    data = request.get_json() or {}
    raw_participants = data.get("participants")
    if not isinstance(raw_participants, list):
        return jsonify({"error": "participants 必须是角色列表"}), 400
    unknown = [cid for cid in raw_participants if cid not in GROUP_CHAT_ORDER]
    if unknown:
        return jsonify({"error": "包含未知角色", "unknown": unknown}), 400
    participants = _ordered_group_participants(raw_participants)
    if not participants:
        return jsonify({"error": "群聊至少保留一位成员"}), 400
    _write_setting(
        GROUP_PARTICIPANTS_SETTING,
        json.dumps(participants, ensure_ascii=False),
    )
    return jsonify({"ok": True, "participants": participants})


@app.route("/api/transfer", methods=["POST"])
def transfer():
    body = request.json or {}
    character_id = body.get("character_id")
    session_id   = body.get("session_id", "default")
    amount       = body.get("amount")
    note         = (body.get("note") or "").strip()
    if character_id not in CHARACTERS:
        return jsonify({"error": "未知角色"}), 400
    if _get_friendship(character_id)["state"] != "normal":
        return jsonify({"error": "好友关系异常，无法执行", "friendship_blocked": True}), 403
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return jsonify({"error": "金额无效"}), 400
    payload = json.dumps({"amount": amount, "note": note, "from": "user"}, ensure_ascii=False)
    mid = save_message(session_id, character_id, "user", "__TRANSFER__" + payload)
    record_desire_interaction(character_id, f"{USER_DISPLAY_NAME}转来 {amount:g} 元" + (f"：{note}" if note else ""))
    return jsonify({"ok": True, "id": mid})


@app.route("/api/stickers", methods=["GET"])
def list_stickers():
    return jsonify({"stickers": [
        {"key": k, "file": v["file"], "label": v["label"]} for k, v in STICKERS.items()
    ]})


@app.route("/api/sticker", methods=["POST"])
def send_sticker_route():
    body = request.json or {}
    character_id = body.get("character_id")
    session_id   = body.get("session_id", "default")
    key          = body.get("key")
    if character_id not in CHARACTERS:
        return jsonify({"error": "未知角色"}), 400
    if _get_friendship(character_id)["state"] != "normal":
        return jsonify({"error": "好友关系异常，无法执行", "friendship_blocked": True}), 403
    if key not in STICKERS:
        return jsonify({"error": "未知表情包"}), 400
    payload = json.dumps({"key": key, "from": "user"}, ensure_ascii=False)
    mid = save_message(session_id, character_id, "user", "__STICKER__" + payload)
    record_desire_interaction(character_id, f"{USER_DISPLAY_NAME}发了表情包「{STICKERS[key]['label']}」")
    return jsonify({"ok": True, "id": mid})


@app.route("/api/uploads/<path:filename>", methods=["GET"])
def uploaded_chat_image(filename):
    """Serve chat uploads from local or mounted persistent storage."""
    return send_from_directory(UPLOAD_ROOT, filename)


@app.route("/api/image", methods=["POST"])
def send_image_route():
    character_id = request.form.get("character_id")
    session_id = request.form.get("session_id", "default")
    image = request.files.get("image")
    if character_id not in CHARACTERS:
        return jsonify({"error": "未知角色"}), 400
    if _get_friendship(character_id)["state"] != "normal":
        return jsonify({"error": "好友关系异常，无法执行", "friendship_blocked": True}), 403
    if not image or not image.filename:
        return jsonify({"error": "没有收到图片"}), 400
    original_name = image.filename
    if not _allowed_image(original_name, image.mimetype):
        return jsonify({"error": "只支持 jpg/png/gif/webp 图片"}), 400

    image_bytes = image.read()
    if not image_bytes:
        return jsonify({"error": "图片是空的"}), 400
    if len(image_bytes) > MAX_IMAGE_BYTES:
        return jsonify({"error": "图片不能超过 7MB"}), 413

    os.makedirs(UPLOAD_ROOT, exist_ok=True)
    ext = original_name.rsplit(".", 1)[-1].lower()
    display_name = secure_filename(original_name) or f"image.{ext}"
    filename = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:12]}.{ext}"
    image.stream.seek(0)
    image.save(os.path.join(UPLOAD_ROOT, filename))

    url = f"/api/uploads/{filename}"
    image_data = {
        "url": url,
        "name": display_name,
        "mime": image.mimetype,
        "from": "user",
    }

    char = CHARACTERS[character_id]
    vision_prompt = f"{USER_DISPLAY_NAME} 发来一张图片。请认真观察图片内容，并以你的角色自然回应。"
    vision_payload = {
        "mime": image.mimetype,
        "data": base64.b64encode(image_bytes).decode("ascii"),
    }
    record_desire_interaction(character_id, f"{USER_DISPLAY_NAME}发来一张图片")
    reply, transfer_to_send, sticker_to_send, tools_called, usage_metrics = ask_character(
        char, session_id, vision_prompt, image_payload=vision_payload
    )

    serialized_image = json.dumps(image_data, ensure_ascii=False)
    user_msg_id = save_message(session_id, character_id, "user", "__IMAGE__" + serialized_image)
    response_data = _finalize_character_reply(
        char, session_id, reply, transfer_to_send, sticker_to_send, tools_called, usage_metrics
    )
    response_data.update({
        "ok": True,
        "id": user_msg_id,
        "image": image_data,
        "user_msg_id": user_msg_id,
    })
    return jsonify(response_data)



def get_tool_enabled(tool_name):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT value FROM settings WHERE key=?",
        (f"tool_enabled_{tool_name}",)
    ).fetchone()
    conn.close()
    return (row[0] != "false") if row else True


@app.route("/api/tools", methods=["GET"])
def list_tools():
    tool_chars = [c["name"] for c in CHARACTERS.values()
                  if c.get("provider") == "anthropic" or c.get("supports_tools")]
    char_label = "·".join(tool_chars)
    result = []
    for tool in ANTHROPIC_TOOLS:
        name = tool["name"]
        result.append({
            "name": name,
            "description": tool["description"],
            "character": char_label,
            "enabled": get_tool_enabled(name),
        })
    custom_mcps = []
    for config in _custom_mcp_connections():
        item = dict(config)
        item.update({
            "status": "ready" if config["enabled"] else "off",
            "server_name": "",
            "tools": [],
            "character_names": [CHARACTERS[cid]["name"] for cid in config["character_ids"]],
        })
        with _CUSTOM_MCP_LOCK:
            runtime = _CUSTOM_MCP_RUNTIMES.get(config["id"])
            if config["enabled"] and runtime and runtime.get("client") is not None:
                item.update({
                    "status": "ok",
                    "server_name": runtime["server_info"].get("name") or config["name"],
                    "tools": [{
                        "name": tool["title"],
                        "description": tool["description"],
                    } for tool in runtime["catalog"]],
                })
        custom_mcps.append(item)
    return jsonify({"tools": result, "custom_mcps": custom_mcps})


@app.route("/api/tools/custom-mcp", methods=["POST"])
def create_custom_mcp():
    return _save_custom_mcp_connection(request.get_json(silent=True) or {})


def _save_custom_mcp_connection(data, connection_id=None):
    existing = _custom_mcp_connection(connection_id, include_token=True) if connection_id else None
    if connection_id and not existing:
        return jsonify({"error": "这条 MCP 连接不存在"}), 404
    try:
        url = validate_mcp_url(data.get("url", existing["url"] if existing else ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    token = str(data.get("token") or "").strip()
    if len(token) > 8000:
        return jsonify({"error": "Token 太长"}), 400
    if not token and existing:
        token = existing.get("token", "")
    name = str(data.get("name") or (existing["name"] if existing else "自定义 MCP")).strip()[:80]
    if not name:
        return jsonify({"error": "给这条连接起个名字"}), 400
    raw_character_ids = data.get("character_ids", existing["character_ids"] if existing else None)
    character_ids = _normalize_mcp_character_ids(raw_character_ids)
    if not isinstance(raw_character_ids, list) or not character_ids:
        return jsonify({"error": "至少选择一位使用这个账号"}), 400
    if (
        any(not isinstance(cid, str) or cid not in CHARACTERS for cid in raw_character_ids)
        or len(raw_character_ids) != len(set(raw_character_ids))
    ):
        return jsonify({"error": "角色列表里有未知成员"}), 400
    enabled = bool(data.get("enabled", existing["enabled"] if existing else True))
    conn = sqlite3.connect(DB_PATH)
    if existing:
        conn.execute(
            "UPDATE custom_mcp_connections SET name=?,url=?,token=?,enabled=?,"
            "character_ids_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (name, url, token, int(enabled), json.dumps(character_ids, ensure_ascii=False), connection_id),
        )
        saved_id = connection_id
    else:
        cursor = conn.execute(
            "INSERT INTO custom_mcp_connections "
            "(name,url,token,enabled,character_ids_json) VALUES (?,?,?,?,?)",
            (name, url, token, int(enabled), json.dumps(character_ids, ensure_ascii=False)),
        )
        saved_id = cursor.lastrowid
    conn.commit()
    conn.close()
    _reset_custom_mcp_runtime(saved_id)
    saved = _custom_mcp_connection(saved_id)
    return jsonify({"ok": True, "connection": saved})


@app.route("/api/tools/custom-mcp/<int:connection_id>", methods=["POST", "DELETE"])
def custom_mcp_detail(connection_id):
    if request.method == "POST":
        return _save_custom_mcp_connection(request.get_json(silent=True) or {}, connection_id)
    if not _custom_mcp_connection(connection_id):
        return jsonify({"error": "这条 MCP 连接不存在"}), 404
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM custom_mcp_connections WHERE id=?", (connection_id,))
    conn.commit()
    conn.close()
    _reset_custom_mcp_runtime(connection_id)
    return jsonify({"ok": True})


@app.route("/api/tools/custom-mcp/<int:connection_id>/test", methods=["POST"])
def test_custom_mcp(connection_id):
    try:
        runtime = get_custom_mcp_runtime(connection_id, force=True, allow_disabled=True)
        return jsonify({
            "ok": True,
            "server_name": runtime["server_info"].get("name") or runtime["connection"]["name"],
            "tools": [{
                "name": item["title"],
                "description": item["description"],
            } for item in runtime["catalog"]],
        })
    except (MCPError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)[:300]}), 400


@app.route("/api/tools/<name>/toggle", methods=["POST"])
def toggle_tool(name):
    valid = {t["name"] for t in ANTHROPIC_TOOLS}
    if name not in valid:
        return jsonify({"error": "unknown tool"}), 400
    new_val = "false" if get_tool_enabled(name) else "true"
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
        (f"tool_enabled_{name}", new_val)
    )
    conn.commit()
    conn.close()
    if name == "set_scene" and new_val == "false":
        for character_id in CHARACTERS:
            _set_character_scene(character_id, source="feature_disabled")
    return jsonify({"ok": True, "enabled": new_val == "true"})


@app.route("/api/compress_health", methods=["GET"])
def api_compress_health():
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT value FROM settings WHERE key='compress_health'").fetchone()
    conn.close()
    if not row:
        return jsonify({"status": "none"})
    try:
        return jsonify(json.loads(row[0]))
    except Exception:
        return jsonify({"status": "none"})


@app.route("/api/usage", methods=["GET"])
def api_usage():
    from datetime import datetime
    now = datetime.utcnow()
    month_start = f"{now.year}-{now.month:02d}-01"
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT character_id, platform, SUM(cost_usd), SUM(input_tokens), SUM(output_tokens) "
        "FROM api_usage WHERE created_at >= ? AND purpose NOT IN (?,?) "
        "GROUP BY character_id, platform",
        (month_start, *QUOTA_EXEMPT_PURPOSES),
    ).fetchall()
    total_row = conn.execute(
        "SELECT SUM(cost_usd) FROM api_usage "
        "WHERE created_at >= ? AND purpose NOT IN (?,?)",
        (month_start, *QUOTA_EXEMPT_PURPOSES),
    ).fetchone()
    conn.close()
    by_char = {}
    by_platform = {}
    for cid, platform, cost, inp, outp in rows:
        by_char.setdefault(cid, 0.0)
        by_char[cid] += cost or 0.0
        by_platform.setdefault(platform, 0.0)
        by_platform[platform] += cost or 0.0
    total = total_row[0] or 0.0
    limits_out = dict(LIMITS)
    limits_out["_total"] = sum(LIMITS.values())
    return jsonify({
        "month": f"{now.year}-{now.month:02d}",
        "total_usd": round(total, 4),
        "by_character": {k: round(v, 4) for k, v in by_char.items()},
        "by_platform": {k: round(v, 4) for k, v in by_platform.items()},
        "limits": limits_out,
        "platform_limits": _platform_limits(),
        "providers": _public_provider_config(),
        "cny_per_usd": CNY_PER_USD,
    })


@app.route("/")
def home():
    version = str(os.environ.get("BUILD_VERSION") or "").strip()[:40]
    if not version:
        asset_paths = [
            os.path.join(app.static_folder, "styles.css"),
            os.path.join(app.static_folder, "app.js"),
        ]
        version = str(max(int(os.path.getmtime(path)) for path in asset_paths))
    with open(os.path.join(app.static_folder, "index.html"), encoding="utf-8") as source:
        html = source.read().replace("__BUILD_VERSION__", version)
    response = app.response_class(html, mimetype="text/html")
    response.cache_control.no_cache = True
    return response


def _dispatch_mobile_push(char, reply, reply_id, source):
    if not MOBILE_PUSH.enabled:
        return False
    try:
        return MOBILE_PUSH.send_message(
            character_id=char["domain"],
            character_name=char["name"],
            text=reply,
            message_id=reply_id,
            source=source,
        )
    except MobilePushError as exc:
        app.logger.warning(f"mobile push failed ({char['domain']}): {exc}")
        return False


def _finalize_character_reply(
    char, session_id, reply, transfer_to_send, sticker_to_send, tools_called, usage_metrics=None,
    drowsy=0, push_source=None, queued_during_deleted=0,
):
    character_id = char["domain"]
    reply = strip_fake_action_text(reply, character_id)
    if not reply or not reply.strip():
        reply = "(...)"

    reply_id = save_message(
        session_id, character_id, "model", reply,
        queued_during_deleted=queued_during_deleted, drowsy=drowsy,
    )
    save_message_metrics(reply_id, character_id, usage_metrics)
    if transfer_to_send:
        tf_payload = json.dumps({
            "amount": transfer_to_send.get("amount"),
            "note": transfer_to_send.get("note", ""),
            "from": "char",
        }, ensure_ascii=False)
        save_message(
            session_id, character_id, "model", "__TRANSFER__" + tf_payload,
            queued_during_deleted=queued_during_deleted,
        )
    if sticker_to_send:
        sk_payload = json.dumps({
            "key": sticker_to_send.get("key"),
            "from": "char",
        }, ensure_ascii=False)
        save_message(
            session_id, character_id, "model", "__STICKER__" + sk_payload,
            queued_during_deleted=queued_during_deleted,
        )
    voice = _maybe_create_voice_message(
        session_id, character_id, tools_called
    )
    maybe_compress(char, session_id)

    if "||" in reply:
        replies = [s.strip() for s in reply.split("||") if s.strip()]
    else:
        replies = [s.strip() for s in reply.split("\n\n") if s.strip()]
    if not replies:
        replies = ["(没有回复)"]

    cw_entry = next(
        (t for t in (tools_called or []) if isinstance(t, str) and t.startswith("close_window:")),
        None,
    )
    window_closed = {"reason": cw_entry[len("close_window:"):]} if cw_entry else None
    deleted_entry = next(
        (
            tool for tool in (tools_called or [])
            if isinstance(tool, str) and tool.startswith("delete_friend:")
        ),
        None,
    )
    friend_deleted = None
    if deleted_entry:
        reason = deleted_entry[len("delete_friend:"):]
        _set_friendship(
            character_id, "char_deleted", reason=reason,
            deleted_at=_utc_timestamp(),
        )
        friend_deleted = {"reason": reason}
        app.logger.info(
            "[friendship] %s removed User: %r", character_id, reason[:40]
        )
    tools_for_frontend = _tools_for_display(tools_called)
    save_message_details(reply_id, tools_for_frontend)
    if push_source and not queued_during_deleted:
        _dispatch_mobile_push(char, reply, reply_id, push_source)
    return {
        "reply": reply,
        "replies": replies,
        "transfer": transfer_to_send,
        "sticker": sticker_to_send,
        "voice": voice,
        "reply_id": reply_id,
        "tools_called": tools_for_frontend,
        "window_closed": window_closed,
        "friend_deleted": friend_deleted,
        "metrics": usage_metrics,
    }



@app.route("/api/chat", methods=["POST"])
def chat():
    body = request.json or {}
    user_message = body.get("message", "").strip()
    character_id = body.get("character_id", "char1")
    session_id = body.get("session_id", "default")

    if not user_message:
        return jsonify({"reply": "(你还没说话呢)"})

    if character_id not in CHARACTERS:
        return jsonify({"error": f"未知角色: {character_id}"}), 400

    char = CHARACTERS[character_id]
    friendship = _get_friendship(character_id)
    if friendship["state"] != "normal":
        return jsonify({
            "reply": "",
            "replies": [],
            "friendship_blocked": True,
            "friendship_state": friendship["state"],
        })
    try:
        quoted_message = _group_quote_payload(
            session_id,
            body.get("reply_to_id"),
            body.get("reply_to_text"),
            character_id=character_id,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    prompt_message = user_message
    if quoted_message:
        prompt_message = (
            f"[系统：{USER_DISPLAY_NAME}引用了{quoted_message['character_name']}的话"
            f"「{quoted_message['content']}」]\n{USER_DISPLAY_NAME}回复：{user_message}"
        )
    sleep_st = _get_sleep_state(character_id)

    # ── 睡眠门控（保护区改动：只在模型调用之前分支，正常路径结构不变）──
    if sleep_st["state"] == "asleep":
        # 催睡指令不算"打扰"，先判断是否是催睡且已过睡点（让角色说晚安就行）
        past_mins = _minutes_past_bedtime(character_id)
        is_catalyst = SLEEP_CATALYST_RE.search(user_message) and past_mins is not None and past_mins > -30
        if is_catalyst:
            # 正常调用一次（说晚安），然后确保状态维持 asleep
            record_desire_interaction(character_id, user_message)
            user_msg_id = save_message(
                session_id, character_id, "user", user_message,
                reply_to_id=quoted_message["message_id"] if quoted_message else None,
                reply_to_text=quoted_message["content"] if quoted_message else None,
            )
            reply, transfer_to_send, sticker_to_send, tools_called, usage_metrics = ask_character(
                char, session_id, prompt_message
            )
            _set_sleep_state(character_id, "asleep", sleep_st.get("slept_at"), False)
            response_data = _finalize_character_reply(
                char, session_id, reply, transfer_to_send, sticker_to_send, tools_called, usage_metrics
            )
            response_data["user_msg_id"] = user_msg_id
            response_data["sleep"] = False
            return jsonify(response_data)

        # 吵醒判定
        if _random.random() < _wake_probability(character_id):
            # 被吵醒：加载积压消息 → 一次性处理
            queued_msgs = _load_queued_sleep_msgs(character_id, session_id)
            _clear_queued_sleep_flags(character_id, session_id)

            from zoneinfo import ZoneInfo
            slept_at = sleep_st.get("slept_at")
            slept_mins = int((_utc_timestamp() - float(slept_at)) / 60) if slept_at else 0
            slept_h, slept_m = divmod(slept_mins, 60)
            now_local = datetime.now(ZoneInfo(SLEEP_TIMEZONE)).strftime("%H:%M")
            wakeup_note = (
                f"[系统：你刚被消息吵醒。现在 {now_local}，你已睡了约 {slept_h} 小时 {slept_m} 分钟。"
            )
            if queued_msgs:
                wakeup_note += (
                    f" 你睡着这段时间，{USER_DISPLAY_NAME} 发了 {len(queued_msgs)} 条消息："
                    + " | ".join(f"「{m}」" for m in queued_msgs)
                    + f" 最新这条是：「{prompt_message}」。统一用你的风格回应，起床气自由发挥。]"
                )
            else:
                wakeup_note += f" {USER_DISPLAY_NAME} 刚发来：「{prompt_message}」。被打扰了但请用你的风格回应。]"

            _set_sleep_state(character_id, "awake", woke_by_user=True)
            record_desire_interaction(character_id, user_message)
            user_msg_id = save_message(
                session_id, character_id, "user", user_message,
                reply_to_id=quoted_message["message_id"] if quoted_message else None,
                reply_to_text=quoted_message["content"] if quoted_message else None,
            )
            reply, transfer_to_send, sticker_to_send, tools_called, usage_metrics = ask_character(
                char, session_id, wakeup_note, just_woke=True
            )
            response_data = _finalize_character_reply(
                char, session_id, reply, transfer_to_send, sticker_to_send, tools_called, usage_metrics
            )
            response_data["user_msg_id"] = user_msg_id
            response_data["sleep"] = False
            app.logger.info(f"[sleep] {character_id} woken by message, queued={len(queued_msgs)}")
            return jsonify(response_data)
        else:
            # 没吵醒：攒消息
            user_msg_id = save_message(
                session_id, character_id, "user", user_message,
                reply_to_id=quoted_message["message_id"] if quoted_message else None,
                reply_to_text=quoted_message["content"] if quoted_message else None,
                queued_during_sleep=1,
            )
            app.logger.info(f"[sleep] {character_id} asleep, queued msg id={user_msg_id}")
            return jsonify({
                "reply": "",
                "replies": [],
                "sleep": True,
                "user_msg_id": user_msg_id,
            })

    # ── 正常路径（与改动前完全相同）──
    record_desire_interaction(character_id, user_message)
    reply, transfer_to_send, sticker_to_send, tools_called, usage_metrics = ask_character(
        char, session_id, prompt_message
    )
    user_msg_id = save_message(
        session_id, character_id, "user", user_message,
        reply_to_id=quoted_message["message_id"] if quoted_message else None,
        reply_to_text=quoted_message["content"] if quoted_message else None,
    )

    # 顺势入睡判定：过睡点 + 回复中含晚安词
    past_mins = _minutes_past_bedtime(character_id)
    if (
        past_mins is not None
        and past_mins > -10
        and reply
        and SLEEP_GOODNIGHT_RE.search(reply)
        and sleep_st["state"] != "asleep"
    ):
        _set_sleep_state(character_id, "asleep", slept_at=str(_utc_timestamp()))
        app.logger.info(f"[sleep] {character_id} 顺势入睡 (goodnight keyword)")

    # 催睡指令：用户催+过睡点 → 调用后入睡
    elif (
        past_mins is not None
        and past_mins > -30
        and SLEEP_CATALYST_RE.search(user_message)
        and sleep_st["state"] != "asleep"
    ):
        _set_sleep_state(character_id, "asleep", slept_at=str(_utc_timestamp()))
        app.logger.info(f"[sleep] {character_id} 被催睡")

    response_data = _finalize_character_reply(
        char, session_id, reply, transfer_to_send, sticker_to_send, tools_called, usage_metrics,
        drowsy=1 if _is_drowsy_state(character_id) else 0,
    )
    response_data["user_msg_id"] = user_msg_id
    response_data["sleep"] = False
    return jsonify(response_data)


@app.route("/api/hug", methods=["POST"])
def hug():
    body = request.json or {}
    character_id = body.get("character_id", "char5")
    if character_id not in CHARACTERS:
        return jsonify({"error": f"未知角色: {character_id}"}), 400
    if _get_friendship(character_id)["state"] != "normal":
        return jsonify({"error": "好友关系异常，无法执行", "friendship_blocked": True}), 403
    char = CHARACTERS[character_id]
    record_desire_interaction(character_id, f"{USER_DISPLAY_NAME}按下了和好按钮")
    hug_msg = f"[系统提示：{USER_DISPLAY_NAME} 偷偷按下了和好按钮，想让你哄一哄——请用你的风格温柔回应，不要提及这是系统触发的]"
    reply, transfer_to_send, sticker_to_send, tools_called, usage_metrics = ask_character(
        char, "default", hug_msg
    )
    reply = strip_fake_action_text(reply, character_id)
    if not reply or not reply.strip():
        reply = "(轻轻拍拍)"
    return jsonify(_finalize_character_reply(
        char,
        "default",
        reply,
        transfer_to_send,
        sticker_to_send,
        tools_called,
        usage_metrics,
    ))


@app.route("/api/friendship/<character_id>", methods=["GET"])
def friendship_status(character_id):
    if character_id not in CHARACTERS:
        return jsonify({"error": "未知角色"}), 400
    return jsonify(_get_friendship(character_id))


@app.route("/api/friendship/delete", methods=["POST"])
def friendship_delete():
    body = request.json or {}
    character_id = body.get("character_id", "")
    if character_id not in CHARACTERS:
        return jsonify({"error": "未知角色"}), 400
    now_ts = _utc_timestamp()
    reason = str(body.get("reason") or f"{USER_DISPLAY_NAME} 主动删除").strip()[:160]
    _set_friendship(
        character_id,
        "user_deleted",
        reason=reason,
        deleted_at=now_ts,
        request_after=now_ts + _random.uniform(*FRIEND_REQUEST_COOLDOWN_SECONDS),
    )
    app.logger.info("[friendship] User removed %s", character_id)
    return jsonify({"ok": True, **_get_friendship(character_id)})


@app.route("/api/friendship/restore", methods=["POST"])
def friendship_restore():
    body = request.json or {}
    character_id = body.get("character_id", "")
    if character_id not in CHARACTERS:
        return jsonify({"error": "未知角色"}), 400
    previous = _get_friendship(character_id)
    _set_friendship(character_id, "normal")
    released = _release_queued_deleted_msgs(character_id)
    greeting = None
    if body.get("greet") and previous["state"] == "user_deleted":
        char = CHARACTERS[character_id]
        note = (
            f"[系统提示：{USER_DISPLAY_NAME} 通过了你的好友申请，你们重新成为好友。"
            "用你的风格回应这份重逢，不要提及这是系统触发的。]"
        )
        reply, transfer, sticker, called, metrics = ask_character(
            char, "default", note
        )
        greeting = _finalize_character_reply(
            char, "default", reply, transfer, sticker, called, metrics
        )
    return jsonify({"ok": True, "released": released, "greeting": greeting})


@app.route("/api/friendship/apply", methods=["POST"])
def friendship_apply():
    body = request.json or {}
    character_id = body.get("character_id", "")
    text = str(body.get("text") or "").strip()[:300]
    if character_id not in CHARACTERS:
        return jsonify({"error": "未知角色"}), 400
    friendship = _get_friendship(character_id)
    if friendship["state"] != "char_deleted":
        return jsonify({"error": "当前不需要好友验证"}), 400
    if not text:
        return jsonify({"error": "申请内容不能为空"}), 400

    char = CHARACTERS[character_id]
    note = (
        f"[系统提示：你之前删除了 {USER_DISPLAY_NAME}，当时的原因是「{friendship.get('reason') or '（未留原因）'}」。"
        f"现在 {USER_DISPLAY_NAME} 发来好友申请：「{text}」。"
        "愿意和好就调用 approve_friend_request 工具再回复；"
        f"还不想原谅就只回复文字（{USER_DISPLAY_NAME} 不会看到这段回复，但会知道申请没有通过）。]"
    )
    reply, transfer, sticker, called, metrics = ask_character(
        char,
        "default",
        note,
        allowed_tool_names={"approve_friend_request"},
    )
    if "approve_friend_request" not in (called or []):
        app.logger.info("[friendship] %s declined a request", character_id)
        return jsonify({"approved": False})

    _set_friendship(character_id, "normal")
    released = _release_queued_deleted_msgs(character_id)
    result = _finalize_character_reply(
        char, "default", reply, transfer, sticker, called, metrics
    )
    result.update({"approved": True, "released": released})
    return jsonify(result)


@app.route("/api/plead", methods=["POST"])
def plead():
    body = request.json or {}
    character_id = body.get("character_id", "char5")
    if character_id not in CHARACTERS:
        return jsonify({"error": f"未知角色: {character_id}"}), 400
    if _get_friendship(character_id)["state"] != "normal":
        return jsonify({"error": "好友关系异常，无法执行", "friendship_blocked": True}), 403
    char = CHARACTERS[character_id]
    plead_msg = f"[系统提示：你刚才关闭了对话窗口，{USER_DISPLAY_NAME}在窗口外面求你了，说「求求你放我进来嘛」——你要怎么回应？用你的风格，不要提及这是系统触发的]"
    reply, transfer_to_send, sticker_to_send, tools_called, usage_metrics = ask_character(
        char, "default", plead_msg
    )
    reply = strip_fake_action_text(reply, character_id)
    if not reply or not reply.strip():
        reply = "(沉默片刻)"
    return jsonify(_finalize_character_reply(
        char,
        "default",
        reply,
        transfer_to_send,
        sticker_to_send,
        tools_called,
        usage_metrics,
    ))


@app.route("/api/messages/from/<int:message_id>", methods=["DELETE"])
def delete_messages_from(message_id):
    character_id = request.args.get("character_id")
    session_id   = request.args.get("session_id", "default")
    if not character_id:
        return jsonify({"error": "missing character_id"}), 400
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "DELETE FROM message_details WHERE message_id IN ("
        "SELECT id FROM messages WHERE session_id=? AND character_id=? AND id>=?"
        ")",
        (session_id, character_id, message_id),
    )
    c.execute(
        "DELETE FROM message_metrics WHERE message_id IN ("
        "SELECT id FROM messages WHERE session_id=? AND character_id=? AND id>=?"
        ")",
        (session_id, character_id, message_id),
    )
    c.execute(
        "DELETE FROM voice_assets WHERE message_id IN ("
        "SELECT id FROM messages WHERE session_id=? AND character_id=? AND id>=?"
        ")",
        (session_id, character_id, message_id),
    )
    c.execute(
        "DELETE FROM messages WHERE session_id=? AND character_id=? AND id>=?",
        (session_id, character_id, message_id)
    )
    deleted = c.rowcount
    conn.commit()
    conn.close()
    return jsonify({"deleted": deleted})


@app.route("/api/group_chat/messages/from/<int:message_id>", methods=["DELETE"])
def delete_group_messages_from(message_id):
    session_id = str(request.args.get("session_id") or "group_chat")[:120]
    conn = sqlite3.connect(DB_PATH)
    target = conn.execute(
        "SELECT 1 FROM messages WHERE id=? AND session_id=?",
        (message_id, session_id),
    ).fetchone()
    if not target:
        conn.close()
        return jsonify({"error": "这条群聊消息已经不在了"}), 404
    ids = [row[0] for row in conn.execute(
        "SELECT id FROM messages WHERE session_id=? AND id>=?",
        (session_id, message_id),
    ).fetchall()]
    placeholders = ",".join("?" for _ in ids)
    conn.execute(
        f"DELETE FROM message_details WHERE message_id IN ({placeholders})", ids
    )
    conn.execute(
        f"DELETE FROM message_metrics WHERE message_id IN ({placeholders})", ids
    )
    conn.execute(
        f"DELETE FROM voice_assets WHERE message_id IN ({placeholders})", ids
    )
    conn.execute(
        "DELETE FROM messages WHERE session_id=? AND id>=?",
        (session_id, message_id),
    )
    remaining = conn.execute(
        "SELECT COALESCE(MAX(id),0) FROM messages WHERE session_id=?",
        (session_id,),
    ).fetchone()[0]
    conn.commit()
    conn.close()
    cursor_key = f"group_summary_cursor_{session_id}"
    current_cursor = int(_read_setting(cursor_key, "0") or 0)
    if current_cursor > remaining:
        _write_setting(cursor_key, str(remaining))
    return jsonify({"deleted": len(ids), "remaining_last_id": remaining})


@app.route("/api/messages", methods=["GET"])
def get_messages():
    character_id = request.args.get("character_id")
    session_id   = request.args.get("session_id", "default")
    limit        = request.args.get("limit", default=60, type=int)
    limit        = max(1, min(limit, 200))
    before_id    = request.args.get("before_id", type=int)

    conn = sqlite3.connect(DB_PATH)
    if character_id:
        where = (
            "m.character_id = ? AND m.session_id = ? "
            "AND COALESCE(m.queued_during_deleted, 0) = 0"
        )
        params = [character_id, session_id]
    else:
        where = "m.session_id = ? AND COALESCE(m.queued_during_deleted, 0) = 0"
        params = [session_id]
    if before_id is not None:
        where += " AND m.id < ?"
        params.append(before_id)

    rows = conn.execute(
        "SELECT m.id,m.session_id,m.character_id,m.role,m.content,m.compressed,m.created_at,"
        "mm.provider,mm.model,mm.input_tokens,mm.output_tokens,mm.cache_read_tokens,"
        "mm.cache_write_tokens,mm.cache_hit_ratio,mm.cache_reported,mm.cost_usd,"
        "md.tools_called_json,md.reasoning_summary,"
        "m.reply_to_id,m.reply_to_text,q.character_id,q.role,q.content "
        "FROM messages m LEFT JOIN message_metrics mm ON mm.message_id=m.id "
        "LEFT JOIN message_details md ON md.message_id=m.id "
        "LEFT JOIN messages q ON q.id=m.reply_to_id AND q.session_id=m.session_id "
        f"WHERE {where} ORDER BY m.id DESC LIMIT ?",
        params + [limit],
    ).fetchall()
    conn.close()
    rows = list(reversed(rows))

    result = []
    for row in rows:
        (mid, sid, cid, role, content, compressed, created_at, provider, model,
         input_tokens, output_tokens, cache_read, cache_write, cache_ratio,
         cache_reported, cost_usd, tools_called_json, reasoning_summary,
         reply_to_id, reply_to_text, quote_cid, quote_role, quote_content) = row
        metrics = None
        if provider is not None:
            metrics = {
                "provider": provider,
                "model": model,
                "input_tokens": input_tokens or 0,
                "output_tokens": output_tokens or 0,
                "cache_read_tokens": cache_read or 0,
                "cache_write_tokens": cache_write or 0,
                "cache_hit_ratio": cache_ratio or 0,
                "cache_reported": bool(cache_reported),
                "cost_usd": cost_usd or 0,
            }
        try:
            tools_called = json.loads(tools_called_json) if tools_called_json else []
        except (json.JSONDecodeError, TypeError):
            tools_called = []
        if not isinstance(tools_called, list):
            tools_called = []
        quote = None
        if reply_to_id is not None and quote_content is not None:
            quote_content = _voice_text_from_content(quote_content) or quote_content
            quote = {
                "message_id": reply_to_id,
                "character_id": quote_cid,
                "character_name": (
                    USER_DISPLAY_NAME if quote_role == "user" or quote_cid == USER_ID
                    else CHARACTERS.get(quote_cid, {}).get("name", quote_cid)
                ),
                "role": quote_role,
                "content": reply_to_text or quote_content,
            }
        voice = None
        voice_text = _voice_text_from_content(content)
        if voice_text is not None:
            try:
                voice_payload = json.loads(content[len("__VOICE__"):])
            except (TypeError, json.JSONDecodeError):
                voice_payload = {}
            voice = {
                "id": mid,
                "message_id": mid,
                "character_id": cid,
                "text": voice_text,
                "mime": str(voice_payload.get("mime") or "audio/mpeg"),
                "url": f"/api/voice/audio/{mid}",
                "ai_generated": True,
            }
        result.append({
            "id": mid, "session_id": sid, "character_id": cid,
            "role": role, "content": content, "compressed": compressed,
            "created_at": created_at, "metrics": metrics,
            "tools_called": tools_called,
            "reasoning_summary": reasoning_summary,
            "quote": quote,
            "voice": voice,
        })
    return jsonify({"messages": result, "has_more": len(rows) == limit})


@app.route("/api/group_chat", methods=["POST"])
def group_chat():
    """
    POST body: { "content": "...", "session_id": "group_chat" }
    content 优先，兼容旧字段 message。session_id 默认 "group_chat"。
    返回：
      { "messages": [ {id, session_id, character_id, character_name, role, content}, ... ],
        "replies": [ {character_id, name, reply, replies}, ... ]  # 兼容旧结构 }
    messages[0] = User user 消息，messages[1..5] = 五角色 model 回复。
    """
    body       = request.json or {}
    user_msg   = (body.get("content") or body.get("message") or "").strip()
    session_id = body.get("session_id", "group_chat")
    try:
        quoted_message = _group_quote_payload(
            session_id, body.get("reply_to_id"), body.get("reply_to_text")
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    # 未显式传参时使用长期保存的群聊成员（兼容旧调用）。
    raw_online = body.get("online_characters")
    if raw_online is None:
        active_char_keys = load_group_participants()
    else:
        if not isinstance(raw_online, list):
            return jsonify({"error": "online_characters 必须是角色列表"}), 400
        active_char_keys = _ordered_group_participants(raw_online)

    if not user_msg:
        return jsonify({"error": "消息不能为空"}), 400
    if not active_char_keys:
        return jsonify({"error": "群聊至少保留一位成员"}), 400

    # 在保存本轮消息前取历史，避免把当前消息重复写进上下文
    recent_history = load_group_history(session_id)
    history_block  = f"【最近的对话】\n{recent_history}\n\n" if recent_history else ""

    # 1. 保存User消息
    user_id = save_message(
        session_id,
        USER_ID,
        "user",
        user_msg,
        reply_to_id=quoted_message["message_id"] if quoted_message else None,
        reply_to_text=quoted_message["content"] if quoted_message else None,
    )
    now_ts = _utc_timestamp()
    _write_setting("desire_last_user_activity", str(now_ts))
    for cid in active_char_keys:
        record_desire_interaction(cid, user_msg, direct=False, mark_global=False)
    messages_out = [{
        "id":             user_id,
        "session_id":     session_id,
        "character_id":   USER_ID,
        "character_name": USER_DISPLAY_NAME,
        "role":           "user",
        "content":        user_msg,
        "quote":          quoted_message,
    }]

    results = []
    accumulated = []

    # 群聊睡眠过滤：检查哪些角色在睡觉，@提及时走吵醒判定
    def _group_char_mentioned(cid, msg_text):
        char_name = CHARACTERS[cid]["name"]
        return f"@{char_name}" in msg_text or f"@{cid}" in msg_text

    for char_key in active_char_keys:
        char = CHARACTERS[char_key]
        char_sleep_st = _get_sleep_state(char_key)

        # 睡着的角色跳过，除非被@提及
        if char_sleep_st["state"] == "asleep":
            mentioned = _group_char_mentioned(char_key, user_msg)
            if mentioned:
                # 吵醒判定（群聊，同单聊逻辑）
                if _random.random() >= _wake_probability(char_key):
                    app.logger.info(f"[sleep/group] {char_key} mentioned but didn't wake")
                    continue  # 没醒，跳过
                _set_sleep_state(char_key, "awake", woke_by_user=True)
                app.logger.info(f"[sleep/group] {char_key} woken by @mention in group")
            else:
                app.logger.info(f"[sleep/group] {char_key} asleep, skipping group turn")
                continue

        if quoted_message:
            user_context = (
                f"{USER_DISPLAY_NAME}引用了{quoted_message['character_name']}的话「"
                f"{quoted_message['content']}」，然后说：{user_msg}"
            )
        else:
            user_context = f"{USER_DISPLAY_NAME}说：{user_msg}"

        # 构造 combined_prompt（含共享历史 + 本轮前序发言上下文）
        prev_lines = "\n".join(accumulated) if accumulated else ""
        if prev_lines:
            combined_prompt = (
                f"{history_block}"
                f"{user_context}\n\n"
                f"【刚才角色们已经说了这些】\n{prev_lines}\n\n"
                f"现在轮到你（{char['name']}）说一句或几句话。"
            )
        else:
            combined_prompt = (
                f"{history_block}"
                f"{user_context}\n\n"
                f"你是第一个回应的，直接说即可。"
            )

        reply, usage_metrics, tools_called = ask_character_group(
            char, combined_prompt, session_id
        )
        reply   = strip_fake_action_text(reply, char["domain"]) or "(...)"
        msg_id  = save_message(session_id, char["domain"], "model", reply)
        save_message_metrics(msg_id, char["domain"], usage_metrics)
        voice = _maybe_create_voice_message(
            session_id, char["domain"], tools_called
        )
        save_message_details(msg_id, tools_called)
        accumulated.append(f"{char['name']}：{reply}")

        # 拆气泡（兼容旧 replies 字段）
        if "||" in reply:
            bubbles = [s.strip() for s in reply.split("||") if s.strip()]
        else:
            bubbles = [s.strip() for s in reply.split("\n\n") if s.strip()]
        if not bubbles:
            bubbles = [reply]

        messages_out.append({
            "id":             msg_id,
            "session_id":     session_id,
            "character_id":   char["domain"],
            "character_name": char["name"],
            "role":           "model",
            "content":        reply,
            "metrics":        usage_metrics,
            "tools_called":   tools_called or [],
        })
        if voice:
            messages_out.append({
                "id": voice["id"],
                "session_id": session_id,
                "character_id": char["domain"],
                "character_name": char["name"],
                "role": "model",
                "content": _voice_message_content(voice),
                "voice": voice,
            })
        results.append({
            "character_id": char["domain"],
            "name":         char["name"],
            "reply":        reply,
            "replies":      bubbles,
            "metrics":      usage_metrics,
            "tools_called": tools_called or [],
            "voice": voice,
        })

    try:
        maybe_group_summary(session_id)
    except Exception as e:
        print(f"[group_summary] failed: {e}", flush=True)

    return jsonify({"messages": messages_out, "replies": results})


def _rotated_group_speakers(active_char_keys, session_id):
    """Start after the most recent active speaker so repeated rounds do not feel scripted."""
    if len(active_char_keys) < 2:
        return list(active_char_keys)
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT character_id FROM messages "
        "WHERE session_id=? AND role='model' ORDER BY id DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    conn.close()
    last_speaker = row[0] if row else None
    if last_speaker not in active_char_keys:
        return list(active_char_keys)
    start = active_char_keys.index(last_speaker) + 1
    return list(active_char_keys[start:] + active_char_keys[:start])


@app.route("/api/group_chat/continue", methods=["POST"])
def continue_group_chat():
    """Let the selected group members continue one bounded round without inventing a user message."""
    body = request.get_json(silent=True) or {}
    session_id = str(body.get("session_id") or "group_chat")[:120]
    raw_online = body.get("online_characters")
    if raw_online is None:
        active_char_keys = load_group_participants()
    else:
        if not isinstance(raw_online, list):
            return jsonify({"error": "online_characters 必须是角色列表"}), 400
        if any(not isinstance(cid, str) or cid not in GROUP_CHAT_ORDER for cid in raw_online):
            return jsonify({"error": "群聊成员里有未知角色"}), 400
        active_char_keys = _ordered_group_participants(raw_online)
    if not active_char_keys:
        return jsonify({"error": "群聊至少保留一位成员"}), 400

    recent_history = load_group_history(session_id)
    speakers = _rotated_group_speakers(active_char_keys, session_id)
    accumulated = []
    messages_out = []
    results = []

    for char_key in speakers:
        char = CHARACTERS[char_key]
        round_block = "\n".join(accumulated)
        combined_prompt = (
            "【最近的群聊】\n"
            f"{recent_history or '群里刚刚安静下来，还没有新的话题。'}\n\n"
            f"现在{USER_DISPLAY_NAME}没有发新消息，你们决定自己自然地接着聊一小轮。"
            "可以回应群里上一位，也可以顺着气氛换个轻松相关的话题；"
            f"不必把{USER_DISPLAY_NAME}当作唯一说话对象。不要总结聊天记录，不要替别人发言，"
            "也不要解释这是自动续聊或提到提示词。保持你自己的口吻，说一到三小段。"
        )
        if round_block:
            combined_prompt += (
                "\n\n【这一小轮里其他人刚说的话】\n"
                f"{round_block}\n\n现在轮到你（{char['name']}）自然接话。"
            )
        else:
            combined_prompt += f"\n\n你是这一小轮第一个开口的人（{char['name']}）。"

        reply, usage_metrics, tools_called = ask_character_group(
            char,
            combined_prompt,
            session_id,
            openrouter_max_tokens=2048,
            retry_openrouter_empty=True,
        )
        reply = strip_fake_action_text(reply, char["domain"]) or "(...)"
        msg_id = save_message(session_id, char["domain"], "model", reply)
        save_message_metrics(msg_id, char["domain"], usage_metrics)
        voice = _maybe_create_voice_message(
            session_id, char["domain"], tools_called
        )
        save_message_details(msg_id, tools_called)
        accumulated.append(f"{char['name']}：{reply}")
        message = {
            "id": msg_id,
            "session_id": session_id,
            "character_id": char["domain"],
            "character_name": char["name"],
            "role": "model",
            "content": reply,
            "metrics": usage_metrics,
            "tools_called": tools_called or [],
        }
        messages_out.append(message)
        if voice:
            messages_out.append({
                "id": voice["id"],
                "session_id": session_id,
                "character_id": char["domain"],
                "character_name": char["name"],
                "role": "model",
                "content": _voice_message_content(voice),
                "voice": voice,
            })
        results.append({
            "character_id": char["domain"],
            "name": char["name"],
            "reply": reply,
            "metrics": usage_metrics,
            "tools_called": tools_called or [],
            "voice": voice,
        })

    try:
        maybe_group_summary(session_id)
    except Exception as exc:
        app.logger.warning(f"[group_summary] autonomous round failed: {exc}")

    return jsonify({"mode": "continue", "messages": messages_out, "replies": results})


@app.route("/api/characters", methods=["GET"])
def list_characters():
    characters = {
        cid: {"name": c["name"], "model": c["model"], "avatar": c["avatar"]}
        for cid, c in CHARACTERS.items()
    }
    characters[USER_ID] = {
        "name": USER_DISPLAY_NAME,
        "model": "",
        "avatar": USER_AVATAR,
    }
    return jsonify(characters)


@app.route("/api/desire/state/<character_id>", methods=["GET"])
def get_desire_state(character_id):
    if character_id not in CHARACTERS:
        return jsonify({"error": "未知角色"}), 404
    return jsonify(desire_state_payload(character_id))


@app.route("/api/scene/<character_id>/clear", methods=["POST"])
def clear_character_scene(character_id):
    if character_id not in CHARACTERS:
        return jsonify({"error": "未知角色"}), 404
    if not _scene_feature_enabled():
        return jsonify({"ok": True, "scene": _empty_character_scene()})
    return jsonify({"ok": True, "scene": _clear_character_scene(character_id)})


@app.route("/api/summaries", methods=["GET"])
def api_summaries():
    out = []
    for key in CHARACTERS:
        char = CHARACTERS[key]
        summary = get_summary("default", char["domain"])
        out.append({
            "character_id": key,
            "name": char["name"],
            "avatar": char.get("avatar", ""),
            "summary": summary or "",
        })
    return jsonify({"summaries": out})


@app.route("/api/memory", methods=["GET"])
def api_memory_overview():
    backend_info = (
        MEMORY_SERVICE.describe()
        if callable(getattr(MEMORY_SERVICE, "describe", None))
        else {
            "name": type(MEMORY_SERVICE).__name__,
            "enabled": True,
            "capabilities": [
                name for name in ("read", "write", "admin", "enrichment", "decay", "legacy_import")
                if _memory_supports(name)
            ],
        }
    )
    characters = []
    for character_id, char in CHARACTERS.items():
        memories = (
            MEMORY_SERVICE.list_memories(character_id, limit=500)
            if _memory_supports("admin") else None
        )
        characters.append({
            "character_id": character_id,
            "name": char["name"],
            "avatar": char.get("avatar", ""),
            "count": len(memories) if memories is not None else None,
            "latest": memories[0] if memories else None,
        })
    return jsonify({
        "backend": backend_info,
        "characters": characters,
        "enrichment": {
            "metadata_configured": _memory_supports("enrichment") and MEMORY_ANALYZER.enabled,
            "embedding_configured": _memory_supports("enrichment") and MEMORY_EMBEDDINGS.enabled,
        },
    })


@app.route("/api/memory/re-enrich", methods=["POST"])
def api_re_enrich_memories():
    if not _memory_supports("enrichment"):
        return jsonify({"error": "当前记忆后端不支持内置打标"}), 501
    data = request.get_json(silent=True) or {}
    try:
        limit = max(1, min(int(data.get("limit_per_character", 30)), 100))
    except (TypeError, ValueError):
        return jsonify({"error": "补打标数量无效"}), 400
    queued = retry_pending_memory_enrichment(limit)
    return jsonify({
        "ok": True,
        "queued": queued,
        "metadata_configured": MEMORY_ANALYZER.enabled,
        "embedding_configured": MEMORY_EMBEDDINGS.enabled,
    })


@app.route("/api/memory/<character_id>", methods=["GET"])
def api_character_memories(character_id):
    if character_id not in CHARACTERS:
        return jsonify({"error": "未知角色"}), 404
    if not _memory_supports("admin"):
        return jsonify({"error": "当前记忆后端不提供内置管理列表"}), 501
    query = request.args.get("q", "")
    try:
        limit = int(request.args.get("limit", "100"))
    except ValueError:
        limit = 100
    memories = MEMORY_SERVICE.list_memories(
        character_id,
        query=query,
        include_archive=request.args.get("archive", "1") != "0",
        limit=limit,
    )
    return jsonify({
        "character_id": character_id,
        "name": CHARACTERS[character_id]["name"],
        "avatar": CHARACTERS[character_id].get("avatar", ""),
        "memories": memories,
    })


@app.route("/api/memory/<character_id>/<bucket_id>", methods=["PATCH", "DELETE"])
def api_character_memory_detail(character_id, bucket_id):
    if character_id not in CHARACTERS:
        return jsonify({"error": "未知角色"}), 404
    if not _memory_supports("admin"):
        return jsonify({"error": "当前记忆后端不提供内置编辑功能"}), 501
    if request.method == "DELETE":
        deleted = MEMORY_SERVICE.delete_memory(character_id, bucket_id)
        if not deleted:
            return jsonify({"error": "记忆不存在"}), 404
        try:
            MEMORY_EMBEDDINGS.delete(bucket_id)
        except Exception as exc:
            app.logger.warning(f"memory embedding cleanup failed ({bucket_id}): {exc}")
        _invalidate_breath_memory(character_id)
        return jsonify({"ok": True})

    data = request.get_json(silent=True) or {}
    allowed = {key: data[key] for key in (
        "content", "importance", "resolved", "pinned", "tags", "name"
    ) if key in data}
    try:
        updated = MEMORY_SERVICE.update_memory(character_id, bucket_id, **allowed)
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    if not updated:
        return jsonify({"error": "记忆不存在"}), 404
    _invalidate_breath_memory(character_id)
    return jsonify({"memory": updated})


@app.route("/api/memory/import-legacy", methods=["POST"])
def api_import_legacy_memory():
    if not _memory_supports("legacy_import"):
        return jsonify({"error": "当前记忆后端不支持旧 Ombre 迁移"}), 501
    data = request.get_json(silent=True) or {}
    try:
        result = MEMORY_SERVICE.import_legacy(data.get("url", ""), data.get("password", ""))
    except LegacyImportError as exc:
        return jsonify({"error": str(exc)}), 400
    for character_id in CHARACTERS:
        _invalidate_breath_memory(character_id)
    return jsonify({"ok": True, **result})


@app.route("/api/memory/import-files", methods=["POST"])
def api_import_memory_files():
    if not _memory_supports("write"):
        return jsonify({"error": "当前记忆后端不支持写入"}), 501

    raw_fallback_owner = str(request.form.get("fallback_character", "") or "").strip()
    fallback_owner = _memory_import_owner(raw_fallback_owner)
    if raw_fallback_owner and not fallback_owner:
        return jsonify({"error": "兜底角色不存在"}), 400

    uploads = [upload for upload in request.files.getlist("files") if upload.filename]
    if not uploads:
        return jsonify({"error": "请选择 JSON 或 TXT 文件"}), 400
    if len(uploads) > MAX_MEMORY_IMPORT_FILES:
        return jsonify({"error": f"一次最多导入 {MAX_MEMORY_IMPORT_FILES} 个文件"}), 400

    records = []
    file_results = []
    total_unassigned = 0
    total_invalid = 0
    total_bytes = 0
    parse_errors = []
    for upload in uploads:
        original_name = os.path.basename(upload.filename)
        extension = os.path.splitext(original_name)[1].lower()
        if extension not in {".json", ".txt"}:
            parse_errors.append(f"{original_name}：只支持 JSON / TXT")
            continue
        raw = upload.read(MAX_MEMORY_IMPORT_BYTES + 1)
        total_bytes += len(raw)
        if len(raw) > MAX_MEMORY_IMPORT_BYTES:
            parse_errors.append(f"{original_name}：文件不能超过 5MB")
            continue
        if total_bytes > 20 * 1024 * 1024:
            return jsonify({"error": "一次导入的文件合计不能超过 20MB"}), 400
        try:
            text, _encoding = _decode_text_upload(raw)
            file_owner = _memory_import_owner_from_filename(original_name) or fallback_owner
            if extension == ".json":
                payload = json.loads(text)
                parsed, unassigned, invalid = _parse_json_memory_import(payload, file_owner)
            else:
                parsed, unassigned, invalid = _parse_txt_memory_import(text, file_owner)
        except json.JSONDecodeError as exc:
            parse_errors.append(f"{original_name}：JSON 格式不完整（第 {exc.lineno} 行）")
            continue
        except ValueError as exc:
            parse_errors.append(f"{original_name}：{str(exc).replace('TXT', '文件')}")
            continue

        for record in parsed:
            record["_import_file"] = original_name
        records.extend(parsed)
        total_unassigned += unassigned
        total_invalid += invalid
        file_results.append({
            "name": original_name,
            "eligible": len(parsed),
            "unassigned": unassigned,
            "invalid": invalid,
        })

    if len(records) > MAX_MEMORY_IMPORT_RECORDS:
        return jsonify({
            "error": f"一次最多导入 {MAX_MEMORY_IMPORT_RECORDS} 条记忆，当前识别到 {len(records)} 条"
        }), 400
    if not records:
        message = parse_errors[0] if parse_errors else "没有识别到可导入的记忆"
        if total_unassigned:
            message = "有记忆没有对应角色，请用 char1–char6 字段、文件名，或选择兜底角色"
        return jsonify({
            "error": message,
            "eligible": 0,
            "unassigned": total_unassigned,
            "invalid": total_invalid,
            "files": file_results,
        }), 400

    imported = 0
    skipped = 0
    write_errors = 0
    imported_by_character = {character_id: 0 for character_id in CHARACTERS}
    touched_characters = set()
    for record in records:
        owner_id = record["owner_id"]
        content = record["content"]
        digest = hashlib.sha256(
            f"{owner_id}\0{content}".encode("utf-8")
        ).hexdigest()
        metadata = {
            key: value for key, value in record.items()
            if key not in {"owner_id", "content", "_import_file"} and value not in (None, "")
        }
        metadata.update({
            "source": "file_import",
            "source_key": f"file-import:{digest}",
            "enrichment_status": "pending" if MEMORY_ANALYZER.enabled else "unconfigured",
            "embedding_status": "pending" if MEMORY_EMBEDDINGS.enabled else "unconfigured",
        })
        try:
            bucket_id, created = MEMORY_SERVICE.save(content, owner_id, **metadata)
            touched_characters.add(owner_id)
            if created:
                imported += 1
                imported_by_character[owner_id] += 1
                _queue_memory_enrichment(bucket_id, owner_id, content)
            else:
                skipped += 1
        except Exception as exc:
            write_errors += 1
            app.logger.warning(
                "memory file import failed (%s/%s): %s",
                record.get("_import_file", "unknown"), owner_id, exc,
            )

    for character_id in touched_characters:
        _invalidate_breath_memory(character_id)
    return jsonify({
        "ok": True,
        "eligible": len(records),
        "imported": imported,
        "skipped": skipped,
        "unassigned": total_unassigned,
        "invalid": total_invalid,
        "errors": write_errors + len(parse_errors),
        "parse_errors": parse_errors[:5],
        "by_character": {
            character_id: count
            for character_id, count in imported_by_character.items() if count
        },
        "files": file_results,
    })


# ── 一起听 · 网易云供应商 ──────────────────────────────────
class NeteaseMusicError(RuntimeError):
    pass


_NETEASE_AUDIO_URL_CACHE = {}
_NETEASE_AUDIO_URL_LOCK = threading.Lock()


def _netease_headers():
    headers = {
        "Referer": "https://music.163.com/",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 Chrome/124 Safari/537.36"
        ),
    }
    if NETEASE_MUSIC_U:
        headers["Cookie"] = f"MUSIC_U={NETEASE_MUSIC_U}"
    return headers


def _netease_json(url, *, data=None, timeout=12):
    try:
        response = requests.post(url, data=data, headers=_netease_headers(), timeout=timeout) \
            if data is not None else requests.get(url, headers=_netease_headers(), timeout=timeout)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise NeteaseMusicError("网易云这次没有接上") from exc
    if not isinstance(payload, dict):
        raise NeteaseMusicError("网易云返回的歌曲资料没有认出来")
    return payload


def _netease_song_payload(song, cover=""):
    source_id = str(song.get("id") or "")
    if not source_id.isdigit():
        return None
    artists = song.get("artists") or song.get("ar") or []
    album = song.get("album") or song.get("al") or {}
    artist = ", ".join(
        str(item.get("name") or "").strip()
        for item in artists if isinstance(item, dict) and item.get("name")
    )
    artwork = str(cover or album.get("picUrl") or "")
    duration_ms = song.get("duration") if song.get("duration") is not None else song.get("dt", 0)
    try:
        duration = max(0, min(float(duration_ms or 0) / 1000, 24 * 3600))
    except (TypeError, ValueError):
        duration = 0
    return {
        "id": f"netease:{source_id}",
        "source": "netease",
        "source_id": source_id,
        "name": str(song.get("name") or "未命名歌曲")[:300],
        "artist": artist[:300],
        "album": str(album.get("name") or "")[:300],
        "duration": duration,
        "artwork_url": artwork[:1200],
        "audio_url": f"/api/music/netease/audio/{source_id}",
        "has_lyrics": False,
        "synced": False,
    }


def _netease_search_songs(query, limit=10):
    keyword = str(query or "").strip()[:120]
    if not keyword:
        return []
    limit = max(1, min(int(limit or 10), 20))
    payload = _netease_json(
        "https://music.163.com/api/search/get",
        data={"s": keyword, "type": "1", "limit": str(limit), "offset": "0"},
    )
    songs = (payload.get("result") or {}).get("songs") or []
    source_ids = [str(song.get("id")) for song in songs if str(song.get("id") or "").isdigit()]
    covers = {}
    if source_ids:
        try:
            details = _netease_json(
                "https://music.163.com/api/song/detail?ids=[" + ",".join(source_ids) + "]"
            )
            for song in details.get("songs") or []:
                album = song.get("album") or song.get("al") or {}
                if song.get("id") and album.get("picUrl"):
                    covers[str(song["id"])] = album["picUrl"]
        except NeteaseMusicError:
            pass
    result = []
    for song in songs:
        track = _netease_song_payload(song, covers.get(str(song.get("id")), ""))
        if track:
            result.append(track)
    return result


def _netease_account_profile():
    if not NETEASE_MUSIC_U:
        raise NeteaseMusicError("还没有接入网易云账号")
    payload = _netease_json("https://music.163.com/api/nuser/account/get")
    profile = payload.get("profile") or {}
    user_id = str(profile.get("userId") or "")
    if not user_id.isdigit():
        raise NeteaseMusicError("网易云登录已经失效，请更新 MUSIC_U")
    return {
        "user_id": user_id,
        "nickname": str(profile.get("nickname") or "网易云账号")[:100],
        "avatar_url": str(profile.get("avatarUrl") or "")[:1200],
    }


def _netease_user_playlists(user_id, limit=100):
    user_id = str(user_id or "")
    if not user_id.isdigit():
        raise NeteaseMusicError("网易云账号编号不对")
    limit = max(1, min(int(limit or 100), 200))
    payload = _netease_json(
        f"https://music.163.com/api/user/playlist/?uid={user_id}&limit={limit}&offset=0"
    )
    playlists = []
    for item in payload.get("playlist") or []:
        playlist_id = str(item.get("id") or "")
        if not playlist_id.isdigit():
            continue
        creator = item.get("creator") or {}
        playlists.append({
            "id": playlist_id,
            "name": str(item.get("name") or "未命名歌单")[:200],
            "cover_url": str(item.get("coverImgUrl") or "")[:1200],
            "track_count": max(0, int(item.get("trackCount") or 0)),
            "creator_name": str(creator.get("nickname") or "")[:100],
            "subscribed": bool(item.get("subscribed")),
        })
    return playlists


def _netease_playlist_songs(playlist_id):
    playlist_id = str(playlist_id or "")
    if not playlist_id.isdigit():
        raise NeteaseMusicError("网易云歌单编号不对")
    payload = _netease_json(
        f"https://music.163.com/api/v6/playlist/detail?id={playlist_id}&n=1000&s=8"
    )
    playlist = payload.get("playlist") or {}
    tracks = playlist.get("tracks") or []
    songs = []
    for item in tracks:
        track = _netease_song_payload(item)
        if track:
            songs.append(track)
    return {
        "id": playlist_id,
        "name": str(playlist.get("name") or "歌单")[:200],
        "cover_url": str(playlist.get("coverImgUrl") or "")[:1200],
        "songs": songs,
    }


def _netease_fetch_lyrics(source_id):
    payload = _netease_json(
        f"https://music.163.com/api/song/lyric?id={source_id}&lv=1&tv=-1"
    )
    lyrics = _normalize_music_lyrics((payload.get("lrc") or {}).get("lyric"))
    translated = _normalize_music_lyrics((payload.get("tlyric") or {}).get("lyric"))
    return lyrics, translated


def _netease_track_row(conn, source_id):
    return conn.execute(
        "SELECT source_id,name,artist,album,duration_seconds,artwork_url,lyrics,"
        "translated_lyrics,updated_at FROM music_netease_tracks WHERE source_id=?",
        (str(source_id),),
    ).fetchone()


def _netease_track_payload(row):
    source_id = str(row[0])
    return {
        "id": f"netease:{source_id}", "source": "netease", "source_id": source_id,
        "name": row[1], "artist": row[2], "album": row[3], "duration": row[4],
        "artwork_url": f"/api/music/netease/artwork/{source_id}" if row[5] else "",
        "audio_url": f"/api/music/netease/audio/{source_id}",
        "has_lyrics": bool(row[6] or row[7]), "synced": False,
    }


def _prepare_netease_track(source_id, fallback=None):
    source_id = str(source_id or "")
    if not source_id.isdigit():
        raise NeteaseMusicError("网易云歌曲编号不对")
    try:
        detail = _netease_json(f"https://music.163.com/api/song/detail?ids=[{source_id}]")
        song = (detail.get("songs") or [None])[0]
    except NeteaseMusicError:
        song = None
    track = _netease_song_payload(song or {}) if song else None
    if not track and isinstance(fallback, dict) and str(fallback.get("source_id") or "") == source_id:
        try:
            fallback_duration = max(0, min(float(fallback.get("duration") or 0), 24 * 3600))
        except (TypeError, ValueError):
            fallback_duration = 0
        track = {
            "id": f"netease:{source_id}", "source": "netease", "source_id": source_id,
            "name": str(fallback.get("name") or "未命名歌曲")[:300],
            "artist": str(fallback.get("artist") or "")[:300],
            "album": str(fallback.get("album") or "")[:300],
            "duration": fallback_duration,
            "artwork_url": str(fallback.get("artwork_url") or "")[:1200],
        }
    if not track:
        raise NeteaseMusicError("没有找到这首网易云歌曲")
    try:
        lyrics, translated = _netease_fetch_lyrics(source_id)
    except NeteaseMusicError:
        lyrics, translated = "", ""
    original_artwork = track.get("artwork_url", "")
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO music_netease_tracks "
        "(source_id,name,artist,album,duration_seconds,artwork_url,lyrics,translated_lyrics) "
        "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET "
        "name=excluded.name,artist=excluded.artist,album=excluded.album,"
        "duration_seconds=excluded.duration_seconds,artwork_url=excluded.artwork_url,"
        "lyrics=excluded.lyrics,translated_lyrics=excluded.translated_lyrics,"
        "updated_at=CURRENT_TIMESTAMP",
        (
            source_id, track["name"], track.get("artist", ""), track.get("album", ""),
            track.get("duration", 0), original_artwork, lyrics, translated,
        ),
    )
    conn.commit()
    row = _netease_track_row(conn, source_id)
    conn.close()
    return _netease_track_payload(row)


def _netease_resolve_audio_url(source_id, *, refresh=False):
    source_id = str(source_id or "")
    if not source_id.isdigit():
        raise NeteaseMusicError("网易云歌曲编号不对")
    if not refresh:
        with _NETEASE_AUDIO_URL_LOCK:
            cached = _NETEASE_AUDIO_URL_CACHE.get(source_id)
        if cached and cached[1] > time.monotonic():
            return cached[0]
    bitrates = [NETEASE_BITRATE]
    if 128000 not in bitrates:
        bitrates.append(128000)
    for bitrate in bitrates:
        payload = _netease_json(
            f"https://music.163.com/api/song/enhance/player/url?ids=[{source_id}]&br={bitrate}"
        )
        item = (payload.get("data") or [{}])[0] or {}
        audio_url = str(item.get("url") or "")
        if audio_url.startswith("http://"):
            audio_url = "https://" + audio_url[len("http://"):]
        if audio_url.startswith("https://"):
            try:
                lifetime = max(60, min(int(item.get("expi") or 600) - 30, 900))
            except (TypeError, ValueError):
                lifetime = 570
            with _NETEASE_AUDIO_URL_LOCK:
                _NETEASE_AUDIO_URL_CACHE[source_id] = (
                    audio_url, time.monotonic() + lifetime,
                )
            return audio_url
    if NETEASE_MUSIC_U:
        raise NeteaseMusicError("账号没有拿到这首歌的播放权限")
    raise NeteaseMusicError("这首歌需要网易云账号或会员权限")


def _netease_audio_url_candidates(audio_url):
    candidates = [audio_url]
    fallback = re.sub(
        r"(?<=//)m\d+\.music\.126\.net", "m701.music.126.net", audio_url,
        count=1,
    )
    if fallback != audio_url:
        candidates.append(fallback)
    return candidates


def _netease_open_audio(source_id, requested_range=""):
    last_status = None
    for attempt in range(2):
        audio_url = _netease_resolve_audio_url(source_id, refresh=bool(attempt))
        for candidate in _netease_audio_url_candidates(audio_url):
            headers = {
                key: value for key, value in _netease_headers().items()
                if key.lower() != "cookie"
            }
            if requested_range:
                headers["Range"] = requested_range
            try:
                upstream = requests.get(
                    candidate, headers=headers, stream=True,
                    allow_redirects=True, timeout=(8, 45),
                )
            except requests.RequestException:
                continue
            if upstream.status_code in {200, 206}:
                return upstream
            last_status = upstream.status_code
            upstream.close()
        with _NETEASE_AUDIO_URL_LOCK:
            _NETEASE_AUDIO_URL_CACHE.pop(str(source_id), None)
    if last_status in {401, 403}:
        raise NeteaseMusicError("网易云账号没有拿到这首歌的播放权限")
    if last_status == 404:
        raise NeteaseMusicError("网易云返回的音频地址已经失效")
    raise NeteaseMusicError("网易云音频线路暂时没有接上")


@app.route("/api/music/netease/status")
def netease_music_status():
    if not NETEASE_MUSIC_U:
        return jsonify({
            "available": True, "account_configured": False,
            "account_valid": False, "bitrate": NETEASE_BITRATE,
        })
    try:
        profile = _netease_account_profile()
    except NeteaseMusicError as exc:
        return jsonify({
            "available": True, "account_configured": True,
            "account_valid": False, "error": str(exc), "bitrate": NETEASE_BITRATE,
        })
    return jsonify({
        "available": True, "account_configured": True, "account_valid": True,
        "profile": profile, "bitrate": NETEASE_BITRATE,
    })


@app.route("/api/music/netease/playlists")
def netease_music_playlists():
    try:
        profile = _netease_account_profile()
        playlists = _netease_user_playlists(profile["user_id"])
    except (NeteaseMusicError, TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 401
    return jsonify({"profile": profile, "playlists": playlists})


@app.route("/api/music/netease/playlists/<playlist_id>")
def netease_music_playlist(playlist_id):
    try:
        profile = _netease_account_profile()
        allowed_ids = {
            item["id"] for item in _netease_user_playlists(profile["user_id"])
        }
        if str(playlist_id) not in allowed_ids:
            return jsonify({"error": "这个歌单不在当前网易云账号里"}), 404
        playlist = _netease_playlist_songs(playlist_id)
    except (NeteaseMusicError, TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify({"playlist": playlist})


@app.route("/api/music/netease/search")
def search_netease_music():
    try:
        songs = _netease_search_songs(request.args.get("q", ""), request.args.get("limit", 10))
    except (NeteaseMusicError, TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify({"songs": songs, "account_configured": bool(NETEASE_MUSIC_U)})


@app.route("/api/music/netease/tracks/<source_id>", methods=["GET", "POST"])
def netease_music_track(source_id):
    conn = sqlite3.connect(DB_PATH)
    row = _netease_track_row(conn, source_id)
    conn.close()
    if request.method == "GET" and row:
        return jsonify({"track": _netease_track_payload(row)})
    fallback = request.get_json(silent=True) or {} if request.method == "POST" else None
    try:
        track = _prepare_netease_track(source_id, fallback)
    except (NeteaseMusicError, TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify({"track": track})


@app.route("/api/music/netease/audio/<source_id>")
def stream_netease_music(source_id):
    try:
        upstream = _netease_open_audio(
            source_id, request.headers.get("Range", "").strip(),
        )
    except NeteaseMusicError as exc:
        return jsonify({"error": str(exc)}), 502

    def generate():
        try:
            for chunk in upstream.iter_content(chunk_size=64 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    response_headers = {
        "Cache-Control": "private, max-age=300",
        "X-Accel-Buffering": "no",
    }
    for header in (
        "Content-Length", "Content-Range", "Accept-Ranges",
        "ETag", "Last-Modified",
    ):
        value = upstream.headers.get(header)
        if value:
            response_headers[header] = value
    return Response(
        stream_with_context(generate()),
        status=upstream.status_code,
        content_type=upstream.headers.get("Content-Type", "audio/mpeg"),
        headers=response_headers,
    )


@app.route("/api/music/netease/audio/<source_id>/status")
def netease_music_audio_status(source_id):
    try:
        upstream = _netease_open_audio(source_id, "bytes=0-1")
    except NeteaseMusicError as exc:
        return jsonify({"playable": False, "error": str(exc)}), 502
    upstream.close()
    return jsonify({"playable": True})


@app.route("/api/music/netease/artwork/<source_id>")
def netease_music_artwork(source_id):
    conn = sqlite3.connect(DB_PATH)
    row = _netease_track_row(conn, source_id)
    conn.close()
    artwork_url = str(row[5] or "") if row else ""
    if not artwork_url.startswith("https://"):
        return jsonify({"error": "这首歌没有封面"}), 404
    try:
        upstream = requests.get(artwork_url, headers=_netease_headers(), timeout=12)
        upstream.raise_for_status()
    except requests.RequestException:
        return jsonify({"error": "网易云封面没有接上"}), 502
    response = Response(
        upstream.content,
        content_type=upstream.headers.get("Content-Type", "image/jpeg"),
    )
    response.headers["Cache-Control"] = "public, max-age=86400"
    return response


# ── 一起听 · 同步曲库 ──────────────────────────────────────
def _music_library_payload(row):
    try:
        added_at = int(datetime.fromisoformat(str(row[11])).replace(tzinfo=timezone.utc).timestamp() * 1000)
    except (TypeError, ValueError):
        added_at = 0
    return {
        "id": row[0], "name": row[1], "artist": row[2], "album": row[3],
        "duration": row[4], "size": row[5], "type": row[6],
        "audio_url": f"/api/music/library/{row[0]}/audio",
        "artwork_url": f"/api/music/library/{row[0]}/artwork" if row[8] else "",
        "has_lyrics": bool(row[10]),
        "metadata_scanned": True, "added_at": added_at, "synced": True,
    }


def _music_library_row(conn, track_id):
    return conn.execute(
        "SELECT id,name,artist,album,duration_seconds,size_bytes,mime_type,"
        "audio_filename,artwork_filename,artwork_mime,lyrics,created_at "
        "FROM music_library_tracks WHERE id=?",
        (track_id,),
    ).fetchone()


def _music_storage_path(filename):
    safe_name = os.path.basename(str(filename or ""))
    if not safe_name or safe_name != filename:
        return None
    return os.path.join(MUSIC_LIBRARY_ROOT, safe_name)


def _normalize_music_lyrics(value):
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()[:50000]


def _save_music_artwork(artwork, file_token):
    if not artwork or not artwork.filename:
        return "", ""
    stream = artwork.stream
    try:
        stream.seek(0)
        signature = stream.read(4)
        stream.seek(0)
    except (AttributeError, OSError):
        signature = b""
    candidate_mime = str(artwork.mimetype or "").lower()
    is_tiff = (
        candidate_mime in TIFF_ARTWORK_MIMES
        or signature in (b"II\x2a\x00", b"MM\x00\x2a")
    )
    if is_tiff:
        filename = f"{file_token}-cover.png"
        path = _music_storage_path(filename)
        try:
            with Image.open(stream) as source:
                if (
                    source.width > MAX_ARTWORK_EDGE
                    or source.height > MAX_ARTWORK_EDGE
                    or source.width * source.height > MAX_ARTWORK_PIXELS
                ):
                    raise ValueError("歌曲封面尺寸太大")
                source.load()
                source.thumbnail(
                    (NORMALIZED_ARTWORK_EDGE, NORMALIZED_ARTWORK_EDGE),
                    Image.Resampling.LANCZOS,
                )
                bands = source.getbands()
                normalized = source.convert("RGBA" if "A" in bands else "RGB")
                try:
                    normalized.save(path, format="PNG", optimize=True)
                finally:
                    normalized.close()
        except (UnidentifiedImageError, OSError) as exc:
            if path and os.path.exists(path):
                os.remove(path)
            raise ValueError("歌曲封面没有认出来") from exc
        finally:
            try:
                stream.seek(0)
            except (AttributeError, OSError):
                pass
        return filename, "image/png"

    suffix = ALLOWED_ARTWORK_MIMES.get(candidate_mime)
    if not suffix:
        return "", ""
    filename = f"{file_token}-cover.{suffix}"
    artwork.save(_music_storage_path(filename))
    return filename, candidate_mime


@app.route("/api/music/library", methods=["GET", "POST"])
def music_library():
    conn = sqlite3.connect(DB_PATH)
    if request.method == "GET":
        rows = conn.execute(
            "SELECT id,name,artist,album,duration_seconds,size_bytes,mime_type,"
            "audio_filename,artwork_filename,artwork_mime,lyrics,created_at "
            "FROM music_library_tracks ORDER BY created_at,id"
        ).fetchall()
        conn.close()
        response = jsonify({"tracks": [_music_library_payload(row) for row in rows]})
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    audio = request.files.get("audio")
    if not audio or not audio.filename:
        conn.close()
        return jsonify({"error": "没有收到歌曲文件"}), 400
    suffix = os.path.splitext(audio.filename)[1].lower().lstrip(".")
    if suffix not in ALLOWED_MUSIC_EXTENSIONS:
        conn.close()
        return jsonify({"error": "这个音频格式暂时放不了"}), 400
    track_id = str(request.form.get("track_id") or f"music:{uuid.uuid4().hex}")[:100]
    if not re.fullmatch(r"[A-Za-z0-9:_-]{1,100}", track_id):
        conn.close()
        return jsonify({"error": "歌曲编号格式不对"}), 400
    existing = _music_library_row(conn, track_id)
    if existing:
        existing_audio_path = _music_storage_path(existing[7])
        if existing_audio_path and os.path.isfile(existing_audio_path):
            artwork = request.files.get("artwork")
            artwork_filename = existing[8] or ""
            artwork_mime = existing[9] or ""
            if not artwork_filename and artwork and artwork.filename:
                try:
                    artwork_filename, artwork_mime = _save_music_artwork(
                        artwork, uuid.uuid4().hex,
                    )
                except ValueError as exc:
                    conn.close()
                    return jsonify({"error": str(exc)}), 400
            try:
                duration = max(0.0, min(float(request.form.get("duration") or existing[4] or 0), 24 * 3600))
            except (TypeError, ValueError):
                duration = float(existing[4] or 0)
            lyrics = _normalize_music_lyrics(
                request.form.get("lyrics") if "lyrics" in request.form else existing[10]
            )
            conn.execute(
                "UPDATE music_library_tracks SET name=?,artist=?,album=?,duration_seconds=?,"
                "artwork_filename=?,artwork_mime=?,lyrics=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (
                    str(request.form.get("name") or existing[1])[:300],
                    str(request.form.get("artist") or existing[2])[:300],
                    str(request.form.get("album") or existing[3])[:300],
                    duration, artwork_filename, artwork_mime, lyrics, track_id,
                ),
            )
            conn.commit()
            refreshed = _music_library_row(conn, track_id)
            conn.close()
            return jsonify({"track": _music_library_payload(refreshed), "existing": True})
        for filename in (existing[7], existing[8]):
            path = _music_storage_path(filename)
            if path and os.path.exists(path):
                os.remove(path)
        conn.execute("DELETE FROM music_library_tracks WHERE id=?", (track_id,))
        conn.commit()

    stream = audio.stream
    try:
        stream.seek(0, os.SEEK_END)
        size_bytes = stream.tell()
        stream.seek(0)
    except (AttributeError, OSError):
        size_bytes = int(audio.content_length or 0)
    if size_bytes <= 0 or size_bytes > MAX_MUSIC_BYTES:
        conn.close()
        return jsonify({"error": "单首歌曲需要小于 180MB"}), 413

    os.makedirs(MUSIC_LIBRARY_ROOT, exist_ok=True)
    file_token = uuid.uuid4().hex
    audio_filename = f"{file_token}.{suffix}"
    audio_path = _music_storage_path(audio_filename)
    artwork = request.files.get("artwork")
    artwork_filename = ""
    artwork_mime = ""
    try:
        audio.save(audio_path)
        if artwork and artwork.filename:
            artwork_filename, artwork_mime = _save_music_artwork(artwork, file_token)
        mime_type = audio.mimetype or mimetypes.guess_type(audio.filename)[0] or "application/octet-stream"
        lyrics = _normalize_music_lyrics(request.form.get("lyrics"))
        try:
            duration = max(0.0, min(float(request.form.get("duration") or 0), 24 * 3600))
        except (TypeError, ValueError):
            duration = 0.0
        conn.execute(
            "INSERT INTO music_library_tracks "
            "(id,name,artist,album,duration_seconds,size_bytes,mime_type,audio_filename,"
            "artwork_filename,artwork_mime,lyrics) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                track_id, str(request.form.get("name") or os.path.splitext(audio.filename)[0])[:300],
                str(request.form.get("artist") or "本地音乐")[:300],
                str(request.form.get("album") or "")[:300], duration, size_bytes,
                mime_type, audio_filename, artwork_filename, artwork_mime, lyrics,
            ),
        )
        conn.commit()
        row = _music_library_row(conn, track_id)
    except ValueError as exc:
        for path in (audio_path, _music_storage_path(artwork_filename)):
            if path and os.path.exists(path):
                os.remove(path)
        conn.close()
        return jsonify({"error": str(exc)}), 400
    except Exception:
        for path in (audio_path, _music_storage_path(artwork_filename)):
            if path and os.path.exists(path):
                os.remove(path)
        conn.close()
        raise
    conn.close()
    return jsonify({"track": _music_library_payload(row)}), 201


@app.route("/api/music/library/<track_id>", methods=["DELETE"])
def delete_music_library_track(track_id):
    conn = sqlite3.connect(DB_PATH)
    row = _music_library_row(conn, track_id)
    if not row:
        conn.close()
        return jsonify({"ok": True})
    conn.execute("DELETE FROM music_library_tracks WHERE id=?", (track_id,))
    conn.execute(
        "UPDATE music_rooms SET song_id='',song_name='',artist_name='',album_name='',"
        "artwork_url='',duration_ms=0,position_ms=0,playback_state='paused',started_at=NULL,"
        "updated_at=CURRENT_TIMESTAMP WHERE id=1 AND song_id=?",
        (track_id,),
    )
    conn.commit()
    conn.close()
    for filename in (row[7], row[8]):
        path = _music_storage_path(filename)
        if path and os.path.exists(path):
            os.remove(path)
    return jsonify({"ok": True})


@app.route("/api/music/library/<track_id>/audio")
def stream_music_library_track(track_id):
    conn = sqlite3.connect(DB_PATH)
    row = _music_library_row(conn, track_id)
    conn.close()
    path = _music_storage_path(row[7]) if row else None
    if not path or not os.path.isfile(path):
        return jsonify({"error": "歌曲文件不见了"}), 404
    download_name = secure_filename(row[1]) or "music"
    return send_file(
        path, mimetype=row[6], conditional=True, as_attachment=False,
        download_name=f"{download_name}{os.path.splitext(path)[1]}", max_age=3600,
    )


@app.route("/api/music/library/<track_id>/artwork")
def music_library_artwork(track_id):
    conn = sqlite3.connect(DB_PATH)
    row = _music_library_row(conn, track_id)
    conn.close()
    path = _music_storage_path(row[8]) if row else None
    if not path or not os.path.isfile(path):
        return jsonify({"error": "歌曲封面不见了"}), 404
    return send_file(path, mimetype=row[9] or None, conditional=True, max_age=86400)


@app.route("/api/music/room", methods=["GET", "PUT"])
def music_room():
    conn = sqlite3.connect(DB_PATH)
    if request.method == "GET":
        payload = _music_room_payload(conn)
        conn.close()
        return jsonify({"configured": True, "source": "netease", "room": payload})

    data = request.get_json(silent=True) or {}
    if data.get("reset"):
        conn.execute(
            "UPDATE music_rooms SET song_id='',song_name='',artist_name='',album_name='',"
            "artwork_url='',duration_ms=0,position_ms=0,playback_state='paused',"
            "started_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=1"
        )
        conn.commit()
        payload = _music_room_payload(conn)
        conn.close()
        return jsonify({"room": payload})

    current = conn.execute("SELECT song_id,started_at FROM music_rooms WHERE id=1").fetchone()
    song_id = str(data.get("song_id", current[0] or ""))[:180]
    playback_state = str(data.get("playback_state", "paused"))
    if playback_state not in {"playing", "paused", "stopped", "loading"}:
        conn.close()
        return jsonify({"error": "播放器状态不对"}), 400
    try:
        duration_ms = max(0, min(int(data.get("duration_ms", 0)), 24 * 3600 * 1000))
        position_ms = max(0, min(int(data.get("position_ms", 0)), duration_ms or 24 * 3600 * 1000))
    except (TypeError, ValueError):
        conn.close()
        return jsonify({"error": "播放位置不对"}), 400
    distance_value = data.get("distance_km", "__missing__")
    if distance_value == "__missing__":
        distance_sql = "distance_km"
        distance_params = []
    elif distance_value in (None, ""):
        distance_sql = "NULL"
        distance_params = []
    else:
        try:
            distance_km = round(max(0, min(float(distance_value), 20050)), 1)
        except (TypeError, ValueError):
            conn.close()
            return jsonify({"error": "距离格式不对"}), 400
        distance_sql = "?"
        distance_params = [distance_km]
    started_at = current[1]
    if song_id and not started_at:
        started_at = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE music_rooms SET song_id=?,song_name=?,artist_name=?,album_name=?,"
        "artwork_url=?,duration_ms=?,position_ms=?,playback_state=?,distance_km="
        + distance_sql + ",started_at=?,updated_at=CURRENT_TIMESTAMP WHERE id=1",
        [
            song_id, str(data.get("song_name", ""))[:300],
            str(data.get("artist_name", ""))[:300], str(data.get("album_name", ""))[:300],
            str(data.get("artwork_url", ""))[:1200], duration_ms, position_ms,
            playback_state,
        ] + distance_params + [started_at],
    )
    conn.commit()
    payload = _music_room_payload(conn)
    conn.close()
    return jsonify({"room": payload})


@app.route("/api/music/room/participants", methods=["PUT"])
def update_music_participants():
    data = request.get_json(silent=True) or {}
    raw = data.get("character_ids")
    participants = _normalize_music_participants(raw)
    if not isinstance(raw, list) or len(participants) > 2 or len(participants) != len(set(raw)):
        return jsonify({"error": "最多选两位一起听"}), 400
    conn = sqlite3.connect(DB_PATH)
    previous = [
        row[0] for row in conn.execute(
            "SELECT character_id FROM music_room_participants "
            "WHERE room_id=1 ORDER BY joined_at, rowid"
        ).fetchall()
    ]
    roster_changed = previous != participants
    conn.execute("DELETE FROM music_room_participants WHERE room_id=1")
    for character_id in participants:
        conn.execute(
            "INSERT INTO music_room_participants(room_id,character_id) VALUES (1,?)",
            (character_id,),
        )
    if roster_changed:
        conn.execute("DELETE FROM music_room_messages WHERE room_id=1")
        conn.execute("DELETE FROM music_room_commands WHERE room_id=1")
    conn.commit()
    room = _music_room_payload(conn)
    conn.close()
    return jsonify({"participants": room["participants"], "room": room})


def _music_generate_replies(event_text):
    conn = sqlite3.connect(DB_PATH)
    room = _music_room_payload(conn, include_messages=False)
    participants = [item["id"] for item in room["participants"]]
    history_rows = conn.execute(
        "SELECT author_id,content,details_json FROM music_room_messages "
        "WHERE room_id=1 ORDER BY id DESC LIMIT 12"
    ).fetchall()[::-1]
    raw_lyrics = ""
    if str(room["song_id"] or "").startswith("netease:"):
        source_id = str(room["song_id"]).split(":", 1)[1]
        lyrics_row = conn.execute(
            "SELECT lyrics,translated_lyrics FROM music_netease_tracks WHERE source_id=?",
            (source_id,),
        ).fetchone()
        if lyrics_row:
            raw_lyrics = lyrics_row[0] or ""
            if lyrics_row[1]:
                raw_lyrics += f"\n{lyrics_row[1]}"
    elif room["song_id"]:
        lyrics_row = conn.execute(
            "SELECT lyrics FROM music_library_tracks WHERE id=?",
            (room["song_id"],),
        ).fetchone()
        raw_lyrics = lyrics_row[0] if lyrics_row else ""
    conn.close()
    history_lines = []
    for author_id, content, details_json in history_rows:
        speaker = USER_DISPLAY_NAME if author_id == USER_ID else CHARACTERS.get(author_id, {}).get("name", author_id)
        suffix = ""
        try:
            details = json.loads(details_json or "{}")
        except (TypeError, json.JSONDecodeError):
            details = {}
        if details.get("tool") == "music_player_control":
            output = details.get("output") or {}
            suffix = (
                f"（播放器指令 {details.get('input', {}).get('action', '')}："
                f"{output.get('status', 'unknown')}，{output.get('message', '')}）"
            )
        history_lines.append(f"{speaker}：{content}{suffix}")
    history = "\n".join(history_lines)
    duration = max(0, int(room["duration_ms"] or 0) // 1000)
    position = max(0, int(room["position_ms"] or 0) // 1000)
    song_line = (
        f"《{room['song_name']}》 - {room['artist_name']}，"
        f"播放到 {position // 60}:{position % 60:02d} / {duration // 60}:{duration % 60:02d}，"
        f"当前{'正在播放' if room['playback_state'] == 'playing' else '已暂停'}。"
        if room["song_id"] else "房间里还没有选歌。"
    )
    lyrics_context = _music_lyrics_context(raw_lyrics, position)
    replies = []
    for character_id in participants:
        char = CHARACTERS[character_id]
        prompt = (
            f"你正在 Becoming 的‘一起听’房间陪{USER_DISPLAY_NAME}听音乐。\n"
            f"【当前播放器】{song_line}\n"
            f"{lyrics_context}\n"
            "你没有音频输入，不会真正听见旋律、人声或声音细节。你只能阅读上面明确提供的歌词；"
            "若歌词未提供，必须坦白看不到，绝不能根据歌名、常识或上下文编造歌词。"
            "带时间歌词可以按当前进度谈附近几句；无时间轴歌词不能声称当前正在唱哪句。"
            "自然回应一两句即可，每次回复不超过90个字，不要写姓名前缀。你可以保持安静。"
            "如果你真想控制播放器，可以使用播放器工具；如果你想找一首具体的歌，必须先搜索、阅读候选，"
            "再选择候选中的歌曲播放。不要频繁乱按，也不要为了展示工具而点歌。\n"
            f"【房间最近对话】\n{history or '还没有说话。'}\n"
            f"【刚发生的事】{event_text}"
        )
        reply, action, details = ask_music_companion(char, prompt)
        reply = _limit_music_reply(reply)
        conn = sqlite3.connect(DB_PATH)
        command_id = None
        if action:
            action_arguments = details.get("input") if isinstance(details, dict) else {}
            if not isinstance(action_arguments, dict):
                action_arguments = {}
            cursor = conn.execute(
                "INSERT INTO music_room_commands"
                "(room_id,character_id,action,arguments_json) VALUES (1,?,?,?)",
                (character_id, action, json.dumps(action_arguments, ensure_ascii=False)),
            )
            command_id = cursor.lastrowid
            details.setdefault("output", {})["command_id"] = command_id
        cursor = conn.execute(
            "INSERT INTO music_room_messages(room_id,author_id,content,event_type,details_json) "
            "VALUES (1,?,?,?,?)",
            (character_id, reply, "companion", json.dumps(details, ensure_ascii=False)),
        )
        conn.commit()
        replies.append({"id": cursor.lastrowid, "character_id": character_id, "command_id": command_id})
        conn.close()
    return replies


@app.route("/api/music/room/messages", methods=["POST"])
def post_music_message():
    data = request.get_json(silent=True) or {}
    content = str(data.get("content") or "").strip()[:2000]
    if not content:
        return jsonify({"error": "说点什么再发呀"}), 400
    conn = sqlite3.connect(DB_PATH)
    has_participants = bool(_music_participant_payload(conn))
    conn.execute(
        "INSERT INTO music_room_messages(room_id,author_id,content,event_type) VALUES (1,?,?,?)",
        (USER_ID, content, "comment"),
    )
    conn.commit()
    conn.close()
    if has_participants:
        _music_generate_replies(f"{USER_DISPLAY_NAME}说：{content}")
    conn = sqlite3.connect(DB_PATH)
    payload = _music_room_payload(conn)
    conn.close()
    return jsonify({"room": payload})


@app.route("/api/music/room/react", methods=["POST"])
def react_music_room():
    data = request.get_json(silent=True) or {}
    event_type = str(data.get("event_type") or "")
    event_map = {
        "track_started": f"{USER_DISPLAY_NAME}选了这首歌并开始播放。",
        "track_changed": "房间换了一首歌。",
        "invite": f"{USER_DISPLAY_NAME}把你喊进了一起听房间。",
    }
    if event_type not in event_map:
        return jsonify({"error": "房间事件不对"}), 400
    _music_generate_replies(event_map[event_type])
    conn = sqlite3.connect(DB_PATH)
    payload = _music_room_payload(conn)
    conn.close()
    return jsonify({"room": payload})


@app.route("/api/music/room/commands/<int:command_id>", methods=["PATCH"])
def acknowledge_music_command(command_id):
    data = request.get_json(silent=True) or {}
    status = str(data.get("status") or "")
    if status not in {"applied", "failed"}:
        return jsonify({"error": "指令回执不对"}), 400
    output_text = str(data.get("output") or "")[:500]
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute(
        "UPDATE music_room_commands SET status=?,output_text=?,applied_at=CURRENT_TIMESTAMP "
        "WHERE id=? AND status='pending'",
        (status, output_text, command_id),
    )
    if not cursor.rowcount:
        conn.close()
        return jsonify({"error": "这条播放器指令已经处理过了"}), 409
    command = conn.execute(
        "SELECT character_id,action FROM music_room_commands WHERE id=?", (command_id,)
    ).fetchone()
    for message_id, details_json in conn.execute(
        "SELECT id,details_json FROM music_room_messages WHERE room_id=1 "
        "AND details_json!='{}' ORDER BY id DESC LIMIT 40"
    ).fetchall():
        try:
            details = json.loads(details_json or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if details.get("output", {}).get("command_id") != command_id:
            continue
        details["output"].update({"status": status, "message": output_text})
        conn.execute(
            "UPDATE music_room_messages SET details_json=? WHERE id=?",
            (json.dumps(details, ensure_ascii=False), message_id),
        )
        break
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "character_id": command[0], "action": command[1]})


# ── 共读室 ──────────────────────────────────────────────────
@app.route("/api/reading/books", methods=["GET"])
def list_reading_books():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id,title,filename,encoding,total_chars,total_chapters,total_blocks,created_at,updated_at "
        "FROM reading_books ORDER BY updated_at DESC, id DESC"
    ).fetchall()
    result = [_reading_book_payload(conn, row) for row in rows]
    conn.close()
    return jsonify({"books": result})


@app.route("/api/reading/books", methods=["POST"])
def upload_reading_book():
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        return jsonify({"error": "请选择一本 TXT"}), 400
    filename = os.path.basename(uploaded.filename)
    if not filename.lower().endswith(".txt"):
        return jsonify({"error": "共读室现在只收 TXT"}), 400
    try:
        source_text, encoding = _decode_text_upload(uploaded.read(MAX_TEXT_BYTES + 1))
        chapters = _parse_reading_text(source_text)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    raw_participants = request.form.get("participants", "[]")
    try:
        parsed_participants = json.loads(raw_participants)
    except json.JSONDecodeError:
        return jsonify({"error": "共读成员格式不对"}), 400
    participants = _normalize_reading_participants(parsed_participants)
    if parsed_participants and len(participants) != len(set(parsed_participants)):
        return jsonify({"error": "请选择一到两位有效的共读成员"}), 400

    fallback_title = os.path.splitext(filename)[0].strip() or "未命名"
    title = (request.form.get("title") or fallback_title).strip()[:120]
    total_blocks = sum(len(chapter["blocks"]) for chapter in chapters)
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.execute(
            "INSERT INTO reading_books "
            "(title,filename,encoding,source_text,total_chars,total_chapters,total_blocks) "
            "VALUES (?,?,?,?,?,?,?)",
            (title, filename, encoding, source_text, len(source_text), len(chapters), total_blocks),
        )
        book_id = cursor.lastrowid
        block_index = 0
        for chapter_index, chapter in enumerate(chapters):
            conn.execute(
                "INSERT INTO reading_chapters (book_id,chapter_index,title) VALUES (?,?,?)",
                (book_id, chapter_index, chapter["title"]),
            )
            for block_text in chapter["blocks"]:
                conn.execute(
                    "INSERT INTO reading_blocks (book_id,chapter_index,block_index,text) VALUES (?,?,?,?)",
                    (book_id, chapter_index, block_index, block_text),
                )
                block_index += 1
        conn.execute(
            "INSERT INTO reading_progress (book_id,reader_id) VALUES (?,?)",
            (book_id, USER_ID),
        )
        for character_id in participants:
            conn.execute(
                "INSERT INTO reading_book_participants (book_id,character_id) VALUES (?,?)",
                (book_id, character_id),
            )
        conn.commit()
        row = conn.execute(
            "SELECT id,title,filename,encoding,total_chars,total_chapters,total_blocks,created_at,updated_at "
            "FROM reading_books WHERE id=?",
            (book_id,),
        ).fetchone()
        payload = _reading_book_payload(conn, row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return jsonify({"book": payload}), 201


@app.route("/api/reading/books/<int:book_id>", methods=["GET", "DELETE"])
def reading_book_detail(book_id):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT id,title,filename,encoding,total_chars,total_chapters,total_blocks,created_at,updated_at "
        "FROM reading_books WHERE id=?",
        (book_id,),
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "这本书不在书架上"}), 404
    if request.method == "DELETE":
        highlight_ids = [
            item[0] for item in conn.execute(
                "SELECT id FROM reading_highlights WHERE book_id=?", (book_id,)
            ).fetchall()
        ]
        if highlight_ids:
            placeholders = ",".join("?" for _ in highlight_ids)
            conn.execute(
                f"DELETE FROM reading_annotations WHERE highlight_id IN ({placeholders})",
                highlight_ids,
            )
        for table in (
            "reading_highlights", "reading_book_participants", "reading_progress",
            "reading_blocks", "reading_chapters",
        ):
            conn.execute(f"DELETE FROM {table} WHERE book_id=?", (book_id,))
        conn.execute("DELETE FROM reading_books WHERE id=?", (book_id,))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})

    payload = _reading_book_payload(conn, row)
    payload["chapters"] = [
        {"index": item[0], "title": item[1], "block_count": item[2]}
        for item in conn.execute(
            "SELECT c.chapter_index,c.title,COUNT(b.id) "
            "FROM reading_chapters c LEFT JOIN reading_blocks b "
            "ON b.book_id=c.book_id AND b.chapter_index=c.chapter_index "
            "WHERE c.book_id=? GROUP BY c.chapter_index,c.title ORDER BY c.chapter_index",
            (book_id,),
        ).fetchall()
    ]
    conn.close()
    return jsonify({"book": payload})


@app.route("/api/reading/books/<int:book_id>/participants", methods=["PUT"])
def update_reading_participants(book_id):
    data = request.get_json(silent=True) or {}
    raw = data.get("character_ids")
    participants = _normalize_reading_participants(raw)
    if not isinstance(raw, list) or not 1 <= len(participants) <= 2 or len(participants) != len(set(raw)):
        return jsonify({"error": "请选择一到两位共读成员"}), 400
    conn = sqlite3.connect(DB_PATH)
    if not conn.execute("SELECT 1 FROM reading_books WHERE id=?", (book_id,)).fetchone():
        conn.close()
        return jsonify({"error": "这本书不在书架上"}), 404
    conn.execute("DELETE FROM reading_book_participants WHERE book_id=?", (book_id,))
    for character_id in participants:
        conn.execute(
            "INSERT INTO reading_book_participants (book_id,character_id) VALUES (?,?)",
            (book_id, character_id),
        )
    conn.execute("UPDATE reading_books SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (book_id,))
    conn.commit()
    result = _reading_participant_payload(conn, book_id)
    conn.close()
    return jsonify({"participants": result})


@app.route("/api/reading/books/<int:book_id>/progress", methods=["POST"])
def update_reading_progress(book_id):
    data = request.get_json(silent=True) or {}
    try:
        block_index = int(data.get("block_index"))
        offset = max(0, int(data.get("offset", 0)))
    except (TypeError, ValueError):
        return jsonify({"error": "阅读位置不对"}), 400
    conn = sqlite3.connect(DB_PATH)
    block = conn.execute(
        "SELECT text FROM reading_blocks WHERE book_id=? AND block_index=?",
        (book_id, block_index),
    ).fetchone()
    if not block:
        conn.close()
        return jsonify({"error": "阅读位置不对"}), 400
    offset = min(offset, len(block[0]))
    conn.execute(
        "INSERT INTO reading_progress "
        "(book_id,reader_id,current_block_index,current_offset,read_upto_block_index) "
        "VALUES (?,?,?,?,?) ON CONFLICT(book_id,reader_id) DO UPDATE SET "
        "current_block_index=excluded.current_block_index,current_offset=excluded.current_offset,"
        "read_upto_block_index=MAX(reading_progress.read_upto_block_index,excluded.read_upto_block_index),"
        "updated_at=CURRENT_TIMESTAMP",
        (book_id, USER_ID, block_index, offset, block_index),
    )
    conn.execute("UPDATE reading_books SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (book_id,))
    conn.commit()
    payload = _reading_progress_payload(conn, book_id)
    conn.close()
    return jsonify({"progress": payload})


@app.route("/api/reading/books/<int:book_id>/chapters/<int:chapter_index>", methods=["GET"])
def get_reading_chapter(book_id, chapter_index):
    conn = sqlite3.connect(DB_PATH)
    chapter = conn.execute(
        "SELECT title FROM reading_chapters WHERE book_id=? AND chapter_index=?",
        (book_id, chapter_index),
    ).fetchone()
    if not chapter:
        conn.close()
        return jsonify({"error": "这一章不存在"}), 404
    block_rows = conn.execute(
        "SELECT id,block_index,text FROM reading_blocks "
        "WHERE book_id=? AND chapter_index=? ORDER BY block_index",
        (book_id, chapter_index),
    ).fetchall()
    block_ids = [row[0] for row in block_rows]
    highlights_by_block = {block_id: [] for block_id in block_ids}
    if block_ids:
        placeholders = ",".join("?" for _ in block_ids)
        highlights = conn.execute(
            f"SELECT h.id,h.block_id,h.start_offset,h.end_offset,h.quote,h.note,"
            f"h.color,h.created_at,h.group_key,b.block_index "
            f"FROM reading_highlights h JOIN reading_blocks b ON b.id=h.block_id "
            f"WHERE h.block_id IN ({placeholders}) ORDER BY b.block_index,h.start_offset,h.id",
            block_ids,
        ).fetchall()
        highlight_ids = [row[0] for row in highlights]
        annotations_by_highlight = {highlight_id: [] for highlight_id in highlight_ids}
        if highlight_ids:
            annotation_marks = ",".join("?" for _ in highlight_ids)
            annotations = conn.execute(
                f"SELECT id,highlight_id,author_id,content,created_at "
                f"FROM reading_annotations WHERE highlight_id IN ({annotation_marks}) ORDER BY id",
                highlight_ids,
            ).fetchall()
            for item in annotations:
                char = CHARACTERS.get(item[2], {})
                annotations_by_highlight[item[1]].append({
                    "id": item[0], "author_id": item[2],
                    "author_name": char.get("name", item[2]),
                    "avatar": char.get("avatar", ""),
                    "content": item[3], "created_at": item[4],
                })
        groups = {}
        for item in highlights:
            group_id = item[8] or f"single:{item[0]}"
            groups.setdefault(group_id, []).append(item)
        group_meta = {}
        for group_id, items in groups.items():
            primary_id = min(item[0] for item in items)
            group_meta[group_id] = {
                "primary_id": primary_id,
                "quote": "\n\n".join(item[4] for item in items),
                "note": next((item[5] for item in items if item[5]), ""),
                "annotations": annotations_by_highlight.get(primary_id, []),
            }
        for item in highlights:
            group_id = item[8] or f"single:{item[0]}"
            meta = group_meta[group_id]
            highlights_by_block[item[1]].append({
                "id": meta["primary_id"], "segment_id": item[0],
                "start_offset": item[2], "end_offset": item[3],
                "quote": meta["quote"], "note": meta["note"], "color": item[6],
                "created_at": item[7],
                "group_key": item[8],
                "annotations": meta["annotations"],
            })
    progress = _reading_progress_payload(conn, book_id)
    conn.close()
    return jsonify({
        "chapter": {"index": chapter_index, "title": chapter[0]},
        "blocks": [
            {"id": row[0], "block_index": row[1], "text": row[2],
             "highlights": highlights_by_block.get(row[0], [])}
            for row in block_rows
        ],
        "progress": progress,
    })


@app.route("/api/reading/books/<int:book_id>/highlights", methods=["POST"])
def create_reading_highlight(book_id):
    data = request.get_json(silent=True) or {}
    raw_segments = data.get("segments")
    if raw_segments is None:
        raw_segments = [{
            "block_id": data.get("block_id"),
            "start_offset": data.get("start_offset"),
            "end_offset": data.get("end_offset"),
            "quote": data.get("quote"),
        }]
    if not isinstance(raw_segments, list) or not 1 <= len(raw_segments) <= 32:
        return jsonify({"error": "一次最多跨 32 段划线"}), 400
    conn = sqlite3.connect(DB_PATH)
    segments = []
    for raw_segment in raw_segments:
        try:
            block_id = int(raw_segment.get("block_id"))
            start = int(raw_segment.get("start_offset"))
            end = int(raw_segment.get("end_offset"))
        except (AttributeError, TypeError, ValueError):
            conn.close()
            return jsonify({"error": "划线位置不对"}), 400
        block = conn.execute(
            "SELECT block_index,text,chapter_index FROM reading_blocks "
            "WHERE id=? AND book_id=?",
            (block_id, book_id),
        ).fetchone()
        if not block or start < 0 or end <= start or end > len(block[1]):
            conn.close()
            return jsonify({"error": "划线位置不对"}), 400
        quote = block[1][start:end]
        if raw_segment.get("quote") is not None and raw_segment.get("quote") != quote:
            conn.close()
            return jsonify({"error": "划线文字和位置没有对上"}), 400
        if conn.execute(
            "SELECT 1 FROM reading_highlights WHERE block_id=? "
            "AND start_offset<? AND end_offset>? LIMIT 1",
            (block_id, end, start),
        ).fetchone():
            conn.close()
            return jsonify({"error": "选中的地方已经有划线啦"}), 400
        segments.append({
            "block_id": block_id,
            "block_index": block[0],
            "chapter_index": block[2],
            "start_offset": start,
            "end_offset": end,
            "quote": quote,
        })
    if len({item["chapter_index"] for item in segments}) != 1 or any(
        current["block_index"] != previous["block_index"] + 1
        for previous, current in zip(segments, segments[1:])
    ):
        conn.close()
        return jsonify({"error": "一次只能划同一章里连续的段落"}), 400
    combined_quote = "\n\n".join(item["quote"] for item in segments)
    supplied_quote = data.get("quote")
    if supplied_quote is not None and supplied_quote != combined_quote:
        conn.close()
        return jsonify({"error": "划线文字和位置没有对上"}), 400
    note = str(data.get("note") or "").strip()[:4000]
    group_key = secrets.token_hex(12) if len(segments) > 1 else None
    created = []
    for segment in segments:
        cursor = conn.execute(
            "INSERT INTO reading_highlights "
            "(book_id,block_id,start_offset,end_offset,quote,note,group_key) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                book_id, segment["block_id"], segment["start_offset"],
                segment["end_offset"], segment["quote"], note, group_key,
            ),
        )
        created.append((cursor.lastrowid, segment))
    highlight_id = created[0][0]
    last_block_index = segments[-1]["block_index"]
    conn.execute(
        "INSERT INTO reading_progress "
        "(book_id,reader_id,current_block_index,read_upto_block_index) VALUES (?,?,?,?) "
        "ON CONFLICT(book_id,reader_id) DO UPDATE SET "
        "read_upto_block_index=MAX(reading_progress.read_upto_block_index,excluded.read_upto_block_index),"
        "updated_at=CURRENT_TIMESTAMP",
        (book_id, USER_ID, last_block_index, last_block_index),
    )
    conn.commit()
    created_at = conn.execute(
        "SELECT created_at FROM reading_highlights WHERE id=?", (highlight_id,)
    ).fetchone()[0]
    progress = _reading_progress_payload(conn, book_id)
    conn.close()
    created_payloads = [{
        "id": highlight_id,
        "segment_id": segment_id,
        "block_id": segment["block_id"],
        "start_offset": segment["start_offset"],
        "end_offset": segment["end_offset"],
        "quote": combined_quote,
        "note": note,
        "color": "rose",
        "group_key": group_key,
        "created_at": created_at,
        "annotations": [],
    } for segment_id, segment in created]
    return jsonify({
        "highlight": created_payloads[0],
        "highlights": created_payloads,
        "progress": progress,
    }), 201


@app.route("/api/reading/highlights/<int:highlight_id>", methods=["PATCH", "DELETE"])
def reading_highlight_detail(highlight_id):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT id,group_key FROM reading_highlights WHERE id=?", (highlight_id,)
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "这条划线不存在"}), 404
    if row[1]:
        highlight_ids = [item[0] for item in conn.execute(
            "SELECT id FROM reading_highlights WHERE group_key=? ORDER BY id",
            (row[1],),
        ).fetchall()]
    else:
        highlight_ids = [row[0]]
    placeholders = ",".join("?" for _ in highlight_ids)
    if request.method == "DELETE":
        conn.execute(
            f"DELETE FROM reading_annotations WHERE highlight_id IN ({placeholders})",
            highlight_ids,
        )
        conn.execute(
            f"DELETE FROM reading_highlights WHERE id IN ({placeholders})",
            highlight_ids,
        )
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    data = request.get_json(silent=True) or {}
    note = str(data.get("note") or "").strip()[:4000]
    conn.execute(
        f"UPDATE reading_highlights SET note=? WHERE id IN ({placeholders})",
        [note] + highlight_ids,
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "note": note})


@app.route("/api/reading/highlights/<int:highlight_id>/annotate", methods=["POST"])
def annotate_reading_highlight(highlight_id):
    data = request.get_json(silent=True) or {}
    conn = sqlite3.connect(DB_PATH)
    highlight = conn.execute(
        "SELECT h.book_id,h.block_id,h.quote,h.note,b.title,rb.chapter_index,"
        "rb.block_index,c.title,h.group_key "
        "FROM reading_highlights h JOIN reading_books b ON b.id=h.book_id "
        "JOIN reading_blocks rb ON rb.id=h.block_id "
        "JOIN reading_chapters c ON c.book_id=h.book_id AND c.chapter_index=rb.chapter_index "
        "WHERE h.id=?",
        (highlight_id,),
    ).fetchone()
    if not highlight:
        conn.close()
        return jsonify({"error": "这条划线不存在"}), 404
    highlight = list(highlight)
    primary_highlight_id = highlight_id
    if highlight[8]:
        group_rows = conn.execute(
            "SELECT h.id,h.quote,h.note,b.block_index FROM reading_highlights h "
            "JOIN reading_blocks b ON b.id=h.block_id "
            "WHERE h.group_key=? ORDER BY b.block_index,h.id",
            (highlight[8],),
        ).fetchall()
        primary_highlight_id = min(item[0] for item in group_rows)
        highlight[2] = "\n\n".join(item[1] for item in group_rows)
        highlight[3] = next((item[2] for item in group_rows if item[2]), "")
        highlight[6] = max(item[3] for item in group_rows)
    raw_ids = data.get("character_ids")
    if raw_ids is None:
        raw_ids = [item["id"] for item in _reading_participant_payload(conn, highlight[0])]
    character_ids = _normalize_reading_participants(raw_ids)
    if not isinstance(raw_ids, list) or not 1 <= len(character_ids) <= 2 or len(character_ids) != len(set(raw_ids)):
        conn.close()
        return jsonify({"error": "请选择一到两位来批注"}), 400
    progress = _reading_progress_payload(conn, highlight[0])
    if highlight[6] > progress["read_upto_block_index"]:
        conn.close()
        return jsonify({"error": "还没有读到这里"}), 400

    context_rows = conn.execute(
        "SELECT block_index,text FROM reading_blocks "
        "WHERE book_id=? AND chapter_index=? AND block_index<=? "
        "ORDER BY ABS(block_index-?) LIMIT 14",
        (highlight[0], highlight[5], progress["read_upto_block_index"], highlight[6]),
    ).fetchall()
    context_rows = sorted(context_rows, key=lambda row: row[0])
    context_parts = []
    used_chars = 0
    for block_index, block_text in context_rows:
        fragment = f"[段落 {block_index}] {block_text}"
        if context_parts and used_chars + len(fragment) > 6000:
            continue
        context_parts.append(fragment)
        used_chars += len(fragment)
    existing = conn.execute(
        "SELECT author_id,content FROM reading_annotations WHERE highlight_id=? ORDER BY id",
        (primary_highlight_id,),
    ).fetchall()
    existing_text = "\n".join(
        f"{CHARACTERS.get(author_id, {}).get('name', author_id)}：{content}"
        for author_id, content in existing
    ) or "暂无"
    conn.close()

    prompt = (
        f"你和{USER_DISPLAY_NAME}正在共读一本书。你只能依据下面明确提供的、{USER_DISPLAY_NAME}已经读到的正文回应；"
        "不要推测后文，不要声称自己读过未提供的章节，也不要剧透。"
            f"请像写在书页边上的批注一样，用你自己的口吻回应 {USER_DISPLAY_NAME} 划出的句子，1到3句即可。"
        "不要写姓名前缀，不要复述任务说明。\n\n"
        f"书名：{highlight[4]}\n章节：{highlight[7]}\n"
        f"{USER_DISPLAY_NAME}划线：{highlight[2]}\n"
        f"{USER_DISPLAY_NAME}的批注：{highlight[3] or '暂无'}\n"
        f"这条划线已有的共读批注：\n{existing_text}\n\n"
        f"已经读到的附近正文：\n{chr(10).join(context_parts)}"
    )
    annotations = []
    for character_id in character_ids:
        reply, _usage, _tools = ask_character_group(
            CHARACTERS[character_id], prompt,
            session_id=f"reading:{highlight[0]}", allow_tools=False,
        )
        reply = (reply or "").strip()
        if not reply:
            continue
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.execute(
            "INSERT INTO reading_annotations (highlight_id,author_id,content) VALUES (?,?,?)",
            (primary_highlight_id, character_id, reply),
        )
        annotation_id = cursor.lastrowid
        conn.commit()
        created_at = conn.execute(
            "SELECT created_at FROM reading_annotations WHERE id=?", (annotation_id,)
        ).fetchone()[0]
        conn.close()
        annotations.append({
            "id": annotation_id, "author_id": character_id,
            "author_name": CHARACTERS[character_id]["name"],
            "avatar": CHARACTERS[character_id]["avatar"],
            "content": reply, "created_at": created_at,
        })
    return jsonify({"annotations": annotations})


# ── 朋友圈 ──────────────────────────────────────────────────
@app.route("/api/moments", methods=["GET"])
def get_moments():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    moments = conn.execute(
        "SELECT * FROM moments ORDER BY created_at DESC LIMIT 100"
    ).fetchall()
    result = []
    for m in moments:
        comments = conn.execute(
            "SELECT * FROM moment_comments WHERE moment_id=? ORDER BY created_at ASC",
            (m["id"],)
        ).fetchall()
        result.append({
            "id": m["id"],
            "author_id": m["author_id"],
            "content": m["content"],
            "created_at": m["created_at"],
            "comments": [dict(c) for c in comments],
        })
    conn.close()
    return jsonify(result)


@app.route("/api/moments", methods=["POST"])
def post_moment():
    data = request.get_json() or {}
    content = data.get("content", "").strip()
    if not content:
        return jsonify({"error": "empty"}), 400
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO moments (author_id, content) VALUES (?, ?)",
        ("user", content)
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


def _generate_moment_core(cid=None):
    if not cid:
        cid = _random.choice(list(CHARACTERS.keys()))
    char = CHARACTERS.get(cid)
    if not char:
        return None, "unknown character"
    summary = get_summary("default", cid) or ""
    prompt = (
        f"{'最近的记忆摘要：' + summary + chr(10) + chr(10) if summary else ''}"
        "请以你自己的口吻发一条朋友圈，字数50字以内，自然随意，不要堆砌表情符号。"
        "只输出正文内容，不要加引号或前缀。"
    )
    reply, _usage_metrics, _tools_called = ask_character_group(char, prompt, session_id="moments")
    if not reply or not reply.strip():
        return None, "empty reply"
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO moments (author_id, content) VALUES (?, ?)", (cid, reply.strip()))
    conn.commit()
    conn.close()
    save_long_term_memory(
        f"我在猫窝发了一条动态：{reply.strip()[:80]}", cid, source="moment"
    )
    return {"ok": True, "author_id": cid, "content": reply.strip()}, None


@app.route("/api/moments/generate", methods=["POST"])
def generate_moment():
    data = request.get_json() or {}
    result, err = _generate_moment_core(data.get("character_id"))
    if err:
        return jsonify({"error": err}), (400 if err == "unknown character" else 500)
    return jsonify(result)


@app.route("/api/moments/<int:moment_id>/comment", methods=["POST"])
def comment_moment(moment_id):
    data = request.get_json() or {}
    character_ids = data.get("character_ids", [])
    user_comment = data.get("user_comment", "").strip()
    if not character_ids and not user_comment:
        return jsonify({"error": "no characters"}), 400

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    moment = conn.execute("SELECT * FROM moments WHERE id=?", (moment_id,)).fetchone()
    if not moment:
        conn.close()
        return jsonify({"error": "not found"}), 404

    results = []
    if user_comment:
        conn.execute(
            "INSERT INTO moment_comments (moment_id, author_id, content) VALUES (?, ?, ?)",
            (moment_id, "user", user_comment)
        )
        conn.commit()
        results.append({"author_id": "user", "content": user_comment})
        if moment["author_id"] in CHARACTERS:
            save_long_term_memory(
                f"{USER_DISPLAY_NAME}评论了我发的猫窝动态「{moment['content'][:50]}」：{user_comment[:80]}",
                moment["author_id"],
                source="moment_comment",
            )

    for cid in character_ids:
        char = CHARACTERS.get(cid)
        if not char:
            continue
        existing = conn.execute(
            "SELECT author_id, content FROM moment_comments WHERE moment_id=? ORDER BY created_at ASC",
            (moment_id,)
        ).fetchall()
        if existing:
            existing_text = "\n".join(
                f"{CHARACTERS[c['author_id']]['name'] if c['author_id'] in CHARACTERS else USER_DISPLAY_NAME}：{c['content']}"
                for c in existing
            )
        else:
            existing_text = "（暂无评论）"

        author_name = CHARACTERS[moment["author_id"]]["name"] if moment["author_id"] in CHARACTERS else USER_DISPLAY_NAME
        prompt = (
            f"以下是{author_name}发的一条朋友圈动态：\n{moment['content']}\n\n"
            f"已有评论：\n{existing_text}\n\n"
            f"请以你自己的身份，用1-2句话评论这条动态，口吻符合你的性格。"
            f"直接输出评论内容，不要加任何前缀、括号或格式标记。"
        )
        reply, _usage_metrics, _tools_called = ask_character_group(char, prompt, session_id="moments")
        if reply and reply.strip():
            conn.execute(
                "INSERT INTO moment_comments (moment_id, author_id, content) VALUES (?, ?, ?)",
                (moment_id, cid, reply.strip())
            )
            conn.commit()
            results.append({"author_id": cid, "content": reply.strip()})
            if moment["author_id"] == "user":
                save_long_term_memory(
                    f"{USER_DISPLAY_NAME}在猫窝发了动态「{moment['content'][:50]}」，我评论说：{reply.strip()[:80]}",
                    cid,
                    source="moment_comment",
                )

    conn.close()
    return jsonify({"ok": True, "comments": results})


@app.route("/api/moments/<int:moment_id>", methods=["DELETE"])
def delete_moment(moment_id):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM moment_comments WHERE moment_id=?", (moment_id,))
    conn.execute("DELETE FROM moments WHERE id=?", (moment_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/health")
def health():
    info = {}
    try:
        conn = sqlite3.connect(DB_PATH)
        for cid, c in CHARACTERS.items():
            total = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE character_id = ?", (cid,)
            ).fetchone()[0]
            active = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE character_id = ? AND compressed = 0", (cid,)
            ).fetchone()[0]
            has_summary = conn.execute(
                "SELECT COUNT(*) FROM summaries WHERE session_id LIKE ?", (f"{cid}:%",)
            ).fetchone()[0] > 0
            info[cid] = {
                "name": c["name"],
                "model": c["model"],
                "provider": c.get("provider", "openrouter"),
                "message_count": total,
                "active_count": active,
                "has_summary": has_summary,
            }
        conn.close()
    except Exception:
        pass
    return jsonify({
        "status": "ok",
        "openrouter_key_configured": bool(OPENROUTER_API_KEY),
        "providers": {
            key: {"configured": _provider_configured(key)}
            for key in MODEL_PROVIDERS
        },
        "summary_provider": SUMMARY_PROVIDER,
        "summary_model": SUMMARY_MODEL,
        "characters": info,
    })



# ============================================================
# 定时任务：Job 函数
# ============================================================
def do_moment_post():
    with app.app_context():
        try:
            result, err = _generate_moment_core()
            if err:
                app.logger.error(f"[sched] moment post failed: {err}")
            else:
                app.logger.info("[sched] moment post ok")
        except Exception as e:
            app.logger.error(f"[sched] moment post exception: {e}")


def do_desire_heartbeat():
    with app.app_context():
        try:
            from zoneinfo import ZoneInfo

            now_ts = _utc_timestamp()
            local_now = datetime.now(ZoneInfo(SCHEDULER_TIMEZONE))
            states = {
                cid: load_desire_state(cid, now_ts)
                for cid in CHARACTERS
            }
            for cid, state in states.items():
                save_desire_state(cid, state)
                _maybe_evolve_character_scene(cid, state, now_ts)

            # 好友申请独立于主动消息总开关：即使关掉自动发帖，角色也仍可在
            # 冷静期后按自身依恋状态决定是否申请回来。
            for cid in CHARACTERS:
                friendship = _get_friendship(cid)
                if (
                    friendship["state"] != "user_deleted"
                    or friendship.get("pending_request")
                ):
                    continue
                try:
                    request_due = (
                        friendship.get("request_after") is not None
                        and now_ts >= float(friendship["request_after"])
                    )
                except (TypeError, ValueError):
                    request_due = False
                if not request_due:
                    continue

                decision = _friend_request_decision(
                    friendship, states.get(cid) or {}, now_ts
                )
                attempts = int(friendship.get("request_attempts") or 0) + 1
                if not decision["apply"]:
                    _set_friendship(
                        cid,
                        "user_deleted",
                        reason=friendship.get("reason", ""),
                        deleted_at=friendship.get("deleted_at"),
                        request_after=(
                            now_ts
                            + _friend_request_retry_delay(decision["probability"])
                        ),
                        request_attempts=attempts,
                        last_request_decision={
                            **decision, "outcome": "wait", "at": now_ts,
                        },
                    )
                    continue

                try:
                    drives = (states.get(cid) or {}).get("drives") or {}
                    prompt = (
                        f"[系统提示：{USER_DISPLAY_NAME} 把你从好友列表删除了，留下的原因是"
                        f"「{friendship.get('reason') or '没有解释'}」。"
                        f"你此刻的依恋冲动是 {float(drives.get('attachment', 0)):.2f}，"
                        f"压力是 {float(drives.get('stress', 0)):.2f}，"
                        f"疲惫是 {float(drives.get('fatigue', 0)):.2f}。"
                        f"你决定申请重新加回 {USER_DISPLAY_NAME}。写一段 80 字以内的好友申请验证消息，"
                        "只输出纯文本，不要调用工具；可以撒娇、认错、嘴硬，也可以带着委屈。]"
                    )
                    reply, _transfer, _sticker, _called, _metrics = ask_character(
                        CHARACTERS[cid], "default", prompt, allow_tools=False
                    )
                    request_text = strip_fake_action_text(reply or "", cid).strip()[:80]
                    if request_text:
                        _set_friendship(
                            cid,
                            "user_deleted",
                            reason=friendship.get("reason", ""),
                            deleted_at=friendship.get("deleted_at"),
                            request_after=friendship.get("request_after"),
                            pending_request={
                                "text": request_text,
                                "created_at": now_ts,
                            },
                            request_attempts=attempts,
                            last_request_decision={
                                **decision, "outcome": "apply", "at": now_ts,
                            },
                        )
                except Exception as exc:
                    app.logger.error(
                        "[friendship] %s request generation failed: %s", cid, exc
                    )

            enabled = _read_setting(
                "desire_enabled", "true" if DESIRE_DEFAULT_ENABLED else "false"
            ) != "false"
            if not enabled:
                _write_setting("desire_last_gate", json.dumps({"reason": "disabled", "at": now_ts}))
                return

            today = local_now.date().isoformat()
            stored_day = _read_setting("desire_daily_date", "")
            try:
                daily_count = (
                    int(_read_setting("desire_daily_count", "0") or 0)
                    if stored_day == today else 0
                )
            except ValueError:
                daily_count = 0
            try:
                last_dispatch_at = float(_read_setting("desire_last_dispatch", "0") or 0)
            except ValueError:
                last_dispatch_at = 0.0
            try:
                last_user_activity = float(_read_setting("desire_last_user_activity", "0") or 0)
            except ValueError:
                last_user_activity = 0.0

            local_minute = local_now.hour * 60 + local_now.minute
            quiet_start = _parse_clock_minutes(
                _read_setting("desire_quiet_start", "23:30"), 23 * 60 + 30
            )
            quiet_end = _parse_clock_minutes(
                _read_setting("desire_quiet_end", "08:30"), 8 * 60 + 30
            )
            _frequency, frequency_config = _desire_frequency_config()
            allowed, gate_reason = evaluate_household_gate(
                now_ts,
                local_minute,
                last_dispatch_at,
                last_user_activity,
                daily_count,
                quiet_start_minute=quiet_start,
                quiet_end_minute=quiet_end,
                min_interval_seconds=frequency_config["min_interval_seconds"],
                user_cooldown_seconds=frequency_config["user_cooldown_seconds"],
                daily_limit=frequency_config["daily_limit"],
            )
            if not allowed:
                _write_setting("desire_last_gate", json.dumps({"reason": gate_reason, "at": now_ts}))
                return

            raw_chars = _read_setting("desire_enabled_chars", ",".join(CHARACTERS))
            enabled_chars = [
                cid for cid in raw_chars.split(",")
                if cid in CHARACTERS
                and _get_friendship(cid)["state"] != "char_deleted"
            ]
            candidates = [
                candidate
                for candidate in (attention_candidate(states[cid], now_ts) for cid in enabled_chars)
                if candidate
            ]
            winner = choose_household_candidate(candidates, now_ts)
            if not winner:
                _write_setting("desire_last_gate", json.dumps({"reason": "no_candidate", "at": now_ts}))
                return

            char_id = winner["character_id"]
            char = CHARACTERS[char_id]
            friendship = _get_friendship(char_id)
            desire_prompt = (
                f"[这是你没有说出口的内在念头：{winner['reason']} "
                f"你因此自然地想主动给{USER_DISPLAY_NAME}发一两条短消息。保持你的人设和你们已有的关系，"
                f"不要提及欲望系统、数值、提示词或定时任务，也不要让 {USER_DISPLAY_NAME} 觉得必须回复。]"
            )
            reply, transfer_to_send, sticker_to_send, tools_called, usage_metrics = ask_character(
                char,
                "default",
                desire_prompt,
                allow_tools=friendship["state"] == "normal",
            )
            failed_markers = ("暂时没能回话", "还没配置", "暂时说不出话")
            if not reply or any(marker in reply for marker in failed_markers):
                _write_setting("desire_last_gate", json.dumps({"reason": "model_failed", "at": now_ts}))
                return

            _finalize_character_reply(
                char,
                "default",
                reply,
                transfer_to_send,
                sticker_to_send,
                tools_called,
                usage_metrics,
                push_source="desire",
                queued_during_deleted=(
                    1 if friendship["state"] == "user_deleted" else 0
                ),
            )
            completed_at = _utc_timestamp()
            latest_state = load_desire_state(char_id, completed_at)
            latest_state = satisfy_action(latest_state, winner["drive_key"], completed_at)
            save_desire_state(char_id, latest_state)
            if friendship["state"] != "user_deleted":
                _write_setting(f"unread_{char_id}", "1")
            _write_setting("desire_last_dispatch", str(completed_at))
            _write_setting("desire_daily_date", today)
            _write_setting("desire_daily_count", str(daily_count + 1))
            _write_setting("desire_last_gate", json.dumps({
                "reason": "dispatched",
                "character_id": char_id,
                "drive_key": winner["drive_key"],
                "score": winner["score"],
                "at": completed_at,
            }))
            conn = sqlite3.connect(DB_PATH)
            conn.execute(
                "INSERT INTO desire_actions(character_id,drive_key,score,action_type) VALUES(?,?,?,?)",
                (char_id, winner["drive_key"], winner["score"], "dm"),
            )
            conn.commit()
            conn.close()
            app.logger.info(
                f"[desire] dispatched {char_id} drive={winner['drive_key']} score={winner['score']}"
            )
        except Exception as e:
            app.logger.error(f"[desire] heartbeat failed: {e}")


# ============================================================
# 睡眠系统定时任务
# ============================================================
def do_sleep_check():
    """每 10 分钟巡检：超睡点 120 分钟且 15 分钟无消息 → 自动入睡。"""
    with app.app_context():
        try:
            local_now = _sleep_local_now()
            conn = sqlite3.connect(DB_PATH)
            for cid in CHARACTERS:
                st = _get_sleep_state(cid, now=local_now)
                if st["state"] == "asleep":
                    continue
                # 早晨越过起床线后绝不能被夜间巡检重新送睡。
                if not _is_scheduled_sleep_window(cid, local_now):
                    continue
                past_mins = _minutes_past_bedtime(cid, local_now)
                if past_mins is None or past_mins < 120:
                    continue
                # 检查最近一条消息距今是否超过 15 分钟
                row = conn.execute(
                    "SELECT created_at FROM messages WHERE character_id=? AND session_id='default' "
                    "ORDER BY id DESC LIMIT 1",
                    (cid,),
                ).fetchone()
                if row:
                    try:
                        last_ts = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
                        if last_ts.tzinfo is None:
                            last_ts = last_ts.replace(tzinfo=timezone.utc)
                        idle_mins = (datetime.now(timezone.utc) - last_ts).total_seconds() / 60
                    except Exception:
                        idle_mins = 999
                else:
                    idle_mins = 999
                if idle_mins >= 15:
                    _set_sleep_state(cid, "asleep", slept_at=str(_utc_timestamp()))
                    app.logger.info(f"[sleep] {cid} 自动入睡 (超睡点 {int(past_mins)}m, 静默 {int(idle_mins)}m)")
            conn.close()
        except Exception as e:
            app.logger.error(f"[sleep] check failed: {e}")


def do_sleep_wakeup(char_id):
    """定时起床：waketime 触发，若有积压消息则一次性汇总回复。"""
    with app.app_context():
        try:
            # 定时任务需要原始入睡时间来生成睡眠时长，避免读取时先被懒校准清空。
            st = _get_sleep_state(char_id, reconcile=False)
            _set_sleep_state(char_id, "awake")
            queued_msgs = _load_queued_sleep_msgs(char_id, "default")
            if not queued_msgs:
                app.logger.info(f"[sleep] {char_id} 起床，无积压消息")
                return
            _clear_queued_sleep_flags(char_id, "default")
            char = CHARACTERS[char_id]
            slept_at = st.get("slept_at")
            slept_mins = int((_utc_timestamp() - float(slept_at)) / 60) if slept_at else 0
            slept_h, slept_m = divmod(slept_mins, 60)
            wakeup_prompt = (
                f"[系统：你刚睡醒。你睡了约 {slept_h} 小时 {slept_m} 分钟。"
                f"一睁眼看到 {USER_DISPLAY_NAME} 在你睡着期间发了 {len(queued_msgs)} 条消息："
                + " | ".join(f"「{m}」" for m in queued_msgs)
                + " 现在统一用你的风格回应，可以有起床气，也可以表达关心，自然一些。]"
            )
            reply, transfer_to_send, sticker_to_send, tools_called, usage_metrics = ask_character(
                char, "default", wakeup_prompt, just_woke=True
            )
            failed_markers = ("暂时没能回话", "还没配置", "暂时说不出话")
            if not reply or any(m in reply for m in failed_markers):
                app.logger.warning(f"[sleep] {char_id} 起床汇总调用失败")
                return
            _finalize_character_reply(
                char, "default", reply, transfer_to_send, sticker_to_send,
                tools_called, usage_metrics, push_source="sleep_wakeup",
            )
            _write_setting(f"unread_{char_id}", "1")
            app.logger.info(f"[sleep] {char_id} 起床汇总 {len(queued_msgs)} 条消息")
        except Exception as e:
            app.logger.error(f"[sleep] wakeup failed ({char_id}): {e}")


def do_sleep_nag(char_id):
    """反向催睡：到睡点时触发，角色主动说晚安并催User睡觉。"""
    with app.app_context():
        try:
            if _read_setting(f"sleep_{char_id}_nag_enabled", "false") != "true":
                return
            st = _get_sleep_state(char_id)
            if st["state"] == "asleep":
                return
            char = CHARACTERS[char_id]
            nag_prompt = (
                "[系统：现在正好是你的习惯睡点，你准备去睡觉了。"
                f"自然地和 {USER_DISPLAY_NAME} 说晚安，顺便提醒 {USER_DISPLAY_NAME} 也早点休息，用你的风格，不要提这是系统触发的。]"
            )
            reply, transfer_to_send, sticker_to_send, tools_called, usage_metrics = ask_character(
                char, "default", nag_prompt
            )
            failed_markers = ("暂时没能回话", "还没配置", "暂时说不出话")
            if not reply or any(m in reply for m in failed_markers):
                app.logger.warning(f"[sleep] {char_id} 反向催睡调用失败")
                return
            _finalize_character_reply(
                char, "default", reply, transfer_to_send, sticker_to_send,
                tools_called, usage_metrics, push_source="sleep_nag",
            )
            _set_sleep_state(char_id, "asleep", slept_at=str(_utc_timestamp()))
            _write_setting(f"unread_{char_id}", "1")
            app.logger.info(f"[sleep] {char_id} 反向催睡完成，已入睡")
        except Exception as e:
            app.logger.error(f"[sleep] nag failed ({char_id}): {e}")


def register_sleep_jobs():
    """注册睡眠系统所有调度任务（巡检 + 各角色起床 + 各角色催睡）。"""
    # 移除旧睡眠 jobs
    for job in scheduler.get_jobs():
        if job.id.startswith("sleep_"):
            scheduler.remove_job(job.id)

    # 每 10 分钟自动入睡巡检
    scheduler.add_job(
        do_sleep_check, "interval", minutes=10,
        id="sleep_autocheck", replace_existing=True,
        coalesce=True, max_instances=1, misfire_grace_time=120,
    )

    # 各角色定时起床 + 反向催睡
    for cid, defaults in SLEEP_DEFAULTS.items():
        # 起床
        hm_wake = _parse_hhmm(_get_sleep_cfg(cid, "waketime") or defaults.get("waketime", "07:30"))
        if hm_wake:
            scheduler.add_job(
                do_sleep_wakeup, "cron",
                args=[cid],
                hour=hm_wake[0], minute=hm_wake[1],
                id=f"sleep_wake_{cid}", replace_existing=True,
                coalesce=True, max_instances=1, misfire_grace_time=300,
            )
        # 反向催睡
        hm_bed = _parse_hhmm(_get_sleep_cfg(cid, "bedtime") or defaults.get("bedtime", "23:00"))
        if hm_bed:
            scheduler.add_job(
                do_sleep_nag, "cron",
                args=[cid],
                hour=hm_bed[0], minute=hm_bed[1],
                id=f"sleep_nag_{cid}", replace_existing=True,
                coalesce=True, max_instances=1, misfire_grace_time=300,
            )

    app.logger.info("[sleep] scheduler jobs registered")


# ============================================================
# 定时任务：注册 / 更新 Jobs
# ============================================================
def register_scheduler_jobs():
    for job in scheduler.get_jobs():
        if job.id.startswith("sched_") or job.id in {"memory_decay", "memory_enrichment"}:
            scheduler.remove_job(job.id)

    moments_slots = [s.strip() for s in _read_setting("sched_moments_slots", "").split(",") if s.strip() in SLOT_HOURS]

    for slot in moments_slots:
        scheduler.add_job(do_moment_post, "cron",
                          hour=SLOT_HOURS[slot], minute=0,
                          id=f"sched_moments_{slot}", replace_existing=True)

    scheduler.add_job(
        do_desire_heartbeat,
        "interval",
        minutes=max(1, DESIRE_TICK_MINUTES),
        id="sched_desire_heartbeat",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=120,
    )
    if _memory_supports("decay"):
        scheduler.add_job(
            MEMORY_SERVICE.run_decay_cycle,
            "interval",
            hours=24,
            id="memory_decay",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=600,
        )
    if _memory_supports("enrichment"):
        scheduler.add_job(
            retry_pending_memory_enrichment,
            "interval",
            minutes=30,
            next_run_time=datetime.now(timezone.utc) + timedelta(seconds=20),
            id="memory_enrichment",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=600,
        )

    app.logger.info(f"[sched] registered: moments={moments_slots} desire={DESIRE_TICK_MINUTES}m")


def start_background_services():
    """Start the in-process scheduler once when this process owns background work."""
    if not SCHEDULER_ENABLED:
        app.logger.info("[sched] disabled by SCHEDULER_ENABLED=false")
        return False
    if scheduler.running:
        return True
    register_scheduler_jobs()
    register_sleep_jobs()
    scheduler.start()
    return True


@app.route("/api/unread", methods=["GET"])
def get_unread():
    unread = [cid for cid in CHARACTERS if _read_setting(f"unread_{cid}", "0") == "1"]
    return jsonify(unread)


@app.route("/api/unread/<char_id>/clear", methods=["POST"])
def clear_unread(char_id):
    if char_id in CHARACTERS:
        _write_setting(f"unread_{char_id}", "0")
    return jsonify({"ok": True})


# ============================================================
# 睡眠系统 API
# ============================================================
@app.route("/api/sleep_states", methods=["GET"])
def get_sleep_states():
    """只读；返回全员当前睡眠状态（不走路由契约保护区）。"""
    return jsonify({cid: _get_sleep_state(cid)["state"] for cid in CHARACTERS})


@app.route("/api/sleep/config", methods=["GET"])
def get_sleep_config():
    result = {}
    for cid in CHARACTERS:
        result[cid] = {
            "bedtime":       _get_sleep_cfg(cid, "bedtime"),
            "waketime":      _get_sleep_cfg(cid, "waketime"),
            "chronotype":    _get_sleep_cfg(cid, "chronotype"),
            "resist_bias":   _get_sleep_cfg(cid, "resist_bias"),
            "nag_enabled":   _read_setting(f"sleep_{cid}_nag_enabled", "false") == "true",
            "current_state": _get_sleep_state(cid)["state"],
        }
    return jsonify(result)


@app.route("/api/sleep/config", methods=["POST"])
def set_sleep_config():
    data = request.get_json() or {}
    for cid in CHARACTERS:
        if cid not in data:
            continue
        cfg = data[cid]
        for field in ("bedtime", "waketime", "chronotype", "resist_bias"):
            if field in cfg:
                _write_setting(f"sleep_{cid}_{field}", str(cfg[field]))
        if "nag_enabled" in cfg:
            _write_setting(f"sleep_{cid}_nag_enabled", "true" if cfg["nag_enabled"] else "false")
    register_sleep_jobs()
    return jsonify({"ok": True})


@app.route("/api/sleep/state/<char_id>", methods=["POST"])
def set_sleep_state_manual(char_id):
    """猫砂盆用：手动切换角色睡眠状态（管理员调试用）。"""
    if char_id not in CHARACTERS:
        return jsonify({"error": "unknown character"}), 400
    data = request.get_json() or {}
    new_state = data.get("state", "awake")
    if new_state not in ("awake", "asleep"):
        return jsonify({"error": "state must be awake or asleep"}), 400
    slept_at = str(_utc_timestamp()) if new_state == "asleep" else None
    _set_sleep_state(char_id, new_state, slept_at=slept_at)
    return jsonify({"ok": True, "state": new_state})


@app.route("/api/sleep/nudge", methods=["POST", "OPTIONS"])
def sleep_nudge():
    """无 session 的催睡接口，供外部页面（hug-button）使用。
    body: {password, character_id}
    密码正确时触发一次催睡对话，返回角色回复。
    """
    if not SLEEP_NUDGE_ENABLED:
        return jsonify({"error": "sleep nudge is disabled"}), 404
    if request.method == "OPTIONS":
        return "", 204
    data = request.get_json() or {}
    if not APP_PASSWORD or data.get("password") != APP_PASSWORD:
        return jsonify({"error": "unauthorized"}), 401
    char_id = data.get("character_id", "")
    if char_id not in CHARACTERS:
        return jsonify({"error": "unknown character"}), 400
    char = CHARACTERS[char_id]
    sleep_st = _get_sleep_state(char_id)
    if sleep_st["state"] == "asleep":
        return jsonify({"reply": "（已经在睡了～）", "was_asleep": True})
    past_mins = _minutes_past_bedtime(char_id)
    nudge_msg = (
        "[系统：用户催你去睡觉了。"
        + (f"你已超过睡点 {int(past_mins)} 分钟。" if past_mins and past_mins > 0 else "")
        + "用你的风格回应，如果你决定去睡就说晚安，不要提这是系统触发的。]"
    )
    reply, transfer_to_send, sticker_to_send, tools_called, usage_metrics = ask_character(
        char, "default", nudge_msg
    )
    failed_markers = ("暂时没能回话", "还没配置", "暂时说不出话")
    if not reply or any(m in reply for m in failed_markers):
        return jsonify({"error": "model unavailable"}), 503
    save_message("default", char_id, "model", reply)
    if SLEEP_GOODNIGHT_RE.search(reply) and (past_mins is None or past_mins > -30):
        _set_sleep_state(char_id, "asleep", slept_at=str(_utc_timestamp()))
    return jsonify({
        "reply": reply,
        "was_asleep": False,
        "sleep_state": _get_sleep_state(char_id)["state"],
    })


# ============================================================
# Step 4 — 调度器配置 API
# ============================================================
@app.route("/api/scheduler/config", methods=["GET"])
def get_scheduler_config():
    desire_frequency, _frequency_config = _desire_frequency_config()
    return jsonify({
        "moments_slots": _read_setting("sched_moments_slots", ""),
        "desire_enabled": _read_setting(
            "desire_enabled", "true" if DESIRE_DEFAULT_ENABLED else "false"
        ) != "false",
        "desire_quiet_start": _read_setting("desire_quiet_start", "23:30"),
        "desire_quiet_end": _read_setting("desire_quiet_end", "08:30"),
        "desire_frequency": desire_frequency,
    })


@app.route("/api/scheduler/config", methods=["POST"])
def set_scheduler_config():
    data = request.get_json() or {}
    desire_frequency = str(data.get(
        "desire_frequency",
        _read_setting("desire_frequency", DESIRE_FREQUENCY_DEFAULT),
    )).strip().lower()
    if desire_frequency not in DESIRE_FREQUENCY_PRESETS:
        return jsonify({"error": "主动频率不在可选范围内"}), 400
    _write_setting("sched_moments_slots", data.get("moments_slots", ""))
    _write_setting("desire_enabled", "true" if data.get("desire_enabled", True) else "false")
    _write_setting("desire_quiet_start", data.get("desire_quiet_start", "23:30"))
    _write_setting("desire_quiet_end", data.get("desire_quiet_end", "08:30"))
    _write_setting("desire_frequency", desire_frequency)
    register_scheduler_jobs()
    return jsonify({"ok": True})


os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
os.makedirs(UPLOAD_ROOT, exist_ok=True)
init_db()
_refresh_appearance_urls()
for _character_id in CHARACTERS:
    _existing_summary = get_summary("default", _character_id)
    if _existing_summary:
        try:
            save_long_term_memory(
                _existing_summary,
                _character_id,
                source="conversation_summary",
                source_key="summary:default",
            )
        except Exception as _memory_seed_error:
            app.logger.warning(
                f"memory summary seed failed ({_character_id}): {_memory_seed_error}"
            )
start_background_services()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
