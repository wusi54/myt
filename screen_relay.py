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
import asyncio
import threading
import queue
import subprocess
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
SEND_TIMEOUT = float(os.environ.get('RELAY_SEND_TIMEOUT', '2'))  # 单客户端发送超时(秒)

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


def get_device_size(adb_addr):
    """读取设备(容器)逻辑分辨率，Override 优先，其次 Physical。"""
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
            return pair
        physical = physical or pair
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


class Stream:
    """同一 adb_addr 的共享投屏流：一个 screenrecord 进程 + 多线程 + 队列广播。"""

    def __init__(self, adb_addr, loop):
        self.adb_addr = adb_addr
        self.loop = loop                      # 主事件循环，用于跨线程调度 ws.send
        self.q = queue.Queue(maxsize=QMAX)    # 生产者 -> 分发者 的有界缓冲
        self.subs = set()                     # 订阅该设备的客户端 WebSocket
        self.sub_lock = threading.Lock()
        self.stop = threading.Event()
        self.kick = threading.Event()         # 触发重启 screenrecord 以吐 IDR（后加入观众）
        self.last_start = 0.0
        self.producer = None
        self.dispatcher = None
        self.heartbeat = None

    def start(self):
        self.producer = threading.Thread(target=self._produce, daemon=True,
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

    def _produce(self):
        """生产者线程：adb connect -> screenrecord -> 读 stdout 入队。"""
        while not self.stop.is_set():
            if not self.subs:
                time.sleep(0.2)
                continue
            # adb connect（阻塞调用，放在线程里不阻塞事件循环）
            connected = False
            for attempt in range(1, 6):
                if self.stop.is_set() or not self.subs:
                    return
                try:
                    r = subprocess.run([ADB, 'connect', self.adb_addr],
                                       capture_output=True, timeout=10)
                    out = (r.stdout or b'').decode(errors='replace')
                except Exception as e:
                    out = ''
                    print(f'[stream] {self.adb_addr} adb connect exception: {e}', flush=True)
                if 'connected' in out or 'already' in out:
                    connected = True
                    break
                print(f'[stream] {self.adb_addr} adb connect failed (attempt {attempt}/5): {out.strip()}', flush=True)
                time.sleep(3)
            if not connected:
                print(f'[stream] {self.adb_addr} adb connect failed after retries', flush=True)
                for ws in self._subs_snapshot():
                    try:
                        asyncio.run_coroutine_threadsafe(ws.close(4002, 'adb connect failed'), self.loop)
                    except Exception:
                        pass
                return

            out_size = scaled_size(self.adb_addr)
            print(f'[stream] {self.adb_addr} output {out_size[0]}x{out_size[1]} @ {SCREEN_BITRATE}bps', flush=True)

            while not self.stop.is_set() and self.subs:
                try:
                    proc = subprocess.Popen(
                        [ADB, '-s', self.adb_addr, 'exec-out', 'screenrecord',
                         '--output-format=h264', '--size', f'{out_size[0]}x{out_size[1]}',
                         '--bit-rate', str(SCREEN_BITRATE), '--time-limit', str(SCREEN_LIMIT), '-'],
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                except Exception as e:
                    print(f'[stream] {self.adb_addr} screenrecord start error: {e}', flush=True)
                    time.sleep(1)
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
                            print(f'[stream] {self.adb_addr} stdout EOF', flush=True)
                            break
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
                if self.stop.is_set() or not self.subs:
                    break
                time.sleep(0.2)   # screenrecord 到时退出后自动重启，保持持续推流
            # 内层退出：可能是无订阅者/被停止 -> 外层 while 再判断

    def _dispatch(self):
        """分发线程：从队列取分片，并发扇出给所有订阅客户端。"""
        last_auth = time.time()
        while not self.stop.is_set():
            try:
                chunk = self.q.get(timeout=0.5)
            except queue.Empty:
                if not self.subs:
                    break
                continue
            subs = self._subs_snapshot()
            # 并发调度所有客户端的发送，避免单慢客户端拖住全体
            futs = []
            for ws in subs:
                try:
                    futs.append((ws, asyncio.run_coroutine_threadsafe(ws.send(chunk), self.loop)))
                except Exception:
                    pass
            dead = []
            for ws, fut in futs:
                try:
                    fut.result(timeout=SEND_TIMEOUT)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                try:
                    self.subs.discard(ws)
                except Exception:
                    pass
                try:
                    asyncio.run_coroutine_threadsafe(ws.close(4003, 'send failed'), self.loop)
                except Exception:
                    pass
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
    with lock:
        s = streams.get(adb_addr)
        if s is None:
            s = Stream(adb_addr, loop)
            streams[adb_addr] = s
            s.start()
        s.add_sub(ws)
    try:
        await ws.wait_closed()
    except Exception:
        pass
    finally:
        with lock:
            s.remove_sub(ws)
            if not s.subs and streams.get(adb_addr) is s:
                streams.pop(adb_addr, None)


async def main():
    from websockets.asyncio.server import serve
    print(f'[relay][multithread {__version__}] Starting on ws://0.0.0.0:{WS_PORT}', flush=True)
    async with serve(handler, '0.0.0.0', WS_PORT, ping_interval=20, ping_timeout=10):
        await asyncio.Future()


if __name__ == '__main__':
    asyncio.run(main())
