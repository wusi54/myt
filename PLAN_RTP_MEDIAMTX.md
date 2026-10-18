# RTP + MediaMTX 方案 —— 执行规划

## 架构

```
云手机 Agent（Go）
  scrcpy 采集 H.264（tunnel_forward 本地 socket）
  → 解析 annexb NAL
  → gortsplib H264→RTP encoder
  → RTSP RECORD 推流到 MediaMTX

MediaMTX（MIT 开源）
  接收 RTSP 推流
  → 自动转 WebRTC（WHEP 协议）
  → 浏览器拉流

浏览器
  WebRTC WHEP 拉流
  → <video> 播放（全平台兼容，含 iOS）
```

## 执行步骤

### 第 1 步：Agent 改造（relay.go → rtsp.go）
- 删除 gorilla/websocket 依赖
- 新增 gortsplib 依赖
- 新增 rtsp.go：RTSP 推流（StartRecording + rtpEnc.Encode + WritePacketRTP）
- scrcpy.go 不变（采集逻辑已验证）
- main.go 调整主循环（scrcpy 读帧 → 解析 NAL → RTP 推流）

### 第 2 步：docker-compose.yml 加 MediaMTX
- 新增 mediamtx 服务（8554 RTSP 推流入，8889 WebRTC 拉流出）
- relay 保留（Agent 部署 + 控制指令，不再做画面分发）

### 第 3 步：前端改造（index.html + screen.html）
- WallPlayer 改用 WebRTC WHEP 拉 MediaMTX 流
- <video> 播放（替代 canvas + WebCodecs）
- 触控仍走 HTTP /api/screen/touch（不变）

### 第 4 步：relay 简化（screen_relay.py）
- 移除 WebSocket 分发逻辑（/stream, _dispatch）
- 保留 Agent 部署逻辑（_deploy_agent）
- 保留控制 HTTP（:9001）
- relay 变成"Agent 部署器 + 控制中继"，不再做画面分发

### 第 5 步：编译 + 提交 + 部署文档

## 关键技术点

### NAL 解析
scrcpy 输出 annexb 格式（含 0x000001/0x00000001 start code 的 NAL）。
gortsplib 的 rtpEnc.Encode() 输入是 `[][]byte`（NAL 数组，不含 start code）。
需要从 annexb 解析出 NAL：找 start code，切分 NAL，去掉 start code。

### RTP 时间戳
gortsplib 示例用 `forma.ClockRate()`（90000）计算 PTS。
Agent 可以用 `time.Since(start)` 换算成 90000 时钟基。

### MediaMTX 流路径
每个设备一个流：`rtsp://mediamtx:8554/device_{adb_addr_转义}`
浏览器拉流：`http://mediamtx:8889/device_{adb_addr_转义}`（WHEP）

### MediaMTX WHEP
浏览器 POST SDP offer 到 MediaMTX 的 `/device_xxx/whip` 或 `/device_xxx/whep`，
MediaMTX 返回 SDP answer，浏览器建立 WebRTC 连接。
