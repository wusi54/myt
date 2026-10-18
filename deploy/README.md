# myt live 部署（MediaMTX）

生产环境一键部署 MediaMTX（Docker + host 网络），支持 RTSP 推流 + WebRTC 播放，带推/拉认证。

## 目录结构

```
deploy/
├── mediamtx.yml       # 生产配置（认证/公网 IP 用环境变量 ${VAR} 注入）
├── docker-compose.yml # host 网络模式，无需端口映射
├── .env.example       # 环境变量模板（复制为 .env 填写，.env 不入库）
├── deploy.sh          # 一键部署脚本
└── README.md
```

## 部署步骤（在 live 服务器上执行）

```bash
# 1. 拉取代码
git clone https://github.com/wusi54/myt.git && cd myt/deploy

# 2. 准备环境变量
cp .env.example .env
vi .env     # 填写 PUBLIC_IP / PUBLISH_USER / PUBLISH_PASS / READ_USER / READ_PASS

# 3. 部署（docker compose 需已安装）
chmod +x deploy.sh && ./deploy.sh

# 4. 防火墙放行（ufw 示例）
sudo ufw allow 8554/tcp    # RTSP
sudo ufw allow 8889/tcp    # WebRTC HTTP
sudo ufw allow 8189/udp    # WebRTC ICE（必须）
sudo ufw allow 8890/udp    # SRT（可选）
```

## 关键配置说明

| 项 | 值 | 说明 |
|---|---|---|
| 监听地址 | `:8554 / :8889 / :8189` | host 网络下绑定服务器全部网卡 |
| ICE 候选 | `webrtcAdditionalHosts` | 服务器直连公网：自动检测，可留空；NAT/负载均衡后：必须填浏览器看到的那层公网 IP |
| 推流认证 | `publishUser/publishPass` | Agent 推流 URL 带认证 |
| 拉流认证 | `readUser/readPass` | 浏览器页面 `?user=&pass=` |

## 验证

```bash
# 流在线检查（推流保持时）
ffprobe -rtsp_transport tcp -i rtsp://<PUBLIC_IP>:8554/dev_xxx

# 浏览器播放
# http://<PUBLIC_IP>:8889/dev_xxx/?user=view_user&pass=view_pass
```

## Agent（云手机端）推流

```
-relay 'rtsp://push_user:push_pass@<PUBLIC_IP>:8554/dev_<设备标识>' -transport tcp
```

- `-transport tcp`：生产推荐 TCP 推流（穿透 NAT/防火墙，稳定）；UDP 需额外放行 `8000-8001/udp` 并做来源 IP 白名单（裸 RTP 不受 RTSP 认证保护）
- 设备标识规则与 relay 现有逻辑一致：`dev_` + adb 地址转义

## 安全提醒

- MediaMTX **无内置 TURN**：浏览器所在网络封 UDP 时 WebRTC 无法播放，需另部署 TURN（如 coturn）并配置 `webrtcICEServers2`
- 生产务必修改 `.env` 中的默认密码
