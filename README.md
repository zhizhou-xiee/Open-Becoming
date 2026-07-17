# Open-Becoming

Open-Becoming 是 Becoming 的公开源码版本：一个支持多角色单聊、群聊、长期记忆、动态、共读、MCP 工具和定时唤醒的移动端优先聊天 PWA。

公开版使用中性标识 `user` 与 `char1`–`char6`，不包含原项目的私人姓名、人设、数据库、记忆、上传文件、部署地址或 Git 历史。六个示例角色都可以在界面中编辑人设与模型。

> 本项目采用 PolyForm Noncommercial 1.0.0，只允许非商业用途。因为包含非商业限制，它是“源码可用”项目，不属于 OSI 定义的开源软件。

## 功能

- 六个相互隔离的角色槽，支持 OpenRouter 与 Anthropic
- 单聊、群聊、引用、搜索、图片、虚拟转账与工具动作
- Markdown + YAML frontmatter 长期记忆和可选语义向量
- 动态、共读、定时发帖与欲望驱动的主动消息
- 自定义 MCP 连接、主题、头像、背景、人设、模型和用量上限
- SQLite 本地持久化，适合单实例个人部署

## 快速开始

需要 Python 3.11 或更高版本。

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

编辑 `.env` 后把变量载入当前终端，再启动应用：

```bash
set -a
. ./.env
set +a
python app.py
```

打开 `http://127.0.0.1:5000`，使用 `APP_PASSWORD` 登录。

最少需要配置：

```dotenv
APP_PASSWORD=replace-with-a-strong-password
FLASK_SECRET_KEY=replace-with-a-long-random-value
OPENROUTER_API_KEY=replace-with-your-openrouter-key
ANTHROPIC_API_KEY=replace-with-your-anthropic-key
```

`OPENROUTER_API_KEY` 供 `char1`–`char4`、`char6` 和默认摘要流程使用；`ANTHROPIC_API_KEY` 供 `char5` 使用。不需要某个提供商时，可以在代码或界面中调整相应角色。未设置 `APP_PASSWORD` 时，登录接口会保持关闭。

## 配置角色

默认模型可分别用 `MODEL_CHAR1`–`MODEL_CHAR6` 覆盖，摘要模型使用 `SUMMARY_MODEL`。角色名称、人设、模型、头像和用量上限也可以在界面中修改。

公开版的 `user` 和 `charN` 是持久化标识。建议只修改显示名称；如果直接修改这些 ID，需要同步更新后端、前端、测试和现有数据库。

调度器默认使用 UTC。可把 `SCHEDULER_TIMEZONE` 设置为有效的 IANA 时区，例如 `Asia/Shanghai` 或 `America/New_York`。

## 数据与记忆

- 默认数据库：`becoming.db`
- 默认记忆目录：数据库同级的 `becoming_memory/`
- 默认图片上传目录：`static/uploads/chat_images/`
- 自定义数据库：`DB_PATH`
- 自定义记忆目录：`BECOMING_MEMORY_DIR`

数据库、记忆和上传目录都已加入 `.gitignore`。生产部署时请把数据库和记忆目录放在同一个持久化磁盘，并定期备份。

记忆打标默认可复用 OpenRouter，也支持 DeepSeek 直连；语义向量可选用 Gemini。完整变量及安全说明见 [.env.example](.env.example) 和 [PRIVACY.md](PRIVACY.md)。

## 自定义素材

公开版保留中性命名的示例头像。原仓库中来源不明的网络表情包没有进入公开版，`static/stickers/placeholder.svg` 是可替换的占位图。添加素材前，请确认你拥有公开分发权，并在需要时更新 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试使用临时数据库和假凭据，不会调用外部模型服务。

## 安全与隐私

- 不要提交 `.env`、数据库、记忆、上传内容、MCP token 或真实对话。
- 对话、图片和记忆元数据可能发送给你配置的模型提供商或 MCP 服务。
- 前端会从 Google Fonts 加载字体和 Material Symbols；如需完全离线，请自行托管这些资源。
- 这是为单用户、单实例场景设计的项目；公开部署前请配置强密码、稳定的 `FLASK_SECRET_KEY`、HTTPS 和持久化存储。

详见 [PRIVACY.md](PRIVACY.md) 与 [SECURITY.md](SECURITY.md)。

## 参与贡献

提交改动前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。请勿在 issue、日志、截图、测试夹具或提交历史中包含个人信息和真实密钥。

## 许可

项目代码和项目自有素材采用 [PolyForm Noncommercial License 1.0.0](LICENSE)。允许为非商业目的使用、修改和分发；商业用途不在许可范围内。

第三方组件保留各自许可，见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
