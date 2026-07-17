import asyncio
import subprocess
import signal
import os

WS_PORT = 9000
ADB = r"C:\Users\wusi0\AppData\Local\Android\Sdk\platform-tools\adb.exe"
active_streams = {}

async def adb_heartbeat(adb_addr):
    while True:
        await asyncio.sleep(30)
        try:
            subprocess.run([ADB, 'connect', adb_addr],
                           capture_output=True, timeout=5)
        except:
            pass

async def stream_worker(ws, adb_addr):
    # 连接 ADB，检查是否成功
    r = subprocess.run([ADB, 'connect', adb_addr], capture_output=True, timeout=10)
    out = r.stdout.decode(errors='replace')
    if 'connected' not in out and 'already' not in out:
        print(f'[stream] adb connect failed: {out}', flush=True)
        await ws.close(4002, f'adb connect failed: {out}')
        return
    print(f'[stream] adb connected: {adb_addr}', flush=True)

    heartbeat = asyncio.create_task(adb_heartbeat(adb_addr))

    proc = await asyncio.create_subprocess_exec(
        ADB, '-s', adb_addr, 'exec-out',
        'screenrecord', '--output-format=h264',
        '--size', '480x720',
        '--bit-rate', '500000',
        '--time-limit', '180', '-',
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    active_streams[adb_addr] = proc
    # 等待第一帧，超时15秒则退出
    try:
        first = await asyncio.wait_for(proc.stdout.read(65536), timeout=15)
        if first:
            await ws.send(first)
        else:
            await ws.close(4003, 'no video data')
            return
    except asyncio.TimeoutError:
        print(f'[stream] {adb_addr} timeout waiting for first frame', flush=True)
        await ws.close(4003, 'screenrecord timeout')
        proc.kill()
        return
    try:
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                break
            await ws.send(chunk)
        await proc.wait()
        err = await proc.stderr.read()
        if err:
            print(f'[stream] {adb_addr} stderr: {err.decode(errors="replace")}', flush=True)
    except Exception as e:
        print(f'[stream] {adb_addr} error: {e}', flush=True)
        try: proc.kill()
        except: pass
    finally:
        heartbeat.cancel()
        active_streams.pop(adb_addr, None)

async def handler(ws):
    from urllib.parse import urlparse, parse_qs
    path = ws.request.path if ws.request else '/'
    qs = parse_qs(urlparse(path).query)
    adb_addr = (qs.get('adb_addr') or [''])[0]
    if not adb_addr:
        await ws.close(4000, 'missing adb_addr')
        return
    if adb_addr in active_streams:
        await ws.close(4001, 'already streaming')
        return
    await stream_worker(ws, adb_addr)

async def main():
    import websockets
    print(f'[relay] Starting on ws://0.0.0.0:{WS_PORT}', flush=True)
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try: loop.add_signal_handler(sig, lambda: stop.set() if not stop.is_set() else None)
        except: pass
    async with websockets.serve(handler, '0.0.0.0', WS_PORT):
        await stop.wait()
    for p in active_streams.values():
        try: p.kill()
        except: pass

if __name__ == '__main__':
    asyncio.run(main())