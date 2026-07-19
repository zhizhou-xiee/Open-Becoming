# 移动端扩展接口

Open-Becoming 不内置任何厂商推送 SDK，也不会默认读取手机数据或申请麦克风权限。项目提供默认关闭的移动扩展接口，供 Android/iOS 伴侣应用、自动化工具或自托管服务实现。网页端另有默认关闭的可插拔语音收发，不依赖伴侣应用。

## 1. 消息推送：签名 webhook

主动消息落库后，服务端可以向一个由部署者控制的 webhook 发送最小化事件。伴侣服务负责把事件转换成 APNs、FCM、本地通知或其他平台通知。

```dotenv
MOBILE_PUSH_ENABLED=true
MOBILE_PUSH_WEBHOOK_URL=https://companion.example.com/becoming/events
MOBILE_PUSH_WEBHOOK_SECRET=replace-with-at-least-16-random-characters
MOBILE_PUSH_TIMEOUT=5
```

当前会为欲望系统主动私信、定时起床汇总和催睡消息发送推送，普通聊天回复不会重复推送。事件不会包含对话历史、模型密钥或 MCP 凭据，只包含角色 ID、显示名、消息 ID、不超过 240 字符的预览和来源。

请求头：

```text
Content-Type: application/json
X-Becoming-Event: message.created
X-Becoming-Timestamp: 1712345678
X-Becoming-Signature: sha256=<hex digest>
```

签名内容为：

```text
HMAC_SHA256(secret, timestamp + "." + raw_request_body)
```

接收端必须先校验时间戳和签名，再把消息交给 APNs/FCM；建议拒绝与当前时间相差超过五分钟的请求，防止重放。

## 2. 原生播放器扩展：MCP

Open-Becoming 现在自带网页“一起听”房间。下面的接口只用于把 Apple Music、Spotify、系统媒体控制等原生手机能力接进来，不是内置房间的前置条件。原生播放和对应会员账号留在手机或用户自己的桥接服务中；桥接服务通过现有的“自定义 MCP”面板接入，并建议提供以下稳定工具名：

- `music_get_state`：读取曲目、播放进度、播放状态和当前共听会话。
- `music_start_session`：由用户明确发起或加入共听会话。
- `music_control`：执行 `play`、`pause`、`seek`、`next` 等受限动作。

推荐的状态返回格式：

```json
{
  "session_id": "local-session-id",
  "track": {"id": "provider-track-id", "title": "Track", "artist": "Artist"},
  "position_ms": 42000,
  "playing": true,
  "updated_at": "2026-07-17T09:30:00Z"
}
```

主项目不保存这类原生播放器令牌，也不替桥接服务指定供应商。桥接服务应自己处理版权、地区、会员和播放权限。网页内置房间使用的可选网易云 `MUSIC_U` 是另一项服务端环境变量，详见部署与隐私文档。

## 3. AI 查询手机：只读 MCP

手机查询使用 `phone_search` 工具，建议参数为：

```json
{
  "query": "明天下午的牙医预约",
  "scope": "calendar",
  "limit": 10,
  "reason": "User 在当前对话中明确要求查询日历"
}
```

建议仅允许以下只读范围：`contacts`、`calendar`、`reminders`、`notifications`、`photos`、`files`。不要把删除、发送消息、拨号、付款、修改联系人等写操作塞进 `phone_search`。

安全边界必须在手机端桥接服务中强制执行：

1. 首次访问和新增范围时显示系统权限与应用内确认。
2. 每次调用校验允许的角色、数据范围和结果条数；不能只相信模型填写的 `reason`。
3. 默认返回最少字段和少量匹配结果，不返回整本通讯录、完整相册或全部通知历史。
4. 记录可供用户查看和撤销的访问日志，并提供一键断开 MCP 的方式。
5. 密码、验证码、支付信息、健康数据和精确位置默认拒绝。

## 4. 语音：网页内置接口与可选伴侣端

网页端已经提供「说说喵°语音收发」，可直接连接 OpenAI-compatible 或自定义 HTTP TTS/STT。开启 TTS 后，模型会得到 `send_voice` 工具；生成的音频通过登录态接口播放，文字稿随消息进入历史与记忆。iPhone 可以在 HTTPS 页面录音，服务器先转写，再把文字送入原有聊天流程。配置、限额和 HTTP 协议见 [语音收发教程](VOICE.md)。

如果需要系统级后台能力、离线模型、蓝牙设备或不经过网页的本地录音，也可以继续通过原生伴侣端或自定义 MCP 扩展。建议稳定工具名为：

- `voice_get_capabilities`：返回支持的语言、音色、输入时长、格式和是否能够离线处理。
- `voice_transcribe`：把用户明确录制的一段音频转成文字。
- `voice_synthesize`：把一条角色回复合成为可播放音频。

建议使用不透明的 `audio_ref` 传递音频，不把 base64 音频、永久文件路径或真实录音地址塞进模型上下文。`audio_ref` 可以是伴侣端本地 ID，也可以是桥接服务生成的短期签名引用。

转写请求示例：

```json
{
  "audio_ref": "local-recording:8f31",
  "mime_type": "audio/mp4",
  "language": "zh-CN",
  "max_seconds": 60,
  "reason": "User 点击并按住语音输入按钮"
}
```

推荐返回：

```json
{
  "transcript": "今晚一起读下一章吧",
  "language": "zh-CN",
  "confidence": 0.96,
  "duration_ms": 2840
}
```

合成请求示例：

```json
{
  "text": "好呀，我在共读室等你。",
  "character_id": "char1",
  "voice_id": "user-selected-voice",
  "format": "audio/mp4",
  "reason": "User 点击了播放语音"
}
```

推荐返回：

```json
{
  "audio_ref": "temporary-audio:42",
  "mime_type": "audio/mp4",
  "duration_ms": 3100,
  "expires_at": "2026-07-17T10:00:00Z"
}
```

伴侣端建议流程是“用户明确开始录音 → 本地或桥接服务转写 → 让用户确认文字 → 交给现有文字聊天接口”；回复侧则是“收到文字回复 → 用户点击播放 → 合成并播放”。默认不要自动开启麦克风、后台常驻录音或收到消息就自动外放。

桥接端还应限制录音长度和 MIME 类型、删除临时音频、避免把音色克隆默认开放，并在新增麦克风、语音识别或蓝牙权限时重新向用户说明用途。网页内置语音不捆绑真实供应商、音色或账号；部署者应只使用有权使用的音色，并向使用者说明这是 AI 生成语音。

## 能力发现

登录后请求 `GET /api/mobile/extensions` 可得到不含 URL、密钥或 token 的公开能力清单。它用于让后续移动端客户端判断主程序支持的协议版本；其中音乐的 `built_in: false` 专指原生手机播放器桥接仍需外部伴侣服务，并不表示网页一起听房间不存在。

这些接口是稳定的扩展边界，不代表仓库已经获得 Android/iOS 系统权限。网页录音仍需用户授予浏览器麦克风权限；任何原生客户端仍需遵守对应平台的权限、后台运行和商店审核规则。
