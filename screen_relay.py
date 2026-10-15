import os
import re
import asyncio
import subprocess

WS_PORT = int(os.environ.get('RELAY_PORT', '9000'))
ADB = os.environ.get('ADB_PATH') or (
    r"C:\Users\wusi0\AppData\Local\Android\Sdk\platform-tools\adb.exe"
    if os.name == 'nt' else 'adb')
SCREEN_LIMIT = int(os.environ.get('SCREEN_LIMIT', '180'))          # 单段最长录制秒数
SCREEN_MAX = int(os.environ.get('SCREEN_MAX', '640'))              # 输出最长边（等比缩放）
SCREEN_BITRATE = int(os.environ.get('SCREEN_BITRATE', '300000'))  # 码率 bps
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
            subprocess.run([ADB, 'connect', adb_addr],
                           capture_output=True, timeout=5)
        except:
            pass

async def stream_worker(ws, adb_addr):
    r = subprocess.run([ADB, 'connect', adb_addr], capture_output=True, timeout=10)
    out = r.stdout.decode(errors='replace')
    if 'connected' not in out and 'already' not in out:
        print(f'[stream] adb connect failed: {out}', flush=True)
        await ws.close(4002, f'adb connect failed: {out}')
        return
    print(f'[stream] adb connected: {adb_addr}', flush=True)

    out_size = await asyncio.to_thread(scaled_size, adb_addr)
    print(f'[stream] {adb_addr} output {out_size[0]}x{out_size[1]} @ {SCREEN_BITRATE}bps', flush=True)

    heartbeat = asyncio.create_task(adb_heartbeat(adb_addr))
    restart_count = 0

    try:
        while True:
            print(f'[stream] {adb_addr} starting screenrecord (attempt {restart_count + 1})', flush=True)
            proc = await asyncio.create_subprocess_exec(
                ADB, '-s', adb_addr, 'exec-out',
                'screenrecord', '--output-format=h264',
                '--size', f'{out_size[0]}x{out_size[1]}',
                '--bit-rate', str(SCREEN_BITRATE),
                '--time-limit', str(SCREEN_LIMIT), '-',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            active_streams[adb_addr] = proc

            ws_alive = True
            try:
                while True:
                    chunk = await proc.stdout.read(65536)
                    if not chunk:
                        print(f'[stream] {adb_addr} stdout EOF, stream ended', flush=True)
                        break
                    await ws.send(chunk)
            except Exception as e:
                ws_alive = False
                print(f'[stream] {adb_addr} send error: {e}', flush=True)

            try:
                proc.kill()
            except:
                pass

            if not ws_alive:
                print(f'[stream] {adb_addr} client disconnected', flush=True)
                break

            await proc.wait()
            err = await proc.stderr.read()
            if err:
                print(f'[stream] {adb_addr} stderr: {err.decode(errors="replace")}', flush=True)

            restart_count += 1
            print(f'[stream] {adb_addr} screenrecord ended, restarting in 1s...', flush=True)
            await asyncio.sleep(1)

    except Exception as e:
        print(f'[stream] {adb_addr} error: {e}', flush=True)
    finally:
        heartbeat.cancel()
        active_streams.pop(adb_addr, None)

async def handler(ws):
    from urllib.parse import urlparse, parse_qs
    path = ws.request.path if hasattr(ws, 'request') else '/'
    print(f'[relay] new connection, path={path}, type={type(ws).__name__}', flush=True)
    qs = parse_qs(urlparse(path).query)
    adb_addr = (qs.get('adb_addr') or [''])[0]
    if not adb_addr:
        print(f'[relay] missing adb_addr, closing', flush=True)
        await ws.close(4000, 'missing adb_addr')
        return
    if adb_addr in active_streams:
        print(f'[relay] {adb_addr} already streaming, closing', flush=True)
        await ws.close(4001, 'already streaming')
        return
    print(f'[relay] starting stream for {adb_addr}', flush=True)
    await stream_worker(ws, adb_addr)

async def main():
    from websockets.asyncio.server import serve
    print(f'[relay] Starting on ws://0.0.0.0:{WS_PORT}', flush=True)
    async with serve(handler, '0.0.0.0', WS_PORT, ping_interval=20, ping_timeout=10):
        await asyncio.Future()

if __name__ == '__main__':
    asyncio.run(main())
