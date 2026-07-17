# 部署 Open-Becoming

Open-Becoming 不依赖某一家云平台。只要主机能运行 Python 3.11+ 或 Docker、提供 HTTPS，并允许挂载持久化目录，就可以部署。

## 通用规则

- 设置强随机 `APP_PASSWORD` 与固定的 `FLASK_SECRET_KEY`。
- 将 `DB_PATH`、`BECOMING_MEMORY_DIR`、`UPLOAD_ROOT` 都放到持久化磁盘；容器模板默认统一放在 `/data`。
- 应用监听平台提供的 `PORT`，默认端口为 `8000`。
- 内置定时任务运行在 Web 进程里。默认只开一个 Gunicorn worker，避免主动消息、记忆衰减和定时动态重复执行。
- 如果平台需要横向扩容，所有额外 Web 副本都应设置 `SCHEDULER_ENABLED=false`，并只保留一个启用调度器的实例。SQLite 仍更适合单实例个人部署。
- 生产环境应在平台入口或反向代理处启用 HTTPS。

## Docker

构建：

```bash
docker build -t open-becoming .
```

运行：

```bash
docker run --name open-becoming \
  -p 8000:8000 \
  --env-file .env \
  -v open-becoming-data:/data \
  open-becoming
```

数据库、长期记忆和聊天上传都会进入同一个持久化卷。升级镜像前请备份该卷。

## 普通 Linux / VPS

建立虚拟环境并安装 `requirements.txt` 后，将 `.env` 中的变量注入进程，再运行：

```bash
gunicorn -c gunicorn.conf.py wsgi:app
```

可以交给 systemd、Supervisor 或其他进程管理器守护，并用 Nginx、Caddy 或云负载均衡器终止 HTTPS。工作目录必须是仓库根目录；运行账户需要对三个持久化路径有读写权限。

## 主流 PaaS

支持 Procfile 的平台会自动使用仓库中的启动命令。其他平台可手动填写：

```text
gunicorn -c gunicorn.conf.py wsgi:app
```

同时配置环境变量和持久化磁盘。Render、Fly.io、Koyeb、Railway 等平台的界面名称不同，但不需要平台专属代码。没有持久化磁盘的免费实例会在重建后丢失数据库、记忆和上传内容，不适合作为长期使用环境。

## 多进程注意事项

`WEB_CONCURRENCY=1` 是默认值，也是内置 SQLite + APScheduler 模式的推荐设置。若只想运行无后台任务的 Web 副本，可以设置：

```dotenv
SCHEDULER_ENABLED=false
WEB_CONCURRENCY=2
```

这只解决后台任务重复问题，不会把 SQLite 变成适合多机共享的数据库。需要多实例部署时，应另行实现共享数据库和独立任务进程。
