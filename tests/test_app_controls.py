import io
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

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
        app_module.LIMITS.clear()
        app_module.LIMITS.update(self.original_limits)
        app_module._write_setting(app_module.THEME_SETTING_KEY, app_module.DEFAULT_THEME_ID)

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

    def test_login_stays_closed_without_a_configured_password(self):
        unauthenticated = app_module.app.test_client()
        with patch.object(app_module, "APP_PASSWORD", ""):
            response = unauthenticated.post("/api/login", json={"password": ""})
        self.assertEqual(response.status_code, 503)

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
        self.assertTrue(payload["phone"]["read_only"])
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
        self.assertEqual(appearance["chat_background"]["default_url"], "/static/theme_matcha.png")
        self.assertEqual(appearance["chat_background"]["url"], "/static/theme_matcha.png")
        self.assertEqual(
            [theme["name"] for theme in appearance["themes"]],
            ["恋人", "抹茶", "雾港", "丁香"],
        )
        self.assertEqual(self.client.get("/api/appearance").get_json()["theme"], "dreamscape")

        invalid = self.client.post("/api/appearance", json={"theme": "unknown"})
        self.assertEqual(invalid.status_code, 400)

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
