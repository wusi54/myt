# 独立 relay 公共服务 —— 部署操作手册

> 方向 A：scrcpy 采集 + WebSocket 出站（画面绕开 adb 隧道）
> 目标：把 relay 独立部署，NAT 映射到 `113.105.100.58:8111`，云手机 Agent 出站直连。

---

## 〇、前置条件核对

部署前请逐项确认：

- [ ] relay 服务器能 `adb connect 27.185.79.181:5001`（能连云手机，用于部署 Agent）
- [ ] relay 服务器已安装 Docker + Docker Compose
- [ ] 网关/防火墙能做端口映射：外网 `113.105.100.58:8111` → 内网 relay `9000`
- [ ] 云手机已 root（adb shell 进去是 `#` 提示符）

---

## 一、准备部署文件

以下文件必须齐备（当前仓库已包含）：

| 文件 | 说明 |
|---|---|
| `screen_relay.py` | relay 主程序（含 /agent 端点 + Agent 部署逻辑） |
| `auth.py`、`db.py` | relay 依赖 |
| `scrcpy/scrcpy-server` | scrcpy 采集核心（Agent 会推送到设备） |
| `agent/myt-agent-arm64` | 设备端 Agent 二进制（Go 编译） |
| `docker-compose.relay.yml` | 独立 relay 部署配置 |
| `Dockerfile`、`docker-entrypoint.sh`、`requirements.txt` | 镜像构建 |

---

## 二、配置环境变量

创建 `.env` 文件（与 docker-compose.relay.yml 同目录）：

```bash
# .env
# 1. relay 的 NAT 映射地址（Agent 出站连这里，务必是外网可达地址）
AGENT_RELAY_HOST=ws://113.105.100.58:8111

# 2. Agent 上行流认证密钥（生产务必设置，Agent 和 relay 共用）
AGENT_TOKEN=change-this-to-a-strong-token

# 3. JWT 密钥（需与 myt 平台一致，用于浏览器端鉴权）
SECRET_KEY=change-me-in-production
```

> ⚠️ 说明：
> - `AGENT_RELAY_HOST` 是**外网地址**（云手机要能访问到它），不是内网地址
> - `AGENT_TOKEN` 必须设置，否则 Agent 上行流无认证，任何人都能推流
> - `SECRET_KEY` 要和现有 myt 平台的 `SECRET_KEY` 完全一致

---

## 三、启动 relay 服务

```bash
# 在项目目录执行
docker compose -f docker-compose.relay.yml up -d --build
```

查看日志确认启动成功：

```bash
docker compose -f docker-compose.relay.yml logs -f relay
```

预期日志：

```
[relay][multithread 2.2.1-scrcpy] Starting on ws://0.0.0.0:9000
[relay] control http listening on :9001
```

---

## 四、NAT 映射

在网关/防火墙做端口映射：

```
外网 113.105.100.58:8111  →  内网 relay服务器:9000
```

验证映射生效（在云手机或外网机器上测试）：

```bash
# 云手机内执行，测试能否连到 relay（应能建立连接）
curl -v http://113.105.100.58:8111/ 2>&1 | head -20
```

> 如果 `AGENT_RELAY_HOST` 配的是 `ws://`（明文），NAT 映射后 Agent 走 ws。生产建议配 `wss://`（需 relay 前加 TLS 反代，见附录）。

---

## 五、接入 myt 平台（浏览器端）

浏览器仍走 myt 平台（9111 nginx），需要 myt 平台的 nginx 把 `/ws/screen` 反代到**独立 relay**（而不是原 docker 内网 relay）。

修改 myt 平台的 `nginx.conf`：

```nginx
# 原：反代到 docker 内网 relay
# proxy_pass http://$relay_up/stream$is_args$args;

# 改：反代到独立 relay 服务
location /ws/screen {
    proxy_pass http://113.105.100.58:8111/stream$is_args$args;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $connection_upgrade;
    proxy_set_header Host $host;
    proxy_set_header Cookie $http_cookie;
    proxy_read_timeout 86400s;
    proxy_send_timeout 86400s;
}
```

重启 myt 平台 nginx：

```bash
docker compose restart nginx
```

---

## 六、验证流程

### 6.1 浏览器打开投屏（触发 Agent 部署）

1. 浏览器登录 myt 平台 → 容器管理 → 点某容器的「投屏」或打开群控窗
2. 首次会触发 relay 自动部署 Agent（adb push + 启动），稍等几秒

### 6.2 确认 Agent 已部署到设备

```bash
adb -s 27.185.79.181:5001 shell "ps -A | grep myt-agent"
# 应看到 myt-agent 进程
```

### 6.3 确认 Agent 出站连上 relay

```bash
docker compose -f docker-compose.relay.yml logs -f relay | grep "agent connected"
# 应看到 [relay][agent] agent connected adb_addr=xxx
```

### 6.4 确认画面正常

浏览器应能看到投屏画面。如果黑屏，看 Agent 日志：

```bash
adb -s 27.185.79.181:5001 shell "cat /data/local/tmp/myt-agent.log"
```

---

## 七、常见问题排查

### 7.1 Agent 未部署成功

```bash
# 看 relay 日志里 Agent 部署是否报错
docker compose -f docker-compose.relay.yml logs relay | grep "\[agent\]"
```

### 7.2 Agent 连不上 relay

- 确认 `AGENT_RELAY_HOST` 是外网可达地址
- 确认 NAT 映射 8111 → 9000 生效
- 确认 `AGENT_TOKEN` 在 relay 和 Agent 一致（Agent 的 token 由 relay 部署时写入）

### 7.3 画面黑屏

```bash
# 看 Agent 日志，确认 scrcpy-server 是否启动成功
adb -s 27.185.79.181:5001 shell "cat /data/local/tmp/myt-agent.log"
adb -s 27.185.79.181:5001 shell "cat /data/local/tmp/scrcpy-agent.log"
```

### 7.4 回退到传统模式

若方向 A 有问题，可临时回退（改 `.env` 后重启）：

```bash
# .env 加一行
RELAY_USE_AGENT=0
docker compose -f docker-compose.relay.yml up -d
```

---

## 附录：生产环境 TLS 加固（可选）

Agent 出站目前是 `ws://` 明文。生产环境建议：

1. relay 前加一个 TLS 反代（nginx/caddy），对外 `wss://`
2. `AGENT_RELAY_HOST` 改成 `wss://113.105.100.58:8111`
3. Agent 代码已支持 wss（gorilla/websocket 自动处理）

示例（caddy）：

```
113.105.100.58:8111 {
    reverse_proxy 127.0.0.1:9000
}
```
