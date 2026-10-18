package main

import (
	"encoding/binary"
	"fmt"
	"io"
	"log"
	"net"
	"os"
	"os/exec"
	"strconv"
	"syscall"
	"time"
)

// ScrcpyServer 负责在设备端启动 scrcpy-server（tunnel_forward 模式）。
//
// tunnel_forward=true 时，scrcpy-server 会监听本地 abstract socket（名字 "scrcpy"，
// 当 scid=-1 时），等待本地进程连接。这正是 Agent 绕开 adb 隧道的关键。
type ScrcpyServer struct {
	JarPath string
	MaxSize int
	Bitrate int
	cmd     *exec.Cmd
}

// Start 启动 scrcpy-server 进程（app_process，tunnel_forward 模式）。
func (s *ScrcpyServer) Start() error {
	if s.JarPath == "" {
		s.JarPath = "/data/local/tmp/scrcpy-server.jar"
	}
	// jar 缺失时直接报错，不去调 app_process 空转：
	// 设备会不定时清理 /data/local/tmp，jar 没了必须由 relay 重新部署时一并推送
	if _, err := os.Stat(s.JarPath); err != nil {
		return fmt.Errorf("scrcpy-server.jar 不存在(%s)，需由 relay 重新部署", s.JarPath)
	}
	if s.MaxSize <= 0 {
		s.MaxSize = 720
	}
	if s.Bitrate <= 0 {
		s.Bitrate = 1000000
	}

	// 清理旧实例（幂等）
	exec.Command("pkill", "-f", "com.genymobile.scrcpy.Server").Run()

	// 参数：scid=-1 让 socket 名为 "scrcpy"，tunnel_forward=true 让 server 监听
	args := []string{
		"/system/bin/app_process", "/",
		"com.genymobile.scrcpy.Server", "5.0.1",
		"scid=-1",
		"tunnel_forward=true",
		"log_level=info",
		"video=true",
		"audio=false",
		"control=false",
		"send_dummy_byte=true",
		"send_device_meta=false",
		"send_stream_meta=true",
		"send_frame_meta=true",
		"max_size=" + strconv.Itoa(s.MaxSize),
		"video_bit_rate=" + strconv.Itoa(s.Bitrate),
	}

	cmd := exec.Command(args[0], args[1:]...)
	// 关键：必须在继承父进程环境的基础上追加 CLASSPATH，
	// 否则 app_process 丢失 PATH 等环境变量导致启动失败
	cmd.Env = append(os.Environ(), "CLASSPATH="+s.JarPath)
	// 输出到日志文件，避免阻塞
	logf, err := openLogFile("/data/local/tmp/scrcpy-agent.log")
	if err == nil {
		cmd.Stdout = logf
		cmd.Stderr = logf
		defer logf.Close()
	}
	if err := cmd.Start(); err != nil {
		return fmt.Errorf("app_process 启动失败: %w", err)
	}
	s.cmd = cmd
	log.Printf("[agent] scrcpy-server 已启动 (pid=%d), tunnel_forward=true", cmd.Process.Pid)
	return nil
}

func openLogFile(path string) (*os.File, error) {
	return os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0644)
}

// Connect 连接 scrcpy-server 监听的本地 abstract socket "scrcpy"。
// 返回一个连接，读它会得到 H.264 帧流（先 1 字节 dummy，再 12字节头+payload）。
func (s *ScrcpyServer) Connect() (net.Conn, error) {
	// abstract namespace socket: 名字前面加 \x00
	addr := &net.UnixAddr{Name: "\x00scrcpy", Net: "unix"}
	var conn net.Conn
	var err error
	// 重试连接（scrcpy-server 启动需要时间）
	for i := 0; i < 50; i++ {
		conn, err = net.DialUnix("unix", nil, addr)
		if err == nil {
			break
		}
		time.Sleep(200 * time.Millisecond)
	}
	if err != nil {
		return nil, fmt.Errorf("连接 abstract socket 失败: %w", err)
	}

	// ⚠ 协议头必须完整消费，否则帧解析整体错位（表现为大量「异常帧 size=...」）。
	// 本 Agent 用 send_dummy_byte=true + send_stream_meta=true，socket 上的布局是：
	//   1B dummy + 4B codec id("h264") + 12B stream meta(宽/高) + 帧流
	// 这与 relay 传统 scrcpy 路径（codec id + stream meta）一致，只多 1 字节 dummy。
	conn.SetReadDeadline(time.Now().Add(5 * time.Second))

	// 1) dummy byte
	if _, err := io.ReadFull(conn, make([]byte, 1)); err != nil {
		conn.Close()
		return nil, fmt.Errorf("读 dummy byte 失败: %w", err)
	}
	// 2) codec id（4 字节）
	codecID := make([]byte, 4)
	if _, err := io.ReadFull(conn, codecID); err != nil {
		conn.Close()
		return nil, fmt.Errorf("读 codec id 失败: %w", err)
	}
	if string(codecID) != "h264" {
		conn.Close()
		return nil, fmt.Errorf("非预期 codec: %q（帧解析会错位）", string(codecID))
	}
	// 3) stream meta（12 字节：[4:8] 宽 / [8:12] 高）
	meta := make([]byte, 12)
	if _, err := io.ReadFull(conn, meta); err != nil {
		conn.Close()
		return nil, fmt.Errorf("读 stream meta 失败: %w", err)
	}
	w := binary.BigEndian.Uint32(meta[4:8])
	h := binary.BigEndian.Uint32(meta[8:12])

	conn.SetReadDeadline(time.Time{})
	log.Printf("[agent] 已连接 scrcpy-server 本地 socket，codec=%s 分辨率=%dx%d",
		string(codecID), w, h)
	return conn, nil
}

// Stop 停止 scrcpy-server。
func (s *ScrcpyServer) Stop() {
	if s.cmd != nil && s.cmd.Process != nil {
		s.cmd.Process.Signal(syscall.SIGTERM)
		time.Sleep(500 * time.Millisecond)
		s.cmd.Process.Kill()
	}
	exec.Command("pkill", "-f", "com.genymobile.scrcpy.Server").Run()
}
