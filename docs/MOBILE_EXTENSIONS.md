# 移动端扩展接口

Open-Becoming 不内置任何厂商推送 SDK，也不会默认读取手机数据。项目提供三个默认关闭的扩展接口，供 Android/iOS 伴侣应用、自动化工具或自托管服务实现。

## 1. 消息推送：签名 webhook

主动消息落库后，服务端可以向一个由部署者控制的 webhook 发送最小化事件。伴侣服务负责把事件转换成 APNs、FCM、本地通知或其他平台通知。

```dotenv
MOBILE_PUSH_ENABLED=true
MOBILE_PUSH_WEBHOOK_URL=https://companion.example.com/becoming/events
MOBILE_PUSH_WEBHOOK_SECRET=replace-with-at-least-16-random-characters
MOBILE_PUSH_TIMEOUT=5
```

当前只在欲望系统产生主动私信时发送推送，普通聊天回复不会重复推送。事件不会包含对话历史、模型密钥或 MCP 凭据，只包含角色 ID、显示名、消息 ID、不超过 240 字符的预览和来源。

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

## 2. 一起听音乐：MCP

音乐播放和会员账号都留在手机或用户自己的桥接服务中。桥接服务通过现有的“自定义 MCP”面板接入，并建议提供以下稳定工具名：

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

主项目不保存音乐账号令牌，也不指定 Spotify、Apple Music 或其他供应商。桥接服务应自己处理版权、地区、会员和播放权限。

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

## 能力发现

登录后请求 `GET /api/mobile/extensions` 可得到不含 URL、密钥或 token 的公开能力清单。它用于让后续移动端客户端判断主程序支持的协议版本；其中 `built_in: false` 表示仍需外部伴侣服务实现。

这三个接口是稳定的扩展边界，不代表仓库已经获得 Android/iOS 系统权限。任何原生客户端仍需遵守对应平台的权限、后台运行和商店审核规则。
