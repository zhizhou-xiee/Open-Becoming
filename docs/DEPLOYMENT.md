# Open-Becoming 部署教程

这份教程按“完全第一次部署也能照着做”来写。Open-Becoming 不依赖某一家云平台：只要环境能运行 Python 3.11+ 或 Docker、能提供 HTTPS，并有不会在重启后消失的持久化目录，就可以部署。

## 先判断：它适合谁？

### 很适合

- 想给自己部署一个长期使用的多角色聊天空间。
- 能接受对话会发送给自己选择的模型服务商。
- 愿意保管 API key、备份数据，并偶尔更新服务。
- 使用单台服务器、NAS、家中小主机或单个云实例。
- 用于个人学习、研究、创作或其他符合非商业许可的场景。

### 可以用，但需要额外改造

- 两三位熟人共用：当前只有一个登录密码，没有独立账户和数据隔离。
- 多实例或高可用部署：默认 SQLite 和进程内定时器按单实例设计。
- 完全离线：需要自托管字体/图标，并换成本地模型和本地记忆服务。
- 对数据驻留、审计、删除时限有硬性要求：需要自行审核模型、MCP、日志和备份链路。

### 不适合直接拿来用

- 面向公众注册的多租户聊天平台。
- 商业服务、收费产品或公司内部商业用途；项目许可证限制商业使用。
- 把它当成企业身份系统、支付系统、医疗或其他高风险系统。
- 不想管理服务器、密钥、备份，也不接受任何外部模型服务的数据政策。

## 选择一条部署路径

| 你的情况 | 建议路径 | 难度 | 适合长期运行 |
|---|---|---:|---:|
| 只想先看看 | 本机 Python | 低 | 否 |
| Windows 用户 | Docker Desktop / WSL | 低 | 可以 |
| NAS、迷你主机、普通云主机 | Docker Compose | 低 | 是 |
| 熟悉 Linux 服务管理 | Python + systemd + 反向代理 | 中 | 是 |
| 使用支持源码部署的云平台 | 通用 PaaS | 中 | 是，前提是有持久化磁盘 |

如果不确定，优先选 Docker Compose。它会自动把数据库、记忆和上传文件放进同一个持久卷，同时避免本机路径写错。

## 开始前：准备代码和运行环境

你需要准备：

- Open-Becoming 源码；
- 一种运行方式：Docker Desktop / Docker Engine，或 Python 3.11 及以上版本；
- 至少一个受支持模型服务商的 API key；
- 长期部署时需要一个域名或其他 HTTPS 入口，以及可备份的持久化磁盘。

使用 Git 下载源码：

```bash
git clone https://github.com/zhizhou-xiee/Open-Becoming.git
cd Open-Becoming
```

不熟悉 Git 也可以在 GitHub 页面选择 **Code → Download ZIP**，解压后在终端进入解压目录。Windows PowerShell 示例：

```powershell
Set-Location C:\Open-Becoming
```

如果目录名或上级目录包含空格，要用引号包住完整路径：

```powershell
Set-Location "C:\My Apps\Open-Becoming"
```

开始前确认当前目录里能看到 `app.py`、`requirements.txt` 和 `.env.example`。如果只能看到另一个同名文件夹，说明 ZIP 多套了一层，需要再进入一层。

按自己选择的路径检查运行环境；不需要同时安装 Python 和 Docker：

```bash
git --version
python3 --version
docker version
docker compose version
```

Windows 的 Python 命令也可能叫 `py`。只要你选择的那条路径对应命令正常即可；例如选择 Docker Compose 时，不要求本机 `python3` 可用。

## 密钥在哪里？浏览器能看到吗？

模型密钥只由服务端从环境变量读取：

```dotenv
OPENROUTER_API_KEY=replace-with-your-openrouter-key
ANTHROPIC_API_KEY=replace-with-your-anthropic-key
```

它们不会写进 HTML、JavaScript、本地数据库，也不会通过角色配置接口返回给浏览器。浏览器只向你部署的 Open-Becoming 请求 `/api/chat`；再由服务器携带 API key 请求模型服务商。

需要区分三类配置：

| 类型 | 示例 | 应该放在哪里 | 浏览器是否需要知道 |
|---|---|---|---:|
| 服务器秘密 | 模型 API key、网易云 `MUSIC_U`、`FLASK_SECRET_KEY`、推送签名密钥 | 环境变量或平台 Secret | 否 |
| 登录凭据 | `APP_PASSWORD` | 服务端环境变量；登录时由用户输入 | 仅登录当次 |
| 普通设置 | 模型名称、显示昵称、主题、额度提醒 | 环境变量或应用设置 | 可以 |

自定义 MCP token 是一个例外：它由用户在管理界面输入并保存在 SQLite 中，接口不会再返回 token 原文。因此数据库和备份同样要按密钥文件保护。

网易云 `MUSIC_U` 也是登录凭据。它只应配置为服务端的 `NETEASE_MUSIC_U` Secret，不会返回浏览器；如果泄露，应退出相关网易云会话并更换 Cookie。

### 密钥安全底线

1. 不要把真实 key 写进 `app.py`、`static/`、截图、Issue 或聊天记录。
2. 不要提交 `.env`。仓库已忽略它，但仍建议运行 `git check-ignore .env` 确认。
3. 不要给环境变量加 `VITE_`、`NEXT_PUBLIC_`、`PUBLIC_` 等前端公开前缀。
4. PaaS 上使用平台的 Secret / Environment Variables 页面，不要把 key 写进构建参数或 Dockerfile。
5. 怀疑泄露时先去服务商撤销旧 key，再生成新 key；仅删除 Git 提交不代表旧 key 已失效。

## 第 0 步：准备配置

复制示例配置：

```bash
cp .env.example .env
```

Windows PowerShell：

```powershell
Copy-Item .env.example .env
```

至少修改下面四项：

```dotenv
APP_PASSWORD=replace-with-a-strong-password
FLASK_SECRET_KEY=replace-with-a-long-random-value
OPENROUTER_API_KEY=replace-with-your-openrouter-key
ANTHROPIC_API_KEY=replace-with-your-anthropic-key
```

- `APP_PASSWORD`：你每次登录网页时使用的密码。
- `FLASK_SECRET_KEY`：用于保护登录会话；第一次生成后应保持不变，否则现有登录会失效。
- 两个模型 key：默认六个角色会同时用到 OpenRouter 与 Anthropic。少配一个不会泄密，但对应角色或摘要功能可能无法回复。

生成随机值（macOS / Linux / Windows 都可）：

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Windows 如果命令是 `py`：

```powershell
py -c "import secrets; print(secrets.token_urlsafe(48))"
```

把输出分别用作强密码或 `FLASK_SECRET_KEY`。不要照抄教程里的占位文字。

### 可选：接入一起听的个人歌单

不配置账号也可以尝试搜索公开曲库。若要读取自己的网易云歌单，当前实现需要部署者本人网页登录后的 `MUSIC_U` Cookie：

1. 在自己的浏览器登录网易云音乐网页版。
2. 打开浏览器开发者工具，在 Application / Storage / 存储的 Cookies 中找到 `MUSIC_U`；不同浏览器的菜单名称略有差异。
3. 只复制它的值，不要连同 `MUSIC_U=` 前缀一起复制。
4. 在 `.env` 或平台 Secret 页面配置：

```dotenv
NETEASE_MUSIC_U=replace-with-your-own-music-u-cookie
NETEASE_BITRATE=320000
```

不要把这个值发给他人、写进前端或提交到 Git。账号退出、Cookie 轮换或服务商策略变化后可能失效；更新 Secret 并重启应用即可。只播放自己有权访问的内容，并遵守音乐服务条款与所在地法律。

### 可选：睡眠时区与外部催睡页面

睡眠节律默认跟随 `SCHEDULER_TIMEZONE`。需要单独设置时使用有效的 IANA 时区：

```dotenv
SLEEP_TIMEZONE=Asia/Shanghai
```

`/api/sleep/nudge` 默认整个关闭。如果部署者确实有自己的静态控制页，可以显式开启，并把允许的来源配置成逗号分隔的完整 Origin；不要使用 `*`：

```dotenv
SLEEP_NUDGE_ENABLED=true
CORS_ALLOW_ORIGINS=https://your-static-page.example
```

跨域白名单只作用于这个催睡接口，不会开放聊天、记忆或音乐 API。外部页面会提交 `APP_PASSWORD`，因此双方都必须使用 HTTPS，并建议在反向代理处增加速率限制。

## 路径一：本机 Python 试跑

适合确认界面、模型和配置是否正常，不建议直接暴露到互联网。

### macOS / Linux

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
set -a
. ./.env
set +a
python app.py
```

打开 `http://127.0.0.1:5000`。

### Windows PowerShell

Gunicorn 不原生支持 Windows。试跑可以直接使用 Flask；长期运行建议改用 Docker Desktop 或 WSL。

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

项目不会自动读取 `.env`，PowerShell 试跑时请把必要变量载入当前终端：

```powershell
$env:APP_PASSWORD = "your-login-password"
$env:FLASK_SECRET_KEY = "your-random-session-secret"
$env:OPENROUTER_API_KEY = "your-openrouter-key"
$env:ANTHROPIC_API_KEY = "your-anthropic-key"
python app.py
```

关闭这个 PowerShell 窗口后，这些临时变量会消失。不要把含真实 key 的脚本提交到仓库。

## 路径二：Docker Compose（推荐）

安装 Docker 后，在仓库根目录准备好 `.env`，然后运行：

```bash
docker compose up -d --build
```

打开 `http://127.0.0.1:8000`。查看运行状态：

```bash
docker compose ps
docker compose logs -f --tail=100
```

停止但保留数据：

```bash
docker compose down
```

再次启动：

```bash
docker compose up -d
```

`compose.yaml` 会强制使用以下容器内路径，避免本机 `.env` 覆盖镜像的持久化默认值：

```text
/data/becoming.db
/data/memory
/data/uploads
/data/music_library
```

它们都存进 Compose 管理的 `open-becoming-data` 卷（实际 Docker 卷名可能带项目名前缀）。不要执行 `docker compose down -v`，除非你明确要删除全部运行数据。

如果本机 `8000` 已被占用，在 `.env` 增加：

```dotenv
BECOMING_HOST_PORT=18000
```

然后访问 `http://127.0.0.1:18000`。这个变量只控制主机端口，容器内部仍使用 `8000`。

## 路径三：单条 Docker 命令

不使用 Compose 时，必须显式覆盖四个数据路径；否则 `.env` 里的本机默认路径可能把数据写进容器临时层。

```bash
docker build -t open-becoming .
docker run -d \
  --name open-becoming \
  --restart unless-stopped \
  -p 8000:8000 \
  --env-file .env \
  -e PORT=8000 \
  -e DB_PATH=/data/becoming.db \
  -e BECOMING_MEMORY_DIR=/data/memory \
  -e UPLOAD_ROOT=/data/uploads \
  -e MUSIC_LIBRARY_DIR=/data/music_library \
  -e WEB_CONCURRENCY=1 \
  -e GUNICORN_THREADS=8 \
  -v open-becoming-data:/data \
  open-becoming
```

## 路径四：Linux / VPS 原生部署

下面假设代码位于 `/opt/open-becoming`，数据位于 `/var/lib/open-becoming`。可以换成其他绝对路径，但不要把数据放进每次发布都会被替换的代码目录。

### 1. 建立目录与运行账户

```bash
sudo useradd --system --home /opt/open-becoming --shell /usr/sbin/nologin becoming
sudo mkdir -p /opt/open-becoming /var/lib/open-becoming/uploads /var/lib/open-becoming/memory /var/lib/open-becoming/music_library
sudo chown -R becoming:becoming /opt/open-becoming /var/lib/open-becoming
```

把仓库代码放进 `/opt/open-becoming` 后安装依赖：

```bash
cd /opt/open-becoming
sudo -u becoming python3 -m venv .venv
sudo -u becoming .venv/bin/python -m pip install -r requirements.txt
```

### 2. 建立仅服务器可读的环境文件

例如 `/etc/open-becoming.env`：

```dotenv
APP_PASSWORD=replace-with-a-strong-password
FLASK_SECRET_KEY=replace-with-a-long-random-value
OPENROUTER_API_KEY=replace-with-your-openrouter-key
ANTHROPIC_API_KEY=replace-with-your-anthropic-key
DB_PATH=/var/lib/open-becoming/becoming.db
BECOMING_MEMORY_DIR=/var/lib/open-becoming/memory
UPLOAD_ROOT=/var/lib/open-becoming/uploads
MUSIC_LIBRARY_DIR=/var/lib/open-becoming/music_library
PORT=8000
WEB_CONCURRENCY=1
GUNICORN_THREADS=8
SCHEDULER_ENABLED=true
SCHEDULER_TIMEZONE=UTC
# SLEEP_TIMEZONE=UTC
```

保护权限：

```bash
sudo chown root:becoming /etc/open-becoming.env
sudo chmod 640 /etc/open-becoming.env
```

### 3. 配置 systemd

创建 `/etc/systemd/system/open-becoming.service`：

```ini
[Unit]
Description=Open-Becoming
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=becoming
Group=becoming
WorkingDirectory=/opt/open-becoming
EnvironmentFile=/etc/open-becoming.env
ExecStart=/opt/open-becoming/.venv/bin/gunicorn -c gunicorn.conf.py wsgi:app
Restart=on-failure
RestartSec=5
PrivateTmp=true
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

启用并检查：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now open-becoming
sudo systemctl status open-becoming
sudo journalctl -u open-becoming -n 100 --no-pager
```

### 4. 反向代理与 HTTPS

Gunicorn 只监听应用端口。生产环境应让 Caddy、Nginx 或云负载均衡器负责域名、HTTPS 和公网入口。反向代理目标是：

```text
http://127.0.0.1:8000
```

Caddy 的最小示意：

```caddyfile
chat.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

把域名换成自己的，并按反向代理的官方文档配置 DNS、证书、防火墙和可信代理头。不要把 8000 端口直接开放给整个互联网。

## 路径五：通用 PaaS / 源码托管平台

不同平台会把同一组选项叫作 Environment Variables、Secrets、Persistent Disk、Volume、Start Command 或 Service。对应关系如下：

| 平台选项 | 填什么 |
|---|---|
| Runtime | Python 3.11+ |
| Build command | `pip install -r requirements.txt` |
| Start command | `gunicorn -c gunicorn.conf.py wsgi:app` |
| Health check | `GET /` |
| Port | 使用平台注入的 `PORT` |
| Worker count | `WEB_CONCURRENCY=1` |
| Threads | `GUNICORN_THREADS=8` |
| Persistent mount | 例如 `/data` |
| Database path | `/data/becoming.db` |
| Memory path | `/data/memory` |
| Upload path | `/data/uploads` |
| Music path | `/data/music_library` |

把 `.env.example` 中的变量逐项录入平台控制台，但不要上传含真实值的 `.env`。支持 Procfile 的平台可直接使用仓库中的 `Procfile`；不支持也只需填写相同的启动命令。

部署成功但重启后聊天或本地音乐消失，几乎总是因为没有挂载持久化磁盘，或四个路径没有指向挂载点。只有代码文件持久化是不够的。

## NAS、家庭服务器与内网访问

NAS 或迷你主机优先使用 Docker Compose。只在可信内网使用时，可以通过主机局域网地址访问；需要外网访问时，优先使用带身份保护的反向代理或可信隧道，并保持应用自身密码与 HTTPS。不要通过路由器直接裸露容器端口。

## 持久化目录怎么选？

四个路径可以放在同一磁盘，也可以分开：

| 变量 | 内容 | 是否必须备份 |
|---|---|---:|
| `DB_PATH` | 对话、摘要、设置、动态、MCP 配置 | 是 |
| `BECOMING_MEMORY_DIR` | 长期记忆 Markdown 与向量 | 是 |
| `UPLOAD_ROOT` | 用户上传的聊天图片 | 视需要 |
| `MUSIC_LIBRARY_DIR` | 本地音乐、封面和修复后的资源 | 视需要；使用本地曲库时应备份 |

裸机部署推荐绝对路径。运行账户必须能创建、读取、修改这些文件；反向代理账户不需要读取它们。不要把数据库放在只读目录、临时目录、镜像层或会被自动清理的缓存目录。

## 调度器和进程数

主动消息、定时动态、记忆打标和衰减由进程内调度器执行。默认值：

```dotenv
SCHEDULER_ENABLED=true
WEB_CONCURRENCY=1
GUNICORN_THREADS=8
```

一个实例只运行一个 worker，最不容易重复发送主动消息。若平台额外启动 Web 副本，额外副本必须设为：

```dotenv
SCHEDULER_ENABLED=false
```

只关闭重复调度仍不能让 SQLite 适合多机并发。真正的多实例部署需要改成共享数据库，并把后台任务拆成独立 worker；这不属于默认支持范围。

## 首次上线后的检查清单

1. 打开首页，确认未登录时 API 返回 401，而不是直接展示聊天内容。
2. 用 `APP_PASSWORD` 登录，分别测试 OpenRouter 与 Anthropic 角色。
3. 发一张测试图片，重启服务后确认仍能显示。
4. 重启服务后确认聊天、长期记忆和主题仍存在。
5. 检查时区、睡眠节律和主动消息时间；需要时设置 `SCHEDULER_TIMEZONE` 与 `SLEEP_TIMEZONE`。
6. 检查浏览器 Network：聊天请求应发往你的 `/api/chat`，不应由浏览器直接请求模型服务商。
7. 在仓库根目录运行 `rg "OPENROUTER_API_KEY|ANTHROPIC_API_KEY|NETEASE_MUSIC_U" static`；正常应没有结果。
8. 查看服务日志，确认平台没有记录请求正文、密码或环境变量。
9. 配置备份并实际恢复一次；“有备份文件”不等于“能恢复”。

## 备份与恢复

### 裸机

先短暂停止应用，避免复制到写入一半的 SQLite 文件：

```bash
sudo systemctl stop open-becoming
sudo tar -czf open-becoming-backup.tar.gz -C /var/lib open-becoming
sudo systemctl start open-becoming
```

恢复时停止应用，把数据库、记忆和上传目录还原到原路径，修正属主后再启动。

### Docker Compose

查看卷名：

```bash
docker volume ls
```

不同 Docker 环境的卷备份工具不同。最稳妥的原则是：停止应用，对 Compose 创建的 `open-becoming-data` 卷做完整快照；恢复时把卷挂回 `/data`。实际卷名以 `docker volume ls` 为准，可能带项目名前缀。不要只备份镜像，镜像里没有运行数据。

## 更新版本

更新前先备份。源码部署：

```bash
git pull --ff-only
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
sudo systemctl restart open-becoming
```

Docker Compose：

```bash
git pull --ff-only
docker compose build --pull
docker compose up -d
docker compose logs --tail=100
```

不要在没有备份时同时升级代码、修改存储路径和迁移记忆后端；一次只改一类，出问题更容易回退。

## 常见问题

### 页面能打开，但角色说“还没配置 API key”

- 环境变量只在启动时读取，改完后要重启服务。
- 检查变量名拼写和是否多了引号/空格。
- Docker 用户检查变量是否真的进入容器，而不是只存在宿主机终端。
- 默认角色同时使用两个服务商；确认当前角色对应的 key 已配置。

### 重启后数据全没了

检查 `DB_PATH`、`BECOMING_MEMORY_DIR`、`UPLOAD_ROOT`、`MUSIC_LIBRARY_DIR` 是否都指向持久化挂载点。Docker 用户应优先使用仓库的 `compose.yaml`。

### 主动消息发了两遍

通常是启动了多个启用调度器的 worker 或副本。保持 `WEB_CONCURRENCY=1`，额外副本设置 `SCHEDULER_ENABLED=false`。

### 上传图片后显示 404

确认 `UPLOAD_ROOT` 存在、运行账户可写，并且升级时没有只迁移数据库却漏掉上传目录。

### 登录后立刻掉线

确保 `FLASK_SECRET_KEY` 是固定值，且没有在每次部署时重新生成。若经过反向代理，确认 HTTPS 和代理头配置正确。

### Windows 上 Gunicorn 启动失败

这是运行环境差异。Windows 本机试跑使用 `python app.py`；长期部署使用 Docker Desktop、WSL 或 Linux 服务器。
