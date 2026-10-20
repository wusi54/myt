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
	"strings"
	"syscall"
	"time"
)

// ScrcpyServer 负责在设备端启动并管理 scrcpy-server（tunnel_forward 模式）的生命周期。
type ScrcpyServer struct {
	JarPath          string
	MaxSize          int
	Bitrate          int
	KeyFrameInterval int
	cmd              *exec.Cmd
	stopCh           chan struct{} // 主动停止时关闭，通知 watcher 忽略此次退出
	exitCh           chan error    // watcher 在进程意外退出时上报退出错误
	doneCh           chan struct{} // 会话结束(Stop)时关闭，通知看门狗 goroutine 退出
}

// Start 启动 scrcpy-server 进程（app_process，tunnel_forward 模式），并启动进程监控。
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
	if s.KeyFrameInterval < 0 {
		s.KeyFrameInterval = 0
	}

	// 清理旧实例（幂等）；等待旧 server 释放 abstract socket "scrcpy"，
	// 否则新实例会因 Address already in use 退出，表现为群控窗黑屏/重连。
	exec.Command("pkill", "-f", "com.genymobile.scrcpy.Server").Run()
	for i := 0; i < 20; i++ {
		out, _ := exec.Command("pgrep", "-f", "com.genymobile.scrcpy.Server").Output()
		if len(strings.TrimSpace(string(out))) == 0 {
			break
		}
		exec.Command("pkill", "-9", "-f", "com.genymobile.scrcpy.Server").Run()
		time.Sleep(100 * time.Millisecond)
	}
	time.Sleep(1 * time.Second)

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
		// 关键帧间隔：MediaMTX/WHEP 新观众加入时必须尽快拿到 IDR，
		// 否则只能等到设备画面变化触发关键帧，群控窗会出现长时间黑屏。
		// 注意：该 Xiaomi 编码器会把 1/2 秒钳成 10 秒 GOP，只有 0（每帧 IDR）生效。
		"video_codec_options=i-frame-interval:int=" + strconv.Itoa(s.KeyFrameInterval),
	}

	cmd := exec.Command(args[0], args[1:]...)
	// 继承父进程环境变量，追加 CLASSPATH
	cmd.Env = append(os.Environ(), "CLASSPATH="+s.JarPath)
	// scrcpy-server 输出（含 Java 崩溃栈）写入日志，便于排查为何没起来
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

	// 进程监控：另起 goroutine 等待进程退出，意外退出时通过 exitCh 上报。
	// 之前没有这一步，scrcpy-server 崩溃后 agent 只能在连 socket 超时(20s)后才发现，
	// 且拿不到退出原因（Java 异常/退出码），盲重启循环。
	s.stopCh = make(chan struct{})
	s.exitCh = make(chan error, 1)
	s.doneCh = make(chan struct{})
	go func() {
		err := cmd.Wait()
		select {
		case <-s.stopCh:
			return // 主动停止，忽略
		case s.exitCh <- err:
		}
	}()

	// 立即校验进程是否真活着（cmd.Start 成功只代表被 spawn，不代表没瞬间退出）
	if !s.alive() {
		return fmt.Errorf("scrcpy-server 启动后立即退出（jar 或设备兼容问题），详见 scrcpy-agent.log")
	}
	log.Printf("[agent] scrcpy-server 已启动 (pid=%d), tunnel_forward=true", cmd.Process.Pid)
	return nil
}

// alive 判断 scrcpy-server 进程是否仍存活。
func (s *ScrcpyServer) alive() bool {
	if s.cmd == nil || s.cmd.Process == nil {
		return false
	}
	return s.cmd.Process.Signal(syscall.Signal(0)) == nil
}

// Connect 连接 scrcpy-server 监听的本地 abstract socket "scrcpy"。
// 握手会读取 dummy、codec、首个视频尺寸元数据；返回连接从第一条 12 字节帧头开始。
// 连接等待期间若 scrcpy-server 进程意外退出，立即报错返回（不再干等 20s）。
func (s *ScrcpyServer) Connect() (net.Conn, error) {
	// abstract namespace socket: 名字前面加 \x00
	addr := &net.UnixAddr{Name: "\x00scrcpy", Net: "unix"}
	var conn net.Conn
	var err error
	// 重试连接（scrcpy-server 启动需要时间）
	for i := 0; i < 100; i++ {
		conn, err = net.DialUnix("unix", nil, addr)
		if err == nil {
			break
		}
		// 连接等待期间若 scrcpy-server 进程已退出，立即返回，避免盲等满 20s
		select {
		case e := <-s.exitCh:
			return nil, fmt.Errorf("scrcpy-server 进程在连接前退出: %v（详见 scrcpy-agent.log）", e)
		default:
		}
		time.Sleep(200 * time.Millisecond)
	}
	if err != nil {
		return nil, fmt.Errorf("连接 abstract socket 失败: %w", err)
	}

	conn.SetReadDeadline(time.Now().Add(3 * time.Second))
	// 读 dummy byte（send_dummy_byte=true 时，server 会先发 1 字节 0）。
	dummy := make([]byte, 1)
	if _, err := io.ReadFull(conn, dummy); err != nil {
		conn.Close()
		return nil, fmt.Errorf("读 dummy byte 失败: %w", err)
	}
	// 读 stream meta（send_stream_meta=true 时，server 会发 4 字节 codec id）
	streamMeta := make([]byte, 4)
	if _, err := io.ReadFull(conn, streamMeta); err != nil {
		conn.Close()
		return nil, fmt.Errorf("读 stream meta 失败: %w", err)
	}
	if string(streamMeta) != "h264" {
		conn.Close()
		return nil, fmt.Errorf("不支持的 scrcpy codec: %q", streamMeta)
	}

	// 该 scrcpy 5.0.1 fork 在 codec id 后发送 12 字节视频尺寸记录：
	// 8 字节字段的最高位为标记、低位是宽度，随后 4 字节是高度。
	// 它看起来像帧头，但不是 H.264 帧；若留给 pumpFrame，会导致错位。
	videoMeta := make([]byte, 12)
	if _, err := io.ReadFull(conn, videoMeta); err != nil {
		conn.Close()
		return nil, fmt.Errorf("读视频尺寸元数据失败: %w", err)
	}
	metaValue := binary.BigEndian.Uint64(videoMeta[:8])
	width := uint32(metaValue & 0x7fffffff)
	height := binary.BigEndian.Uint32(videoMeta[8:12])
	if metaValue&(uint64(1)<<63) == 0 || width == 0 || width > 16384 || height == 0 || height > 16384 {
		conn.Close()
		return nil, fmt.Errorf("无效的视频尺寸元数据: value=%#x width=%d height=%d", metaValue, width, height)
	}
	conn.SetReadDeadline(time.Time{})
	log.Printf("[agent] 已连接 scrcpy-server 本地 socket (codec=%s, size=%dx%d)", streamMeta, width, height)
	return conn, nil
}

// Stop 停止 scrcpy-server。
func (s *ScrcpyServer) Stop() {
	if s.doneCh != nil {
		select {
		case <-s.doneCh: // 已关闭，避免重复 close panic
		default:
			close(s.doneCh)
		}
	}
	if s.stopCh != nil {
		select {
		case <-s.stopCh: // 已关闭，避免重复 close panic
		default:
			close(s.stopCh)
		}
	}
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
