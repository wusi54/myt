# Agent 构建归档 - 20261019-scropty-socket-fix

## 构建信息
- 构建时间: 2026-10-19
- Git TAG: fix/scropty-socket-handshake-20261019
- Git Commit: 0b477e3
- 构建目标: agent/main.go, agent/scrcpy.go, agent/rtsp.go, agent/embed.go

## 构建环境
- Go 版本: go1.26.4 windows/amd64
- 交叉编译: GOOS=android, GOARCH=arm64, CGO_ENABLED=0
- 构建命令:
  ```
  cd agent
  $env:GOOS="android"
  $env:GOARCH="arm64"
  $env:CGO_ENABLED="0"
  go build -ldflags "-s -w" -o myt-agent-arm64 .
  ```

## 生成物
- agent/builds/archive/20261019-scropty-socket-fix/myt-agent-arm64
- 文件大小: 7,799,080 字节 (~7.4 MB)
- 文件类型: ELF 64-bit LSB executable, ARM aarch64

## 修复内容
1. **scrcpy.go Connect()**: 增加读取 12 字节视频尺寸元数据，确保帧边界对齐
   - scrcpy-server 5.0.1 fork 发送 dummy(1B) + stream meta(4B 'h264') + video size meta(12B)
   - 之前只读取了 4 字节 stream meta，导致 pumpFrame 从错位偏移开始解析帧头，size 字段读错，最终 EOF
2. **scrcpy.go Start()**: pkill 旧 scrcpy-server 后增加 500ms 等待，避免 abstract socket 占用冲突
3. **main.go runOnce()**: 恢复完整推流逻辑；读帧错误后立即重启 scrcpy-server，不再等待 5 秒

## 部署方式
```bash
adb -s <设备> push agent/builds/archive/20261019-scropty-socket-fix/myt-agent-arm64 /data/local/tmp/myt-agent
adb -s <设备> shell "chmod 755 /data/local/tmp/myt-agent"
adb -s <设备> shell "nohup /data/local/tmp/myt-agent -relay 'rtsp://<MediaMTX_IP>:8554/dev_<设备标识>' -max-size 720 -bitrate 1000000 -transport tcp >/data/local/tmp/myt-agent.log 2>&1 </dev/null &"
```

## 部署验证
- 设备: Xiaomi 2509FPN0BC (Android 14), emulator-5554
- 推流地址: rtsp://192.168.10.9:8554/dev_emulator_5554
- MediaMTX: Docker 服务 myt-mediamtx (host network mode)
- 浏览器 WHEP: http://<MediaMTX_IP>:8889/dev_emulator_5554/whep

## 已验证
- Agent 启动 -> scrcpy-server 启动 -> 连接 socket -> RTSP 推流 MediaMTX -> RTP encoder 初始化 (SPS=18, PPS=6)
- 运行 40+ 秒无读帧错误，无自动重启
