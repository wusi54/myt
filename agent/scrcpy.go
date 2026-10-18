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
	// 自动释放嵌入的 scrcpy-server.jar（单包部署，无需单独推送）
	if err := ensureScrcpyJar(s.JarPath); err != nil {
		log.Printf("[agent] 释放 scrcpy-server.jar 失败: %v（尝试使用已有文件）", err)
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
	// 继承父进程环境变量，追加 CLASSPATH
	cmd.Env = append(os.Environ(), "CLASSPATH="+s.JarPath)
	// 输出到日志文件，避免阻塞
	logf, err := os.OpenFile("/data/local/tmp/scrcpy-agent.log", os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0644)
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

	// 读 dummy byte（send_dummy_byte=true 时，server 会先发 1 字节 0）
	dummy := make([]byte, 1)
	conn.SetReadDeadline(time.Now().Add(3 * time.Second))
	if _, err := conn.Read(dummy); err != nil {
		conn.Close()
		return nil, fmt.Errorf("读 dummy byte 失败: %w", err)
	}
	// 读 stream meta（send_stream_meta=true 时，server 会发 4 字节 codec id）
	streamMeta := make([]byte, 4)
	if _, err := conn.Read(streamMeta); err != nil {
		conn.Close()
		return nil, fmt.Errorf("读 stream meta 失败: %w", err)
	}
	conn.SetReadDeadline(time.Time{})
	log.Printf("[agent] 已连接 scrcpy-server 本地 socket，dummy+stream_meta 已读 (codec=%x)", streamMeta)
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

// ReadFrame 从 conn 读取一个 scrcpy 帧（12字节头 + payload）。
// 返回 (annexb payload, 是否 config 帧, error)。
func (s *ScrcpyServer) ReadFrame(conn net.Conn) ([]byte, bool, error) {
	// 读 12 字节头
	header := make([]byte, 12)
	if _, err := io.ReadFull(conn, header); err != nil {
		return nil, false, err
	}
	val := binary.BigEndian.Uint64(header[0:8])
	size := binary.BigEndian.Uint32(header[8:12])
	config := (val >> 63) & 1
	if size == 0 {
		return nil, config == 1, nil
	}
	if size > 8*1024*1024 {
		return nil, false, fmt.Errorf("帧 size 异常: %d", size)
	}
	payload := make([]byte, size)
	if _, err := io.ReadFull(conn, payload); err != nil {
		return nil, false, err
	}
	return payload, config == 1, nil
}