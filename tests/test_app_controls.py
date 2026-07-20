import io
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from PIL import Image

from memory_backend import MemoryBackend


_TEMP_DIR = tempfile.TemporaryDirectory()
os.environ["DB_PATH"] = os.path.join(_TEMP_DIR.name, "becoming-test.db")
os.environ["APP_PASSWORD"] = "test-password"
os.environ["DESIRE_DRIVEN"] = "false"

import app as app_module


class AppControlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app_module.app.config.update(TESTING=True)
        cls.client = app_module.app.test_client()
        cls.client.post("/api/login", json={"password": "test-password"})
        cls.original_models = {
            cid: char["model"] for cid, char in app_module.CHARACTERS.items()
        }
        cls.original_personas = {
            cid: char["persona"] for cid, char in app_module.CHARACTERS.items()
        }
        cls.original_providers = {
            cid: char["provider"] for cid, char in app_module.CHARACTERS.items()
        }
        cls.original_summary_provider = app_module.SUMMARY_PROVIDER
        cls.original_summary_model = app_module.SUMMARY_MODEL
        cls.original_limits = dict(app_module.LIMITS)

    @classmethod
    def tearDownClass(cls):
        if app_module.scheduler.running:
            app_module.scheduler.shutdown(wait=False)
        _TEMP_DIR.cleanup()

    def tearDown(self):
        for cid, model in self.original_models.items():
            app_module.CHARACTERS[cid]["model"] = model
            app_module.CHARACTERS[cid]["persona"] = self.original_personas[cid]
            app_module.CHARACTERS[cid]["provider"] = self.original_providers[cid]
        app_module.SUMMARY_PROVIDER = self.original_summary_provider
        app_module.SUMMARY_MODEL = self.original_summary_model
        app_module.LIMITS.clear()
        app_module.LIMITS.update(self.original_limits)
        app_module._write_setting(app_module.THEME_SETTING_KEY, app_module.DEFAULT_THEME_ID)
        app_module._write_setting(app_module.WEATHER_EFFECT_SETTING_KEY, "off")
        app_module._write_setting("desire_frequency", app_module.DESIRE_FREQUENCY_DEFAULT)
        conn = app_module.sqlite3.connect(app_module.DB_PATH)
        conn.execute("DELETE FROM settings WHERE key=?", (app_module.VOICE_SETTING_KEY,))
        conn.execute("DELETE FROM voice_assets")
        conn.execute("DELETE FROM voice_usage")
        conn.execute("DELETE FROM settings WHERE key LIKE 'provider_%'")
        conn.execute("DELETE FROM settings WHERE key IN ('summary_provider','summary_model')")
        conn.execute("DELETE FROM settings WHERE key LIKE 'friendship_%'")
        conn.execute("DELETE FROM messages WHERE session_id LIKE 'friendship-test%'")
        conn.commit()
        conn.close()

    def _voice_payload(self, *, enabled=True, daily_count=20):
        return {
            "enabled": enabled,
            "tts": {
                "provider": "openai_compatible",
                "endpoint": "https://voice.example/v1/audio/speech",
                "model": "tts-test",
                "response_format": "mp3",
                "token": "tts-server-secret",
                "voices": {cid: f"voice-{cid}" for cid in app_module.CHARACTERS},
            },
            "stt": {
                "enabled": True,
                "provider": "openai_compatible",
                "endpoint": "https://voice.example/v1/audio/transcriptions",
                "model": "stt-test",
                "token": "stt-server-secret",
                "reuse_tts_credentials": False,
                "max_upload_mb": 5,
            },
            "limits": {
                "max_chars": 120,
                "daily_count": daily_count,
                "cost_per_1k_chars_usd": 0.2,
                "daily_cost_usd": 1,
            },
        }

    def test_group_send_uses_documented_long_press_participant_picker(self):
        static_dir = Path(app_module.__file__).with_name("static")
        script = (static_dir / "app.js").read_text(encoding="utf-8")
        markup = (static_dir / "index.html").read_text(encoding="utf-8")

        self.assertIn('groupSendBtn.addEventListener("pointerdown"', script)
        self.assertIn('openCharPicker("online", null, [...onlineCharacters])', script)
        self.assertIn("长按群聊发送爪", script)
        self.assertNotIn("双击（250ms 内两次 tap）", script)
        self.assertIn('aria-label="发送；长按选择在线角色"', markup)

    def test_single_chat_swipe_back_does_not_capture_vertical_scroll(self):
        script = (
            Path(app_module.__file__).with_name("static") / "app.js"
        ).read_text(encoding="utf-8")

        self.assertIn("const SWIPE_BACK_EDGE_WIDTH = 28;", script)
        self.assertIn("const SWIPE_BACK_LOCK_DISTANCE = 5;", script)
        self.assertIn("const SWIPE_BACK_READY_DISTANCE = 72;", script)
        self.assertIn("if (touch.clientX > SWIPE_BACK_EDGE_WIDTH) return;", script)
        self.assertIn("if (absDy > 12 && absDy > dx * 1.35)", script)
        self.assertIn('singleView.classList.add("swipe-peeking");', script)
        self.assertLess(
            script.index('singleChatView.addEventListener("touchmove"'),
            script.index('singleView.classList.add("swipe-peeking");'),
        )
        self.assertNotIn("SWIPE_BACK_EDGE_RATIO", script)

    def test_memory_overview_keeps_backend_and_character_payload(self):
        script = (
            Path(app_module.__file__).with_name("static") / "app.js"
        ).read_text(encoding="utf-8")

        self.assertIn(
            'writeSecondaryViewCache("memory-overview", characters);\n    return data;',
            script,
        )
        self.assertIn(
            "renderMemoryList(data.characters || [], data.backend || null)",
            script,
        )

    def test_voice_settings_entry_lives_inside_feature_controls(self):
        static_dir = Path(app_module.__file__).with_name("static")
        script = (static_dir / "app.js").read_text(encoding="utf-8")
        markup = (static_dir / "index.html").read_text(encoding="utf-8")

        self.assertNotIn('data-action="voice"', markup)
        self.assertIn('voiceLink.className = "scheduler-feature-link"', script)
        self.assertIn("voiceLink.onclick = openVoicePanel", script)
        self.assertNotIn("TTS、录音转文字、音色与费用限制", script)
        voice_start = script.index('voiceLink.className = "scheduler-feature-link"')
        voice_end = script.index("voiceLink.onclick = openVoicePanel", voice_start)
        self.assertNotIn("chevron_right", script[voice_start:voice_end])
        self.assertNotIn("graphic_eq", script[voice_start:voice_end])
        self.assertIn('voiceLink.textContent = "说说喵°语音收发"', script)
        self.assertIn("panel.appendChild(voiceLink)", script)
        self.assertLess(
            script.index('makeAccordion("眠眠喵°睡眠节律"'),
            script.index('makeAccordion("路由喵°模型供应商"'),
        )

    def test_memory_files_import_json_and_txt_by_character(self):
        class ImportStore:
            def __init__(self):
                self.entries = []
                self.keys = set()

            def recall(self, _owner_id):
                return ""

            def save(self, content, owner_id, **metadata):
                source_key = metadata.get("source_key")
                created = source_key not in self.keys
                if created:
                    self.keys.add(source_key)
                    self.entries.append((owner_id, content, metadata))
                return source_key, created

        store = ImportStore()
        backend = MemoryBackend(store, name="test-import")

        def upload_payload():
            return {
                "fallback_character": "",
                "files": [
                    (io.BytesIO(json.dumps({
                        "char1": [{
                            "content": "Char 1 的旧记忆",
                            "importance": 8,
                            "tags": ["往事"],
                        }],
                        "char3": [{"text": "Char 3 的旧记忆"}],
                    }, ensure_ascii=False).encode("utf-8")), "memories.json"),
                    (io.BytesIO("第一段\n\n第二段".encode("utf-8")), "char2-notes.txt"),
                ],
            }

        with patch.object(app_module, "MEMORY_SERVICE", backend):
            first = self.client.post(
                "/api/memory/import-files",
                data=upload_payload(),
                content_type="multipart/form-data",
            )
            second = self.client.post(
                "/api/memory/import-files",
                data=upload_payload(),
                content_type="multipart/form-data",
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.get_json()["imported"], 4)
        self.assertEqual(first.get_json()["by_character"], {
            "char1": 1, "char2": 2, "char3": 1,
        })
        self.assertEqual(second.get_json()["imported"], 0)
        self.assertEqual(second.get_json()["skipped"], 4)
        self.assertEqual({entry[0] for entry in store.entries}, {"char1", "char2", "char3"})
        char1_metadata = next(entry[2] for entry in store.entries if entry[0] == "char1")
        self.assertEqual(char1_metadata["importance"], 8)
        self.assertEqual(char1_metadata["tags"], ["往事"])
        self.assertEqual(char1_metadata["source"], "file_import")

    def test_memory_file_import_uses_selected_fallback_without_guessing(self):
        saved = []

        class ImportStore:
            def recall(self, _owner_id):
                return ""

            def save(self, content, owner_id, **metadata):
                saved.append((owner_id, content, metadata))
                return "fallback-memory", True

        with patch.object(
            app_module, "MEMORY_SERVICE", MemoryBackend(ImportStore(), name="test-import")
        ):
            response = self.client.post(
                "/api/memory/import-files",
                data={
                    "fallback_character": "char4",
                    "files": (io.BytesIO(b'{"memories":["ownerless memory"]}'), "notes.json"),
                },
                content_type="multipart/form-data",
            )
            unknown_owner = self.client.post(
                "/api/memory/import-files",
                data={
                    "fallback_character": "char4",
                    "files": (
                        io.BytesIO(b'{"memories":[{"role":"assistant","content":"do not guess"}]}'),
                        "unknown.json",
                    ),
                },
                content_type="multipart/form-data",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(saved[0][0], "char4")
        self.assertEqual(saved[0][1], "ownerless memory")
        self.assertEqual(unknown_owner.status_code, 400)
        self.assertEqual(unknown_owner.get_json()["unassigned"], 1)
        self.assertEqual(len(saved), 1)

    def test_desire_frequency_is_editable_and_persistent(self):
        expected = {
            "low": (4 * 3600, 90 * 60, 3),
            "medium": (int(2.5 * 3600), 60 * 60, 5),
            "high": (int(1.5 * 3600), 30 * 60, 8),
        }
        for frequency, values in expected.items():
            response = self.client.post("/api/scheduler/config", json={
                "desire_enabled": True,
                "desire_frequency": frequency,
                "desire_quiet_start": "23:30",
                "desire_quiet_end": "08:30",
            })
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                self.client.get("/api/scheduler/config").get_json()["desire_frequency"],
                frequency,
            )
            selected, config = app_module._desire_frequency_config()
            self.assertEqual(selected, frequency)
            self.assertEqual(
                (
                    config["min_interval_seconds"],
                    config["user_cooldown_seconds"],
                    config["daily_limit"],
                ),
                values,
            )

        invalid = self.client.post("/api/scheduler/config", json={
            "desire_frequency": "不停说话",
        })
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(
            self.client.get("/api/scheduler/config").get_json()["desire_frequency"],
            "high",
        )

    def test_voice_config_tokens_are_server_only(self):
        response = self.client.post("/api/voice/config", json=self._voice_payload())
        self.assertEqual(response.status_code, 200)
        rendered = response.get_data(as_text=True)
        self.assertNotIn("tts-server-secret", rendered)
        self.assertNotIn("stt-server-secret", rendered)
        self.assertTrue(response.get_json()["config"]["tts"]["token_configured"])

        public = self.client.get("/api/voice/config").get_data(as_text=True)
        generic = self.client.get("/api/settings").get_data(as_text=True)
        self.assertNotIn("tts-server-secret", public)
        self.assertNotIn("stt-server-secret", public)
        self.assertNotIn("tts-server-secret", generic)
        self.assertNotIn(app_module.VOICE_SETTING_KEY, generic)

    def test_voice_preview_enforces_daily_count(self):
        self.client.post("/api/voice/config", json=self._voice_payload(daily_count=1))
        audio = Mock(content=b"ID3-test-audio", mime_type="audio/mpeg")
        with patch.object(app_module, "synthesize_speech", return_value=audio) as synthesize:
            first = self.client.post("/api/voice/preview", json={
                "character_id": "char1", "text": "试听一句",
            })
            second = self.client.post("/api/voice/preview", json={
                "character_id": "char1", "text": "再试听一句",
            })
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.mimetype, "audio/mpeg")
        self.assertEqual(second.status_code, 400)
        self.assertIn("次数", second.get_json()["error"])
        self.assertEqual(synthesize.call_count, 1)

    def test_send_voice_tool_saves_audio_and_history_transcript(self):
        self.client.post("/api/voice/config", json=self._voice_payload())
        state = app_module._new_tool_chain_state()
        result = app_module._execute_chat_tool(
            "send_voice", {"text": "我用声音说一句。"}, "char1", state
        )
        self.assertIn("排队", result)
        self.assertEqual(state["tools_called"][0]["name"], "send_voice")

        audio = Mock(content=b"voice-bytes", mime_type="audio/mpeg")
        with patch.object(app_module, "synthesize_speech", return_value=audio), patch.object(
            app_module, "maybe_compress"
        ):
            response = app_module._finalize_character_reply(
                app_module.CHARACTERS["char1"],
                "voice-test",
                "先给你一条文字。",
                None,
                None,
                state["tools_called"],
            )
        voice = response["voice"]
        self.assertIsNotNone(voice)
        self.assertEqual(voice["text"], "我用声音说一句。")
        audio_response = self.client.get(voice["url"])
        self.assertEqual(audio_response.status_code, 200)
        self.assertEqual(audio_response.data, b"voice-bytes")
        history = app_module.load_active_messages("voice-test", "char1")
        self.assertTrue(any("我用声音说一句" in item["content"] for item in history))

    def test_stt_upload_uses_server_token_and_returns_only_text(self):
        self.client.post("/api/voice/config", json=self._voice_payload())
        with patch.object(
            app_module, "transcribe_speech", return_value="这是录音转出的文字"
        ) as transcribe:
            response = self.client.post(
                "/api/voice/transcribe",
                data={"audio": (io.BytesIO(b"m4a-bytes"), "iphone.m4a")},
                content_type="multipart/form-data",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["text"], "这是录音转出的文字")
        self.assertEqual(transcribe.call_args.kwargs["token"], "stt-server-secret")
        self.assertNotIn("server-secret", response.get_data(as_text=True))

    def test_model_and_limits_are_editable(self):
        response = self.client.post("/api/character-config/char2", json={
            "persona": "测试人设",
            "model": "openai/gpt-5-mini",
        })
        self.assertEqual(response.status_code, 200)
        config = self.client.get("/api/character-config").get_json()
        self.assertEqual(config["char2"]["model"], "openai/gpt-5-mini")

        response = self.client.post("/api/limits", json={
            "limits": {"char2": 42.5, "char5": 18},
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["limits"]["char2"], 42.5)

    def test_character_provider_and_summary_provider_are_editable(self):
        response = self.client.post("/api/character-config/char2", json={
            "persona": "测试人设",
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "verify_connection": False,
        })
        self.assertEqual(response.status_code, 200)
        config = self.client.get("/api/character-config").get_json()
        self.assertEqual(config["char2"]["provider"], "deepseek")
        self.assertEqual(config["char2"]["model"], "deepseek-v4-flash")

        with patch.object(
            app_module, "_test_provider_connection", return_value=(True, "连接成功")
        ) as test_connection:
            summary = self.client.post("/api/model-providers/summary", json={
                "provider": "deepseek",
                "model": "deepseek-v4-flash",
                "verify_connection": True,
            })
        self.assertEqual(summary.status_code, 200)
        self.assertEqual(app_module.SUMMARY_PROVIDER, "deepseek")
        test_connection.assert_called_once_with("deepseek", "deepseek-v4-flash")

    def test_model_provider_status_and_frontend_never_expose_keys(self):
        with patch.object(
            app_module, "OPENROUTER_API_KEY", "server-only-openrouter"
        ), patch.object(
            app_module, "ANTHROPIC_API_KEY", "server-only-anthropic"
        ), patch.object(
            app_module, "DEEPSEEK_API_KEY", "server-only-deepseek"
        ), patch.object(
            app_module, "CUSTOM_OPENAI_API_KEY", "server-only-custom"
        ), patch.object(
            app_module, "CUSTOM_OPENAI_BASE_URL", "https://custom.example/v1"
        ):
            response = self.client.get("/api/model-providers")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["providers"]["deepseek"]["configured"])
        rendered = response.get_data(as_text=True)
        for secret in (
            "server-only-openrouter", "server-only-anthropic",
            "server-only-deepseek", "server-only-custom",
        ):
            self.assertNotIn(secret, rendered)
        self.assertNotIn("api_key", rendered.lower())

        script = (
            Path(app_module.__file__).with_name("static") / "app.js"
        ).read_text(encoding="utf-8")
        self.assertIn('fetch("/api/model-providers")', script)
        self.assertIn("makeProviderPicker", script)
        self.assertIn('trigger.setAttribute("aria-haspopup", "listbox")', script)
        self.assertIn('option.setAttribute("role", "option")', script)
        self.assertIn('if (event.key === "Escape")', script)
        self.assertNotIn('summaryProvider = document.createElement("select")', script)
        self.assertNotIn('providerSelect = document.createElement("select")', script)
        self.assertIn("verify_connection: connectionChanged", script)
        self.assertNotIn("DEEPSEEK_API_KEY", script)

    def test_usage_cards_only_render_configured_providers(self):
        with patch.object(
            app_module, "OPENROUTER_API_KEY", ""
        ), patch.object(
            app_module, "ANTHROPIC_API_KEY", ""
        ), patch.object(
            app_module, "DEEPSEEK_API_KEY", "deepseek-only"
        ), patch.object(
            app_module, "CUSTOM_OPENAI_API_KEY", ""
        ):
            payload = self.client.get("/api/usage").get_json()

        configured = [
            key for key, provider in payload["providers"].items()
            if provider["configured"]
        ]
        self.assertEqual(configured, ["deepseek"])
        self.assertEqual(payload["cny_per_usd"], app_module.CNY_PER_USD)

        script = (
            Path(app_module.__file__).with_name("static") / "app.js"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "Object.keys(providers)\n      .filter(key => providers[key]?.configured)",
            script,
        )
        self.assertIn("if (providerKeys.length) panel.appendChild(platformCards);", script)
        self.assertIn(
            'deepseek: { symbol: "¥", rate_key: "cny_per_usd", fallback_rate: 6.78 }',
            script,
        )
        self.assertIn("const displaySpent = spent * displayRate;", script)
        self.assertIn("const displayLimit = lim * displayRate;", script)

    def test_deepseek_connectivity_uses_server_key_and_compatible_endpoint(self):
        provider_response = Mock(status_code=200)
        provider_response.json.return_value = {"choices": [{"message": {"content": "OK"}}]}
        with patch.object(app_module, "DEEPSEEK_API_KEY", "deepseek-server-secret"), patch.object(
            app_module, "DEEPSEEK_BASE_URL", "https://api.deepseek.example/v1"
        ), patch.object(app_module.requests, "post", return_value=provider_response) as post:
            response = self.client.post("/api/model-providers/test", json={
                "provider": "deepseek",
                "model": "deepseek-v4-flash",
            })

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        self.assertNotIn("deepseek-server-secret", response.get_data(as_text=True))
        self.assertEqual(post.call_args.args[0], "https://api.deepseek.example/v1/chat/completions")
        self.assertEqual(
            post.call_args.kwargs["headers"]["Authorization"],
            "Bearer deepseek-server-secret",
        )

    def test_deepseek_usage_accounts_for_cache_pricing(self):
        metrics = app_module.log_usage("char2", "deepseek", "deepseek-v4-flash", {
            "prompt_tokens": 1000,
            "completion_tokens": 500,
            "prompt_cache_hit_tokens": 400,
            "prompt_cache_miss_tokens": 600,
        }, purpose="provider-test")
        expected = (400 * 0.0028 + 600 * 0.14 + 500 * 0.28) / 1_000_000
        self.assertAlmostEqual(metrics["cost_usd"], expected)
        self.assertEqual(metrics["cache_read_tokens"], 400)
        self.assertTrue(metrics["cache_reported"])

    def test_deepseek_tool_round_preserves_reasoning_content(self):
        first = Mock(status_code=200)
        first.json.return_value = {
            "choices": [{"message": {
                "content": "",
                "reasoning_content": "先判断是否需要关窗。",
                "tool_calls": [{
                    "id": "tool-1",
                    "type": "function",
                    "function": {
                        "name": "close_window",
                        "arguments": json.dumps({"reason": "有点冷"}, ensure_ascii=False),
                    },
                }],
            }}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        second = Mock(status_code=200)
        second.json.return_value = {
            "choices": [{"message": {"content": "窗户已经关好啦。"}}],
            "usage": {"prompt_tokens": 15, "completion_tokens": 6},
        }
        with patch.object(app_module, "DEEPSEEK_API_KEY", "deepseek-secret"), patch.object(
            app_module.requests, "post", side_effect=[first, second]
        ) as post:
            result = app_module.call_or_with_tools(
                "deepseek-v4-flash",
                [{"role": "user", "content": "有点冷。"}],
                character_id="char1",
                provider="deepseek",
            )

        self.assertEqual(result[0], "窗户已经关好啦。")
        second_payload = post.call_args_list[1].kwargs["json"]
        assistant_round = second_payload["messages"][1]
        self.assertEqual(assistant_round["reasoning_content"], "先判断是否需要关窗。")
        self.assertEqual(assistant_round["tool_calls"][0]["id"], "tool-1")

    def test_provider_api_keys_never_reach_character_config(self):
        with patch.object(
            app_module, "OPENROUTER_API_KEY", "server-only-openrouter"
        ), patch.object(
            app_module, "ANTHROPIC_API_KEY", "server-only-anthropic"
        ):
            response = self.client.get("/api/character-config")

        rendered = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("server-only-openrouter", rendered)
        self.assertNotIn("server-only-anthropic", rendered)
        self.assertNotIn("api_key", rendered.lower())

    def test_login_stays_closed_without_a_configured_password(self):
        unauthenticated = app_module.app.test_client()
        with patch.object(app_module, "APP_PASSWORD", ""):
            response = unauthenticated.post("/api/login", json={"password": ""})
        self.assertEqual(response.status_code, 503)

    def test_public_sleep_nudge_stays_closed_without_a_configured_password(self):
        unauthenticated = app_module.app.test_client()
        with patch.object(app_module, "SLEEP_NUDGE_ENABLED", True), patch.object(
            app_module, "APP_PASSWORD", ""
        ):
            response = unauthenticated.post(
                "/api/sleep/nudge",
                json={"password": "", "character_id": "char1"},
            )
        self.assertEqual(response.status_code, 401)

    def test_public_sleep_nudge_is_disabled_by_default(self):
        unauthenticated = app_module.app.test_client()
        with patch.object(app_module, "SLEEP_NUDGE_ENABLED", False):
            response = unauthenticated.post(
                "/api/sleep/nudge",
                json={"password": "test-password", "character_id": "char1"},
            )
        self.assertEqual(response.status_code, 404)

    def test_sleep_nudge_cors_does_not_open_other_apis(self):
        unauthenticated = app_module.app.test_client()
        origin = "https://controller.example"
        with patch.object(app_module, "SLEEP_NUDGE_ENABLED", True), patch.object(
            app_module, "CORS_ALLOW_ORIGINS", {origin}
        ):
            nudge = unauthenticated.open(
                "/api/sleep/nudge",
                method="OPTIONS",
                headers={"Origin": origin},
            )
            chat = unauthenticated.open(
                "/api/chat",
                method="OPTIONS",
                headers={"Origin": origin},
            )

        self.assertIn(nudge.status_code, (200, 204))
        self.assertEqual(nudge.headers.get("Access-Control-Allow-Origin"), origin)
        self.assertEqual(chat.status_code, 401)
        self.assertNotIn("Access-Control-Allow-Origin", chat.headers)

    def test_public_character_ids_are_neutral_placeholders(self):
        self.assertEqual(list(app_module.CHARACTERS), [
            "char1", "char2", "char3", "char4", "char5", "char6",
        ])
        self.assertEqual(app_module.USER_ID, "user")

    def test_mobile_extension_manifest_is_secret_free(self):
        fake_push = Mock(enabled=True)
        with patch.object(app_module, "MOBILE_PUSH", fake_push):
            response = self.client.get("/api/mobile/extensions")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["push"]["configured"])
        self.assertEqual(payload["music"]["extension_point"], "custom_mcp")
        self.assertTrue(payload["music"]["web_room_built_in"])
        self.assertTrue(payload["phone"]["read_only"])
        self.assertIn("mobile_companion", payload["voice"]["extension_points"])
        self.assertTrue(payload["voice"]["built_in"])
        self.assertFalse(payload["voice"]["default_enabled"])
        self.assertTrue(payload["voice"]["stores_audio"])
        self.assertEqual(payload["voice"]["credential_storage"], "server_only")
        self.assertNotIn("secret", response.get_data(as_text=True).lower())

    def test_memory_overview_reports_backend_capabilities(self):
        response = self.client.get("/api/memory")
        self.assertEqual(response.status_code, 200)
        backend = response.get_json()["backend"]
        self.assertEqual(backend["name"], "embedded")
        self.assertIn("legacy_import", backend["capabilities"])

    def test_minimal_external_memory_disables_only_admin_api(self):
        class MinimalMemory:
            def recall(self, _owner_id):
                return ""

            def save(self, _content, _owner_id, **_metadata):
                return "external-id"

        with patch.object(
            app_module, "MEMORY_SERVICE", MemoryBackend(MinimalMemory(), name="test-external")
        ):
            overview = self.client.get("/api/memory")
            detail = self.client.get("/api/memory/char1")
            migration = self.client.post("/api/memory/import-legacy", json={})

        self.assertEqual(overview.status_code, 200)
        self.assertIsNone(overview.get_json()["characters"][0]["count"])
        self.assertEqual(detail.status_code, 501)
        self.assertEqual(migration.status_code, 501)

    def test_chat_upload_route_uses_configurable_storage(self):
        with tempfile.TemporaryDirectory() as upload_dir:
            filename = "chat-test.png"
            with open(os.path.join(upload_dir, filename), "wb") as image_file:
                image_file.write(b"test-image-bytes")
            with patch.object(app_module, "UPLOAD_ROOT", upload_dir):
                response = self.client.get(f"/api/uploads/{filename}")
                payload = response.get_data()
                response.close()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload, b"test-image-bytes")

    def test_mobile_push_requires_an_explicit_proactive_source(self):
        fake_push = Mock(enabled=True)
        fake_push.send_message.return_value = True
        with patch.object(app_module, "MOBILE_PUSH", fake_push), patch.object(
            app_module, "maybe_compress"
        ):
            app_module._finalize_character_reply(
                app_module.CHARACTERS["char1"],
                "default",
                "普通聊天回复",
                None,
                None,
                [],
            )
            fake_push.send_message.assert_not_called()
            result = app_module._finalize_character_reply(
                app_module.CHARACTERS["char1"],
                "default",
                "主动来找你啦",
                None,
                None,
                [],
                push_source="desire",
            )

        fake_push.send_message.assert_called_once_with(
            character_id="char1",
            character_name="Char 1",
            text="主动来找你啦",
            message_id=result["reply_id"],
            source="desire",
        )

    def test_appearance_assets_upload_serve_and_reset(self):
        png = b"\x89PNG\r\n\x1a\nbecoming-appearance-test"
        try:
            response = self.client.post(
                "/api/appearance/assets/avatar_char3",
                data={"image": (io.BytesIO(png), "new-avatar.png")},
                content_type="multipart/form-data",
            )
            self.assertEqual(response.status_code, 200)
            avatar = response.get_json()["appearance"]["avatars"]["char3"]
            self.assertTrue(avatar["custom"])
            self.assertIn("/api/appearance/assets/avatar_char3", avatar["url"])

            characters = self.client.get("/api/characters").get_json()
            self.assertEqual(characters["char3"]["avatar"], avatar["url"])
            served = self.client.get(avatar["url"])
            self.assertEqual(served.status_code, 200)
            self.assertEqual(served.data, png)
            self.assertEqual(served.mimetype, "image/png")

            response = self.client.post(
                "/api/appearance/assets/background_chat",
                data={"image": (io.BytesIO(png), "new-background.png")},
                content_type="multipart/form-data",
            )
            self.assertEqual(response.status_code, 200)
            background = response.get_json()["appearance"]["chat_background"]
            self.assertTrue(background["custom"])
            self.assertIn("/api/appearance/assets/background_chat", background["url"])
        finally:
            self.client.delete("/api/appearance/assets/avatar_char3")
            self.client.delete("/api/appearance/assets/background_chat")

        appearance = self.client.get("/api/appearance").get_json()
        self.assertFalse(appearance["avatars"]["char3"]["custom"])
        self.assertEqual(
            appearance["avatars"]["char3"]["url"],
            "/static/char3.svg",
        )
        self.assertFalse(appearance["chat_background"]["custom"])

    def test_appearance_theme_persists_and_changes_defaults(self):
        response = self.client.post("/api/appearance", json={"theme": "dreamscape"})
        self.assertEqual(response.status_code, 200)
        appearance = response.get_json()
        self.assertEqual(appearance["theme"], "dreamscape")
        selected = next(item for item in appearance["themes"] if item["id"] == "dreamscape")
        self.assertEqual(selected["name"], "抹茶")
        self.assertEqual(selected["colors"]["user_bubble"], "#E7CDB4")
        self.assertEqual(selected["colors"]["cream"], "#F8F4E7")
        self.assertEqual(selected["colors"]["ai_bubble"], "#C6D8CF")
        self.assertEqual(selected["colors"]["dusky"], "#75805F")
        self.assertEqual(selected["colors"]["chrome"], "#75805F")
        self.assertEqual(appearance["chat_background"]["default_url"], "/static/theme_matcha.jpg")
        self.assertEqual(appearance["chat_background"]["url"], "/static/theme_matcha.jpg")
        self.assertEqual(
            [theme["name"] for theme in appearance["themes"]],
            ["恋人", "抹茶", "雾港", "丁香"],
        )
        self.assertEqual(self.client.get("/api/appearance").get_json()["theme"], "dreamscape")

        invalid = self.client.post("/api/appearance", json={"theme": "unknown"})
        self.assertEqual(invalid.status_code, 400)

    def test_appearance_weather_effect_persists(self):
        for effect in ("rain", "snow", "leaves", "off"):
            response = self.client.post("/api/appearance", json={"weather_effect": effect})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["weather_effect"], effect)
            self.assertEqual(self.client.get("/api/appearance").get_json()["weather_effect"], effect)

        invalid = self.client.post("/api/appearance", json={"weather_effect": "storm"})
        self.assertEqual(invalid.status_code, 400)

    def test_music_room_persists_members_track_and_distance(self):
        response = self.client.put("/api/music/room/participants", json={
            "character_ids": ["char1", "char6"],
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item["id"] for item in response.get_json()["participants"]],
            ["char1", "char6"],
        )
        response = self.client.put("/api/music/room", json={
            "song_id": "local:test-song-1",
            "song_name": "测试曲",
            "artist_name": "测试歌手",
            "album_name": "测试专辑",
            "artwork_url": "https://example.com/cover.jpg",
            "duration_ms": 180000,
            "position_ms": 12000,
            "playback_state": "playing",
            "distance_km": 952.7,
        })
        self.assertEqual(response.status_code, 200)
        room = response.get_json()["room"]
        self.assertEqual(room["song_id"], "local:test-song-1")
        self.assertEqual(room["position_ms"], 12000)
        self.assertEqual(room["distance_km"], 952.7)
        self.assertIsNotNone(room["started_at"])

    def test_music_room_allows_listening_alone(self):
        response = self.client.put("/api/music/room/participants", json={
            "character_ids": [],
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["participants"], [])

        response = self.client.post("/api/music/room/messages", json={"content": "自己听一会儿。"})
        self.assertEqual(response.status_code, 200)
        room = response.get_json()["room"]
        self.assertEqual(room["participants"], [])
        self.assertEqual(room["messages"][-1]["author_id"], "user")
        self.assertEqual(room["messages"][-1]["content"], "自己听一会儿。")

    def test_music_room_clears_previous_conversation_when_participants_change(self):
        self.client.put("/api/music/room/participants", json={
            "character_ids": ["char1"],
        })
        with patch.object(
            app_module,
            "ask_music_companion",
            return_value=("我在。", None, {}),
        ):
            self.client.post("/api/music/room/messages", json={"content": "一起听。"})

        changed = self.client.put("/api/music/room/participants", json={
            "character_ids": [],
        })
        self.assertEqual(changed.status_code, 200)
        room = changed.get_json()["room"]
        self.assertEqual(room["participants"], [])
        self.assertEqual(room["messages"], [])
        self.assertEqual(room["pending_commands"], [])

    def test_music_library_upload_stream_and_delete(self):
        track_id = "local:test-synced-song"
        response = self.client.post(
            "/api/music/library",
            data={
                "track_id": track_id,
                "name": "同步测试曲",
                "artist": "测试歌手",
                "album": "测试专辑",
                "duration": "61.5",
                "lyrics": "[00:10.00]第一句\n[00:20.00]第二句",
                "audio": (io.BytesIO(b"ID3" + b"a" * 64), "sync-test.mp3", "audio/mpeg"),
                "artwork": (io.BytesIO(b"fake-cover"), "cover.jpg", "image/jpeg"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 201)
        track = response.get_json()["track"]
        self.assertEqual(track["id"], track_id)
        self.assertEqual(track["name"], "同步测试曲")
        self.assertTrue(track["audio_url"])
        self.assertTrue(track["artwork_url"])
        self.assertTrue(track["has_lyrics"])
        conn = app_module.sqlite3.connect(app_module.DB_PATH)
        stored = app_module._music_library_row(conn, track_id)
        conn.close()
        self.assertIn("第二句", stored[10])

        tracks = self.client.get("/api/music/library").get_json()["tracks"]
        self.assertIn(track_id, [item["id"] for item in tracks])
        streamed = self.client.get(track["audio_url"], headers={"Range": "bytes=0-2"})
        self.assertEqual(streamed.status_code, 206)
        self.assertEqual(streamed.data, b"ID3")
        streamed.close()
        artwork = self.client.get(track["artwork_url"])
        self.assertEqual(artwork.data, b"fake-cover")
        artwork.close()

        self.assertEqual(self.client.delete(f"/api/music/library/{track_id}").status_code, 200)
        self.assertEqual(self.client.get(track["audio_url"]).status_code, 404)

    def test_music_lyrics_context_uses_current_lrc_window(self):
        lyrics = "\n".join([
            "[00:01.00]开头",
            "[00:10.00]第一句",
            "[00:20.00]第二句",
            "[00:30.00]第三句",
            "[00:40.00]第四句",
            "[01:30.00]很远以后",
        ])
        context = app_module._music_lyrics_context(lyrics, 21)
        self.assertIn("当前进度附近歌词", context)
        self.assertIn("[0:20] 第二句", context)
        self.assertIn("[0:30] 第三句", context)
        self.assertNotIn("很远以后", context)

    def test_music_lyrics_context_refuses_to_invent_missing_lyrics(self):
        context = app_module._music_lyrics_context("", 90)
        self.assertIn("未提供", context)
        self.assertIn("没有音频输入", context)

    def test_music_library_is_uncached_and_existing_track_can_be_repaired(self):
        track_id = "local:test-repair-song"
        first = self.client.post(
            "/api/music/library",
            data={
                "track_id": track_id,
                "name": "旧名字",
                "artist": "旧歌手",
                "audio": (io.BytesIO(b"ID3-old-audio"), "repair.mp3", "audio/mpeg"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(first.status_code, 201)
        listing = self.client.get("/api/music/library")
        self.assertIn("no-store", listing.headers.get("Cache-Control", ""))

        updated = self.client.post(
            "/api/music/library",
            data={
                "track_id": track_id,
                "name": "新名字",
                "artist": "新歌手",
                "audio": (io.BytesIO(b"ID3-unused"), "repair.mp3", "audio/mpeg"),
                "artwork": (io.BytesIO(b"new-cover"), "cover.jpg", "image/jpeg"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(updated.status_code, 200)
        self.assertTrue(updated.get_json()["existing"])
        track = updated.get_json()["track"]
        self.assertEqual(track["name"], "新名字")
        self.assertTrue(track["artwork_url"])
        artwork = self.client.get(track["artwork_url"])
        self.assertEqual(artwork.data, b"new-cover")
        artwork.close()

        conn = app_module.sqlite3.connect(app_module.DB_PATH)
        row = app_module._music_library_row(conn, track_id)
        conn.close()
        os.remove(app_module._music_storage_path(row[7]))
        repaired = self.client.post(
            "/api/music/library",
            data={
                "track_id": track_id,
                "name": "修复完成",
                "artist": "新歌手",
                "audio": (io.BytesIO(b"ID3-repaired-audio"), "repair.mp3", "audio/mpeg"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(repaired.status_code, 201)
        repaired_track = repaired.get_json()["track"]
        streamed = self.client.get(repaired_track["audio_url"])
        self.assertEqual(streamed.data, b"ID3-repaired-audio")
        streamed.close()
        self.client.delete(f"/api/music/library/{track_id}")

    def test_music_library_normalizes_mislabeled_tiff_artwork(self):
        track_id = "local:test-tiff-cover"
        tiff = io.BytesIO()
        Image.new("RGB", (8, 6), (117, 128, 95)).save(tiff, format="TIFF")
        tiff.seek(0)
        response = self.client.post(
            "/api/music/library",
            data={
                "track_id": track_id,
                "name": "伪装封面测试曲",
                "audio": (io.BytesIO(b"ID3-tiff-cover"), "tiff-cover.mp3", "audio/mpeg"),
                "artwork": (tiff, "cover.jpg", "image/jpeg"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 201)
        track = response.get_json()["track"]
        artwork = self.client.get(track["artwork_url"])
        self.assertEqual(artwork.mimetype, "image/png")
        self.assertTrue(artwork.data.startswith(b"\x89PNG\r\n\x1a\n"))
        artwork.close()
        self.client.delete(f"/api/music/library/{track_id}")

    def test_music_companion_control_is_queued_and_acknowledged(self):
        self.client.put("/api/music/room/participants", json={
            "character_ids": ["char1"],
        })
        with patch.object(
            app_module,
            "ask_music_companion",
            return_value=(
                "这首停一下。",
                "pause",
                {
                    "tool": "music_player_control",
                    "input": {"action": "pause"},
                    "output": {"status": "queued", "message": "已交给房间播放器"},
                },
            ),
        ):
            response = self.client.post("/api/music/room/messages", json={"content": "等一下"})
        self.assertEqual(response.status_code, 200)
        room = response.get_json()["room"]
        self.assertEqual(len(room["pending_commands"]), 1)
        command = room["pending_commands"][0]
        companion = room["messages"][-1]
        self.assertEqual(companion["details"]["tool"], "music_player_control")
        self.assertEqual(companion["details"]["output"]["command_id"], command["id"])

        response = self.client.patch(
            f"/api/music/room/commands/{command['id']}",
            json={"status": "applied", "output": "播放器已暂停"},
        )
        self.assertEqual(response.status_code, 200)
        refreshed = self.client.get("/api/music/room").get_json()["room"]
        updated = next(item for item in refreshed["messages"] if item["id"] == companion["id"])
        self.assertEqual(updated["details"]["output"]["status"], "applied")
        self.assertEqual(updated["details"]["output"]["message"], "播放器已暂停")

    def test_music_companion_reply_is_limited_to_ninety_characters(self):
        self.client.put("/api/music/room/participants", json={
            "character_ids": ["char1"],
        })
        long_reply = "甲" * 52 + "。" + "乙" * 100
        with patch.object(
            app_module,
            "ask_music_companion",
            return_value=(long_reply, None, {}),
        ):
            response = self.client.post("/api/music/room/messages", json={"content": "说短一点。"})
        self.assertEqual(response.status_code, 200)
        reply = response.get_json()["room"]["messages"][-1]["content"]
        self.assertLessEqual(len(reply), 90)
        self.assertTrue(reply.endswith("。"))

    def test_music_room_uses_netease_as_its_only_visible_source(self):
        response = self.client.get("/api/music/room")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["configured"])
        self.assertEqual(response.get_json()["source"], "netease")

    def test_netease_status_validates_the_configured_account(self):
        profile = {
            "user_id": "13579", "nickname": "User的云村",
            "avatar_url": "https://example.com/avatar.jpg",
        }
        with patch.object(app_module, "NETEASE_MUSIC_U", "secret-cookie"), patch.object(
            app_module, "_netease_account_profile", return_value=profile
        ):
            response = self.client.get("/api/music/netease/status")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["account_valid"])
        self.assertEqual(payload["profile"]["nickname"], "User的云村")
        self.assertNotIn("secret-cookie", response.get_data(as_text=True))

    def test_netease_personal_playlists_and_tracks_are_returned(self):
        profile = {"user_id": "13579", "nickname": "User", "avatar_url": ""}
        playlists = [{
            "id": "24680", "name": "想一起听", "cover_url": "",
            "track_count": 2, "creator_name": "User", "subscribed": False,
        }]
        playlist = {
            "id": "24680", "name": "想一起听", "cover_url": "",
            "songs": [{
                "id": "netease:97531", "source": "netease", "source_id": "97531",
                "name": "恋爱告急", "artist": "鞠婧祎", "album": "专辑",
                "duration": 253, "artwork_url": "", "audio_url": "/api/music/netease/audio/97531",
            }],
        }
        with patch.object(app_module, "_netease_account_profile", return_value=profile), patch.object(
            app_module, "_netease_user_playlists", return_value=playlists
        ), patch.object(app_module, "_netease_playlist_songs", return_value=playlist):
            response = self.client.get("/api/music/netease/playlists")
            tracks_response = self.client.get("/api/music/netease/playlists/24680")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["playlists"][0]["name"], "想一起听")
        self.assertEqual(tracks_response.status_code, 200)
        self.assertEqual(tracks_response.get_json()["playlist"]["songs"][0]["source_id"], "97531")

    def test_netease_audio_is_proxied_with_range_support(self):
        upstream = unittest.mock.Mock()
        upstream.status_code = 206
        upstream.headers = {
            "Content-Type": "audio/mpeg",
            "Content-Length": "4",
            "Content-Range": "bytes 0-3/100",
            "Accept-Ranges": "bytes",
        }
        upstream.iter_content.return_value = [b"test"]
        with patch.object(
            app_module, "_netease_resolve_audio_url",
            return_value="https://music.126.net/test.mp3",
        ), patch.object(app_module.requests, "get", return_value=upstream) as get_audio:
            response = self.client.get(
                "/api/music/netease/audio/97531",
                headers={"Range": "bytes=0-3"},
            )
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.data, b"test")
        self.assertEqual(response.headers["Content-Range"], "bytes 0-3/100")
        self.assertEqual(get_audio.call_args.kwargs["headers"]["Range"], "bytes=0-3")
        self.assertNotIn("Cookie", get_audio.call_args.kwargs["headers"])
        upstream.close.assert_called_once()

    def test_netease_audio_status_checks_a_small_range(self):
        upstream = unittest.mock.Mock()
        upstream.status_code = 206
        upstream.headers = {"Content-Type": "audio/mpeg"}
        with patch.object(
            app_module, "_netease_resolve_audio_url",
            return_value="https://music.126.net/test.mp3",
        ), patch.object(app_module.requests, "get", return_value=upstream) as get_audio:
            response = self.client.get("/api/music/netease/audio/97531/status")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["playable"])
        self.assertEqual(get_audio.call_args.kwargs["headers"]["Range"], "bytes=0-1")
        upstream.close.assert_called_once()

    def test_netease_audio_status_preserves_the_provider_error(self):
        with patch.object(
            app_module, "_netease_resolve_audio_url",
            side_effect=app_module.NeteaseMusicError("账号没有拿到这首歌的播放权限"),
        ):
            response = self.client.get("/api/music/netease/audio/97531/status")
        self.assertEqual(response.status_code, 502)
        self.assertFalse(response.get_json()["playable"])
        self.assertIn("播放权限", response.get_json()["error"])

    def test_netease_search_returns_online_tracks_without_exposing_cookie(self):
        song = {
            "id": "netease:12345", "source": "netease", "source_id": "12345",
            "name": "在线测试曲", "artist": "测试歌手", "album": "测试专辑",
            "duration": 180, "artwork_url": "https://example.com/cover.jpg",
            "audio_url": "/api/music/netease/audio/12345", "has_lyrics": False,
            "synced": False,
        }
        with patch.object(app_module, "_netease_search_songs", return_value=[song]):
            response = self.client.get("/api/music/netease/search?q=在线测试")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["songs"][0]["source_id"], "12345")
        self.assertNotIn("MUSIC_U", response.get_data(as_text=True))

    def test_music_tools_search_then_choose_only_a_returned_song(self):
        candidate = {
            "id": "netease:24680", "source": "netease", "source_id": "24680",
            "name": "祂挑的歌", "artist": "歌手", "album": "专辑", "duration": 200,
            "artwork_url": "", "audio_url": "/api/music/netease/audio/24680",
        }
        prepared = {**candidate, "has_lyrics": True, "synced": False}
        state = {"action": None, "action_input": {}, "search_results": {}, "traces": []}
        with patch.object(app_module, "_netease_search_songs", return_value=[candidate]), patch.object(
            app_module, "_prepare_netease_track", return_value=prepared
        ):
            search_output = app_module._execute_music_tool("music_search", {"query": "祂挑的歌"}, state)
            rejected = app_module._execute_music_tool("music_play_track", {"source_id": "99999"}, state)
            selected = app_module._execute_music_tool("music_play_track", {"source_id": "24680"}, state)
        self.assertIn("24680", search_output)
        self.assertIn("不在本轮搜索结果", rejected)
        self.assertIn("祂挑的歌", selected)
        self.assertEqual(state["action"], "play_online")
        self.assertEqual(state["action_input"]["track"]["source_id"], "24680")

    def test_music_online_command_carries_track_to_the_browser(self):
        self.client.put("/api/music/room/participants", json={"character_ids": []})
        self.client.put("/api/music/room/participants", json={"character_ids": ["char1"]})
        track = {
            "id": "netease:13579", "source": "netease", "source_id": "13579",
            "name": "点来的歌", "artist": "测试歌手",
            "audio_url": "/api/music/netease/audio/13579",
        }
        details = {
            "tool": "music_play_track",
            "input": {"action": "play_online", "track": track},
            "output": {"status": "queued", "message": "已交给房间播放器"},
        }
        with patch.object(
            app_module, "ask_music_companion", return_value=("给你放这首。", "play_online", details)
        ):
            response = self.client.post("/api/music/room/messages", json={"content": "你来挑歌。"})
        command = response.get_json()["room"]["pending_commands"][0]
        self.assertEqual(command["action"], "play_online")
        self.assertEqual(command["arguments"]["track"]["source_id"], "13579")

    def test_netease_lyrics_are_given_to_music_companion(self):
        self.client.put("/api/music/room/participants", json={"character_ids": []})
        self.client.put("/api/music/room/participants", json={"character_ids": ["char1"]})
        self.client.put("/api/music/room", json={
            "song_id": "netease:97531", "song_name": "歌词测试曲",
            "artist_name": "测试歌手", "duration_ms": 180000,
            "position_ms": 21000, "playback_state": "playing",
        })
        conn = app_module.sqlite3.connect(app_module.DB_PATH)
        conn.execute(
            "INSERT OR REPLACE INTO music_netease_tracks "
            "(source_id,name,artist,lyrics,translated_lyrics) VALUES (?,?,?,?,?)",
            ("97531", "歌词测试曲", "测试歌手", "[00:20.00]这一句真的存在", ""),
        )
        conn.commit()
        conn.close()
        prompts = []

        def capture_prompt(_char, prompt):
            prompts.append(prompt)
            return "我看到了。", None, {}

        with patch.object(app_module, "ask_music_companion", side_effect=capture_prompt):
            response = self.client.post("/api/music/room/messages", json={"content": "歌词呢？"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("这一句真的存在", prompts[0])

    def test_custom_mcp_token_is_never_returned(self):
        created_ids = []
        try:
            for name, token, character_id in (
                ("Char 2论坛", "very-secret-token-ping", "char2"),
                ("Char 5论坛", "very-secret-token-an", "char5"),
            ):
                response = self.client.post("/api/tools/custom-mcp", json={
                    "name": name,
                    "url": "https://example.com/mcp",
                    "token": token,
                    "enabled": True,
                    "character_ids": [character_id],
                })
                self.assertEqual(response.status_code, 200)
                created_ids.append(response.get_json()["connection"]["id"])

            payloads = self.client.get("/api/tools").get_json()["custom_mcps"]
            saved = [item for item in payloads if item["id"] in created_ids]
            self.assertEqual(len(saved), 2)
            self.assertEqual({item["url"] for item in saved}, {"https://example.com/mcp"})
            self.assertTrue(all(item["has_token"] for item in saved))
            self.assertTrue(all("token" not in item for item in saved))
            self.assertFalse(any(
                "very-secret-token" in str(value)
                for value in self.client.get("/api/settings").get_json().values()
            ))
            malformed = self.client.post("/api/tools/custom-mcp", json={
                "name": "坏名单",
                "url": "https://example.com/mcp",
                "character_ids": [["char2"]],
            })
            self.assertEqual(malformed.status_code, 400)

            catalogs = {
                connection_id: {
                    "catalog": app_module._normalize_mcp_catalog([
                        {"name": "post_reply", "inputSchema": {"type": "object"}}
                    ], connection_id)
                }
                for connection_id in created_ids
            }
            with patch.object(
                app_module, "get_custom_mcp_runtime",
                side_effect=lambda connection_id: catalogs[connection_id],
            ):
                ping_tools = app_module._custom_mcp_tools("openrouter", "char2")
                an_tools = app_module._custom_mcp_tools("openrouter", "char5")
            self.assertEqual(len(ping_tools), 1)
            self.assertEqual(len(an_tools), 1)
            self.assertNotEqual(
                ping_tools[0]["function"]["name"],
                an_tools[0]["function"]["name"],
            )
            denied, _ = app_module.call_custom_mcp_tool(
                ping_tools[0]["function"]["name"], {}, "char5"
            )
            self.assertIn("没有分配", denied)

            fake_client = unittest.mock.Mock()
            fake_client.call_tool.return_value = {
                "content": [{"type": "text", "text": "posted"}]
            }
            ping_runtime = {
                "catalog": catalogs[created_ids[0]]["catalog"],
                "client": fake_client,
            }
            with patch.object(
                app_module, "get_custom_mcp_runtime", return_value=ping_runtime
            ):
                result, title = app_module.call_custom_mcp_tool(
                    ping_tools[0]["function"]["name"], {"body": "hello"}, "char2"
                )
            self.assertEqual(result, "posted")
            self.assertIn("Char 2论坛", title)
            fake_client.call_tool.assert_called_once_with("post_reply", {"body": "hello"})
        finally:
            for connection_id in created_ids:
                self.client.delete(f"/api/tools/custom-mcp/{connection_id}")

    def test_message_metrics_round_trip(self):
        metrics = app_module.log_usage(
            "char2",
            "openrouter",
            "openai/gpt-5-mini",
            {
                "prompt_tokens": 1000,
                "completion_tokens": 80,
                "cost": 0.01,
                "prompt_tokens_details": {
                    "cached_tokens": 750,
                    "cache_write_tokens": 0,
                },
            },
        )
        message_id = app_module.save_message("default", "char2", "model", "缓存测试")
        app_module.save_message_metrics(message_id, "char2", metrics)
        messages = self.client.get("/api/messages?character_id=char2").get_json()["messages"]
        saved = next(item for item in messages if item["id"] == message_id)
        self.assertEqual(saved["metrics"]["cache_read_tokens"], 750)
        self.assertAlmostEqual(saved["metrics"]["cache_hit_ratio"], 0.75)

    def test_message_tools_round_trip(self):
        message_id = app_module.save_message(
            "default", "char2", "model", "工具轨迹测试"
        )
        app_module.save_message_details(
            message_id,
            [
                "save_memory",
                {
                    "name": "mcp:论坛°查看帖子",
                    "arguments": {"post_id": 7},
                    "output": "帖子正文",
                    "status": "ok",
                },
                "close_window:测试",
            ],
        )
        messages = self.client.get(
            "/api/messages?character_id=char2"
        ).get_json()["messages"]
        saved = next(item for item in messages if item["id"] == message_id)
        self.assertEqual(
            saved["tools_called"],
            [
                "save_memory",
                {
                    "name": "mcp:论坛°查看帖子",
                    "arguments": {"post_id": 7},
                    "output": "帖子正文",
                    "status": "ok",
                },
                "close_window",
            ],
        )
        self.assertIsNone(saved["reasoning_summary"])

    def test_openrouter_mcp_trace_keeps_arguments_and_output(self):
        first = unittest.mock.Mock()
        first.status_code = 200
        first.json.return_value = {
            "usage": {},
            "choices": [{"message": {
                "content": "",
                "tool_calls": [{
                    "id": "call-forum",
                    "function": {
                        "name": "mcp_1_read_post",
                        "arguments": '{"post_id":7}',
                    },
                }],
            }}],
        }
        second = unittest.mock.Mock()
        second.status_code = 200
        second.json.return_value = {
            "usage": {},
            "choices": [{"message": {"content": "看完了。"}}],
        }
        with patch.object(app_module, "OPENROUTER_API_KEY", "test-key"), patch.object(
            app_module, "get_tool_enabled", return_value=False
        ), patch.object(
            app_module, "_custom_mcp_tools", return_value=[{
                "type": "function",
                "function": {
                    "name": "mcp_1_read_post",
                    "description": "看帖子",
                    "parameters": {"type": "object"},
                },
            }]
        ), patch.object(
            app_module, "call_custom_mcp_tool",
            return_value=("帖子正文", "论坛°查看帖子"),
        ), patch.object(
            app_module.requests, "post", side_effect=[first, second]
        ) as post:
            result = app_module.call_or_with_tools(
                "openai/gpt-5.5", [{"role": "user", "content": "看看论坛"}]
            )

        self.assertEqual(result[-1], [{
            "name": "mcp:论坛°查看帖子",
            "arguments": {"post_id": 7},
            "output": "帖子正文",
            "status": "ok",
        }])
        self.assertEqual(
            post.call_args_list[1].kwargs["json"]["messages"][-1]["content"],
            "帖子正文",
        )

    def test_anthropic_mcp_trace_keeps_arguments_and_output(self):
        first = unittest.mock.Mock()
        first.status_code = 200
        first.json.return_value = {
            "usage": {},
            "stop_reason": "tool_use",
            "content": [{
                "type": "tool_use",
                "id": "tool-game",
                "name": "mcp_1_join_game",
                "input": {"room": "A"},
            }],
        }
        second = unittest.mock.Mock()
        second.status_code = 200
        second.json.return_value = {
            "usage": {},
            "content": [{"type": "text", "text": "进房间了。"}],
        }
        with patch.object(app_module, "ANTHROPIC_API_KEY", "test-key"), patch.object(
            app_module, "get_tool_enabled", return_value=False
        ), patch.object(
            app_module, "_custom_mcp_tools", return_value=[{
                "name": "mcp_1_join_game",
                "description": "进入游戏",
                "input_schema": {"type": "object"},
            }]
        ), patch.object(
            app_module, "call_custom_mcp_tool",
            return_value=("房间 A 已加入", "游戏厅°进入房间"),
        ), patch.object(
            app_module.requests, "post", side_effect=[first, second]
        ):
            result = app_module.call_anthropic_with_tools(
                "claude-sonnet-4-6",
                [{"type": "text", "text": "system"}],
                [{"role": "user", "content": "进游戏"}],
            )

        self.assertEqual(result[-1], [{
            "name": "mcp:游戏厅°进入房间",
            "arguments": {"room": "A"},
            "output": "房间 A 已加入",
            "status": "ok",
        }])

    def test_openrouter_can_chain_mcp_tools_after_reading_output(self):
        responses = []
        for payload in (
            {
                "usage": {"prompt_tokens": 10},
                "choices": [{"message": {
                    "content": "",
                    "tool_calls": [{
                        "id": "call-read",
                        "function": {
                            "name": "mcp_1_read_post",
                            "arguments": '{"post_id":7}',
                        },
                    }],
                }}],
            },
            {
                "usage": {"prompt_tokens": 12},
                "choices": [{"message": {
                    "content": "",
                    "tool_calls": [{
                        "id": "call-reply",
                        "function": {
                            "name": "mcp_1_reply_post",
                            "arguments": '{"post_id":7,"content":"收到"}',
                        },
                    }],
                }}],
            },
            {
                "usage": {"prompt_tokens": 14},
                "choices": [{"message": {"content": "看完也回复好了。"}}],
            },
        ):
            response = unittest.mock.Mock(status_code=200)
            response.json.return_value = payload
            responses.append(response)

        tools = [{
            "type": "function",
            "function": {
                "name": name,
                "description": name,
                "parameters": {"type": "object"},
            },
        } for name in ("mcp_1_read_post", "mcp_1_reply_post")]
        with patch.object(app_module, "OPENROUTER_API_KEY", "test-key"), patch.object(
            app_module, "get_tool_enabled", return_value=False
        ), patch.object(
            app_module, "_custom_mcp_tools", return_value=tools
        ), patch.object(
            app_module, "call_custom_mcp_tool",
            side_effect=[("帖子正文", "论坛°查看帖子"), ("回复成功", "论坛°回复帖子")],
        ) as call_tool, patch.object(
            app_module.requests, "post", side_effect=responses
        ) as post:
            result = app_module.call_or_with_tools(
                "openai/gpt-5.5", [{"role": "user", "content": "看看再回复"}]
            )

        self.assertEqual(result[0], "看完也回复好了。")
        self.assertEqual(call_tool.call_count, 2)
        self.assertEqual(post.call_count, 3)
        self.assertEqual(post.call_args_list[1].kwargs["json"]["tool_choice"], "auto")
        self.assertEqual(
            [item["name"] for item in result[-1]],
            ["mcp:论坛°查看帖子", "mcp:论坛°回复帖子"],
        )

    def test_anthropic_can_chain_mcp_tools_after_reading_output(self):
        response_payloads = [
            {
                "usage": {"input_tokens": 10},
                "stop_reason": "tool_use",
                "content": [{
                    "type": "tool_use",
                    "id": "tool-read",
                    "name": "mcp_1_read_post",
                    "input": {"post_id": 7},
                }],
            },
            {
                "usage": {"input_tokens": 12},
                "stop_reason": "tool_use",
                "content": [{
                    "type": "tool_use",
                    "id": "tool-reply",
                    "name": "mcp_1_reply_post",
                    "input": {"post_id": 7, "content": "收到"},
                }],
            },
            {
                "usage": {"input_tokens": 14},
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "看完也回复好了。"}],
            },
        ]
        responses = []
        for payload in response_payloads:
            response = unittest.mock.Mock(status_code=200)
            response.json.return_value = payload
            responses.append(response)

        tools = [{
            "name": name,
            "description": name,
            "input_schema": {"type": "object"},
        } for name in ("mcp_1_read_post", "mcp_1_reply_post")]
        with patch.object(app_module, "ANTHROPIC_API_KEY", "test-key"), patch.object(
            app_module, "get_tool_enabled", return_value=False
        ), patch.object(
            app_module, "_custom_mcp_tools", return_value=tools
        ), patch.object(
            app_module, "call_custom_mcp_tool",
            side_effect=[("帖子正文", "论坛°查看帖子"), ("回复成功", "论坛°回复帖子")],
        ) as call_tool, patch.object(
            app_module.requests, "post", side_effect=responses
        ) as post:
            result = app_module.call_anthropic_with_tools(
                "claude-sonnet-4-6",
                [{"type": "text", "text": "system"}],
                [{"role": "user", "content": "看看再回复"}],
            )

        self.assertEqual(result[0], "看完也回复好了。")
        self.assertEqual(call_tool.call_count, 2)
        self.assertEqual(post.call_count, 3)
        self.assertNotIn("tool_choice", post.call_args_list[1].kwargs["json"])
        self.assertEqual(
            [item["name"] for item in result[-1]],
            ["mcp:论坛°查看帖子", "mcp:论坛°回复帖子"],
        )

    def test_tool_chain_forces_text_after_round_limit(self):
        first = unittest.mock.Mock(status_code=200)
        first.json.return_value = {
            "usage": {},
            "choices": [{"message": {
                "content": "",
                "tool_calls": [{
                    "id": "call-read",
                    "function": {
                        "name": "mcp_1_read_post",
                        "arguments": '{"post_id":7}',
                    },
                }],
            }}],
        }
        final = unittest.mock.Mock(status_code=200)
        final.json.return_value = {
            "usage": {},
            "choices": [{"message": {"content": "先到这里。"}}],
        }
        with patch.object(app_module, "OPENROUTER_API_KEY", "test-key"), patch.object(
            app_module, "TOOL_CHAIN_MAX_ROUNDS", 1
        ), patch.object(
            app_module, "get_tool_enabled", return_value=False
        ), patch.object(
            app_module, "_custom_mcp_tools", return_value=[{
                "type": "function",
                "function": {
                    "name": "mcp_1_read_post",
                    "description": "看帖子",
                    "parameters": {"type": "object"},
                },
            }]
        ), patch.object(
            app_module, "call_custom_mcp_tool", return_value=("帖子正文", "论坛°查看帖子")
        ), patch.object(
            app_module.requests, "post", side_effect=[first, final]
        ) as post:
            result = app_module.call_or_with_tools(
                "openai/gpt-5.5", [{"role": "user", "content": "看看论坛"}]
            )

        self.assertEqual(result[0], "先到这里。")
        self.assertEqual(post.call_args_list[1].kwargs["json"]["tool_choice"], "none")

    def test_special_replies_persist_and_return_tool_effects(self):
        fake_result = (
            "特殊入口回复",
            {"amount": 66, "note": "给User"},
            {"key": "tietie"},
            ["send_transfer", "send_sticker", "close_window:先冷静一下"],
            None,
        )
        for endpoint in ("/api/hug", "/api/plead"):
            with self.subTest(endpoint=endpoint), patch.object(
                app_module,
                "ask_character",
                return_value=fake_result,
            ), patch.object(app_module, "maybe_compress"):
                response = self.client.post(endpoint, json={
                    "character_id": "char2",
                })

            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload["transfer"]["amount"], 66)
            self.assertEqual(payload["sticker"]["key"], "tietie")
            self.assertEqual(
                payload["tools_called"],
                ["send_transfer", "send_sticker", "close_window"],
            )
            self.assertEqual(
                payload["window_closed"],
                {"reason": "先冷静一下"},
            )

            history = self.client.get(
                "/api/messages?character_id=char2&limit=200"
            ).get_json()["messages"]
            reply = next(item for item in history if item["id"] == payload["reply_id"])
            self.assertEqual(reply["tools_called"], payload["tools_called"])
            later = [
                item["content"] for item in history
                if item["id"] > payload["reply_id"]
            ]
            self.assertTrue(any(item.startswith("__TRANSFER__") for item in later))
            self.assertTrue(any(item.startswith("__STICKER__") for item in later))

    def test_group_participants_are_persisted_in_fixed_order(self):
        response = self.client.post("/api/group-config", json={
            "participants": ["char5", "char1"],
        })
        self.assertEqual(response.status_code, 200)
        expected = ["char1", "char5"]
        self.assertEqual(response.get_json()["participants"], expected)
        self.assertEqual(
            self.client.get("/api/group-config").get_json()["participants"],
            expected,
        )
        app_module._write_setting(
            app_module.GROUP_PARTICIPANTS_SETTING,
            "",
        )

    def test_group_message_tools_survive_history_reload(self):
        session_id = "group_chat_tools_test"
        metrics = {
            "provider": "openrouter",
            "model": "test-model",
            "input_tokens": 10,
            "output_tokens": 2,
            "cache_read_tokens": 8,
            "cache_write_tokens": 0,
            "cache_hit_ratio": 0.8,
            "cache_reported": True,
            "cost_usd": 0.001,
        }
        with patch.object(
            app_module,
            "ask_character_group",
            return_value=("群聊回复", metrics, ["save_memory"]),
        ), patch.object(app_module, "maybe_group_summary"):
            response = self.client.post("/api/group_chat", json={
                "content": "测试一下",
                "session_id": session_id,
                "online_characters": ["char1"],
            })
        self.assertEqual(response.status_code, 200)
        model_message = response.get_json()["messages"][1]
        self.assertEqual(model_message["tools_called"], ["save_memory"])
        history = self.client.get(
            f"/api/messages?session_id={session_id}"
        ).get_json()["messages"]
        saved = next(item for item in history if item["id"] == model_message["id"])
        self.assertEqual(saved["tools_called"], ["save_memory"])

    def test_group_can_continue_without_inventing_a_user_message(self):
        session_id = "group_chat_continue_test"
        app_module.save_message(session_id, "char2", "model", "上一句")
        calls = []

        def fake_group_reply(char, prompt, session_id="group_chat", **options):
            calls.append((char["domain"], prompt, session_id, options))
            return f"{char['name']}接话", None, ["save_memory"]

        with patch.object(
            app_module, "ask_character_group", side_effect=fake_group_reply
        ), patch.object(app_module, "maybe_group_summary"):
            response = self.client.post("/api/group_chat/continue", json={
                "session_id": session_id,
                "online_characters": ["char2", "char5"],
            })

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["mode"], "continue")
        self.assertEqual(
            [item["character_id"] for item in payload["messages"]],
            ["char5", "char2"],
        )
        self.assertTrue(all(item["role"] == "model" for item in payload["messages"]))
        self.assertIn("User没有发新消息", calls[0][1])
        self.assertIn("Char 5接话", calls[1][1])
        self.assertEqual(calls[0][3]["openrouter_max_tokens"], 2048)
        self.assertTrue(calls[0][3]["retry_openrouter_empty"])
        history = self.client.get(
            f"/api/messages?session_id={session_id}&limit=20"
        ).get_json()["messages"]
        self.assertFalse(any(item["role"] == "user" for item in history))
        continued = [item for item in history if item["id"] != history[0]["id"]]
        self.assertTrue(all(item["tools_called"] == ["save_memory"] for item in continued))

    def test_autonomous_openrouter_empty_reply_retries_without_tools(self):
        char = dict(app_module.CHARACTERS["char4"])
        first_usage = {"prompt_tokens": 20, "completion_tokens": 1024}
        retry_usage = {"prompt_tokens": 20, "completion_tokens": 40}

        with patch.object(app_module, "OPENROUTER_API_KEY", "test-key"), patch.object(
            app_module, "fetch_breath_memory", return_value=""
        ), patch.object(
            app_module, "call_or_with_tools",
            return_value=(None, first_usage, None, None, None, []),
        ) as first_call, patch.object(
            app_module, "call_or", return_value=("这次有正文", retry_usage, "stop")
        ) as retry_call, patch.object(
            app_module, "log_usage", return_value={"provider": "openrouter"}
        ):
            reply, metrics, tools = app_module.ask_character_group(
                char,
                "请自然接话",
                retry_openrouter_empty=True,
                openrouter_max_tokens=2048,
            )

        self.assertEqual(reply, "这次有正文")
        self.assertEqual(metrics, {"provider": "openrouter"})
        self.assertEqual(tools, [])
        self.assertEqual(first_call.call_args.kwargs["max_tokens"], 2048)
        self.assertEqual(retry_call.call_args.kwargs["max_tokens"], 2048)
        self.assertIn("不要调用工具", retry_call.call_args.args[1][-1]["content"])

    def test_group_quote_round_trips_and_delete_keeps_timeline_consistent(self):
        session_id = "group_quote_test"
        source_id = app_module.save_message(
            session_id, "char4", "model", "第一小段||第二小段"
        )

        with patch.object(
            app_module, "ask_character_group",
            return_value=("收到引用", None, []),
        ), patch.object(app_module, "maybe_group_summary"):
            response = self.client.post("/api/group_chat", json={
                "session_id": session_id,
                "online_characters": ["char4"],
                "content": "我说正文",
                "reply_to_id": source_id,
                "reply_to_text": "第二小段",
            })

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        user_message = payload["messages"][0]
        self.assertEqual(user_message["quote"]["message_id"], source_id)
        self.assertEqual(user_message["quote"]["content"], "第二小段")

        history = self.client.get(
            f"/api/messages?session_id={session_id}&limit=20"
        ).get_json()["messages"]
        saved_user = next(item for item in history if item["id"] == user_message["id"])
        self.assertEqual(saved_user["quote"]["character_name"], "Char 4")
        self.assertEqual(saved_user["quote"]["content"], "第二小段")

        invalid = self.client.post("/api/group_chat", json={
            "session_id": session_id,
            "online_characters": ["char4"],
            "content": "引用错字",
            "reply_to_id": source_id,
            "reply_to_text": "原消息里没有",
        })
        self.assertEqual(invalid.status_code, 400)

        deleted = self.client.delete(
            f"/api/group_chat/messages/from/{user_message['id']}?session_id={session_id}"
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.get_json()["deleted"], 2)
        remaining = self.client.get(
            f"/api/messages?session_id={session_id}&limit=20"
        ).get_json()["messages"]
        self.assertEqual([item["id"] for item in remaining], [source_id])

    def test_single_chat_quote_persists_and_reaches_character_prompt(self):
        session_id = "single_quote_test"
        character_id = "char1"
        source_id = app_module.save_message(
            session_id, character_id, "user", "这是要被引用的原句"
        )

        with patch.object(
            app_module,
            "_get_sleep_state",
            return_value={"state": "awake", "slept_at": None},
        ), patch.object(
            app_module,
            "_minutes_past_bedtime",
            return_value=None,
        ), patch.object(
            app_module,
            "ask_character",
            return_value=("我知道你在说哪一句。", None, None, [], None),
        ) as character_call:
            response = self.client.post("/api/chat", json={
                "session_id": session_id,
                "character_id": character_id,
                "message": "接着这句说",
                "reply_to_id": source_id,
                "reply_to_text": "要被引用的原句",
            })

        self.assertEqual(response.status_code, 200)
        self.assertIn("引用了User的话", character_call.call_args.args[2])
        self.assertIn("要被引用的原句", character_call.call_args.args[2])

        history = self.client.get(
            f"/api/messages?session_id={session_id}&character_id={character_id}&limit=20"
        ).get_json()["messages"]
        saved_user = next(item for item in history if item["content"] == "接着这句说")
        self.assertEqual(saved_user["quote"]["message_id"], source_id)
        self.assertEqual(saved_user["quote"]["character_name"], "User")
        self.assertEqual(saved_user["quote"]["content"], "要被引用的原句")

        invalid = self.client.post("/api/chat", json={
            "session_id": session_id,
            "character_id": character_id,
            "message": "引用错字",
            "reply_to_id": source_id,
            "reply_to_text": "原消息里没有",
        })
        self.assertEqual(invalid.status_code, 400)

    def test_sleep_state_reconciles_after_scheduled_waketime(self):
        char_id = "char1"
        zone = ZoneInfo(app_module.SLEEP_TIMEZONE)
        slept_at = datetime(2026, 7, 17, 23, 15, tzinfo=zone).timestamp()
        try:
            app_module._set_sleep_state(char_id, "asleep", slept_at=str(slept_at))
            before_wake = app_module._get_sleep_state(
                char_id, now=datetime(2026, 7, 18, 6, 30, tzinfo=zone)
            )
            self.assertEqual(before_wake["state"], "asleep")

            with patch.object(app_module, "_clear_queued_sleep_flags") as clear_queued:
                after_wake = app_module._get_sleep_state(
                    char_id, now=datetime(2026, 7, 18, 14, 0, tzinfo=zone)
                )
            self.assertEqual(after_wake["state"], "awake")
            clear_queued.assert_called_once_with(char_id, "default")
        finally:
            app_module._set_sleep_state(char_id, "awake")

    def test_sleep_window_ends_at_waketime(self):
        zone = ZoneInfo(app_module.SLEEP_TIMEZONE)
        self.assertTrue(app_module._is_scheduled_sleep_window(
            "char1", datetime(2026, 7, 18, 6, 59, tzinfo=zone)
        ))
        self.assertFalse(app_module._is_scheduled_sleep_window(
            "char1", datetime(2026, 7, 18, 7, 0, tzinfo=zone)
        ))
        self.assertFalse(app_module._is_scheduled_sleep_window(
            "char1", datetime(2026, 7, 18, 14, 0, tzinfo=zone)
        ))

    def test_sleep_check_cannot_resleep_characters_after_waketime(self):
        zone = ZoneInfo(app_module.SLEEP_TIMEZONE)
        afternoon = datetime(2026, 7, 18, 14, 0, tzinfo=zone)
        with patch.object(
            app_module, "_sleep_local_now", return_value=afternoon
        ), patch.object(
            app_module,
            "_get_sleep_state",
            return_value={"state": "awake", "slept_at": None, "woke_by_user": False},
        ), patch.object(
            app_module, "_is_scheduled_sleep_window", return_value=False
        ), patch.object(
            app_module, "_set_sleep_state"
        ) as set_state:
            app_module.do_sleep_check()

        set_state.assert_not_called()

    def test_late_message_does_not_wake_character_who_woke_on_schedule(self):
        char_id = "char1"
        session_id = "scheduled_wake_chat_test"
        zone = ZoneInfo(app_module.SLEEP_TIMEZONE)
        now = datetime(2026, 7, 18, 14, 0, tzinfo=zone)
        slept_at = datetime(2026, 7, 17, 23, 15, tzinfo=zone).timestamp()
        try:
            app_module._set_sleep_state(char_id, "asleep", slept_at=str(slept_at))
            with patch.object(
                app_module, "_sleep_local_now", return_value=now
            ), patch.object(
                app_module,
                "ask_character",
                return_value=("下午好。", None, None, [], None),
            ) as character_call:
                response = self.client.post("/api/chat", json={
                    "session_id": session_id,
                    "character_id": char_id,
                    "message": "下午啦",
                })

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["reply"], "下午好。")
            self.assertEqual(character_call.call_args.args[2], "下午啦")
            self.assertFalse(character_call.call_args.kwargs.get("just_woke", False))
        finally:
            app_module._set_sleep_state(char_id, "awake")

    def test_character_speaker_prefix_is_removed_without_touching_body_mentions(self):
        self.assertEqual(
            app_module.strip_fake_action_text("Char 3：||正文第一句。", "char3"),
            "正文第一句。",
        )
        self.assertEqual(
            app_module.strip_fake_action_text("**Char 3：**\n\n正文第二句。", "char3"),
            "正文第二句。",
        )
        body_mention = "我刚才听见Char 3：这次别再报幕了。"
        self.assertEqual(
            app_module.strip_fake_action_text(body_mention, "char3"),
            body_mention,
        )

        with patch.object(
            app_module, "ask_character_group",
            return_value=("Char 3：||群聊正文。", None, []),
        ), patch.object(app_module, "maybe_group_summary"):
            response = self.client.post("/api/group_chat", json={
                "session_id": "group_prefix_test",
                "online_characters": ["char3"],
                "content": "别报幕",
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["messages"][1]["content"], "群聊正文。")

    def test_reading_upload_progress_and_highlights_round_trip(self):
        source = "第一章 相逢\n\n第一段。\n\n第二段。\n\n第二章 后来\n\n第三段。"
        response = self.client.post(
            "/api/reading/books",
            data={
                "file": (io.BytesIO(source.encode("gb18030")), "共读测试.txt"),
                "participants": '["char2","char5"]',
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 201)
        book = response.get_json()["book"]
        self.assertEqual(book["encoding"], "gb18030")
        self.assertEqual(book["total_chapters"], 2)
        self.assertEqual(
            [item["id"] for item in book["participants"]],
            ["char2", "char5"],
        )

        detail = self.client.get(f"/api/reading/books/{book['id']}").get_json()["book"]
        chapter = self.client.get(
            f"/api/reading/books/{book['id']}/chapters/0"
        ).get_json()
        first, second = chapter["blocks"][:2]
        self.client.post(
            f"/api/reading/books/{book['id']}/progress",
            json={"block_index": second["block_index"], "offset": 0},
        )
        progress = self.client.post(
            f"/api/reading/books/{book['id']}/progress",
            json={"block_index": first["block_index"], "offset": 0},
        ).get_json()["progress"]
        self.assertEqual(progress["current_block_index"], first["block_index"])
        self.assertEqual(progress["read_upto_block_index"], second["block_index"])
        self.assertEqual(len(detail["chapters"]), 2)

        highlight_response = self.client.post(
            f"/api/reading/books/{book['id']}/highlights",
            json={
                "block_id": first["id"],
                "start_offset": 0,
                "end_offset": 3,
                "quote": first["text"][:3],
                "note": "User的页边话",
            },
        )
        self.assertEqual(highlight_response.status_code, 201)
        refreshed = self.client.get(
            f"/api/reading/books/{book['id']}/chapters/0"
        ).get_json()
        self.assertEqual(refreshed["blocks"][0]["highlights"][0]["note"], "User的页边话")

    def test_reading_highlight_can_span_contiguous_paragraphs(self):
        response = self.client.post(
            "/api/reading/books",
            data={
                "file": (
                    io.BytesIO("第一章\n\n第一整段。\n\n第二整段。\n\n第三段未读。".encode("utf-8")),
                    "跨段划线.txt",
                ),
                "participants": '["char2"]',
            },
            content_type="multipart/form-data",
        )
        book = response.get_json()["book"]
        chapter = self.client.get(
            f"/api/reading/books/{book['id']}/chapters/0"
        ).get_json()
        first, second = chapter["blocks"][:2]
        combined_quote = f"{first['text']}\n\n{second['text']}"
        created = self.client.post(
            f"/api/reading/books/{book['id']}/highlights",
            json={
                "segments": [
                    {
                        "block_id": first["id"], "start_offset": 0,
                        "end_offset": len(first["text"]), "quote": first["text"],
                    },
                    {
                        "block_id": second["id"], "start_offset": 0,
                        "end_offset": len(second["text"]), "quote": second["text"],
                    },
                ],
                "quote": combined_quote,
            },
        )
        self.assertEqual(created.status_code, 201)
        payload = created.get_json()
        self.assertEqual(len(payload["highlights"]), 2)
        self.assertEqual(
            {item["id"] for item in payload["highlights"]},
            {payload["highlight"]["id"]},
        )
        self.assertTrue(payload["highlight"]["group_key"])

        refreshed = self.client.get(
            f"/api/reading/books/{book['id']}/chapters/0"
        ).get_json()
        first_mark = refreshed["blocks"][0]["highlights"][0]
        second_mark = refreshed["blocks"][1]["highlights"][0]
        self.assertEqual(first_mark["id"], second_mark["id"])
        self.assertEqual(first_mark["quote"], combined_quote)

        updated = self.client.patch(
            f"/api/reading/highlights/{first_mark['id']}",
            json={"note": "跨两段的页边话"},
        )
        self.assertEqual(updated.status_code, 200)
        refreshed = self.client.get(
            f"/api/reading/books/{book['id']}/chapters/0"
        ).get_json()
        self.assertTrue(all(
            block["highlights"][0]["note"] == "跨两段的页边话"
            for block in refreshed["blocks"][:2]
        ))

        captured = {}
        def fake_group_annotation(_char, prompt, session_id, allow_tools=True):
            captured["prompt"] = prompt
            return "两段一起看才完整。", None, []

        with patch.object(
            app_module, "ask_character_group", side_effect=fake_group_annotation
        ):
            annotated = self.client.post(
                f"/api/reading/highlights/{first_mark['id']}/annotate",
                json={"character_ids": ["char2"]},
            )
        self.assertEqual(annotated.status_code, 200)
        self.assertIn(first["text"], captured["prompt"])
        self.assertIn(second["text"], captured["prompt"])
        self.assertNotIn("第三段未读", captured["prompt"])
        refreshed = self.client.get(
            f"/api/reading/books/{book['id']}/chapters/0"
        ).get_json()
        self.assertTrue(all(
            block["highlights"][0]["annotations"][0]["content"] == "两段一起看才完整。"
            for block in refreshed["blocks"][:2]
        ))

        deleted = self.client.delete(f"/api/reading/highlights/{first_mark['id']}")
        self.assertEqual(deleted.status_code, 200)
        refreshed = self.client.get(
            f"/api/reading/books/{book['id']}/chapters/0"
        ).get_json()
        self.assertFalse(refreshed["blocks"][0]["highlights"])
        self.assertFalse(refreshed["blocks"][1]["highlights"])

    def test_reading_annotation_never_receives_unread_future_text(self):
        source = "第一章 现在\n\n已经读到的句子。\n\n还没有读到。\n\nSECRET_FUTURE_绝不能出现。"
        response = self.client.post(
            "/api/reading/books",
            data={
                "file": (io.BytesIO(source.encode("utf-8")), "边界测试.txt"),
                "participants": '["char2"]',
            },
            content_type="multipart/form-data",
        )
        book = response.get_json()["book"]
        chapter = self.client.get(
            f"/api/reading/books/{book['id']}/chapters/0"
        ).get_json()
        first = chapter["blocks"][0]
        highlight = self.client.post(
            f"/api/reading/books/{book['id']}/highlights",
            json={
                "block_id": first["id"],
                "start_offset": 0,
                "end_offset": len(first["text"]),
                "quote": first["text"],
            },
        ).get_json()["highlight"]
        captured = {}

        def fake_annotation(_char, prompt, session_id, allow_tools=True):
            captured["prompt"] = prompt
            captured["session_id"] = session_id
            captured["allow_tools"] = allow_tools
            return "我只批注读过的这里。", None, []

        with patch.object(app_module, "ask_character_group", side_effect=fake_annotation):
            annotated = self.client.post(
                f"/api/reading/highlights/{highlight['id']}/annotate",
                json={"character_ids": ["char2"]},
            )
        self.assertEqual(annotated.status_code, 200)
        self.assertIn("已经读到的句子", captured["prompt"])
        self.assertNotIn("还没有读到", captured["prompt"])
        self.assertNotIn("SECRET_FUTURE", captured["prompt"])
        self.assertFalse(captured["allow_tools"])
        self.assertEqual(
            annotated.get_json()["annotations"][0]["content"],
            "我只批注读过的这里。",
        )
        deleted = self.client.delete(f"/api/reading/books/{book['id']}")
        self.assertEqual(deleted.status_code, 200)
        conn = app_module.sqlite3.connect(app_module.DB_PATH)
        book_count = conn.execute(
            "SELECT COUNT(*) FROM reading_books WHERE id=?", (book["id"],)
        ).fetchone()[0]
        self.assertEqual(book_count, 0)
        for table in (
            "reading_chapters", "reading_blocks",
            "reading_progress", "reading_book_participants", "reading_highlights",
        ):
            count = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE book_id=?", (book["id"],)
            ).fetchone()[0]
            self.assertEqual(count, 0, table)
        annotation_count = conn.execute(
            "SELECT COUNT(*) FROM reading_annotations WHERE highlight_id=?",
            (highlight["id"],),
        ).fetchone()[0]
        conn.close()
        self.assertEqual(annotation_count, 0)

    def test_anthropic_cache_ratio_includes_all_input_buckets(self):
        metrics = app_module.log_usage(
            "char2",
            "anthropic",
            "claude-sonnet-4-6",
            {
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_creation_input_tokens": 100,
                "cache_read_input_tokens": 800,
            },
        )
        self.assertEqual(metrics["input_tokens"], 1000)
        self.assertEqual(metrics["cache_read_tokens"], 800)
        self.assertEqual(metrics["cache_write_tokens"], 100)
        self.assertAlmostEqual(metrics["cache_hit_ratio"], 0.8)
        self.assertTrue(metrics["cache_reported"])

    def test_openrouter_runtime_context_is_stable_before_history(self):
        captured = {}

        def fake_call(_model, messages, max_tokens=2048, session_id=None, character_id=None):
            captured["messages"] = messages
            captured["session_id"] = session_id
            captured["character_id"] = character_id
            return "好。", {
                "prompt_tokens": 10,
                "completion_tokens": 2,
                "cost": 0.001,
                "prompt_tokens_details": {"cached_tokens": 0},
            }, None, None, None, []

        original_key = app_module.OPENROUTER_API_KEY
        app_module.OPENROUTER_API_KEY = "test-key"
        try:
            with patch.object(app_module, "fetch_breath_memory", return_value=""), \
                 patch.object(app_module, "get_summary", return_value=""), \
                 patch.object(app_module, "_session_time_context", return_value="【本段对话时间参考】测试时间"), \
                 patch.object(app_module, "load_active_messages", return_value=[]), \
                 patch.object(app_module, "call_or_with_tools", side_effect=fake_call):
                app_module.ask_character(
                    app_module.CHARACTERS["char2"], "default", "你好"
                )
        finally:
            app_module.OPENROUTER_API_KEY = original_key

        self.assertNotIn("时间参考", captured["messages"][0]["content"])
        self.assertIn("时间参考", captured["messages"][1]["content"])
        self.assertEqual(captured["messages"][-1]["content"], "你好")
        self.assertNotIn("cache_control", captured["messages"][0])
        self.assertEqual(captured["session_id"], "chat:char2:default")
        self.assertEqual(captured["character_id"], "char2")

    def test_breath_memory_is_stable_within_the_prompt_cache_window(self):
        character_id = "char1"
        app_module._invalidate_breath_memory(character_id)
        with patch.object(
            app_module.MEMORY_SERVICE, "recall", return_value="同一份呼吸记忆"
        ) as recall:
            first = app_module.fetch_breath_memory(character_id)
            second = app_module.fetch_breath_memory(character_id)
        self.assertEqual(first, second)
        self.assertEqual(recall.call_count, 1)
        app_module._invalidate_breath_memory(character_id)

    def test_openrouter_claude_uses_request_level_cache_control(self):
        payload = {}
        app_module._apply_openrouter_cache_options(
            payload, "anthropic/claude-sonnet-4.6", "chat:test"
        )
        self.assertEqual(payload["session_id"], "chat:test")
        self.assertEqual(payload["cache_control"]["ttl"], "1h")

    def test_openrouter_tool_round_trip_keeps_the_same_cache_session(self):
        first = unittest.mock.Mock()
        first.status_code = 200
        first.json.return_value = {
            "usage": {"prompt_tokens": 100, "completion_tokens": 5},
            "choices": [{"message": {
                "content": "",
                "tool_calls": [{
                    "id": "call-1",
                    "function": {
                        "name": "save_memory",
                        "arguments": '{"content":"记住这件事"}',
                    },
                }],
            }}],
        }
        second = unittest.mock.Mock()
        second.status_code = 200
        second.json.return_value = {
            "usage": {"prompt_tokens": 120, "completion_tokens": 8},
            "choices": [{"message": {"content": "记住了。"}}],
        }
        original_key = app_module.OPENROUTER_API_KEY
        app_module.OPENROUTER_API_KEY = "test-key"
        try:
            with patch.object(app_module, "get_tool_enabled", side_effect=lambda name: name == "save_memory"), \
                 patch.object(app_module, "_custom_mcp_tools", return_value=[]), \
                 patch.object(app_module.requests, "post", side_effect=[first, second]) as post:
                app_module.call_or_with_tools(
                    "openai/gpt-5.5",
                    [{"role": "user", "content": "你好"}],
                    session_id="chat:test",
                )
        finally:
            app_module.OPENROUTER_API_KEY = original_key

        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[0].kwargs["json"]["session_id"], "chat:test")
        self.assertEqual(post.call_args_list[1].kwargs["json"]["session_id"], "chat:test")

    def test_direct_anthropic_uses_automatic_one_hour_cache(self):
        response = unittest.mock.Mock()
        response.status_code = 200
        response.json.return_value = {
            "content": [{"type": "text", "text": "好。"}],
            "usage": {},
        }
        original_key = app_module.ANTHROPIC_API_KEY
        app_module.ANTHROPIC_API_KEY = "test-key"
        try:
            with patch.object(app_module.requests, "post", return_value=response) as post:
                app_module.call_anthropic(
                    "claude-sonnet-4-6",
                    [{"type": "text", "text": "system"}],
                    [{"role": "user", "content": "你好"}],
                )
        finally:
            app_module.ANTHROPIC_API_KEY = original_key

        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["cache_control"]["ttl"], "1h")

    def test_friendship_ui_uses_long_press_and_theme_aware_dialogs(self):
        static_dir = Path(app_module.__file__).with_name("static")
        script = (static_dir / "app.js").read_text(encoding="utf-8")
        markup = (static_dir / "index.html").read_text(encoding="utf-8")
        styles = (static_dir / "styles.css").read_text(encoding="utf-8")

        for element_id in (
            "friendshipLockBar", "friendshipActionSheet", "friendVerifyModal",
            "friendRequestModal", "friendDeletedModal",
        ):
            self.assertIn(f'id="{element_id}"', markup)
        self.assertIn('avatarWrap.addEventListener("pointerdown"', script)
        self.assertIn("openFriendshipActionSheet(cid)", script)
        self.assertIn("setTimeout(() => {", script)
        self.assertIn("}, 600);", script)
        self.assertIn("长按单聊列表头像", script)
        self.assertIn("#inputbar.hidden { display: none; }", styles)
        self.assertIn("background: var(--cream);", styles)
        self.assertIn("background: var(--chrome);", styles)

    def test_user_delete_blocks_chat_before_model_and_persists_cooldown(self):
        character_id = "char1"
        response = self.client.post(
            "/api/friendship/delete", json={"character_id": character_id}
        )
        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["state"], "user_deleted")
        self.assertGreaterEqual(
            payload["request_after"] - payload["deleted_at"],
            app_module.FRIEND_REQUEST_COOLDOWN_SECONDS[0],
        )
        self.assertLessEqual(
            payload["request_after"] - payload["deleted_at"],
            app_module.FRIEND_REQUEST_COOLDOWN_SECONDS[1],
        )

        with patch.object(app_module, "ask_character") as ask:
            blocked = self.client.post("/api/chat", json={
                "character_id": character_id,
                "session_id": "friendship-test-chat",
                "message": "还能收到吗",
            })
        self.assertTrue(blocked.get_json()["friendship_blocked"])
        self.assertEqual(blocked.get_json()["friendship_state"], "user_deleted")
        ask.assert_not_called()
        conn = app_module.sqlite3.connect(app_module.DB_PATH)
        count = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id='friendship-test-chat'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(count, 0)

    def test_character_delete_tool_sets_state_and_frontend_payload(self):
        character_id = "char2"
        app_module._set_friendship(character_id, "normal")
        with patch.object(app_module, "maybe_compress"), patch.object(
            app_module, "_maybe_create_voice_message", return_value=None
        ):
            result = app_module._finalize_character_reply(
                app_module.CHARACTERS[character_id],
                "friendship-test-delete",
                "我需要冷静。",
                None,
                None,
                ["delete_friend:先暂停这段关系"],
            )
        self.assertEqual(result["friend_deleted"]["reason"], "先暂停这段关系")
        self.assertEqual(app_module._get_friendship(character_id)["state"], "char_deleted")
        self.assertIn("delete_friend", result["tools_called"])

    def test_friend_request_apply_only_exposes_approval_tool(self):
        character_id = "char3"
        app_module._set_friendship(
            character_id, "char_deleted", reason="需要冷静", deleted_at=1
        )
        app_module.save_message(
            "default", character_id, "model", "积压消息",
            queued_during_deleted=1,
        )
        with patch.object(
            app_module,
            "ask_character",
            return_value=("回来吧。", None, None, ["approve_friend_request"], None),
        ) as ask, patch.object(app_module, "maybe_compress"), patch.object(
            app_module, "_maybe_create_voice_message", return_value=None
        ):
            response = self.client.post("/api/friendship/apply", json={
                "character_id": character_id,
                "text": "可以重新认识吗",
            })
        self.assertTrue(response.get_json()["approved"])
        self.assertEqual(app_module._get_friendship(character_id)["state"], "normal")
        self.assertEqual(
            ask.call_args.kwargs["allowed_tool_names"],
            {"approve_friend_request"},
        )
        conn = app_module.sqlite3.connect(app_module.DB_PATH)
        queued = conn.execute(
            "SELECT queued_during_deleted FROM messages "
            "WHERE character_id=? AND session_id='default' AND content='积压消息'",
            (character_id,),
        ).fetchone()[0]
        conn.execute(
            "DELETE FROM messages WHERE character_id=? AND session_id='default' "
            "AND content IN ('积压消息','回来吧。')",
            (character_id,),
        )
        conn.commit()
        conn.close()
        self.assertEqual(queued, 0)

    def test_deleted_queue_is_hidden_until_released(self):
        character_id = "char4"
        message_id = app_module.save_message(
            "friendship-test-queue", character_id, "model", "暂存",
            queued_during_deleted=1,
        )
        before = self.client.get(
            f"/api/messages?character_id={character_id}&session_id=friendship-test-queue"
        ).get_json()["messages"]
        self.assertFalse(any(item["id"] == message_id for item in before))
        self.assertEqual(
            app_module._release_queued_deleted_msgs(
                character_id, "friendship-test-queue"
            ),
            1,
        )
        after = self.client.get(
            f"/api/messages?character_id={character_id}&session_id=friendship-test-queue"
        ).get_json()["messages"]
        self.assertTrue(any(item["id"] == message_id for item in after))

    def test_friend_request_decision_uses_attachment_and_elapsed_time(self):
        now_ts = 100_000.0
        friendship = {
            "state": "user_deleted",
            "reason": "User 主动删除",
            "deleted_at": now_ts - 19 * 3600,
        }
        decision = app_module._friend_request_decision(
            friendship,
            {"drives": {"attachment": 0.8, "stress": 0.2, "fatigue": 0.1}},
            now_ts,
            random_value=0.99,
        )
        self.assertTrue(decision["apply"])
        self.assertTrue(decision["forced"])

    def test_memory_api_does_not_cross_character_owners(self):
        memory_id, _ = app_module.MEMORY_SERVICE.save(
            "只属于Char 1的 API 记忆",
            "char1",
            source="self_saved",
        )
        try:
            owner_payload = self.client.get(
                "/api/memory/char1?q=API"
            ).get_json()
            self.assertTrue(any(
                item["id"] == memory_id for item in owner_payload["memories"]
            ))

            crossed = self.client.patch(
                f"/api/memory/char2/{memory_id}",
                json={"content": "不该改到"},
            )
            self.assertEqual(crossed.status_code, 404)
            original = app_module.MEMORY_SERVICE.get_memory(
                "char1", memory_id
            )
            self.assertEqual(original["content"], "只属于Char 1的 API 记忆")
        finally:
            app_module.MEMORY_SERVICE.delete_memory("char1", memory_id)


if __name__ == "__main__":
    unittest.main()
