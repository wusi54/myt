# -*- coding: utf-8 -*-
"""魔云腾(MYT) SDK WEB API 客户端封装。

接口文档: 每个设备的 API base (参见 devices.url) + /docs
参考文档: http://113.105.100.58:83/docs
"""
import json
import requests

DEFAULT_TIMEOUT = 30

# set_custom_devinfo 支持的 query 字段
DEVINFO_QUERY_FIELDS = ['androidId', 'iccid', 'imei', 'imsi', 'seriesNum', 'btaddr',
                        'btname', 'wifiMac', 'wifiName', 'oaid', 'aaid', 'vaid']

# 真机参数库支持的完整字段（用于解析上传文件）
DEVINFO_FIELDS = [
    'brand', 'model', 'device', 'manufacturer', 'board', 'hardware',
    'build_id', 'build_fingerprint', 'build_tags', 'build_type',
    'build_version_release', 'build_version_sdk', 'build_description',
    'serialno', 'serialNum', 'mac', 'imei', 'meid', 'imsi', 'android_id', 'androidId',
    'wifi_mac', 'wifiMac', 'bt_mac', 'btaddr', 'btname', 'baseband_version',
    'bootloader', 'product', 'product_name', 'product_device',
    'lac', 'cid', 'lat', 'lon', 'mcc', 'mnc', 'phonenumber',
    'country', 'language', 'timezone', 'opercode', 'opername', 'iccid', 'gaid',
    'wifiName', 'oaid', 'aaid', 'vaid',
]


def normalize_base(url, ip='', port='83'):
    """把设备配置解析成可用的 API base。"""
    url = (url or '').strip()
    if url:
        if not url.startswith('http://') and not url.startswith('https://'):
            url = 'http://' + url
        return url.rstrip('/')
    if ip:
        return f"http://{ip}:{port or '83'}"
    return ''


class MytClient:
    def __init__(self, base, timeout=DEFAULT_TIMEOUT):
        self.base = (base or '').rstrip('/')
        self.timeout = timeout

    # ---------- 底层 ----------
    def get(self, path, params=None, timeout=None):
        if not self.base:
            return {'code': 500, 'msg': 'api base 未配置'}
        try:
            r = requests.get(f"{self.base}{path}", params=params or {},
                             timeout=timeout or self.timeout)
            return r.json() if r.text else {'code': 500, 'msg': 'empty response'}
        except Exception as e:
            return {'code': 500, 'msg': str(e)}

    def post(self, path, params=None, data=None, files=None, timeout=None):
        if not self.base:
            return {'code': 500, 'msg': 'api base 未配置'}
        try:
            r = requests.post(f"{self.base}{path}", params=params or {}, json=data,
                              files=files, timeout=timeout or self.timeout)
            return r.json() if r.text else {'code': 500, 'msg': 'empty response'}
        except Exception as e:
            return {'code': 500, 'msg': str(e)}

    # ---------- 主机 ----------
    def query_myt(self):
        """获取当前网络在线设备列表 -> {ip/24: devicecode}"""
        return self.get('/host_api/v1/query_myt')

    def host_ver(self, ip):
        return self.get(f'/dc_api/v1/get_host_ver/{ip}')

    def systeminfo(self, ip):
        return self.get(f'/host_api/v1/get_systeminfo/{ip}')

    def get_devinfo(self):
        """获取机型字典表 {brand: {model: id}}"""
        return self.get('/get_devinfo')

    # ---------- 容器 ----------
    def list_containers(self, ip):
        return self.get(f'/dc_api/v1/list/{ip}')

    def android_detail(self, ip, name):
        return self.get(f'/dc_api/v1/get_android_detail/{ip}/{name}')

    def boot_status(self, ip, name):
        return self.get(f'/dc_api/v1/get_android_boot_status/{ip}/{name}')

    def start(self, ip, name):
        return self.get(f'/dc_api/v1/run/{ip}/{name}')

    def stop(self, ip, name):
        return self.get(f'/dc_api/v1/stop/{ip}/{name}')

    def reset(self, ip, name):
        return self.get(f'/dc_api/v1/reset/{ip}/{name}')

    def reboot(self, ip, name):
        return self.get(f'/dc_api/v1/reboot/{ip}/{name}')

    def remove(self, ip, name):
        return self.get(f'/dc_api/v1/remove/{ip}/{name}')

    def rename(self, ip, old_name, new_name):
        return self.get(f'/dc_api/v1/rename/{ip}/{old_name}/{new_name}')

    def create(self, ip, index, name, **opts):
        return self.post(f'/dc_api/v1/create/{ip}/{index}/{name}', params=opts)

    def update(self, ip, name, **opts):
        return self.post(f'/dc_api/v1/update/{ip}/{name}', params=opts)

    # ---------- 设备信息 / 真机参数 ----------
    def set_custom_devinfo(self, ip, name, data):
        """自定义设备参数信息。data 为字段字典。"""
        params = {}
        for k in DEVINFO_QUERY_FIELDS:
            if data.get(k):
                params[k] = data[k]
        # 完整字段放进 devinfo body
        body_fields = {k: v for k, v in data.items() if v not in (None, '')}
        payload = {'devinfo': json.dumps(body_fields, ensure_ascii=False)}
        return self.post(f'/and_api/v1/set_custom_devinfo/{ip}/{name}',
                         params=params, data=payload)

    def random_devinfo(self, ip, name, **opts):
        return self.get(f'/and_api/v1/random_devinfo/{ip}/{name}', params=opts)

    def random_devinfo_async(self, ip, name, **opts):
        return self.get(f'/and_api/v1/random_devinfo_async/{ip}/{name}', params=opts)

    def upload_fingerprint(self, ip, name, params):
        keys = ['lac', 'cid', 'lat', 'lon', 'mcc', 'mnc', 'phonenumber', 'country',
                'language', 'timezone', 'opercode', 'opername', 'iccid', 'imsi', 'imei', 'gaid']
        p = {k: params[k] for k in keys if params.get(k)}
        return self.get(f'/and_api/v1/upload_fingerprint/{ip}/{name}', params=p)

    # ---------- 代理 ----------
    def s5_set(self, ip, name, s5ip, s5port, s5user='', s5pwd='', domain_mode=2):
        return self.get(f'/and_api/v1/s5_set/{ip}/{name}', params={
            's5ip': s5ip, 's5port': s5port, 's5user': s5user,
            's5pwd': s5pwd, 'domain_mode': domain_mode})

    def s5_query(self, ip, name):
        return self.get(f'/and_api/v1/s5_query/{ip}/{name}')

    def s5_stop(self, ip, name):
        return self.get(f'/and_api/v1/s5_stop/{ip}/{name}')

    # ---------- 应用 ----------
    def shell(self, ip, name, cmd):
        return self.post(f'/and_api/v1/shell/{ip}/{name}', data={'cmd': cmd})

    def install_apk(self, ip, name, apk_path, filename, timeout=300):
        """上传 APK 到容器并 pm install。返回 (status, message)。"""
        try:
            with open(apk_path, 'rb') as f:
                r = requests.post(
                    f"{self.base}/and_api/v1/upload_file_android/{ip}/{name}",
                    files={'file': (filename, f, 'application/octet-stream')},
                    timeout=timeout)
            resp = r.json() if r.text else {}
            if resp.get('code') != 200:
                return 'failed', f"upload failed: {json.dumps(resp, ensure_ascii=False)}"
            src = f'/data/media/0/Download/{filename}'
            dst = f'/data/local/tmp/{filename}'
            r2 = self.shell(ip, name, f'cp "{src}" "{dst}" && chmod 644 "{dst}" && echo OK')
            if 'OK' not in (r2.get('message') or ''):
                return 'failed', f"copy failed: {(r2.get('message') or '')[:200]}"
            r3 = self.shell(ip, name, f'pm install -r "{dst}" 2>&1')
            msg = (r3.get('message') or '')[:500]
            if 'success' in msg.lower():
                return 'success', msg
            return 'failed', msg
        except Exception as e:
            return 'failed', str(e)
