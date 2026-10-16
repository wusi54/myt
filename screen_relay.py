"""多线程 + 队列 版投屏中继（dev_multithread_2.1.0 对比测试用）。

与 asyncio 协程版的关键区别：
- 每个 adb_addr 起 1 个「生产者线程」跑 screenrecord 并读 stdout，把 H.264 分片塞进一个
  有界 queue.Queue（背压：队列满则丢弃分片，避免内存爆）；
- 另起 1 个「分发线程」从队列取分片，用 asyncio.run_coroutine_threadsafe 把同一分片
  扇出给所有订阅该设备的客户端 WebSocket；
- 心跳、鉴权、token 过期剔除等都在各自线程里完成，事件循环只负责 WS 收发。

协议/端口/WS 路径(?adb_addr=) 与旧版完全一致，便于同 asyncio 版做 AB 对比。
"""
import os
import re
import time
import json
import asyncio
import threading
import queue
import subprocess
import socket
import random
import http.server
import auth

__version__ = '2.1.0-multithread'

WS_PORT = int(os.environ.get('RELAY_PORT', '9000'))
ADB = os.environ.get('ADB_PATH') or (
    r"C:\Users\wusi0\AppData\Local\Android\Sdk\platform-tools\adb.exe"
    if os.name == 'nt' else 'adb')
SCREEN_LIMIT = int(os.environ.get('SCREEN_LIMIT', '180'))          # 单段最长录制秒数
SCREEN_MAX = int(os.environ.get('SCREEN_MAX', '480'))              # 输出最长边（等比缩放）
SCREEN_BITRATE = int(os.environ.get('SCREEN_BITRATE', '150000'))  # 码率 bps
DEFAULT_SIZE = (360, 640)                                          # 取不到分辨率时兜底(9:16)

QMAX = int(os.environ.get('RELAY_QMAX', '60'))        # 广播队列上限（分片）
SEND_TIMEOUT = float(os.environ.get('RELAY_SEND_TIMEOUT', '2'))  # 保留(兼容环境变量)
MAX_PENDING = int(os.environ.get('RELAY_MAX_PENDING', '30'))       # 单客户端最大在途帧，超过则丢帧限流

# ---- scrcpy 单隧道采屏 ----
# 用 scrcpy-server 取代 screenrecord：一条 adb reverse 隧道同时承载画面(annexb H.264)。
# 控制(P2)将复用同一隧道。开关便于回退到 screenrecord。
USE_SCRCPY = os.environ.get('RELAY_SCRCPY', '1') == '1'
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
# 剥掉后浏览器用默认色彩转换(与早年 screenrecord 一致、显示正常)。RELAY_STRIP_VUI=0 可关闭。
SCRCPY_STRIP_VUI = os.environ.get('RELAY_STRIP_VUI', '0') == '1'

streams = {}                 # adb_addr -> Stream
lock = threading.Lock()      # 保护 streams 字典
SIZE_CACHE = {}              # adb_addr -> (size_tuple, expire_ts)
SIZE_CACHE_TTL = 300


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


def _even(v):
    v = int(round(v))
    return v - (v % 2) or 2


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


def scaled_size(adb_addr):
    """按设备分辨率等比缩放到最长边 SCREEN_MAX，宽高取偶数（H.264 要求）。"""
    cached = SIZE_CACHE.get(adb_addr)
    if cached and cached[1] > time.time():
        return cached[0]
    size = get_device_size(adb_addr)
    if not size:
        return DEFAULT_SIZE
    w, h = size
    if max(w, h) <= 0:
        return DEFAULT_SIZE
    k = min(1.0, SCREEN_MAX / max(w, h))
    out = (_even(w * k), _even(h * k))
    SIZE_CACHE[adb_addr] = (out, time.time() + SIZE_CACHE_TTL)
    return out


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
    """同一 adb_addr 的共享投屏流：一个 screenrecord 进程 + 多线程 + 队列广播。"""

    def __init__(self, adb_addr, loop):
        self.adb_addr = adb_addr
        self.loop = loop                      # 主事件循环，用于跨线程调度 ws.send
        self.q = queue.Queue(maxsize=QMAX)    # 生产者 -> 分发者 的有界缓冲
        self.subs = set()                     # 订阅该设备的客户端 WebSocket
        self.sub_lock = threading.Lock()
        self.pending_lock = threading.Lock()
        self._pending = {}            # ws -> 在途帧数(防止半开/慢客户端堆积)
        self.stop = threading.Event()
        self.kick = threading.Event()         # 触发重启 screenrecord 以吐 IDR（后加入观众）
        self.last_start = 0.0
        self.backoff = 0.0                  # 失败退避时间(秒)，成功出流后归零
        self._last_sr_err = ''
        self._max_size = SCREEN_MAX         # scrcpy max_size（输出最长边）
        self._want_resize = False           # 运行中热切换分辨率标志
        self.producer = None
        self.dispatcher = None
        self.heartbeat = None

    def start(self):
        base = self._produce_scrcpy if USE_SCRCPY else self._produce

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

    # ---- 订阅管理 ----
    def add_sub(self, ws):
        with self.sub_lock:
            first = len(self.subs) == 0
            self.subs.add(ws)
        # 后加入者错过当前 IDR，重启 screenrecord 让其快速出画（0.3s 去抖）
        if not first and self.last_start and (time.time() - self.last_start) > 0.3:
            self.kick.set()

    def remove_sub(self, ws):
        with self.sub_lock:
            self.subs.discard(ws)
            empty = len(self.subs) == 0
        if empty:
            self.stop.set()

    def _subs_snapshot(self):
        with self.sub_lock:
            return list(self.subs)

    def _heartbeat(self):
        while not self.stop.is_set():
            time.sleep(30)
            if self.stop.is_set():
                break
            try:
                subprocess.run([ADB, 'connect', self.adb_addr],
                               capture_output=True, timeout=5)
            except Exception:
                pass

    def _fail_backoff(self, err):
        """screenrecord/adb 失败后退避重试：offline/closed 时先 reconnect，再按指数退避(2..30s)，
        避免对掉线设备 0.2s 猛锤而制造重连风暴。"""
        e = (err or '').lower()
        offline = ('offline' in e) or ('closed' in e) or ('not found' in e) or ('timed out' in e) or ('refused' in e)
        if offline:
            try:
                subprocess.run([ADB, 'disconnect', self.adb_addr], capture_output=True, timeout=5)
            except Exception:
                pass
            try:
                subprocess.run([ADB, 'connect', self.adb_addr], capture_output=True, timeout=10)
            except Exception:
                pass
        if self.backoff <= 0:
            self.backoff = 2.0
        else:
            self.backoff = min(self.backoff * 2, 30.0)
        print(f'[stream] {self.adb_addr} backoff {self.backoff:.0f}s (offline={offline})', flush=True)
        time.sleep(self.backoff)

    def _produce(self):
        """生产者线程：adb connect -> screenrecord -> 读 stdout 入队；失败时退避重试，不掉线猛锤。"""
        while not self.stop.is_set():
            if not self.subs:
                time.sleep(0.2)
                continue
            # adb connect（阻塞调用，放在线程里不阻塞事件循环）；失败则退避重试并保持 WS，不制造重连风暴
            connected = False
            for attempt in range(1, 3):
                if self.stop.is_set() or not self.subs:
                    return
                try:
                    r = subprocess.run([ADB, 'connect', self.adb_addr],
                                       capture_output=True, timeout=5)
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
                print(f'[stream] {self.adb_addr} adb connect failed after retries', flush=True)
                self._fail_backoff(out)   # 退避重试并保持 WS，设备恢复后自动续流
                continue

            out_size = scaled_size(self.adb_addr)
            print(f'[stream] {self.adb_addr} output {out_size[0]}x{out_size[1]} @ {SCREEN_BITRATE}bps', flush=True)

            eof = False
            while not self.stop.is_set() and self.subs:
                # 清掉设备上可能残留的上一轮 screenrecord，避免 "already running" 导致新进程秒退
                try:
                    subprocess.run([ADB, '-s', self.adb_addr, 'shell', 'pkill', '-f', 'screenrecord'],
                                   capture_output=True, timeout=5)
                except Exception:
                    pass
                try:
                    proc = subprocess.Popen(
                        [ADB, '-s', self.adb_addr, 'exec-out', 'screenrecord',
                         '--output-format=h264', '--size', f'{out_size[0]}x{out_size[1]}',
                         '--bit-rate', str(SCREEN_BITRATE), '--time-limit', str(SCREEN_LIMIT), '-'],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                except Exception as e:
                    print(f'[stream] {self.adb_addr} screenrecord start error: {e}', flush=True)
                    self._fail_backoff(str(e))
                    continue
                self.last_start = time.time()
                self.kick.clear()
                print(f'[stream] {self.adb_addr} screenrecord started', flush=True)
                try:
                    while not self.stop.is_set():
                        if not self.subs:
                            break
                        if self.kick.is_set():
                            print(f'[stream] {self.adb_addr} kick -> restart for IDR', flush=True)
                            break
                        chunk = proc.stdout.read(65536)
                        if not chunk:
                            eof = True
                            print(f'[stream] {self.adb_addr} stdout EOF', flush=True)
                            break
                        self.backoff = 0.0   # 真正读到数据才归零退避；持续离线的设备会退避到 30s，避免每 2s 猛锤 flaky 网关
                        try:
                            self.q.put(chunk, timeout=2)   # 队列满则等待(背压)
                        except queue.Full:
                            pass                          # 或丢弃：分发者落后时避免堆积
                finally:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    try:
                        proc.wait(timeout=5)
                    except Exception:
                        pass
                    # 诊断：screenrecord 秒退时把设备端报错打出来（同一错误去重，避免刷屏）
                    try:
                        err = (proc.stderr.read(8192) or b'').decode(errors='replace')
                    except Exception:
                        err = ''
                    err = err.strip()
                    if err and err != self._last_sr_err:
                        self._last_sr_err = err
                        print(f'[stream] {self.adb_addr} screenrecord stderr: {err!r}', flush=True)
                if self.stop.is_set() or not self.subs:
                    break
                if eof:
                    # 设备掉线/管道断开 -> 退避重试(含 reconnect)，保持 WS 不制造重连风暴
                    self._fail_backoff(self._last_sr_err)
                else:
                    time.sleep(0.2)   # kick 等正常重启，短暂等待
            # 内层退出：可能是无订阅者/被停止 -> 外层 while 再判断

    # ---- scrcpy 单隧道采屏 ----
    def _scrcpy_cleanup(self):
        """清理设备上残留的 scrcpy server 与反向隧道；残留会叠加采屏压垮 adbd。"""
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

    def _produce_scrcpy(self):
        """生产者线程：adb reverse 隧道启动 scrcpy-server，读 annexb H.264 帧入队。
        scrcpy 单隧道承载画面(控制 P2 复用)；关键帧自带 SPS/PPS，媒体帧直接入队(零转换)。"""
        while not self.stop.is_set():
            if not self.subs:
                time.sleep(0.2)
                continue
            # adb connect
            connected = False
            out = ''
            for attempt in range(1, 3):
                if self.stop.is_set() or not self.subs:
                    return
                try:
                    r = subprocess.run([ADB, 'connect', self.adb_addr], capture_output=True, timeout=5)
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

            # 推送 server jar（每次重试都确保在位，版本必须匹配）
            try:
                subprocess.run([ADB, '-s', self.adb_addr, 'push', SCRCPY_SERVER_LOCAL, DEVICE_SERVER_JAR],
                               capture_output=True, timeout=30)
            except Exception as e:
                print(f'[stream] {self.adb_addr} scrcpy push server error: {e}', flush=True)

            self._scrcpy_cleanup()
            scid = '%08x' % random.randint(1, 0x7fffffff)
            sockname = 'scrcpy_' + scid
            hostport = 47000 + random.randint(0, 15000)
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
                self._scrcpy_cleanup()
                self._fail_backoff(str(e))
                continue
            srv.settimeout(30)
            # 默认(非 tunnel_forward)：server 主动回连抽象 socket；无 dummy byte
            server_cmd = [ADB, '-s', self.adb_addr, 'shell', 'CLASSPATH=' + DEVICE_SERVER_JAR,
                          'app_process', '/', 'com.genymobile.scrcpy.Server', SCRCPY_VERSION,
                          'scid=' + scid, 'log_level=warn', 'video=true', 'audio=false',
                          'control=false', 'send_device_meta=false', 'send_stream_meta=true',
                          'send_frame_meta=true', 'max_size=%d' % self._max_size,
                          'video_bit_rate=%d' % SCREEN_BITRATE]
            logf = open('/tmp/scrcpy_server_%s.log' % self.adb_addr.replace(':', '_').replace('.', '_'), 'ab')
            self.last_start = time.time()
            self.kick.clear()
            try:
                proc = subprocess.Popen(server_cmd, stdout=logf, stderr=logf)
            except Exception as e:
                print(f'[stream] {self.adb_addr} scrcpy server start error: {e}', flush=True)
                logf.close()
                try:
                    srv.close()
                except Exception:
                    pass
                self._scrcpy_cleanup()
                self._fail_backoff(str(e))
                continue
            print(f'[stream] {self.adb_addr} scrcpy server starting (scid={scid} port={hostport})', flush=True)
            conn = None
            resized = False          # 务必在 try 外初始化：早期异常(accept/codec 超时)时下面 if resized 才不会 UnboundLocalError 崩线程
            try:
                conn, _ = srv.accept()          # server 主动回连 -> video socket
                conn.settimeout(30)
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
                    hdr = self._recvn(conn, 12)
                    if len(hdr) < 12:
                        raise EOFError('short frame header')
                    val = int.from_bytes(hdr[0:8], 'big')
                    size = int.from_bytes(hdr[8:12], 'big')
                    config = (val >> 62) & 1
                    if size == 0:
                        continue
                    payload = self._recvn(conn, size)
                    if len(payload) < size:
                        raise EOFError('short frame payload')
                    if config:
                        continue   # 配置包(SPS/PPS)：关键帧已内联，跳过
                    if SCRCPY_STRIP_VUI:
                        payload = _strip_frame_sps_vui(payload)   # 剥离 SPS color VUI，修正偏蓝
                    self.backoff = 0.0
                    try:
                        self.q.put(payload, timeout=2)
                    except queue.Full:
                        pass
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
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    logf.close()
                except Exception:
                    pass
                self._scrcpy_cleanup()
            if self.stop.is_set() or not self.subs:
                break
            if resized:
                time.sleep(0.2)   # 热切换分辨率：立即用新 max_size 重启，不退避
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
    with lock:
        s = streams.get(adb_addr)
        if s is None:
            s = Stream(adb_addr, loop)
            if req_ms:
                s._max_size = req_ms
            streams[adb_addr] = s
            s.start()
        elif req_ms and req_ms != s._max_size:
            # 已存在的流：请求不同分辨率 -> 热切换(重启 producer 用新 max_size)
            s._max_size = req_ms
            s._want_resize = True
        s.add_sub(ws)
    try:
        # 同时：① 读取客户端下发的控制指令(文本帧) ② 等待关闭。
        # 视频帧由分发线程经本 WS 下发，与这里的 recv 互不阻塞。
        # 控制指令由 relay 用「同一个 adb server / 已建立的连接」下发 input，
        # 避免 web 容器另起 adb 连接与 screenrecord 抢占设备导致投屏中断。
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
                        # 墙端解码器卡顿/恢复时请求一次新关键帧：重启 screenrecord 即可，不重连
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
        return f"input text {d.get('text', '')}"
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
    """供 web 容器(及弹窗页)转发控制指令：与 screenrecord 同一 adb server，避免抢设备。"""

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


def _start_control_http():
    try:
        srv = http.server.ThreadingHTTPServer(('0.0.0.0', 9001), _ControlHTTPHandler)
        print('[relay] control http listening on :9001', flush=True)
        srv.serve_forever()
    except Exception as e:
        print(f'[relay] control http failed: {e}', flush=True)


async def main():
    from websockets.asyncio.server import serve
    print(f'[relay][multithread {__version__}] Starting on ws://0.0.0.0:{WS_PORT}', flush=True)
    threading.Thread(target=_start_control_http, daemon=True).start()
    async with serve(handler, '0.0.0.0', WS_PORT, ping_interval=30, ping_timeout=120):
        await asyncio.Future()


if __name__ == '__main__':
    asyncio.run(main())
