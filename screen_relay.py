import os
import re
import asyncio
import subprocess

WS_PORT = int(os.environ.get('RELAY_PORT', '9000'))
ADB = os.environ.get('ADB_PATH') or (
    r"C:\Users\wusi0\AppData\Local\Android\Sdk\platform-tools\adb.exe"
    if os.name == 'nt' else 'adb')
SCREEN_LIMIT = int(os.environ.get('SCREEN_LIMIT', '180'))          # 单段最长录制秒数
SCREEN_MAX = int(os.environ.get('SCREEN_MAX', '480'))              # 输出最长边（等比缩放）
SCREEN_BITRATE = int(os.environ.get('SCREEN_BITRATE', '150000'))  # 码率 bps
DEFAULT_SIZE = (360, 640)                                          # 取不到分辨率时兜底(9:16)
active_streams = {}


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
    size = get_device_size(adb_addr)
    if not size:
        return DEFAULT_SIZE
    w, h = size
    if max(w, h) <= 0:
        return DEFAULT_SIZE
    k = min(1.0, SCREEN_MAX / max(w, h))
    return (_even(w * k), _even(h * k))

async def adb_heartbeat(adb_addr):
    while True:
        await asyncio.sleep(30)
        try:
            # 必须用 to_thread，否则阻塞事件循环会导致所有客户端 ping/pong 超时断线
            await asyncio.to_thread(subprocess.run, [ADB, 'connect', adb_addr],
                                   capture_output=True, timeout=5)
        except:
            pass

class StreamEntry:
    """同一 adb_addr 的共享投屏流：一个 screenrecord 进程，向多个客户端广播。"""
    def __init__(self):
        self.subs = set()      # 所有订阅该设备的客户端 WebSocket
        self.task = None       # 广播协程任务
        self.proc = None       # 当前 screenrecord 子进程


async def _broadcast_loop(adb_addr, entry):
    """为单个 adb_addr 启动 screenrecord，并把输出广播给所有订阅者。"""
    # adb connect 必须放到线程里执行：同步 subprocess.run 会阻塞整个事件循环，
    # 导致其他客户端的 ping/pong 无法被处理而被服务端超时关闭（断线风暴）。
    connected = False
    for attempt in range(1, 4):
        try:
            r = await asyncio.to_thread(subprocess.run, [ADB, 'connect', adb_addr],
                                        capture_output=True, timeout=10)
        except Exception as e:
            print(f'[stream] {adb_addr} adb connect exception: {e}', flush=True)
            r = None
        out = (r.stdout.decode(errors='replace') if r else '')
        if r and ('connected' in out or 'already' in out):
            connected = True
            break
        print(f'[stream] {adb_addr} adb connect failed (attempt {attempt}/3): {out.strip()}', flush=True)
        await asyncio.sleep(2)
    if not connected:
        print(f'[stream] {adb_addr} adb connect failed after retries', flush=True)
        for ws in list(entry.subs):
            try:
                await ws.close(4002, 'adb connect failed')
            except Exception:
                pass
        return
    print(f'[stream] {adb_addr} adb connected', flush=True)
    out_size = await asyncio.to_thread(scaled_size, adb_addr)
    print(f'[stream] {adb_addr} output {out_size[0]}x{out_size[1]} @ {SCREEN_BITRATE}bps', flush=True)

    heartbeat = asyncio.create_task(adb_heartbeat(adb_addr))
    try:
        while entry.subs:                      # 只要有订阅者就持续推流
            print(f'[stream] {adb_addr} starting screenrecord', flush=True)
            proc = await asyncio.create_subprocess_exec(
                ADB, '-s', adb_addr, 'exec-out', 'screenrecord', '--output-format=h264',
                '--size', f'{out_size[0]}x{out_size[1]}', '--bit-rate', str(SCREEN_BITRATE),
                '--time-limit', str(SCREEN_LIMIT), '-',
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            entry.proc = proc
            try:
                while True:
                    chunk = await proc.stdout.read(65536)
                    if not chunk:
                        print(f'[stream] {adb_addr} stdout EOF', flush=True)
                        break
                    dead = []
                    for ws in list(entry.subs):
                        try:
                            await ws.send(chunk)
                        except Exception:
                            dead.append(ws)
                    for ws in dead:
                        entry.subs.discard(ws)
            except Exception as e:
                print(f'[stream] {adb_addr} send error: {e}', flush=True)
            finally:
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    await proc.wait()
                except Exception:
                    pass
            if not entry.subs:
                break
            await asyncio.sleep(0.2)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f'[stream] {adb_addr} error: {e}', flush=True)
    finally:
        heartbeat.cancel()
        entry.proc = None
        if not entry.subs and active_streams.get(adb_addr) is entry:
            active_streams.pop(adb_addr, None)


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
    # 同一 adb_addr 允许多客户端共享：复用已有广播流，不再拒绝
    entry = active_streams.get(adb_addr)
    if entry is None:
        entry = StreamEntry()
        active_streams[adb_addr] = entry
        entry.task = asyncio.create_task(_broadcast_loop(adb_addr, entry))
    entry.subs.add(ws)
    try:
        await ws.wait_closed()
    except Exception:
        pass
    finally:
        entry.subs.discard(ws)
        if not entry.subs:
            if entry.task and not entry.task.done():
                entry.task.cancel()
            if active_streams.get(adb_addr) is entry:
                active_streams.pop(adb_addr, None)


async def main():
    from websockets.asyncio.server import serve
    print(f'[relay] Starting on ws://0.0.0.0:{WS_PORT}', flush=True)
    async with serve(handler, '0.0.0.0', WS_PORT, ping_interval=20, ping_timeout=10):
        await asyncio.Future()


if __name__ == '__main__':
    asyncio.run(main())
