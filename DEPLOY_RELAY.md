# 云 phone 管理平台 —— 部署操作手册（方向 A：Agent 上行流）

> 架构：relay + web + nginx 集合部署。画面数据由设备端 Agent 经 WebSocket 出站到 relay，
> 彻底绕开不稳定的 adb 隧道。

---

## 一、架构总览

```
┌─ 云手机（root）─────────────────────────────────┐
│  Agent（myt-agent-arm64，常驻）                  │
│    scrcpy 采集(tunnel_forward 本地 socket)       │
│    → WebSocket 主动出站                          │
└──────────────────┬──────────────────────────────┘
                   │ ws://113.105.100.58:9000/agent?adb_addr=xxx&token=xxx
                   ▼
┌─ 服务器（docker compose 集合部署）──────────────┐
│  relay（9000 端口，NAT 已映射）                  │
│    /agent  收 Agent 帧 → Stream                  │
│    /stream 发浏览器（经 nginx 反代）             │
│  nginx（9111 端口，NAT 已映射）                  │
│    /ws/screen → relay /stream                    │
│  web（Flask，9111）                              │
└──────────────────┬──────────────────────────────┘
                   │ 浏览器经 9111（https）
                   ▼
              浏览器（群控窗/单窗）
```

**端口约定**：
- `9000`：relay WS 端口（Agent 出站连这里，已做 NAT 映射）
- `9111`：nginx HTTP/HTTPS 端口（浏览器访问，已做 NAT 映射）

---

## 二、前置条件

- [ ] 服务器已安装 Docker + Docker Compose
- [ ] 服务器已安装 adb，且能 `adb connect 27.185.79.181:5001`（连云手机）
- [ ] NAT 映射已做：`113.105.100.58:9000` → 服务器 9000；`113.105.100.58:9111` → 服务器 9111
- [ ] 云手机已 root

---

## 三、拉取代码

```bash
git clone https://github.com/wusi54/myt.git
cd myt
git checkout feat/agent-websocket-relay
```

确认关键文件：

```bash
ls -la agent/myt-agent-arm64     # Agent 二进制（5MB）
ls -la scrcpy/scrcpy-server      # scrcpy 采集核心
ls -la docker-compose.yml        # 部署配置
```

---

## 四、配置环境变量

创建 `.env` 文件（与 docker-compose.yml 同目录）：

```bash
cat > .env << 'EOF'
# Agent 出站地址（云手机要能访问到的公网地址 + 9000 端口）
AGENT_RELAY_HOST=ws://113.105.100.58:9000

# Agent 上行流认证密钥（生产必设）
AGENT_TOKEN=你的强随机密钥
EOF
```

> ⚠️ 说明：
> - `AGENT_RELAY_HOST` 必须是**云手机能访问到的公网地址**，不是内网地址
> - `AGENT_TOKEN` 必设，否则 Agent 上行流无认证
> - `SECRET_KEY` 已在 docker-compose.yml 里写死为 `change-me-in-production`（web 和 relay 一致，无需额外配置）

---

## 五、构建并启动

```bash
docker compose up -d --build
```

查看日志：

```bash
docker compose logs -f relay
# 预期看到：
#   [relay][multithread 2.2.1-scrcpy] Starting on ws://0.0.0.0:9000
#   [relay] control http listening on :9001
```

---

## 六、验证完整链路

### 6.1 触发 Agent 部署

浏览器访问 `https://113.105.100.58:9111` → 登录 → 容器管理 → 点某容器的「投屏」。

首次会触发 relay 自动部署 Agent（adb push + 启动），等 5~10 秒。

### 6.2 确认 Agent 已部署到设备

```bash
adb -s 27.185.79.181:5001 shell "ps -A | grep myt-agent"
# 应看到 myt-agent 进程
```

### 6.3 确认 Agent 连上 relay

```bash
docker compose logs -f relay | grep "agent connected"
# 应看到：[relay][agent] agent connected adb_addr=27.185.79.181:5001
```

### 6.4 确认画面正常

浏览器应看到投屏画面。

---

## 七、排查指南

| 现象 | 排查命令 | 常见原因 |
|---|---|---|
| 无 `agent connected` | `adb shell "cat /data/local/tmp/myt-agent.log"` | `AGENT_RELAY_HOST` 填错/不可达 |
| Agent 起了但连不上 | 上面日志看 ws 报错 | NAT 映射没生效 / token 不匹配 |
| 画面黑屏 | `adb shell "cat /data/local/tmp/scrcpy-agent.log"` | scrcpy-server 启动失败 |
| 浏览器 401 | relay 日志 `unauthorized` | SECRET_KEY 不一致（应是默认值） |

---

## 八、回退方案

若方向 A 有问题，临时回退传统 adb reverse 模式：

```bash
# 在 .env 加一行
echo "RELAY_USE_AGENT=0" >> .env
docker compose up -d relay
```

---

## 九、已知限制（本次实现范围）

- ✅ 画面拉流（Agent 上行 + 浏览器下行）
- ⚠️ 触控/按键仍走 adb（`/api/screen/touch`），未并入 Agent 通道
- ⚠️ Agent 断线后需 relay 重新部署（未做 Agent 侧自动重连）
- ⚠️ iOS 浏览器兼容需配合 `static/myt-h264.js`（已提交，未接入页面）

---

## 十、常用运维命令

```bash
# 查看 relay 日志
docker compose logs -f relay

# 重启 relay（改代码后）
docker compose restart relay

# 查看 Agent 状态（在设备上）
adb -s 27.185.79.181:5001 shell "ps -A | grep myt-agent"

# 手动停止 Agent（在设备上）
adb -s 27.185.79.181:5001 shell "pkill -f myt-agent"

# 查看 Agent 日志（在设备上）
adb -s 27.185.79.181:5001 shell "cat /data/local/tmp/myt-agent.log"
```
