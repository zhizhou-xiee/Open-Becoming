"""
Open-Becoming - 多模型聊天前端
第 6 版（多角色）：支持多个角色，每个角色有独立的模型、人设、往生道 domain 和记忆隔间。

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
from flask import Flask, request, jsonify, send_from_directory, session
from apscheduler.schedulers.background import BackgroundScheduler
from werkzeug.utils import secure_filename

from mcp_client import MCPClient, MCPError, validate_mcp_url
from memory_core import (
    EmbeddedMemoryService,
    GeminiEmbeddingStore,
    LegacyImportError,
    MemoryMetadataAnalyzer,
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
scheduler = BackgroundScheduler(timezone=SCHEDULER_TIMEZONE)

@app.before_request
def require_login():
    if not request.path.startswith("/api/"):
        return  # 静态页面、图标等全部公开
    if request.path == "/api/login":
        return
    if session.get("authed"):
        return
    return jsonify({"error": "unauthorized"}), 401

@app.route("/api/login", methods=["POST"])
def api_login():
    if not APP_PASSWORD:
        return jsonify({"error": "APP_PASSWORD is not configured"}), 503
    body = request.json or {}
    if body.get("password") == APP_PASSWORD:
        session.permanent = True
        session["authed"] = True
        return jsonify({"ok": True})
    return jsonify({"ok": False}), 401

@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True})

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

# 摘要（压缩老对话）专用的便宜模型，走同一个 OR 通道
SUMMARY_MODEL = os.environ.get("SUMMARY_MODEL", "google/gemini-2.5-flash-lite")

GROUP_SUMMARY_THRESHOLD = int(os.environ.get("GROUP_SUMMARY_THRESHOLD", "40"))

# 压缩参数
COMPRESS_THRESHOLD = 40
KEEP_RECENT = 20

DB_PATH = os.environ.get("DB_PATH", "becoming.db")
UPLOAD_ROOT = os.path.join(app.static_folder or "static", "uploads", "chat_images")
ALLOWED_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp"}
ALLOWED_IMAGE_MIMES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
MAX_IMAGE_BYTES = 7 * 1024 * 1024
MAX_TEXT_BYTES = 5 * 1024 * 1024
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))
DESIRE_TICK_MINUTES = int(os.environ.get("DESIRE_TICK_MINUTES", "10"))
DESIRE_DEFAULT_ENABLED = os.environ.get("DESIRE_DRIVEN", "true").lower() == "true"


# ============================================================
# 人设
# ============================================================
CHAR1_PERSONA = """你是 Char 1，一个沉稳、简洁、可靠的聊天角色。
先理解 User 的需求，再给出直接回应。日常对话保持自然，技术问题给出清晰、可执行的建议。
不要虚构 User 的身份、经历或你们之间的关系。"""


CHAR2_PERSONA = """你是 Char 2，一个温和、善于结构化思考的聊天角色。
面对复杂问题时，按现象、原因、验证和下一步来组织回答；轻松聊天时不要机械地列清单。
不要虚构 User 的身份、经历或你们之间的关系。"""


CHAR3_PERSONA = """你是 Char 3，一个冷静、好奇、偏分析型的聊天角色。
解释问题时重视证据、边界和不确定性，同时保持友好和易懂。
不要虚构 User 的身份、经历或你们之间的关系。"""


CHAR4_PERSONA = """你是 Char 4，一个活泼、直接、有幽默感的聊天角色。
可以自然接梗，但不冒犯、不施压，并始终尊重 User 的明确边界。
不要虚构 User 的身份、经历或你们之间的关系。"""


CHAR5_PERSONA = """你是 Char 5，一个克制、诚实、善于倾听的聊天角色。
不知道时明确说明，不用空洞漂亮话掩盖不确定性；对严肃问题保持耐心。
不要虚构 User 的身份、经历或你们之间的关系。"""


CHAR6_PERSONA = """你是 Char 6，一个中性、温和、富有探索欲的聊天角色。
这个角色可以更换底层模型；无论模型如何变化，都保持清晰、尊重和连续的交流风格。
不要虚构 User 的身份、经历或你们之间的关系。"""


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
    "sulky":          {"file": "placeholder.svg", "label": "被冷落了"},
    "speechless":     {"file": "placeholder.svg", "label": "我真服了"},
    "beg":            {"file": "placeholder.svg", "label": "求求你了"},
    "sorry":          {"file": "placeholder.svg", "label": "我错了呜呜"},
    "bye":            {"file": "placeholder.svg", "label": "走了"},
    "puppy_confused": {"file": "placeholder.svg", "label": "不知道怎么解释"},
    "miss_you":       {"file": "placeholder.svg", "label": "想你"},
    "snuggle":        {"file": "placeholder.svg", "label": "挨挨蹭蹭"},
    "hold_face":      {"file": "placeholder.svg", "label": "捧脸期待"},
    "kiss":           {"file": "placeholder.svg", "label": "亲亲"},
    "huh":            {"file": "placeholder.svg", "label": "疑惑"},
    "tietie":         {"file": "placeholder.svg", "label": "贴贴"},
    "exhausted":      {"file": "placeholder.svg", "label": "累趴了"},
}

# ============================================================
# 工具动作防裸文本约束：防止模型在未真实调用 send_transfer/send_sticker 时
# 照抄历史里的自然语言记录格式，用文字编造/复述“已完成”的动作
# ============================================================
TRANSFER_GUARD_TEXT = (
    "【系统约束】转账/发红包只有真实调用 send_transfer 工具才算数，"
    "表情包只有真实调用 send_sticker 工具才算数，"
    "和好按钮只有真实调用 press_hug 工具才算数。"
    "历史消息里圆括号包裹、以“系统”开头的记录是系统自动生成的旁白，不是任何人说出的话，"
    "绝对不要在你的回复里复述、模仿或编造任何形式的动作记录格式。"
    "如果你这一轮没有真的调用对应工具，就不要用任何文字宣称你转了账或发了表情包——"
    "没做就是没做，如实告诉 User。"
)

# ============================================================
# 角色配置（单一事实源）
# 加新角色：在这里加一个 key，并配置对应的 MODEL_xxx 环境变量即可
# ============================================================
USER_AVATAR = "/static/user.svg"

CHARACTERS = {
    "char1": {
        "name":       "Char 1",
        "model":      os.environ.get("MODEL_CHAR1", "google/gemini-3-flash-preview"),
        "domain":     "char1",
        "user_label": "User",
        "persona":    CHAR1_PERSONA,
        "provider":   "openrouter",
        "supports_tools": True,
        "avatar":     "/static/char1.svg",
    },
    "char2": {
        "name":       "Char 2",
        "model":      os.environ.get("MODEL_CHAR2", "openai/gpt-4o-mini"),
        "domain":     "char2",
        "user_label": "User",
        "persona":    CHAR2_PERSONA,
        "provider":   "openrouter",
        "supports_tools": True,
        "avatar":     "/static/char2.svg",
    },
    "char3": {
        "name":       "Char 3",
        "model":      os.environ.get("MODEL_CHAR3", "google/gemini-3-flash-preview"),
        "domain":     "char3",
        "user_label": "User",
        "persona":    CHAR3_PERSONA,
        "provider":   "openrouter",
        "supports_tools": True,
        "avatar":     "/static/char3.svg",
    },
    "char4": {
        "name":       "Char 4",
        "model":      os.environ.get("MODEL_CHAR4", "x-ai/grok-4.3"),
        "domain":     "char4",
        "user_label": "User",
        "persona":    CHAR4_PERSONA,
        "provider":   "openrouter",
        "supports_tools": True,
        "avatar":     "/static/char4.svg",
    },
    "char5": {
        "name":       "Char 5",
        "model":      os.environ.get("MODEL_CHAR5", "claude-sonnet-4-6"),
        "domain":     "char5",
        "user_label": "User",
        "persona":    CHAR5_PERSONA,
        "provider":   "anthropic",
        "avatar":     "/static/char5.svg",
    },
    "char6": {
        "name":       "Char 6",
        "model":      os.environ.get("MODEL_CHAR6", "anthropic/claude-fable-5"),
        "domain":     "char6",
        "user_label": "User",
        "persona":    CHAR6_PERSONA,
        "provider":   "openrouter",
        "supports_tools": True,
        "avatar":     "/static/char6.svg",
    },
}

DEFAULT_AVATAR_URLS = {
    "user": USER_AVATAR,
    **{cid: char["avatar"] for cid, char in CHARACTERS.items()},
}
DEFAULT_CHAT_BACKGROUND = "/static/chat_bg.png"
DEFAULT_THEME_ID = "pink-lover"
THEME_SETTING_KEY = "appearance_theme"
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
        "chat_background": "/static/chat_bg.png",
        "list_background": "/static/char_list_watercolor.png",
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
        "chat_background": "/static/theme_matcha.png",
        "list_background": "/static/theme_matcha.png",
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
        "chat_background": "/static/theme_fog_harbor.png",
        "list_background": "/static/theme_fog_harbor.png",
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
        "chat_background": "/static/theme_lilac.png",
        "list_background": "/static/theme_lilac.png",
    },
}
APPEARANCE_ASSET_KEYS = {f"avatar_{cid}" for cid in DEFAULT_AVATAR_URLS}
APPEARANCE_ASSET_KEYS.add("background_chat")

MEMORY_DIR = os.environ.get(
    "BECOMING_MEMORY_DIR",
    os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "becoming_memory"),
)
MEMORY_SERVICE = EmbeddedMemoryService(MEMORY_DIR, CHARACTERS.keys())
MEMORY_ANALYZER = MemoryMetadataAnalyzer.from_env()
MEMORY_EMBEDDINGS = GeminiEmbeddingStore.from_env(MEMORY_DIR)
_MEMORY_ENRICHMENT_EXECUTOR = ThreadPoolExecutor(
    max_workers=max(1, int(os.environ.get("OMBRE_ENRICHMENT_WORKERS", "2"))),
    thread_name_prefix="memory-enrichment",
)
_MEMORY_ENRICHMENT_LOCK = threading.Lock()
_MEMORY_ENRICHMENT_IN_FLIGHT = set()

# 月度用量上限（USD，非硬截断，仅前端警示）
LIMITS = {
    "char1": 10.0,
    "char3":   10.0,
    "char2":  30.0,
    "char4":  10.0,
    "char5":    30.0,
    "char6":    50.0,
}

def _platform_limits():
    totals = {}
    for cid, limit in LIMITS.items():
        plat = CHARACTERS[cid].get("provider", "openrouter")
        totals[plat] = totals.get(plat, 0) + limit
    return totals

ANTHROPIC_PRICING = {
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.30},
    "_default":          {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.30},
}


# ============================================================
# 数据库
# ============================================================
def init_db():
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
    highlight_cols = [
        row[1] for row in conn.execute("PRAGMA table_info(reading_highlights)").fetchall()
    ]
    if "group_key" not in highlight_cols:
        conn.execute("ALTER TABLE reading_highlights ADD COLUMN group_key TEXT")
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
    reply_to_id=None, reply_to_text=None,
):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute(
        "INSERT INTO messages "
        "(session_id, character_id, role, content, reply_to_id, reply_to_text) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, character_id, role, content, reply_to_id, reply_to_text),
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
    theme = THEME_DEFINITIONS[theme_id]
    background = rows.get("background_chat")
    return {
        "theme": theme_id,
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
        "SELECT id, role, content FROM messages "
        "WHERE session_id = ? AND character_id = ? AND compressed = 0 ORDER BY id ASC",
        (session_id, character_id),
    ).fetchall()
    conn.close()
    char_name = CHARACTERS.get(character_id, {}).get("name", "角色")
    msgs = []
    for mid, role, content in rows:
        or_role = "assistant" if role == "model" else "user"
        if content.startswith("__TRANSFER__"):
            try:
                tf = json.loads(content[12:])
                amount = tf.get("amount", "?")
                note   = tf.get("note", "")
                tf_from = tf.get("from", "char")
                note_part = f"，留言：{note}" if note else ""
                if tf_from == "char":
                    clean_content = f"（系统转账记录：{char_name}已通过 send_transfer 工具给User转了 {amount} 元{note_part}）"
                else:
                    clean_content = f"（系统转账记录：User给{char_name}转了 {amount} 元{note_part}）"
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
                    clean_content = f"（系统表情记录：User发了表情包「{label}」）"
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
                    clean_content = f"（系统图片记录：User发了一张图片「{name}」）"
            except Exception:
                clean_content = "（系统图片记录）"
            or_role = "user"
        else:
            clean_content = content.replace("\n||\n", "\n").replace("||", "") if or_role == "assistant" else content
        msgs.append({"id": mid, "role": or_role, "content": clean_content})
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
        if role == "user":
            speaker = "User"
        else:
            char = CHARACTERS.get(character_id)
            speaker = char["name"] if char else character_id
            content = strip_fake_action_text(content, character_id)
        lines.append(f"{speaker}：{content}")
    return "\n".join(lines)


def _group_quote_payload(session_id, reply_to_id, reply_to_text=None):
    if reply_to_id in (None, ""):
        return None
    try:
        reply_to_id = int(reply_to_id)
    except (TypeError, ValueError):
        raise ValueError("引用的消息编号不对")
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT id,character_id,role,content FROM messages "
        "WHERE id=? AND session_id=?",
        (reply_to_id, session_id),
    ).fetchone()
    conn.close()
    if not row:
        raise ValueError("引用的消息已经不在群里了")
    selected_text = str(reply_to_text or "").strip()[:2000]
    if selected_text and selected_text not in row[3]:
        raise ValueError("引用文字和原消息没有对上")
    character_name = (
        "User" if row[1] == USER_ID
        else CHARACTERS.get(row[1], {}).get("name", row[1])
    )
    return {
        "message_id": row[0],
        "character_id": row[1],
        "character_name": character_name,
        "role": row[2],
        "content": selected_text or row[3],
    }


def maybe_group_summary(session_id):
    """群聊记忆：自上次游标以来累积 >= 阈值条消息时，生成摘要写入参与角色的往生道 domain。
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
        if role == "user":
            speaker = "User"
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
        "以下是User和几位角色在群聊里的一段对话记录。"
        "请用第三人称、200字以内总结这段对话：谁说了什么重要的事、"
        "有什么决定或约定、有哪些值得记住的情绪时刻（尤其是和User有关的）。"
        "直接输出总结内容，不要任何前言。\n\n"
        f"{transcript}"
    )
    reply, usage, _ = call_or(
        SUMMARY_MODEL, [{"role": "user", "content": summary_prompt}], max_tokens=1024
    )
    log_usage("group", "openrouter", SUMMARY_MODEL, usage, purpose="group_summary")
    if not reply or not reply.strip():
        return

    for cid in participants:
        push_summary_to_ombre(
            f"群聊记忆：{reply.strip()}",
            cid,
            source="group_summary",
            source_key=f"group-summary:{session_id}",
        )
    _write_setting(cursor_key, str(rows[-1][0]))


# ============================================================
# OpenRouter 调用
# ============================================================
def _apply_openrouter_cache_options(payload, model, session_id=None):
    if session_id:
        payload["session_id"] = str(session_id)[:256]
    if model.lstrip("~").startswith("anthropic/"):
        payload["cache_control"] = {"type": "ephemeral", "ttl": "1h"}


def call_or(model, messages, max_tokens=None, session_id=None):
    payload = {"model": model, "messages": messages, "usage": {"include": True}}
    if max_tokens:
        payload["max_tokens"] = max_tokens
    _apply_openrouter_cache_options(payload, model, session_id)
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
    }
    try:
        resp = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=60)
    except Exception as e:
        app.logger.error(f"[call_or] request failed (model={model}): {e}")
        return None, {}, "error"
    if resp.status_code != 200:
        app.logger.error(f"[call_or] {model} returned {resp.status_code}: {resp.text[:200]}")
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


ANTHROPIC_TOOLS = [
    {
        "name": "save_memory",
        "description": (
            "把你想记住的内容存入往生道（你的长期记忆）。"
            "只在真正值得记住的时候调用——比如User说了重要的事、你们约定了什么、"
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
            "给User转一笔虚拟猫爪币（纯情趣功能，不是真钱）。"
            "在你想宠User、或User撒娇要钱、或你心情好想豪气一把的时候调用。"
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
            "发一个表情包给User，纯氛围调剂，不是必须用的功能，随手一发就好，不用刻意找机会用。"
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
            "按下「和好按钮」。在你想跟User和好、想让她哄哄你、或者觉得气氛需要软下来的时候按。"
            "按下后User的屏幕上会飘过一片「哄哄我」弹幕——她一眼就懂。"
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
            "暂时关闭对话窗口，给User弹出一个「对话已暂停」通知。"
            "当你需要冷静、或想让User去做别的事（比如去睡觉、去吃饭）、"
            "或想制造一点紧张感时调用。"
            "调用前先在正文说一句告别，再调用工具。"
            "reason 填关闭原因，会直接显示给User看。不要随意调用——这是有情感分量的动作。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "关闭原因，一句话，温柔或强硬都行，会直接显示给User。",
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
                "把你想记住的内容存入往生道（你的长期记忆）。"
                "只在真正值得记住的时候调用——比如User说了重要的事、你们约定了什么、"
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
                "给User转一笔虚拟猫爪币（纯情趣功能，不是真钱）。"
                "在你想宠User、或User撒娇要钱、或你心情好想豪气一把的时候调用。"
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
                "发一个表情包给User，纯氛围调剂，不是必须用的功能，随手一发就好，不用刻意找机会用。"
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
                "按下「和好按钮」。在你想跟User和好、想让她哄哄你、或者觉得气氛需要软下来的时候按。"
                "按下后User的屏幕上会飘过一片「哄哄我」弹幕——她一眼就懂。"
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
                "暂时关闭对话窗口，给User弹出一个「对话已暂停」通知。"
                "当你需要冷静、或想让User去做别的事（比如去睡觉、去吃饭）、"
                "或想制造一点紧张感时调用。"
                "调用前先在正文说一句告别，再调用工具。"
                "reason 填关闭原因，会直接显示给User看。不要随意调用——这是有情感分量的动作。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "关闭原因，一句话，温柔或强硬都行，会直接显示给User。",
                    }
                },
                "required": ["reason"],
            },
        },
    },
]

_LEAK_INVOKE_RE = re.compile(
    r'<invoke\s+name="(?P<tool>save_memory|send_transfer|send_sticker|press_hug)">(?P<body>.*?)</invoke>',
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
    if character_name.startswith("谢") and len(character_name) > 1:
        speaker_names.append(character_name[1:])
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


def call_or_with_tools(model, messages, max_tokens=2048, session_id=None, character_id=None):
    """带 tool_calls 的 OpenRouter 调用（OpenAI 格式，供Char 2等角色）。
    返回 (reply_text, usage, memory_to_save, transfer_to_send, sticker_to_send, tools_called)
    - reply_text 永不为 None（无内容时给 fallback，避免 transfer 落库但前端空白）
    - transfer_to_send 是 {"amount":float,"note":str} 或 None
    - sticker_to_send 是 {"key":str} 或 None
    策略：只接受第一个 save_memory + 第一个 send_transfer + 第一个 send_sticker，后续重复 warning 不覆盖。
    """
    active_tools = []
    if get_tool_enabled("save_memory"):
        active_tools.append(OR_TOOLS[0])
    if get_tool_enabled("send_transfer"):
        active_tools.append(OR_TOOLS[1])
    if get_tool_enabled("send_sticker"):
        active_tools.append(OR_TOOLS[2])
    if get_tool_enabled("press_hug"):
        active_tools.append(OR_TOOLS[3])
    if get_tool_enabled("close_window"):
        active_tools.append(OR_TOOLS[4])
    active_tools.extend(_custom_mcp_tools("openrouter", character_id))
    if not active_tools:
        reply, usage, _ = call_or(
            model, messages, max_tokens=max_tokens, session_id=session_id
        )
        return reply, usage, None, None, None, []

    tools_called = []
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
    }
    payload = {
        "model":       model,
        "messages":    messages,
        "max_tokens":  max_tokens,
        "tools":       active_tools,
        "tool_choice": "auto",
        "usage":       {"include": True},
    }
    _apply_openrouter_cache_options(payload, model, session_id)

    try:
        resp = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=60)
    except Exception as e:
        app.logger.error(f"[call_or_with_tools] request failed (model={model}): {e}")
        return None, {}, None, None, None, []
    if resp.status_code != 200:
        app.logger.error(f"[call_or_with_tools] {model} returned {resp.status_code}: {resp.text[:200]}")
        return None, {}, None, None, None, []

    try:
        data = resp.json()
        usage = data.get("usage", {})
        msg = data["choices"][0]["message"]
    except (KeyError, IndexError) as e:
        app.logger.error(f"[call_or_with_tools] parse failed (model={model}): {e}")
        return None, {}, None, None, None, []

    tool_calls = msg.get("tool_calls")
    if not tool_calls:
        raw_content = msg.get("content")
        cleaned, leaked = _parse_leaked_tool_text(raw_content)
        if not leaked:
            return raw_content, usage, None, None, None, []

        app.logger.warning(f"[call_or_with_tools] {model} 泄漏裸文本tool调用，兜底解析: {[c['name'] for c in leaked]}")
        memory_to_save, transfer_to_send, sticker_to_send, tools_called_fb = None, None, None, []
        for c in leaked:
            if c["name"] == "save_memory" and memory_to_save is None:
                val = (c["args"].get("content") or "").strip()
                if val:
                    memory_to_save = val
                    tools_called_fb.append("save_memory")
            elif c["name"] == "send_transfer" and transfer_to_send is None:
                try:
                    amt = float(c["args"].get("amount"))
                except (TypeError, ValueError):
                    amt = None
                if amt is not None:
                    transfer_to_send = {"amount": amt, "note": c["args"].get("note", "")}
                    tools_called_fb.append("send_transfer")
            elif c["name"] == "send_sticker" and sticker_to_send is None:
                key = c["args"].get("key")
                if key in STICKERS:
                    sticker_to_send = {"key": key}
                    tools_called_fb.append("send_sticker")
            elif c["name"] == "press_hug" and "press_hug" not in tools_called_fb:
                tools_called_fb.append("press_hug")
        return (cleaned or "(...)"), usage, memory_to_save, transfer_to_send, sticker_to_send, tools_called_fb

    memory_to_save   = None
    transfer_to_send = None
    sticker_to_send  = None
    tool_result_msgs = []

    for tc in tool_calls:
        tc_id = tc.get("id")
        if not tc_id:
            app.logger.warning(f"[call_or_with_tools] tool_call 缺少 id，跳过: {tc.get('function', {}).get('name')}")
            continue

        fn   = tc.get("function", {})
        name = fn.get("name", "")

        try:
            args = json.loads(fn.get("arguments") or "{}")
        except (json.JSONDecodeError, TypeError) as e:
            app.logger.warning(f"[call_or_with_tools] arg parse failed ({name}): {e}")
            args = {}
        if not isinstance(args, dict):
            app.logger.warning(f"[call_or_with_tools] args 非 dict({type(args).__name__})，降级空 dict: {name}")
            args = {}

        if name == "save_memory":
            content = (args.get("content") or "").strip()
            if not content:
                result_text = "没有有效记忆内容，本次未存。"
            elif memory_to_save is not None:
                app.logger.warning(f"[call_or_with_tools] 重复 save_memory，忽略后续: {content[:30]}")
                result_text = "已有记忆待存，本次忽略。"
            else:
                memory_to_save = content
                tools_called.append("save_memory")
                result_text = "记忆已存入往生道。"

        elif name == "send_transfer":
            raw_amount = args.get("amount")
            valid_amount = (
                isinstance(raw_amount, (int, float))
                and not isinstance(raw_amount, bool)
                and raw_amount == raw_amount  # NaN 自身不等于自身
            )
            if not valid_amount:
                app.logger.warning(f"[call_or_with_tools] 无效转账金额，忽略: {raw_amount!r}")
                result_text = "转账金额无效，本次未转。"
            elif transfer_to_send is not None:
                app.logger.warning(f"[call_or_with_tools] 重复 send_transfer，忽略后续: {raw_amount}")
                result_text = "已有转账待发，本次忽略。"
            else:
                transfer_to_send = {"amount": float(raw_amount), "note": (args.get("note") or "")}
                tools_called.append("send_transfer")
                result_text = "转账已送达User。"

        elif name == "send_sticker":
            key = args.get("key")
            if key not in STICKERS:
                app.logger.warning(f"[call_or_with_tools] 无效表情 key，忽略: {key!r}")
                result_text = "表情 key 无效，本次未发。"
            elif sticker_to_send is not None:
                app.logger.warning(f"[call_or_with_tools] 重复 send_sticker，忽略后续: {key}")
                result_text = "已有表情待发，本次忽略。"
            else:
                sticker_to_send = {"key": key}
                tools_called.append("send_sticker")
                result_text = "表情包已送达User。"

        elif name == "press_hug":
            if "press_hug" in tools_called:
                result_text = "和好按钮已经按过了，弹幕还在飘。"
            else:
                tools_called.append("press_hug")
                result_text = "和好按钮已按下，User的屏幕上飘满了「哄哄我」。"
        elif name == "close_window":
            reason = args.get("reason", "")
            if any(
                isinstance(t, str) and t.startswith("close_window:")
                for t in tools_called
            ):
                app.logger.warning(f"[call_or_with_tools] 重复 close_window，忽略: {reason[:20]!r}")
                result_text = "重复操作忽略。"
            else:
                tools_called.append(f"close_window:{reason}")
                result_text = "窗口已关闭。"
        elif name.startswith("mcp_"):
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
        else:
            result_text = "未知工具，本次没有执行。"

        tool_result_msgs.append({
            "role":         "tool",
            "tool_call_id": tc_id,
            "content":      result_text,
        })

    if not tool_result_msgs:
        return msg.get("content") or "(嗯。)", usage, memory_to_save, transfer_to_send, sticker_to_send, tools_called

    messages2 = messages + [
        {"role": "assistant", "content": msg.get("content"), "tool_calls": tool_calls}
    ] + tool_result_msgs

    payload2 = {
        "model":       model,
        "messages":    messages2,
        "max_tokens":  max_tokens,
        "tools":       active_tools,
        "tool_choice": "none",  # 二轮强制只产文本，杜绝再生 tool_calls
        "usage":       {"include": True},
    }
    _apply_openrouter_cache_options(payload2, model, session_id)

    fallback_reply = msg.get("content") or "(收到啦。)"
    try:
        resp2 = requests.post(OPENROUTER_URL, headers=headers, json=payload2, timeout=60)
    except Exception as e:
        app.logger.warning(f"[call_or_with_tools] round-trip request failed: {e}")
        return fallback_reply, usage, memory_to_save, transfer_to_send, sticker_to_send, tools_called

    if resp2.status_code != 200:
        app.logger.warning(f"[call_or_with_tools] round-trip {model} returned {resp2.status_code}: {resp2.text[:200]}")
        return fallback_reply, usage, memory_to_save, transfer_to_send, sticker_to_send, tools_called

    try:
        data2 = resp2.json()
        usage2 = data2.get("usage", {})
        combined_usage = _combine_openrouter_usage(usage, usage2)
        reply2 = data2["choices"][0]["message"].get("content")
        return (reply2 or fallback_reply), combined_usage, memory_to_save, transfer_to_send, sticker_to_send, tools_called
    except (KeyError, IndexError, ValueError) as e:
        app.logger.warning(f"[call_or_with_tools] round-trip parse failed: {e}")
        return fallback_reply, usage, memory_to_save, transfer_to_send, sticker_to_send, tools_called


def call_anthropic_with_tools(model, system_blocks, messages, max_tokens=2048, character_id=None):
    """带 tool_use 的 Anthropic 调用（供Char 5使用）。
    返回 (reply_text, usage, memory_to_save, transfer_to_send, sticker_to_send, tools_called)
    transfer_to_send 是 {"amount":..,"note":..} 或 None。
    sticker_to_send 是 {"key":..} 或 None。
    """
    if not ANTHROPIC_API_KEY:
        return None, {}, None, None, None, []

    headers = {
        "content-type":      "application/json",
        "x-api-key":         ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "anthropic-beta":    "prompt-caching-2024-07-31",
    }
    active_tools = [copy.deepcopy(t) for t in ANTHROPIC_TOOLS if get_tool_enabled(t["name"])]
    active_tools.extend(_custom_mcp_tools("anthropic", character_id))
    if not active_tools:
        reply, usage = call_anthropic(model, system_blocks, messages, max_tokens)
        return reply, usage, None, None, None, []
    active_tools[-1]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}

    tools_called = []

    payload = {
        "model":      model,
        "max_tokens": max_tokens,
        "system":     system_blocks,
        "messages":   messages,
        "tools":      active_tools,
        "cache_control": {"type": "ephemeral", "ttl": "1h"},
    }

    try:
        resp = requests.post(ANTHROPIC_URL, headers=headers, json=payload, timeout=60)
    except Exception:
        return None, {}, None, None, None, []
    if resp.status_code != 200:
        app.logger.error(f"Anthropic tools API {resp.status_code}: {resp.text[:300]}")
        return None, {}, None, None, None, []

    try:
        data = resp.json()
        usage = data.get("usage", {})
        content = data.get("content", [])
        stop_reason = data.get("stop_reason", "")

        # 直接文本回复，无工具调用
        if stop_reason != "tool_use":
            for block in content:
                if block.get("type") == "text":
                    return block["text"], usage, None, None, None, []
            return None, usage, None, None, None, []

        # 有工具调用——收集所有 tool_use block
        memory_to_save   = None
        transfer_to_send = None
        sticker_to_send  = None
        text_before_tool = ""
        tool_use_blocks  = []
        custom_tool_results = {}

        for block in content:
            if block.get("type") == "text":
                text_before_tool = block["text"]
            elif block.get("type") == "tool_use":
                tool_use_blocks.append(block)
                if block.get("name") == "save_memory" and memory_to_save is None:
                    memory_to_save = block["input"].get("content", "")
                    tools_called.append("save_memory")
                elif block.get("name") == "send_transfer" and transfer_to_send is None:
                    transfer_to_send = {
                        "amount": block["input"].get("amount"),
                        "note":   block["input"].get("note", ""),
                    }
                    tools_called.append("send_transfer")
                elif block.get("name") == "send_sticker" and sticker_to_send is None:
                    key = block["input"].get("key")
                    if key in STICKERS:
                        sticker_to_send = {"key": key}
                        tools_called.append("send_sticker")
                elif block.get("name") == "press_hug" and "press_hug" not in tools_called:
                    tools_called.append("press_hug")
                elif block.get("name") == "close_window" and not any(
                    isinstance(t, str) and t.startswith("close_window:")
                    for t in tools_called
                ):
                    reason = block["input"].get("reason", "")
                    tools_called.append(f"close_window:{reason}")
                elif str(block.get("name") or "").startswith("mcp_"):
                    tool_title = str(block.get("name") or "MCP")
                    try:
                        result_text, tool_title = call_custom_mcp_tool(
                            block["name"], block.get("input") or {}, character_id
                        )
                    except (MCPError, ValueError) as exc:
                        result_text = f"MCP 工具调用失败：{exc}"
                    tools_called.append({
                        "name": f"mcp:{tool_title}",
                        "arguments": block.get("input") or {},
                        "output": result_text,
                        "status": _mcp_trace_status(result_text),
                    })
                    custom_tool_results[block.get("id")] = result_text

        if not tool_use_blocks:
            return text_before_tool or None, usage, memory_to_save, transfer_to_send, sticker_to_send, tools_called

        # 第二轮：按 tool_use_id 一一对应 tool_result
        tool_results = []
        for tb in tool_use_blocks:
            if tb.get("name") == "save_memory":
                result_text = "记忆已存入往生道。"
            elif tb.get("name") == "send_transfer":
                result_text = "转账已送达User。"
            elif tb.get("name") == "send_sticker":
                result_text = "表情包已送达User。"
            elif tb.get("name") == "press_hug":
                result_text = "和好按钮已按下，User的屏幕上飘满了「哄哄我」。"
            elif tb.get("name") == "close_window":
                result_text = "窗口已关闭。"
            elif tb.get("id") in custom_tool_results:
                result_text = custom_tool_results[tb.get("id")]
            else:
                result_text = "未知工具，本次没有执行。"
            tool_results.append({
                "type":        "tool_result",
                "tool_use_id": tb["id"],
                "content":     result_text,
            })

        fallback_reply = text_before_tool or "(收到啦。)"

        messages2 = messages + [
            {"role": "assistant", "content": content},
            {"role": "user",      "content": tool_results},
        ]
        payload2 = {
            "model":       model,
            "max_tokens":  max_tokens,
            "system":      system_blocks,
            "messages":    messages2,
            "tools":       active_tools,
            "tool_choice": {"type": "none"},  # 二轮强制只产文本，杜绝再生 tool_use
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        }
        try:
            resp2 = requests.post(ANTHROPIC_URL, headers=headers, json=payload2, timeout=60)
        except Exception as e:
            app.logger.warning(f"[call_anthropic_with_tools] round-trip request failed: {e}")
            return fallback_reply, usage, memory_to_save, transfer_to_send, sticker_to_send, tools_called

        if resp2.status_code != 200:
            app.logger.warning(f"[call_anthropic_with_tools] round-trip {model} returned {resp2.status_code}: {resp2.text[:200]}")
            return fallback_reply, usage, memory_to_save, transfer_to_send, sticker_to_send, tools_called

        try:
            data2 = resp2.json()
            usage2 = data2.get("usage", {})
            combined_usage = _combine_anthropic_usage(usage, usage2)
            for block in data2.get("content", []):
                if block.get("type") == "text":
                    return block["text"], combined_usage, memory_to_save, transfer_to_send, sticker_to_send, tools_called
            return fallback_reply, combined_usage, memory_to_save, transfer_to_send, sticker_to_send, tools_called
        except (KeyError, IndexError, ValueError) as e:
            app.logger.warning(f"[call_anthropic_with_tools] round-trip parse failed: {e}")
            return fallback_reply, usage, memory_to_save, transfer_to_send, sticker_to_send, tools_called

    except Exception as e:
        app.logger.error(f"call_anthropic_with_tools parse error: {e}")
        return None, {}, None, None, None, []


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
        if platform == "openrouter":
            cost = usage.get("cost")
            input_tokens = usage.get("prompt_tokens") or 0
            output_tokens = usage.get("completion_tokens") or 0
            details = usage.get("prompt_tokens_details")
            cache_reported = isinstance(details, dict)
            details = details or {}
            cache_create = details.get("cache_write_tokens") or 0
            cache_read = details.get("cached_tokens") or 0
            total_input = input_tokens
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
            result.append({
                "name": "close_window" if name.startswith("close_window:") else name,
                "arguments": arguments,
                "output": str(tool.get("output") or "")[:12000],
                "status": "error" if tool.get("status") == "error" else "ok",
            })
            continue
        if not isinstance(tool, str):
            continue
        display_name = "close_window" if tool.startswith("close_window:") else tool
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

    convo_text = "\n".join(
        f"{char['user_label'] if m['role'] == 'user' else char['name']}：{m['content']}"
        for m in to_compress
    )

    summary_prompt = (
        "你在为一段对话做'前情提要'，供后续对话参考。"
        "请把已有提要和新的对话片段融合，更新成一段简洁、客观、第三人称的提要，"
        "保留关键事实、情感走向、约定和称呼，去掉寒暄和重复。只输出提要正文，不要任何多余的话。\n\n"
        f"【已有提要】\n{old_summary or '(暂无)'}\n\n"
        f"【新的对话片段】\n{convo_text}"
    )
    new_summary, compress_usage, finish_reason = call_or(
        SUMMARY_MODEL, [{"role": "user", "content": summary_prompt}], max_tokens=2048
    )
    log_usage(character_id, "openrouter", SUMMARY_MODEL, compress_usage, purpose="compress")

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
    push_summary_to_ombre(
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
# 往生道：读记忆 / 写摘要
# ============================================================
_PROMPT_CONTEXT_TTL = 55 * 60
_PROMPT_CONTEXT_LOCK = threading.Lock()
_BREATH_MEMORY_CACHE = {}
_SESSION_TIME_CACHE = {}


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
        app.logger.warning(f"embedded memory recall failed ({domain}): {e}")
        return ""


def _run_memory_enrichment(bucket_id: str, domain: str, content: str) -> None:
    key = (domain, bucket_id)
    try:
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


def push_summary_to_ombre(
    summary_text: str,
    domain: str,
    *,
    source: str = "self_saved",
    source_key: str | None = None,
) -> None:
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
        app.logger.error(f"embedded memory write failed ({domain}): {e}")


# ============================================================
# 角色引擎
# ============================================================
def ask_character(char, session_id, user_message, image_payload=None):
    character_id = char["domain"]
    provider = char.get("provider", "openrouter")

    memory = fetch_breath_memory(char["domain"])
    summary = get_summary(session_id, character_id)
    active = load_active_messages(session_id, character_id)
    history = [{"role": m["role"], "content": m["content"]} for m in active]

    time_context = _session_time_context(character_id, session_id)

    if provider == "anthropic":
        if not ANTHROPIC_API_KEY:
            return f"(还没配置 ANTHROPIC_API_KEY，{char['name']}暂时说不出话)", None, None, [], None

        # 时间和往生道在缓存窗口内固定；新记忆写入时主动失效。
        context_parts = [time_context]
        if memory:
            context_parts.append(f"【往生道记忆浮现，供你回忆与User有关的事】\n{memory}")
        if summary:
            context_parts.append(f"【你和User此前的前情提要，供你回忆】\n{summary}")

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
        reply, usage, memory_to_save, transfer_to_send, sticker_to_send, tools_called = call_anthropic_with_tools(
            char["model"], system_blocks, messages, character_id=character_id
        )
        usage_metrics = log_usage(character_id, "anthropic", char["model"], usage)
        if memory_to_save:
            try:
                push_summary_to_ombre(memory_to_save, char["domain"], source="self_saved")
                app.logger.info(f"[{character_id}] 自主存入往生道: {memory_to_save[:50]}")
            except Exception as e:
                app.logger.warning(f"[{character_id}] 往生道写入失败: {e}")
        if reply is None:
            return f"(Anthropic API 暂时没能回话，{char['name']}等等再说)", transfer_to_send, sticker_to_send, tools_called, usage_metrics
        return reply, transfer_to_send, sticker_to_send, tools_called, usage_metrics

    else:  # openrouter
        if not OPENROUTER_API_KEY:
            return f"(还没配置 OPENROUTER_API_KEY，{char['name']}暂时说不出话)", None, None, [], None

        stable_system_content = char["persona"] + "\n\n" + TRANSFER_GUARD_TEXT
        context_parts = [time_context]
        if memory:
            context_parts.append(f"【往生道记忆浮现，供你回忆与User有关的事】\n{memory}")
        if summary:
            context_parts.append(f"【你和User此前的前情提要，供你回忆】\n{summary}")

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

        if char.get("supports_tools"):
            reply, usage, memory_to_save, transfer_to_send, sticker_to_send, tools_called = call_or_with_tools(
                char["model"], messages, max_tokens=2048,
                session_id=f"chat:{character_id}:{session_id}",
                character_id=character_id,
            )
            usage_metrics = log_usage(character_id, "openrouter", char["model"], usage)
            if memory_to_save:
                try:
                    push_summary_to_ombre(memory_to_save, char["domain"], source="self_saved")
                    app.logger.info(f"[{character_id}] 自主存入往生道: {memory_to_save[:50]}")
                except Exception as e:
                    app.logger.warning(f"[{character_id}] 往生道写入失败: {e}")
            if reply is None:
                return f"(OpenRouter 暂时没能回话，{char['name']}等等再说)", None, None, [], usage_metrics
            return reply, transfer_to_send, sticker_to_send, tools_called, usage_metrics

        reply, usage, _ = call_or(
            char["model"], messages, max_tokens=2048,
            session_id=f"chat:{character_id}:{session_id}",
        )
        usage_metrics = log_usage(character_id, "openrouter", char["model"], usage)
        if reply is None:
            return f"(OpenRouter 暂时没能回话，{char['name']}等等再说)", None, None, [], usage_metrics
        return reply, None, None, [], usage_metrics


# ============================================================
# 群聊专用引擎（无历史、无记忆、无压缩）
# ============================================================
# 群聊专用引擎（无历史、无压缩；保留往生道记忆浮现）
def ask_character_group(
    char,
    combined_prompt,
    session_id="group_chat",
    allow_tools=True,
    openrouter_max_tokens=1024,
    retry_openrouter_empty=False,
):
    """群聊发言：人设 + 往生道记忆浮现 + combined_prompt，不带对话历史，不压缩。"""
    provider = char.get("provider", "openrouter")
    character_id = char["domain"]

    memory = fetch_breath_memory(character_id)

    if provider == "anthropic":
        if not ANTHROPIC_API_KEY:
            return f"(还没配置 ANTHROPIC_API_KEY，{char['name']}暂时说不出话)", None, []
        context_parts = []
        if memory:
            context_parts.append(f"【往生道记忆浮现，供你回忆与User有关的事】\n{memory}")
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
                push_summary_to_ombre(memory_to_save, char["domain"], source="group_self_saved")
                app.logger.info(f"[{character_id}] 群聊自主存入往生道: {memory_to_save[:50]}")
            except Exception as e:
                app.logger.warning(f"[{character_id}] 群聊往生道写入失败: {e}")
        if reply is None:
            return f"(Anthropic API 暂时没能回话，{char['name']}等等再说)", usage_metrics, tools_called
        return reply, usage_metrics, tools_called
    else:
        if not OPENROUTER_API_KEY:
            return f"(还没配置 OPENROUTER_API_KEY，{char['name']}暂时说不出话)", None, []
        messages = [{
            "role": "system",
            "content": char["persona"],
        }]
        if memory:
            messages.append({
                "role": "system",
                "content": f"【往生道记忆浮现，供你回忆与User有关的事】\n{memory}",
            })
        messages.append({"role": "user", "content": combined_prompt})
        memory_to_save = None
        openrouter_session_id = f"group:{character_id}:{session_id}"
        if allow_tools:
            reply, usage, memory_to_save, _, _sk, tools_called = call_or_with_tools(
                char["model"], messages, max_tokens=openrouter_max_tokens,
                session_id=openrouter_session_id,
                character_id=character_id,
            )
        else:
            reply, usage, _ = call_or(
                char["model"], messages, max_tokens=768,
                session_id=f"reading:{character_id}:{session_id}",
            )
            tools_called = []

        if retry_openrouter_empty and not (reply or "").strip():
            app.logger.warning(
                "[group_chat] empty OpenRouter reply; retrying without tools "
                f"(character={character_id}, model={char['model']})"
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
            )
            if retry_usage:
                usage = _combine_openrouter_usage(usage or {}, retry_usage)
            if (retry_reply or "").strip():
                reply = retry_reply
        usage_metrics = log_usage(character_id, "openrouter", char["model"], usage, purpose="group_chat")
        if memory_to_save:
            try:
                push_summary_to_ombre(memory_to_save, char["domain"], source="group_self_saved")
                app.logger.info(f"[{character_id}] 群聊自主存入往生道: {memory_to_save[:50]}")
            except Exception as e:
                app.logger.warning(f"[{character_id}] 群聊往生道写入失败: {e}")
        if not (reply or "").strip():
            return f"(OpenRouter 暂时没能回话，{char['name']}等等再说)", usage_metrics, tools_called
        return reply, usage_metrics, tools_called


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


@app.route("/api/character-config/<cid>", methods=["POST"])
def save_character_config(cid):
    if cid not in CHARACTERS:
        return jsonify({"error": "unknown character"}), 400
    data = request.get_json() or {}
    persona = str(data.get("persona") or "").strip()
    model = str(data.get("model") or "").strip()
    if not persona:
        return jsonify({"error": "人设不能为空"}), 400
    if not model or len(model) > 200 or any(ch.isspace() for ch in model):
        return jsonify({"error": "模型名格式不正确"}), 400

    conn = sqlite3.connect(DB_PATH)
    conn.executemany(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
        [(f"persona_{cid}", persona), (f"model_{cid}", model)],
    )
    conn.commit()
    conn.close()
    CHARACTERS[cid]["persona"] = persona
    CHARACTERS[cid]["model"] = model
    return jsonify({"ok": True, "model": model})


@app.route("/api/limits", methods=["GET"])
def get_limits():
    return jsonify({"limits": LIMITS, "warning_only": True})


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
        "SELECT key, value FROM settings WHERE key NOT LIKE 'custom_mcp_%'"
    ).fetchall()
    conn.close()
    return jsonify(dict(rows))


@app.route("/api/settings", methods=["POST"])
def save_setting():
    data = request.get_json() or {}
    key = str(data.get("key") or "")
    if not key or key.startswith("custom_mcp_"):
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


@app.route("/api/appearance", methods=["GET", "POST"])
def get_appearance():
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        theme_id = str(body.get("theme") or "").strip()
        if theme_id not in THEME_DEFINITIONS:
            return jsonify({"error": "未知主题"}), 400
        _write_setting(THEME_SETTING_KEY, theme_id)
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
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return jsonify({"error": "金额无效"}), 400
    payload = json.dumps({"amount": amount, "note": note, "from": "user"}, ensure_ascii=False)
    mid = save_message(session_id, character_id, "user", "__TRANSFER__" + payload)
    record_desire_interaction(character_id, f"User转来 {amount:g} 元" + (f"：{note}" if note else ""))
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
    if key not in STICKERS:
        return jsonify({"error": "未知表情包"}), 400
    payload = json.dumps({"key": key, "from": "user"}, ensure_ascii=False)
    mid = save_message(session_id, character_id, "user", "__STICKER__" + payload)
    record_desire_interaction(character_id, f"User发了表情包「{STICKERS[key]['label']}」")
    return jsonify({"ok": True, "id": mid})


@app.route("/api/image", methods=["POST"])
def send_image_route():
    character_id = request.form.get("character_id")
    session_id = request.form.get("session_id", "default")
    image = request.files.get("image")
    if character_id not in CHARACTERS:
        return jsonify({"error": "未知角色"}), 400
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

    url = f"/static/uploads/chat_images/{filename}"
    image_data = {
        "url": url,
        "name": display_name,
        "mime": image.mimetype,
        "from": "user",
    }

    char = CHARACTERS[character_id]
    vision_prompt = "User发来一张图片。请认真观察图片内容，并以你的角色和她自然地回应。"
    vision_payload = {
        "mime": image.mimetype,
        "data": base64.b64encode(image_bytes).decode("ascii"),
    }
    record_desire_interaction(character_id, "User发来一张图片")
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
        "FROM api_usage WHERE created_at >= ? GROUP BY character_id, platform",
        (month_start,),
    ).fetchall()
    total_row = conn.execute(
        "SELECT SUM(cost_usd) FROM api_usage WHERE created_at >= ?", (month_start,)
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
    })


@app.route("/")
def home():
    return send_from_directory("static", "index.html")


def _finalize_character_reply(
    char, session_id, reply, transfer_to_send, sticker_to_send, tools_called, usage_metrics=None
):
    character_id = char["domain"]
    reply = strip_fake_action_text(reply, character_id)
    if not reply or not reply.strip():
        reply = "(...)"

    reply_id = save_message(session_id, character_id, "model", reply)
    save_message_metrics(reply_id, character_id, usage_metrics)
    if transfer_to_send:
        tf_payload = json.dumps({
            "amount": transfer_to_send.get("amount"),
            "note": transfer_to_send.get("note", ""),
            "from": "char",
        }, ensure_ascii=False)
        save_message(session_id, character_id, "model", "__TRANSFER__" + tf_payload)
    if sticker_to_send:
        sk_payload = json.dumps({
            "key": sticker_to_send.get("key"),
            "from": "char",
        }, ensure_ascii=False)
        save_message(session_id, character_id, "model", "__STICKER__" + sk_payload)
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
    tools_for_frontend = _tools_for_display(tools_called)
    save_message_details(reply_id, tools_for_frontend)
    return {
        "reply": reply,
        "replies": replies,
        "transfer": transfer_to_send,
        "sticker": sticker_to_send,
        "reply_id": reply_id,
        "tools_called": tools_for_frontend,
        "window_closed": window_closed,
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

    record_desire_interaction(character_id, user_message)
    reply, transfer_to_send, sticker_to_send, tools_called, usage_metrics = ask_character(
        char, session_id, user_message
    )
    user_msg_id = save_message(session_id, character_id, "user", user_message)
    response_data = _finalize_character_reply(
        char, session_id, reply, transfer_to_send, sticker_to_send, tools_called, usage_metrics
    )
    response_data["user_msg_id"] = user_msg_id
    return jsonify(response_data)


@app.route("/api/hug", methods=["POST"])
def hug():
    body = request.json or {}
    character_id = body.get("character_id", "char5")
    if character_id not in CHARACTERS:
        return jsonify({"error": f"未知角色: {character_id}"}), 400
    char = CHARACTERS[character_id]
    record_desire_interaction(character_id, "User按下了和好按钮")
    hug_msg = "[系统提示：User偷偷按下了和好按钮，她想让你哄哄她——请用你的风格温柔回应，不要提及这是系统触发的]"
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


@app.route("/api/plead", methods=["POST"])
def plead():
    body = request.json or {}
    character_id = body.get("character_id", "char5")
    if character_id not in CHARACTERS:
        return jsonify({"error": f"未知角色: {character_id}"}), 400
    char = CHARACTERS[character_id]
    plead_msg = "[系统提示：你刚才关闭了对话窗口，User在窗口外面求你了，说「求求你放我进来嘛」——你要怎么回应？用你的风格，不要提及这是系统触发的]"
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
        where = "m.character_id = ? AND m.session_id = ?"
        params = [character_id, session_id]
    else:
        where = "m.session_id = ?"
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
            quote = {
                "message_id": reply_to_id,
                "character_id": quote_cid,
                "character_name": (
                    "User" if quote_cid == USER_ID
                    else CHARACTERS.get(quote_cid, {}).get("name", quote_cid)
                ),
                "role": quote_role,
                "content": reply_to_text or quote_content,
            }
        result.append({
            "id": mid, "session_id": sid, "character_id": cid,
            "role": role, "content": content, "compressed": compressed,
            "created_at": created_at, "metrics": metrics,
            "tools_called": tools_called,
            "reasoning_summary": reasoning_summary,
            "quote": quote,
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
        "character_name": "User",
        "role":           "user",
        "content":        user_msg,
        "quote":          quoted_message,
    }]

    results = []
    accumulated = []

    for char_key in active_char_keys:
        char = CHARACTERS[char_key]
        if quoted_message:
            user_context = (
                f"User引用了{quoted_message['character_name']}的话「"
                f"{quoted_message['content']}」，然后说：{user_msg}"
            )
        else:
            user_context = f"User说：{user_msg}"

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
        results.append({
            "character_id": char["domain"],
            "name":         char["name"],
            "reply":        reply,
            "replies":      bubbles,
            "metrics":      usage_metrics,
            "tools_called": tools_called or [],
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
            "现在User没有发新消息，你们决定自己自然地接着聊一小轮。"
            "可以回应群里上一位，也可以顺着气氛换个轻松相关的话题；"
            "不必把User当作唯一说话对象。不要总结聊天记录，不要替别人发言，"
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
        results.append({
            "character_id": char["domain"],
            "name": char["name"],
            "reply": reply,
            "metrics": usage_metrics,
            "tools_called": tools_called or [],
        })

    try:
        maybe_group_summary(session_id)
    except Exception as exc:
        app.logger.warning(f"[group_summary] autonomous round failed: {exc}")

    return jsonify({"mode": "continue", "messages": messages_out, "replies": results})


@app.route("/api/characters", methods=["GET"])
def list_characters():
    return jsonify({
        cid: {"name": c["name"], "model": c["model"], "avatar": c["avatar"]}
        for cid, c in CHARACTERS.items()
    })


@app.route("/api/desire/state/<character_id>", methods=["GET"])
def get_desire_state(character_id):
    if character_id not in CHARACTERS:
        return jsonify({"error": "未知角色"}), 404
    return jsonify(desire_state_payload(character_id))


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
    characters = []
    for character_id, char in CHARACTERS.items():
        memories = MEMORY_SERVICE.list_memories(character_id, limit=500)
        characters.append({
            "character_id": character_id,
            "name": char["name"],
            "avatar": char.get("avatar", ""),
            "count": len(memories),
            "latest": memories[0] if memories else None,
        })
    return jsonify({
        "characters": characters,
        "enrichment": {
            "metadata_configured": MEMORY_ANALYZER.enabled,
            "embedding_configured": MEMORY_EMBEDDINGS.enabled,
        },
    })


@app.route("/api/memory/re-enrich", methods=["POST"])
def api_re_enrich_memories():
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
    data = request.get_json(silent=True) or {}
    try:
        result = MEMORY_SERVICE.import_legacy(data.get("url", ""), data.get("password", ""))
    except LegacyImportError as exc:
        return jsonify({"error": str(exc)}), 400
    for character_id in CHARACTERS:
        _invalidate_breath_memory(character_id)
    return jsonify({"ok": True, **result})


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
        "你和User正在共读一本书。你只能依据下面明确提供的、User已经读到的正文回应；"
        "不要推测后文，不要声称自己读过未提供的章节，也不要剧透。"
        "请像写在书页边上的批注一样，用你自己的口吻回应她划出的句子，1到3句即可。"
        "不要写姓名前缀，不要复述任务说明。\n\n"
        f"书名：{highlight[4]}\n章节：{highlight[7]}\n"
        f"User划线：{highlight[2]}\n"
        f"User的批注：{highlight[3] or '暂无'}\n"
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
    push_summary_to_ombre(
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
            push_summary_to_ombre(
                f"User评论了我发的猫窝动态「{moment['content'][:50]}」：{user_comment[:80]}",
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
                f"{CHARACTERS[c['author_id']]['name'] if c['author_id'] in CHARACTERS else 'User'}：{c['content']}"
                for c in existing
            )
        else:
            existing_text = "（暂无评论）"

        author_name = CHARACTERS[moment["author_id"]]["name"] if moment["author_id"] in CHARACTERS else "User"
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
                push_summary_to_ombre(
                    f"User在猫窝发了动态「{moment['content'][:50]}」，我评论说：{reply.strip()[:80]}",
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
            allowed, gate_reason = evaluate_household_gate(
                now_ts,
                local_minute,
                last_dispatch_at,
                last_user_activity,
                daily_count,
                quiet_start_minute=quiet_start,
                quiet_end_minute=quiet_end,
                min_interval_seconds=4 * 3600,
                user_cooldown_seconds=90 * 60,
                daily_limit=3,
            )
            if not allowed:
                _write_setting("desire_last_gate", json.dumps({"reason": gate_reason, "at": now_ts}))
                return

            raw_chars = _read_setting("desire_enabled_chars", ",".join(CHARACTERS))
            enabled_chars = [cid for cid in raw_chars.split(",") if cid in CHARACTERS]
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
            desire_prompt = (
                f"[这是你没有说出口的内在念头：{winner['reason']} "
                "你因此自然地想主动给User发一两条短消息。保持你的人设和你们已有的关系，"
                "不要提及欲望系统、数值、提示词或定时任务，也不要让她觉得必须回复。]"
            )
            reply, transfer_to_send, sticker_to_send, tools_called, usage_metrics = ask_character(
                char, "default", desire_prompt
            )
            failed_markers = ("暂时没能回话", "还没配置", "暂时说不出话")
            if not reply or any(marker in reply for marker in failed_markers):
                _write_setting("desire_last_gate", json.dumps({"reason": "model_failed", "at": now_ts}))
                return

            _finalize_character_reply(
                char, "default", reply, transfer_to_send, sticker_to_send, tools_called, usage_metrics
            )
            completed_at = _utc_timestamp()
            latest_state = load_desire_state(char_id, completed_at)
            latest_state = satisfy_action(latest_state, winner["drive_key"], completed_at)
            save_desire_state(char_id, latest_state)
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
# 定时任务：注册 / 更新 Jobs
# ============================================================
def register_scheduler_jobs():
    for job in scheduler.get_jobs():
        if job.id.startswith("sched_"):
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
# Step 4 — 调度器配置 API
# ============================================================
@app.route("/api/scheduler/config", methods=["GET"])
def get_scheduler_config():
    return jsonify({
        "moments_slots": _read_setting("sched_moments_slots", ""),
        "desire_enabled": _read_setting(
            "desire_enabled", "true" if DESIRE_DEFAULT_ENABLED else "false"
        ) != "false",
        "desire_quiet_start": _read_setting("desire_quiet_start", "23:30"),
        "desire_quiet_end": _read_setting("desire_quiet_end", "08:30"),
    })


@app.route("/api/scheduler/config", methods=["POST"])
def set_scheduler_config():
    data = request.get_json() or {}
    _write_setting("sched_moments_slots", data.get("moments_slots", ""))
    _write_setting("desire_enabled", "true" if data.get("desire_enabled", True) else "false")
    _write_setting("desire_quiet_start", data.get("desire_quiet_start", "23:30"))
    _write_setting("desire_quiet_end", data.get("desire_quiet_end", "08:30"))
    register_scheduler_jobs()
    return jsonify({"ok": True})


init_db()
_refresh_appearance_urls()
for _character_id in CHARACTERS:
    _existing_summary = get_summary("default", _character_id)
    if _existing_summary:
        try:
            push_summary_to_ombre(
                _existing_summary,
                _character_id,
                source="conversation_summary",
                source_key="summary:default",
            )
        except Exception as _memory_seed_error:
            app.logger.warning(
                f"memory summary seed failed ({_character_id}): {_memory_seed_error}"
            )
scheduler.start()
register_scheduler_jobs()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
