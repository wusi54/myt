"""多线程 + 队列 版投屏中继（dev_multithread_2.1.0）。

架构：
- 每个 adb_addr 起 1 个「生产者线程」跑 scrcpy-server（nohup 独立运行）并读 annexb H.264 帧，
  塞进一个有界 queue.Queue（背压：队列满则丢弃分片，避免内存爆）；
- 另起 1 个「分发线程」从队列取分片，用 asyncio.run_coroutine_threadsafe 把同一分片
  扇出给所有订阅该设备的客户端 WebSocket；
- 心跳、鉴权、token 过期剔除等都在各自线程里完成，事件循环只负责 WS 收发。

协议/端口/WS 路径(?adb_addr=) 与旧版完全一致。
"""
import os
import re
import time
import json
import asyncio
import hashlib
import threading
import queue
import subprocess
import socket
import random
import shlex
import http.server
import auth

__version__ = '2.2.1-scrcpy'

WS_PORT = int(os.environ.get('RELAY_PORT', '9000'))
ADB = os.environ.get('ADB_PATH') or (
    r"C:\Users\wusi0\AppData\Local\Android\Sdk\platform-tools\adb.exe"
    if os.name == 'nt' else 'adb')
SCREEN_MAX = int(os.environ.get('SCREEN_MAX', '480'))              # 输出最长边（等比缩放）
SCREEN_BITRATE = int(os.environ.get('SCREEN_BITRATE', '150000'))  # 码率 bps

QMAX = int(os.environ.get('RELAY_QMAX', '60'))        # 广播队列上限（分片）
SEND_TIMEOUT = float(os.environ.get('RELAY_SEND_TIMEOUT', '2'))  # 保留(兼容环境变量)
MAX_PENDING = int(os.environ.get('RELAY_MAX_PENDING', '30'))       # 单客户端最大在途帧，超过则丢帧限流

# ---- scrcpy 单隧道采屏 ----
# 用 scrcpy-server 采屏：一条 adb reverse 隧道同时承载画面(annexb H.264)。
# screenrecord 因存在 180s 单段录制上限已废弃，仅保留 scrcpy 路径。
SCRCPY_VERSION = '5.0.1'                                  # 与 scrcpy-server.jar 严格匹配(server 强校验)


def _scrcpy_server_path():
    """定位 scrcpy-server.jar：优先 env，其次仓库挂载 /app/scrcpy，再 /opt/scrcpy。"""
    for p in (os.environ.get('SCRCPY_SERVER_PATH'),
              '/app/scrcpy/scrcpy-server', '/opt/scrcpy/scrcpy-server'):
        if p and os.path.isfile(p):
            return p
    return os.environ.get('SCRCPY_SERVER_PATH') or '/app/scrcpy/scrcpy-server'


SCRCPY_SERVER_LOCAL = _scrcpy_server_path()
DEVICE_SERVER_JAR = '/data/local/tmp/scrcpy-server.jar'
SCRCPY_CODEC_H264 = 0x68323634                            # "h264" ASCII，大端 codec_id
# 剥离 SPS 的 color VUI：scrcpy 标 BT.601矩阵+BT.709传输，浏览器(尤其硬件解码)偏蓝；
# 剥掉后浏览器用默认色彩转换、显示正常。RELAY_STRIP_VUI=0 可关闭。
SCRCPY_STRIP_VUI = os.environ.get('RELAY_STRIP_VUI', '0') == '1'

# ---- scrcpy 设备端独立运行（detached）----
# 用 nohup 让 scrcpy-server 脱离 adb shell 会话独立运行：adb 短暂断开/重连时，采集进程
# 不随之消失，relay 只需重建 reverse 隧道并重新 accept 续流，避免每次都 push+重启 server。
SCRCPY_DETACHED = os.environ.get('RELAY_SCRCPY_DETACHED', '1') == '1'
# 设备端 server 启动/回连握手的最长等待(秒)；超过则判定 server 未起来，需重新拉起
SCRCPY_HANDSHAKE_TIMEOUT = int(os.environ.get('RELAY_SCRCPY_HANDSHAKE_TIMEOUT', '20'))
# 帧循环读超时(秒)：静止画面不产帧，读超时仅当作"无新帧继续等"，绝不据此判故障
SCRCPY_FRAME_TIMEOUT = float(os.environ.get('RELAY_SCRCPY_FRAME_TIMEOUT', '1.0'))
# 稳定 scid：同一 adb_addr 派生固定 scid，server 常驻后隧道重建能对上号，不重复起实例
SCRCPY_DETACHED_LOG = os.environ.get('RELAY_SCRCPY_LOG', '/data/local/tmp')

streams = {}                 # adb_addr -> Stream
lock = threading.Lock()      # 保护 streams 字典
epochs = {}                  # adb_addr -> 当前生效的代际号；旧 Stream 据此判断自己是否已过期
_epoch_lock = threading.Lock()

# ---- 设备端 Agent 部署（方向 B：RTP + MediaMTX）----
# Agent 是 Go 编译的单二进制，部署到设备 /data/local/tmp/myt-agent 常驻运行。
# relay 负责：检测 Agent 存活 → 不在则 push + 启动（带 MediaMTX RTSP 推流地址）。
# 画面数据由 Agent 经 RTSP 推流到 MediaMTX，浏览器经 WebRTC (WHEP) 拉流，全平台兼容。
# USE_AGENT=1 启用方向 B（默认）；设为 0 回退到传统 adb reverse 模式。
USE_AGENT = os.environ.get('RELAY_USE_AGENT', '1') == '1'
AGENT_BIN_PATH = os.environ.get('AGENT_BIN_PATH') or '/app/agent/myt-agent-arm64'
AGENT_DEVICE_PATH = os.environ.get('AGENT_DEVICE_PATH') or '/data/local/tmp/myt-agent'

# MediaMTX RTSP 推流地址（Agent 出站推流到此；不含 stream path，path 由 relay 拼接）
MEDIAMTX_RTSP_URL = os.environ.get('MEDIAMTX_RTSP_URL', 'rtsp://113.105.100.58:8554')
# MediaMTX WebRTC (WHEP) 拉流地址（浏览器从此拉流）
MEDIAMTX_WEBRTC_URL = os.environ.get('MEDIAMTX_WEBRTC_URL', 'http://113.105.100.58:8889')

AGENT_TOKEN = os.environ.get('AGENT_TOKEN', '')
# Agent 推流传输协议：udp 适合弱网实时性（已在本地点对点验证可用）；如需排障可切回 tcp。
AGENT_TRANSPORT = os.environ.get('AGENT_TRANSPORT', 'udp')
AGENT_MAX_SIZE = int(os.environ.get('AGENT_MAX_SIZE', '720'))
AGENT_BITRATE = int(os.environ.get('AGENT_BITRATE', '1000000'))
AGENT_KEYFRAME_INTERVAL = int(os.environ.get('AGENT_KEYFRAME_INTERVAL', '1'))

# ---- Agent 生命周期管理 ----
AGENT_IDLE_STOP_SEC = int(os.environ.get('AGENT_IDLE_STOP_SEC', '300'))   # 无观众后多少秒停止 Agent（省设备资源），0=不自动停
AGENT_HEALTH_CHECK_SEC = int(os.environ.get('AGENT_HEALTH_CHECK_SEC', '30'))  # 健康检查间隔(秒)

# adb_addr -> AgentState
#   status: 'unknown'|'deploying'|'running'|'stopped'|'error'|'offline'
#   stream_name: MediaMTX 流名
#   last_seen: 最后一次心跳/上报时间戳
#   last_sub_ts: 最后一次有 WebRTC 订阅者的时间戳（用于空闲停止）
agent_states = {}          # type: dict[str, dict]
agent_states_lock = threading.Lock()
agent_deploy_locks = {}     # adb_addr -> per-device deploy lock
agent_deploy_locks_lock = threading.Lock()


def _agent_deploy_lock(adb_addr):
    """Return a lock that serializes lifecycle operations for one device."""
    with agent_deploy_locks_lock:
        return agent_deploy_locks.setdefault(adb_addr, threading.Lock())


class AgentManager:
    """设备端 Agent 生命周期管理：部署、健康检查、空闲停止、自愈。"""

    def __init__(self):
        self._health_thread = None
        self._idle_thread = None
        self._started = False

    def start(self):
        """启动后台健康检查 + 空闲停止线程（仅启动一次）。"""
        with agent_states_lock:
            if self._started:
                return
            self._started = True
        self._health_thread = threading.Thread(target=self._health_check_loop, daemon=True,
                                               name='agent-health')
        self._idle_thread = threading.Thread(target=self._idle_stop_loop, daemon=True,
                                             name='agent-idle-stop')
        self._health_thread.start()
        self._idle_thread.start()
        print('[agent-mgr] Agent 生命周期管理已启动 (health_check=%ds, idle_stop=%ds)' % (
            AGENT_HEALTH_CHECK_SEC, AGENT_IDLE_STOP_SEC), flush=True)

    def get_state(self, adb_addr):
        with agent_states_lock:
            return dict(agent_states.get(adb_addr, {
                'status': 'unknown', 'stream_name': '', 'last_seen': '',
                'last_sub_ts': 0.0,
            }))

    def set_state(self, adb_addr, **fields):
        with agent_states_lock:
            st = agent_states.setdefault(adb_addr, {
                'status': 'unknown', 'stream_name': '', 'last_seen': '',
                'last_sub_ts': 0.0,
            })
            st.update(fields)
            st['last_seen'] = time.strftime('%Y-%m-%d %H:%M:%S')
            return dict(st)

    def touch_subscriber(self, adb_addr):
        """有 WebRTC 订阅者加入时更新最后订阅时间。"""
        with agent_states_lock:
            st = agent_states.setdefault(adb_addr, {
                'status': 'unknown', 'stream_name': '', 'last_seen': '',
                'last_sub_ts': 0.0,
            })
            st['last_sub_ts'] = time.time()
            st['last_seen'] = time.strftime('%Y-%m-%d %H:%M:%S')

    def deploy(self, adb_addr, max_size=None):
        """部署并启动 Agent（幂等）。返回 (ok, msg)。"""
        # 同一设备的并发 ensure/deploy 必须串行，避免两个 HTTP 请求同时启动 Agent。
        lock = _agent_deploy_lock(adb_addr)
        with lock:
            # 保持幂等：健康的 Agent 正在运行时绝不能先 kill。
            # 普通 ensure/WHEP 重连会频繁调用此方法，重启会中断媒体流。
            process_count = _agent_process_count(adb_addr)
            if max_size is None and process_count == 1:
                self.set_state(adb_addr, status='running',
                               stream_name=_agent_stream_name(adb_addr))
                self.touch_subscriber(adb_addr)
                return True, 'Agent 已在运行'

            # Agent 未运行、发现重复实例、或显式切换参数时，清理后重新部署。
            _kill_agent_process(adb_addr)
            self.set_state(adb_addr, status='deploying')
            ok = _deploy_agent(adb_addr, int(max_size) if max_size else None)
            if ok:
                self.set_state(adb_addr, status='running',
                               stream_name=_agent_stream_name(adb_addr))
                self.touch_subscriber(adb_addr)
                return True, 'Agent 已部署并启动'
            self.set_state(adb_addr, status='error')
            return False, 'Agent 部署失败'

    def stop(self, adb_addr):
        """停止设备端 Agent 进程。"""
        try:
            subprocess.run([ADB, 'connect', adb_addr], capture_output=True, timeout=10)
            _kill_agent_process(adb_addr)
        except Exception:
            pass
        self.set_state(adb_addr, status='stopped')
        return True, 'Agent 已停止'

    def is_running(self, adb_addr):
        return _agent_running(adb_addr)

    # ---- 后台循环 ----

    def _health_check_loop(self):
        """每 AGENT_HEALTH_CHECK_SEC 检查一次已知设备的 Agent 存活，死掉则重新部署。"""
        while True:
            time.sleep(AGENT_HEALTH_CHECK_SEC)
            with agent_states_lock:
                addrs = list(agent_states.keys())
            for addr in addrs:
                try:
                    if self.is_running(addr):
                        self.set_state(addr, status='running')
                    else:
                        st = self.get_state(addr)
                        if st['status'] == 'running':
                            # 之前是 running，现在死了 -> 自愈重新部署
                            print(f'[agent-mgr] {addr} Agent 非正常退出，自愈重新部署', flush=True)
                            self.deploy(addr)
                        elif st['status'] in ('unknown', 'error', 'stopped'):
                            # 非运行状态，不主动部署（等用户触发或有订阅者时按需部署）
                            pass
                except Exception as e:
                    print(f'[agent-mgr] {addr} 健康检查异常: {e}', flush=True)

    def _idle_stop_loop(self):
        """每 AGENT_HEALTH_CHECK_SEC 检查空闲设备，无订阅者超过 AGENT_IDLE_STOP_SEC 则停止 Agent。"""
        if AGENT_IDLE_STOP_SEC <= 0:
            return
        while True:
            time.sleep(AGENT_HEALTH_CHECK_SEC)
            now = time.time()
            with agent_states_lock:
                addrs = list(agent_states.keys())
            for addr in addrs:
                st = self.get_state(addr)
                if st['status'] != 'running':
                    continue
                if st['last_sub_ts'] <= 0:
                    continue  # 从未有过订阅者，不主动停（部署时即可能立即观看）
                idle = now - st['last_sub_ts']
                if idle > AGENT_IDLE_STOP_SEC:
                    print(f'[agent-mgr] {addr} 无订阅者空闲 {idle:.0f}s > {AGENT_IDLE_STOP_SEC}s，停止 Agent', flush=True)
                    self.stop(addr)


agent_mgr = AgentManager()


def _agent_stream_name(adb_addr):
    """与 MediaMTX 流名一致：dev_<ip>_<port>"""
    return _stream_name(adb_addr)


def _ensure_agent_for_ws(adb_addr):
    """WebSocket 连接进来时确保 Agent 运行（按需部署）。"""
    agent_mgr.touch_subscriber(adb_addr)
    st = agent_mgr.get_state(adb_addr)
    if st['status'] == 'running' and agent_mgr.is_running(adb_addr):
        return True
    if st['status'] == 'deploying':
        return False  # 正在部署，等
    ok, msg = agent_mgr.deploy(adb_addr)
    return ok


def _stream_name(adb_addr):
    """把 adb_addr 转成 MediaMTX 流名（仅字母数字下划线）。"""
    return 'dev_' + str(adb_addr).replace('.', '_').replace(':', '_').replace('-', '_')


def _agent_process_count(adb_addr):
    """Count actual Agent processes (not their background `sh -c` launcher)."""
    try:
        subprocess.run([ADB, 'connect', adb_addr], capture_output=True, timeout=15)
        # ps -ef exists on the target cloud phones and common Android builds.
        # Exclude shell/grep/awk command lines, which contain the search string.
        r = subprocess.run(
            [ADB, '-s', adb_addr, 'shell',
             "ps -ef | grep 'myt-agent' | grep -v grep | grep -v 'sh -c' | grep -v awk"],
            capture_output=True, timeout=8)
        if r.returncode == 0:
            return sum(1 for line in (r.stdout or b'').decode('utf-8', 'replace').splitlines()
                       if line.strip())
        # Fallback to Android's ps format, excluding shell and helper processes.
        r = subprocess.run(
            [ADB, '-s', adb_addr, 'shell',
             "ps -A -o PID,NAME,ARGS | awk '$2 != \"sh\" && $2 != \"grep\" && $2 != \"awk\" "
             "&& index($0, \"myt-agent\") {print $1}'"],
            capture_output=True, timeout=8)
        if r.returncode == 0:
            return sum(1 for line in (r.stdout or b'').decode('utf-8', 'replace').splitlines()
                       if line.strip())
    except Exception:
        pass
    return -1


def _device_agent_pids(adb_addr):
    """List Agent, scrcpy server and stale launcher PIDs on a device."""
    pids = set()
    commands = (
        "ps -ef | grep -E 'myt-agent|scrcpy.Server' | grep -v grep | grep -v awk | grep -v 'sh -c' | awk '{print $2}'",
        "ps -ef | grep '[c]hmod 755 /data/local/tmp/myt-agent' | awk '{print $2}'",
    )
    for command in commands:
        try:
            r = subprocess.run([ADB, '-s', adb_addr, 'shell', command],
                               capture_output=True, timeout=8)
            for value in (r.stdout or b'').decode('utf-8', 'replace').split():
                if value.isdigit():
                    pids.add(value)
        except Exception:
            continue
    return pids


def _agent_running(adb_addr):
    """设备端是否有 Agent 进程（兼容云手机的 ps -ef/Android ps）。"""
    return _agent_process_count(adb_addr) > 0


def _kill_agent_process(adb_addr):
    """Kill exact PIDs (root or shell) and wait for Agent/scrcpy to exit.

    Avoid pkill -f: its pattern can match the pkill/adb shell command itself,
    and on some cloud phones it leaves root-owned Agent processes behind.
    """
    try:
        subprocess.run([ADB, 'connect', adb_addr], capture_output=True, timeout=15)
    except Exception:
        pass
    for _ in range(3):
        pids = _device_agent_pids(adb_addr)
        if not pids:
            return
        pid_args = ' '.join(sorted(pids, key=int))
        # Try root first, then direct shell privileges for non-root devices.
        for command in (f"su -c 'kill -9 {pid_args}'", f'kill -9 {pid_args}'):
            try:
                subprocess.run([ADB, '-s', adb_addr, 'shell', command],
                               capture_output=True, timeout=10)
            except Exception:
                pass
        time.sleep(0.5)
    # Verify that no managed processes remain; don't silently report success.
    remaining = _device_agent_pids(adb_addr)
    if remaining:
        print(f'[agent] 无法清理设备 {adb_addr} 残余进程 PID={",".join(sorted(remaining))}', flush=True)


def _md5_file(path):
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _deploy_agent(adb_addr, max_size=None):
    """推送并启动设备端 Agent（幂等：MD5 一致则跳过推送）。

    推送两个文件：
      1. Agent 二进制（myt-agent-arm64）
      2. scrcpy-server.jar（Agent 启动时依赖，用于采集）
    """
    if not os.path.exists(AGENT_BIN_PATH):
        print(f'[agent] 本机无 Agent 二进制 {AGENT_BIN_PATH}，跳过部署', flush=True)
        return False
    try:
        # 0. 先 adb connect（adb_addr 是网关映射地址，必须 connect 后才能 push）
        rc = subprocess.run([ADB, 'connect', adb_addr], capture_output=True, timeout=15)
        cout = (rc.stdout or b'').decode('utf-8', 'replace').strip()
        if rc.returncode != 0 and 'connected' not in cout and 'already' not in cout:
            print(f'[agent] adb connect {adb_addr} 失败: {cout}', flush=True)
            return False

        # 1. 检查设备端 Agent 是否与本地二进制一致（MD5）。
        # 只比大小不够：Go 加一串常量后二进制大小可能完全不变，会漏推新版本。
        local_size = os.path.getsize(AGENT_BIN_PATH)
        local_md5 = _md5_file(AGENT_BIN_PATH)
        remote_md5 = ''
        r = subprocess.run([ADB, '-s', adb_addr, 'shell', 'md5sum', AGENT_DEVICE_PATH],
                           capture_output=True, timeout=8)
        if r.returncode == 0 and (r.stdout or b'').strip():
            remote_md5 = r.stdout.decode('utf-8', 'replace').split()[0].lower()
        r = subprocess.run([ADB, '-s', adb_addr, 'shell', 'ls', '-l', AGENT_DEVICE_PATH],
                           capture_output=True, timeout=8)
        remote_exists = r.returncode == 0
        remote_size = 0
        if remote_exists:
            # ls -l 输出格式：-rwxr-xr-x 1 root root 7078050 ... myt-agent
            parts = r.stdout.decode('utf-8', 'replace').split()
            for p in parts:
                if p.isdigit():
                    remote_size = int(p)
                    break
        need_push = True
        if remote_md5:
            need_push = remote_md5 != local_md5
        elif remote_exists and remote_size == local_size:
            # 设备没有 md5sum 时退回大小比较
            need_push = False
        if not need_push:
            print(f'[agent] {adb_addr} 设备端 Agent 已是最新 ({local_md5[:12]}...)，跳过 push', flush=True)
        else:
            # 1. push Agent 二进制（检查返回码，失败即暴露真实原因）
            r = subprocess.run([ADB, '-s', adb_addr, 'push', AGENT_BIN_PATH, AGENT_DEVICE_PATH],
                               capture_output=True, timeout=60)
            if r.returncode != 0:
                err = (r.stderr or r.stdout or b'').decode('utf-8', 'replace').strip()
                print(f'[agent] push Agent 到 {adb_addr} 失败: {err}', flush=True)
                return False
            print(f'[agent] 已 push Agent 到 {adb_addr} ({local_size} bytes)', flush=True)

        # 1.1 push scrcpy-server.jar（Agent 采集依赖）
        jar_local = SCRCPY_SERVER_LOCAL
        jar_device = DEVICE_SERVER_JAR
        if os.path.exists(jar_local):
            local_jar_size = os.path.getsize(jar_local)
            r = subprocess.run([ADB, '-s', adb_addr, 'shell', 'test', '-f', jar_device],
                               capture_output=True, timeout=8)
            if r.returncode != 0:
                # jar 不存在，需要 push
                r2 = subprocess.run([ADB, '-s', adb_addr, 'push', jar_local, jar_device],
                                    capture_output=True, timeout=60)
                if r2.returncode != 0:
                    err = (r2.stderr or r2.stdout or b'').decode('utf-8', 'replace').strip()
                    print(f'[agent] push scrcpy-server 到 {adb_addr} 失败: {err}', flush=True)
                else:
                    print(f'[agent] 已 push scrcpy-server 到 {adb_addr} ({local_jar_size} bytes)', flush=True)
            else:
                print(f'[agent] {adb_addr} 设备端 scrcpy-server.jar 已存在，跳过 push', flush=True)
        else:
            print(f'[agent] 本机无 scrcpy-server {jar_local}，Agent 可能无法采集', flush=True)

        # 2. 授权 + 启动（后台常驻）
        # Agent 推流 URL：MediaMTX RTSP （含 stream path）
        _max_size = int(max_size) if max_size else AGENT_MAX_SIZE
        push_url = f'{MEDIAMTX_RTSP_URL}/{_stream_name(adb_addr)}'
        # 先单独清理旧进程（避免启动命令自匹配被 kill），再启动新进程。
        _kill_agent_process(adb_addr)
        # 整个 { ... } & 组合重定向到日志文件：后台 shell 不再持有 adb shell 的
        # stdout/stderr，否则 adb shell 会一直等后台进程退出（表现为部署超时）。
        launch = (f"{{ chmod 755 {AGENT_DEVICE_PATH} && "
                  f"setsid nohup {AGENT_DEVICE_PATH} -relay '{push_url}' "
                  f"-transport {AGENT_TRANSPORT} -max-size {_max_size} -bitrate {AGENT_BITRATE} "
                  f"-keyframe-interval {AGENT_KEYFRAME_INTERVAL}; }} "
                  f">/data/local/tmp/myt-agent.log 2>&1 </dev/null &")
        # 超时放宽到 15s；即使 adb shell 返回超时，只要 Agent 进程已起来也算成功。
        try:
            r3 = subprocess.run([ADB, '-s', adb_addr, 'shell', launch],
                                capture_output=True, timeout=30)
            launch_ok = r3.returncode == 0
            err = (r3.stderr or r3.stdout or b'').decode('utf-8', 'replace').strip()
        except subprocess.TimeoutExpired:
            launch_ok = False
            err = 'adb shell 等待启动命令超时'
        if not launch_ok:
            time.sleep(1)
            if _agent_running(adb_addr):
                print(f'[agent] 启动命令未正常返回，但 Agent 已在运行: {adb_addr}', flush=True)
            else:
                print(f'[agent] 启动 Agent 到 {adb_addr} 失败: {err}', flush=True)
                return False
        # 启动 Agent 后等待 scrcpy-server 初始化，然后发送 dummy 触摸事件触发第一帧。
        # 云手机等设备在屏幕完全静态时，scrcpy 不会发送第一帧（repeat frame 机制需要至少一帧），
        # 导致 Agent 的 reader.Read() 永久阻塞，画面黑屏。
        time.sleep(5)
        try:
            subprocess.run([ADB, '-s', adb_addr, 'shell', 'input tap 100 100'],
                           capture_output=True, timeout=5)
            print(f'[agent] 已发送 dummy 触摸触发首帧: {adb_addr}', flush=True)
        except Exception as e:
            print(f'[agent] dummy 触摸失败: {e}', flush=True)
    except Exception as e:
        print(f'[agent] 部署 Agent 到 {adb_addr} 失败: {e}', flush=True)
        return False


def _ensure_agent(adb_addr):
    """确保设备端 Agent 存活：不在则部署启动。"""
    if _agent_running(adb_addr):
        return True
    return _deploy_agent(adb_addr)


def _token_from_request(ws):
    """从 WebSocket 握手请求里取 myt_token cookie（与 Web 登录共用同一 JWT）。"""
    req = getattr(ws, 'request', None)
    if not req:
        return None
    headers = getattr(req, 'headers', {}) or {}
    try:
        cookie = headers.get('Cookie', '') or headers.get('cookie', '') or ''
    except Exception:
        cookie = ''
    for part in str(cookie).split(';'):
        part = part.strip()
        if part.startswith('myt_token='):
            return part[len('myt_token='):]
    return None


DEVICE_SIZE_CACHE = {}   # adb_addr -> (expire_ts, (w, h))；分辨率极少变化，缓存避免每次 tap 都 wm size


def get_device_size(adb_addr):
    """读取设备(容器)逻辑分辨率，Override 优先，其次 Physical（带 TTL 缓存，避免每次 tap 都 wm size）。"""
    now = time.time()
    cached = DEVICE_SIZE_CACHE.get(adb_addr)
    if cached and cached[0] > now:
        return cached[1]
    try:
        r = subprocess.run([ADB, '-s', adb_addr, 'shell', 'wm', 'size'],
                           capture_output=True, timeout=10)
        out = (r.stdout or b'').decode(errors='replace')
    except Exception:
        return None
    physical = None
    for line in out.splitlines():
        low = line.lower()
        if 'size:' not in low:
            continue
        m = re.search(r'(\d+)x(\d+)', line)
        if not m:
            continue
        pair = (int(m.group(1)), int(m.group(2)))
        if 'override' in low:
            DEVICE_SIZE_CACHE[adb_addr] = (now + 300, pair)
            return pair
        physical = physical or pair
    if physical:
        DEVICE_SIZE_CACHE[adb_addr] = (now + 300, physical)
    return physical


def _sps_strip_vui(sps):
    """sps: SPS NAL(含 0x67 头)。返回剥离 color VUI(vui_parameters_present=0)后的 SPS，
    让浏览器按默认色彩转换(消除 BT.601/BT.709 混搭导致的偏蓝)。任何解析异常都原样返回(安全)。"""
    try:
        hdr = sps[0]
        rbsp = bytearray(); i = 1; zeros = 0; n = len(sps)
        while i < n:
            b = sps[i]
            if zeros == 2 and b == 0x03:
                zeros = 0; i += 1; continue
            rbsp.append(b)
            zeros = zeros + 1 if b == 0 else 0
            i += 1
        bits = []
        for by in rbsp:
            for k in range(7, -1, -1):
                bits.append((by >> k) & 1)
        pos = 0
        def u(nb):
            nonlocal pos
            v = 0
            for _ in range(nb):
                v = (v << 1) | bits[pos]; pos += 1
            return v
        def ue():
            nonlocal pos
            lz = 0
            while bits[pos] == 0:
                lz += 1; pos += 1
                if lz > 32:
                    raise ValueError('ue')
            pos += 1
            suffix = 0
            for _ in range(lz):
                suffix = (suffix << 1) | bits[pos]; pos += 1
            return (1 << lz) - 1 + suffix
        profile = u(8); u(8); u(8); ue()
        if profile in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
            c = ue()
            if c == 3:
                u(1)
            ue(); ue(); u(1)
            if u(1):
                raise ValueError('scaling')
        ue()
        p = ue()
        if p == 0:
            ue()
        elif p == 1:
            u(1); ue(); ue()
            for _ in range(ue()):
                ue()
        ue(); u(1); ue(); ue(); u(1); u(1)
        if u(1):
            ue(); ue(); ue(); ue()
        vui_pos = pos
        if bits[vui_pos] == 0:
            return sps
        newbits = bits[:vui_pos] + [0, 1]
        while len(newbits) % 8:
            newbits.append(0)
        newrbsp = bytearray()
        for j in range(0, len(newbits), 8):
            by = 0
            for k in range(8):
                by = (by << 1) | newbits[j + k]
            newrbsp.append(by)
        out = bytearray([hdr])
        for by in newrbsp:
            if len(out) >= 2 and out[-1] == 0 and out[-2] == 0 and by <= 3:
                out.append(0x03)
            out.append(by)
        return bytes(out)
    except Exception:
        return sps


def _strip_frame_sps_vui(data):
    """对一帧 annexb H.264，剥离其中 SPS NAL 的 color VUI。无 SPS 或无需改则原样返回。"""
    n = len(data); starts = []; i = 0
    while i < n - 3:
        if data[i] == 0 and data[i+1] == 0 and data[i+2] == 1:
            starts.append((i, 3)); i += 3
        elif data[i] == 0 and data[i+1] == 0 and data[i+2] == 0 and data[i+3] == 1:
            starts.append((i, 4)); i += 4
        else:
            i += 1
    if not starts:
        return data
    out = bytearray(); changed = False
    for idx, (scpos, sclen) in enumerate(starts):
        nal_start = scpos + sclen
        nal_end = starts[idx + 1][0] if idx + 1 < len(starts) else n
        if nal_start < n and (data[nal_start] & 0x1F) == 7 and (data[nal_start] & 0x80) == 0:
            nal = data[nal_start:nal_end]
            newnal = _sps_strip_vui(nal)
            if newnal != nal:
                changed = True
            out += data[scpos:nal_start] + newnal
        else:
            out += data[scpos:nal_end]
    return bytes(out) if changed else data


class Stream:
    """同一 adb_addr 的共享投屏流：一个 scrcpy-server 采集 + 多线程 + 队列广播。"""

    def __init__(self, adb_addr, loop):
        self.adb_addr = adb_addr
        self.loop = loop                      # 主事件循环，用于跨线程调度 ws.send
        self.q = queue.Queue(maxsize=QMAX)    # 生产者 -> 分发者 的有界缓冲
        self.subs = set()                     # 订阅该设备的客户端 WebSocket
        self.sub_lock = threading.Lock()
        self.pending_lock = threading.Lock()
        self._pending = {}            # ws -> 在途帧数(防止半开/慢客户端堆积)
        self.stop = threading.Event()
        self.kick = threading.Event()         # 触发重启以吐 IDR（后加入观众）
        self.last_start = 0.0
        self.backoff = 0.0                  # 失败退避时间(秒)，成功出流后归零
        self._max_size = SCREEN_MAX         # scrcpy max_size（输出最长边）
        self._want_resize = False           # 运行中热切换分辨率标志
        # 稳定 scid：同一 adb_addr 派生固定 scid，设备端 server 常驻后可复用/续流，
        # 不因 adb 抖动重复起新实例（多个实例会叠加采屏压垮 adbd）。
        # 注意：scrcpy-server 用 Integer.parseInt(value, 16) 解析 scid，且必须 <= 0x7fffffff，
        # 否则溢出抛 NumberFormatException 导致 server 秒退。故取 md5 前 8 位并清最高位。
        self.scid = '%08x' % (int(hashlib.md5(adb_addr.encode('utf-8')).hexdigest()[:8], 16) & 0x7fffffff)
        self._server_alive = False          # 设备端 scrcpy-server 是否已被拉起(detached)
        # 代际号：用于防止「旧 Stream 的收尾 kill 误杀新 Stream 刚启动的 server」。
        # 新 Stream 建立时全局 epoch 递增，旧 Stream 的 kill/cleanup 发现 epoch 过期则跳过。
        with _epoch_lock:
            epochs[adb_addr] = epochs.get(adb_addr, 0) + 1
            self._epoch = epochs[adb_addr]
        self._stopped = False               # 本 Stream 是否已彻底停止（防止重复收尾）
        self.producer = None
        self.dispatcher = None
        self.heartbeat = None

    def start(self):
        base = self._produce_scrcpy

        def _guard():
            # 崩溃自恢复：producer 任何未捕获异常都打印堆栈并重启，避免线程一死该格永久黑屏
            while not self.stop.is_set():
                try:
                    base()
                except Exception as e:
                    import traceback
                    try:
                        traceback.print_exc()
                    except Exception:
                        pass
                    print(f'[stream] {self.adb_addr} producer crashed, restart in 2s: {type(e).__name__}: {e}', flush=True)
                    time.sleep(2)

        self.producer = threading.Thread(target=_guard, daemon=True,
                                        name=f'prod-{self.adb_addr}')
        self.dispatcher = threading.Thread(target=self._dispatch, daemon=True,
                                          name=f'disp-{self.adb_addr}')
        self.heartbeat = threading.Thread(target=self._heartbeat, daemon=True,
                                         name=f'hb-{self.adb_addr}')
        self.producer.start()
        self.dispatcher.start()
        self.heartbeat.start()

    def start_agent(self):
        """Agent 上行流模式：只启动 dispatcher（帧来自设备端 Agent 的 WebSocket，非 adb）。
        producer/heartbeat 不启动——不依赖 adb。
        """
        self.dispatcher = threading.Thread(target=self._dispatch, daemon=True,
                                          name=f'disp-{self.adb_addr}')
        self.dispatcher.start()

    # ---- 订阅管理 ----
    def add_sub(self, ws):
        with self.sub_lock:
            first = len(self.subs) == 0
            self.subs.add(ws)
        # 通知 AgentManager 有订阅者（用于空闲停止判断）
        agent_mgr.touch_subscriber(self.adb_addr)
        # 后加入者错过当前 IDR，请求重启让其快速出画（0.3s 去抖）
        if not first and self.last_start and (time.time() - self.last_start) > 0.3:
            self.kick.set()

    def remove_sub(self, ws):
        with self.sub_lock:
            self.subs.discard(ws)
            empty = len(self.subs) == 0
        if empty:
            # 仅置 stop：server 的清理交给 producer 线程 finally 同步处理。
            # 不能在这里异步 kill——否则会与「前端重连后新 Stream 的 launch」发生竞态，
            # 异步 kill 可能晚于新 launch 才执行，把刚启动的新 server 杀掉。
            self.stop.set()

    def _subs_snapshot(self):
        with self.sub_lock:
            return list(self.subs)

    def feed_frame(self, payload):
        """Agent 上行流入口：设备端 Agent 经 WebSocket 推送的 H.264 帧直接入队。
        复用现有 _dispatch 分发线程扇出给浏览器订阅者（绕开 adb 隧道）。
        """
        try:
            self.q.put(payload, timeout=2)
        except queue.Full:
            pass

    def _heartbeat(self):
        while not self.stop.is_set():
            time.sleep(30)
            if self.stop.is_set():
                break
            try:
                # 超时放宽到 15s：5s 会 SIGKILL 掉正在握手的 adb 进程，留下半开传输
                subprocess.run([ADB, 'connect', self.adb_addr],
                               capture_output=True, timeout=15)
            except Exception:
                pass

    def _fail_backoff(self, err):
        """单设备失败后的处理策略：**告警 + 等待**，不执行任何全局破坏性动作。

        ⚠ 设计约束：绝不调用 `adb kill-server`。
          kill-server 会瞬间断开本机**所有**设备的 adb 连接，把本来健康的设备一起拖下线。
          修复动作严格限定在当前这一个 addr：adb disconnect -> adb connect。

        等待策略在旧实现基础上加严：
          - 退避起点 2s -> 15s，上限 30s -> 120s：避免对着刚断开的设备猛锤；
          - 每次等待叠加 ±30% 随机抖动：防止多台设备「同频重试」，把唯一的 adb daemon 挤死。
        """
        e = (err or '').lower()
        offline = ('offline' in e) or ('closed' in e) or ('not found' in e) or ('timed out' in e) or ('refused' in e)
        if offline:
            # 告警：明确是单设备掉线，且只对该设备做 disconnect -> connect
            print(f'[alarm] {self.adb_addr} adb 掉线 ({err}) -> 仅对本设备 disconnect/connect 后等待重试，'
                  f'不执行 kill-server', flush=True)
            try:
                subprocess.run([ADB, 'disconnect', self.adb_addr], capture_output=True, timeout=10)
            except Exception:
                pass
            time.sleep(1.0)
            try:
                subprocess.run([ADB, 'connect', self.adb_addr], capture_output=True, timeout=15)
            except Exception:
                pass
        else:
            # 画面中断但 adb 判定在线：只告警 + 等待，让设备/网络自行恢复
            print(f'[alarm] {self.adb_addr} 画面中断 ({err}) -> 告警并等待重试，不做全局动作', flush=True)
        if self.backoff <= 0:
            self.backoff = 15.0
        else:
            self.backoff = min(self.backoff * 2, 120.0)
        # ±30% 抖动：打散多设备的重试时刻，避免重连风暴
        wait = max(1.0, self.backoff * random.uniform(0.7, 1.3))
        print(f'[stream] {self.adb_addr} backoff wait {wait:.0f}s (offline={offline})', flush=True)
        time.sleep(wait)

    # ---- scrcpy 单隧道采屏 ----
    def _scrcpy_cleanup(self):
        """清理设备上残留的 scrcpy server 与反向隧道；残留会叠加采屏压垮 adbd。
        若本 Stream 已被新 Stream 取代（epoch 过期），跳过，避免误杀新 server。"""
        if self._is_stale():
            return
        try:
            subprocess.run([ADB, '-s', self.adb_addr, 'shell', 'pkill', '-f',
                            'com.genymobile.scrcpy.Server'], capture_output=True, timeout=8)
        except Exception:
            pass
        try:
            subprocess.run([ADB, '-s', self.adb_addr, 'reverse', '--remove-all'],
                           capture_output=True, timeout=8)
        except Exception:
            pass

    @staticmethod
    def _recvn(sock, n):
        buf = b''
        while len(buf) < n:
            c = sock.recv(n - len(buf))
            if not c:
                break
            buf += c
        return buf

    def _is_stale(self):
        """本 Stream 是否已被新 Stream 取代（epoch 过期）。旧 Stream 的 kill/cleanup 应跳过。"""
        with _epoch_lock:
            return epochs.get(self.adb_addr, 0) != self._epoch

    def _scrcpy_kill_server(self):
        """杀掉本 scid 对应的 scrcpy-server 进程（含 nohup 包装的 shell 与 app_process）。

        注意：
        - `pkill -f <scid>` 匹配完整命令行，会同时命中 nohup 的 sh 包装进程和
          app_process（两者命令行都含 scid=xxx），能彻底清理。
        - 若本 Stream 已被新 Stream 取代（epoch 过期），跳过 kill，避免误杀新 server。
        """
        if self._is_stale():
            return
        try:
            subprocess.run([ADB, '-s', self.adb_addr, 'shell', 'pkill', '-f', self.scid],
                           capture_output=True, timeout=8)
        except Exception:
            pass
        self._server_alive = False

    def _launch_scrcpy_server(self):
        """幂等启动 scrcpy-server：先杀本 scid 旧实例，再启动，保证设备端单实例。

        关键：launch 自身幂等（先 kill 后起），任何调用点都不会导致重复启动叠加。
        SCRCPY_DETACHED=1 时用 `nohup sh -c '...' &` 使其脱离 adb shell 会话独立运行。

        注意：必须用 `sh -c '...'` 包裹命令——`CLASSPATH=... app_process` 里的环境变量
        赋值是 shell 语法，直接 `nohup CLASSPATH=... app_process` 会让 nohup 把
        `CLASSPATH=...` 当成可执行命令名，导致 server 启动即失败（秒退、从未存活）。
        """
        # 幂等 + 防竞态：先杀旧实例；若本 Stream 已过期则直接返回（不启动）
        if self._is_stale():
            return
        self._scrcpy_kill_server()
        cmd = ('CLASSPATH=%s app_process / com.genymobile.scrcpy.Server %s '
               'scid=%s log_level=warn video=true audio=false '
               'control=false send_device_meta=false send_stream_meta=true '
               'send_frame_meta=true max_size=%d video_bit_rate=%d') % (
            DEVICE_SERVER_JAR, SCRCPY_VERSION, self.scid, self._max_size, SCREEN_BITRATE)
        logfile = os.path.join(SCRCPY_DETACHED_LOG, 'scrcpy_%s.log' % self.scid)
        if SCRCPY_DETACHED:
            shell = "nohup sh -c '%s' >%s 2>&1 </dev/null &" % (cmd, logfile)
            subprocess.run([ADB, '-s', self.adb_addr, 'shell', shell],
                           capture_output=True, timeout=10)
            self._server_alive = True
        else:
            logf = open('/tmp/scrcpy_server_%s.log' % self.scid, 'ab')
            subprocess.Popen([ADB, '-s', self.adb_addr, 'shell', cmd],
                             stdout=logf, stderr=logf)
            self._server_alive = True

    def _produce_scrcpy(self):
        """生产者线程：adb reverse 隧道 + scrcpy-server 采屏，读 annexb H.264 帧入队。

        稳定性设计（修复频繁断开重连）：
        - scid 固定、hostport 固定：隧道地址可预期，不随机漂移；
        - 每次循环前 pkill 本 scid 旧实例再 launch：保证设备端单实例，避免多实例抢隧道；
        - 帧流用「读缓冲区」解析：TCP 分包时读一半超时绝不丢字节，杜绝帧头错位；
        - 帧读超时只当作"静止画面无新帧"继续等，绝不据此判故障重启。
        """
        sockname = 'scrcpy_' + self.scid
        # 固定 hostport（从 scid 派生）：端口不再随机漂移，避免 TIME_WAIT 累积与
        # server 重连时地址不可预期的问题。
        hostport = 47000 + (int(self.scid, 16) % 15000)
        while not self.stop.is_set():
            # 旧 Stream 被新 Stream 取代后，本 producer 应立即退出，不再 launch/kill
            if self._is_stale():
                break
            if not self.subs:
                time.sleep(0.2)
                continue
            # ---- adb connect（失败才退避，不杀 server）----
            connected = False
            out = ''
            for attempt in range(1, 3):
                if self.stop.is_set() or not self.subs:
                    return
                try:
                    r = subprocess.run([ADB, 'connect', self.adb_addr], capture_output=True, timeout=15)
                    out = (r.stdout or b'').decode(errors='replace')
                except Exception as e:
                    out = ''
                    print(f'[stream] {self.adb_addr} adb connect exception: {e}', flush=True)
                if 'connected' in out or 'already' in out:
                    connected = True
                    break
                print(f'[stream] {self.adb_addr} adb connect failed (attempt {attempt}/2): {out.strip()}', flush=True)
                time.sleep(2)
            if not connected:
                self._fail_backoff(out)
                continue

            # ---- 推送 server jar（jar 小，始终 push 保证版本一致）----
            try:
                subprocess.run([ADB, '-s', self.adb_addr, 'push', SCRCPY_SERVER_LOCAL, DEVICE_SERVER_JAR],
                               capture_output=True, timeout=15)
            except Exception as e:
                print(f'[stream] {self.adb_addr} scrcpy push server error: {e}', flush=True)

            # ---- reverse 隧道 + 本地监听（固定 hostport）----
            try:
                subprocess.run([ADB, '-s', self.adb_addr, 'reverse',
                                f'localabstract:{sockname}', f'tcp:{hostport}'],
                               capture_output=True, timeout=10)
            except Exception as e:
                print(f'[stream] {self.adb_addr} scrcpy reverse error: {e}', flush=True)
                self._fail_backoff(str(e))
                continue
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                srv.bind(('127.0.0.1', hostport))
                srv.listen(2)
            except Exception as e:
                print(f'[stream] {self.adb_addr} scrcpy bind error: {e}', flush=True)
                try:
                    srv.close()
                except Exception:
                    pass
                self._fail_backoff(str(e))
                continue
            srv.settimeout(SCRCPY_HANDSHAKE_TIMEOUT)

            # ---- 启动 server（每次干净重建，单实例）----
            self.last_start = time.time()
            self.kick.clear()
            try:
                self._launch_scrcpy_server()
            except Exception as e:
                print(f'[stream] {self.adb_addr} scrcpy server start error: {e}', flush=True)
                try:
                    srv.close()
                except Exception:
                    pass
                self._fail_backoff(str(e))
                continue
            if self._is_stale():
                # launch 期间被新 Stream 取代：立即放弃，不 accept、不误杀
                try:
                    srv.close()
                except Exception:
                    pass
                break
            print(f'[stream] {self.adb_addr} scrcpy server launched (scid={self.scid})', flush=True)

            conn = None
            resized = False
            try:
                conn, _ = srv.accept()          # server 主动回连 -> video socket
                conn.settimeout(SCRCPY_HANDSHAKE_TIMEOUT)
                cid = self._recvn(conn, 4)
                if len(cid) < 4 or int.from_bytes(cid, 'big') != SCRCPY_CODEC_H264:
                    raise EOFError('unexpected codec %s' % cid.hex())
                sess = self._recvn(conn, 12)
                if len(sess) < 12:
                    raise EOFError('short session')
                sw = int.from_bytes(sess[4:8], 'big')
                sh_ = int.from_bytes(sess[8:12], 'big')
                print(f'[stream] {self.adb_addr} scrcpy started {sw}x{sh_}', flush=True)
                resized = False
                # 握手完成后切回帧读超时（静止画面容忍）
                conn.settimeout(SCRCPY_FRAME_TIMEOUT)
                # ---- 帧流读取：读缓冲区方案 ----
                # scrcpy 帧 = 12 字节头(val+size) + size 字节 payload，是连续字节流。
                # 绝不能用"读满 n 字节否则丢弃"的方式读帧头：TCP 分包时若读一半超时，
                # 已读的半截帧头被丢弃，后续字节流错位 -> 花屏/卡死/误判断流。
                # 改为累积到 rbuf，从 rbuf 解析完整帧；读超时只代表"暂无新数据"继续等。
                rbuf = b''
                while not self.stop.is_set():
                    if not self.subs:
                        break
                    if self._want_resize:
                        self._want_resize = False
                        resized = True
                        print(f'[stream] {self.adb_addr} resize -> restart at {self._max_size}', flush=True)
                        break
                    if self.kick.is_set():
                        # scrcpy 关键帧频繁，新订阅者很快拿到 IDR，无需重启 server
                        self.kick.clear()
                    try:
                        chunk = conn.recv(65536)
                    except socket.timeout:
                        # 静止画面不产帧：暂无新数据，继续等（已读字节保留在 rbuf，不丢弃）
                        continue
                    if not chunk:
                        raise EOFError('connection closed')
                    rbuf += chunk
                    self.backoff = 0.0
                    # 从缓冲区解析完整帧（可能一次解析多帧）
                    while len(rbuf) >= 12:
                        val = int.from_bytes(rbuf[0:8], 'big')
                        size = int.from_bytes(rbuf[8:12], 'big')
                        config = (val >> 62) & 1
                        # 异常 size 防护：单帧过大视为字节流已错位，主动重连
                        if size <= 0 or size > 8 * 1024 * 1024:
                            raise EOFError('bad frame size %d' % size)
                        if len(rbuf) < 12 + size:
                            break   # 帧未收全，等更多数据
                        payload = rbuf[12:12 + size]
                        rbuf = rbuf[12 + size:]
                        if config:
                            continue   # 配置包(SPS/PPS)：关键帧已内联，跳过
                        if SCRCPY_STRIP_VUI:
                            payload = _strip_frame_sps_vui(payload)   # 剥离 SPS color VUI，修正偏蓝
                        try:
                            self.q.put(payload, timeout=2)
                        except queue.Full:
                            pass
            except socket.timeout as e:
                # accept 超时：server 未回连（可能启动失败/残留冲突），下一轮重新拉起
                print(f'[stream] {self.adb_addr} scrcpy accept timeout: {e}', flush=True)
            except Exception as e:
                print(f'[stream] {self.adb_addr} scrcpy stream error: {type(e).__name__}: {e}', flush=True)
            finally:
                try:
                    if conn:
                        conn.close()
                except Exception:
                    pass
                try:
                    srv.close()
                except Exception:
                    pass
                # server 残留清理：detached 下 pkill 本 scid，避免多实例叠加抢隧道
                if SCRCPY_DETACHED:
                    self._scrcpy_kill_server()
                else:
                    self._scrcpy_cleanup()
            if self.stop.is_set() or not self.subs:
                break
            if resized:
                # 分辨率切换：finally 已杀 server，下一轮 launch 会用新 max_size 重启
                time.sleep(0.2)
                continue
            self._fail_backoff('scrcpy stream ended')

    async def _send(self, ws, chunk):
        """发送一帧：慢客户端只会让该协程在事件循环里挂起，循环会切去服务其它客户端，
        不会误杀；只有连接真正断开(ws.send 抛异常)才会在回调里移除。"""
        await ws.send(chunk)

    def _on_send_done(self, fut, ws):
        with self.pending_lock:
            v = self._pending.get(ws, 0) - 1
            if v <= 0:
                self._pending.pop(ws, None)
            else:
                self._pending[ws] = v
        if fut.cancelled():
            return
        exc = fut.exception()
        if exc is not None and not isinstance(exc, asyncio.CancelledError):
            with self.sub_lock:
                self.subs.discard(ws)
            print(f'[stream] {self.adb_addr} client send failed ({type(exc).__name__}), closing', flush=True)
            try:
                asyncio.run_coroutine_threadsafe(ws.close(4003, 'send failed'), self.loop)
            except Exception:
                pass

    def _dispatch(self):
        """分发线程：从队列取分片，非阻塞扇出给所有订阅客户端。

        所有客户端共用同一个事件循环；若某客户端慢(后台标签/解码卡/网络抖)，其 ws.send
        协程会在循环里挂起，但循环会切换到其它客户端继续发送，不会卡死全体，也不会像旧版
        那样用 fut.result(timeout) 阻塞并 close(4003) 触发墙端「重连中」风暴。仅当连接真正
        断开(ws.send 抛异常)才移除并关闭。对半开/僵死连接用 _pending 上限限流，避免堆积。
        """
        last_auth = time.time()
        while not self.stop.is_set():
            try:
                chunk = self.q.get(timeout=0.5)
            except queue.Empty:
                if not self.subs:
                    break
                continue
            for ws in self._subs_snapshot():
                with self.pending_lock:
                    n = self._pending.get(ws, 0)
                    if n >= MAX_PENDING:
                        continue   # 该客户端落后太多，丢这一帧，不堆积
                    self._pending[ws] = n + 1
                try:
                    fut = asyncio.run_coroutine_threadsafe(self._send(ws, chunk), self.loop)
                    fut.add_done_callback(lambda f, w=ws: self._on_send_done(f, w))
                except Exception:
                    with self.pending_lock:
                        self._pending.pop(ws, None)
            # 周期性复查登录态：token 过期则断开
            now = time.time()
            if now - last_auth > 30:
                last_auth = now
                expired = [w for w in self._subs_snapshot()
                           if not auth.decode_token(getattr(w, '_myt_token', None))]
                for w in expired:
                    try:
                        self.subs.discard(w)
                    except Exception:
                        pass
                    try:
                        asyncio.run_coroutine_threadsafe(w.close(4001, 'token expired'), self.loop)
                    except Exception:
                        pass
            self.q.task_done()
        # 退出前排空队列
        while not self.q.empty():
            try:
                self.q.get_nowait()
            except Exception:
                break


async def handler(ws):
    from urllib.parse import urlparse, parse_qs
    path = ws.request.path if hasattr(ws, 'request') else '/'
    print(f'[relay] new connection, path={path}, type={type(ws).__name__}', flush=True)
    qs = parse_qs(urlparse(path).query)
    adb_addr = (qs.get('adb_addr') or [''])[0]
    if not adb_addr:
        print(f'[relay] missing adb_addr, closing', flush=True)
        await ws.close(4000, 'missing_adb_addr')
        return
    # 鉴权：校验登录 JWT
    token = _token_from_request(ws)
    if not auth.decode_token(token):
        print('[relay] unauthorized (no valid token), closing', flush=True)
        await ws.close(4001, 'unauthorized')
        return
    ws._myt_token = token

    loop = asyncio.get_running_loop()
    # 可选：按流指定分辨率(scrcpy max_size)，用于群控窗每格选择清晰度
    max_size_raw = (qs.get('max_size') or [''])[0]
    req_ms = None
    if max_size_raw:
        try:
            req_ms = max(240, min(1080, int(max_size_raw)))
        except Exception:
            req_ms = None
    # 方向 B：确保设备端 Agent 存活（Agent 出站 RTSP 推流，绕开 adb 隧道）
    # 部署 Agent 是阻塞操作（push + 启动），放到线程避免阻塞事件循环
    if USE_AGENT:
        threading.Thread(target=lambda: agent_mgr.deploy(adb_addr), daemon=True).start()
    with lock:
        s = streams.get(adb_addr)
        if s is None:
            s = Stream(adb_addr, loop)
            if req_ms:
                s._max_size = req_ms
            streams[adb_addr] = s
            if USE_AGENT:
                s.start_agent()   # Agent 模式：只启动 dispatcher，等 Agent 上行帧
            else:
                s.start()         # 传统模式：adb producer
        elif req_ms and req_ms != s._max_size:
            # 已存在的流：请求不同分辨率 -> 热切换(重启 producer 用新 max_size)
            s._max_size = req_ms
            s._want_resize = True
        s.add_sub(ws)
    try:
        # 同时：① 读取客户端下发的控制指令(文本帧) ② 等待关闭。
        # 视频帧由分发线程经本 WS 下发，与这里的 recv 互不阻塞。
        # 控制指令由 relay 用「同一个 adb server / 已建立的连接」下发 input，
        # 避免 web 容器另起 adb 连接与 scrcpy 采集抢占设备导致投屏中断。
        while True:
            try:
                msg = await ws.recv()
            except Exception:
                break
            if isinstance(msg, str):
                try:
                    d = json.loads(msg)
                except Exception:
                    d = None
                if isinstance(d, dict):
                    if d.get('type') == 'control':
                        await _handle_control(adb_addr, msg)
                    elif d.get('type') == 'request_idr':
                        # 墙端解码器卡顿/恢复时请求一次新关键帧：请求 IDR 即可，不重连
                        s.kick.set()
    finally:
        try:
            print(f'[relay] connection closed adb_addr={adb_addr} code={getattr(ws, "close_code", None)} reason={getattr(ws, "close_reason", None)}', flush=True)
        except Exception:
            pass
        with lock:
            s.remove_sub(ws)
            if not s.subs and streams.get(adb_addr) is s:
                streams.pop(adb_addr, None)


async def agent_handler(ws):
    """Agent 上行流端点：设备端 Agent 经 WebSocket 主动出站推送 H.264 帧。

    路径: /agent?adb_addr=xxx&token=xxx
    协议: 二进制帧（annexb H.264，12字节头+payload 已被 Agent 拆好，直接是 payload）。
    说明: 这是方向 A 的核心——画面数据不经过 adb 隧道，由 Agent 出站推送。
    认证: 用独立 Agent 共享密钥（AGENT_TOKEN），不用用户 JWT——Agent 是设备端受信组件。
    """
    from urllib.parse import urlparse, parse_qs
    path = ws.request.path if hasattr(ws, 'request') else '/'
    qs = parse_qs(urlparse(path).query)
    adb_addr = (qs.get('adb_addr') or [''])[0]
    if not adb_addr:
        await ws.close(4000, 'missing_adb_addr')
        return
    # Agent 认证：独立共享密钥（AGENT_TOKEN）
    token = (qs.get('token') or [''])[0]
    if AGENT_TOKEN and token != AGENT_TOKEN:
        print('[relay][agent] bad agent token, closing', flush=True)
        await ws.close(4001, 'unauthorized')
        return

    print(f'[relay][agent] agent connected adb_addr={adb_addr}', flush=True)
    agent_mgr.touch_subscriber(adb_addr)
    agent_mgr.set_state(adb_addr, status='running', stream_name=_agent_stream_name(adb_addr))
    loop = asyncio.get_running_loop()
    with lock:
        s = streams.get(adb_addr)
        if s is None or (s.stop.is_set() and s.producer is None):
            # 新建 Stream（不存在，或是 Agent 模式下已 stop 的旧 Stream 残留）
            s = Stream(adb_addr, loop)
            streams[adb_addr] = s
            s.start_agent()   # 只启动 dispatcher，不启动 producer/heartbeat
    try:
        while True:
            try:
                msg = await ws.recv()
            except Exception:
                break
            if isinstance(msg, (bytes, bytearray)):
                s.feed_frame(bytes(msg))
            # 文本消息忽略（保留扩展，如控制指令）
    finally:
        print(f'[relay][agent] agent disconnected adb_addr={adb_addr}', flush=True)
        # Agent 断开：停止 dispatcher，并从 streams 移除（Agent 模式无 producer）
        s.stop.set()
        with lock:
            if streams.get(adb_addr) is s:
                streams.pop(adb_addr, None)
        # 自愈：Agent 断开后，后台重新部署 Agent（Agent 会自动重连 /agent）
        # 避免「Agent 断了就永远黑屏，直到浏览器重连才恢复」
        threading.Thread(target=lambda: agent_mgr.deploy(adb_addr), daemon=True).start()


def build_input_cmd(adb_addr, d):
    """根据归一化坐标/参数构造设备端 `input` 命令；参数缺失/不支持返回 None。"""
    if not isinstance(d, dict):
        return None
    action = d.get('action')
    if action not in ('tap', 'swipe', 'key', 'text'):
        return None
    # 设备逻辑分辨率（实际屏幕像素），用于把归一化坐标换算成 input 坐标
    try:
        size = get_device_size(adb_addr) or (0, 0)
        w, h = size
    except Exception:
        w = h = 0
    if action == 'tap':
        if not (w and h):
            return None
        x = int(round(float(d.get('nx', 0)) * w))
        y = int(round(float(d.get('ny', 0)) * h))
        return f'input tap {x} {y}'
    if action == 'swipe':
        if not (w and h):
            return None
        x = int(round(float(d.get('nx', 0)) * w))
        y = int(round(float(d.get('ny', 0)) * h))
        x2 = int(round(float(d.get('nx2', d.get('nx', 0))) * w))
        y2 = int(round(float(d.get('ny2', d.get('ny', 0))) * h))
        dur = int(d.get('duration', 300))
        return f'input swipe {x} {y} {x2} {y2} {dur}'
    if action == 'key':
        return f"input keyevent {int(d.get('keycode', 0))}"
    if action == 'text':
        # adb shell 会把参数重新拼进设备端 shell 执行，必须整体加引号，
        # 否则空格/特殊字符会被拆散。CJK 依赖设备 IME，多数机型不支持（沿用原有限制）。
        txt = str(d.get('text', '')).replace('\n', ' ').replace('\r', ' ')
        return 'input text ' + shlex.quote(txt) if txt else None
    return None


async def _handle_control(adb_addr, msg):
    """处理墙端(WS)下发的触控/按键/输入指令：用 relay 自身的 adb 连接下发 input。"""
    try:
        d = json.loads(msg)
    except Exception:
        return
    if not isinstance(d, dict) or d.get('type') != 'control':
        return
    cmd = build_input_cmd(adb_addr, d)
    if not cmd:
        return
    try:
        # 复用生产者/心跳已建立并维持的连接，直接下发；不再每次 touch 前冗余 adb connect（省延迟、减 adb churn）
        await asyncio.to_thread(subprocess.run, [ADB, '-s', adb_addr, 'shell', cmd],
                               capture_output=True, timeout=10)
    except Exception as e:
        print(f'[control] {adb_addr} cmd failed: {e}', flush=True)


class _ControlHTTPHandler(http.server.BaseHTTPRequestHandler):
    """供 web 容器(及弹窗页)转发控制指令：与 scrcpy 采集同一 adb server，避免抢设备。"""

    def do_POST(self):
        if self.path.rstrip('/') != '/control':
            self.send_response(404)
            self.end_headers()
            return
        try:
            n = int(self.headers.get('Content-Length', 0) or 0)
            body = self.rfile.read(n) if n else b''
            d = json.loads(body.decode('utf-8', 'replace'))
        except Exception:
            self.send_response(400)
            self.end_headers()
            return
        adb_addr = (d.get('adb_addr') or '') if isinstance(d, dict) else ''
        if not adb_addr:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'missing adb_addr')
            return
        cmd = build_input_cmd(adb_addr, d)
        if not cmd:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'bad action/params')
            return
        try:
            r = subprocess.run([ADB, '-s', adb_addr, 'shell', cmd],
                               capture_output=True, timeout=10)
            ok = r.returncode == 0
        except Exception as e:
            ok = False
            print(f'[control-http] {adb_addr} failed: {e}', flush=True)
        if ok:
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"code":200}')
        else:
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"code":500,"msg":"adb send failed"}')

    def log_message(self, *a):
        pass


# ---- Agent 生命周期 HTTP 端点（必须在 _ControlHTTPHandler 定义之后扩展）----
_orig_do_POST = _ControlHTTPHandler.do_POST


def _agent_http_do_POST(self):
    path = self.path.rstrip('/')
    if path == '/agent/deploy':
        self._handle_agent_deploy()
        return
    if path == '/agent/stop':
        self._handle_agent_stop()
        return
    if path.startswith('/agent/status/'):
        addr = path[len('/agent/status/'):]
        self._handle_agent_status(addr)
        return
    _orig_do_POST(self)


def _agent_cmd(self, action):
    try:
        n = int(self.headers.get('Content-Length', 0) or 0)
        body = self.rfile.read(n) if n else b''
        d = json.loads(body.decode('utf-8', 'replace')) if body else {}
    except Exception:
        d = {}
    adb_addr = (d.get('adb_addr') or '') if isinstance(d, dict) else ''
    if not adb_addr:
        self._send_json(400, {'code': 400, 'msg': 'missing adb_addr'})
        return
    if action == 'deploy':
        ok, msg = agent_mgr.deploy(adb_addr, d.get('max_size') if isinstance(d, dict) else None)
    else:
        ok, msg = agent_mgr.stop(adb_addr)
    self._send_json(200 if ok else 500, {'code': 200 if ok else 500, 'msg': msg})


def _agent_status(self, adb_addr):
    st = agent_mgr.get_state(adb_addr)
    st['running_now'] = agent_mgr.is_running(adb_addr)
    if st['running_now'] and not st.get('stream_name'):
        st['stream_name'] = _agent_stream_name(adb_addr)
    stream = st.get('stream_name') or _agent_stream_name(adb_addr)
    st['stream_url_whep'] = f'{MEDIAMTX_WEBRTC_URL}/{stream}/whep'
    st['stream_url_rtsp'] = f'{MEDIAMTX_RTSP_URL}/{stream}'
    self._send_json(200, {'code': 200, 'data': st})


def _send_json(self, code, body):
    self.send_response(code)
    self.send_header('Content-Type', 'application/json')
    self.end_headers()
    self.wfile.write(json.dumps(body, ensure_ascii=False).encode('utf-8'))


_ControlHTTPHandler.do_POST = _agent_http_do_POST
_ControlHTTPHandler._handle_agent_deploy = lambda self: _agent_cmd(self, 'deploy')
_ControlHTTPHandler._handle_agent_stop = lambda self: _agent_cmd(self, 'stop')
_ControlHTTPHandler._handle_agent_status = lambda self, addr: _agent_status(self, addr)
_ControlHTTPHandler._send_json = _send_json


def _start_control_http():
    try:
        srv = http.server.ThreadingHTTPServer(('0.0.0.0', 9001), _ControlHTTPHandler)
        print('[relay] control http listening on :9001', flush=True)
        agent_mgr.start()
        srv.serve_forever()
    except Exception as e:
        print(f'[relay] control http failed: {e}', flush=True)


async def main():
    from websockets.asyncio.server import serve
    print(f'[relay][multithread {__version__}] Starting on ws://0.0.0.0:{WS_PORT}', flush=True)
    threading.Thread(target=_start_control_http, daemon=True).start()

    async def router(ws):
        """按路径分发：/agent 走 Agent 上行流，其余走浏览器投屏。"""
        path = ws.request.path if hasattr(ws, 'request') else '/'
        if path.startswith('/agent'):
            await agent_handler(ws)
        else:
            await handler(ws)

    async with serve(router, '0.0.0.0', WS_PORT, ping_interval=30, ping_timeout=120,
                     max_size=4 * 1024 * 1024, write_limit=4 * 1024 * 1024):
        await asyncio.Future()


if __name__ == '__main__':
    asyncio.run(main())
