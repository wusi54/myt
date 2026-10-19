# -*- coding: utf-8 -*-
"""把本地 USB/局域网测试机注册成平台设备 + 容器（POC 联调用）。

设备：192.168.10.121:5555（adb tcpip 模式，Xiaomi 2509FPN0BC / Android 14）
用途：本地 MediaMTX + Agent + WHEP 群控窗联调；公网设备无法推流到本地 MediaMTX。

运行：
    python seed_local_emulator.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault('DB_PATH', os.path.join(ROOT, 'data', 'data.db'))

import db  # noqa: E402

DEVICE_IP = '192.168.10.121'
ADB_PORT = '5555'
DEVICE_NAME = '本地测试机 (2509FPN0BC)'
CONTAINER_ID = 'local-emulator-5554'
CONTAINER_NAME = 'emulator-5554'


def main():
    db.init_db()
    dev = db.row('SELECT * FROM devices WHERE ip=?', (DEVICE_IP,))
    if dev:
        dev_id = dev['id']
        db.update_device(dev_id, port=ADB_PORT, url=f'http://{DEVICE_IP}:{ADB_PORT}',
                         name=DEVICE_NAME, screen_net='lan', enabled=1)
        print(f'[seed] 复用设备 id={dev_id} {DEVICE_IP}:{ADB_PORT}')
    else:
        dev_id = db.add_device(DEVICE_IP, ADB_PORT, f'http://{DEVICE_IP}:{ADB_PORT}',
                               DEVICE_NAME, 'POC 本地联调设备', 'lan')
        db.update_device(dev_id, enabled=1, status='online', container_count=1)
        print(f'[seed] 新增设备 id={dev_id} {DEVICE_IP}:{ADB_PORT}')

    db.upsert_container({
        'id': CONTAINER_ID,
        'device_id': dev_id,
        'device_ip': DEVICE_IP,
        'name': CONTAINER_NAME,
        'index_num': 1,
        'short_id': 'local',
        'adb_port': ADB_PORT,
        'adb_addr': f'{DEVICE_IP}:{ADB_PORT}',
        'image': 'local-device',
        'state': 'running',
        'status': 'Up (adb tcpip)',
        'and_state': 1,
        'api_port': '',
        'ctr_port': '',
        'webrtc': '',
        's5_addr': '',
        's5_status': 0,
        'resolution': '404x720',
        'data_path': '',
    })
    db.update_container(CONTAINER_ID, screen_status='ok',
                        agent_status='unknown', agent_stream_name='')
    db.update_device(dev_id, status='online', container_count=1)
    print(f'[seed] 容器 {CONTAINER_ID} -> adb {DEVICE_IP}:{ADB_PORT}')
    print('[seed] 完成。群控窗刷新后应能看到该设备。')


if __name__ == '__main__':
    sys.exit(main())
