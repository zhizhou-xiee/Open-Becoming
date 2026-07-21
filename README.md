# Open-Becoming

Open-Becoming 是 Becoming 的公开源码版本：一个支持多角色单聊、群聊、长期记忆、动态、共读、MCP 工具和定时唤醒的移动端优先聊天 PWA。

公开版使用中性标识 `user` 与 `char1`–`char6`，不包含原项目的私人姓名、人设、数据库、记忆、上传文件、部署地址或 Git 历史。六个示例角色都可以在界面中编辑人设与模型。

> **第一次部署前请先给自己和六个角色起名。** 在 `.env` 或部署平台的 Variables / Secrets 中修改 `USER_DISPLAY_NAME` 与 `NAME_CHAR1`–`NAME_CHAR6`。聊天里的长按备注只是本地聊天称呼，不会替代记忆、额度、群聊摘要、语音等功能读取的全局名字。

> 本项目采用 PolyForm Noncommercial 1.0.0，只允许非商业用途。因为包含非商业限制，它是“源码可用”项目，不属于 OSI 定义的开源软件。

## 功能

- 六个相互隔离的角色槽，支持 OpenRouter、Anthropic、DeepSeek 和自定义 OpenAI-compatible 服务
- 单聊、群聊、引用、搜索、图片、虚拟转账与有上限的多轮工具调用
- 可替换的长期记忆后端；内置 Markdown + YAML 记忆和可选语义向量
- 动态、共读、睡眠节律、定时发帖与欲望驱动的主动消息
- 内置“一起听”房间：网易云歌单与搜索、歌词上下文、角色点歌和房间聊天
- 默认关闭的 Android/iOS 推送、可插拔语音收发、原生播放器与手机只读查询扩展接口
- 自定义 MCP 连接、天气动效、主题、头像、背景、人设、模型和用量上限
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

按仓库默认角色配置，最少需要：

```dotenv
APP_PASSWORD=replace-with-a-strong-password
FLASK_SECRET_KEY=replace-with-a-long-random-value
OPENROUTER_API_KEY=replace-with-your-openrouter-key
ANTHROPIC_API_KEY=replace-with-your-anthropic-key
# 可选：直连 DeepSeek 官方
# DEEPSEEK_API_KEY=replace-with-your-deepseek-key
# 可选：为长期记忆生成 Gemini 语义向量
# OMBRE_EMBEDDING_API_KEY=replace-with-your-google-ai-studio-key
```

默认配置中，`OPENROUTER_API_KEY` 供 `char1`–`char4`、`char6` 和摘要流程使用，`ANTHROPIC_API_KEY` 供 `char5` 使用。也可以配置 `DEEPSEEK_API_KEY`，再在「换毛期°人设编辑」里给每个角色和摘要流程切换 OpenRouter、Anthropic 官方、DeepSeek 官方或自定义 OpenAI-compatible 服务。密钥只从后端环境变量读取，前端只显示是否已配置。未设置 `APP_PASSWORD` 时，登录接口会保持关闭。

如果打算全员只用 DeepSeek 官方或自定义线路，不必同时购买 OpenRouter 与 Anthropic；删掉未使用的占位 Key，并同步修改角色和摘要的供应商、模型即可。不要把 `replace-with-...` 原样留在真实环境变量里，它会被识别成一个已填写但无效的凭据。

记忆的 `valence` / `arousal` 情感坐标会依次读取专用记忆 Key、`DEEPSEEK_API_KEY`，最后才回退到 `OPENROUTER_API_KEY`。Gemini 语义 embedding 是另一条可选链路，需要单独配置 `OMBRE_EMBEDDING_API_KEY`（也兼容 `GEMINI_API_KEY`）；模型和地址不填时默认使用 `gemini-embedding-2` 与 Google Generative Language API。完整变量与数据流见[部署教程的“可选记忆增强”](docs/DEPLOYMENT.md#可选记忆增强deepseek-情感打标与-gemini-embedding)。

## 配置角色

默认供应商可用 `PROVIDER_CHAR1`–`PROVIDER_CHAR6` 覆盖，默认模型可分别用 `MODEL_CHAR1`–`MODEL_CHAR6` 覆盖；摘要使用 `SUMMARY_PROVIDER` 和 `SUMMARY_MODEL`。角色的人设、供应商、模型、头像和用量上限也可以在界面中修改，界面保存的选择会持久化到数据库。

公开版的 `user` 和 `char1`–`char6` 是数据库、记忆域和工具路由使用的持久化 ID，请保留不动。真正需要个性化的是：

```dotenv
USER_DISPLAY_NAME=你的名字或昵称
NAME_CHAR1=第一位角色的名字
NAME_CHAR2=第二位角色的名字
# 继续填写到 NAME_CHAR6
```

这些变量会统一用于聊天、群聊、朋友圈、记忆、额度、摘要、一起听和语音设置等区域。随后在「更多 → 换毛期°人设编辑」为每位角色写入对应人设；默认人设也会自动带入上述名字。若要从源码层修改默认值，可编辑 `app.py` 中的 `USER_DISPLAY_NAME`、`CHARACTER_DISPLAY_NAMES` 与 `CHAR1_PERSONA`–`CHAR6_PERSONA`，但仍不要把内部 ID 做全局搜索替换。

长按聊天名称得到的是仅用于聊天界面的备注，适合临时昵称，不是全局角色名。完整的首次命名步骤和检查清单见 [部署教程：先把公开占位名换掉](docs/DEPLOYMENT.md#第-0-步先把公开占位名换掉)。

调度器默认使用 UTC。可把 `SCHEDULER_TIMEZONE` 设置为有效的 IANA 时区，例如 `Asia/Shanghai` 或 `America/New_York`；睡眠节律默认跟随它，也可以单独设置 `SLEEP_TIMEZONE`。

## 数据与记忆

- 默认数据库：`becoming.db`
- 默认记忆目录：数据库同级的 `becoming_memory/`
- 默认图片上传目录：`static/uploads/chat_images/`
- 默认本地音乐目录：数据库同级的 `music_library/`
- 自定义数据库：`DB_PATH`
- 自定义记忆目录：`BECOMING_MEMORY_DIR`
- 自定义图片目录：`UPLOAD_ROOT`
- 自定义本地音乐目录：`MUSIC_LIBRARY_DIR`

数据库、记忆、图片和音乐目录都已加入 `.gitignore`。生产部署时请把它们放在持久化磁盘，并定期备份。

聊天原图最大 25 MB。浏览器会先尝试压缩，后端仍会校验并把长边限制到 2048 像素、压到约 2.5 MB 后再保存；模型视觉与聊天历史复用同一份处理后图片，避免手机照片长期把磁盘和请求体撑大。

记忆打标默认可复用 OpenRouter，也支持 DeepSeek 直连；语义向量可选用 Gemini。默认内置记忆、关闭记忆和自定义外部记忆库的接入方式见 [长期记忆后端](docs/MEMORY_BACKENDS.md)。完整变量及安全说明见 [.env.example](.env.example) 和 [PRIVACY.md](PRIVACY.md)。

## 部署

项目提供通用 WSGI 入口、Gunicorn 配置、Procfile、Dockerfile 与 Docker Compose，不依赖 Railway。还不确定是否适合自己、API key 应放哪里，或想按本机、Windows、NAS、VPS、PaaS 等路径一步步部署，请从 [适宜人群与部署教程](docs/DEPLOYMENT.md) 开始。

## 一起听音乐

“猫窝 → 一起听”可以在不配置音乐账号时搜索公开曲库；个人歌单和受账号权限控制的在线播放需要部署者自己的网易云音乐 `MUSIC_U` Cookie：

```dotenv
NETEASE_MUSIC_U=replace-with-your-own-music-u-cookie
NETEASE_BITRATE=320000
```

`MUSIC_U` 只从服务器环境变量读取，不写入数据库，也不会返回浏览器。当前实现使用 `128000`、`192000`、`320000` 或 `999000` 码率；账号失效后更新 Secret 并重启服务即可。仅使用自己有权访问的账号和内容，遵守音乐服务条款与所在地法律，不要借此分发音频。

房间会为每位参与角色带入祂自己的少量近期私聊，不会串到其他角色；结束一段有来有往的共听后，角色会用自己的模型判断是否值得写入长期记忆。默认至少需要 4 条房间消息才进入判断，可用 `MUSIC_MEMORY_MIN_MESSAGES` 调整，闲聊仍可由角色选择跳过。

## 移动端扩展

公开版为 Android/iOS 伴侣应用预留了签名消息推送 webhook；原生手机播放器和 AI 查询手机通过伴侣端或用户自建的 MCP 服务接入。这些原生扩展默认全部关闭，不包含任何真实设备权限或厂商账号；网页内置的一起听房间不依赖伴侣端。协议、示例和安全边界见 [移动端扩展接口](docs/MOBILE_EXTENSIONS.md)。

网页端另有独立的「说说喵°语音收发」：支持 OpenAI-compatible TTS/STT 与自定义 HTTP 服务、每个角色单独音色、试听、录音转写，以及字数、每日次数和估算费用上限。它默认关闭；开启 TTS 后，模型会得到 `send_voice` 工具并自行决定何时发送 AI 语音，文字稿仍进入对话历史和记忆链路。配置步骤、接口格式和 iPhone 注意事项见 [语音收发教程](docs/VOICE.md)。

## 自定义素材

公开版保留原项目的主题背景，并使用中性命名的示例头像。内置表情包来自原创作者 **呆猫八条**（小红书号：`9861276720`），原创 IP 禁止商用；转载或继续分发时请明确标注原创出处。详细说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

`static/stickers/placeholder.svg` 是项目自有的备用占位图。添加或替换其他素材前，请确认你拥有公开分发权，并在需要时更新第三方素材说明。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试使用临时数据库和假凭据，不会调用外部模型服务。

## 安全与隐私

- 不要提交 `.env`、数据库、记忆、上传内容、MCP token 或真实对话。
- 对话、图片、语音文字稿和记忆元数据可能发送给你配置的模型、语音或 MCP 服务。
- 开启移动推送后，主动消息的角色名和短预览会发送到你配置的 webhook。
- 前端会从 Google Fonts 加载字体和 Material Symbols；如需完全离线，请自行托管这些资源。
- 这是为单用户、单实例场景设计的项目；公开部署前请配置强密码、稳定的 `FLASK_SECRET_KEY`、HTTPS 和持久化存储。

详见 [PRIVACY.md](PRIVACY.md) 与 [SECURITY.md](SECURITY.md)。

## 参与贡献

提交改动前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。请勿在 issue、日志、截图、测试夹具或提交历史中包含个人信息和真实密钥。

## 许可

项目代码和项目自有素材采用 [PolyForm Noncommercial License 1.0.0](LICENSE)。允许为非商业目的使用、修改和分发；商业用途不在许可范围内。第三方表情包不属于项目自有素材，也不由本项目许可重新授权。

第三方组件保留各自许可，见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
