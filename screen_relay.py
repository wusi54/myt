import asyncio
import subprocess
import signal
import os

WS_PORT = 9000
ADB_PATH = r"C:\Users\wusi0\AppData\Local\Android\Sdk\platform-tools\adb.exe"
active_streams = {}

async def stream_worker(ws, adb_addr):
    proc = await asyncio.create_subprocess_exec(
        ADB_PATH, '-s', adb_addr, 'exec-out',
        'screenrecord', '--output-format=h264',
        '--size', '480x720',
        '--bit-rate', '500000',
        '--time-limit', '180', '-',
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL
    )
    active_streams[adb_addr] = proc
    try:
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                break
            await ws.send(chunk)
        await proc.wait()
    except:
        try: proc.kill()
        except: pass
    finally:
        active_streams.pop(adb_addr, None)

async def handler(ws, path):
    from urllib.parse import urlparse, parse_qs
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
    print(f'Screen relay on ws://0.0.0.0:{WS_PORT}')
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